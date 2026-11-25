---
title: One lock per corpus so a second writing command waits instead of racing - Plan
type: feat
date: 2026-11-24
issue: 303
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# One lock per corpus so a second writing command waits instead of racing - Plan

## Goal Capsule

- **Objective:** two overlapping corpus-writing commands produce the same corpus as running them one after the other; no reader is ever delayed by a writer; a killed writer never leaves the corpus locked (the next writer acquires within seconds).
- **Means:** a new standard-library module `scripts/corpus_lock.py` holds one OS-level lock per corpus in a local user-state folder; `main()` takes it right after config resolution for every run that writes, holds it through dispatch and the topic-stamp flush, and a second writer waits in line (or exits 4 with `--no-wait`). Preview runs and readers never lock, never wait, and print one note when a writer is running. `nugget` and `briefings` get exclusive-create file names. The standalone scripts that write into a corpus they can name take the same lock at their entry points. Outside the guard, by design and documented: `wiki_atlas.py`, `remediation_run.py`'s own small writes (its child `process` commands lock per video), `atlas_render.py` and `atlas_export.py` when the atlas is not in a corpus `_atlas` folder, `intel_graph.py load` (reads the corpus, writes outside it), and library callers that bypass `main()`. The standalone script table below is the full list.
- **Authority:** the issue #303 text (option C, owner decided), the orchestrator decisions 1-17 and the orchestrator resolutions win over this plan; this plan wins on mechanism.
- **Delivery:** three phases (A module, B `video_intel.py` wiring, C standalone scripts and docs), each implemented, tested and committed on its own with the full suite green at its end.
- **Stop conditions:** stop and report if the Windows "held" errno observed by Phase A's test against the real call is neither `EACCES` nor `EDEADLOCK`; if any subprocess test cannot be made deterministic without a sleep as the only ordering mechanism; if a doc-review P0/P1 contradicts a decision.
- **Execution profile:** TDD; no paid or external call anywhere (every paid boundary is stubbed); no live corpus; subprocess tests use scratch corpora under `tmp_path` and assert that before running anything.

---

## Product Contract

### Summary

A writing command now runs inside `corpus_lock.hold_for_cli(...)` in `main()`. When the lock is free it is taken at once and nothing else changes. When another process holds it, the command logs "Waiting for the corpus lock: <holder details>." with a hint at once and again every 3 minutes, polls about once a second, and proceeds by itself when the lock frees; a command that waited re-reads its config before it does anything else. Ctrl-C while waiting prints one line saying nothing was written and exits 130. `--no-wait` (global, before the subcommand) exits 4 at once with the same details. `scan --dry-run` and every other preview mode take no lock and write no config snapshot. `search`, `status`, `nugget`, `briefings` and `profile show` never lock; when a writer is running they log one line naming the last recorded holder and suggesting a re-run afterwards.

### Requirements (numbered for tests)

- R1. `scripts/corpus_lock.py`, standard library only, never imports `video_intel` or any third-party package; covered by the subprocess import-isolation tests.
- R2. Three files per corpus in `lock_state_dir()`: `<key>.lock`, `<key>.busy`, `<key>.holder.json`, `key = sha256(normcase(realpath(output_dir)))[:16]`. Lock files are never unlinked; no takeover by elapsed time; no break-lock flag.
- R3. Re-entrant and thread-safe within one process (one OS acquisition per key, a depth count, a per-key thread lock held for the whole OS acquisition).
- R4. Acquisition is transactional, and a lock-system failure fails closed: one ERROR naming the path and the error, exit 1, nothing written, every descriptor closed.
- R5. ONE predicate `run_takes_corpus_lock(args)`: command in `CORPUS_LOCK_COMMANDS` (= `CONFIG_BACKUP_COMMANDS` plus `profile init`) and not a preview. ONE preview table `CORPUS_LOCK_PREVIEW`.
- R6. Acquisition in `main()` after `load_config()` and before the config snapshot; nothing before it reads corpus state; held through dispatch and the `flush_topic_stamps()` `finally`; outside the snapshot's warning-only `try`. A command that waited re-reads its config after acquisition.
- R7. Wait: the fixed wait line at once and every 180 s, poll every 1 s, proceed on release; Ctrl-C while waiting exits 130 with one line and no traceback.
- R8. `--no-wait`: exit `EXIT_CORPUS_BUSY = 4` with the holder details, before any snapshot, write or paid call. Exit code 4 is unused anywhere in `scripts/video_intel.py` today (its only named code is `EXIT_PARTIAL = 3`; argparse uses 2).
- R9. Preview runs take no lock and write no config snapshot (`main()` uses the same predicate; `cmd_scan` and `cmd_headlines` skip their own snapshot call on `--dry-run`).
- R10. Non-locking runs make one probe of `<key>.busy` (one retry after 100 ms before reporting) and log one line when a writer runs; `nugget` and `search --vector` add the search-index sentence; a failing probe is silent (DEBUG).
- R11. `nugget` and `briefings` write their one file through exclusive create with the existing `-2`, `-3` suffix scheme.
- R12. Standalone scripts that write into a corpus they can name take the lock at their command-line entry point through the same helper. Exceptions, each documented: `wiki_atlas.py` (no corpus argument), `remediation_run.py` (no lock of its own; each child `process` locks per video), `atlas_render.py` and `atlas_export.py` when the atlas is not in a corpus `_atlas` folder, `intel_graph.py load` (writes outside the corpus), and library callers. See the standalone script table.
- R13. Inventory test: every registered subcommand is classified as locking with a named preview condition, locking and always writing, or never locking with a reason.
- R14. Docs and guardrails in the same PR; root `CLAUDE.md` does not grow (79,679 characters against the 80,000 budget enforced by `tests/test_guardrail_inventory.py`).

### Scope Boundaries (not building)

