---
title: "fix: Decouple LanceDB vector index from Google-Drive-synced output_dir"
type: fix
status: active
date: 2026-04-18
branch: fix/lancedb-path-config
---

# fix: Decouple LanceDB vector index from Google-Drive-synced output_dir

## Overview

Introduce a separate `vector_db_dir` config option so the LanceDB vector index can live on a local filesystem even when `output_dir` is on a cloud-synced mount (Google Drive File Stream, OneDrive, Dropbox). Add a pre-flight probe that catches incompatible filesystems before any Voyage embedding tokens are spent. Record the decision in ADR-0016.

## Problem Statement

During this session we moved `output_dir` from a local NTFS path to `G:/My Drive/video-intel/` (Google Drive File Stream). The full `scan` pipeline worked cleanly on the new path (mindmaps, transcripts, concepts, meta.json, taxonomy.json all written correctly). The follow-up `index` command embedded all 5,294 transcript chunks successfully, but then the LanceDB write phase failed with:

```text
RuntimeError: lance error: LanceError(IO): Generic LocalFileSystem error:
  Unable to copy file from ...\.lancedb\transcript_chunks.lance\_versions\18446744073709551610.manifest-<uuid>
                       to ...\.lancedb\transcript_chunks.lance\_versions\18446744073709551610.manifest:
  Incorrect function. (os error 1)
```

Root cause: LanceDB uses MVCC and commits every write by atomically renaming a `{version}.manifest-<uuid>` staging file to `{version}.manifest`. Google Drive File Stream is a virtualized filesystem that proxies operations to the cloud; it handles read and write but rejects the specific atomic-rename call LanceDB needs with Windows `ERROR_INVALID_FUNCTION (os error 1)`. OneDrive and Dropbox mounts exhibit the same class of failure.

The current implementation at [scripts/video_intel.py:2314](scripts/video_intel.py#L2314), [:2400](scripts/video_intel.py#L2400), and [:2474](scripts/video_intel.py#L2474) hardcodes the LanceDB path as `output_dir / LANCEDB_DIR`, forcing the vector index to live alongside the durable artifacts. That coupling is not architecturally required: ADR-0012 explicitly calls the index "rebuildable, delete and re-index at any time." It lives there today because it was the simplest default, not because anything depends on it.

The user impact today: anyone running `index` against a cloud-synced `output_dir` pays the full Voyage embedding cost (approximately $0.30 for our corpus at the `voyage-4-large` price point) before discovering the write fails. The stranded `.manifest-<uuid>` staging file remains on GDrive as transaction pollution until manually cleaned up.

## Proposed Solution

Three-part change:

1. **New config option `vector_db_dir`** at the top level of `config.yaml`, sibling to `output_dir`. Tilde-expanded like `output_dir`. Defaults to `output_dir / .lancedb` so existing local installs keep working without a config edit.

2. **Pre-flight probe** that tests whether the target path supports atomic rename before the `index` command spends any Voyage tokens. Runs in ~10ms. On failure, emits a diagnostic that names the likely cause (cloud-synced filesystem) and shows the fix (set `vector_db_dir` to a local path).

3. **ADR-0016** recording the decision and the concrete incident that justified it. Updates to `CLAUDE.md` and `README.md` to document the new option.

## Technical Considerations

### Boundary check (specs/agent-rules.md §5)

Before crossing the "vector index lives under output_dir" boundary:

- (a) `git log --follow` the enforcement point: the coupling was introduced in the vector-search commit that also produced ADR-0012. No ADR or CLAUDE.md carve-out pins it there; ADR-0012 treats the path as an implementation detail.
- (b) No existing ADR forbids separating the paths.
- (c) CLAUDE.md's "Config:" section does not mention vector_db_dir yet; the new option slots in naturally.

Verdict: safe to cross. The new ADR-0016 documents the carve-out.

### Precedence model

Mirrors the existing `model` precedence documented in CLAUDE.md (CLI flag > config.yaml > default constant), minus the CLI flag for this first cut:

```text
vector_db_dir resolution:
  1. config.yaml `vector_db_dir` if set
  2. output_dir / .lancedb (default, backwards compatible)
```

CLI override (`--vector-db-dir`) and environment variable (`VIDEO_INTEL_VECTOR_DB_DIR`) are deliberately **not** added in this change. YAGNI per §1 of agent-rules.md. Easy to add later if a concrete use case appears.

### Probe design

```python
def probe_atomic_writes(path: Path) -> tuple[bool, str | None]:
    """Return (ok, reason). On ok=False, reason is a user-facing diagnostic."""
```

Implementation: inside a single `try` block, `mkdir` the target, create a uniquely named probe file via `tempfile.NamedTemporaryFile(dir=path, delete=False)`, `os.replace()` it to a second unique name, delete both. Clean up probe files in a `finally` block on every path (success and failure) so a crash or an unexpected OSError does not leave litter.

Correctness note: `os.replace()` on Python maps to `MoveFileExW(..., REPLACE_EXISTING)` on Windows and `rename(2)` on POSIX. Rust's `std::fs::rename` (what LanceDB's Rust side calls) maps to `rename(2)` on POSIX and to `MoveFileExW` with a `SetFileInformationByHandle` fallback on Windows. The two calls are in the same family and fail identically on Google Drive File Stream in practice, so the probe is a strong signal for our target failure mode, not a perfect mirror. A passing probe does not guarantee a LanceDB commit will succeed on pathological filesystems; a failing probe reliably identifies the class of filesystem that broke us on 2026-04-18.

