"""The corpus lock as `video_intel.main()` takes it (issue #303, Phase B).

Every test drives the real `main()` in-process with `sys.argv` patched and the
config read from a scratch file through the driver's `make_load_config`. A
holder in ANOTHER process is the driver's `hold` mode (re-entrancy would make
an in-process holder invisible). Paid boundaries are stubbed through the
driver's `install_paid_stubs`; nothing here makes a network call. The lock
folder is the per-test one the autouse `corpus_lock_state_dir` fixture pins
(or, for the module-scoped holder, a module folder each test re-pins).
"""

from __future__ import annotations

import ast
import builtins
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import corpus_lock
import pytest
import yaml
from corpus_lock_driver import install_paid_stubs, make_load_config
from test_config_backup import _registered_commands
from test_nugget_persistence import _FakeClient, _hit

import video_intel as vi

DRIVER = Path(__file__).resolve().parent / "corpus_lock_driver.py"
BOUND_SECONDS = 20.0
POLL_SECONDS = 0.02
URL = "https://www.youtube.com/watch?v=vid00000001"
CHANNEL_ID = "UC" + "a" * 22

# The smallest valid argv for every registered run, and the preview argv for
# the commands that have one. A new subcommand fails the inventory test until
# it is classified AND given an argv here.
WRITING_ARGV: dict[str, list[str]] = {
    "scan": ["scan"],
    "mindmap": ["mindmap", "--url", URL],
    "transcript": ["transcript", "--url", URL],
    "process": ["process", "--url", URL],
    "concepts": ["concepts"],
    "taxonomy-build": ["taxonomy-build"],
    "topics-build": ["topics-build"],
    "index": ["index"],
    "repair-metas": ["repair-metas", "--apply"],
    "assess-transcripts": ["assess-transcripts", "--apply", "--backup-dir", "bk"],
    "repair-timestamps": ["repair-timestamps", "--url", URL, "--channel", "alpha", "--apply"],
    "backfill-descriptions": ["backfill-descriptions", "--apply"],
    "backfill-durations": ["backfill-durations", "--apply"],
    "dedupe": ["dedupe", "--apply"],
    "prune-shorts": ["prune-shorts", "--apply"],
    "mark-skip": ["mark-skip", "--url", URL, "--mode", "transcript"],
    "headlines": ["headlines"],
    "profile init": ["profile", "init"],
}
PREVIEW_ARGV: dict[str, list[str]] = {
    "scan": ["scan", "--dry-run"],
    "concepts": ["concepts", "--dry-run"],
    "topics-build": ["topics-build", "--dry-run"],
    "repair-metas": ["repair-metas"],
    "assess-transcripts": ["assess-transcripts"],
    "repair-timestamps": ["repair-timestamps", "--url", URL, "--channel", "alpha"],
    "backfill-descriptions": ["backfill-descriptions"],
    "backfill-durations": ["backfill-durations"],
    "dedupe": ["dedupe"],
    "prune-shorts": ["prune-shorts"],
    "headlines": ["headlines", "--dry-run"],
}
NEVER_LOCKS: dict[str, str] = {
    "search": "reader",
    "status": "reader",
    "profile show": "reader",
    "nugget": "never locks: writes only its own new file by exclusive create",
    "briefings": "never locks: writes only its own new file by exclusive create",
}
READER_ARGV: dict[str, list[str]] = {
    "search": ["search", "mcp"],
    "status": ["status"],
    "profile show": ["profile", "show"],
    "nugget": ["nugget", "mcp", "--no-save"],
    "briefings": ["briefings", "--unseen", "--dry-run"],
}


def _dispatch_attr(name: str) -> str:
    return "cmd_profile" if name.startswith("profile ") else "cmd_" + name.replace("-", "_")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _write_config(cfg_path: Path, output_dir: Path, extra: str = "") -> None:
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "output_dir": str(output_dir),
                "channels": [{"name": "alpha", "url": "https://www.youtube.com/@alpha", "channel_id": CHANNEL_ID}],
            }
        )
        + extra,
        encoding="utf-8",
    )


