"""Tests for scripts/corpus_lock.py: one OS-level lock per corpus (issue #303, Phase A).

Cross-process tests run `tests/corpus_lock_driver.py` as a child process.
Every child gets the test's own state folder (the autouse
`corpus_lock_state_dir` fixture in conftest.py), so no lock file ever lands
in the real user-state folder. Ordering never rests on a bare sleep: children
signal through per-process ready/release files and event files, and every
wait is a bounded poll. Every in-process acquisition that a mutation could
turn into a deadlock runs through `run_bounded`, which fails the test instead
of hanging the suite.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import corpus_lock

# Captured at collection time, before the autouse fixture swaps it per test.
_REAL_LOCK_STATE_DIR = corpus_lock.lock_state_dir

DRIVER = Path(__file__).resolve().parent / "corpus_lock_driver.py"
POLL_SECONDS = 0.02
BOUND_SECONDS = 20.0

if os.name == "nt":
    DOCUMENTED_HELD_ERRNOS = {errno.EACCES, errno.EDEADLOCK}
else:
    DOCUMENTED_HELD_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}


def run_bounded(fn, *, timeout: float = BOUND_SECONDS):
    """Run `fn` in a daemon thread; re-raise what it raised; fail (never hang) past `timeout`."""
    outcome: dict = {}

    def target():
        try:
            outcome["value"] = fn()
        except BaseException as e:  # SystemExit and KeyboardInterrupt included
            outcome["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        pytest.fail(f"did not finish within {timeout} s (a deadlock or an endless wait)")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def wait_until(predicate, what: str, timeout: float = BOUND_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout} s waiting for {what}")
        time.sleep(POLL_SECONDS)


def fd_is_closed(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


class Children:
    """Starts driver child processes and reads their event files."""

    def __init__(self, state_dir: Path, sync: Path) -> None:
        self.state_dir = state_dir
        self.sync = sync
        self.procs: list[subprocess.Popen] = []
        self._try_count = 0

    def _argv(self, name: str, mode: str, corpus: Path, *extra: str) -> list[str]:
        return [
            sys.executable,
            str(DRIVER),
            "--state-dir",
            str(self.state_dir),
            "--sync",
            str(self.sync),
            "--name",
            name,
            mode,
            "--corpus",
            str(corpus),
            *extra,
        ]

    def output(self, name: str) -> str:
        path = self.sync / f"{name}.out"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def start(self, name: str, mode: str, corpus: Path, *extra: str, wait_ready: bool = True) -> subprocess.Popen:
        with open(self.sync / f"{name}.out", "wb") as out:
            proc = subprocess.Popen(self._argv(name, mode, corpus, *extra), stdout=out, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        if wait_ready:
            ready = self.sync / f"{name}.ready"
            wait_until(lambda: ready.exists() or proc.poll() is not None, f"{name}.ready")
            if not ready.exists():
                pytest.fail(f"{name} exited {proc.returncode} before it was ready:\n{self.output(name)}")
        return proc

    def release(self, name: str, proc: subprocess.Popen) -> None:
        (self.sync / f"{name}.release").write_text("", encoding="utf-8")
        proc.wait(timeout=BOUND_SECONDS)
        assert proc.returncode == 0, self.output(name)

    def try_exit(self, corpus: Path) -> int:
        self._try_count += 1
        name = f"try{self._try_count}"
        result = subprocess.run(self._argv(name, "try", corpus), capture_output=True, text=True, timeout=BOUND_SECONDS)
        assert result.returncode in (0, corpus_lock.EXIT_CORPUS_BUSY), result.stdout + result.stderr
        return result.returncode

    def events(self, name: str) -> list[tuple[int, str]]:
        lines = (self.sync / f"{name}.events").read_text(encoding="utf-8").splitlines()
        return [(int(ns), event) for ns, event in (line.split(" ", 1) for line in lines)]

    def event_time(self, name: str, event: str) -> int:
        matches = [ns for ns, ev in self.events(name) if ev == event]
        assert matches, f"{name} recorded no {event!r} event: {self.events(name)}"
        return matches[0]


@pytest.fixture(autouse=True)
def fresh_lock_state():
    """Give each test its own registry, key locks and mutex; fail a test that leaks a hold."""
    saved = (corpus_lock._REGISTRY, corpus_lock._KEY_LOCKS, corpus_lock._REGISTRY_MUTEX)
    corpus_lock._REGISTRY = {}
    corpus_lock._KEY_LOCKS = {}
    corpus_lock._REGISTRY_MUTEX = threading.Lock()
    yield
    leaked = dict(corpus_lock._REGISTRY)
    corpus_lock._REGISTRY, corpus_lock._KEY_LOCKS, corpus_lock._REGISTRY_MUTEX = saved
    assert not leaked, f"test left the corpus lock held: {leaked}"


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    path = tmp_path / "corpus"
    path.mkdir()
    return path


@pytest.fixture
def children(corpus_lock_state_dir: Path, tmp_path: Path):
    sync = tmp_path / "sync"
    sync.mkdir()
    kids = Children(corpus_lock_state_dir, sync)
    yield kids
    for proc in kids.procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def fast_polls(monkeypatch):
    monkeypatch.setattr(corpus_lock, "WAIT_POLL_SECONDS", 0.02)


def acquire_now(corpus: Path, **kwargs) -> corpus_lock.Acquired:
    kwargs.setdefault("command", "scan")
    kwargs.setdefault("wait", False)
    return run_bounded(lambda: corpus_lock.acquire(corpus, **kwargs))


def release_now(corpus: Path) -> None:
    run_bounded(lambda: corpus_lock.release(corpus))


# --- keys and the state folder ---------------------------------------------


def test_key_same_for_two_spellings_of_one_path(tmp_path):
    # Arrange
    corpus = tmp_path / "corpus"
    (corpus / "sub").mkdir(parents=True)
    spellings = [str(corpus) + os.sep, str(corpus / "sub" / "..")]
    if os.name == "nt":
        spellings += [str(corpus).upper(), str(corpus).lower(), str(corpus).replace("\\", "/")]
    else:
        link = tmp_path / "link"
        link.symlink_to(corpus, target_is_directory=True)
        spellings.append(str(link))

    # Act
    keys = {corpus_lock.corpus_key(s) for s in spellings}

    # Assert
    assert keys == {corpus_lock.corpus_key(corpus)}


def test_key_differs_for_two_corpora(tmp_path):
    assert corpus_lock.corpus_key(tmp_path / "one") != corpus_lock.corpus_key(tmp_path / "two")


def test_state_dir_windows_and_posix_rules(monkeypatch, tmp_path):
    assert _REAL_LOCK_STATE_DIR.__module__ == "corpus_lock"
    assert _REAL_LOCK_STATE_DIR.__name__ == "lock_state_dir"

    monkeypatch.setattr(corpus_lock, "_IS_WINDOWS", True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
    assert _REAL_LOCK_STATE_DIR() == tmp_path / "appdata" / "video-intel" / "locks"
    monkeypatch.delenv("LOCALAPPDATA")
    assert _REAL_LOCK_STATE_DIR() == Path.home() / "AppData" / "Local" / "video-intel" / "locks"

    monkeypatch.setattr(corpus_lock, "_IS_WINDOWS", False)
    monkeypatch.setenv("XDG_STATE_HOME", "/xdg/state")
    assert _REAL_LOCK_STATE_DIR() == Path("/xdg/state") / "video-intel" / "locks"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert _REAL_LOCK_STATE_DIR() == Path.home() / ".local" / "state" / "video-intel" / "locks"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert _REAL_LOCK_STATE_DIR() == Path.home() / ".local" / "state" / "video-intel" / "locks"


# --- cross-process exclusion -------------------------------------------------


def test_second_process_try_acquire_is_refused_while_held(children, corpus):
    holder = children.start("A", "hold", corpus)

    assert children.try_exit(corpus) == corpus_lock.EXIT_CORPUS_BUSY

    children.release("A", holder)
    assert children.try_exit(corpus) == 0


def test_held_signal_errno_is_the_documented_one(children, corpus, monkeypatch):
    # Arrange: a real holder in another process, and the real platform call under a spy.
    holder = children.start("A", "hold", corpus)
    caught: list[OSError] = []
    real_lock = corpus_lock._platform_lock

    def spy(fd):
        try:
            real_lock(fd)
        except OSError as e:
            caught.append(e)
            raise

    monkeypatch.setattr(corpus_lock, "_platform_lock", spy)
    fd = corpus_lock._open_lock_file(corpus_lock.lock_paths(corpus).lock)

    # Act
    try:
        got_it = corpus_lock._try_lock(fd)
    finally:
        os.close(fd)

    # Assert: the errno observed on THIS platform is the documented one (a stop condition otherwise).
    assert got_it is False
    assert len(caught) == 1
    assert caught[0].errno in DOCUMENTED_HELD_ERRNOS, (caught[0].errno, caught[0])
    children.release("A", holder)


@pytest.mark.parametrize("err", [errno.EBADF, errno.EINVAL], ids=["EBADF", "EINVAL"])
def test_non_held_lock_error_fails_closed(corpus, monkeypatch, err):
    # Arrange
    opened: list[int] = []
    real_open = corpus_lock._open_lock_file

    def tracking_open(path):
        fd = real_open(path)
        opened.append(fd)
        return fd

    def broken_lock(_fd):
        raise OSError(err, os.strerror(err))

    monkeypatch.setattr(corpus_lock, "_open_lock_file", tracking_open)
    monkeypatch.setattr(corpus_lock, "_platform_lock", broken_lock)
    reports: list[str] = []
    errors: list[str] = []

    def body():
        with corpus_lock.hold_for_cli(corpus, command="scan", wait=True, report=reports.append, error=errors.append):
            pytest.fail("the body must not run when the lock system fails")

    # Act
    with pytest.raises(SystemExit) as exc:
        run_bounded(body)

    # Assert
    assert exc.value.code == 1
    assert len(errors) == 1
    assert str(corpus_lock.lock_paths(corpus).lock) in errors[0]
    assert errors[0].startswith("Corpus lock unavailable (")
    assert reports == []
    assert opened and all(fd_is_closed(fd) for fd in opened)
    assert corpus_lock.held_depth(corpus) == 0


def test_waiter_proceeds_when_holder_releases(children, corpus, fast_polls):
    # Arrange
    holder = children.start("A", "hold", corpus)
    reports: list[str] = []
    acquired_at: list[int] = []
    result: dict = {}

    def waiter():
        try:
            result["acquired"] = corpus_lock.acquire(corpus, command="taxonomy-build", wait=True, report=reports.append)
            acquired_at.append(time.perf_counter_ns())
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()

    # Act
    wait_until(lambda: reports, "the wait line")
    children.release("A", holder)
    thread.join(BOUND_SECONDS)

    # Assert
    assert not thread.is_alive(), "the waiter never proceeded after the holder released"
    assert "error" not in result, result.get("error")
    pattern = (
        rf"Waiting for the corpus lock: scan, channel alpha, PID {holder.pid}, "
        r"started \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2} \(last recorded\)\. "
        r"This command continues on its own when the lock frees; Ctrl-C cancels with nothing written\."
    )
    assert re.fullmatch(pattern, reports[0]), reports[0]
    assert children.event_time("A", "last-protected") < acquired_at[0]
    assert result["acquired"].waited is True
    release_now(corpus)


def test_wait_reports_at_start_and_every_three_minutes(corpus, monkeypatch):
    # Arrange: a fake clock, and the lock reported held for 400 polls.
    clock = [0.0]
    monkeypatch.setattr(corpus_lock, "_monotonic", lambda: clock[0])

    def fake_sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(corpus_lock, "_sleep", fake_sleep)
    calls = [0]
    real_try = corpus_lock._try_lock

    def held_400_times(fd):
        calls[0] += 1
        return False if calls[0] <= 400 else real_try(fd)

    monkeypatch.setattr(corpus_lock, "_try_lock", held_400_times)
    reports: list[tuple[float, str]] = []

    # Act
    acquired = acquire_now(corpus, wait=True, report=lambda line: reports.append((clock[0], line)))

    # Assert
    assert [t for t, _ in reports] == [0.0, 180.0, 360.0]
    assert reports[0][1].startswith("Waiting for the corpus lock: ")
    assert reports[1][1].startswith("Still waiting for the corpus lock (3 min): ")
    assert reports[2][1].startswith("Still waiting for the corpus lock (6 min): ")
    assert acquired.waited is True
    release_now(corpus)


def test_no_wait_raises_with_holder_details(children, corpus):
    holder = children.start("A", "hold", corpus)

    with pytest.raises(corpus_lock.CorpusBusy) as exc:
        acquire_now(corpus, wait=False)

    assert exc.value.details["command"] == "scan"
    assert exc.value.details["channel"] == "alpha"
    assert exc.value.details["pid"] == holder.pid
    children.release("A", holder)


def test_default_wait_is_read_at_call_time(children, corpus, monkeypatch):
    holder = children.start("A", "hold", corpus)
    monkeypatch.setattr(corpus_lock, "DEFAULT_WAIT", False)

    with pytest.raises(corpus_lock.CorpusBusy):
        run_bounded(lambda: corpus_lock.acquire(corpus, command="scan"), timeout=5)

    children.release("A", holder)


# --- re-entrancy and threads -------------------------------------------------


def test_reentrant_depth_and_single_release(children, corpus):
    first = acquire_now(corpus)
    again = acquire_now(corpus)
    assert corpus_lock.held_depth(corpus) == 2
    assert again.waited is False
    assert first.waited is False

    release_now(corpus)
    assert corpus_lock.held_depth(corpus) == 1
    assert children.try_exit(corpus) == corpus_lock.EXIT_CORPUS_BUSY

    release_now(corpus)
    assert corpus_lock.held_depth(corpus) == 0
    assert children.try_exit(corpus) == 0


def test_two_threads_share_one_os_acquisition(children, corpus, monkeypatch):
    # Arrange
    lock_opens: list[str] = []
    real_open = corpus_lock._open_lock_file

    def counting_open(path):
        if Path(path).suffix == ".lock":
            lock_opens.append(str(path))
        return real_open(path)

    monkeypatch.setattr(corpus_lock, "_open_lock_file", counting_open)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker():
        try:
            barrier.wait(timeout=BOUND_SECONDS)
            corpus_lock.acquire(corpus, command="scan", wait=False)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(2)]

    # Act
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(BOUND_SECONDS)

    # Assert
    assert not any(t.is_alive() for t in threads), "an acquiring thread never finished (deadlock)"
    assert errors == []
    assert corpus_lock.held_depth(corpus) == 2
    assert len(lock_opens) == 1
    release_now(corpus)
    assert children.try_exit(corpus) == corpus_lock.EXIT_CORPUS_BUSY
    release_now(corpus)
    assert children.try_exit(corpus) == 0


def test_waiting_on_one_corpus_does_not_block_another_corpus(children, corpus, tmp_path, fast_polls):
    # Arrange: this process waits on a corpus another process holds.
    holder = children.start("A", "hold", corpus)
    other = tmp_path / "other"
    other.mkdir()
    reports: list[str] = []
    waiter = threading.Thread(
        target=lambda: corpus_lock.acquire(corpus, command="scan", wait=True, report=reports.append), daemon=True
    )
    waiter.start()
    wait_until(lambda: reports, "the wait line")

    # Act / Assert: another corpus is taken and released at once meanwhile.
    acquire_now(other, wait=False)
    release_now(other)

    children.release("A", holder)
    waiter.join(BOUND_SECONDS)
    assert not waiter.is_alive()
    release_now(corpus)


# --- the .busy file ----------------------------------------------------------


def test_writer_waits_for_a_reader_paused_mid_probe(children, corpus, monkeypatch, fast_polls):
    # Arrange
    prober = children.start("P", "probe-hold", corpus)
    monkeypatch.setattr(corpus_lock, "BUSY_WAIT_REPORT_SECONDS", 0.1)
    reports: list[str] = []
    result: dict = {}

    def writer():
        try:
            result["acquired"] = corpus_lock.acquire(corpus, command="scan", wait=False, report=reports.append)
        except BaseException as e:
            result["error"] = e

    thread = threading.Thread(target=writer, daemon=True)

    # Act
    thread.start()
    wait_until(lambda: reports or "error" in result, "the reader-probe wait line")
    children.release("P", prober)
    thread.join(BOUND_SECONDS)

    # Assert
    assert not thread.is_alive()
    assert "error" not in result, result.get("error")
    assert reports == ["Waiting for a reader's lock probe to finish."]
    assert corpus_lock.held_depth(corpus) == 1
    release_now(corpus)


def test_ctrl_c_during_busy_wait_releases_the_lock(children, corpus, monkeypatch):
    # Arrange: a reader holds .busy, so the first sleep is inside the busy wait.
    prober = children.start("P", "probe-hold", corpus)

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(corpus_lock, "_sleep", interrupt)
    errors: list[str] = []

    def body():
        with corpus_lock.hold_for_cli(corpus, command="scan", wait=True, report=lambda _l: None, error=errors.append):
            pytest.fail("the body must not run after Ctrl-C")

    # Act
    with pytest.raises(SystemExit) as exc:
        run_bounded(body)

    # Assert
    assert exc.value.code == 130
    assert errors == ["Interrupted while waiting for the corpus lock; nothing was written."]
    assert corpus_lock.held_depth(corpus) == 0
    children.release("P", prober)
    assert children.try_exit(corpus) == 0


@pytest.mark.parametrize("step", ["after_lock_open", "after_lock_locked", "after_details_written", "after_busy_open"])
def test_interrupted_acquisition_closes_every_descriptor(children, corpus, monkeypatch, step):
    # Arrange: KeyboardInterrupt injected at one step of the acquisition.
    opened: list[int] = []
    opens = [0]
    tries = [0]
    real_open = corpus_lock._open_lock_file
    real_try = corpus_lock._try_lock

    def open_step(path):
        opens[0] += 1
        if step == "after_details_written" and opens[0] == 2:
            raise KeyboardInterrupt
        fd = real_open(path)
        opened.append(fd)
        return fd

    def try_step(fd):
        tries[0] += 1
        if (step == "after_lock_open" and tries[0] == 1) or (step == "after_busy_open" and tries[0] == 2):
            raise KeyboardInterrupt
        return real_try(fd)

    def write_step(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(corpus_lock, "_open_lock_file", open_step)
    monkeypatch.setattr(corpus_lock, "_try_lock", try_step)
    if step == "after_lock_locked":
        monkeypatch.setattr(corpus_lock, "_write_holder", write_step)

    # Act
    with pytest.raises(KeyboardInterrupt):
        acquire_now(corpus)

    # Assert
    assert corpus_lock._REGISTRY == {}
    assert corpus_lock.held_depth(corpus) == 0
    assert opened and all(fd_is_closed(fd) for fd in opened), opened
    monkeypatch.undo()
    assert children.try_exit(corpus) == 0


def test_release_attempts_every_close_even_if_one_unlock_fails(children, corpus, monkeypatch):
    # Arrange
    acquire_now(corpus)
    held = corpus_lock._REGISTRY[corpus_lock.corpus_key(corpus)]
    real_unlock = corpus_lock._platform_unlock

    def busy_unlock_fails(fd):
        if fd == held.busy_fd:
            raise OSError(errno.EIO, "injected unlock failure")
        real_unlock(fd)

    monkeypatch.setattr(corpus_lock, "_platform_unlock", busy_unlock_fails)

    # Act
    with pytest.raises(OSError, match="injected unlock failure"):
        release_now(corpus)

    # Assert
    assert fd_is_closed(held.busy_fd)
    assert fd_is_closed(held.lock_fd)
    assert corpus_lock.held_depth(corpus) == 0
    assert children.try_exit(corpus) == 0


# --- death, leftovers and probes ---------------------------------------------


def test_killed_holder_frees_the_lock(children, corpus):
    # Arrange
    holder = children.start("A", "hold", corpus)
    holder_file = corpus_lock.lock_paths(corpus).holder

    # Act
    holder.kill()
    holder.wait(timeout=BOUND_SECONDS)

    # Assert: within one 10 s bound the probe says "no writer" and a no-wait acquire succeeds.
    assert holder_file.exists(), "the killed holder should leave its details behind"
    deadline = time.monotonic() + 10
    acquired = False
    while not acquired:
        assert time.monotonic() < deadline, "the killed holder's lock was not freed within 10 s"
        leftover_present = holder_file.exists()
        if corpus_lock.probe_writer(corpus) is None:
            assert leftover_present
            try:
                acquire_now(corpus)
                acquired = True
            except corpus_lock.CorpusBusy:
                pass
        if not acquired:
            time.sleep(POLL_SECONDS)
    release_now(corpus)


def test_leftover_details_without_a_lock_are_not_a_writer(corpus):
    # Arrange: every file a dead writer leaves behind, no process holding anything.
    paths = corpus_lock.lock_paths(corpus)
    paths.lock.parent.mkdir(parents=True)
    paths.lock.write_bytes(b"")
    paths.busy.write_bytes(b"")
    paths.holder.write_text(json.dumps({"command": "scan", "channel": "alpha", "pid": 999999}), encoding="utf-8")

    # Act / Assert
    assert corpus_lock.probe_writer(corpus) is None
    acquire_now(corpus)
    release_now(corpus)


def test_probe_reports_running_writer_and_reads_details(children, corpus):
    holder = children.start("A", "hold", corpus)

    details = corpus_lock.probe_writer(corpus)

    assert details is not None
    assert (details["command"], details["channel"], details["pid"]) == ("scan", "alpha", holder.pid)
    assert corpus_lock.describe_holder(details).endswith("(last recorded)")
    children.release("A", holder)
    assert corpus_lock.probe_writer(corpus) is None


def test_probe_without_any_files_creates_nothing(corpus, corpus_lock_state_dir):
    assert not corpus_lock_state_dir.exists()

    assert corpus_lock.probe_writer(corpus) is None

    assert not corpus_lock_state_dir.exists()


def test_probe_never_raises_on_unreadable_state_dir(corpus, caplog):
    # Arrange: a directory squats on the .busy name, so opening it fails with a non-missing error.
    caplog.set_level(logging.DEBUG, logger="corpus_lock")
    corpus_lock.lock_paths(corpus).busy.mkdir(parents=True)

    # Act
    result = corpus_lock.probe_writer(corpus)

    # Assert
    assert result is None
    records = [r for r in caplog.records if r.name == "corpus_lock"]
    assert records and all(r.levelno == logging.DEBUG for r in records)


def test_two_readers_probing_together_do_not_report_each_other(children, corpus, monkeypatch):
    # Arrange: another reader holds .busy; it lets go as soon as this probe has seen it held.
    prober = children.start("P", "probe-hold", corpus)
    monkeypatch.setattr(corpus_lock, "_PROBE_RETRY_SECONDS", 0.5)
    seen: list[bool] = []
    real_try = corpus_lock._try_lock

    def spy(fd):
        got = real_try(fd)
        seen.append(got)
        if not got:
            (children.sync / "P.release").write_text("", encoding="utf-8")
        return got

    monkeypatch.setattr(corpus_lock, "_try_lock", spy)

    # Act
    result = run_bounded(lambda: corpus_lock.probe_writer(corpus))

    # Assert
    assert seen[0] is False, "the probe did not land inside the other reader's window"
    assert result is None
    prober.wait(timeout=BOUND_SECONDS)


def test_reader_probe_never_makes_a_no_wait_writer_fail(children, corpus, monkeypatch, fast_polls):
    # Arrange: a reader probing in a tight loop in another process.
    prober = children.start("R", "probe-loop", corpus, "--seconds", "60")
    busy_refusals = [0]

    def cycles():
        for _ in range(200):
            try:
                corpus_lock.acquire(corpus, command="scan", wait=False)
            except corpus_lock.CorpusBusy:
                busy_refusals[0] += 1
                continue
            corpus_lock.release(corpus)
            # A gap with no writer, so the loop runs free probes right where the next acquire lands.
            time.sleep(0.002)
        # One longer hold so the loop is sure to observe a running writer.
        corpus_lock.acquire(corpus, command="scan", wait=False)
        time.sleep(0.5)
        corpus_lock.release(corpus)

    # Act
    run_bounded(cycles, timeout=60)
    (children.sync / "R.release").write_text("", encoding="utf-8")
    prober.wait(timeout=BOUND_SECONDS)

    # Assert
    assert busy_refusals[0] == 0
    counts = [ev for _, ev in children.events("R") if ev.startswith("counts ")]
    writer_seen = int(re.search(r"writer=(\d+)", counts[0]).group(1))
    assert writer_seen >= 1, counts


@pytest.mark.parametrize("failures", ["three", "always"])
def test_holder_details_replace_retries_then_degrades(corpus, monkeypatch, failures):
    # Arrange
    paths = corpus_lock.lock_paths(corpus)
    paths.holder.parent.mkdir(parents=True)
    paths.holder.write_text(json.dumps({"command": "old", "pid": 999999}), encoding="utf-8")
    monkeypatch.setattr(corpus_lock, "_HOLDER_REPLACE_ATTEMPTS", 10)
    calls = [0]
    real_replace = os.replace

    def flaky_replace(src, dst):
        calls[0] += 1
        if failures == "always" or calls[0] <= 3:
            raise PermissionError(errno.EACCES, "held open by a reader")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    # Act
    acquire_now(corpus)
    monkeypatch.undo()

    # Assert
    leftovers = list(paths.holder.parent.glob("*.tmp"))
    try:
        assert leftovers == []
        if failures == "three":
            assert calls[0] == 4
            assert json.loads(paths.holder.read_text(encoding="utf-8"))["pid"] == os.getpid()
        else:
            assert calls[0] == 10
            assert not paths.holder.exists()
        assert corpus_lock.held_depth(corpus) == 1
    finally:
        release_now(corpus)


def test_lock_fd_is_not_inheritable(corpus):
    acquire_now(corpus)
    held = corpus_lock._REGISTRY[corpus_lock.corpus_key(corpus)]

    try:
        assert os.get_inheritable(held.lock_fd) is False
        assert os.get_inheritable(held.busy_fd) is False
    finally:
        release_now(corpus)


def test_lock_files_are_never_unlinked(corpus):
    paths = corpus_lock.lock_paths(corpus)

    acquire_now(corpus)
    release_now(corpus)

    assert paths.lock.exists()
    assert paths.busy.exists()