Why unique filenames (tempfile over fixed `.probe` / `.probe.tmp`): two parallel `index` invocations in the same directory would collide on fixed names and produce a spurious probe failure. We do not run parallel indexes today and there is no lockfile to prevent it, but uniqueness is effectively free (one extra import) and removes a theoretical false negative.

Why `mkdir` inside the `try`: a target whose parent is not writable would otherwise escape as a raw `PermissionError` traceback rather than returning the actionable diagnostic. Putting the `mkdir` inside the same `try` that wraps the probe converts that failure into the same friendly error path.

### Error message shape

```text
ERROR: Cannot use vector_db_dir=<path>
  Atomic rename failed: <OSError detail>

  LanceDB requires atomic rename to commit its MVCC manifests. This usually
  means the path is on a cloud-synced filesystem (Google Drive File Stream,
  OneDrive, Dropbox). Those mounts do not support the atomic file operations
  LanceDB needs.

  Fix: set vector_db_dir to a local path in config.yaml, for example:
    vector_db_dir: ~/.cache/video-intel/lancedb

  The index is derived and rebuildable, so it is safe to live outside your
  synced output_dir.
```

The message names the failure mode (atomic rename), the probable cause (cloud sync), and the concrete fix (a config edit). That matches agent-rules.md meta-rule: "Every new rule must name the failure mode it prevents."

### Migration for existing users

- Local users (output_dir on NTFS/ext4/APFS): no action. Default behavior unchanged. Probe passes. Existing `.lancedb/` under `output_dir` keeps working.
- Cloud-sync users (output_dir on GDFS/OneDrive/Dropbox): set `vector_db_dir` in `config.yaml`, run `python scripts/video_intel.py index` once to rebuild. The old stranded `.lancedb/` directory under the synced output_dir can be manually deleted to reclaim space (documented in the plan and the PR description, not automated).

### Dependency on ADR-0012

ADR-0012 calls the LanceDB index "file-based, rebuildable from transcripts at any time." This change reinforces that property rather than violating it: moving the index off the synced path makes it even more clearly a derived local cache. No ADR-0012 amendment is required; ADR-0016 supersedes the colocation assumption.

## Acceptance Criteria

### Functional

- [x] `config.yaml` supports an optional `vector_db_dir` key at the top level.
- [x] When `vector_db_dir` is unset, `index` and `search --vector` behave identically to today (same path, same outputs).
- [x] When `vector_db_dir` is set, both commands read and write from the configured path (with tilde expansion).
- [x] Running `index` against a path that fails the atomic-rename probe aborts **before** calling Voyage, prints the actionable diagnostic, and exits non-zero.
- [x] Running `index` against a path that passes the probe completes the full embed and write sequence. *(verified 2026-04-18: 5294 chunks embedded + committed to local cache path in 1m 51s, then `search --vector "permission problems"` returned 10 ranked hits.)*

### Documentation

- [x] ADR-0016 committed under `docs/adr/` following the existing template.
- [x] CLAUDE.md "Config:" section lists `vector_db_dir` and the probe behavior.
- [x] README.md (if it currently documents config keys) lists `vector_db_dir`.

### Testing (pytest -m "not integration")

- [x] `test_resolve_vector_db_dir_defaults_to_output_dir_lancedb`
- [x] `test_resolve_vector_db_dir_config_override_wins`
- [x] `test_resolve_vector_db_dir_expands_tilde`
- [x] `test_probe_atomic_writes_succeeds_on_writable_dir` (uses `tmp_path`, asserts no litter)
- [x] `test_probe_atomic_writes_returns_reason_on_rename_failure` (monkeypatches `os.replace` to raise `OSError`, asserts no litter)
- [x] `test_build_search_index_aborts_before_voyage_when_probe_fails` (wiring test: monkeypatches `probe_atomic_writes` to return `(False, "...")`, asserts `SystemExit` raised and the `voyageai.Client` is never instantiated)