class Cli:
    """A scratch corpus plus a scratch config file that the real `main()` resolves."""

    def __init__(self, monkeypatch, root: Path, corpus: Path | None = None) -> None:
        self.monkeypatch = monkeypatch
        self.root = root
        self.corpus = corpus or (root / "corpus")
        self.corpus.mkdir(parents=True, exist_ok=True)
        self.cfg = root / "scratch-config.yml"
        _write_config(self.cfg, self.corpus)
        self.loads = 0
        self.paid: list[str] = []

        def _count(_raw: bytes) -> None:
            self.loads += 1

        temp_root = Path(os.path.commonpath([root, self.corpus]))
        monkeypatch.setattr(vi, "load_config", make_load_config(vi, self.cfg, temp_root, on_load=_count))
        for key, value in {"GEMINI_API_KEY": "f", "YOUTUBE_API_KEY": "f", "VOYAGE_API_KEY": "f"}.items():
            monkeypatch.setenv(key, value)
        install_paid_stubs(vi, monkeypatch.setattr, self.paid.append)

    def point_at(self, output_dir: Path | str) -> None:
        _write_config(self.cfg, Path(output_dir))

    def record(self, name: str, behavior=None) -> list[int]:
        """Replace `name`'s dispatch target with a recorder of the lock depth it ran under."""
        depths: list[int] = []

        def _recorder(_args, _config):
            depths.append(corpus_lock.held_depth(self.corpus))
            if behavior is not None:
                behavior()

        self.monkeypatch.setattr(vi, _dispatch_attr(name), _recorder)
        return depths

    def run(self, argv: list[str], *, no_wait: bool = False) -> int:
        """Run the real `main()`; return the exit code (0 when it returns)."""
        self.monkeypatch.setattr(sys, "argv", ["video_intel.py", *(["--no-wait"] if no_wait else []), *argv])
        try:
            vi.main()
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return 0

    def backups(self) -> list[str]:
        d = self.corpus / vi.CONFIG_BACKUP_DIR_NAME
        return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _wait_for(predicate, what: str) -> None:
    deadline = time.monotonic() + BOUND_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {BOUND_SECONDS} s waiting for {what}")
        time.sleep(POLL_SECONDS)


class Holder:
    """A driver `hold` process on one corpus (another process, so the lock really contends)."""

    def __init__(self, state_dir: Path, sync: Path, corpus: Path, name: str = "holder") -> None:
        self.state_dir, self.sync, self.corpus, self.name = state_dir, sync, corpus, name
        sync.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, str(DRIVER), "--state-dir", str(state_dir), "--sync", str(sync), "--name", name]
            + ["hold", "--corpus", str(corpus)]
        )
        _wait_for(lambda: (sync / f"{name}.ready").exists() or self.proc.poll() is not None, "the holder")
        assert self.proc.poll() is None, "the holder exited before holding"

    @property
    def pid(self) -> int:
        return self.proc.pid

    def release(self) -> None:
        (self.sync / f"{self.name}.release").write_text("", encoding="utf-8")
        try:
            self.proc.wait(timeout=BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
            pytest.fail("the holder did not exit after its release")


def _try_exit(state_dir: Path, sync: Path, corpus: Path) -> int:
    """Exit code of a driver `try` (0 acquired, 4 busy) from another process."""
    sync.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(DRIVER), "--state-dir", str(state_dir), "--sync", str(sync), "--name", "try"]
        + ["try", "--corpus", str(corpus)],
        timeout=BOUND_SECONDS,
    ).returncode


@pytest.fixture
def cli(monkeypatch, tmp_path):
    return Cli(monkeypatch, tmp_path)


