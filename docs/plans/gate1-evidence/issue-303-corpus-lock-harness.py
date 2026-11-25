"""Cross-platform harness for scripts/corpus_lock.py (issue #303). Standard library only.

Runs the corpus lock against real child processes of this same file, with no
pytest, so it can run under any Python 3.12+ on Windows or Linux:

    python docs/plans/gate1-evidence/issue-303-corpus-lock-harness.py --state-dir <scratch folder>

`--state-dir` is required and must not be the real user-state folder: every
lock file this harness creates lands there. Corpora and signal files live in a
fresh temporary folder. Prints one PASS / FAIL / SKIP line per check, then a
summary line; exits non-zero on any FAIL.

Checks: contend, waiter proceeds after release, try-acquire refused while
held, killed holder frees the lock, probe never blocks a try-acquire, and (POSIX
only) a waiting child that receives a real SIGINT exits 130, and a forked child
neither re-enters nor releases its parent's hold.
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
MODULE_PATH = HERE.parents[3] / "scripts" / "corpus_lock.py"
BOUND_SECONDS = 20.0
POLL_SECONDS = 0.02


def load_corpus_lock(state_dir: Path):
    spec = importlib.util.spec_from_file_location("corpus_lock", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["corpus_lock"] = module
    spec.loader.exec_module(module)
    module._real_lock_state_dir = module.lock_state_dir
    module.lock_state_dir = lambda: state_dir
    return module


# --- child side ----------------------------------------------------------------


def event(sync: Path, name: str, what: str) -> None:
    with open(sync / f"{name}.events", "a", encoding="utf-8") as fh:
        fh.write(f"{time.perf_counter_ns()} {what}\n")
        fh.flush()


def wait_for_file(path: Path, bound: float = 60.0) -> None:
    deadline = time.monotonic() + bound
    while not path.exists():
        if time.monotonic() > deadline:
            raise SystemExit(f"no {path.name} within {bound} s")
        time.sleep(POLL_SECONDS)


def child_main(args: argparse.Namespace) -> int:
    cl = load_corpus_lock(args.state_dir)
    sync, name, corpus = args.sync, args.name, args.corpus
    if args.child == "hold":
        cl.acquire(corpus, command="scan", channel="alpha", wait=False)
        event(sync, name, "acquired")
        (sync / f"{name}.ready").write_text("", encoding="utf-8")
        wait_for_file(sync / f"{name}.release")
        event(sync, name, "last-protected")
        cl.release(corpus)
        return 0
    if args.child == "wait-hold":
        cl.WAIT_POLL_SECONDS = 0.05
        with cl.hold_for_cli(corpus, command="harness-waiter"):
            event(sync, name, "acquired")
            event(sync, name, "last-protected")
        return 0
    if args.child == "try":
        try:
            cl.acquire(corpus, command="harness-try", wait=False)
        except cl.CorpusBusy:
            return cl.EXIT_CORPUS_BUSY
        cl.release(corpus)
        return 0
    if args.child == "count":
        cl.WAIT_POLL_SECONDS = 0.02
        counter = sync / "counter.txt"
        with cl.hold_for_cli(corpus, command="harness-count", report=lambda _line: None):
            event(sync, name, "start")
            value = int(counter.read_text(encoding="utf-8")) if counter.exists() else 0
            time.sleep(0.05)  # widens the read-modify-write window; exclusion is what keeps it correct
            counter.write_text(str(value + 1), encoding="utf-8")
            event(sync, name, "end")
        return 0
    if args.child == "probe-loop":
        writer = no_writer = 0
        first = True
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (sync / f"{name}.release").exists():
            if cl.probe_writer(corpus) is None:
                no_writer += 1
            else:
                writer += 1
            if first:
                (sync / f"{name}.ready").write_text("", encoding="utf-8")
                first = False
        event(sync, name, f"counts {writer} {no_writer}")
        return 0
    raise SystemExit(f"unknown child mode {args.child}")


# --- parent side ---------------------------------------------------------------


class CheckFailed(Exception):
    pass


class Parent:
    def __init__(self, state_dir: Path, work: Path) -> None:
        self.state_dir = state_dir
        self.work = work
        self.procs: list[subprocess.Popen] = []

    def argv(self, mode: str, sync: Path, name: str, corpus: Path) -> list[str]:
        return [
            sys.executable,
            str(HERE),
            "--state-dir",
            str(self.state_dir),
            "--child",
            mode,
            "--sync",
            str(sync),
            "--name",
            name,
            "--corpus",
            str(corpus),
        ]

    def start(self, mode: str, sync: Path, name: str, corpus: Path, wait_ready: bool = False) -> subprocess.Popen:
        with open(sync / f"{name}.out", "wb") as out:
            proc = subprocess.Popen(self.argv(mode, sync, name, corpus), stdout=out, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        if wait_ready:
            ready = sync / f"{name}.ready"
            poll(lambda: ready.exists() or proc.poll() is not None, f"{name} ready")
            if not ready.exists():
                raise CheckFailed(f"{name} exited {proc.returncode} before ready: {output(sync, name)}")
        return proc

    def run(self, mode: str, sync: Path, name: str, corpus: Path) -> int:
        result = subprocess.run(self.argv(mode, sync, name, corpus), capture_output=True, timeout=BOUND_SECONDS)
        return result.returncode

    def finish(self, proc: subprocess.Popen, sync: Path, name: str) -> int:
        (sync / f"{name}.release").write_text("", encoding="utf-8")
        return proc.wait(timeout=BOUND_SECONDS)

    def cleanup(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def poll(predicate, what: str, bound: float = BOUND_SECONDS) -> None:
    deadline = time.monotonic() + bound
    while not predicate():
        if time.monotonic() > deadline:
            raise CheckFailed(f"timed out after {bound} s waiting for {what}")
        time.sleep(POLL_SECONDS)


def output(sync: Path, name: str) -> str:
    path = sync / f"{name}.out"
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def events(sync: Path, name: str) -> list[tuple[int, str]]:
    path = sync / f"{name}.events"
    if not path.exists():
        return []
    return [(int(ns), ev) for ns, ev in (line.split(" ", 1) for line in path.read_text(encoding="utf-8").splitlines())]


def first(sync: Path, name: str, what: str) -> int:
    for ns, ev in events(sync, name):
        if ev == what:
            return ns
    raise CheckFailed(f"{name} recorded no {what!r}")


def check_contend(parent: Parent, sync: Path, corpus: Path, cl) -> None:
    names = [f"C{i}" for i in range(4)]
    procs = [parent.start("count", sync, name, corpus) for name in names]
    for name, proc in zip(names, procs, strict=True):
        code = proc.wait(timeout=60)
        if code != 0:
            raise CheckFailed(f"{name} exited {code}: {output(sync, name)}")
    total = int((sync / "counter.txt").read_text(encoding="utf-8"))
    if total != len(names):
        raise CheckFailed(f"counter is {total}, expected {len(names)} (a lost update means no exclusion)")
    spans = sorted((first(sync, n, "start"), first(sync, n, "end")) for n in names)
    for (_, end), (start, _) in itertools.pairwise(spans):
        if start < end:
            raise CheckFailed("two children were inside the lock at the same time")


def check_waiter(parent: Parent, sync: Path, corpus: Path, cl) -> None:
    holder = parent.start("hold", sync, "A", corpus, wait_ready=True)
    waiter = parent.start("wait-hold", sync, "B", corpus)
    poll(lambda: "Waiting for the corpus lock: scan, channel alpha" in output(sync, "B"), "B's wait line")
    if any(ev == "acquired" for _, ev in events(sync, "B")):
        raise CheckFailed("B acquired while A held the lock")
    if parent.finish(holder, sync, "A") != 0:
        raise CheckFailed(f"A failed: {output(sync, 'A')}")
    code = waiter.wait(timeout=BOUND_SECONDS)
    if code != 0:
        raise CheckFailed(f"B exited {code}: {output(sync, 'B')}")
    if not first(sync, "A", "last-protected") < first(sync, "B", "acquired"):
        raise CheckFailed("B acquired before A's last protected action")


def check_try_refused(parent: Parent, sync: Path, corpus: Path, cl) -> None:
    holder = parent.start("hold", sync, "A", corpus, wait_ready=True)
    code = parent.run("try", sync, "T1", corpus)
    if code != cl.EXIT_CORPUS_BUSY:
        raise CheckFailed(f"try while held exited {code}, expected {cl.EXIT_CORPUS_BUSY}")
    if parent.finish(holder, sync, "A") != 0:
        raise CheckFailed(f"A failed: {output(sync, 'A')}")
    code = parent.run("try", sync, "T2", corpus)
    if code != 0:
        raise CheckFailed(f"try after release exited {code}, expected 0")


def check_killed_holder(parent: Parent, sync: Path, corpus: Path, cl) -> None:
    holder = parent.start("hold", sync, "A", corpus, wait_ready=True)
    holder.kill()
    holder.wait(timeout=BOUND_SECONDS)
    deadline = time.monotonic() + 10
    attempt = 0
    while True:
        attempt += 1
        if parent.run("try", sync, f"T{attempt}", corpus) == 0:
            return
        if time.monotonic() > deadline:
            raise CheckFailed("the killed holder's lock was not freed within 10 s")
        time.sleep(POLL_SECONDS)


def check_probe_never_blocks(parent: Parent, sync: Path, corpus: Path, cl) -> None:
    prober = parent.start("probe-loop", sync, "R", corpus, wait_ready=True)
    cl.WAIT_POLL_SECONDS = 0.02
    refusals = 0
    for _ in range(200):
        try:
            cl.acquire(corpus, command="harness-parent", wait=False)
        except cl.CorpusBusy:
            refusals += 1
            continue
        cl.release(corpus)
    cl.acquire(corpus, command="harness-parent", wait=False)
    time.sleep(0.5)  # one longer hold so the probe loop is sure to observe a writer
    cl.release(corpus)
    (sync / "R.release").write_text("", encoding="utf-8")
    prober.wait(timeout=BOUND_SECONDS)
    if refusals:
        raise CheckFailed(f"{refusals} of 200 try-acquires were refused while only a reader was probing")
    counts = [ev for _, ev in events(sync, "R") if ev.startswith("counts ")]
    if not counts or int(counts[0].split()[1]) < 1:
        raise CheckFailed(f"the probe loop never observed the writer: {counts}")


def check_sigint(parent: Parent, sync: Path, corpus: Path, cl) -> None:
    holder = parent.start("hold", sync, "A", corpus, wait_ready=True)
    waiter = parent.start("wait-hold", sync, "B", corpus)
    poll(lambda: "Waiting for the corpus lock" in output(sync, "B"), "B's wait line")
    waiter.send_signal(signal.SIGINT)
    code = waiter.wait(timeout=BOUND_SECONDS)
    text = output(sync, "B")
    parent.finish(holder, sync, "A")
    if code != 130:
        raise CheckFailed(f"B exited {code} after SIGINT, expected 130: {text}")
    if "Interrupted while waiting for the corpus lock; nothing was written." not in text:
        raise CheckFailed(f"B did not print the interrupt line: {text}")
    if "Traceback" in text:
        raise CheckFailed(f"B printed a traceback: {text}")
    if any(ev == "acquired" for _, ev in events(sync, "B")):
        raise CheckFailed("B acquired the lock")


def check_fork(parent: Parent, sync: Path, corpus: Path, cl) -> None:
    cl.acquire(corpus, command="harness-parent", wait=False)
    try:
        pid = os.fork()
        if pid == 0:  # the child: its acquire must be refused, never a re-entry
            code = 1
            try:
                try:
                    cl.acquire(corpus, command="harness-child", wait=False)
                    code = 2
                except cl.CorpusBusy:
                    code = 0 if cl.held_depth(corpus) == 0 else 3
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
        if code == 2:
            raise CheckFailed("the forked child took or re-entered the lock while the parent held it")
        if code != 0:
            raise CheckFailed(f"the forked child exited {code}")
        if cl.held_depth(corpus) != 1:
            raise CheckFailed(f"the parent's depth is {cl.held_depth(corpus)} after the fork, expected 1")
        if parent.run("try", sync, "T1", corpus) != cl.EXIT_CORPUS_BUSY:
            raise CheckFailed("the parent lost its lock after the forked child exited")
    finally:
        cl.release(corpus)
    if parent.run("try", sync, "T2", corpus) != 0:
        raise CheckFailed("the lock was not free after the parent released it")


CHECKS = [
    ("contend", check_contend),
    ("waiter-proceeds-after-release", check_waiter),
    ("try-acquire-refused-while-held", check_try_refused),
    ("killed-holder-frees-the-lock", check_killed_holder),
    ("probe-never-blocks-a-try-acquire", check_probe_never_blocks),
    ("sigint-while-waiting-exits-130", check_sigint),
    ("fork-child-neither-reenters-nor-releases", check_fork),
]


def parent_main(state_dir: Path) -> int:
    cl = load_corpus_lock(state_dir)
    real = cl._real_lock_state_dir()
    if os.path.normcase(os.path.realpath(state_dir)) == os.path.normcase(os.path.realpath(real)):
        print(f"Refusing to run: --state-dir is the real user-state folder ({real}).")
        return 2
    print(f"corpus_lock: {MODULE_PATH}")
    print(f"python: {sys.version.split()[0]} on {sys.platform}; state dir: {state_dir}")
    passed = failed = skipped = 0
    work = Path(tempfile.mkdtemp(prefix="vi303-harness-"))
    try:
        for index, (name, check) in enumerate(CHECKS):
            if name.startswith("sigint") and os.name == "nt":
                print(f"SKIP {name}: a real SIGINT to a waiting child is POSIX only")
                skipped += 1
                continue
            if name.startswith("fork") and not hasattr(os, "fork"):
                print(f"SKIP {name}: fork() is POSIX only")
                skipped += 1
                continue
            sync = work / f"{index}-sync"
            corpus = work / f"{index}-corpus"
            sync.mkdir()
            corpus.mkdir()
            parent = Parent(state_dir, work)
            try:
                check(parent, sync, corpus, cl)
            except (CheckFailed, subprocess.TimeoutExpired, OSError, ValueError) as e:
                print(f"FAIL {name}: {e}")
                failed += 1
            else:
                print(f"PASS {name}")
                passed += 1
            finally:
                parent.cleanup()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"SUMMARY: {passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--child")
    parser.add_argument("--sync", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--corpus", type=Path)
    args = parser.parse_args()
    state_dir = args.state_dir.resolve()
    if args.child:
        args.state_dir = state_dir
        return child_main(args)
    return parent_main(state_dir)


if __name__ == "__main__":
    sys.exit(main())