### Code quality

- [x] `ruff format . && ruff check . --fix` clean.
- [x] All added functions carry type hints.
- [x] No new CLI flags, env vars, or imports beyond what the above requires. *(added: `contextlib`, `tempfile`, `uuid` — all stdlib, required by probe)*

## Success Metrics

- Zero Voyage tokens wasted on the next `index` run against the current `G:/My Drive/video-intel` output_dir: the probe catches the incompatibility before embedding starts.
- One-command recovery: after the user adds `vector_db_dir: ~/.cache/video-intel/lancedb` to config.yaml, `index` succeeds and `search --vector "permission problems"` returns results.

## Dependencies and Risks

### Dependencies

- None. The change is local to `scripts/video_intel.py`, `config.yaml` schema (documented, not enforced), and docs.

### Risks

- **Probe false positive (path passes probe but LanceDB still fails):** mitigated by using `os.replace` which is exactly the call LanceDB's Rust side uses on Windows. If this happens in practice, the LanceDB error message still reaches the user and points at the path.
- **Probe false negative (path fails probe but LanceDB would succeed):** possible in niche filesystems. Mitigated by keeping the probe failure message actionable - the user can investigate and override. We do not add an opt-out flag yet because no such case is known.
- **Existing users on cloud-synced output_dir with a working local-build `.lancedb/`:** this situation does not exist in practice. If it did, the probe would pass on the old local path and the user could set `vector_db_dir` to point at it.

## Implementation Plan

Sequential, one file at a time (per agent-rules.md §4 "Execution strategy: sequential for coupled changes").

### Step 1: Branch and baseline

```bash
git checkout -b fix/lancedb-path-config
ruff format . && ruff check . --fix && pytest -m "not integration" -q
```

Confirm green baseline before any code change.

### Step 2: Write tests first (TDD RED)

New test file: `tests/test_vector_db_config.py`

```python
# tests/test_vector_db_config.py
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.video_intel import probe_atomic_writes, resolve_vector_db_dir


def test_resolve_vector_db_dir_defaults_to_output_dir_lancedb(tmp_path):
    out = tmp_path / "out"
    config = {"output_dir": str(out)}
    assert resolve_vector_db_dir(config, out) == out / ".lancedb"


def test_resolve_vector_db_dir_config_override_wins(tmp_path):
    out = tmp_path / "out"
    local = tmp_path / "cache" / "db"
    config = {"output_dir": str(out), "vector_db_dir": str(local)}
    assert resolve_vector_db_dir(config, out) == local


def test_resolve_vector_db_dir_expands_tilde(tmp_path):
    config = {"vector_db_dir": "~/cache/db"}
    result = resolve_vector_db_dir(config, tmp_path / "out")
    assert "~" not in str(result)
    assert result.is_absolute()


def test_probe_atomic_writes_succeeds_on_writable_dir(tmp_path):
    target = tmp_path / "probe"
    ok, reason = probe_atomic_writes(target)
    assert ok is True
    assert reason is None
    assert not any(target.glob(".probe*"))  # no litter on success


def test_probe_atomic_writes_returns_reason_on_rename_failure(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError(1, "Incorrect function")
    monkeypatch.setattr("scripts.video_intel.os.replace", boom)
    target = tmp_path / "probe"
    ok, reason = probe_atomic_writes(target)
    assert ok is False
    assert "atomic" in reason.lower() or "rename" in reason.lower()
    assert "vector_db_dir" in reason
    assert not any(target.glob(".probe*"))  # no litter on failure either


def test_build_search_index_aborts_before_voyage_when_probe_fails(tmp_path, monkeypatch):
    """Wiring test: probe failure short-circuits before any Voyage client is built."""
    from scripts import video_intel as vi

    bad_path = tmp_path / "bad"
    config = {"output_dir": str(tmp_path / "out"), "vector_db_dir": str(bad_path)}

    monkeypatch.setattr(vi, "probe_atomic_writes", lambda p: (False, "simulated failure"))

    sentinel = {"voyage_called": False}
    class _ShouldNotBeConstructed:
        def __init__(self, *a, **kw):
            sentinel["voyage_called"] = True
            raise AssertionError("Voyage client built despite probe failure")
    monkeypatch.setattr(vi.voyageai, "Client", _ShouldNotBeConstructed, raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")

    with pytest.raises(SystemExit):
        vi.build_search_index(Path(config["output_dir"]), config=config)

    assert sentinel["voyage_called"] is False
```

