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
import hashlib
import os
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

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


# ---------------------------------------------------------------------------
# `main` mode (issue #303 Phase B): a real `video_intel.main()` in a child
# process, with every paid boundary stubbed and the config read from a scratch
# file. The helpers below are also imported by the in-process CLI tests, so the
# stubs and the config reader have one definition.
# ---------------------------------------------------------------------------

FAKE_KEYS = {"GEMINI_API_KEY": "fake-gemini", "YOUTUBE_API_KEY": "fake-youtube", "VOYAGE_API_KEY": "fake-voyage"}


def make_load_config(vi, cfg_path: Path, temp_root: Path, on_load=None):
    """A `load_config` that re-reads the scratch config file on every call (so edits are real).

    It sets `_LAST_RESOLVED_PATH` so the real snapshot copies the scratch file,
    and refuses any config whose corpus is not under `temp_root`: a plugin-local
    config would otherwise win the precedence chain and name a live corpus.
    """
    import yaml

    def _load_config():
        raw = Path(cfg_path).read_bytes()
        config = yaml.safe_load(raw)
        output_dir = Path(config["output_dir"])
        if not os.path.realpath(output_dir).startswith(os.path.realpath(temp_root)):
            raise SystemExit(f"refusing a corpus outside the test temp folder: {output_dir}")
        vi._LAST_RESOLVED_PATH = Path(cfg_path)
        if on_load is not None:
            on_load(raw)
        return config

    return _load_config


class FakeVoyage:
    """Voyage client stand-in: `embed` is the paid boundary; `on_embed` observes each call."""

    def __init__(self, on_embed=None) -> None:
        self.on_embed = on_embed

    def client(self):
        return self

    def embed(self, texts, *_a, **_kw):
        texts = list(texts)
        if self.on_embed is not None:
            self.on_embed()
        return SimpleNamespace(embeddings=[[float(len(t) % 7), 0.5, 0.25, 0.125] for t in texts])


def install_paid_stubs(vi, setattr_, paid, voyage: FakeVoyage | None = None) -> None:
    """Stub every paid boundary a locking command can reach; `paid(name)` observes each call.

    `setattr_` is `monkeypatch.setattr` in-process and the builtin `setattr` in a child.
    """
    setattr_(vi, "require_gemini", lambda: (None, None))
    setattr_(vi, "require_youtube", lambda: lambda *a, **kw: None)

    def _create_client(*_a, **_kw):
        paid("create_client")

    def _fetch_channel_videos(*_a, **_kw):
        paid("fetch_channel_videos")
        return []

    setattr_(vi, "create_client", _create_client)
    setattr_(vi, "fetch_channel_videos", _fetch_channel_videos)
    setattr_(vi, "render_headline_digest", lambda *a, **kw: [])
    voyage = voyage or FakeVoyage(on_embed=lambda: paid("voyage-embed"))
    setattr_(vi, "require_voyageai", lambda: SimpleNamespace(Client=voyage.client))


def _pausing(sync: Path, name: str, func):
    """Wrap `func`: record `pause-entered`, create `<name>.paused`, wait for `<name>.release`, then run it."""

    def _wrapper(*a, **kw):
        _event(sync, name, "pause-entered")
        _touch(sync / f"{name}.paused")
        _wait_for_release(sync, name, RELEASE_POLL_SECONDS)
        _event(sync, name, "last-protected")
        return func(*a, **kw)

    return _wrapper


def _mode_main(args: argparse.Namespace) -> int:
    import video_intel as vi

    sync, name = args.sync, args.name
    os.environ.update(FAKE_KEYS)
    corpus_lock.WAIT_POLL_SECONDS = 0.05  # a waiter notices a release quickly; the wait logic is unchanged

    def _record_load(raw: bytes) -> None:
        _event(sync, name, f"load-config {hashlib.sha256(raw).hexdigest()[:12]}")

    config = make_load_config(vi, args.config, args.temp_root)()
    assert Path(vi.resolve_output_dir(config, create=False)).resolve().is_relative_to(args.temp_root.resolve())
    vi.load_config = make_load_config(vi, args.config, args.temp_root, on_load=_record_load)

    def _paid(boundary: str) -> None:
        _event(sync, name, f"paid {boundary}")

    voyage = FakeVoyage(on_embed=lambda: _paid("voyage-embed"))
    install_paid_stubs(vi, setattr, _paid, voyage)

    # Every dispatch target records `entered` before it runs.
    for attr in [a for a in dir(vi) if a.startswith("cmd_")]:
        real = getattr(vi, attr)

        def _entered(*a, _real=real, _attr=attr, **kw):
            _event(sync, name, f"entered {_attr}")
            return _real(*a, **kw)

        setattr(vi, attr, _entered)

    if args.pause_at == "voyage-embed":
        first = {"done": False}

        def _embed_pause() -> None:
            _paid("voyage-embed")
            if not first["done"]:
                first["done"] = True
                _pausing(sync, name, lambda: None)()

        voyage.on_embed = _embed_pause
    elif args.pause_at:
        setattr(vi, args.pause_at, _pausing(sync, name, getattr(vi, args.pause_at)))

    sys.argv = ["video_intel.py", *args.argv]
    code = 0
    try:
        vi.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:
        traceback.print_exc()
        code = 1
    _event(sync, name, f"exit {code}")
    return code


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
    main_mode = sub.add_parser("main")
    main_mode.add_argument("--config", type=Path, required=True)
    main_mode.add_argument("--temp-root", type=Path, required=True)
    main_mode.add_argument("--pause-at", default="")
    main_mode.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode == "main" and args.argv[:1] == ["--"]:
        args.argv = args.argv[1:]

    state_dir = args.state_dir
    corpus_lock.lock_state_dir = lambda: state_dir
    modes = {
        "hold": _mode_hold,
        "try": _mode_try,
        "probe-hold": _mode_probe_hold,
        "probe-loop": _mode_probe_loop,
        "main": _mode_main,
    }
    return modes[args.mode](args)


if __name__ == "__main__":
    sys.exit(main())