@pytest.fixture
def holder(cli, corpus_lock_state_dir, tmp_path):
    h = Holder(corpus_lock_state_dir, tmp_path / "sync", cli.corpus)
    yield h
    if h.proc.poll() is None:
        h.release()


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(corpus_lock, "WAIT_POLL_SECONDS", 0.05)


def _run_in_thread(cli: Cli, argv: list[str]) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def _target() -> None:
        try:
            result["code"] = cli.run(argv)
        except BaseException as e:  # surfaced by the test, never swallowed
            result["error"] = e

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, result


def _join(thread: threading.Thread, result: dict) -> int:
    thread.join(BOUND_SECONDS)
    if thread.is_alive():
        pytest.fail(f"main() did not finish within {BOUND_SECONDS} s")
    if "error" in result:
        raise result["error"]
    return result["code"]


# ---------------------------------------------------------------------------
# U3: classification, the preview snapshot rule
# ---------------------------------------------------------------------------


def _profile_actions(src: str) -> set[str]:
    """The nested `profile_actions.add_parser("<action>")` registrations."""
    actions = set()
    for node in ast.walk(ast.parse(src)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "profile_actions"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            actions.add(node.args[0].value)
    return actions


def _inventory() -> set[str]:
    src = Path(vi.__file__).read_text(encoding="utf-8")
    commands = _registered_commands(src)
    assert "profile" in commands
    # `profile` is classified per action: `profile init` writes, `profile show` reads.
    actions = _profile_actions(src)
    assert actions, "found no profile actions: the extractor no longer sees the registry"
    return (commands - {"profile"}) | {f"profile {a}" for a in actions}


class TestEveryRunIsClassifiedForTheLock:
    def test_every_registered_run_is_classified_exactly_once(self):
        inventory = _inventory()

        assert set(vi.CORPUS_LOCK_PREVIEW) == vi.CORPUS_LOCK_COMMANDS
        assert vi.CORPUS_LOCK_COMMANDS & set(NEVER_LOCKS) == set()
        assert inventory == vi.CORPUS_LOCK_COMMANDS | set(NEVER_LOCKS), (
            "every subcommand must be in CORPUS_LOCK_PREVIEW (locking) or NEVER_LOCKS (with a reason); "
            f"unclassified: {sorted(inventory - vi.CORPUS_LOCK_COMMANDS - set(NEVER_LOCKS))}"
        )
        assert vi.CORPUS_LOCK_COMMANDS >= vi.CONFIG_BACKUP_COMMANDS
        assert vi.CORPUS_LOCK_COMMANDS == vi.CONFIG_BACKUP_COMMANDS | vi.CORPUS_LOCK_EXTRA

    def test_every_run_has_an_argv_here(self):
        assert set(WRITING_ARGV) == vi.CORPUS_LOCK_COMMANDS
        assert set(PREVIEW_ARGV) == {n for n, p in vi.CORPUS_LOCK_PREVIEW.items() if p is not None}
        assert set(READER_ARGV) == set(NEVER_LOCKS)


@pytest.mark.parametrize("name", sorted(vi.CORPUS_LOCK_PREVIEW) + sorted(NEVER_LOCKS))
def test_lock_and_preview_follow_the_real_parser(cli, name):
    depths = cli.record(name)

    if name in NEVER_LOCKS:
        assert cli.run(READER_ARGV[name]) == 0
        assert depths == [0]
        return
    assert cli.run(WRITING_ARGV[name]) == 0
    assert depths == [1], f"{name} must hold the lock while it writes"
    if name in PREVIEW_ARGV:
        assert cli.run(PREVIEW_ARGV[name]) == 0
        assert depths == [1, 0], f"{name}'s preview must take no lock"


@pytest.mark.parametrize("name", sorted(PREVIEW_ARGV))
def test_preview_run_writes_no_snapshot_in_main(cli, name):
    depths = cli.record(name)

    assert cli.run(PREVIEW_ARGV[name]) == 0

    assert depths == [0], "the recorder ran"
    assert not (cli.corpus / vi.CONFIG_BACKUP_DIR_NAME).exists()


@pytest.mark.parametrize("name", sorted(vi.CONFIG_BACKUP_COMMANDS))
def test_writing_run_still_snapshots_in_main(cli, name):
    cli.record(name)

    assert cli.run(WRITING_ARGV[name]) == 0

    assert "config.latest.yaml" in cli.backups()


# ---------------------------------------------------------------------------
# U4: acquisition, --no-wait, waiting, the config re-read, the reader note
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def module_holder(tmp_path_factory):
    root = tmp_path_factory.mktemp("held")
    state_dir, corpus = root / "locks", root / "corpus"
    corpus.mkdir()
    h = Holder(state_dir, root / "sync", corpus)
    yield h
    h.release()


class TestNoWaitRefusesEveryWriter:
    @pytest.mark.parametrize("name", sorted(WRITING_ARGV))
    def test_no_wait_exits_four_before_snapshot_or_dispatch(self, module_holder, monkeypatch, tmp_path, caplog, name):
        # Arrange: re-pin the lock folder to the module holder's, and make a snapshot due (none exists yet).
        monkeypatch.setattr(corpus_lock, "lock_state_dir", lambda: module_holder.state_dir)
        cli = Cli(monkeypatch, tmp_path, corpus=module_holder.corpus)
        monkeypatch.setattr(vi, _dispatch_attr(name), lambda *_a: pytest.fail(f"{name} dispatched while refused"))
        assert cli.backups() == []

        # Act
        code = cli.run(WRITING_ARGV[name], no_wait=True)

        # Assert
        assert code == vi.EXIT_CORPUS_BUSY == 4
        assert cli.backups() == []
        refusals = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
        assert len(refusals) == 1, refusals
        assert refusals[0].startswith("Another command is writing to this corpus: scan, channel alpha, ")
        assert f"PID {module_holder.pid}" in refusals[0]
        assert cli.paid == []


def _two_video_corpus(corpus: Path) -> None:
    (corpus / "taxonomy.json").write_text(
        json.dumps(
            {"concepts": {"ai.mcp": {"preferred_label": "MCP", "aliases": ["mcp"], "video_count": 2, "domain": "ai"}}}
        ),
        encoding="utf-8",
    )
    channel = corpus / "alpha"
    channel.mkdir()
    for vid in ("vid001", "vid002"):
        prefix = f"2026-01-0{vid[-1]}-{vid}"
        (channel / f"{prefix}.concepts.json").write_text(
            json.dumps({"video_id": vid, "concepts": [{"concept_id": "ai.mcp", "preferred_label": "MCP"}]}),
            encoding="utf-8",
        )
        (channel / f"{prefix}.meta.json").write_text(
            json.dumps({"video_id": vid, "title": vid, "published": f"2026-01-0{vid[-1]}", "channel": "alpha"}),
            encoding="utf-8",
        )


INDEX_SENTENCE = "Search results come from the search index, which a scan does not update"
READER_CASES = {
    "search": (["search", "mcp"], False),
    "search --vector": (["search", "mcp", "--vector"], True),
    "status": (["status"], False),
    "nugget": (["nugget", "mcp", "--no-save"], True),
    "profile show": (["profile", "show"], False),
    "briefings dry run": (["briefings", "--unseen", "--dry-run"], False),
    "scan dry run": (["scan", "--dry-run"], False),
}


def _reader_setup(cli: Cli, monkeypatch) -> None:
    _two_video_corpus(cli.corpus)
    monkeypatch.setattr(
        vi,
        "hybrid_search",
        lambda *a, **kw: [
            _hit(video_id="vid001", channel="alpha", relevance=0.9, source_file="alpha/2026-01-01-vid001.transcript.md")
        ],
    )
    monkeypatch.setattr(vi, "create_client", lambda *a, **kw: _FakeClient("## Consensus\n- They agree.\n"))


def _notes(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("A writing command is running")]


@pytest.mark.parametrize("case", sorted(READER_CASES))
def test_readers_and_previews_run_while_held_and_note_once(cli, holder, monkeypatch, caplog, case):
    _reader_setup(cli, monkeypatch)
    argv, index_sentence = READER_CASES[case]

    # A bounded join: a reader that waited for the lock would hang here, not pass.
    code = _join(*_run_in_thread(cli, argv))

    assert code == 0
    notes = _notes(caplog)
    assert len(notes) == 1, notes
    assert f"scan, channel alpha, PID {holder.pid}, started " in notes[0]
    assert "(last recorded). Results reflect the corpus as it is now" in notes[0]
    assert (INDEX_SENTENCE in notes[0]) is index_sentence
    assert holder.proc.poll() is None, "the holder still holds"


@pytest.mark.parametrize("case", sorted(READER_CASES))
def test_no_note_without_a_writer(cli, monkeypatch, caplog, case):
    _reader_setup(cli, monkeypatch)

    assert cli.run(READER_CASES[case][0]) == 0

    assert _notes(caplog) == []


def _tree(root: Path) -> dict[str, bytes | None]:
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


def test_scan_dry_run_during_a_held_lock_leaves_the_tree_byte_identical(cli, holder, corpus_lock_state_dir, tmp_path):
    _two_video_corpus(cli.corpus)
    before = _tree(cli.corpus)

    assert cli.run(["scan", "--dry-run"]) == 0

    assert _tree(cli.corpus) == before
    assert "fetch_channel_videos" in cli.paid, "the dry run really ran its fetch"
    assert _try_exit(corpus_lock_state_dir, tmp_path / "sync", cli.corpus) == 4


def test_topic_flush_runs_under_the_lock(cli, corpus_lock_state_dir, tmp_path, monkeypatch):
    cli.record("taxonomy-build")
    seen: list[int] = []
    monkeypatch.setattr(
        vi, "flush_topic_stamps", lambda: seen.append(_try_exit(corpus_lock_state_dir, tmp_path / "s", cli.corpus))
    )

    assert cli.run(["taxonomy-build"]) == 0

    assert seen == [4], "the flush must run while the lock is still held"
    assert _try_exit(corpus_lock_state_dir, tmp_path / "s", cli.corpus) == 0


def _raise(exc: BaseException):
    def _behavior() -> None:
        raise exc

    return _behavior


@pytest.mark.parametrize(
    ("behavior", "expected"),
    [
        (_raise(RuntimeError("boom")), RuntimeError),
        (lambda: sys.exit(vi.EXIT_PARTIAL), vi.EXIT_PARTIAL),
        (_raise(KeyboardInterrupt()), KeyboardInterrupt),
    ],
    ids=["exception", "partial-exit", "ctrl-c"],
)
def test_lock_released_after_failure(cli, corpus_lock_state_dir, tmp_path, behavior, expected):
    cli.record("taxonomy-build", behavior)

    if isinstance(expected, int):
        assert cli.run(["taxonomy-build"]) == expected
    else:
        with pytest.raises(expected):
            cli.run(["taxonomy-build"])

    assert corpus_lock.held_depth(cli.corpus) == 0
    assert _try_exit(corpus_lock_state_dir, tmp_path / "s", cli.corpus) == 0


def test_ctrl_c_while_waiting_exits_130_and_writes_nothing(cli, holder, monkeypatch, caplog, capsys):
    depths = cli.record("taxonomy-build")

    def _interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(corpus_lock, "_sleep", _interrupt)

    code = cli.run(["taxonomy-build"])

    assert code == 130
    assert "Interrupted while waiting for the corpus lock; nothing was written." in [
        r.getMessage() for r in caplog.records if r.levelname == "ERROR"
    ]
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err + captured.out + caplog.text
    assert depths == []
    assert cli.backups() == []


def test_waiting_writer_reads_state_only_after_acquisition(cli, holder, fast_poll, monkeypatch, caplog):
    # Arrange: a recorded snapshot exists, then the config changes so a new snapshot would be due.
    built: list[int] = []
    monkeypatch.setattr(vi, "build_taxonomy", lambda out: built.append(1) or {"concepts": {}, "built_from": 0})
    vi.backup_config_if_changed(cli.corpus, config_path=cli.cfg)
    latest = cli.corpus / vi.CONFIG_BACKUP_DIR_NAME / "config.latest.yaml"
    latest_before = latest.read_bytes()
    _write_config(cli.cfg, cli.corpus, extra="# edited\n")

    # Act: start the writer; it must wait in line.
    thread, result = _run_in_thread(cli, ["taxonomy-build"])
    _wait_for(lambda: "Waiting for the corpus lock: " in caplog.text, "the wait line")

    # Assert: nothing read or written while waiting; everything after the release.
    assert built == []
    assert latest.read_bytes() == latest_before
    holder.release()
    assert _join(thread, result) == 0
    assert built == [1]
    assert latest.read_bytes() != latest_before


def test_config_change_to_another_corpus_while_waiting_exits_one(cli, holder, fast_poll, tmp_path, caplog):
    depths = cli.record("taxonomy-build")
    other = tmp_path / "other-corpus"
    other.mkdir()

    thread, result = _run_in_thread(cli, ["taxonomy-build"])
    _wait_for(lambda: "Waiting for the corpus lock: " in caplog.text, "the wait line")
    cli.point_at(other)
    holder.release()
    code = _join(thread, result)

    assert code == 1
    assert (
        "The config changed while this command waited and now names a different corpus. "
        "Nothing was written; run the command again."
    ) in [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert depths == []
    assert cli.backups() == []
    assert not (other / vi.CONFIG_BACKUP_DIR_NAME).exists()
    assert corpus_lock.held_depth(cli.corpus) == 0


def test_run_that_did_not_wait_loads_config_once(cli):
    cli.record("taxonomy-build")

    assert cli.run(["taxonomy-build"]) == 0

    assert cli.loads == 1


def test_two_spellings_of_one_corpus_contend_and_two_corpora_do_not(cli, holder, tmp_path):
    depths = cli.record("taxonomy-build")

    cli.point_at(str(cli.corpus.parent / "sub" / ".." / cli.corpus.name) + os.sep)
    assert cli.run(["taxonomy-build"], no_wait=True) == 4
    assert depths == []

    other = tmp_path / "other-corpus"
    other.mkdir()
    cli.point_at(other)
    assert cli.run(["taxonomy-build"], no_wait=True) == 0
    assert len(depths) == 1


def test_nothing_before_acquisition_reads_the_corpus(cli, holder, monkeypatch):
    # Arrange: real corpus content that a premature read would touch.
    _two_video_corpus(cli.corpus)
    cli.record("taxonomy-build")
    root = os.path.realpath(cli.corpus)
    reads: list[str] = []

    def _under_corpus(path) -> bool:
        try:
            return os.path.realpath(os.fspath(path)).startswith(root)
        except TypeError:
            return False  # a file descriptor, not a path

    def _wrap_method(name):
        real = getattr(Path, name)

        def _wrapped(self, *a, **kw):
            if _under_corpus(self):
                reads.append(f"{name} {self}")
            return real(self, *a, **kw)

        monkeypatch.setattr(Path, name, _wrapped)

    for method in ("read_bytes", "read_text"):
        _wrap_method(method)
    real_open = builtins.open

    def _open(file, *a, **kw):
        if _under_corpus(file):
            reads.append(f"open {file}")
        return real_open(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", _open)

    # Act
    code = cli.run(["taxonomy-build"], no_wait=True)

    # Assert
    assert code == 4
    assert reads == []
