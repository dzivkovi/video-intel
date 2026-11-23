"""The manual --url paths persist the duration they already look up (issue #275).

`transcript --url`, `mindmap --url` (video branch) and `process --url` call
`_lookup_video_duration_seconds` for chunking and the frame cap, then used to
drop the value: only the scan's mindmap writer persisted `duration_seconds`.
The fix puts the looked-up value on the video dict, where the existing writer
seam (`_transcript_identity_fields`, `process_mindmap`) carries it to disk,
exactly like the YouTube description in issue #224.

The command cases drive the REAL commands and the REAL writers, with only the
YouTube duration lookup and the Gemini calls stubbed, and read meta.json back
off disk [core: real-caller].
"""

from __future__ import annotations

import json
import sys
import types as _types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi

VIDEO_ID = "abcdefghijk"
CHANNEL = "chan"
PREFIX = "2026-08-12-a-talk"

SHORT_DURATION = 600
LONG_DURATION = 4000  # above the default 30-minute chunk threshold

_TRANSCRIPT_PAYLOAD = {
    "transcripts": [{"start": "00:10", "voice": 1, "text": "hi"}],
    "screen_content": [],
    "speakers": [{"voice": 1, "name": "A"}],
}
_MINDMAP = "## Theme\n\n* **Sub**\n  - point (0:10)"


def _usage(prompt_tokens: int = 5000):
    return _types.SimpleNamespace(
        usage_metadata=_types.SimpleNamespace(
            prompt_token_count=prompt_tokens,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=10,
            total_token_count=prompt_tokens + 10,
        )
    )


