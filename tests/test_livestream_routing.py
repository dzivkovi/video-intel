"""Tests for completed-livestream VOD routing (issue #120).

Completed livestream VODs fail Gemini's YouTube-URI ingestion at a far higher
rate than regular uploads (10 of 22 broken vs 0 of 377 in the 2026-07-24 corpus
sample). So they route captions-FIRST, get at most ONE guarded Gemini transcript
attempt when no caption track exists, and never spend a mindmap-from-video call
against the same URI after that attempt fails.

Regular uploads must keep byte-identical routing - that is the highest
regression risk in this change, so it gets its own class.
"""

import json
from argparse import Namespace
from unittest.mock import MagicMock

from youtube_captions import CaptionsResult

import video_intel as vi

# ---------------------------------------------------------------------------
# Shared stubs (mirrors tests/test_captions_failover.py's style)
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = {
    "transcripts": [{"start": "00:00", "voice": 1, "text": "hello world"}],
    "speakers": [{"voice": 1, "name": "A"}],
    "screen_content": [],
}


class _Usage:
    def __init__(self, prompt):
        self.prompt_token_count = prompt
        self.cached_content_token_count = 0
        self.thoughts_token_count = 0
        self.candidates_token_count = 10
        self.total_token_count = prompt + 10


class _Resp:
    def __init__(self, prompt):
        self.usage_metadata = _Usage(prompt)


def _video():
    return {
        "video_id": "vid123",
        "url": "https://www.youtube.com/watch?v=vid123",
        "title": "Test",
        "published": "2026-06-13",
    }


def _stub_gemini(monkeypatch, *, prompt_tokens=5000, payload=None, raises=None, calls=None, raw_text=None):
    """Stub call_gemini; `calls` (a list) records every video-ingestion call."""

    def fake_call_gemini(client, types, media_uri, prompt, model, response_json=False, **kw):
        if calls is not None:
            calls.append(media_uri)
        if raises is not None:
            raise raises
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(_Resp(prompt_tokens))
        if raw_text is not None:
            return raw_text
        return json.dumps(payload if payload is not None else _VALID_PAYLOAD)

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)


def _stub_captions(monkeypatch, captions, *, fetches=None):
    """Stub fetch_english_captions; `fetches` (a list) records every fetch."""

    def fake_fetch(video_id):
        if fetches is not None:
            fetches.append(video_id)
        return captions

    monkeypatch.setattr(vi, "fetch_english_captions", fake_fetch)


def _run(tmp_path, transcript_source, **kw):
    prefix = "2026-06-13-test"
    return (
        vi.process_transcript(
            object(),
            None,
            _video(),
            "prompt",
            "stub-model",
            tmp_path,
            prefix,
            transcript_source=transcript_source,
            media_resolution="LOW",
            **kw,
        ),
        tmp_path / f"{prefix}.transcript.md",
        tmp_path / f"{prefix}.meta.json",
    )


# ---------------------------------------------------------------------------
# (d) fetch_preflight_status classification
# ---------------------------------------------------------------------------