A database, a daemon, a job queue, heartbeat leases, a distributed or cross-machine lock, per-channel or per-file locks, a force or break-lock flag, an index-path lock for two corpora sharing one `vector_db_dir`, a `--no-wait` flag on the standalone scripts, any change to library callers that bypass `main()` (documented, not guarded), a `build_parser()` refactor or any other restructuring of `main()` beyond wrapping its body after config resolution. `topics.json` is rewritten in place, so a reader can see a torn file while a writer runs; that is pre-existing, the readers already fail soft, and it stays out of scope. Two machines writing one synced corpus stays an operating rule in `docs/troubleshooting.md`.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Module `scripts/corpus_lock.py`** (all names public unless prefixed `_`):

  ```python
  EXIT_CORPUS_BUSY = 4
  WAIT_POLL_SECONDS = 1.0
  WAIT_REPORT_SECONDS = 180.0
  BUSY_WAIT_REPORT_SECONDS = 5.0    # one line if a reader's probe keeps .busy longer than this
  DEFAULT_WAIT = True               # test seam read at call time; not a flag, not a documented knob

  class LockPaths(NamedTuple): lock: Path; busy: Path; holder: Path
  class CorpusBusy(Exception):       # carries .details: dict | None
  class LockSystemError(Exception):  # carries .path and the underlying OSError
  class Acquired(NamedTuple): waited: bool

  def lock_state_dir() -> Path
  def corpus_key(output_dir: str | os.PathLike) -> str
  def lock_paths(output_dir) -> LockPaths
  def acquire(output_dir, *, command: str, channel: str | None = None, wait: bool | None = None,
              report: Callable[[str], None] = _stderr_line) -> Acquired
  def release(output_dir) -> None
  def held_depth(output_dir) -> int
  @contextmanager
  def hold(output_dir, **acquire_kwargs) -> Iterator[Acquired]
  @contextmanager
  def hold_for_cli(output_dir, *, command, channel=None, wait=None, report=_stderr_line,
                   error=_stderr_line) -> Iterator[Acquired]
  def probe_writer(output_dir) -> dict | None
  def describe_holder(details: dict | None) -> str
  ```

  - `lock_state_dir()`: Windows `Path(os.environ["LOCALAPPDATA"]) / "video-intel" / "locks"`, falling back to `~/AppData/Local/...` when the variable is unset; elsewhere `$XDG_STATE_HOME/video-intel/locks` when that variable is an absolute path, else `~/.local/state/video-intel/locks`. ONE function; no config key, flag or environment variable of our own. Every other function looks it up through the module global at call time, so the test fixture's `monkeypatch.setattr(corpus_lock, "lock_state_dir", ...)` reaches all of them.
  - `corpus_key(p)`: `hashlib.sha256(os.path.normcase(os.path.realpath(os.fspath(p))).encode("utf-8")).hexdigest()[:16]`. `realpath` drops trailing separators and resolves `..` and links; `normcase` folds case and turns `/` into `\` on Windows and is the identity on POSIX.
  - `wait=None` means `DEFAULT_WAIT`, read at call time, so a test can force fail-fast on a standalone script without a CLI flag.
  - **Thread-safe re-entrancy.** Module state: `_REGISTRY: dict[key, _Held]` (fds and depth), `_KEY_LOCKS: dict[key, threading.Lock]`, and a global `_REGISTRY_MUTEX` held only for dictionary lookups and updates, never during polling. `acquire` takes (creating under the mutex if absent) the per-key `threading.Lock` and holds it for the whole OS acquisition, waiting included. Under it, a registry hit increments the depth and returns `Acquired(waited=False)`; a second thread of the same process therefore blocks on the per-key lock, then finds the entry and only increments. A re-entry from the same thread happens after its first acquisition finished, so the per-key lock is free again.
  - **OS acquisition, transactional.** `os.makedirs(state_dir, exist_ok=True)`, open `<key>.lock` with `os.open(path, os.O_RDWR | os.O_CREAT, 0o644)` (fds are non-inheritable by default since PEP 446, so the yt-dlp child that `probe_live_status` spawns during a scan never inherits the lock; a test asserts `os.get_inheritable(fd) is False`), then a non-blocking try-lock in a loop. Held and not waiting: close the fd, raise `CorpusBusy(probe_writer(output_dir))`. Held and waiting: `report("Waiting for the corpus lock: " + describe_holder(probe_writer(output_dir)) + ". This command continues on its own when the lock frees; Ctrl-C cancels with nothing written.")` once, then `_sleep(WAIT_POLL_SECONDS)` and retry, and every `WAIT_REPORT_SECONDS` of `_monotonic()` time `report("Still waiting for the corpus lock (<N> min): " + describe_holder(...) + ".")` (`_sleep` and `_monotonic` are module attributes so a unit test can drive a fake clock). Once `.lock` is held: write `holder.json` (below), then open `<key>.busy` the same way and poll it with the same cancellable loop and no `--no-wait` exit (only readers' probes can hold `.busy`, and `--no-wait` is about another writer), logging `"Waiting for a reader's lock probe to finish."` once if it lasts more than `BUSY_WAIT_REPORT_SECONDS`. Every descriptor opened during the attempt is tracked locally and closed in a `try/finally` that catches `BaseException` (so `KeyboardInterrupt`, `CorpusBusy` and `LockSystemError` all close them, unlocking `.lock` first when it was taken); the registry entry is written only after both locks are held; `Acquired(waited=True)` when any held attempt occurred on `.lock`.
  - **Holder details.** `{"command", "channel", "pid", "started", "corpus"}`: `command` is the subcommand name (`"scan"`, `"profile init"`, or a standalone script's file stem), `channel` a separate field (`getattr(args, "channel", None)` in `main()`, `None` for scripts), `started` local ISO text with seconds. Written to `<key>.holder.json.<pid>.tmp` then `os.replace`, retried in 10 ms steps for about 2 s because Windows refuses the replace while a reader has the file open; on final failure the holder removes the stale `holder.json` best-effort and always removes its temp file, and the run continues (details are metadata, never exclusion). Readers and waiters read it with one `read_bytes`. Because a reader can land between a holder's write and a later holder's replace, the details are "last recorded", and every message says so.
  - `release`: decrement; at zero, best-effort remove `holder.json` when its `pid` is ours, then unlock and close `.busy`, then unlock and close `.lock`, attempting every unlock and close even if an earlier one raised (the first error is re-raised after all were attempted), and drop the registry entry. Never unlinks `.lock` or `.busy`.
  - `hold_for_cli`: the ONE CLI wrapper [core: one-definition] for `main()` and every standalone script, yielding the `Acquired`. It maps `CorpusBusy` to `error("Another command is writing to this corpus: <details>. Exiting without writing anything (exit 4). Re-run when it finishes, or drop --no-wait to wait in line.")` plus `SystemExit(EXIT_CORPUS_BUSY)`; `KeyboardInterrupt` raised inside `acquire` (on `.lock` or on `.busy`) to `error("Interrupted while waiting for the corpus lock; nothing was written.")` plus `SystemExit(130)`; `LockSystemError` to `error("Corpus lock unavailable (<path>: <error>). Nothing was written.")` plus `SystemExit(1)`. A `KeyboardInterrupt` raised later, inside the body, propagates unchanged (today's behavior) and the `finally` releases.
  - `probe_writer`: never raises. Opens `<key>.busy` with `os.O_RDWR` and NO `O_CREAT` (a missing file means no writer has ever run here, and a reader must not create files: `profile show` keeps its zero-write promise). One non-blocking try-lock: acquired means no writer, unlock at once, return `None`; held means another probe or a writer, so close, wait 100 ms, try once more (two readers probing at the same instant must not report each other); held again means a writer runs, return the parsed `holder.json` or `{}` ("details unavailable") when it is missing or unreadable. Any other `OSError` returns `None` and logs DEBUG through `logging.getLogger("corpus_lock")`. It never touches `<key>.lock`, so it can never make a `--no-wait` writer fail; at worst it makes a writer wait a few milliseconds on `.busy`.
  - `describe_holder`: `"scan, channel alpha, PID 1234, started 2026-11-24T10:02:11 (last recorded)"`, without the channel part when `channel` is `None`, or `"details unavailable"` for `{}`/`None`.

- KTD2. **The "held by another process" signal, per platform.**
  - Windows, `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` after `os.lseek(fd, 0, os.SEEK_SET)` (the call locks `nbytes` from the current file position; the unlock `msvcrt.LK_UNLCK` is issued from the same position). Python docs, `msvcrt.locking`: "LK_LOCK / LK_RLCK: Locks the specified bytes. If the bytes cannot be locked, the program immediately tries again after 1 second. If, after 10 attempts, the bytes cannot be locked, OSError is raised." and "LK_NBLCK / LK_NBRLCK: Locks the specified bytes. If the bytes cannot be locked, OSError is raised." The Python docs do not name the errno. The CRT `_locking` documentation says it sets `errno` to `EACCES` ("Locking violation (file already locked or unlocked)") or `EDEADLOCK` (after 10 attempts with `_LK_LOCK` or `_LK_RLCK`), `EBADF` or `EINVAL` otherwise. So "held" = `OSError` with `errno in {errno.EACCES, errno.EDEADLOCK}` (Python surfaces `EACCES` as `PermissionError`); `EBADF` (including a closed descriptor), `EINVAL` and anything else is a lock-system failure, never "held". The locked byte is byte 0 of a zero-length file; Windows allows a lock past end of file, and no reader ever reads `.lock` or `.busy` (details live in `holder.json`), so "a byte-range lock blocks reads of that region" never bites. A Phase A test against the real call pins the observed errno; a different errno is a stop condition.
  - POSIX, `fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)`, unlock `fcntl.LOCK_UN`. Python docs, `fcntl.flock`: "Perform the lock operation operation on file descriptor fd ... See the Unix manual flock(2) for details. (On some systems, this function is emulated using fcntl().)" The `fcntl.lockf` entry states the portable rule for the emulated case: "If LOCK_NB is used and the lock cannot be acquired, an OSError will be raised and the exception will have an errno attribute set to EACCES or EAGAIN (depending on the operating system; for portability, check for both values)." flock(2): `EWOULDBLOCK` "The file is locked and the LOCK_NB flag was selected", and `EINTR` on a signal. So "held" = `OSError` with `errno in {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}` (Python raises `BlockingIOError` for the first two); `EINTR` retries the same attempt; `EBADF` and anything else is a lock-system failure.
  - Same-process caveat that makes the registry mandatory: a second `open` plus lock of the same file inside one process conflicts on both platforms (flock locks belong to the open file description, msvcrt locks to the handle), so re-entry must never reopen.
  - Release on death: both locks are released by the OS when the process ends. Microsoft's `LockFileEx` documentation adds that the time this takes "depends upon available system resources", so the docs say the next writer acquires "within seconds" and the hard-kill test polls with a 10 s bound.

- KTD3. **Classification lives in `scripts/video_intel.py`, next to `CONFIG_BACKUP_COMMANDS`.**

  ```python
  CORPUS_LOCK_EXTRA = frozenset({"profile init"})
  CORPUS_LOCK_COMMANDS = CONFIG_BACKUP_COMMANDS | CORPUS_LOCK_EXTRA   # derived, never restated
  # command -> the argparse condition under which a run of it writes nothing; None = always writes
  CORPUS_LOCK_PREVIEW: dict[str, Callable[[argparse.Namespace], bool] | None] = {...}
  def lock_command_name(args) -> str      # "profile init" / "profile show" for profile, else args.command
  def is_preview_run(args) -> bool        # CORPUS_LOCK_PREVIEW.get(name) is not None and that predicate(args)
  def run_takes_corpus_lock(args) -> bool # name in CORPUS_LOCK_COMMANDS and not is_preview_run(args)
  ```

  Two predicates only, `_dry_run = lambda a: bool(getattr(a, "dry_run", False))` and `_not_apply = lambda a: not getattr(a, "apply", False)`. The snapshot in `main()` becomes `if args.command in CONFIG_BACKUP_COMMANDS and not is_preview_run(args):`. The backup set is not widened.

- KTD4. **`main()` shape after the change.** Parser construction and `parse_args()` stay where they are; the body after config resolution is wrapped, nothing else is restructured:

  ```python
  args = parser.parse_args()            # pure: argparse plus topic_slug_arg / iso_date_arg
  ...logging setup...
  config = load_config()                # reads plugin/user config or the env var only
  output_dir = resolve_output_dir(config, create=False)   # pure path arithmetic
  takes_lock = run_takes_corpus_lock(args)
  lock = (corpus_lock.hold_for_cli(output_dir, command=lock_command_name(args),
                                   channel=getattr(args, "channel", None),
                                   wait=not args.no_wait, report=log.warning, error=log.error)
          if takes_lock else contextlib.nullcontext(None))
  with lock as acquired:
      if acquired is not None and acquired.waited:
          config = load_config()        # the config may have changed while this run waited
          if corpus_lock.corpus_key(resolve_output_dir(config, create=False)) != corpus_lock.corpus_key(output_dir):
              log.error("The config changed while this command waited and now names a different corpus. "
                        "Nothing was written; run the command again.")
              sys.exit(1)               # the with-block releases the lock
      if not takes_lock:
          note_running_writer(args, output_dir)          # R10, one probe, never raises
      if args.command in CONFIG_BACKUP_COMMANDS and not is_preview_run(args):
          try: backup_config_if_changed(resolve_output_dir(config))
          except Exception as e: log.warning(...)      # unchanged warning-only handler
      _STEP_MODELS.update(resolve_step_models(args, config))
      ...
      try:
          ...dispatch chain unchanged...
      finally:
          flush_topic_stamps()
  ```

  A command that did not wait keeps the config it loaded. The dispatch chain keeps its exact `args.command == "..."` shape so `_dispatch_commands` in `tests/test_config_backup.py` still parses it (`test_dispatch_inventory_matches_the_argparse_registry` is the loud failure if it does not). The wait and refusal lines go through `log.warning` / `log.error` so they are visible at the default `info` level and at `--log-level warning`.

- KTD5. **What runs before acquisition, proven not to read corpus state.** In order: module import (no module-level corpus read: corpus paths are only built inside functions that take `output_dir`), `argparse` construction and `parse_args()` (the two `type=` callables, `topic_slug_arg` and `iso_date_arg`, are pure string checks), the `assess-transcripts --apply` parser check, logging setup, `load_config()` (reads `SKILL_DIR/config.yaml`, or `$VIDEO_INTEL_OUTPUT_DIR`, or `~/.video-intel/config.yaml`; `_coerce_prompt_dirs` inspects only the configured prompt folders; none of these is inside the corpus), and `resolve_output_dir(config, create=False)` (path arithmetic, no I/O). `resolve_step_models` moves inside the `with` and reads only `config`. Everything that reads corpus state (taxonomy, metas, the index, `seen.json`, the channel-id state file, `config.latest.yaml`) is inside a `cmd_*` function or `backup_config_if_changed`, both after acquisition (and after the re-read when the run waited).

- KTD6. **Reader note** `note_running_writer(args, output_dir)` in `video_intel.py`: one `corpus_lock.probe_writer(output_dir)`; `None` prints nothing; otherwise ONE `log.warning` line: `"A writing command is running on this corpus: <describe_holder>. Results reflect the corpus as it is now; re-run after it finishes for the complete picture."`, and for `nugget` or `search --vector` the same line ends with `" Search results come from the search index, which a scan does not update, so they match what you would get before the scan; new videos become searchable after index --incremental."` It runs for every non-locking run, readers and previews alike (decision 15).

- KTD7. **Collision-safe names.** One helper in `video_intel.py`, `write_new_numbered_file(directory: Path, base_name: str, content: str, suffix: str = ".md") -> Path`: `mkdir(parents=True, exist_ok=True)`, then loop `open(directory / f"{base}{'' if n == 1 else f'-{n}'}{suffix}", "x", encoding="utf-8")` catching `FileExistsError` and incrementing from 2, write `content`, return the path. `write_nugget_brief` (the `while out_path.exists()` loop and `write_text`) and `cmd_briefings` (the same loop at the `catch-up-unseen` write) both call it [core: one-definition]; the names produced are byte-identical to today's (`<date>-<slug>.md`, then `-2`, `-3`). The briefings PDF keeps `out_path.with_suffix(".pdf")`, unique because its Markdown name is. `nugget --output PATH` keeps its overwrite semantics (an operator-chosen path).

- KTD8. **Snapshot behavior.** `main()` uses the predicate (KTD3). `cmd_scan` wraps its own call: `if not args.dry_run: backup_config_if_changed(output_dir)`, still placed above every fetch so `TestScanSnapshotsBeforeMutating` keeps its meaning. `cmd_headlines` does the same with `args.dry_run`. `profile init` is in the lock set but not the backup set, so it still writes no snapshot (unchanged).

- KTD9. **Standalone scripts.** Each locking script calls `corpus_lock.hold_for_cli(corpus, command="<script file stem>", report=<stderr line>, error=<stderr line>)` around its work, after argument parsing and before its first corpus read or paid call. Corpus key per script is in the script table.

### Subcommand table (every registered subcommand, read from the code)

| Subcommand | Lock | Preview condition | Preview writes nothing today? |
|---|---|---|---|
| `scan` | yes | `--dry-run` | Writes the config snapshot today (`main()` and `cmd_scan`'s own call); this PR removes both on a dry run. Otherwise verified: alt-title capture, the channel-id state file (`resolve_channel_identity(..., dry_run=True)`), the headline seen-state (`render_headline_digest(..., dry_run=True)`) and `rebuild_free_derived_layers` (gated on `changed_channels`, empty on a dry run) are all gated. Makes YouTube quota calls. |
| `mindmap` | yes | none, always writes | n/a (a `--file` outside the corpus with no channel writes beside the file; still locks, the simpler rule) |
| `transcript` | yes | none, always writes | n/a (same `--file` note) |
| `process` | yes | none, always writes | n/a (same `--file` note) |
| `concepts` | yes | `--dry-run` | Yes after this PR; today only the snapshot. Builds a Gemini client but calls nothing, reads `taxonomy.json`, returns before any extraction. |
| `taxonomy-build` | yes | none, always writes | n/a |
| `topics-build` | yes | `--dry-run` | Yes after this PR; today only the snapshot (`cmd_topics_build` prints and returns before `write_topics`). |
| `index` | yes | none, always writes | n/a (`probe_atomic_writes` itself writes a throwaway table in `vector_db_dir`) |
| `repair-metas` | yes | no `--apply` | Yes after this PR; both writes are gated on `args.apply`. |
| `assess-transcripts` | yes | no `--apply` | Yes after this PR; `_write_meta_atomic` and the backup copy run only with `apply`. |
| `repair-timestamps` | yes | no `--apply` | Yes after this PR; returns `{"outcome": "dry_run"}` before any write. Fetches the caption track (free, network). Uses `resolve_output_dir(..., create=False)`. |
| `backfill-descriptions` | yes | no `--apply` | Yes after this PR; the per-meta write is skipped with `continue`. Makes YouTube `videos.list` calls (quota) on a dry run. |
| `backfill-durations` | yes | no `--apply` | Yes after this PR; returns before the YouTube client is built. |
| `dedupe` | yes | no `--apply` | Yes after this PR; `_apply_dedupe_group` only with `apply`. |
| `prune-shorts` | yes | no `--apply` | Yes after this PR; `_apply_prune_shorts` only with `apply`. May call `videos.list` and the Shorts HEAD probe on a dry run. |
| `mark-skip` | yes | none, always writes | n/a |
| `headlines` | yes | `--dry-run` | Writes the config snapshot today (`main()` and `cmd_headlines`'s own call); this PR removes both. Seen-state and the channel-id state file are already gated. |
| `profile init` | yes (extra) | none, always writes | n/a; no snapshot (not in the backup set) |
| `profile show` | never | n/a | Reader: `create=False`, writes nothing (`test_profile_show_reports_inferred_source_and_writes_nothing`). |
| `search` | never | n/a | Reader. |
| `status` | never | n/a | Reader. |
| `nugget` | never | n/a | Writes only its own new brief, by exclusive create (KTD7). |
| `briefings` | never | n/a | Writes only its own new briefing (and optional PDF), by exclusive create (KTD7). Stays classified read-only for snapshots in `tests/test_config_backup.py`. |

Every preview except `repair-timestamps` calls `resolve_output_dir(config)`, which creates the corpus root when it does not exist; several previews spend YouTube quota while unlocked. Both are accepted limits and documented.

### Standalone script table (every script under `scripts/` with a command-line entry point)

| Script | Writes into the corpus? Where | Lock |
|---|---|---|
| `atlas_build.py` | Yes: `<corpus>/_atlas/atlas.json` (or `--out`). Reads the whole corpus and the index. | Yes, key `--corpus`, always (it reads corpus state even with `--out` elsewhere). In `main(argv)`, after `parse_args`, before `build_catalog`. `rebuild_free_derived_layers` calls `build_catalog`, not `main`, so a scan never re-enters here. |
| `atlas_render.py` | Yes: `catalog.html` next to the atlas (or `--out`). Reads only the atlas file. | Only when the atlas file sits in a corpus `_atlas` folder (`atlas_build.ATLAS_DIRNAME`); key = that folder's parent, `Path(os.path.abspath(atlas)).parent.parent`. No lock otherwise (for example the committed demo under `examples/catalog`). Re-entered in-process by `rebuild_free_derived_layers` (`atlas_render.main(["--atlas", ...])`) while the scan holds the lock: depth 2, no wait. |
| `atlas_export.py` | Yes by default: `_atlas-export/` next to `_atlas/` (or `--out`). Calls the YouTube Data API. | Same rule as `atlas_render.py`, through one shared helper; acquired before `load_atlas` and before the visibility call [core: probe-before-pay]. |
| `wiki_concepts.py` | Yes: `<corpus>/_wiki/concept-pages` (or `--out`). Reads taxonomy and concepts. | Yes, key `--corpus`, before `build_pages`. `main()` takes no `argv`; tests patch `sys.argv`. |
| `wiki_atlas.py` | Only if the operator points `--wiki-dir` into a corpus (the help text suggests `<output_dir>/_wiki`). Reads the DuckDB store, never the corpus; no corpus argument. | No lock; documented as outside the guard. |
| `remediation_sweep.py` | Yes: `<corpus>/_reports/` worklist; reads every meta; optional YouTube calls. | Yes unless `--dry-run`, key `vi.resolve_output_dir(vi.load_config())`, before `collect_rows`. `main()` takes no `argv`; tests patch `sys.argv`. |
| `remediation_restore.py` | Yes: restores transcripts, mindmaps, concepts and metas from `_reports/remediation-backup/`. | Yes unless `--dry-run`, key `vi.resolve_output_dir(vi.load_config())` (the same expression its `main()` already uses), before `find_regressions`. `main()` takes no `argv`; tests patch `sys.argv`. |
| `remediation_run.py` | Yes: backups and the state file under `_reports/`; its per-video work is a child `video_intel.py process --url ... --force` subprocess. | No lock; each child `process` command locks per video; its own backup and state writes are outside the guard (documented). A child's 3600 s timeout now includes any time it waits for another writer. |
| `intel_graph.py` | No: writes the DuckDB store under `~/.cache/video-intel/` (or `--db`); `load` reads the corpus. | No (writes outside the corpus, decision 17). Its `load` can read a mid-scan view; documented. |
| `lead_lag_report.py`, `lead_lag_viz.py`, `burst_report.py`, `sdsm_network.py`, `disparity_backbone.py` | No: read the DuckDB store, write `--out` or stdout. | No. An operator-chosen `--out` inside a corpus is outside the guard; documented. |
| `model_eval.py` | No: writes under `tests/evals/model-cards/` in the repo; never calls `load_config`. | No. |
| `markdown_pdf.py` | No: writes the operator's named output file. | No. |
| `register_obsidian_vault.py` | No: writes Obsidian's own registry. | No. |
| `translate_video.py` | Writes subtitle files; operationally separate. | Not wired (decision 17). |
| `video_intel.py` | See the subcommand table. | `main()`. |

`board-sync.sh` is a shell script for the project board and touches no corpus. `corpus_walk.py`, `caption_align.py`, `briefing_pdf.py`, `gemini_common.py`, `timestamp_utils.py` and `youtube_captions.py` have no command-line entry point.

### Assumptions (accepted by the orchestrator)

- A1. Holder details carry `command` (the subcommand name, or a standalone script's file stem) and `channel` as separate fields.
- A2. The reader note is a WARNING: it is the "results may be incomplete" signal and must survive `--log-level warning`.
- A3. The standalone scripts wait only; no `--no-wait` there. An agent that needs fail-fast uses the `video_intel.py` commands.
- A4. Readers probe with no `O_CREAT`, so a corpus that never had a writer reports nothing and creates nothing.
- A5. Exit 130 is used on every platform for Ctrl-C while waiting (a plain `SystemExit(130)`, not a re-raised signal).

### Test infrastructure shared by all phases

- **State folder fixture.** An autouse fixture `corpus_lock_state_dir` in `tests/conftest.py` pins `corpus_lock.lock_state_dir` to `tmp_path / "locks"` for every test, in the style of `no_shorts_probe`, and yields the path so subprocess helpers can be handed it. A test class that needs one holder for many cases overrides it with a module-scoped folder (Phase B).
- **Driver `tests/corpus_lock_driver.py`** (not collected: the name does not start with `test_`). argv `--state-dir D --sync S --name N MODE ...`. It patches `corpus_lock.lock_state_dir` to `D` before anything else. Each process records events only in its own file `S/<name>.events`, one line per event, `"<time.monotonic_ns()> <event>"` (one system-wide clock on Windows and Linux), opened, written, flushed and closed per line; cross-process order is asserted by comparing timestamps across files, never through one shared append log. A process records its last protected action BEFORE it unlocks, so "A's last protected action precedes B's acquired" is a valid ordering check. Modes: `hold --corpus C` (acquire with `command="scan", channel="alpha"`, record `acquired`, create `S/<name>.ready`, poll for `S/<name>.release` every 20 ms with a 60 s bound, record `last-protected`, release, record `released`); `try --corpus C` (non-blocking acquire, exit 0 acquired or 4 busy); `probe-hold --corpus C` (take `<key>.busy` directly through the private try-lock, as a reader paused mid-probe, create `ready`, wait for `release`); `probe-loop --corpus C --seconds T` (call `probe_writer` in a tight loop, write `S/<name>.ready` after the first probe, record counts of writer and no-writer results at exit); `main --pause-at TARGET -- ARGV...` (Phase B).
- **No hang rule.** Every acquisition that a mutation could turn into a deadlock uses `wait=False`, or runs in a daemon thread with a bounded `join` whose timeout fails the test with a message. Tests wait for `.ready` / `.paused` files or for text in a captured stderr file with a bounded poll (20 ms step, 20 s bound); no test uses a sleep as its only ordering mechanism.

---

## Implementation Units

### Phase A: the `corpus_lock` module (commit on its own; full suite green at its end)

Nothing in the repo imports the module yet, so this phase changes no behavior.

#### U1. `scripts/corpus_lock.py`, its fixture, tests and harness

- **Files:** `scripts/corpus_lock.py` (new), `tests/test_corpus_lock.py` (new), `tests/corpus_lock_driver.py` (new, all modes except `main`), `tests/conftest.py` (the autouse fixture), `tests/test_timestamp_utils.py` (add `corpus_lock` to `TestStandaloneScriptImportIsolation`'s parametrize list), `docs/plans/gate1-evidence/issue-303-corpus-lock-harness.py` (new, standard library only).
- **Approach:** KTD1 and KTD2.
- **Tests (`tests/test_corpus_lock.py`):**
  - `test_key_same_for_two_spellings_of_one_path`: trailing separator, `..` segment, and on Windows upper and lower case and `/` versus `\`; on POSIX a symlink to the corpus (skipped on Windows).
  - `test_key_differs_for_two_corpora`.
  - `test_state_dir_windows_and_posix_rules`: environment patched; `LOCALAPPDATA` unset fallback; relative `XDG_STATE_HOME` ignored.
  - `test_second_process_try_acquire_is_refused_while_held`: driver `hold`, then driver `try` exits 4; after `release`, `try` exits 0.
  - `test_held_signal_errno_is_the_documented_one`: driver `hold` holds; the private `_try_lock` against the real platform call is driven in-process and the caught `OSError.errno` asserted to be in the documented set for the platform (the stop condition).
  - `test_non_held_lock_error_fails_closed`: the platform lock call patched to raise `OSError(errno.EBADF)`, then `OSError(errno.EINVAL)`; `hold_for_cli` with waiting enabled runs in a daemon thread with a bounded join; assert `SystemExit` code `== 1`, exactly one ERROR naming the lock path, no wait line, every opened descriptor closed.
  - `test_waiter_proceeds_when_holder_releases`: driver A `hold`; in-process `acquire(wait=True)` in a daemon thread whose `report` appends to a list; after the list has its first entry (bounded), assert it equals `"Waiting for the corpus lock: scan, channel alpha, PID <A pid>, started ... (last recorded). This command continues on its own when the lock frees; Ctrl-C cancels with nothing written."` (prefix and hint compared exactly, the start time by pattern); create `A.release`; bounded join; the test records its own `acquired` event with `time.monotonic_ns()` and asserts A's `last-protected` timestamp is earlier, and `Acquired.waited is True`.
  - `test_wait_reports_at_start_and_every_three_minutes`: fake `_monotonic` and `_sleep`, `_try_lock` patched to report held for 400 polls; reports at 0, 180 and 360 fake seconds and nowhere else; the repeat line starts with `"Still waiting for the corpus lock ("`.
  - `test_no_wait_raises_with_holder_details`: `CorpusBusy.details` carries A's command, channel and PID as separate fields.
  - `test_default_wait_is_read_at_call_time`: with driver A holding, `corpus_lock.DEFAULT_WAIT = False` patched, `acquire(...)` with `wait` omitted raises `CorpusBusy` at once.
  - `test_reentrant_depth_and_single_release`: acquire twice, `held_depth == 2`, release once keeps it held (driver `try` exits 4), release again frees it (driver `try` exits 0); `Acquired.waited` is `False` on the re-entry.
  - `test_two_threads_share_one_os_acquisition`: a `threading.Barrier(2)` releases two daemon threads that both call `acquire(..., wait=False)` on the same key; bounded joins; neither raised, `held_depth == 2`, the private open call (wrapped by a counter) ran once for `.lock`, driver `try` exits 4 until both released.
  - `test_writer_waits_for_a_reader_paused_mid_probe`: driver `probe-hold` holds `.busy`; `BUSY_WAIT_REPORT_SECONDS` patched to 0.1; in-process `acquire(wait=False)` in a daemon thread (proving `--no-wait` does not apply to `.busy`); the report list receives `"Waiting for a reader's lock probe to finish."`; release the prober; bounded join; acquired, no `CorpusBusy`.
  - `test_ctrl_c_during_busy_wait_releases_the_lock`: as above, with `_sleep` raising `KeyboardInterrupt` once the busy wait starts; `hold_for_cli` exits 130; driver `try` then exits 0 (`.lock` was released) and `held_depth == 0`.
  - `test_interrupted_acquisition_closes_every_descriptor`: `KeyboardInterrupt` injected at each step of the acquisition in turn (after `.lock` open, after `.lock` locked, after details written, after `.busy` open); each time the registry is empty, the tracked descriptors are closed (`os.fstat` raises `OSError`), and driver `try` exits 0.
  - `test_release_attempts_every_close_even_if_one_unlock_fails`: the `.busy` unlock patched to raise; `release` re-raises after closing both descriptors; driver `try` exits 0.
  - `test_killed_holder_frees_the_lock`: driver `hold`, `proc.kill()`, `proc.wait()`; inside one 10 s bound poll until both hold: in-process `acquire(wait=False)` succeeds and, before that, `probe_writer` returned `None`; the dead holder's leftover `holder.json` was never reported as a running writer.
  - `test_leftover_details_without_a_lock_are_not_a_writer`: a `holder.json` written by hand; `probe_writer` returns `None`; `acquire(wait=False)` succeeds at once.
  - `test_probe_reports_running_writer_and_reads_details`, `test_probe_without_any_files_creates_nothing`, `test_probe_never_raises_on_unreadable_state_dir` (DEBUG only).
  - `test_two_readers_probing_together_do_not_report_each_other`: driver `probe-hold` holds `.busy` for 50 ms then releases; an in-process `probe_writer` started inside that window returns `None` thanks to the 100 ms retry.
  - `test_reader_probe_never_makes_a_no_wait_writer_fail`: driver `probe-loop` for 3 s; the test waits for its `ready` file (bounded), then runs `acquire(wait=False)` / `release` 200 times in a daemon thread with a bounded join; zero `CorpusBusy`; the loop's counts show it observed the writer at least once.
  - `test_holder_details_replace_retries_then_degrades`: `os.replace` patched to raise `PermissionError` for the first three calls (succeeds after retry), and in a second case always; the acquisition succeeds in both; in the second, no stale `holder.json` and no temp file remain.
  - `test_lock_fd_is_not_inheritable`.
  - `test_lock_files_are_never_unlinked`: after acquire and release, `.lock` and `.busy` still exist.
- **Import isolation:** `corpus_lock` added to the existing parametrize list, plus `test_corpus_lock_imports_only_the_standard_library`: in a subprocess, record `set(sys.modules)` before and after `import corpus_lock` and assert every new top-level name is in `sys.stdlib_module_names`.
- **Linux harness:** `docs/plans/gate1-evidence/issue-303-corpus-lock-harness.py`, stdlib only, imports `scripts/corpus_lock.py` by path, takes `--state-dir`, and runs six checks with child processes of itself: contend, waiter proceeds after release, try-acquire refused while held, killed holder frees the lock (bounded poll), probe never blocks a try-acquire, and a waiting child receives a real `SIGINT` (POSIX only) and exits 130. Prints one PASS/FAIL line per check, exits non-zero on any FAIL. The orchestrator runs it under WSL with the state folder on the Linux filesystem, and on Windows (where the `SIGINT` check is skipped).
- **Stated limit:** a real Windows console interrupt (Ctrl-C delivered to a waiting process) is not tested; the in-process tests raise `KeyboardInterrupt` from the sleep, and the harness sends a real `SIGINT` on Linux only.
- **Falsify (each fails exactly its own test):** treat any `OSError` as held (`test_non_held_lock_error_fails_closed`); probe `.lock` instead of `.busy` (`test_reader_probe_never_makes_a_no_wait_writer_fail`); make `.busy` contention a failure (`test_writer_waits_for_a_reader_paused_mid_probe`); hold the registry mutex while polling (`test_two_threads_share_one_os_acquisition` times out on its bounded join); record registry ownership before `.busy` is held (`test_interrupted_acquisition_closes_every_descriptor`); open the lock fd a second time on re-entry (`test_reentrant_depth_and_single_release`); drop the probe retry (`test_two_readers_probing_together_do_not_report_each_other`); unlink `.lock` on release (`test_lock_files_are_never_unlinked`).
- **Phase end:** full suite green; commit.

### Phase B: `video_intel.py` wiring (commit on its own; full suite green at its end)

#### U2. Collision-safe names for `nugget` and `briefings`

- **Files:** `scripts/video_intel.py` (`write_new_numbered_file`, `write_nugget_brief`, `cmd_briefings`), `tests/test_nugget_persistence.py`, `tests/test_briefings.py`.
- **Approach:** KTD7.
- **Tests:** the existing `test_same_day_rerun_gets_dash2_suffix_never_clobbers`, `test_third_same_day_rerun_gets_dash3_suffix`, `test_two_runs_same_day_do_not_clobber` and `test_cmd_briefings_does_not_overwrite_same_day` stay green unchanged (names byte-identical). New, for each caller, `test_exclusive_create_survives_a_race_between_check_and_write`: patch `Path.exists` to always return `False` (the old check-then-write would then clobber), pre-create the base file, run the real writer, assert the earlier file is byte-identical and the new one is `-2`.
- **Falsify:** restore the `exists()` loop in either caller; its race test fails.

#### U3. Classification, the preview snapshot rule and the guardrail sentences that change with it

- **Files:** `scripts/video_intel.py` (KTD3, the `main()` snapshot condition, the `cmd_scan` and `cmd_headlines` snapshot calls), `tests/test_config_backup.py`, `tests/test_headlines_command.py`, `tests/test_corpus_lock_cli.py` (new), `.claude/rules/scan-config.md` (snapshot entry, `backfill-durations` sentence, `paths:`), `.claude/rules/briefings.md` (headlines item 3), `.claude/rules/search-index.md` (nugget sentence).
- **Approach:** KTD3 and KTD8. No `build_parser()` refactor.
- **Tests:**
  - `TestEveryRunIsClassifiedForTheLock` (in `tests/test_corpus_lock_cli.py`): inventory = `_registered_commands(module_src)` imported from `tests/test_config_backup.py` (not copied) [core: one-definition], with `profile` expanded to `profile show` and `profile init` read from the nested `profile_actions.add_parser` calls; assert `set(CORPUS_LOCK_PREVIEW) == CORPUS_LOCK_COMMANDS`; a `NEVER_LOCKS` dict with a reason each (`search`, `status`, `profile show`: "reader"; `nugget` and `briefings`: "never locks: writes only its own new file by exclusive create"); inventory equals `CORPUS_LOCK_COMMANDS | set(NEVER_LOCKS)` with an empty intersection; `CORPUS_LOCK_COMMANDS >= CONFIG_BACKUP_COMMANDS`. A new unclassified subcommand fails here.
  - `test_lock_and_preview_follow_the_real_parser` (parametrized over every entry of `CORPUS_LOCK_PREVIEW`): the real `main()` with `sys.argv` patched to the command's smallest valid argv, with and without its preview flag, and its `cmd_*` dispatch target replaced by a recorder that stores `corpus_lock.held_depth(output_dir)`; assert depth 0 for the preview argv and 1 for the writing argv (always 1 for `None` entries); the same for `NEVER_LOCKS` entries (always 0).
  - `test_preview_run_writes_no_snapshot_in_main` (parametrized over every preview entry): real `main()` with `sys.argv` patched, the scratch config file resolved through a patched `load_config` that sets `_LAST_RESOLVED_PATH`, the `cmd_*` replaced by a recorder; assert the recorder ran and `_config-backups/` does not exist. Companion `test_writing_run_still_snapshots_in_main`: the same commands without the preview flag create the snapshot.
  - `test_scan_dry_run_skips_its_own_snapshot` and `test_headlines_dry_run_skips_its_own_snapshot`: the real `cmd_scan` / `cmd_headlines` with the existing stubs, `backup_config_if_changed` replaced by a recorder; dry run records nothing, a real run records once.
  - `tests/test_headlines_command.py::TestBackupRunsBeforeRender::test_order` changes from `dry_run=True` to `dry_run=False` (still `["backup", "render"]`), plus `test_dry_run_renders_without_backup` asserting `["render"]`.
  - `tests/test_config_backup.py::TestScanSnapshotsBeforeMutating` stays: the guarded call is still above every fetch in source order. `TestEveryMutatingCommandSnapshots` stays unchanged (`briefings` remains `READ_ONLY` for snapshots).
- **Rule edits in this unit (they change with the snapshot behavior):**
  - `.claude/rules/scan-config.md` `paths:` adds `scripts/corpus_lock.py`, `tests/test_corpus_lock.py`, `tests/test_corpus_lock_cli.py`, `tests/test_corpus_lock_subprocess.py`.
  - The "Config snapshots are AUTOMATIC" entry, sentence one becomes: "`backup_config_if_changed(output_dir)` runs before any corpus-mutating run touches the corpus - from `cmd_scan` itself (immediately before its first fetch, the precise point of record) and from `main()` for every command in `CONFIG_BACKUP_COMMANDS` - except preview runs (`is_preview_run`: `--dry-run`, or no `--apply`), which write nothing at all, snapshot included." The "A diff that removes the call" sentence gains "or that snapshots on a preview run".
  - The `backfill-durations` entry, item (2): "(resolving the output folder and `main()`'s config snapshot are the only side effects, as for `backfill-descriptions`)" becomes "(resolving the output folder is the only side effect, as for `backfill-descriptions`; a dry run writes no config snapshot)". This is the only sentence of its kind in `.claude/rules/`.
  - `.claude/rules/briefings.md` item 3 ("snapshots the config like `scan`") gains "on a run that writes; `headlines --dry-run` takes no lock and writes no snapshot"; one sentence that `briefings` takes no lock and names its file by exclusive create.
  - `.claude/rules/search-index.md`, nugget entry: one sentence that the brief's name comes from `write_new_numbered_file` (exclusive create) and nugget never takes the corpus lock.
- **Falsify:** drop `concepts` from `CORPUS_LOCK_PREVIEW` (the inventory fails); remove the `not is_preview_run(args)` clause from the snapshot (`test_preview_run_writes_no_snapshot_in_main` fails for every preview); remove the `cmd_scan` guard (`test_scan_dry_run_skips_its_own_snapshot`); make `is_preview_run` always `False` (`test_lock_and_preview_follow_the_real_parser` sees depth 1 on previews).

#### U4. `main()` acquisition, waiting, the config re-read, `--no-wait`, the reader note

- **Files:** `scripts/video_intel.py` (`--no-wait` argument, KTD4, KTD5, KTD6, `note_running_writer`, `EXIT_CORPUS_BUSY` re-exported from `corpus_lock`), `tests/test_corpus_lock_cli.py`.
- **Approach:** KTD4 to KTD6. `--no-wait` help: "When another command is already writing to this corpus, exit at once with code 4 instead of waiting. Ignored by commands that only read."
- **Tests (in-process `main()` with `sys.argv` patched; holder = driver `hold` subprocess):**
  - `TestNoWaitRefusesEveryWriter`: one module-scoped fixture starts ONE driver `hold` on a module-scoped corpus and a module-scoped state folder (each test re-pins `corpus_lock.lock_state_dir` to it, overriding the autouse fixture) and releases it at module teardown. `test_no_wait_exits_four_before_snapshot_or_dispatch` is parametrized over every writing entry of `CORPUS_LOCK_COMMANDS`, each with its smallest valid argv, the `cmd_*` replaced by a recorder that would fail the test, and the scratch config edited so a snapshot would be written: `SystemExit` code asserted `== 4` (never a bare `pytest.raises(SystemExit)`), recorder not called, `_config-backups/` unchanged, the ERROR line names the holder's command, channel and PID.
  - `test_readers_and_previews_run_while_held_and_note_once` (parametrized: `search` real concept search over a two-video scratch corpus; `search <query> --vector` with `hybrid_search` stubbed; `status` real; `nugget --no-save` with `hybrid_search` and the Gemini call stubbed; `profile show` real; `briefings --unseen --dry-run` real; `scan --dry-run` with the scan stubs): completes, exactly one note line ending in "(last recorded)" details, the `nugget` and `search --vector` cases carry the search-index sentence and the others do not.
  - `test_no_note_without_a_writer`: the same runs with no holder print no note.
  - `test_scan_dry_run_during_a_held_lock_leaves_the_tree_byte_identical`: holder held; snapshot the whole scratch corpus tree (paths plus bytes) before and after a stubbed `scan --dry-run`; identical; holder still holds (driver `try` exits 4).
  - `test_topic_flush_runs_under_the_lock`: `flush_topic_stamps` patched to run driver `try` and record its exit code; assert it recorded 4; after `main()` returns, driver `try` exits 0.
  - `test_lock_released_after_failure` (parametrized: dispatch raises `RuntimeError`, dispatch calls `sys.exit(EXIT_PARTIAL)`, dispatch raises `KeyboardInterrupt`): the exception or exit code propagates unchanged (code asserted), then `held_depth == 0` and driver `try` exits 0.
  - `test_ctrl_c_while_waiting_exits_130_and_writes_nothing`: holder held; `corpus_lock._sleep` patched to raise `KeyboardInterrupt`; `SystemExit` code `== 130`, the one line logged, no traceback text in captured output, recorder not called, no snapshot.
  - `test_waiting_writer_reads_state_only_after_acquisition`: holder held, a `taxonomy-build` `main()` run in a daemon thread with `build_taxonomy` replaced by a recorder; wait until the wait line appears; assert the recorder has not run and `config.latest.yaml` is unchanged; release; bounded join; recorder ran.
  - `test_config_change_to_another_corpus_while_waiting_exits_one`: holder held; `load_config` patched so its second call names another corpus; release the holder once the wait line appears; bounded join; `SystemExit` code `== 1`, the ERROR line, recorder not called, no snapshot in either corpus, `held_depth == 0`.
  - `test_run_that_did_not_wait_loads_config_once`: no holder; a counter on `load_config` reads 1.
  - `test_two_spellings_of_one_corpus_contend_and_two_corpora_do_not`: holder on spelling 1; `main()` with `--no-wait` on spelling 2 exits 4; on another corpus it runs.
  - `test_nothing_before_acquisition_reads_the_corpus`: `main()` with `--no-wait` against a held lock, with `Path.read_bytes`, `Path.read_text` and `builtins.open` wrapped by a recorder scoped to paths under the corpus root; exit 4 and zero recorded corpus reads.
- **Falsify (each fails exactly its own test):** remove the acquisition (`test_no_wait_exits_four_before_snapshot_or_dispatch` and the U5 scenarios); move it after the snapshot (`test_no_wait_exits_four_before_snapshot_or_dispatch` sees a snapshot; `test_waiting_writer_reads_state_only_after_acquisition` sees a changed latest); release before the flush (`test_topic_flush_runs_under_the_lock`); drop one command from the lock set (its no-wait case and the inventory); remove the config re-read (`test_config_change_to_another_corpus_while_waiting_exits_one` and U5's `test_waiter_runs_with_the_config_edited_while_it_waited`).

#### U5. Two real `main()` subprocesses

- **Files:** `tests/test_corpus_lock_subprocess.py` (new), `tests/corpus_lock_driver.py` (the `main` mode).
- **Driver `main` mode:** patches `corpus_lock.lock_state_dir`; patches `video_intel.load_config` with a function that re-reads and parses the scratch config file from disk on every call and sets `_LAST_RESOLVED_PATH` to it (so the snapshot path and the config re-read are both real); asserts `resolve_output_dir(config)` lies under the test's temp folder before anything runs (the live-corpus guard: a plugin-local `config.yaml` would otherwise win the precedence chain); sets fake `GEMINI_API_KEY` / `YOUTUBE_API_KEY`; installs the stubs for the named scenario; wraps the `--pause-at` target so it records `entered`, creates `S/<name>.paused`, waits for `S/<name>.release` (20 ms poll, 60 s bound), records `last-protected` and returns; every stubbed paid boundary records `paid <boundary>`; on exit records `exit <code>`. Stdout and stderr go to files the test reads. All events go to the process's own `S/<name>.events`.
- **Scenarios (A paused at a fake paid boundary, B started after `A.paused` exists):**
  - `test_full_scan_vs_focused_scan`: A `scan` paused in the stubbed `fetch_channel_videos`; B `scan --channel alpha`.
  - `test_scan_vs_taxonomy_build`: A `scan` as above; B `taxonomy-build`.
  - `test_index_incremental_vs_index_channel`: A `index --incremental` paused in the stubbed Voyage embed; B `index --channel alpha`; a real LanceDB table in the scratch `vector_db_dir` as in `tests/test_scan_derived_layers.py`.
  - `test_dedupe_apply_vs_scan`: A `dedupe --apply` paused in `_apply_dedupe_group`; B `scan --channel alpha`.
  - Each: start B, wait for "Waiting for the corpus lock: " in B's stderr (bounded), assert B's event file holds no `paid` and no `entered` line and `_config-backups/` is unchanged; create `A.release`; wait for both exits (bounded 30 s); assert both exit 0 and A's `last-protected` timestamp precedes B's first `entered` or `paid` timestamp.
  - `test_waiter_runs_with_the_config_edited_while_it_waited`: A `taxonomy-build` paused; B `taxonomy-build`; after B's wait line, edit the scratch config file (same corpus); release A; after B exits 0, `_config-backups/config.latest.yaml` equals the edited bytes and a new dated snapshot exists.
  - `test_no_wait_second_writer_exits_four_before_anything`: B `--no-wait taxonomy-build` exits 4 within the bound, stderr names A's command and PID, B's event file holds only `exit 4`, snapshots unchanged.
  - `test_hard_kill_of_a_running_writer_frees_the_corpus` (the one subprocess hard-kill test of this phase): A paused `taxonomy-build`, `proc.kill()`; within one 10 s bound, B `--no-wait taxonomy-build` retried until exit 0 and an in-process `probe_writer` returns `None`.
- **Runtime budget:** Phase A about 8 s, U2 to U4 about 12 s (in-process `main()`, one module-scoped holder for the no-wait parametrization, other holders import only `corpus_lock`), U5 about 25 s (seven scenarios, two `video_intel` imports each, B starts while A is paused so the pairs overlap), Phase C about 3 s. Total under 50 s, inside the 60 s ceiling; measured in the gate and recorded in the PR.
- **Falsify:** with the acquisition removed, every pair scenario fails on its "B waits" step (no wait line inside the bound, failing with a message saying so) or on its ordering check.
- **Phase end:** full suite green; commit.

### Phase C: standalone scripts and the remaining docs (commit on its own; full suite green at its end)

#### U6. Standalone scripts

- **Files:** `scripts/atlas_build.py`, `scripts/atlas_render.py`, `scripts/atlas_export.py`, `scripts/wiki_concepts.py`, `scripts/remediation_sweep.py`, `scripts/remediation_restore.py`, their tests (`tests/test_atlas_build.py`, `tests/test_atlas_render.py`, `tests/test_atlas_export.py`, `tests/test_wiki_concepts.py`, `tests/test_remediation_sweep.py`, and a new `tests/test_remediation_restore.py`), the `atlas_build.py` module docstring's import list (adds `corpus_lock`), and `.claude/rules/intelligence-layer.md` (the import-weight paragraph adds `corpus_lock` to the stdlib-only modules the catalog scripts may import; one sentence on which scripts lock, pointing at the script table's rule). `remediation_run.py` is not changed.
- **Approach:** the script table and KTD9; `atlas_render.py` defines `corpus_for_atlas(atlas: Path) -> Path | None` once and `atlas_export.py` imports it [core: one-definition].
- **Tests:** per locking script, `test_<script>_refuses_while_the_corpus_is_held_without_reading_or_paying`: driver `hold` on the corpus; `corpus_lock.DEFAULT_WAIT` patched to `False`; the script's first corpus read (`build_catalog` for `atlas_build`, `load_atlas` for `atlas_render` and `atlas_export`, `build_pages` for `wiki_concepts`, `collect_rows` for `remediation_sweep`, `find_regressions` for `remediation_restore`) and every paid call (`fetch_privacy_status` for `atlas_export`, the YouTube duration fetch for `remediation_sweep`) replaced by recorders; call the real `main(argv)`, or `main()` with `sys.argv` patched for `wiki_concepts`, `remediation_sweep` and `remediation_restore`; assert `SystemExit` code `== 4`, no recorder ran, the output file absent. Without a holder each writes as today. `test_dry_run_takes_no_lock` for `remediation_sweep` and `remediation_restore` (runs to completion while held). `test_atlas_render_outside_an_atlas_folder_takes_no_lock` (the demo layout runs while a corpus is held). `test_scan_rebuild_reenters_render_without_waiting`: the real `rebuild_free_derived_layers` inside an in-process `hold` on the same corpus, run in a daemon thread with a bounded join, completes and writes `catalog.html`.
- **Falsify:** drop the acquisition from any one script (its refusal test fails); make `corpus_for_atlas` always return the grandparent (`test_atlas_render_outside_an_atlas_folder_takes_no_lock`); break re-entrancy (`test_scan_rebuild_reenters_render_without_waiting` fails on its bounded join).

#### U7. Remaining docs and guardrails

- **Files:** `README.md`, `skills/video-intel/SKILL.md`, `skills/video-intel-search/SKILL.md`, `docs/troubleshooting.md`, `.claude/rules/scan-config.md`. Root `CLAUDE.md` untouched (79,679 characters against the 80,000 budget).
- **`.claude/rules/scan-config.md`:**
  - The issue #312 entry's accepted limits: "two processes writing the state file at once can lose a new entry until the next run (tracked with the corpus lock, issue #303)" becomes "two `main()` runs no longer write the state file at once (the corpus lock serializes `scan` and `headlines`); a library caller that bypasses `main()` can still race".
  - New entry, "**One lock per corpus; a second writer waits (issue #303).**" carrying: the three files and where they live; the ONE predicate and the ONE preview table and that the lock set is derived from `CONFIG_BACKUP_COMMANDS` plus `profile init`; acquisition after `load_config` and before the snapshot, outside its warning-only handler, released after `flush_topic_stamps`; nothing before acquisition reads corpus state, and a run that waited re-reads its config (a different corpus exits 1); the per-platform held signal and fail-closed on everything else, `EBADF` included; acquisition is transactional and thread-safe; `.busy` contention is a wait, never a failure, and is not subject to `--no-wait`; readers probe `.busy` only (one retry), never `.lock`, and never create files; holder details are "last recorded" metadata, never exclusion; no unlink, no takeover by time, no break flag; re-entrancy is per process, so a script that spawns a `video_intel.py` writer must not hold the lock across the child (`remediation_run.py` takes none); exit 4 and 130; the exceptions in R12; library callers are outside the guard. Test contract: `tests/test_corpus_lock.py`, `tests/test_corpus_lock_cli.py`, `tests/test_corpus_lock_subprocess.py` and the U6 tests [core: real-caller]. Falsifications listed.
- **`docs/troubleshooting.md`:** a quick-reference row "A command prints 'Waiting for the corpus lock'" (cause: another writing command on the same corpus; recovery: let it finish, Ctrl-C to cancel with nothing written, or `--no-wait` to fail fast with exit 4; status `auto (#PR)`), and a scenario section covering: exit code 4; the readers' note and that its details are last recorded; a killed writer (the OS frees the lock and the next writer acquires within seconds; leftover details are ignored); a writer that hangs keeps the queue waiting until it is stopped (stop it; the lock frees itself); two Python installations that resolve different user-state folders (for example a Store-packaged Python) do not exclude each other; library callers and the scripts in R12's exception list are outside the guard; the operating rule: one writer machine at a time for a synced corpus.
- **`README.md`:** a short "Running two commands at once" subsection under Usage (wait in line, `--no-wait` and exit 4, previews and readers never wait, one writer machine at a time).
- **`skills/video-intel/SKILL.md`:** a short section near "When processing fails": a second writing command waits and says for whom; offer `--no-wait` when the user wants fail-fast; do not start a second writer to "speed things up"; previews always run. The existing scan paragraph's "Every other corpus-mutating command does the same." gains "(previews such as `--dry-run` write no snapshot)". Frontmatter `description` gains one trigger phrase, "another command is already running", per skill parity. No issue numbers or dates in SKILL.md text.
- **`skills/video-intel-search/SKILL.md`:** one sentence: when a writing command is running, search and nugget say so in one line and still answer; results come from the index, which a scan does not update.
- **Prose rules for every new line:** no em-dashes or en-dashes, no hard-wrapped new paragraphs; an edit inside an existing hard-wrapped paragraph keeps that paragraph's existing line structure.
- **Phase end:** full suite green; commit.

### Gate (after Phase C)

- Full suite on Windows; the Linux harness under WSL (orchestrator) and on Windows; record the measured added suite runtime; run each falsification listed in Phases A to C once and record which test failed, with a `cp` backup of the file rather than `git checkout --` for the revert.

---

## Verification Contract

| Gate | Command |
|---|---|
| Format changed files | `ruff format <changed files>` |
| Format check | `ruff format --check .` |
| Lint | `ruff check scripts tests` |
| Suite (end of every phase) | `python -m pytest tests/ -q --ignore=tests/evals -p no:cacheprovider` |
| New tests alone, timed | `python -m pytest tests/test_corpus_lock.py tests/test_corpus_lock_cli.py tests/test_corpus_lock_subprocess.py -q --durations=10` |
| Linux | `python3 docs/plans/gate1-evidence/issue-303-corpus-lock-harness.py --state-dir <linux tmp>` under WSL |

---

## Open questions and resolutions

All resolved by the orchestrator; kept here with the resolution so the reasoning stays visible.

1. **`wiki_atlas.py`** writes wherever `--wiki-dir` points but has no corpus argument and reads only DuckDB. Resolved: not locked, documented.
2. **`atlas_render.py` / `atlas_export.py`** have no corpus argument. Resolved: lock only when the atlas file sits in a corpus `_atlas` folder, key = that folder's parent.
3. **`remediation_run.py`** cannot hold the lock across its child `process` commands (re-entrancy is per process, so that would deadlock). Resolved: it takes no lock of its own; each child locks per video; its own backup and state writes are documented as outside the guard. `remediation_sweep.py` and `remediation_restore.py` lock at entry unless `--dry-run`; `wiki_concepts.py` and `atlas_build.py` lock at entry.
4. **Re-entrancy** is needed only because `rebuild_free_derived_layers` calls `atlas_render.main([...])` under the scan's lock (it calls `atlas_build.build_catalog`, a library function, not `atlas_build.main`). Resolved: re-entrancy stays.
5. **Previews create a missing corpus root** (`resolve_output_dir(create=True)` in every preview except `repair-timestamps`), and several spend YouTube quota while unlocked. Resolved: accepted limits, documented.
6. **The Windows "held" errno** is not in the Python documentation (only "OSError is raised"); `EACCES` / `EDEADLOCK` come from the Microsoft CRT `_locking` documentation. Resolved: pinned by a test against the real call; a different errno is a stop condition.
7. **The snapshot sentence to edit** exists only in the `backfill-durations` entry of `scan-config.md`; no `assess-transcripts` sentence of that kind exists. Resolved: only that sentence is edited (U3).
8. **Release after a hard kill on Windows** has OS-dependent timing per Microsoft's `LockFileEx` documentation. Resolved: the next writer acquires within a bounded poll (10 s in tests); the docs say "within seconds", not "at once".
9. **`intel_graph.py load`** reads the corpus unlocked. Resolved: accepted, documented.
10. **`nugget --output PATH`** keeps overwrite semantics. Resolved.
11. **`briefings`** stays classified read-only for snapshots in `tests/test_config_backup.py`; in the lock inventory it is "never locks: writes only its own new file by exclusive create". Resolved.
12. **Root `CLAUDE.md`** is 79,679 characters against the 80,000 budget (an earlier draft of this plan quoted a byte count, which is larger because of non-ASCII characters; the budget test counts characters). Resolved: it does not grow.

## Risks

- **Windows lock-release latency after a kill:** bounded polling in tests; "within seconds" in the docs.
- **A reader paused while holding its probe lock** makes a writer wait on `.busy` (by design, never a failure); the one-line note after 5 s makes the cause visible, and Ctrl-C still exits 130 with `.lock` released.
- **Holder details can be stale or missing** (a reader racing a replace, a failed replace on Windows): they are labelled "last recorded" and never used for exclusion.
- **Tests that run `main()` in-process now take a real lock:** the autouse fixture keeps every lock in a per-test folder; the registry is per process and released in `finally`, so a test that leaves it non-empty is a bug the U4 failure tests would show.
- **Subprocess tests resolving the live corpus:** the driver patches `load_config` and asserts the scratch path before running, because a plugin-local `config.yaml` in a checkout would otherwise win the precedence chain.
- **Two Python installations with different user-state folders** (for example a Store-packaged Python, which redirects `LOCALAPPDATA`) do not exclude each other; documented.
- **A waiting command occupies its terminal, three or more waiters have no guaranteed order, and a hung writer keeps the queue waiting until stopped:** accepted limits, stated in the docs.

## Document review (non-interactive, coherence + feasibility)

Incorporated: a structured doc review and an independent design pass. Their outcomes are in the design text above: `.busy` contention as a wait (KTD1), transactional and thread-safe acquisition (KTD1, R3, R4), the config re-read after a wait (KTD4), the probe retry and "last recorded" details (KTD1, KTD6), `remediation_run.py` without a lock (script table), no `build_parser()` refactor (U3), one module-scoped holder for the no-wait parametrization (U4), per-process event files and the no-hang rule (shared test infrastructure), and the stated limit on Windows console interrupts (U1).

## Definition of Done

- R1 to R14 each proven by a named test or doc edit above; every falsification fails exactly its own test; each phase ends with the full suite green and its own commit; the Linux harness passes under WSL; the measured added suite runtime is under 60 s and recorded in the PR.
