"""Tests for the YouTube-captions failover + confabulation guard (issue #60).

Covers:
- resolve_transcript_source (pure: precedence + validation)
- _build_captions_transcript_body (pure: format, dedup, empties)
- log_usage_metadata return contract (gemini_common)
- process_transcript branches: gemini (+ confab guard), yt-captions, auto failover
"""

import json

import pytest
from youtube_captions import CaptionsResult

import video_intel as vi
from gemini_common import log_usage_metadata

# ---------------------------------------------------------------------------
# resolve_transcript_source
# ---------------------------------------------------------------------------


class TestResolveTranscriptSource:
    def test_default_is_gemini(self):
        assert vi.resolve_transcript_source({}) == "gemini"

    def test_reads_channel_knob(self):
        assert vi.resolve_transcript_source({"transcript_source": "yt-captions"}) == "yt-captions"
        assert vi.resolve_transcript_source({"transcript_source": "auto"}) == "auto"

    def test_cli_override_wins_over_channel(self):
        assert vi.resolve_transcript_source({"transcript_source": "gemini"}, "auto") == "auto"

    def test_cli_none_falls_back_to_channel(self):
        assert vi.resolve_transcript_source({"transcript_source": "auto"}, None) == "auto"

    def test_invalid_channel_value_raises(self):
        with pytest.raises(ValueError, match="transcript_source"):
            vi.resolve_transcript_source({"transcript_source": "captions"})

    def test_invalid_cli_value_raises(self):
        with pytest.raises(ValueError, match="transcript_source"):
            vi.resolve_transcript_source({}, "bogus")


# ---------------------------------------------------------------------------
# _build_captions_transcript_body
# ---------------------------------------------------------------------------


class TestBuildCaptionsBody:
    def _captions(self, snippets):
        return CaptionsResult(snippets=snippets, is_generated=True, language="en")

    def test_basic_mmss_format(self):
        body = vi._build_captions_transcript_body(self._captions([(0.0, "hello"), (65.0, "world")]))
        assert '[00:00] "hello"' in body
        assert '[01:05] "world"' in body

    def test_overlapping_cues_dedup_keeps_longest(self):
        # Rolling-window ASR repeats: same start-second, keep the longest text.
        body = vi._build_captions_transcript_body(self._captions([(5.0, "the"), (5.2, "the quick brown fox")]))
        assert body.count("[00:05]") == 1
        assert "the quick brown fox" in body

    def test_empty_and_whitespace_cues_skipped(self):
        body = vi._build_captions_transcript_body(self._captions([(0.0, "  "), (1.0, "real")]))
        assert '[00:01] "real"' in body
        assert "[00:00]" not in body


# ---------------------------------------------------------------------------
# log_usage_metadata return contract
# ---------------------------------------------------------------------------


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


class _RespNoUsage:
    usage_metadata = None


class TestLogUsageReturn:
    def test_returns_counts_dict(self):
        counts = log_usage_metadata(_Resp(5000), "test")
        assert counts is not None and counts["prompt"] == 5000

    def test_returns_zero_for_confabulation(self):
        counts = log_usage_metadata(_Resp(0), "test")
        assert counts is not None and counts["prompt"] == 0

    def test_returns_none_when_usage_missing(self):
        assert log_usage_metadata(_RespNoUsage(), "test") is None


# ---------------------------------------------------------------------------
# process_transcript: confab guard + source branches
# ---------------------------------------------------------------------------

_VALID_PAYLOAD = {
    "transcripts": [{"start": "00:00", "voice": 1, "text": "hello world"}],
    "speakers": [{"voice": 1, "name": "A"}],
    "screen_content": [],
}
_CONFAB_PAYLOAD = {"transcripts": [{"start": "00:00", "voice": 1, "text": "x"}], "speakers": [], "screen_content": []}


def _video():
    return {
        "video_id": "vid123",
        "url": "https://www.youtube.com/watch?v=vid123",
        "title": "Test",
        "published": "2026-06-13",
    }


def _stub_gemini(monkeypatch, *, prompt_tokens, payload=None, raises=None, calls=None, raw_text=None):
    def fake_call_gemini(client, types, media_uri, prompt, model, response_json=False, **kw):
        if calls is not None:
            calls.append(media_uri)
        if raises is not None:
            raise raises
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(_Resp(prompt_tokens))
        if raw_text is not None:
            return raw_text  # unparseable / non-JSON, to exercise the salvage+retry tail
        return json.dumps(payload if payload is not None else _VALID_PAYLOAD)

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)


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