class TestPreflightLivestreamClassification:
    def _yt(self, items):
        yt = MagicMock()
        yt.videos.return_value.list.return_value.execute.return_value = {"items": items}
        return yt

    def test_completed_livestream_vod_is_flagged(self):
        yt = self._yt(
            [
                {
                    "id": "vod",
                    "snippet": {"liveBroadcastContent": "none"},
                    "status": {"privacyStatus": "public"},
                    "liveStreamingDetails": {
                        "actualStartTime": "2026-06-01T18:00:00Z",
                        "actualEndTime": "2026-06-01T20:00:00Z",
                    },
                }
            ]
        )
        result = vi.fetch_preflight_status(yt, ["vod"])
        assert result["vod"]["was_livestream"] is True
        # It has aired, so the issue #70 pre-flight must still KEEP it.
        assert vi.preflight_skip_reason(result["vod"]) is None

    def test_regular_upload_is_not_flagged(self):
        yt = self._yt(
            [{"id": "reg", "snippet": {"liveBroadcastContent": "none"}, "status": {"privacyStatus": "public"}}]
        )
        result = vi.fetch_preflight_status(yt, ["reg"])
        assert result["reg"]["was_livestream"] is False
        assert vi.preflight_skip_reason(result["reg"]) is None

    def test_upcoming_premiere_is_not_a_vod_and_still_skips(self):
        # A scheduled premiere carries liveStreamingDetails (scheduledStartTime)
        # but has not aired: it must NOT be treated as a completed VOD, and the
        # issue #70 skip must keep firing.
        yt = self._yt(
            [
                {
                    "id": "prem",
                    "snippet": {"liveBroadcastContent": "upcoming"},
                    "status": {"privacyStatus": "public"},
                    "liveStreamingDetails": {"scheduledStartTime": "2026-09-01T18:00:00Z"},
                }
            ]
        )
        result = vi.fetch_preflight_status(yt, ["prem"])
        assert result["prem"]["was_livestream"] is False
        assert "not yet aired" in vi.preflight_skip_reason(result["prem"])

    def test_in_progress_live_is_not_a_vod_and_still_skips(self):
        yt = self._yt(
            [
                {
                    "id": "live",
                    "snippet": {"liveBroadcastContent": "live"},
                    "status": {"privacyStatus": "public"},
                    "liveStreamingDetails": {"actualStartTime": "2026-06-01T18:00:00Z"},
                }
            ]
        )
        result = vi.fetch_preflight_status(yt, ["live"])
        assert result["live"]["was_livestream"] is False
        assert "not yet aired" in vi.preflight_skip_reason(result["live"])

    def test_missing_id_has_no_positive_livestream_signal(self):
        yt = self._yt([])
        result = vi.fetch_preflight_status(yt, ["gone"])
        assert result["gone"] == {}
        assert result["gone"].get("was_livestream") is not True

    def test_requests_live_streaming_details_part(self):
        # No new API call - the existing preflight call just asks for one more part.
        yt = self._yt([])
        vi.fetch_preflight_status(yt, ["a"])
        part = yt.videos.return_value.list.call_args.kwargs["part"]
        assert "liveStreamingDetails" in part
        assert "snippet" in part and "status" in part
        assert yt.videos.return_value.list.return_value.execute.call_count == 1


# ---------------------------------------------------------------------------
# (a) + (b) transcript routing for completed livestream VODs
# ---------------------------------------------------------------------------


class TestLivestreamTranscriptRouting:
    def test_captions_first_never_invokes_gemini(self, tmp_path, monkeypatch):
        calls: list[str] = []
        _stub_gemini(monkeypatch, calls=calls)
        _stub_captions(monkeypatch, CaptionsResult([(0.0, "livestream speech")], True, "en"))
        (_, status), tpath, mpath = _run(tmp_path, "gemini", was_livestream=True)
        assert "captions" in status
        assert calls == [], "Gemini video ingestion must NOT be invoked for a captioned livestream VOD"
        meta = json.loads(mpath.read_text())
        assert meta["transcript_source"] == "youtube_captions"
        assert tpath.exists()

    def test_captions_first_overrides_channel_transcript_source(self, tmp_path, monkeypatch):
        # transcript_source: gemini on the channel must not defeat captions-first.
        for source in ("gemini", "auto"):
            calls: list[str] = []
            _stub_gemini(monkeypatch, calls=calls)
            _stub_captions(monkeypatch, CaptionsResult([(0.0, "speech")], True, "en"))
            target = tmp_path / source
            (_, status), _, _ = _run(target, source, was_livestream=True)
            assert "captions" in status
            assert calls == []

    def test_captionless_livestream_gets_exactly_one_gemini_attempt(self, tmp_path, monkeypatch):
        # Unparseable text would normally buy a bounded parse retry (2 calls).
        calls: list[str] = []
        _stub_gemini(monkeypatch, calls=calls, raw_text="not json at all {{{")
        _stub_captions(monkeypatch, None)
        (_, status), tpath, _ = _run(tmp_path, "gemini", was_livestream=True)
        assert len(calls) == 1, f"livestream VOD must get ONE Gemini attempt, got {len(calls)}"
        assert status.startswith("error")
        assert not tpath.exists()

    def test_captionless_livestream_healthy_gemini_still_lands(self, tmp_path, monkeypatch):
        # A healthy captionless VOD must not be needlessly lost.
        calls: list[str] = []
        _stub_gemini(monkeypatch, calls=calls, prompt_tokens=5000)
        _stub_captions(monkeypatch, None)
        (_, status), tpath, mpath = _run(tmp_path, "gemini", was_livestream=True)
        assert status == "done"
        assert len(calls) == 1
        assert tpath.exists()
        assert json.loads(mpath.read_text())["transcript_source"] == "gemini"

    def test_captionless_livestream_prompt_zero_is_guarded(self, tmp_path, monkeypatch):
        calls: list[str] = []
        _stub_gemini(monkeypatch, calls=calls, prompt_tokens=0)
        _stub_captions(monkeypatch, None)
        (_, status), tpath, _ = _run(tmp_path, "gemini", was_livestream=True)
        assert "confabulation" in status
        assert not tpath.exists()
        assert len(calls) == 1

    def test_captions_are_fetched_once_not_re_fetched_after_gemini_fails(self, tmp_path, monkeypatch):
        # transcript_source=auto would normally retry captions on Gemini failure;
        # captions-first already proved there are none, so do not pay for it twice.
        fetches: list[str] = []
        _stub_gemini(monkeypatch, raises=RuntimeError("400 INVALID_ARGUMENT"))
        _stub_captions(monkeypatch, None, fetches=fetches)
        (_, status), _, _ = _run(tmp_path, "auto", was_livestream=True)
        assert status.startswith("error")
        assert len(fetches) == 1, f"caption track must be fetched once, got {len(fetches)}"

    def test_yt_captions_source_branch_is_unchanged(self, tmp_path, monkeypatch):
        # An explicit yt-captions channel keeps its own contract (error when no
        # captions), livestream or not.
        _stub_gemini(monkeypatch)
        _stub_captions(monkeypatch, None)
        (_, status), tpath, _ = _run(tmp_path, "yt-captions", was_livestream=True)
        assert "no captions" in status
        assert not tpath.exists()