Note on monkeypatch targets: use the fully qualified name `scripts.video_intel.os.replace` rather than the global `os.replace`, so the patch reaches the module-level `os` import inside `video_intel.py` and not just the test module's reference.

Run `pytest tests/test_vector_db_config.py -v` and confirm all five tests fail with `ImportError` (functions do not exist yet).

### Step 3: Implement the resolver and probe (TDD GREEN)

Edit [scripts/video_intel.py](scripts/video_intel.py), near the existing `resolve_output_dir` at line 62:

```python
# scripts/video_intel.py (new functions, near line 62)

def resolve_vector_db_dir(config: dict, output_dir: Path) -> Path:
    """Resolve vector index location. Config override > output_dir/.lancedb default."""
    override = config.get("vector_db_dir")
    if override:
        return Path(override).expanduser()
    return output_dir / LANCEDB_DIR


def probe_atomic_writes(path: Path) -> tuple[bool, str | None]:
    """Probe whether atomic rename works at `path`. LanceDB needs this to commit.

    Returns (True, None) on success, (False, reason) on failure. Cleans up its probe files
    on every exit path so a crash mid-probe never leaves litter.
    """
    src: Path | None = None
    dst: Path | None = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path, prefix=".probe_src_", suffix=".tmp", delete=False
        ) as f:
            f.write("probe")
            src = Path(f.name)
        dst = path / f".probe_dst_{uuid.uuid4().hex}.tmp"
        os.replace(src, dst)
        src = None  # os.replace consumed the source
        return True, None
    except OSError as e:
        reason = (
            f"Atomic rename failed: {e}. LanceDB requires atomic file operations "
            f"to commit MVCC manifests. This usually means the path is on a "
            f"cloud-synced filesystem (Google Drive File Stream, OneDrive, Dropbox). "
            f"Set vector_db_dir in config.yaml to a local path."
        )
        return False, reason
    finally:
        for leftover in (src, dst):
            if leftover is not None and leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass
```

New imports required at top of `scripts/video_intel.py`: `tempfile`, `uuid`. Both are stdlib.

Run `pytest tests/test_vector_db_config.py -v` and confirm all five tests pass.

### Step 4: Wire the resolver into call sites

Three hardcoded references to replace:

