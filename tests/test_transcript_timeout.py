"""Tests for the per-transcript wall-clock timeout (issue #74).

Covers:
- _run_with_timeout: returns on fast fn, raises TranscriptTimeout on a hang
  (promptly, without waiting out the hung call), re-raises the fn's own error,
  and is disabled when the budget is <= 0 / None.
- process_transcript: a hung Gemini call becomes a clean error under 'gemini'
  (no deadlock, no transcript written) and a captions rescue under 'auto'.
"""

import json
import time

import pytest
from youtube_captions import CaptionsResult

import video_intel as vi

_VALID_PAYLOAD = {
    "transcripts": [{"timestamp": "00:00", "speaker": 1, "text": "hello"}],
    "screen_content": [],
    "speakers": [{"voice": 1, "name": "A"}],
}


def _video():
    return {
        "video_id": "vid123",
        "url": "https://www.youtube.com/watch?v=vid123",
        "title": "Test",
        "published": "2026-06-13",
    }


# ---------------------------------------------------------------------------
# _run_with_timeout (unit)
# ---------------------------------------------------------------------------


class TestRunWithTimeout:
    def test_returns_result_when_fast(self):
        assert vi._run_with_timeout(lambda: 42, 5) == 42

    def test_raises_promptly_on_hang(self):
        t0 = time.monotonic()
        with pytest.raises(vi.TranscriptTimeout):
            vi._run_with_timeout(lambda: time.sleep(3), 0.3)
        # Returned at ~0.3s, did NOT block for the full 3s hang.
        assert time.monotonic() - t0 < 2

    def test_reraises_fn_error(self):
        def _boom():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            vi._run_with_timeout(_boom, 5)

    def test_disabled_when_zero_or_none_runs_inline(self):
        assert vi._run_with_timeout(lambda: "x", 0) == "x"
        assert vi._run_with_timeout(lambda: "y", None) == "y"


# ---------------------------------------------------------------------------
# process_transcript integration: hang -> failover
# ---------------------------------------------------------------------------


def _stub_hanging_gemini(monkeypatch, sleep_seconds=3):
    def fake_call_gemini(client, types, media_uri, prompt, model, response_json=False, **kw):
        time.sleep(sleep_seconds)  # simulate a hung Gemini call
        return json.dumps(_VALID_PAYLOAD)

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


class TestProcessTranscriptTimeout:
    def test_hang_under_gemini_is_clean_error_no_deadlock(self, tmp_path, monkeypatch):
        _stub_hanging_gemini(monkeypatch)
        t0 = time.monotonic()
        (_, status), tpath, _ = _run(tmp_path, "gemini", transcript_timeout_seconds=0.3)
        assert time.monotonic() - t0 < 3  # did not deadlock on the 3s hang
        assert "error" in status
        assert not tpath.exists()  # no transcript written for a hung call

    def test_hang_under_auto_falls_back_to_captions(self, tmp_path, monkeypatch):
        _stub_hanging_gemini(monkeypatch)
        monkeypatch.setattr(
            vi,
            "fetch_english_captions",
            lambda vid: CaptionsResult([(0.0, "rescued by captions")], True, "en"),
        )
        (_, status), tpath, mpath = _run(tmp_path, "auto", transcript_timeout_seconds=0.3)
        assert "captions" in status
        assert tpath.exists()
        assert json.loads(mpath.read_text())["transcript_source"] == "youtube_captions"
