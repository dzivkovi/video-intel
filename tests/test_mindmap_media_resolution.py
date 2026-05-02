"""Tests for process_mindmap's media_resolution parameter and LOW default.

Locks in the contract that mindmap-from-video defaults to MEDIA_RESOLUTION_LOW,
mirroring the chunked-transcript path's pattern. Without this default, hour-long
videos hit Gemini's 1M-token input cap (verified empirically: 91-min 1080p video
at HIGH = ~1.4M tokens). The default change applies the issue #58 Gate 3 finding
(LOW = same quality at 3x cheaper for our prompt's needs) to the mindmap path.

The transcript source path is unaffected — it uses call_gemini_text and never
sets media_resolution.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel
from video_intel import _resolve_media_resolution, process_mindmap, process_transcript


@pytest.fixture
def sample_video():
    return {
        "video_id": "ZZZ999",
        "url": "https://youtu.be/ZZZ999",
        "title": "Sample title",
        "published": "2026-05-02",
    }


@pytest.fixture
def fake_types():
    """Stub for the genai types module — only needs MediaResolution enum members."""
    media_resolution = SimpleNamespace(
        MEDIA_RESOLUTION_LOW="MEDIA_RESOLUTION_LOW",
        MEDIA_RESOLUTION_HIGH="MEDIA_RESOLUTION_HIGH",
    )
    return SimpleNamespace(MediaResolution=media_resolution)


class TestProcessMindmapFromVideoMediaResolution:
    """Verifies the LOW default and the HIGH override on the source='video' path."""

    def test_default_passes_low_to_call_gemini(self, sample_video, fake_types, tmp_path, monkeypatch):
        """No explicit media_resolution → call_gemini receives MEDIA_RESOLUTION_LOW."""
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["kw"] = kw
            return "## Topic\n\n* bullet (0:00)\n"

        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="MINDMAP-PROMPT",
            model="stub-model",
            output_dir=tmp_path,
            channel_name="demo",
            source="video",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
        )

        assert "media_resolution" in captured["kw"], (
            "process_mindmap must pass media_resolution to call_gemini on the video path"
        )
        assert captured["kw"]["media_resolution"] == "MEDIA_RESOLUTION_LOW", (
            f"Expected MEDIA_RESOLUTION_LOW default, got {captured['kw']['media_resolution']!r}. "
            "This regression would re-introduce the 1M-token ceiling on hour-long videos "
            "(see issue #58 Gate 3 and the chunked-transcript precedent at scripts/video_intel.py:1466)."
        )

    def test_explicit_high_override_threads_through(self, sample_video, fake_types, tmp_path, monkeypatch):
        """Explicit media_resolution=HIGH → call_gemini receives MEDIA_RESOLUTION_HIGH."""
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["kw"] = kw
            return "## Topic\n\n* bullet (0:00)\n"

        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="MINDMAP-PROMPT",
            model="stub-model",
            output_dir=tmp_path,
            channel_name="demo",
            source="video",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
            media_resolution=fake_types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        )

        assert captured["kw"]["media_resolution"] == "MEDIA_RESOLUTION_HIGH", (
            "Explicit HIGH override must reach call_gemini unchanged."
        )

    def test_transcript_source_unaffected_by_media_resolution_default(
        self, sample_video, fake_types, tmp_path, monkeypatch
    ):
        """source='transcript' must NOT trigger call_gemini and must NOT set media_resolution.

        The transcript path uses call_gemini_text (text-only); media_resolution is meaningless
        there. This test guards against accidentally leaking the new default into the text path.
        """
        # Drop a transcript so the transcript-source branch can read it
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir()
        prefix = "2026-05-02-sample-title"
        (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] Hi\n", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(json.dumps({"transcript_status": "ok"}), encoding="utf-8")

        text_call_kw = {}

        def fake_call_gemini_text(client, types, content, model, **kw):
            text_call_kw["kw"] = kw
            return "## Topic\n\n* bullet (0:00)\n"

        def fake_call_gemini(*args, **kwargs):
            pytest.fail("call_gemini must NOT be invoked on source='transcript' path")

        monkeypatch.setattr(video_intel, "call_gemini_text", fake_call_gemini_text)
        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="MINDMAP-FROM-TRANSCRIPT-PROMPT",
            model="stub-model",
            output_dir=tmp_path,
            channel_name="demo",
            source="transcript",
            prefix=prefix,
        )

        assert "media_resolution" not in text_call_kw["kw"], (
            "media_resolution must not leak into call_gemini_text on the transcript path."
        )

    def test_transcript_source_with_explicit_media_resolution_value_still_ignores_it(
        self, sample_video, fake_types, tmp_path, monkeypatch
    ):
        """Even an explicit media_resolution kwarg must be ignored on source='transcript'."""
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir()
        prefix = "2026-05-02-sample-title"
        (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] Hi\n", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(json.dumps({"transcript_status": "ok"}), encoding="utf-8")

        text_call_kw = {}

        def fake_call_gemini_text(client, types, content, model, **kw):
            text_call_kw["kw"] = kw
            return "## Topic\n\n* bullet (0:00)\n"

        monkeypatch.setattr(video_intel, "call_gemini_text", fake_call_gemini_text)

        process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="MINDMAP-FROM-TRANSCRIPT-PROMPT",
            model="stub-model",
            output_dir=tmp_path,
            channel_name="demo",
            source="transcript",
            prefix=prefix,
            media_resolution=fake_types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        )

        assert "media_resolution" not in text_call_kw["kw"], (
            "Explicit media_resolution must still be ignored on transcript path (call_gemini_text doesn't accept it)."
        )


class TestProcessTranscriptMediaResolution:
    """Single-shot transcript path must default to LOW (parity with mindmap fix).

    Same root cause: HIGH (~258 tokens/frame) hits Gemini's 1M-token cap on
    hour-long videos. Issue #58 Gate 3 finding (LOW = same quality at 3x
    cheaper for talking-head + slide content) extends from chunked-transcript
    to single-shot transcript.
    """

    def test_default_passes_low_to_call_gemini(self, sample_video, fake_types, tmp_path, monkeypatch):
        """process_transcript with no explicit media_resolution → call_gemini receives LOW."""
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["kw"] = kw
            # Return malformed JSON so transcript path takes the early error return,
            # avoiding the rest of the parse/salvage flow we don't care about here.
            raise RuntimeError("stub error to exit early")

        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        _, status = process_transcript(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="TRANSCRIPT-PROMPT",
            model="stub-model",
            channel_dir=tmp_path / "demo",
            prefix="2026-05-02-sample",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
        )
        # We expect error status from the stub raise, but the kw must have been captured first.
        assert status.startswith("error"), f"expected error status, got {status!r}"
        assert "media_resolution" in captured["kw"], (
            "process_transcript must pass media_resolution to call_gemini on the single-shot path"
        )
        assert captured["kw"]["media_resolution"] == "MEDIA_RESOLUTION_LOW", (
            f"Expected MEDIA_RESOLUTION_LOW default, got {captured['kw']['media_resolution']!r}. "
            "This regression would re-introduce the 1M-token ceiling on hour-long videos."
        )

    def test_explicit_high_override_threads_through(self, sample_video, fake_types, tmp_path, monkeypatch):
        """process_transcript with explicit media_resolution=HIGH → call_gemini receives HIGH."""
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["kw"] = kw
            raise RuntimeError("stub error to exit early")

        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        process_transcript(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="TRANSCRIPT-PROMPT",
            model="stub-model",
            channel_dir=tmp_path / "demo",
            prefix="2026-05-02-sample",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
            media_resolution=fake_types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        )

        assert captured["kw"]["media_resolution"] == "MEDIA_RESOLUTION_HIGH"

    def test_thinking_config_is_capped(self, sample_video, fake_types, tmp_path, monkeypatch):
        """Single-shot transcript MUST cap thinking budget like the chunked path does.

        Without this cap, Gemini 2.5 Pro stochastically burns all output tokens
        on internal thinking and returns candidates=0 — observed empirically on
        a 91-min video (thoughts=65533, candidates=0). Mirrors the chunked path's
        line 1493 mitigation. Same justification as issue #58 Gate 2.
        """
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["kw"] = kw
            raise RuntimeError("stub error to exit early")

        # Stub the helper so we can assert the result is forwarded without depending
        # on real types.ThinkingConfig construction.
        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)
        monkeypatch.setattr(
            video_intel,
            "_make_thinking_config_for_transcript",
            lambda types, model: "STUB_THINKING_CONFIG",
        )

        process_transcript(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="TRANSCRIPT-PROMPT",
            model="gemini-2.5-pro",  # the model where this matters most
            channel_dir=tmp_path / "demo",
            prefix="2026-05-02-sample",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
        )

        assert "thinking_config" in captured["kw"], (
            "process_transcript must pass thinking_config to call_gemini "
            "(removing this cap re-introduces the candidates=0 / thoughts-overflow bug)"
        )
        assert captured["kw"]["thinking_config"] == "STUB_THINKING_CONFIG"


class TestTryParseTranscriptJsonNoneDefense:
    """try_parse_transcript_json must not crash on None / empty input.

    Regression: when Gemini's thinking budget overflows, the call returns
    candidates=0 and text=None. Without defensive handling, json.loads(None)
    raises TypeError and the script crashes mid-pipeline instead of routing
    to the salvage path.
    """

    def test_none_input_returns_parse_error_not_typeerror(self):
        from video_intel import try_parse_transcript_json

        parsed, err = try_parse_transcript_json(None)
        assert parsed is None
        assert err is not None
        assert "Empty response" in err or "thinking-budget" in err

    def test_empty_string_input_returns_parse_error(self):
        from video_intel import try_parse_transcript_json

        parsed, err = try_parse_transcript_json("")
        assert parsed is None
        assert err is not None


class TestResolveMediaResolutionHelper:
    """The string -> enum mapping at the CLI boundary."""

    def test_low_maps_to_media_resolution_low(self, fake_types):
        assert _resolve_media_resolution(fake_types, "low") == "MEDIA_RESOLUTION_LOW"

    def test_high_maps_to_media_resolution_high(self, fake_types):
        assert _resolve_media_resolution(fake_types, "high") == "MEDIA_RESOLUTION_HIGH"

    def test_invalid_raises(self, fake_types):
        with pytest.raises(ValueError) as exc_info:
            _resolve_media_resolution(fake_types, "medium")
        assert "medium" in str(exc_info.value)


class TestCmdMindmapMediaResolutionThreading:
    """Integration: argparse args -> cmd_mindmap -> process_mindmap kwarg.

    Asserts that the CLI flag value reaches the helper that ultimately calls
    Gemini, both as the implicit "low" default and as an explicit "high" override.
    """

    def _make_namespace(self, **overrides):
        defaults = dict(
            url=None,
            file=None,
            channel=None,
            video_id=None,
            title=None,
            date=None,
            force=False,
            model=None,
            prompt=None,
            media_resolution="low",
        )
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_default_low_threads_through_cmd_mindmap_to_process_mindmap(self, tmp_path, fake_types, monkeypatch):
        """args.media_resolution='low' -> process_mindmap receives MEDIA_RESOLUTION_LOW."""
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "video.mp4"
        mp4.write_bytes(b"fake mp4 bytes")

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), fake_types))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setattr("video_intel.resolve_model", lambda _args, _cfg: "stub-model")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: f"prompt-{_name}")
        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/uploaded")

        captured = {}

        def fake_process_mindmap(*args, **kwargs):
            captured["kwargs"] = kwargs
            return "video", "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_process_mindmap)

        from video_intel import cmd_mindmap

        config = {"channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc"}]}
        cmd_mindmap(self._make_namespace(file=str(mp4), channel="everyinc"), config)

        assert "media_resolution" in captured["kwargs"]
        assert captured["kwargs"]["media_resolution"] == "MEDIA_RESOLUTION_LOW"

    def test_high_override_threads_through_cmd_mindmap_to_process_mindmap(self, tmp_path, fake_types, monkeypatch):
        """args.media_resolution='high' -> process_mindmap receives MEDIA_RESOLUTION_HIGH."""
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "video.mp4"
        mp4.write_bytes(b"fake mp4 bytes")

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), fake_types))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setattr("video_intel.resolve_model", lambda _args, _cfg: "stub-model")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: f"prompt-{_name}")
        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/uploaded")

        captured = {}

        def fake_process_mindmap(*args, **kwargs):
            captured["kwargs"] = kwargs
            return "video", "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_process_mindmap)

        from video_intel import cmd_mindmap

        config = {"channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc"}]}
        cmd_mindmap(
            self._make_namespace(file=str(mp4), channel="everyinc", media_resolution="high"),
            config,
        )

        assert captured["kwargs"]["media_resolution"] == "MEDIA_RESOLUTION_HIGH"
