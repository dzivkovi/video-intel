"""One OS-level lock per corpus, so a second writing command waits instead of racing.

Standard library only, and it never imports `video_intel`: the standalone
scripts that write into a corpus take the same lock without dragging in the
curate stack.

Three files per corpus live in `lock_state_dir()`, a local user-state folder
(never inside the corpus, which may sit on a cloud-synced mount):

- `<key>.lock` - the writer lock. Exactly one process holds it per corpus.
- `<key>.busy` - held by the writer for the whole hold, and probed by readers.
  A reader never touches `.lock`, so a reader's probe can never make a
  fail-fast writer refuse; at worst a writer waits a moment on `.busy`.
- `<key>.holder.json` - "last recorded" details about the writer. Metadata
  only, never used for exclusion.

Lock files are never unlinked and a lock is never taken over by elapsed time:
the OS releases both locks when the holding process ends.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import posixpath
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

EXIT_CORPUS_BUSY = 4
WAIT_POLL_SECONDS = 1.0
WAIT_REPORT_SECONDS = 180.0
BUSY_WAIT_REPORT_SECONDS = 5.0  # one line if a reader's probe keeps .busy longer than this
DEFAULT_WAIT = True  # test seam read at call time; not a flag, not a documented knob

_PROBE_RETRY_SECONDS = 0.1
_HOLDER_REPLACE_ATTEMPTS = 200
_HOLDER_REPLACE_STEP_SECONDS = 0.01

_IS_WINDOWS = os.name == "nt"

log = logging.getLogger("corpus_lock")

# The "held by another process" signal, per platform. Anything else a lock
# call raises is a lock-system failure, never "held" (fail closed).
if _IS_WINDOWS:
    import msvcrt

    # CRT `_locking`: EACCES for a locking violation, EDEADLOCK after the
    # blocking modes give up. EBADF / EINVAL are failures.
    _HELD_ERRNOS = frozenset({errno.EACCES, errno.EDEADLOCK})

    def _platform_lock(fd: int) -> None:
        # msvcrt.locking locks nbytes from the current position: pin byte 0.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _platform_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    # flock(2) EWOULDBLOCK; an fcntl-emulated flock may raise EACCES or EAGAIN.
    _HELD_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})

    def _platform_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _platform_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class LockPaths(NamedTuple):
    lock: Path
    busy: Path
    holder: Path


class CorpusBusy(Exception):
    """Another process holds the corpus lock and this call was not allowed to wait."""

    def __init__(self, details: dict | None) -> None:
        super().__init__(describe_holder(details))
        self.details = details


class LockSystemError(Exception):
    """The lock system itself failed (not "held"): callers fail closed."""

    def __init__(self, path: Path, error: OSError) -> None:
        super().__init__(f"{path}: {error}")
        self.path = path
        self.error = error


class Acquired(NamedTuple):
    waited: bool


@dataclass
class _Held:
    lock_fd: int
    busy_fd: int
    depth: int
    paths: LockPaths


_REGISTRY: dict[str, _Held] = {}
_KEY_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_MUTEX = threading.Lock()  # dictionary lookups and updates only, never held while polling

# Module attributes so a unit test can drive a fake clock.
_sleep = time.sleep
_monotonic = time.monotonic


def _stderr_line(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def lock_state_dir() -> Path:
    """The local user-state folder that holds every corpus's lock files."""
    if _IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return root / "video-intel" / "locks"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and posixpath.isabs(xdg):
        return Path(xdg) / "video-intel" / "locks"
    return Path.home() / ".local" / "state" / "video-intel" / "locks"


