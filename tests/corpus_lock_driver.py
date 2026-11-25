"""Child-process driver for the corpus lock tests (not collected: no `test_` prefix).

argv: `--state-dir D --sync S --name N MODE ...`. The lock state folder is
pinned to D before anything else, so a child can never touch the real
user-state folder. Each process records events only in its own file
`S/<name>.events`, one `"<time.perf_counter_ns()> <event>"` line per event,
opened, written, flushed and closed per line. `perf_counter_ns` is one
system-wide clock on Windows (QueryPerformanceCounter) and Linux
(CLOCK_MONOTONIC); `monotonic_ns` on Windows ticks every 15.6 ms, so two
processes can stamp the same value. A process records its last
protected action BEFORE it unlocks, so comparing that timestamp with another
process's `acquired` timestamp is a valid ordering check.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import corpus_lock

RELEASE_POLL_SECONDS = 0.02
PROBE_HOLD_POLL_SECONDS = 0.005
RELEASE_BOUND_SECONDS = 60.0


def _event(sync: Path, name: str, event: str) -> None:
    with open(sync / f"{name}.events", "a", encoding="utf-8") as fh:
        fh.write(f"{time.perf_counter_ns()} {event}\n")
        fh.flush()


def _touch(path: Path) -> None:
    path.write_text("", encoding="utf-8")


def _wait_for_release(sync: Path, name: str, poll: float) -> None:
    deadline = time.monotonic() + RELEASE_BOUND_SECONDS
    release_file = sync / f"{name}.release"
    while not release_file.exists():
        if time.monotonic() > deadline:
            raise SystemExit(f"{name}: no release file within {RELEASE_BOUND_SECONDS} s")
        time.sleep(poll)


def _mode_hold(args: argparse.Namespace) -> int:
    corpus_lock.acquire(args.corpus, command="scan", channel="alpha", wait=args.wait)
    _event(args.sync, args.name, "acquired")
    _touch(args.sync / f"{args.name}.ready")
    _wait_for_release(args.sync, args.name, RELEASE_POLL_SECONDS)
    _event(args.sync, args.name, "last-protected")
    corpus_lock.release(args.corpus)
    _event(args.sync, args.name, "released")
    return 0


def _mode_try(args: argparse.Namespace) -> int:
    try:
        corpus_lock.acquire(args.corpus, command="try", wait=False)
    except corpus_lock.CorpusBusy:
        _event(args.sync, args.name, "busy")
        return corpus_lock.EXIT_CORPUS_BUSY
    _event(args.sync, args.name, "acquired")
    corpus_lock.release(args.corpus)
    return 0


def _mode_probe_hold(args: argparse.Namespace) -> int:
    """Hold `<key>.busy` directly, as a reader paused in the middle of its probe."""
    paths = corpus_lock.lock_paths(args.corpus)
    os.makedirs(paths.busy.parent, exist_ok=True)
    fd = corpus_lock._open_lock_file(paths.busy)
    if not corpus_lock._try_lock(fd):
        os.close(fd)
        return 3
    _event(args.sync, args.name, "busy-held")
    _touch(args.sync / f"{args.name}.ready")
    _wait_for_release(args.sync, args.name, PROBE_HOLD_POLL_SECONDS)
    corpus_lock._platform_unlock(fd)
    os.close(fd)
    _event(args.sync, args.name, "busy-released")
    return 0


def _mode_probe_loop(args: argparse.Namespace) -> int:
    """Probe in a tight loop until the release file appears (or `--seconds` passes)."""
    writer = no_writer = 0
    deadline = time.monotonic() + args.seconds
    release_file = args.sync / f"{args.name}.release"
    first = True
    while time.monotonic() < deadline and not release_file.exists():
        if corpus_lock.probe_writer(args.corpus) is None:
            no_writer += 1
        else:
            writer += 1
        if first:
            _touch(args.sync / f"{args.name}.ready")
            first = False
    _event(args.sync, args.name, f"counts writer={writer} no-writer={no_writer}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--sync", type=Path, required=True)
    parser.add_argument("--name", required=True)
    sub = parser.add_subparsers(dest="mode", required=True)
    hold = sub.add_parser("hold")
    hold.add_argument("--corpus", required=True)
    hold.add_argument("--wait", action="store_true")
    sub.add_parser("try").add_argument("--corpus", required=True)
    sub.add_parser("probe-hold").add_argument("--corpus", required=True)
    loop = sub.add_parser("probe-loop")
    loop.add_argument("--corpus", required=True)
    loop.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args(argv)

    state_dir = args.state_dir
    corpus_lock.lock_state_dir = lambda: state_dir
    modes = {"hold": _mode_hold, "try": _mode_try, "probe-hold": _mode_probe_hold, "probe-loop": _mode_probe_loop}
    return modes[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