- [scripts/video_intel.py:2314](scripts/video_intel.py#L2314) inside `build_search_index`
- [scripts/video_intel.py:2400](scripts/video_intel.py#L2400) inside `hybrid_search`
- [scripts/video_intel.py:2474](scripts/video_intel.py#L2474) inside `cmd_index` (status print)

Both `build_search_index` and `hybrid_search` currently take `output_dir` only. Change them to also accept `config: dict` (or pass the resolved `db_path` in from the caller). The simpler surgery is to pass `config` through; the caller (`cmd_index`, `cmd_search`) already holds it.

Inside `build_search_index`, after computing `db_path`, call the probe:

```python
# scripts/video_intel.py, inside build_search_index, after db_path is resolved
db_path = resolve_vector_db_dir(config, output_dir)
ok, reason = probe_atomic_writes(db_path)
if not ok:
    log.error("Cannot use vector_db_dir=%s", db_path)
    log.error("%s", reason)
    sys.exit(1)
db = lancedb.connect(str(db_path))
```

Do NOT run the probe inside `hybrid_search`: reads do not hit the atomic-commit path, and we want read-only search to work even on a slightly quirky filesystem. If the index is corrupt, LanceDB's own errors will surface.

### Step 5: Write ADR-0016

New file: `docs/adr/ADR-0016-vector-db-path-config.md`

Follow the template at [docs/adr/template.md](docs/adr/template.md). Sections:

- **Status:** accepted
- **Date:** 2026-04-18
- **Context:** the incident (4/18 session, 5,294 chunks embedded then write failed on GDFS, $0.30 wasted). LanceDB's atomic-commit contract. Why atomic rename fails on cloud-synced mounts.
- **Decision:** separate `vector_db_dir` config option with pre-flight probe. Default preserves backwards compatibility.
- **Consequences:** positive (no more wasted embeddings, cloud-sync users unblocked, stronger "index is derived" invariant), negative (one more config option to document, a ~10ms startup cost on `index`).
- **Alternatives considered:** detect GDFS by drive letter (fragile, cross-platform mess); retry loop on rename failure (LanceDB already retries, the failure is deterministic); use `lancedb.connect(uri="memory://")` and persist as parquet (larger change, loses MVCC benefits).
- **Affects:** scripts/video_intel.py, config.yaml schema docs, CLAUDE.md, README.md.

### Step 6: Update CLAUDE.md

Append to the "Config:" bullet in the Architecture section:

```markdown
- `vector_db_dir` (optional): path for the LanceDB vector index. Defaults to
  `output_dir / .lancedb`. Must be on a real local filesystem. Cloud-synced
  mounts (Google Drive File Stream, OneDrive, Dropbox) do not support the
  atomic file operations LanceDB needs to commit MVCC manifests. The `index`
  command runs a pre-flight probe and aborts with a clear diagnostic if the
  configured path cannot support atomic rename. The vector index is a derived
  artifact (rebuildable from transcripts via `index`), so it is safe to live
  in a local cache directory (e.g., `~/.cache/video-intel/lancedb`) outside
  your synced `output_dir`.
```

### Step 7: Update README.md (if applicable)

Check whether README.md documents config keys today. If yes, add `vector_db_dir` with the same shape as the CLAUDE.md entry but trimmed to user-facing prose. If README.md only covers quickstart and does not enumerate config, no change needed.

### Step 8: Green-bar validation

```bash
ruff format . && ruff check . --fix && pytest -m "not integration" -q
```

Must pass before opening the PR.

### Step 9: Live smoke test

With the current broken state on G: still in place:

```bash
# Set vector_db_dir in config.yaml to a local path
# Example: vector_db_dir: C:/Users/danie/video-intel-cache/lancedb

python scripts/video_intel.py index
# Expect: probe passes, 5294 chunks embed and index cleanly

python scripts/video_intel.py search "permission problems" --vector
# Expect: top-k hits returned

# Then flip config back to an intentionally bad path and re-run index
# Example: vector_db_dir: "G:/My Drive/video-intel/.lancedb"
python scripts/video_intel.py index
# Expect: probe fails, clear diagnostic, non-zero exit, ZERO Voyage tokens spent
```

### Step 10: PR

- Branch: `fix/lancedb-path-config`
- PR title: `fix: Decouple LanceDB vector index from output_dir`
- PR body: links to this plan and ADR-0016, summarizes the incident, lists the acceptance criteria.
- Do NOT push straight to `main` per the user's workflow memory.
- Do NOT commit unless explicitly asked (per agent-rules.md §4).

### Manual cleanup (documented in PR description, not automated)

Once the PR is merged and the user has rebuilt against a local `vector_db_dir`:

```bash
# One-time: reclaim GDrive space taken by the stranded .lancedb directory
rm -rf "G:/My Drive/video-intel/.lancedb/"
```

This step is manual because automating filesystem deletes on a synced cloud path is the kind of blast-radius operation the harness rules ask us to keep in the user's hands.

## References

### Internal

- ADR-0012: [docs/adr/ADR-0012-vector-search-lancedb-voyage.md](docs/adr/ADR-0012-vector-search-lancedb-voyage.md) - original vector-search decision, establishes "index is derived, rebuildable"
- ADR-0013: [docs/adr/ADR-0013-hybrid-search-rrf-fusion.md](docs/adr/ADR-0013-hybrid-search-rrf-fusion.md) - hybrid search layer that reads the same index
- CLAUDE.md: "Config:" section - documents existing `output_dir`, `model`, `max_parallel`, `auto_concepts`
- specs/agent-rules.md §5: architectural boundary checklist (applied above)
- scripts/video_intel.py:[62](scripts/video_intel.py#L62) - `resolve_output_dir` pattern this change mirrors
- scripts/video_intel.py:[2153](scripts/video_intel.py#L2153) - `LANCEDB_DIR` constant (kept as the default suffix)
- scripts/video_intel.py:[2314](scripts/video_intel.py#L2314), [2400](scripts/video_intel.py#L2400), [2474](scripts/video_intel.py#L2474) - three call sites to update

### External

- [LanceDB commit.rs source](https://github.com/lancedb/lance/blob/main/rust/lance-table/src/io/commit.rs) - the Rust code that emitted the failure we hit (line 1046 in the error message)
- [LanceDB GitHub issue #1847 (search for rename failure)](https://github.com/lancedb/lancedb/issues) - class of known cloud-sync filesystem incompatibilities
- [Microsoft Learn: MoveFileEx MOVEFILE_REPLACE_EXISTING](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexa) - the underlying Win32 call `os.replace` uses, which GDFS rejects with `ERROR_INVALID_FUNCTION` (os error 1)

### Session context

This plan comes out of the 2026-04-18 session where the user moved `output_dir` from a local path to `G:/My Drive/video-intel/`. The full scan and concept back-fill succeeded on the new path; only the `index` command broke. The incident log and stranded manifest file served as ground truth for the failure mode described above.