class TestConfabGuard:
    def test_prompt_zero_discarded_not_complete(self, tmp_path, monkeypatch):
        _stub_gemini(monkeypatch, prompt_tokens=0, payload=_CONFAB_PAYLOAD)
        (_, status), tpath, _ = _run(tmp_path, "gemini")
        assert "confabulation" in status
        assert not tpath.exists()  # garbage transcript NEVER written

    def test_prompt_zero_records_error_on_existing_meta(self, tmp_path, monkeypatch):
        meta = tmp_path / "2026-06-13-test.meta.json"
        meta.write_text(json.dumps({"video_id": "vid123"}), encoding="utf-8")
        _stub_gemini(monkeypatch, prompt_tokens=0, payload=_CONFAB_PAYLOAD)
        _run(tmp_path, "gemini")
        assert "prompt=0" in json.loads(meta.read_text())["last_error"]

    def test_healthy_prompt_writes_complete_gemini_source(self, tmp_path, monkeypatch):
        _stub_gemini(monkeypatch, prompt_tokens=5000, payload=_VALID_PAYLOAD)
        (_, status), tpath, mpath = _run(tmp_path, "gemini")
        assert status == "done"
        assert tpath.exists()
        meta = json.loads(mpath.read_text())
        assert meta["transcript_status"] == "complete"
        assert meta["transcript_source"] == "gemini"


class TestCaptionsSource:
    def test_yt_captions_skips_gemini(self, tmp_path, monkeypatch):
        calls = []
        _stub_gemini(monkeypatch, prompt_tokens=5000, calls=calls)
        monkeypatch.setattr(
            vi, "fetch_english_captions", lambda vid: CaptionsResult([(0.0, "spoken words")], True, "en")
        )
        (_, status), tpath, mpath = _run(tmp_path, "yt-captions")
        assert "captions" in status
        assert calls == []  # Gemini was NEVER called
        assert json.loads(mpath.read_text())["transcript_source"] == "youtube_captions"
        assert "youtube-auto-captions" in tpath.read_text()  # provenance banner present

    def test_yt_captions_no_captions_errors(self, tmp_path, monkeypatch):
        _stub_gemini(monkeypatch, prompt_tokens=5000)
        monkeypatch.setattr(vi, "fetch_english_captions", lambda vid: None)
        (_, status), tpath, _ = _run(tmp_path, "yt-captions")
        assert "no captions" in status
        assert not tpath.exists()

    def test_yt_captions_clips_to_segment(self, tmp_path, monkeypatch):
        # --start/--end must clip the captions, not silently return the whole video.
        _stub_gemini(monkeypatch, prompt_tokens=5000)
        monkeypatch.setattr(
            vi,
            "fetch_english_captions",
            lambda vid: CaptionsResult([(5.0, "before"), (15.0, "inside"), (25.0, "after")], True, "en"),
        )
        (_, status), tpath, _ = _run(tmp_path, "yt-captions", start_offset=10, end_offset=20)
        assert "captions" in status
        text = tpath.read_text()
        assert "inside" in text
        assert "before" not in text and "after" not in text


class TestAutoFailover:
    def test_auto_falls_back_on_gemini_exception(self, tmp_path, monkeypatch):
        _stub_gemini(monkeypatch, prompt_tokens=5000, raises=RuntimeError("400 INVALID_ARGUMENT"))
        monkeypatch.setattr(
            vi, "fetch_english_captions", lambda vid: CaptionsResult([(0.0, "fallback text")], True, "en")
        )
        (_, status), _, mpath = _run(tmp_path, "auto")
        assert "captions" in status
        meta = json.loads(mpath.read_text())
        assert meta["transcript_source"] == "youtube_captions"
        assert "gemini error" in meta["transcript_failover_reason"]

    def test_auto_falls_back_on_confabulation(self, tmp_path, monkeypatch):
        _stub_gemini(monkeypatch, prompt_tokens=0, payload=_CONFAB_PAYLOAD)
        monkeypatch.setattr(
            vi, "fetch_english_captions", lambda vid: CaptionsResult([(0.0, "real captions")], True, "en")
        )
        (_, status), _, mpath = _run(tmp_path, "auto")
        assert "captions" in status
        assert "confabulation" in json.loads(mpath.read_text())["transcript_failover_reason"]

    def test_auto_no_captions_falls_through_to_gemini_error(self, tmp_path, monkeypatch):
        _stub_gemini(monkeypatch, prompt_tokens=5000, raises=RuntimeError("boom"))
        monkeypatch.setattr(vi, "fetch_english_captions", lambda vid: None)
        (_, status), tpath, _ = _run(tmp_path, "auto")
        assert status.startswith("error")
        assert not tpath.exists()

    def test_auto_falls_back_on_parse_exhaustion(self, tmp_path, monkeypatch):
        # Unparseable non-JSON (prompt>0 so the confab guard does NOT fire); after
        # the bounded retries are exhausted, auto falls back to captions.
        _stub_gemini(monkeypatch, prompt_tokens=5000, raw_text="this is not json at all {{{")
        monkeypatch.setattr(
            vi, "fetch_english_captions", lambda vid: CaptionsResult([(0.0, "salvaged via captions")], True, "en")
        )
        (_, status), _, mpath = _run(tmp_path, "auto")
        assert "captions" in status
        assert "parse failure" in json.loads(mpath.read_text())["transcript_failover_reason"]
