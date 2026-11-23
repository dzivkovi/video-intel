"""`backfill-durations`: fill `duration_seconds` into metas written without it.

Every duration-dependent transcript quality check is skipped for a meta with
no duration. The command walks the metas already on disk, fetches durations in
batches of 50 through the existing `enrich_with_durations`, and writes only
that one key. These tests drive the real `cmd_backfill_durations` with only
the YouTube client stubbed, and the printed next-step commands through the
real `main()` parser.

Properties defended here, each with a cheap wrong version:

1. **The dry run is free and byte-identical**: no client, no key, no write,
   and a corrupt meta is counted, never quarantined.
2. **Only one key changes**: no `update_meta`, no other field touched.
3. **The re-read wins**: a meta that changed after the walk is not clobbered.
4. **A failure exits 1 and a re-run continues** where it stopped.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

import video_intel as vi


def _vid(n: int) -> str:
    """A synthetic 11-character YouTube-shaped id."""
    return f"vid{n:08d}"


class _FakeVideos:
    """Records the id list of every `videos.list` call; can fail on call N."""

    def __init__(self, durations: dict[str, str | None], fail_on_call: int | None = None):
        self._durations = durations
        self._fail_on_call = fail_on_call
        self.calls: list[list[str]] = []
        self.parts: list[str] = []

    def videos(self):
        return self

    def list(self, *, id, part):
        ids = id.split(",")
        self.calls.append(ids)
        self.parts.append(part)
        call_no = len(self.calls)
        fail = self._fail_on_call
        durations = self._durations

        class _Exec:
            def execute(self_inner):
                if fail is not None and call_no == fail:
                    raise RuntimeError("quotaExceeded (simulated)")
                items = []
                for vid in ids:
                    if vid not in durations:
                        continue
                    iso = durations[vid]
                    items.append({"id": vid, "contentDetails": {} if iso is None else {"duration": iso}})
                return {"items": items}

        return _Exec()


def _args(apply=False, channel=None):
    return type("A", (), {"apply": apply, "channel": channel})()


def _write_meta(path: Path, body) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body, indent=2)
    path.write_text(text, encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    root = tmp_path / "corpus"
    root.mkdir()
    monkeypatch.setattr(vi, "resolve_output_dir", lambda cfg, **_k: root)
    return root


@pytest.fixture
def youtube(monkeypatch):
    """Install a fake client; returns a setter so each test picks its payload."""
    built: list[_FakeVideos] = []

    def install(durations, fail_on_call=None):
        fake = _FakeVideos(durations, fail_on_call)
        monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **k: (built.append(fake), fake)[1])
        return fake

    install.built = built
    return install


def _snapshot(root: Path) -> tuple[dict[str, tuple[bytes, int]], set[str]]:
    """Every file's bytes AND mtime, plus the directory set: a dry run that
    rewrites identical bytes or creates a folder is caught too."""
    files = {
        str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()
    }
    dirs = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir()}
    return files, dirs


class TestApplyWritesOnlyTheDuration:
    def test_apply_candidate_meta_gains_only_duration_seconds(self, corpus, youtube):
        # Arrange
        meta_path = corpus / "chan" / "2026-01-01-a.meta.json"
        before = {
            "video_url": f"https://www.youtube.com/watch?v={_vid(1)}",
            "video_id": _vid(1),
            "channel": "chan",
            "title": "Café talk",
            "last_error": "an earlier failure that must survive",
            "modes_completed": ["mindmap"],
        }
        _write_meta(meta_path, before)
        fake = youtube({_vid(1): "PT1H2M3S"})

        # Act
        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        # Assert
        after = _read(meta_path)
        assert {k: v for k, v in after.items() if k != "duration_seconds"} == before
        assert after["duration_seconds"] == 3723
        assert list(after) == [*before, "duration_seconds"]
        assert meta_path.read_text(encoding="utf-8") == json.dumps({**before, "duration_seconds": 3723}, indent=2)
        assert counts["written"] == 1
        assert fake.parts == ["contentDetails"]

    def test_apply_zero_or_bool_duration_is_replaced_in_place(self, corpus, youtube):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, {"video_id": _vid(1), "duration_seconds": 0, "title": "T"})
        youtube({_vid(1): "PT10M"})

        vi.cmd_backfill_durations(_args(apply=True), {})

        after = _read(meta_path)
        assert after == {"video_id": _vid(1), "duration_seconds": 600, "title": "T"}
        assert list(after) == ["video_id", "duration_seconds", "title"]

    def test_apply_meta_with_duration_is_never_fetched_or_rewritten(self, corpus, youtube):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, {"video_id": _vid(1), "duration_seconds": 42})
        before = meta_path.read_bytes()
        fake = youtube({_vid(1): "PT10M"})

        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert meta_path.read_bytes() == before
        assert fake.calls == []
        assert youtube.built == [], "no candidate means no client"
        assert counts["has_duration"] == 1

    @pytest.mark.parametrize("bad", [0, -5, True, "600", None, 12.5])
    def test_walk_unusable_stored_duration_is_a_candidate(self, corpus, youtube, bad):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1), "duration_seconds": bad})
        youtube({})

        counts = vi.cmd_backfill_durations(_args(apply=False), {})

        assert counts["candidates"] == 1

    def test_apply_local_file_meta_with_youtube_id_is_filled(self, corpus, youtube):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, {"video_id": _vid(1), "transcript_source": "local_file"})
        youtube({_vid(1): "PT5M"})

        vi.cmd_backfill_durations(_args(apply=True), {})

        assert _read(meta_path)["duration_seconds"] == 300

    def test_apply_leaves_no_tmp_file_behind(self, corpus, youtube):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        youtube({_vid(1): "PT5M"})

        vi.cmd_backfill_durations(_args(apply=True), {})

        assert [p.name for p in (corpus / "chan").iterdir()] == ["a.meta.json"]


class TestDryRun:
    def test_dry_run_writes_nothing_and_builds_no_client(self, corpus, youtube, monkeypatch):
        # Arrange: a candidate, a torn meta, a non-object meta and an empty one.
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        (corpus / "chan" / "torn.meta.json").write_bytes(b'{"video_id": "v\xff\xfe')
        _write_meta(corpus / "chan" / "list.meta.json", "[1, 2]")
        _write_meta(corpus / "chan" / "empty.meta.json", "{}")
        before = _snapshot(corpus)
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

        def _no_client():
            raise AssertionError("a dry run built a YouTube client")

        monkeypatch.setattr(vi, "require_youtube", _no_client)

        # Act
        counts = vi.cmd_backfill_durations(_args(apply=False), {})

        # Assert
        assert _snapshot(corpus) == before
        assert counts["candidates"] == 1
        assert counts["unreadable"] == 3

    def test_dry_run_reports_per_channel_counts_and_quota_estimate(self, corpus, youtube, caplog):
        for n in range(51):
            _write_meta(corpus / "chan" / f"m{n}.meta.json", {"video_id": _vid(n)})
        _write_meta(corpus / "other" / "x.meta.json", {"video_id": "local-file-ingest"})
        youtube({})

        with caplog.at_level("INFO", logger=vi.log.name):
            counts = vi.cmd_backfill_durations(_args(apply=False), {})

        text = caplog.text
        assert "[chan]" in text and "candidates=51" in text
        assert "[other]" in text and "no_youtube_id=1" in text
        assert "quota estimate 2 unit(s)" in text
        assert counts["candidates"] == 51
        assert youtube.built == []


class TestSkipBuckets:
    @pytest.mark.parametrize(
        "body",
        [
            {"title": "no id at all"},
            {"video_id": 123},
            {"video_id": ""},
            {"video_id": "fathom-804160513"},
            {"video_id": "local-ingest-stem"},
        ],
    )
    def test_meta_without_youtube_id_is_counted_and_never_fetched(self, corpus, youtube, body):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, body)
        before = meta_path.read_bytes()
        fake = youtube({})

        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert counts["no_youtube_id"] == 1
        assert counts["candidates"] == 0
        assert fake.calls == []
        assert meta_path.read_bytes() == before

    def test_apply_buckets_add_up(self, corpus, youtube):
        # Arrange: one of every walk bucket and every apply outcome.
        chan = corpus / "chan"
        _write_meta(chan / "has.meta.json", {"video_id": _vid(9), "duration_seconds": 10})
        _write_meta(chan / "local.meta.json", {"video_id": "fathom-804160513"})
        (chan / "torn.meta.json").write_bytes(b"{not json")
        _write_meta(chan / "ok.meta.json", {"video_id": _vid(1)})
        _write_meta(chan / "gone.meta.json", {"video_id": _vid(2)})
        _write_meta(chan / "live.meta.json", {"video_id": _vid(3)})
        _write_meta(chan / "nofield.meta.json", {"video_id": _vid(4)})
        _write_meta(chan / "zero.meta.json", {"video_id": _vid(5)})
        youtube({_vid(1): "PT1M", _vid(3): "P0D", _vid(4): None, _vid(5): "PT0S"})

        # Act
        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        # Assert
        assert counts["has_duration"] == 1
        assert counts["no_youtube_id"] == 1
        assert counts["unreadable"] == 1
        assert counts["candidates"] == 5
        assert counts["written"] == 1
        assert counts["not_found"] == 2  # no item, and an item with no duration field
        assert counts["no_duration"] == 2  # P0D and PT0S
        assert counts["changed_since_walk"] == 0
        assert counts["write_failed"] == 0
        walked = len(list(chan.glob("*.meta.json")))
        assert counts["has_duration"] + counts["no_youtube_id"] + counts["unreadable"] + counts["candidates"] == walked
        assert (
            counts["written"]
            + counts["not_found"]
            + counts["no_duration"]
            + counts["changed_since_walk"]
            + counts["write_failed"]
            == counts["candidates"]
        )
        assert "duration_seconds" not in _read(chan / "live.meta.json")
        assert "duration_seconds" not in _read(chan / "gone.meta.json")
        assert (chan / "torn.meta.json").read_bytes() == b"{not json"
        assert not list(chan.glob("*.corrupt*"))


class TestChannelFilter:
    def test_channel_narrows_the_walk(self, corpus, youtube):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        _write_meta(corpus / "other" / "b.meta.json", {"video_id": _vid(2)})
        fake = youtube({_vid(1): "PT1M", _vid(2): "PT2M"})

        vi.cmd_backfill_durations(_args(apply=True, channel="chan"), {})

        assert fake.calls == [[_vid(1)]]
        assert "duration_seconds" not in _read(corpus / "other" / "b.meta.json")

    def test_unknown_channel_makes_no_call(self, corpus, youtube, caplog):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        youtube({_vid(1): "PT1M"})

        with caplog.at_level("WARNING", logger=vi.log.name):
            counts = vi.cmd_backfill_durations(_args(apply=True, channel="typoo"), {})

        assert youtube.built == []
        assert counts["candidates"] == 0
        assert "typoo" in caplog.text


class TestBatchingAndDuplicates:
    def test_fifty_one_candidates_make_two_calls(self, corpus, youtube):
        for n in range(51):
            _write_meta(corpus / "chan" / f"m{n:02d}.meta.json", {"video_id": _vid(n)})
        fake = youtube({_vid(n): "PT3M" for n in range(51)})

        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert [len(c) for c in fake.calls] == [50, 1]
        assert counts["written"] == 51

    def test_duplicate_id_is_fetched_once_and_written_everywhere(self, corpus, youtube):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        _write_meta(corpus / "chan" / "a-rotated.meta.json", {"video_id": _vid(1)})
        _write_meta(corpus / "_standalone" / "a.meta.json", {"video_id": _vid(1)})
        fake = youtube({_vid(1): "PT7M"})

        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert fake.calls == [[_vid(1)]]
        assert counts["written"] == 3
        for p in corpus.rglob("*.meta.json"):
            assert _read(p)["duration_seconds"] == 420


class TestFailureAndContinue:
    def test_mid_run_failure_exits_one_and_rerun_continues(self, corpus, youtube, caplog):
        # Arrange: 51 candidates; the second batch (1 id) fails.
        for n in range(51):
            _write_meta(corpus / "chan" / f"m{n:02d}.meta.json", {"video_id": _vid(n)})
        durations = {_vid(n): "PT3M" for n in range(51)}
        youtube(durations, fail_on_call=2)

        # Act
        with caplog.at_level("INFO", logger=vi.log.name), pytest.raises(SystemExit) as exc:
            vi.cmd_backfill_durations(_args(apply=True), {})

        # Assert: the exit code is the claim, not merely "it exited".
        assert exc.value.code == 1
        written = [p for p in (corpus / "chan").glob("*.meta.json") if "duration_seconds" in _read(p)]
        assert len(written) == 50
        assert "50 meta(s) written so far" in caplog.text
        assert "Re-run the same command to continue" in caplog.text
        assert "assess-transcripts --force" in caplog.text, "an early stop that wrote must still print the recipe"

        # Act again: the re-run fetches only what is left.
        fake = youtube(durations)
        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert fake.calls == [[_vid(50)]]
        assert counts["candidates"] == 1
        assert counts["has_duration"] == 50
        assert counts["written"] == 1


class TestReReadBeforeWrite:
    def _race(self, monkeypatch, mutate):
        """Run `mutate` after the fetch, i.e. between the walk and the write."""
        real = vi.enrich_with_durations

        def _enrich_then_mutate(youtube, ids):
            out = real(youtube, ids)
            mutate()
            return out

        monkeypatch.setattr(vi, "enrich_with_durations", _enrich_then_mutate)

    def test_meta_that_gained_a_duration_is_not_clobbered(self, corpus, youtube, monkeypatch):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, {"video_id": _vid(1)})
        youtube({_vid(1): "PT9M"})
        concurrent = {"video_id": _vid(1), "duration_seconds": 1234, "title": "written by a concurrent run"}
        self._race(monkeypatch, lambda: _write_meta(meta_path, concurrent))

        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert _read(meta_path) == concurrent
        assert counts["changed_since_walk"] == 1
        assert counts["written"] == 0

    def test_concurrent_writers_other_keys_survive(self, corpus, youtube, monkeypatch):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, {"video_id": _vid(1)})
        youtube({_vid(1): "PT9M"})
        self._race(monkeypatch, lambda: _write_meta(meta_path, {"video_id": _vid(1), "transcript_status": "complete"}))

        vi.cmd_backfill_durations(_args(apply=True), {})

        assert _read(meta_path) == {"video_id": _vid(1), "transcript_status": "complete", "duration_seconds": 540}

    def test_meta_deleted_after_the_walk_is_not_recreated(self, corpus, youtube, monkeypatch):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, {"video_id": _vid(1)})
        youtube({_vid(1): "PT9M"})
        self._race(monkeypatch, meta_path.unlink)

        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert not meta_path.exists()
        assert counts["changed_since_walk"] == 1

    def test_meta_whose_id_changed_is_not_written(self, corpus, youtube, monkeypatch):
        meta_path = corpus / "chan" / "a.meta.json"
        _write_meta(meta_path, {"video_id": _vid(1)})
        youtube({_vid(1): "PT9M"})
        self._race(monkeypatch, lambda: _write_meta(meta_path, {"video_id": _vid(2)}))

        counts = vi.cmd_backfill_durations(_args(apply=True), {})

        assert _read(meta_path) == {"video_id": _vid(2)}
        assert counts["changed_since_walk"] == 1


class TestNextStepCommands:
    def _logged_commands(self, text: str) -> list[list[str]]:
        prefix = "python scripts/video_intel.py "
        return [shlex.split(line.split(prefix, 1)[1]) for line in text.splitlines() if prefix in line]

    def _parse_through_main(self, monkeypatch, tmp_path, argv: list[str]):
        seen = []
        monkeypatch.setattr(vi, "load_config", lambda: {"output_dir": str(tmp_path)})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
        monkeypatch.setattr(vi, "cmd_assess_transcripts", lambda args, config: seen.append(args))
        monkeypatch.setattr(sys, "argv", ["video_intel.py", *argv])
        vi.main()
        assert len(seen) == 1
        return seen[0]

    def test_printed_commands_parse_through_the_real_parser(self, tmp_path, monkeypatch, youtube, caplog):
        # Arrange: an output folder whose name has a space, like a cloud mount.
        root = tmp_path / "My Corpus"
        monkeypatch.setattr(vi, "resolve_output_dir", lambda cfg, **_k: root)
        _write_meta(root / "chan" / "a.meta.json", {"video_id": _vid(1)})
        youtube({_vid(1): "PT1M"})

        with caplog.at_level("INFO", logger=vi.log.name):
            vi.cmd_backfill_durations(_args(apply=True, channel="chan"), {})
        commands = self._logged_commands(caplog.text)

        # Assert: dry run first, then the apply form, both parse.
        assert len(commands) == 2
        dry = self._parse_through_main(monkeypatch, tmp_path, commands[0])
        assert dry.command == "assess-transcripts"
        assert dry.force is True and dry.apply is False and dry.channel == "chan"
        apply = self._parse_through_main(monkeypatch, tmp_path, commands[1])
        assert apply.force is True and apply.apply is True and apply.channel == "chan"
        assert Path(apply.backup_dir).parent == root / "_meta-backups"

    def test_no_commands_when_nothing_was_written(self, corpus, youtube, caplog):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        youtube({})

        with caplog.at_level("INFO", logger=vi.log.name):
            vi.cmd_backfill_durations(_args(apply=True), {})

        assert self._logged_commands(caplog.text) == []

    def test_no_commands_on_a_dry_run(self, corpus, youtube, caplog):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        youtube({_vid(1): "PT1M"})

        with caplog.at_level("INFO", logger=vi.log.name):
            vi.cmd_backfill_durations(_args(apply=False), {})

        assert self._logged_commands(caplog.text) == []


class TestCommandRegistration:
    def test_it_snapshots_the_config_like_other_corpus_writers(self):
        assert "backfill-durations" in vi.CONFIG_BACKUP_COMMANDS

    def test_main_dispatches_without_channels_in_config(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(vi, "load_config", lambda: {"output_dir": str(tmp_path)})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
        monkeypatch.setattr(vi, "cmd_backfill_durations", lambda args, config: seen.append(args))
        monkeypatch.setattr(sys, "argv", ["video_intel.py", "backfill-durations", "--apply", "--channel", "chan"])

        vi.main()

        assert len(seen) == 1
        assert seen[0].apply is True and seen[0].channel == "chan"


class TestFailuresAreCountedNotTracebacks:
    def test_write_failure_is_counted_others_written_exit_one(self, corpus, youtube, monkeypatch, caplog):
        # Arrange: three candidates; replacing the middle one fails like a locked file.
        for n in range(3):
            _write_meta(corpus / "chan" / f"m{n}.meta.json", {"video_id": _vid(n)})
        youtube({_vid(n): "PT2M" for n in range(3)})
        real_replace = vi.os.replace

        def locked(src, dst):
            if Path(dst).name == "m1.meta.json":
                raise PermissionError("locked by another process")
            return real_replace(src, dst)

        monkeypatch.setattr(vi.os, "replace", locked)

        # Act
        with caplog.at_level("INFO", logger=vi.log.name), pytest.raises(SystemExit) as exc:
            vi.cmd_backfill_durations(_args(apply=True), {})

        # Assert
        assert exc.value.code == 1
        assert "duration_seconds" in _read(corpus / "chan" / "m0.meta.json")
        assert "duration_seconds" not in _read(corpus / "chan" / "m1.meta.json")
        assert "duration_seconds" in _read(corpus / "chan" / "m2.meta.json")
        assert "write_failed=1" in caplog.text
        assert "Re-run the same command to continue" in caplog.text
        assert "assess-transcripts --force" in caplog.text
        assert not list((corpus / "chan").glob("*.tmp"))

    def test_walk_read_error_propagates_without_any_write(self, corpus, youtube, monkeypatch):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        _write_meta(corpus / "chan" / "b.meta.json", {"video_id": _vid(2)})
        youtube({_vid(1): "PT1M", _vid(2): "PT1M"})
        real_read_bytes = Path.read_bytes

        def locked(self):
            if self.name == "b.meta.json":
                raise PermissionError("locked")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", locked)

        with pytest.raises(PermissionError):
            vi.cmd_backfill_durations(_args(apply=True), {})

        assert youtube.built == []
        assert "duration_seconds" not in _read(corpus / "chan" / "a.meta.json")

    def test_apply_without_api_key_exits_one(self, corpus, monkeypatch):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)

        with pytest.raises(SystemExit) as exc:
            vi.cmd_backfill_durations(_args(apply=True), {})

        assert exc.value.code == 1


class TestChannelThatSelectsNothing:
    def test_existing_folder_with_no_meta_warns(self, corpus, youtube, caplog):
        _write_meta(corpus / "chan" / "a.meta.json", {"video_id": _vid(1)})
        (corpus / "empty").mkdir()
        youtube({})

        with caplog.at_level("WARNING", logger=vi.log.name):
            counts = vi.cmd_backfill_durations(_args(apply=False, channel="empty"), {})

        assert counts["candidates"] == 0
        assert "selected no meta.json" in caplog.text


class TestBareInvocationIsADryRun:
    def test_main_without_flags_parses_apply_false(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(vi, "load_config", lambda: {"output_dir": str(tmp_path)})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
        monkeypatch.setattr(vi, "cmd_backfill_durations", lambda args, config: seen.append(args))
        monkeypatch.setattr(sys, "argv", ["video_intel.py", "backfill-durations"])

        vi.main()

        assert len(seen) == 1
        assert seen[0].apply is False
        assert seen[0].channel is None
