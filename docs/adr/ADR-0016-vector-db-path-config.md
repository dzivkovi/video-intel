# Decouple LanceDB Vector Index Path from output_dir

**Status:** accepted

**Date:** 2026-04-18

**Decision Maker(s):** Daniel Zivkovic

## Context

[ADR-0012](ADR-0012-vector-search-lancedb-voyage.md) introduced LanceDB as a
file-based vector store, placed by default inside `output_dir/.lancedb`. At
the time this was simply the path of least resistance: one config option
(`output_dir`), one folder tree, one backup target. ADR-0012 describes the
index as "file-based, no server, rebuildable from transcripts at any time."

On 2026-04-18 the user moved `output_dir` from a local NTFS path to
`G:/My Drive/video-intel/` (Google Drive File Stream). The full `scan`
pipeline worked cleanly on the new path: mindmaps, transcripts, concept
extraction, meta.json, and taxonomy.json all wrote correctly. The follow-up
`index` command embedded all 5,294 transcript chunks successfully, then
failed on the LanceDB write phase with:

```text
RuntimeError: lance error: LanceError(IO): Generic LocalFileSystem error:
  Unable to copy file from ...\.lancedb\transcript_chunks.lance\_versions\
    18446744073709551610.manifest-<uuid>
    to ...\.lancedb\transcript_chunks.lance\_versions\
    18446744073709551610.manifest:
  Incorrect function. (os error 1)
```

Root cause: LanceDB uses MVCC and commits every write by atomically renaming
a `{version}.manifest-<uuid>` staging file to `{version}.manifest`. Google
Drive File Stream is a virtualized filesystem that proxies operations to the
cloud; it handles read and write but rejects the specific atomic-rename call
LanceDB needs with Windows `ERROR_INVALID_FUNCTION (os error 1)`. OneDrive
and Dropbox mounts exhibit the same class of failure.

Two concrete costs were paid in the incident:

1. **Wasted embeddings.** Approximately $0.30 of Voyage `voyage-4-large`
   tokens (5,294 chunks) embedded before the write failed. Every re-run
   against the same broken path repeats the charge.
2. **Transaction pollution.** The stranded `.manifest-<uuid>` staging file
   remains on GDrive until manually cleaned up.

The coupling lives at three call sites in `scripts/video_intel.py` where
`output_dir / LANCEDB_DIR` is hardcoded. No ADR, `CLAUDE.md` carve-out, or
test pins the index to that location - ADR-0012 explicitly calls it
"rebuildable, delete and re-index at any time," which is the opposite of a
durable-artifact invariant.

## Decision

Introduce `vector_db_dir` as an optional top-level config key in
`config.yaml`, sibling to `output_dir`. Add a pre-flight atomic-rename probe
to `build_search_index` that runs before any Voyage tokens are spent.

### Precedence

```text
vector_db_dir resolution:
  1. config.yaml `vector_db_dir` if set (tilde-expanded)
  2. output_dir / LANCEDB_DIR (default - backwards compatible)
```

No CLI flag, no environment variable. Easy to add later if a concrete use
case appears; YAGNI per agent-rules.md §1.

### Probe design

`probe_atomic_writes(path: Path) -> tuple[bool, str | None]` uses **LanceDB
itself as the oracle**:

1. `mkdir(parents=True, exist_ok=True)` on the target path.
2. Connect LanceDB to a uuid-named throwaway subdir under the target.
3. `create_table` a 1-row table, then `drop_table`.
4. `shutil.rmtree(probe_dir, ignore_errors=True)` in `finally`.

Returns `(True, None)` if the round-trip completes, `(False, reason)` if
LanceDB raises `OSError` or `RuntimeError` (the latter wraps lance-table
IO errors).

### Why an integration probe, not a mechanism probe

The initial design (plan rev 1) used a file-level mechanism probe:
`os.replace` of a stdlib tempfile to a unique name, then later
`os.replace`-over-existing after a rev 2 iteration. **Both iterations
produced false negatives on Google Drive File Stream** during live smoke
tests on 2026-04-18:

- Rev 1 (`os.replace` to new name): probe passed, LanceDB still failed,
  ~$0.30 of Voyage tokens wasted on the 5,294-chunk embed that was
  discarded.
- Rev 2 (`os.replace` over existing destination): probe passed again, same
  failure, another ~$0.30 wasted.

Root cause of the false negatives: the failure is inside LanceDB's Rust
commit path (`lance-table-4.0.0/src/io/commit.rs:1046`) going through the
`object_store` crate's `LocalFileSystem` implementation. The failing
Windows call is in a different syscall family than Python's
`os.replace`/`MoveFileExW` - the observed error says "Unable to **copy**
file from ... to ..." which suggests an internal `fs::copy` or
object-store-specific path, not a plain rename. GDFS tolerates
Python-level `MoveFileExW` but rejects whatever LanceDB's Rust side
calls.

A mechanism probe is only predictive when the probe call and the real
call share the same filesystem code path. Once we saw empirically that
they do not, the only reliable oracle is LanceDB itself. The integration
probe costs ~100-300ms per `index` invocation, which is a trivial tax
compared to the ~$0.30 + 30s of embedding work it saves on a failed write.

The regression guard `test_probe_uses_integration_oracle_not_mechanism_probe`
asserts that `lancedb.connect` is called during the probe so a future
"simplification" back to a mechanism probe fails loudly.

