"""Two real `video_intel.main()` processes on one corpus (issue #303, Phase B).

Writer A runs in the driver's `main` mode and pauses at a fake paid boundary;
writer B starts only after A's `.paused` file exists. Every paid boundary is
stubbed inside each child by the driver, and each child reads its config from
a scratch file under `tmp_path` (the driver refuses any corpus outside it).
Order across processes is read from each child's own event file
(`time.perf_counter_ns()`), never from a shared log, and every wait is a
bounded poll.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import corpus_lock
import pytest
import yaml
from corpus_lock_driver import FakeVoyage

import video_intel as vi

DRIVER = Path(__file__).resolve().parent / "corpus_lock_driver.py"
BOUND_SECONDS = 20.0
EXIT_BOUND_SECONDS = 30.0
KILL_BOUND_SECONDS = 10.0
POLL_SECONDS = 0.02
CHANNEL_ID = "UC" + "a" * 22
WAIT_LINE = "Waiting for the corpus lock: "


def _wait_for(predicate, what: str, bound: float = BOUND_SECONDS) -> None:
    deadline = time.monotonic() + bound
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {bound} s waiting for {what}")
        time.sleep(POLL_SECONDS)


class Scratch:
    """A scratch corpus, its config file, the lock folder and the sync folder for one test."""

    def __init__(self, tmp_path: Path, state_dir: Path, **config_extra) -> None:
        self.root = tmp_path
        self.state_dir = state_dir
        self.sync = tmp_path / "sync"
        self.sync.mkdir()
        self.corpus = tmp_path / "corpus"
        (self.corpus / "alpha").mkdir(parents=True)
        self.cfg = tmp_path / "scratch-config.yml"
        self.config = {
            "output_dir": str(self.corpus),
            "channels": [{"name": "alpha", "url": "https://www.youtube.com/@alpha", "channel_id": CHANNEL_ID}],
            **config_extra,
        }
        self.cfg.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        # The live-corpus guard: everything a child can resolve lies under this test's temp folder.
        assert self.corpus.resolve().is_relative_to(tmp_path.resolve())

    def backups(self) -> dict[str, bytes]:
        d = self.corpus / vi.CONFIG_BACKUP_DIR_NAME
        return {p.name: p.read_bytes() for p in sorted(d.iterdir())} if d.exists() else {}

    def start(self, name: str, argv: list[str], pause_at: str = "") -> Child:
        return Child(self, name, argv, pause_at)


class Child:
    def __init__(self, scratch: Scratch, name: str, argv: list[str], pause_at: str) -> None:
        self.scratch, self.name = scratch, name
        self.out = scratch.sync / f"{name}.stdout"
        self.err = scratch.sync / f"{name}.stderr"
        cmd = [sys.executable, str(DRIVER), "--state-dir", str(scratch.state_dir), "--sync", str(scratch.sync)]
        cmd += ["--name", name, "main", "--config", str(scratch.cfg), "--temp-root", str(scratch.root)]
        cmd += ["--pause-at", pause_at, "--", *argv]
        with open(self.out, "wb") as out, open(self.err, "wb") as err:
            self.proc = subprocess.Popen(cmd, stdout=out, stderr=err)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def stderr(self) -> str:
        return self.err.read_text(encoding="utf-8", errors="replace") if self.err.exists() else ""

    def events(self) -> list[tuple[int, str]]:
        path = self.scratch.sync / f"{self.name}.events"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            stamp, _, event = line.partition(" ")
            rows.append((int(stamp), event))
        return rows

    def first(self, *prefixes: str) -> int:
        stamps = [t for t, e in self.events() if e.startswith(prefixes)]
        assert stamps, f"{self.name} recorded none of {prefixes}: {self.events()}"
        return min(stamps)

    def wait_paused(self) -> None:
        paused = self.scratch.sync / f"{self.name}.paused"
        _wait_for(lambda: paused.exists() or self.proc.poll() is not None, f"{self.name} to pause")
        assert self.proc.poll() is None, f"{self.name} exited before pausing:\n{self.stderr()}"

    def wait_line(self, text: str) -> None:
        _wait_for(lambda: text in self.stderr() or self.proc.poll() is not None, f"{self.name} to print {text!r}")
        assert text in self.stderr(), f"{self.name} exited without {text!r}:\n{self.stderr()}"

    def release(self) -> None:
        (self.scratch.sync / f"{self.name}.release").write_text("", encoding="utf-8")

    def wait_exit(self, bound: float = EXIT_BOUND_SECONDS) -> int:
        try:
            return self.proc.wait(timeout=bound)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            pytest.fail(f"{self.name} did not exit within {bound} s:\n{self.stderr()}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


@pytest.fixture
def children():
    started: list[Child] = []
    yield started
    for child in started:
        child.stop()


def _protected_events(child: Child) -> list[str]:
    return [e for _, e in child.events() if e.startswith(("paid", "entered"))]


def _run_pair(scratch: Scratch, children: list, a_argv, a_pause, b_argv) -> tuple[Child, Child]:
    """A pauses at its paid boundary; B must wait, write nothing, then run after A's last protected action."""
    a = scratch.start("A", a_argv, a_pause)
    children.append(a)
    a.wait_paused()
    backups_while_a_holds = scratch.backups()

    b = scratch.start("B", b_argv)
    children.append(b)
    b.wait_line(WAIT_LINE)
    assert f"PID {a.pid}" in b.stderr(), b.stderr()
    assert _protected_events(b) == [], f"B ran protected work while A held the lock: {b.events()}"
    assert scratch.backups() == backups_while_a_holds, "B snapshotted while waiting"

    a.release()
    assert a.wait_exit() == 0, a.stderr()
    assert b.wait_exit() == 0, b.stderr()
    assert a.first("last-protected") < b.first("entered", "paid")
    return a, b


