"""The YouTube description is fetched by every producer and must be kept (#224).

Before this change, `snippet.description` was requested by four API calls and
thrown away at seven sites. For a pointer-feed channel that is where the
shownotes PDF link and every `Links featured:` repo URL live, and nowhere else.

Three properties this file exists to defend, because each one has a cheap wrong
version that looks right:

1. **No new API call.** The data is already in hand. A fix that adds a second
   `videos.list` would cost quota per video for a field we were discarding.
2. **The description NEVER reaches the search index.** `_extract_video_metadata`
   whitelists four keys, so this holds by construction today - the test pins it
   so a later "just pass the whole meta through" refactor fails loudly rather
   than quietly filling LanceDB with marketing URLs.
3. **Filling never clobbers.** A manual run that skipped the API lookup, or a
   video with an empty description, must leave whatever is already on disk
   alone. The falsy-drop in `_transcript_identity_fields` is what enforces it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from datetime import UTC

import video_intel as vi

DESC = "Link to Resources: https://example.test/notes\n\nLinks featured:\n- Archify - https://github.com/tt-ali/archify"


class _Executable:
    def __init__(self, payload, recorder, label):
        self._payload = payload
        self._recorder = recorder
        self._label = label

    def execute(self):
        self._recorder.append(self._label)
        return self._payload


class FakeYouTube:
    """Records every API call so a test can prove none was ADDED."""

    def __init__(self, *, playlist_items=None, videos=None, search=None):
        self.calls: list[str] = []
        self._playlist_items = playlist_items or {}
        self._videos = videos or {}
        self._search = search or {}

    def playlistItems(self):
        return self

    def videos(self):
        return self

    def search(self):
        return self

    def list(self, **kw):
        if "playlistId" in kw:
            return _Executable(self._playlist_items, self.calls, f"playlistItems:{kw.get('part')}")
        if "q" in kw:
            return _Executable(self._search, self.calls, f"search:{kw.get('part')}")
        return _Executable(self._videos, self.calls, f"videos:{kw.get('part')}")


def _playlist_payload(desc=DESC):
    return {
        "items": [
            {
                "contentDetails": {"videoId": "vid123", "videoPublishedAt": "2026-09-11T10:00:00Z"},
                "snippet": {"title": "Top 10 Repos", "publishedAt": "2026-09-11T10:00:00Z", "description": desc},
            }
        ]
    }


class TestProducersKeepTheDescriptionTheyAlreadyFetch:
    def test_fetch_channel_videos_keeps_it(self):
        yt = FakeYouTube(playlist_items=_playlist_payload())
        from datetime import datetime

        videos = vi.fetch_channel_videos(yt, "UCabc", datetime(2026, 1, 1, tzinfo=UTC))
        assert videos[0]["description"] == DESC

    def test_fetch_playlist_videos_keeps_it(self):
        yt = FakeYouTube(playlist_items=_playlist_payload())
        videos = vi.fetch_playlist_videos(yt, "PLabc")
        assert videos[0]["description"] == DESC

    def test_an_empty_description_becomes_none_not_empty_string(self):
        """So the falsy-drop treats 'no description' and 'absent' identically."""
        yt = FakeYouTube(playlist_items=_playlist_payload(desc=""))
        videos = vi.fetch_playlist_videos(yt, "PLabc")
        assert videos[0]["description"] is None

    def test_no_extra_api_call_is_made(self):
        """The whole premise: the data was already being paid for."""
        yt = FakeYouTube(playlist_items=_playlist_payload())
        vi.fetch_playlist_videos(yt, "PLabc")
        assert yt.calls == ["playlistItems:snippet,contentDetails"], (
            "the producer made a call it did not make before this change"
        )


class TestPreflightIsTheAuthoritativeCopy:
    def test_preflight_returns_the_description(self):
        yt = FakeYouTube(
            videos={
                "items": [
                    {
                        "id": "vid123",
                        "snippet": {"liveBroadcastContent": "none", "description": DESC},
                        "status": {"privacyStatus": "public"},
                    }
                ]
            }
        )
        out = vi.fetch_preflight_status(yt, ["vid123"])
        assert out["vid123"]["description"] == DESC

    def test_preflight_still_makes_exactly_one_call_with_the_same_parts(self):
        """Issue #120's 'parts are free, never a second call' rule. The part
        string is unchanged: `snippet` was already requested."""
        yt = FakeYouTube(videos={"items": []})
        vi.fetch_preflight_status(yt, ["a", "b"])
        assert yt.calls == ["videos:snippet,status,liveStreamingDetails"]

    def test_a_video_missing_from_the_response_yields_no_description(self):
        yt = FakeYouTube(videos={"items": []})
        out = vi.fetch_preflight_status(yt, ["ghost"])
        assert out["ghost"].get("description") is None


class TestTheMetaSeamCarriesItAndNeverClobbers:
    def test_the_description_reaches_the_meta_fields(self, tmp_path):
        fields = vi._transcript_identity_fields(
            {"video_id": "v", "url": "u", "title": "t", "published": "2026-09-11", "description": DESC},
            tmp_path / "chan",
        )
        assert fields["description"] == DESC

    @pytest.mark.parametrize("absent", [{}, {"description": None}, {"description": ""}])
    def test_an_absent_or_empty_description_is_dropped_not_written(self, tmp_path, absent):
        """The clobber guard. A manual run that passed --channel/--title/--date
        never calls the API, so it has no description - and must not erase one."""
        video = {"video_id": "v", "url": "u", "title": "t", "published": "2026-09-11", **absent}
        assert "description" not in vi._transcript_identity_fields(video, tmp_path / "chan")

    def test_a_real_writer_merges_rather_than_replaces(self, tmp_path):
        """End to end through update_meta: a later write with no description
        leaves the stored one intact."""
        meta_path = tmp_path / "x.meta.json"
        vi.update_meta(meta_path, {"video_id": "v", "description": DESC}, mode="identity")
        vi.update_meta(meta_path, {"video_id": "v", "title": "renamed"}, mode="identity")
        assert json.loads(meta_path.read_text(encoding="utf-8"))["description"] == DESC


class TestTheIndexNeverSeesIt:
    def test_extract_video_metadata_ignores_the_description(self, tmp_path):
        chan = tmp_path / "chan"
        chan.mkdir()
        (chan / "p.meta.json").write_text(
            json.dumps({"title": "T", "published": "2026-09-11", "video_id": "v", "description": DESC}),
            encoding="utf-8",
        )
        out = vi._extract_video_metadata("p", chan, "chan")
        assert set(out) == {"title", "published", "video_id", "channel"}
        assert DESC not in json.dumps(out)

    def test_the_guard_is_not_vacuous(self, tmp_path):
        """Proves the fixture really does put a description in front of the
        function - otherwise the assertion above passes against nothing."""
        chan = tmp_path / "chan"
        chan.mkdir()
        (chan / "p.meta.json").write_text(
            json.dumps({"title": "T", "published": "2026-09-11", "video_id": "v", "description": DESC}),
            encoding="utf-8",
        )
        assert "description" in json.loads((chan / "p.meta.json").read_text(encoding="utf-8"))


class TestBackfill:
    def _corpus(self, tmp_path, metas):
        chan = tmp_path / "chan"
        chan.mkdir()
        for name, body in metas.items():
            (chan / name).write_text(json.dumps(body), encoding="utf-8")
        return chan

    def _args(self, apply=False, channel=None):
        return type("A", (), {"apply": apply, "channel": channel})()

    def _patch(self, monkeypatch, tmp_path, yt):
        monkeypatch.setattr(vi, "resolve_output_dir", lambda cfg: tmp_path)
        monkeypatch.setenv("YOUTUBE_API_KEY", "k")
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **k: yt)

    def test_it_fills_a_meta_that_has_none(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, {"a.meta.json": {"video_id": "aX8Y183qDpY", "title": "T"}})
        yt = FakeYouTube(videos={"items": [{"id": "aX8Y183qDpY", "snippet": {"description": DESC}}]})
        self._patch(monkeypatch, tmp_path, yt)
        assert vi.cmd_backfill_descriptions(self._args(apply=True), {}) == 1
        assert json.loads((tmp_path / "chan" / "a.meta.json").read_text(encoding="utf-8"))["description"] == DESC

    def test_it_never_overwrites_an_existing_description(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, {"a.meta.json": {"video_id": "aX8Y183qDpY", "description": "mine, hand-edited"}})
        yt = FakeYouTube(videos={"items": [{"id": "aX8Y183qDpY", "snippet": {"description": DESC}}]})
        self._patch(monkeypatch, tmp_path, yt)
        vi.cmd_backfill_descriptions(self._args(apply=True), {})
        stored = json.loads((tmp_path / "chan" / "a.meta.json").read_text(encoding="utf-8"))["description"]
        assert stored == "mine, hand-edited"
        assert yt.calls == [], "a meta that already had one must not even be fetched"

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, {"a.meta.json": {"video_id": "aX8Y183qDpY"}})
        yt = FakeYouTube(videos={"items": [{"id": "aX8Y183qDpY", "snippet": {"description": DESC}}]})
        self._patch(monkeypatch, tmp_path, yt)
        vi.cmd_backfill_descriptions(self._args(apply=False), {})
        assert "description" not in json.loads((tmp_path / "chan" / "a.meta.json").read_text(encoding="utf-8"))

    def test_a_non_youtube_video_id_is_refused_not_reported_as_deleted(self, tmp_path, monkeypatch):
        """A Fathom/Goldcast/local-file id would return nothing from the API
        and then be counted as "deleted, private, or empty", misattributing
        the cause. repair-metas refuses non-YouTube sources; so does this."""
        self._corpus(tmp_path, {"a.meta.json": {"video_id": "fathom-804160513"}})
        yt = FakeYouTube(videos={"items": []})
        self._patch(monkeypatch, tmp_path, yt)
        assert vi.cmd_backfill_descriptions(self._args(apply=True), {}) == 0
        assert yt.calls == [], "a non-YouTube id reached the YouTube API"

    def test_a_typoed_channel_does_not_read_as_success(self, tmp_path, monkeypatch):
        """Issue #183 invariant 5c: folder existence separates a typo from a
        channel that is genuinely already complete."""
        self._corpus(tmp_path, {"a.meta.json": {"video_id": "aX8Y183qDpY"}})
        yt = FakeYouTube(videos={"items": []})
        self._patch(monkeypatch, tmp_path, yt)
        assert vi.cmd_backfill_descriptions(self._args(apply=True, channel="typoo"), {}) == 0
        assert yt.calls == []

    def test_a_meta_without_a_usable_video_id_is_skipped_not_coerced(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, {"a.meta.json": {"video_id": 123}, "b.meta.json": {"title": "no id"}})
        yt = FakeYouTube(videos={"items": []})
        self._patch(monkeypatch, tmp_path, yt)
        assert vi.cmd_backfill_descriptions(self._args(apply=True), {}) == 0
        assert yt.calls == []

    def test_an_unreadable_meta_does_not_abort_the_walk(self, tmp_path, monkeypatch):
        chan = self._corpus(tmp_path, {"good.meta.json": {"video_id": "aX8Y183qDpY"}})
        (chan / "bad.meta.json").write_bytes(b'{"video_id": "v\xff\xfe')
        yt = FakeYouTube(videos={"items": [{"id": "aX8Y183qDpY", "snippet": {"description": DESC}}]})
        self._patch(monkeypatch, tmp_path, yt)
        assert vi.cmd_backfill_descriptions(self._args(apply=True), {}) == 1

    def test_a_video_gone_upstream_is_not_an_error(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, {"a.meta.json": {"video_id": "dEl3t3dV1d0"}})
        yt = FakeYouTube(videos={"items": []})
        self._patch(monkeypatch, tmp_path, yt)
        assert vi.cmd_backfill_descriptions(self._args(apply=True), {}) == 0

    def test_channel_filter_restricts_the_walk(self, tmp_path, monkeypatch):
        self._corpus(tmp_path, {"a.meta.json": {"video_id": "aX8Y183qDpY"}})
        other = tmp_path / "other"
        other.mkdir()
        (other / "b.meta.json").write_text(json.dumps({"video_id": "NuMmY3bX6NI"}), encoding="utf-8")
        yt = FakeYouTube(videos={"items": [{"id": "aX8Y183qDpY", "snippet": {"description": DESC}}]})
        self._patch(monkeypatch, tmp_path, yt)
        vi.cmd_backfill_descriptions(self._args(apply=True, channel="chan"), {})
        assert "description" not in json.loads((other / "b.meta.json").read_text(encoding="utf-8"))

    def test_it_is_classified_as_a_corpus_mutating_command(self):
        """It writes into output_dir, so the config snapshot must fire for it."""
        assert "backfill-descriptions" in vi.CONFIG_BACKUP_COMMANDS


class _FakeYouTubeForCommands:
    """Returns a snippet carrying a description, like the real API does."""

    def __init__(self, description=DESC):
        self._description = description
        self.calls = 0

    def videos(self):
        return self

    def list(self, **kw):
        self.calls += 1
        return self

    def execute(self):
        return {
            "items": [
                {
                    "snippet": {
                        "title": "A Talk",
                        "publishedAt": "2026-08-12T00:00:00Z",
                        "channelId": "UC-unconfigured",
                        "channelTitle": "Some Creator",
                        "description": self._description,
                    }
                }
            ]
        }


def _cmd_args(**overrides):
    from types import SimpleNamespace

    base = {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "file": None,
        "channel": None,
        "title": None,
        "date": None,
        "start": None,
        "end": None,
        "force": False,
        "prompt": None,
        "model": None,
        "video_id": None,
        "media_resolution": "low",
        "chunk_minutes": None,
        "transcript_source": None,
        "topic": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def drive_command(monkeypatch, tmp_path):
    """Run a real manual --url command with only the network stubbed, and
    capture the `video` dict the processing helpers actually receive.

    This is the coverage that matters (issue #205 invariant 6): a suite that
    only calls `_transcript_identity_fields` directly passes happily while a
    command builds its video dict without a description.
    """

    def _wire(description=DESC):
        captured: list[dict] = []
        yt = _FakeYouTubeForCommands(description)

        def record(*a, **kw):
            for value in list(a) + list(kw.values()):
                if isinstance(value, dict) and "video_id" in value:
                    captured.append(value)
            return ("2026-08-12-a-talk", "done")

        for name in ("process_transcript", "process_mindmap", "process_concepts"):
            monkeypatch.setattr(vi, name, record)
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: yt)
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
        monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _v: False)
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _v: 600)
        monkeypatch.setattr(vi, "require_channels_config", lambda _c: None)
        monkeypatch.setattr(vi, "match_configured_channel", lambda *_a, **_kw: None)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake-yt-key")
        return captured, yt

    return _wire


_COMMANDS = {"transcript": "cmd_transcript", "mindmap": "cmd_mindmap", "process": "cmd_process"}


class TestEveryManualUrlCommandCapturesIt:
    """Drives the REAL commands. A helper that is unit-tested but never proven
    to be reached is the same blind spot as a stub agreeing with itself."""

    @pytest.mark.parametrize("command", sorted(_COMMANDS))
    def test_the_description_reaches_the_processing_helpers(self, command, drive_command):
        captured, _ = drive_command()
        code = _run_command(command, _cmd_args(), {"channels": []})
        assert code in (0, vi.EXIT_PARTIAL), f"{command}: unexpected exit {code}"
        assert captured, f"{command}: no video dict reached the processing helpers"
        assert any(v.get("description") == DESC for v in captured), (
            f"{command}: built its video dict without the description the API returned"
        )

    @pytest.mark.parametrize("command", sorted(_COMMANDS))
    def test_no_lookup_means_no_description_and_no_crash(self, command, drive_command):
        """With --channel/--title/--date supplied the API is never called, so
        there is nothing to carry - and nothing may blow up."""
        captured, yt = drive_command()
        code = _run_command(command, _cmd_args(channel="mychan", title="T", date="2026-08-12"), {"channels": []})
        assert code in (0, vi.EXIT_PARTIAL), f"{command}: unexpected exit {code}"
        assert yt.calls == 0, f"{command}: made an API call it did not need"
        assert captured, f"{command}: no video dict reached the processing helpers"
        assert all(v.get("description") is None for v in captured)


def _run_command(command, args, config):
    """Run a real command and return its exit code (0 when it did not exit).

    NOT `contextlib.suppress(SystemExit, Exception)`. Two things went wrong
    with that (in-family review of PR #229): a post-capture crash injected
    into the command left every case green, so a test named
    `..._and_no_crash` could not detect a crash; and the `[process]` cases
    were already passing over a real `SystemExit(3)` (EXIT_PARTIAL). Per the
    issue #185 rule, a test that asserts on an exit code asserts the CODE.
    Anything that is not a SystemExit propagates and fails the test.
    """
    try:
        getattr(vi, _COMMANDS[command])(args, config)
    except SystemExit as exc:
        return exc.code if exc.code is not None else 0
    return 0