### Probe placement

Probe runs inside `build_search_index` before any `voyageai.Client()`
construction. It does **not** run inside `hybrid_search` (the read path):
reads do not hit the atomic-commit path, and a read-only search should work
even on a slightly quirky filesystem. If the index itself is corrupt,
LanceDB's own errors surface.

## Consequences

### Positive Consequences

- **Zero wasted embeddings** on the next `index` run against an
  incompatible filesystem. The probe catches the problem in ~10ms before
  Voyage is ever called.
- **Clear diagnostic** naming the failure mode (atomic rename), probable
  cause (cloud sync), and concrete fix (a one-line config edit).
- **Reinforces the "index is derived" invariant** from ADR-0012. Moving the
  index off a synced path makes it even more clearly a derived local cache.
- **Backwards compatible**: existing local installs with no `vector_db_dir`
  set keep working. Default behavior is unchanged.
- **No new dependencies**: `tempfile` and `uuid` are stdlib.

### Negative Consequences

- **One more config option** to document in `CLAUDE.md`, `README.md`, and
  `config.yaml.example` (when it exists).
- **~100-300ms startup cost** on every `index` invocation. Probe does a
  full LanceDB connect + create + drop round-trip. This is the correctness
  tax for the integration-oracle approach; see "Why an integration probe"
  above for why a cheaper mechanism probe was tried and rejected.
- **Probe is a near-perfect LanceDB oracle but still fallible**: false
  negatives (probe fails but LanceDB would succeed) remain theoretically
  possible on filesystems that behave differently for 1-row tables than
  for full-sized ones. No such case has been observed. No opt-out flag
  is added yet; if one appears, the user can point `vector_db_dir`
  elsewhere.

## Alternatives Considered

- **File-level mechanism probe using `os.replace` (rev 1 and rev 2).**
  - Pros: ~10ms cost, no LanceDB dependency during probing, pure stdlib.
  - Cons: Empirically produces false negatives on GDrive File Stream.
    Rev 1 (`os.replace` to new name) and rev 2 (`os.replace` over
    existing destination) both let the probe pass while LanceDB's Rust
    commit failed, wasting ~$0.30 of Voyage tokens per run. See "Why
    an integration probe" above.
  - **Status:** rejected after live smoke-test falsification.

- **Detect GDFS by drive letter or mount type.**
  - Pros: Zero-config for GDrive users on Windows.
  - Cons: Fragile across OSes (different mount semantics on macOS/Linux),
    does not cover OneDrive/Dropbox/Box without per-vendor heuristics, and
    can still miss user-mounted network shares that behave identically.
  - **Status:** rejected.

- **Retry loop on rename failure.**
  - Pros: Minimal code change. Familiar pattern.
  - Cons: LanceDB's Rust side already retries internally. The failure is
    deterministic on GDrive File Stream - more retries just burn more time
    and do not change the outcome.
  - **Status:** rejected.

- **Switch LanceDB to a memory-backed store and persist as parquet.**
  - Pros: Sidesteps the rename contract entirely.
  - Cons: Loses MVCC benefits (consistent snapshots during concurrent
    reads), larger change surface, different performance profile,
    disk-layout migration for existing indices.
  - **Status:** rejected.

- **Hardcode `.cache/video-intel/lancedb` as the new default, drop
  colocation entirely.**
  - Pros: Simpler mental model (index always in user cache).
  - Cons: Breaks existing local installs that expect the index next to the
    data. Violates the "backwards compatible default" discipline.
  - **Status:** rejected in favor of the config override approach.

## Affects

Source files changed by this decision:

- `scripts/video_intel.py`:
  - `resolve_vector_db_dir()` (new)
  - `probe_atomic_writes()` (new)
  - `build_search_index()` (accepts `config`, calls probe before Voyage)
  - `hybrid_search()` (accepts `config`, uses resolver)
  - `cmd_index()` / `cmd_search()` (pass `config` through)
- `tests/test_vector_db_config.py` (new)
- `CLAUDE.md` (Config section documents `vector_db_dir`)
- `README.md` (if it enumerates config keys)

## Related Debt

- **Manual cleanup of the stranded `.lancedb/` on GDrive.** Documented in
  the PR description, not automated. Automating filesystem deletes on a
  cloud-synced path is the kind of blast-radius operation that belongs in
  the user's hands.

## Research References

- [Plan: 2026-04-18-fix-decouple-lancedb-path-plan.md](../plans/2026-04-18-fix-decouple-lancedb-path-plan.md)
- [ADR-0012: Vector Search via LanceDB + Voyage AI](ADR-0012-vector-search-lancedb-voyage.md)
- [ADR-0013: Hybrid Search RRF Fusion](ADR-0013-hybrid-search-rrf-fusion.md)
- [LanceDB commit.rs](https://github.com/lancedb/lance/blob/main/rust/lance-table/src/io/commit.rs) - the Rust side that emits the observed error.
- [Microsoft Learn: MoveFileEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexa) - the underlying Win32 call `os.replace` uses on Windows, which GDFS rejects with `ERROR_INVALID_FUNCTION` (os error 1).

## Notes

This ADR reinforces rather than supersedes ADR-0012. The original decision
("LanceDB, file-based, rebuildable") is still in force; ADR-0016 only
clarifies that the *location* of those files is a user-tunable concern
when the output_dir host cannot support LanceDB's atomic-commit contract.