@pytest.fixture
def scratch(tmp_path, corpus_lock_state_dir):
    return Scratch(tmp_path, corpus_lock_state_dir)


def test_full_scan_vs_focused_scan(scratch, children):
    _, b = _run_pair(scratch, children, ["scan"], "fetch_channel_videos", ["scan", "--channel", "alpha"])
    assert any(e == "paid fetch_channel_videos" for _, e in b.events())


def test_scan_vs_taxonomy_build(scratch, children):
    _run_pair(scratch, children, ["scan"], "fetch_channel_videos", ["taxonomy-build"])
    assert (scratch.corpus / "taxonomy.json").exists()


TRANSCRIPT = "# Transcript\n\n[00:00] Speaker: {body} one.\n[00:30] Speaker: {body} two.\n"


def _write_video(channel_dir: Path, vid: str, body: str) -> None:
    prefix = f"2026-01-01-{vid}"
    (channel_dir / f"{prefix}.transcript.md").write_text(TRANSCRIPT.format(body=body), encoding="utf-8")
    (channel_dir / f"{prefix}.meta.json").write_text(
        json.dumps({"video_id": vid, "title": vid, "published": "2026-01-01", "channel": channel_dir.name}),
        encoding="utf-8",
    )


def test_index_incremental_vs_index_channel(tmp_path, corpus_lock_state_dir, children, monkeypatch):
    pytest.importorskip("lancedb")
    db = tmp_path / "lancedb"
    scratch = Scratch(tmp_path, corpus_lock_state_dir, vector_db_dir=str(db))
    _write_video(scratch.corpus / "alpha", "vid001", "alpha first")
    # A real LanceDB table, built here with the paid boundary stubbed; then one new video for A to embed.
    monkeypatch.setattr(vi, "require_voyageai", lambda: SimpleNamespace(Client=FakeVoyage().client))
    monkeypatch.setenv("VOYAGE_API_KEY", "fake")
    vi.build_search_index(scratch.corpus, config={"vector_db_dir": str(db)})
    _write_video(scratch.corpus / "alpha", "vid002", "alpha second")

    _, b = _run_pair(scratch, children, ["index", "--incremental"], "voyage-embed", ["index", "--channel", "alpha"])
    assert any(e == "paid voyage-embed" for _, e in b.events())