def corpus_key(output_dir: str | os.PathLike[str]) -> str:
    """One key per corpus, whatever spelling of its path a caller uses."""
    normalized = os.path.normcase(os.path.realpath(os.fspath(output_dir)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def lock_paths(output_dir: str | os.PathLike[str]) -> LockPaths:
    state = lock_state_dir()
    key = corpus_key(output_dir)
    return LockPaths(state / f"{key}.lock", state / f"{key}.busy", state / f"{key}.holder.json")


def describe_holder(details: dict | None) -> str:
    if not details:
        return "details unavailable"
    parts = [str(details.get("command") or "unknown command")]
    channel = details.get("channel")
    if channel:
        parts.append(f"channel {channel}")
    parts.append(f"PID {details.get('pid', '?')}")
    parts.append(f"started {details.get('started', '?')}")
    return ", ".join(parts) + " (last recorded)"


def _open_lock_file(path: Path) -> int:
    # Non-inheritable by default (PEP 446): a child process never inherits the lock.
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def _try_lock(fd: int) -> bool:
    """True when acquired, False when another holder has it; any other error raises."""
    while True:
        try:
            _platform_lock(fd)
            return True
        except OSError as e:
            if e.errno in _HELD_ERRNOS:
                return False
            if e.errno == errno.EINTR:
                continue
            raise


def _read_holder(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _remove_own_holder(paths: LockPaths) -> None:
    try:
        if _read_holder(paths.holder).get("pid") == os.getpid():
            paths.holder.unlink(missing_ok=True)
    except OSError as e:
        log.debug("Could not remove holder details %s: %s", paths.holder, e)


def _write_holder(paths: LockPaths, output_dir: str | os.PathLike[str], command: str, channel: str | None) -> None:
    """Record who holds the lock. Metadata only: any failure degrades, never fails the acquisition."""
    details = {
        "command": command,
        "channel": channel,
        "pid": os.getpid(),
        "started": datetime.now().isoformat(timespec="seconds"),
        "corpus": os.path.realpath(os.fspath(output_dir)),
    }
    tmp = paths.holder.with_name(f"{paths.holder.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(json.dumps(details, ensure_ascii=False).encode("utf-8"))
        for attempt in range(_HOLDER_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, paths.holder)
                return
            except PermissionError:
                # Windows refuses the replace while a reader has the file open.
                if attempt + 1 < _HOLDER_REPLACE_ATTEMPTS:
                    time.sleep(_HOLDER_REPLACE_STEP_SECONDS)
    except OSError as e:
        log.debug("Could not write holder details %s: %s", paths.holder, e)
    for leftover in (paths.holder, tmp):
        try:
            leftover.unlink(missing_ok=True)
        except OSError as e:
            log.debug("Could not remove %s: %s", leftover, e)


def _lock_or_raise(fd: int, path: Path) -> bool:
    try:
        return _try_lock(fd)
    except OSError as e:
        raise LockSystemError(path, e) from e


def _poll_writer_lock(
    fd: int, path: Path, output_dir: str | os.PathLike[str], wait: bool, report: Callable[[str], None]
) -> bool:
    """Take `.lock`, waiting in line when allowed. Returns True when any attempt found it held."""
    started = None
    next_report = 0.0
    while not _lock_or_raise(fd, path):
        if not wait:
            raise CorpusBusy(probe_writer(output_dir))
        now = _monotonic()
        if started is None:
            started = now
            next_report = now + WAIT_REPORT_SECONDS
            report(
                "Waiting for the corpus lock: "
                + describe_holder(probe_writer(output_dir))
                + ". This command continues on its own when the lock frees; Ctrl-C cancels with nothing written."
            )
        elif now >= next_report:
            minutes = int((now - started) // 60)
            report(f"Still waiting for the corpus lock ({minutes} min): {describe_holder(probe_writer(output_dir))}.")
            next_report += WAIT_REPORT_SECONDS
        _sleep(WAIT_POLL_SECONDS)
    return started is not None


def _poll_busy(fd: int, path: Path, report: Callable[[str], None]) -> None:
    """Take `.busy`. Only a reader's probe can hold it, so contention is always a wait, never a refusal."""
    started = None
    reported = False
    while not _lock_or_raise(fd, path):
        now = _monotonic()
        if started is None:
            started = now
        elif not reported and now - started > BUSY_WAIT_REPORT_SECONDS:
            report("Waiting for a reader's lock probe to finish.")
            reported = True
        _sleep(WAIT_POLL_SECONDS)


def _acquire_os(
    output_dir: str | os.PathLike[str],
    command: str,
    channel: str | None,
    wait: bool,
    report: Callable[[str], None],
) -> tuple[_Held, bool]:
    """One transactional OS acquisition: on any failed or interrupted exit every descriptor is closed."""
    paths = lock_paths(output_dir)
    opened: list[int] = []
    locked: list[int] = []
    wrote_holder = False
    done = False
    try:
        try:
            os.makedirs(paths.lock.parent, exist_ok=True)
        except OSError as e:
            raise LockSystemError(paths.lock.parent, e) from e
        try:
            lock_fd = _open_lock_file(paths.lock)
        except OSError as e:
            raise LockSystemError(paths.lock, e) from e
        opened.append(lock_fd)
        waited = _poll_writer_lock(lock_fd, paths.lock, output_dir, wait, report)
        locked.append(lock_fd)
        _write_holder(paths, output_dir, command, channel)
        wrote_holder = True
        try:
            busy_fd = _open_lock_file(paths.busy)
        except OSError as e:
            raise LockSystemError(paths.busy, e) from e
        opened.append(busy_fd)
        _poll_busy(busy_fd, paths.busy, report)
        locked.append(busy_fd)
        done = True
        return _Held(lock_fd=lock_fd, busy_fd=busy_fd, depth=1, paths=paths), waited
    finally:
        if not done:
            if wrote_holder:
                _remove_own_holder(paths)
            for fd in reversed(locked):
                try:
                    _platform_unlock(fd)
                except OSError as e:
                    log.debug("Unlock during a failed acquisition raised: %s", e)
            for fd in reversed(opened):
                try:
                    os.close(fd)
                except OSError as e:
                    log.debug("Close during a failed acquisition raised: %s", e)


def _key_lock(key: str) -> threading.Lock:
    with _REGISTRY_MUTEX:
        key_lock = _KEY_LOCKS.get(key)
        if key_lock is None:
            key_lock = _KEY_LOCKS[key] = threading.Lock()
        return key_lock


def acquire(
    output_dir: str | os.PathLike[str],
    *,
    command: str,
    channel: str | None = None,
    wait: bool | None = None,
    report: Callable[[str], None] = _stderr_line,
) -> Acquired:
    """Take the corpus lock, re-entrantly and thread-safely within this process.

    Raises `CorpusBusy` when another process holds it and waiting is off, and
    `LockSystemError` when the lock system fails. `wait=None` reads
    `DEFAULT_WAIT` at call time.
    """
    if wait is None:
        wait = DEFAULT_WAIT
    key = corpus_key(output_dir)
    # Held for the whole OS acquisition, waiting included: a second thread of
    # this process blocks here, then finds the registry entry and only
    # increments. Re-opening the file in-process would conflict with our own lock.
    with _key_lock(key):
        with _REGISTRY_MUTEX:
            held = _REGISTRY.get(key)
            if held is not None:
                held.depth += 1
                return Acquired(waited=False)
        held, waited = _acquire_os(output_dir, command, channel, wait, report)
        with _REGISTRY_MUTEX:
            _REGISTRY[key] = held
        return Acquired(waited=waited)


def _release_os(held: _Held) -> None:
    _remove_own_holder(held.paths)
    first_error: OSError | None = None
    for step in (
        lambda: _platform_unlock(held.busy_fd),
        lambda: os.close(held.busy_fd),
        lambda: _platform_unlock(held.lock_fd),
        lambda: os.close(held.lock_fd),
    ):
        try:
            step()
        except OSError as e:
            if first_error is None:
                first_error = e
    if first_error is not None:
        raise first_error


def release(output_dir: str | os.PathLike[str]) -> None:
    """Drop one level of the hold; at depth zero unlock and close both files (never unlink them)."""
    key = corpus_key(output_dir)
    with _key_lock(key):
        with _REGISTRY_MUTEX:
            held = _REGISTRY.get(key)
            if held is None:
                raise RuntimeError(f"The corpus lock is not held for {os.fspath(output_dir)}")
            if held.depth > 1:
                held.depth -= 1
                return
        try:
            _release_os(held)
        finally:
            with _REGISTRY_MUTEX:
                _REGISTRY.pop(key, None)


def held_depth(output_dir: str | os.PathLike[str]) -> int:
    key = corpus_key(output_dir)
    with _REGISTRY_MUTEX:
        held = _REGISTRY.get(key)
        return held.depth if held is not None else 0


@contextmanager
def hold(output_dir: str | os.PathLike[str], **acquire_kwargs) -> Iterator[Acquired]:
    acquired = acquire(output_dir, **acquire_kwargs)
    try:
        yield acquired
    finally:
        release(output_dir)


@contextmanager
def hold_for_cli(
    output_dir: str | os.PathLike[str],
    *,
    command: str,
    channel: str | None = None,
    wait: bool | None = None,
    report: Callable[[str], None] = _stderr_line,
    error: Callable[[str], None] = _stderr_line,
) -> Iterator[Acquired]:
    """The one command-line wrapper: maps every acquisition failure to one line and an exit code."""
    try:
        acquired = acquire(output_dir, command=command, channel=channel, wait=wait, report=report)
    except CorpusBusy as e:
        error(
            f"Another command is writing to this corpus: {describe_holder(e.details)}. "
            "Exiting without writing anything (exit 4). Re-run when it finishes, or drop --no-wait to wait in line."
        )
        raise SystemExit(EXIT_CORPUS_BUSY) from None
    except KeyboardInterrupt:
        error("Interrupted while waiting for the corpus lock; nothing was written.")
        raise SystemExit(130) from None
    except LockSystemError as e:
        error(f"Corpus lock unavailable ({e.path}: {e.error}). Nothing was written.")
        raise SystemExit(1) from None
    try:
        yield acquired
    finally:
        release(output_dir)


def probe_writer(output_dir: str | os.PathLike[str]) -> dict | None:
    """Is a writer running? Never raises, never creates a file, never touches `.lock`.

    Returns None when no writer runs (or the probe failed), else the last
    recorded holder details (`{}` when they are missing or unreadable).
    """
    try:
        paths = lock_paths(output_dir)
        for attempt in range(2):
            try:
                fd = os.open(paths.busy, os.O_RDWR)
            except FileNotFoundError:
                return None  # no writer has ever run here
            try:
                if _try_lock(fd):
                    _platform_unlock(fd)
                    return None
            finally:
                os.close(fd)
            if attempt == 0:
                # Another reader's probe can hold `.busy` for an instant: retry once.
                time.sleep(_PROBE_RETRY_SECONDS)
        return _read_holder(paths.holder)
    except OSError as e:
        log.debug("Corpus lock probe failed: %s", e)
        return None