# ---------------------------------------------------------------------------
# (c) regular uploads keep EXACTLY current routing
# ---------------------------------------------------------------------------


class TestRegularUploadRoutingUnchanged:
    def test_gemini_source_ignores_available_captions(self, tmp_path, monkeypatch):
        calls: list[str] = []
        fetches: list[str] = []
        _stub_gemini(monkeypatch, calls=calls)
        _stub_captions(monkeypatch, CaptionsResult([(0.0, "captions")], True, "en"), fetches=fetches)
        (_, status), _, mpath = _run(tmp_path, "gemini")
        assert status == "done"
        assert len(calls) == 1
        assert fetches == [], "regular upload on transcript_source=gemini must never consult captions"
        assert json.loads(mpath.read_text())["transcript_source"] == "gemini"

    def test_parse_retry_budget_intact(self, tmp_path, monkeypatch):
        calls: list[str] = []
        _stub_gemini(monkeypatch, calls=calls, raw_text="not json at all {{{")
        _stub_captions(monkeypatch, None)
        _run(tmp_path, "gemini")
        assert len(calls) == 1 + vi.TRANSCRIPT_PARSE_RETRY_LIMIT

    def test_auto_captions_failover_still_fires_on_failure(self, tmp_path, monkeypatch):
        calls: list[str] = []
        _stub_gemini(monkeypatch, calls=calls, raises=RuntimeError("400 INVALID_ARGUMENT"))
        _stub_captions(monkeypatch, CaptionsResult([(0.0, "fallback")], True, "en"))
        (_, status), _, mpath = _run(tmp_path, "auto")
        assert "captions" in status
        assert len(calls) == 1, "captions must come AFTER Gemini for a regular upload"
        assert "gemini error" in json.loads(mpath.read_text())["transcript_failover_reason"]

    def test_default_flag_is_false(self, tmp_path, monkeypatch):
        # Callers that never pass was_livestream keep today's behavior.
        calls: list[str] = []
        fetches: list[str] = []
        _stub_gemini(monkeypatch, calls=calls)
        _stub_captions(monkeypatch, CaptionsResult([(0.0, "captions")], True, "en"), fetches=fetches)
        (_, status), _, _ = _run(tmp_path, "gemini")
        assert status == "done" and fetches == [] and len(calls) == 1


# ---------------------------------------------------------------------------
# (e) mindmap-from-video is skipped, not attempted-and-guarded
# ---------------------------------------------------------------------------


class TestShouldSkipVideoMindmapForLivestream:
    def test_livestream_with_failed_transcript_blocks_video_mindmap(self):
        assert (
            vi.should_skip_video_mindmap_for_livestream(
                was_livestream=True, resolved_source="video", transcript_status="error: 400 INVALID_ARGUMENT"
            )
            is True
        )

    def test_confabulation_guard_status_also_blocks(self):
        assert (
            vi.should_skip_video_mindmap_for_livestream(
                was_livestream=True,
                resolved_source="video",
                transcript_status="error: confabulation guard (prompt=0)",
            )
            is True
        )

    def test_transcript_source_mindmap_is_never_blocked(self):
        assert (
            vi.should_skip_video_mindmap_for_livestream(
                was_livestream=True, resolved_source="transcript", transcript_status="error: boom"
            )
            is False
        )

    def test_transcript_not_attempted_falls_back_to_today_behavior(self):
        # No transcript attempt means the URI was never proven broken.
        assert (
            vi.should_skip_video_mindmap_for_livestream(
                was_livestream=True, resolved_source="video", transcript_status=None
            )
            is False
        )

    def test_successful_transcript_does_not_block(self):
        assert (
            vi.should_skip_video_mindmap_for_livestream(
                was_livestream=True, resolved_source="video", transcript_status="done"
            )
            is False
        )

    def test_regular_upload_is_never_blocked(self):
        assert (
            vi.should_skip_video_mindmap_for_livestream(
                was_livestream=False, resolved_source="video", transcript_status="error: 400 INVALID_ARGUMENT"
            )
            is False
        )