def test_dedupe_apply_vs_scan(scratch, children):
    channel = scratch.corpus / "alpha"
    for prefix, title in (("2026-01-01-old-title", "old title"), ("2026-01-01-new-title", "new title")):
        (channel / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "vid001",
                    "title": title,
                    "published": "2026-01-01",
                    "channel": "alpha",
                    "processed": f"2026-01-0{2 if 'new' in prefix else 1}T00:00:00",
                    "modes_completed": ["mindmap"],
                }
            ),
            encoding="utf-8",
        )
        (channel / f"{prefix}.mindmap.md").write_text("# map\n", encoding="utf-8")

    _run_pair(scratch, children, ["dedupe", "--apply"], "_apply_dedupe_group", ["scan", "--channel", "alpha"])
    assert len(list(channel.glob("*.meta.json"))) == 1, "the dedupe really applied"


def test_waiter_runs_with_the_config_edited_while_it_waited(scratch, children):
    a = scratch.start("A", ["taxonomy-build"], "build_taxonomy")
    children.append(a)
    a.wait_paused()
    b = scratch.start("B", ["taxonomy-build"])
    children.append(b)
    b.wait_line(WAIT_LINE)

    edited = scratch.cfg.read_bytes() + b"# edited while B waited\n"
    scratch.cfg.write_bytes(edited)
    a.release()

    assert a.wait_exit() == 0, a.stderr()
    assert b.wait_exit() == 0, b.stderr()
    backups = scratch.backups()
    assert backups["config.latest.yaml"] == edited
    assert len([n for n in backups if n != "config.latest.yaml"]) == 2, sorted(backups)
    # B re-read the config after it acquired the lock, and that read saw the edit.
    loads = [(t, e) for t, e in b.events() if e.startswith("load-config ")]
    assert len(loads) == 2, b.events()
    assert loads[-1][1] == "load-config " + hashlib.sha256(edited).hexdigest()[:12]
    assert loads[-1][0] > a.first("last-protected")


def test_no_wait_second_writer_exits_four_before_anything(scratch, children):
    a = scratch.start("A", ["taxonomy-build"], "build_taxonomy")
    children.append(a)
    a.wait_paused()
    backups = scratch.backups()

    b = scratch.start("B", ["--no-wait", "taxonomy-build"])
    children.append(b)
    code = b.wait_exit(BOUND_SECONDS)

    assert code == 4, b.stderr()
    assert "Another command is writing to this corpus: taxonomy-build, PID " in b.stderr()
    assert f"PID {a.pid}" in b.stderr()
    # Only the config read precedes the refusal; nothing protected ran.
    assert [e for _, e in b.events() if not e.startswith("load-config ")] == ["exit 4"]
    assert scratch.backups() == backups
    a.release()
    assert a.wait_exit() == 0, a.stderr()


def test_hard_kill_of_a_running_writer_frees_the_corpus(scratch, children):
    a = scratch.start("A", ["taxonomy-build"], "build_taxonomy")
    children.append(a)
    a.wait_paused()

    a.proc.kill()
    a.proc.wait()

    deadline = time.monotonic() + KILL_BOUND_SECONDS
    attempt = 0
    while True:
        attempt += 1
        b = scratch.start(f"B{attempt}", ["--no-wait", "taxonomy-build"])
        children.append(b)
        if b.wait_exit(BOUND_SECONDS) == 0:
            break
        assert time.monotonic() < deadline, f"the corpus was still locked {KILL_BOUND_SECONDS} s after the kill"
        time.sleep(0.2)
    assert corpus_lock.probe_writer(scratch.corpus) is None