def _args(**overrides):
    base = {
        "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
        "file": None,
        # --channel/--title/--date: no snippet lookup, deterministic prefix.
        "channel": CHANNEL,
        "title": "A Talk",
        "date": "2026-08-12",
        "start": None,
        "end": None,
        "force": False,
        "prompt": None,
        "model": None,
        "video_id": None,
        "media_resolution": "low",
        "chunk_minutes": None,
        "transcript_source": "gemini",
        "topic": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub only the paid boundaries; every writer runs for real."""

    def _wire(duration: int | None):
        lookups: list[str] = []

        def fake_lookup(video_id):
            lookups.append(video_id)
            return duration

        def fake_call_gemini(client, types, media_uri, prompt, model, response_json=False, **kw):
            cb = kw.get("on_response")
            if cb:
                cb(_usage())
            return json.dumps(_TRANSCRIPT_PAYLOAD) if response_json else _MINDMAP

        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", fake_lookup)
        monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
        monkeypatch.setattr(vi, "call_gemini_text", lambda *a, **k: _MINDMAP)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda t, m: None)
        monkeypatch.setattr(vi, "process_concepts", lambda *a, **k: "done (0 concepts)")
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _v: False)
        monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
        monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "require_channels_config", lambda _c: None)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
        return lookups

    return _wire


def _run(command: str, args) -> int:
    """Run a real command; return its exit code (0 when it did not exit).
    Anything that is not a SystemExit propagates and fails the test."""
    try:
        getattr(vi, command)(args, {"channels": []})
    except SystemExit as exc:
        return exc.code if exc.code is not None else 0
    return 0


def _meta(tmp_path: Path) -> dict:
    return json.loads((tmp_path / CHANNEL / f"{PREFIX}.meta.json").read_text(encoding="utf-8"))


class TestEveryLookupSitePersistsTheDuration:
    def test_transcript_url_when_short_persists_looked_up_duration(self, wired, tmp_path):
        # Arrange
        lookups = wired(SHORT_DURATION)
        # Act
        code = _run("cmd_transcript", _args())
        # Assert
        assert code in (0, vi.EXIT_PARTIAL)
        assert lookups == [VIDEO_ID], "the single-shot case must go through the lookup"
        assert _meta(tmp_path)["duration_seconds"] == SHORT_DURATION

    def test_transcript_url_when_chunked_persists_looked_up_duration(self, wired, tmp_path):
        # Arrange
        wired(LONG_DURATION)
        # Act
        code = _run("cmd_transcript", _args())
        # Assert
        assert code in (0, vi.EXIT_PARTIAL)
        meta = _meta(tmp_path)
        assert meta.get("transcript_chunks", 0) > 1, "the chunked writer must be the one under test"
        assert meta["duration_seconds"] == LONG_DURATION

    def test_process_url_persists_looked_up_duration(self, wired, tmp_path):
        # Arrange
        wired(SHORT_DURATION)
        # Act
        code = _run("cmd_process", _args())
        # Assert
        assert code in (0, vi.EXIT_PARTIAL)
        assert _meta(tmp_path)["duration_seconds"] == SHORT_DURATION

    def test_mindmap_url_video_branch_persists_looked_up_duration(self, wired, tmp_path):
        # Arrange: no transcript on disk, so mindmap_source=auto picks the video branch.
        lookups = wired(SHORT_DURATION)
        # Act
        code = _run("cmd_mindmap", _args())
        # Assert
        assert code == 0
        assert lookups == [VIDEO_ID], "the video branch must be the one under test"
        assert (tmp_path / CHANNEL / f"{PREFIX}.mindmap.md").exists()
        assert _meta(tmp_path)["duration_seconds"] == SHORT_DURATION


class TestAnUnknownDurationNeverClobbers:
    @pytest.mark.parametrize("command", ["cmd_transcript", "cmd_process", "cmd_mindmap"])
    def test_lookup_none_when_meta_has_duration_keeps_it(self, command, wired, tmp_path):
        # Arrange
        wired(None)
        chan = tmp_path / CHANNEL
        chan.mkdir()
        (chan / f"{PREFIX}.meta.json").write_text(
            json.dumps({"video_id": VIDEO_ID, "duration_seconds": 777}), encoding="utf-8"
        )
        # Act
        code = _run(command, _args())
        # Assert
        assert code in (0, vi.EXIT_PARTIAL)
        assert _meta(tmp_path)["duration_seconds"] == 777


class TestScanVideoDictStillPersistsFromIso:
    def test_process_mindmap_when_only_duration_iso_persists_seconds(self, tmp_path, monkeypatch):
        """Scan regression: the scan's video dict carries `duration_iso`, never
        `duration_seconds`. The real writer must still persist it."""
        # Arrange
        monkeypatch.setattr(vi, "call_gemini_text", lambda *a, **k: _MINDMAP)
        chan = tmp_path / CHANNEL
        chan.mkdir()
        transcript = chan / f"{PREFIX}.transcript.md"
        transcript.write_text("[00:10] A: hi", encoding="utf-8")
        video = {
            "video_id": VIDEO_ID,
            "url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
            "title": "A Talk",
            "published": "2026-08-12",
            "duration_iso": "PT10M",
        }
        # Act
        vi.process_mindmap(
            MagicMock(),
            MagicMock(),
            video,
            "PROMPT",
            "stub-model",
            tmp_path,
            CHANNEL,
            prefix=PREFIX,
            source="transcript",
            transcript_path=transcript,
        )
        # Assert
        assert _meta(tmp_path)["duration_seconds"] == 600


class TestVideoDurationSecondsHelper:
    def test_int_when_both_present_wins_over_iso(self):
        assert vi._video_duration_seconds({"duration_seconds": 42, "duration_iso": "PT10M"}) == 42

    def test_iso_when_int_absent_is_parsed(self):
        assert vi._video_duration_seconds({"duration_iso": "PT1H2M3S"}) == 3723

    @pytest.mark.parametrize("bad", [True, False, 0, -5, "600", 600.0, None])
    def test_unusable_int_when_iso_present_falls_through_to_iso(self, bad):
        assert vi._video_duration_seconds({"duration_seconds": bad, "duration_iso": "PT10M"}) == 600

    @pytest.mark.parametrize("bad", [True, 0, -5, "600", None])
    def test_unusable_int_when_iso_absent_returns_none(self, bad):
        assert vi._video_duration_seconds({"duration_seconds": bad}) is None

    @pytest.mark.parametrize("iso", ["PT0S", "garbage", "", None])
    def test_unusable_iso_when_int_absent_returns_none(self, iso):
        assert vi._video_duration_seconds({"duration_iso": iso}) is None

    def test_both_absent_returns_none(self):
        assert vi._video_duration_seconds({}) is None


class TestTheSeamCarriesItAndDropsUnknown:
    def test_identity_fields_when_duration_known_include_it(self, tmp_path):
        fields = vi._transcript_identity_fields({"video_id": "v", "duration_seconds": 600}, tmp_path / CHANNEL)
        assert fields["duration_seconds"] == 600

    def test_identity_fields_when_duration_unknown_drop_it(self, tmp_path):
        fields = vi._transcript_identity_fields({"video_id": "v"}, tmp_path / CHANNEL)
        assert "duration_seconds" not in fields