# ---------------------------------------------------------------------------
# cmd_scan integration: flag plumbing + the two routing outcomes
# ---------------------------------------------------------------------------


def _scan_args(**overrides):
    base = {"dry_run": False, "channel": None, "force": False, "since": None, "model": None}
    base.update(overrides)
    return Namespace(**base)


def _scan_setup(monkeypatch, videos, statuses, transcript_result):
    """Wire cmd_scan with stubbed YouTube + Gemini layers.

    `transcript_result` maps video_id -> status string returned by the stubbed
    process_transcript. Returns a dict of captured calls.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
    monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos))
    monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids, "PT20M"))
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: statuses.get(vid, {}) for vid in ids})
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

    captured: dict = {"transcript": [], "mindmap": []}

    def fake_process_transcript(*args, **kwargs):
        video = args[2]
        prefix = args[6]
        captured["transcript"].append((video["video_id"], kwargs.get("was_livestream")))
        return (prefix, transcript_result.get(video["video_id"], "done"))

    def fake_process_mindmap(*args, **kwargs):
        video = args[2]
        captured["mindmap"].append((video["video_id"], kwargs.get("source")))
        return (video["video_id"], "done")

    monkeypatch.setattr(vi, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(vi, "process_mindmap", fake_process_mindmap)
    return captured


class TestCmdScanLivestreamRouting:
    def _config(self, tmp_path):
        return {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"},
            ],
        }

    def test_flag_reaches_process_transcript(self, tmp_path, monkeypatch):
        videos = [
            {"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"},
            {"video_id": "reg1", "title": "Regular", "published": "2026-06-13"},
        ]
        statuses = {
            "vod1": {"live_broadcast_content": "none", "privacy_status": "public", "was_livestream": True},
            "reg1": {"live_broadcast_content": "none", "privacy_status": "public", "was_livestream": False},
        }
        captured = _scan_setup(monkeypatch, videos, statuses, {})
        vi.cmd_scan(_scan_args(), self._config(tmp_path))
        assert dict(captured["transcript"]) == {"vod1": True, "reg1": False}

    def test_failed_livestream_transcript_skips_mindmap_from_video(self, tmp_path, monkeypatch):
        videos = [{"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"}]
        statuses = {"vod1": {"live_broadcast_content": "none", "privacy_status": "public", "was_livestream": True}}
        captured = _scan_setup(monkeypatch, videos, statuses, {"vod1": "error: 400 INVALID_ARGUMENT"})
        vi.cmd_scan(_scan_args(), self._config(tmp_path))
        assert captured["mindmap"] == [], "no mindmap-from-video call may be spent on a broken livestream URI"

    def test_failed_regular_transcript_still_falls_back_to_video_mindmap(self, tmp_path, monkeypatch):
        videos = [{"video_id": "reg1", "title": "Regular", "published": "2026-06-13"}]
        statuses = {"reg1": {"live_broadcast_content": "none", "privacy_status": "public", "was_livestream": False}}
        captured = _scan_setup(monkeypatch, videos, statuses, {"reg1": "error: 400 INVALID_ARGUMENT"})
        vi.cmd_scan(_scan_args(), self._config(tmp_path))
        assert captured["mindmap"] == [("reg1", "video")], "regular-upload routing must be unchanged"

    def test_livestream_with_healthy_transcript_still_gets_a_mindmap(self, tmp_path, monkeypatch):
        # The transcript lands on disk, so the resolver picks source=transcript
        # and the block never applies.
        videos = [{"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"}]
        statuses = {"vod1": {"live_broadcast_content": "none", "privacy_status": "public", "was_livestream": True}}
        captured = _scan_setup(monkeypatch, videos, statuses, {"vod1": "done"})
        video_dir = tmp_path / "ch"
        video_dir.mkdir(parents=True, exist_ok=True)
        prefix = vi.video_file_prefix(videos[0])
        (video_dir / f"{prefix}.transcript.md").write_text("# transcript\n", encoding="utf-8")
        vi.cmd_scan(_scan_args(), self._config(tmp_path))
        assert captured["mindmap"] == [("vod1", "transcript")]
