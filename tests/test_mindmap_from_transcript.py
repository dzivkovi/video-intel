"""Tests for issue #54 - mindmap built from on-disk transcript text.

Three concerns:
1. resolve_mindmap_source() resolver - pure function, encodes the issue's
   user-confirmed edge cases (auto/transcript/video/none + transcript-availability).
2. process_mindmap(source="transcript", ...) path - reads on-disk transcript,
   calls call_gemini_text, writes the same artifact shape as the video path,
   inherits transcript_status into mindmap_source_status when partial.
3. call_gemini_text optional response_mime_type - lets the new mindmap path
   request text/plain while the existing concepts caller keeps application/json.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import video_intel
from video_intel import (
    call_gemini_text,
    process_mindmap,
    resolve_mindmap_source,
)

# ---------------------------------------------------------------------------
# resolve_mindmap_source - pure function
# ---------------------------------------------------------------------------


class TestResolveMindmapSource:
    def test_default_auto_uses_transcript_when_available(self):
        assert resolve_mindmap_source({}, transcript_available=True) == "transcript"

    def test_default_auto_falls_back_to_video_when_no_transcript(self):
        assert resolve_mindmap_source({}, transcript_available=False) == "video"

    def test_explicit_auto_matches_default(self):
        assert resolve_mindmap_source({"mindmap_source": "auto"}, transcript_available=True) == "transcript"
        assert resolve_mindmap_source({"mindmap_source": "auto"}, transcript_available=False) == "video"

    def test_explicit_video_keeps_video_even_when_transcript_available(self):
        assert resolve_mindmap_source({"mindmap_source": "video"}, transcript_available=True) == "video"

    def test_explicit_transcript_with_transcript_returns_transcript(self):
        assert resolve_mindmap_source({"mindmap_source": "transcript"}, transcript_available=True) == "transcript"

    def test_explicit_transcript_without_transcript_raises(self):
        with pytest.raises(ValueError) as exc_info:
            resolve_mindmap_source({"mindmap_source": "transcript"}, transcript_available=False)
        # Actionable message names both knobs the user might tweak.
        msg = str(exc_info.value)
        assert "mindmap_source" in msg
        assert "transcript" in msg

    def test_none_returns_skip_regardless_of_transcript(self):
        assert resolve_mindmap_source({"mindmap_source": "none"}, transcript_available=True) == "skip"
        assert resolve_mindmap_source({"mindmap_source": "none"}, transcript_available=False) == "skip"

    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            resolve_mindmap_source({"mindmap_source": "magic"}, transcript_available=True)


# ---------------------------------------------------------------------------
# process_mindmap with source="transcript"
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_video():
    return {
        "video_id": "ABC123",
        "url": "https://youtu.be/ABC123",
        "title": "Sample title",
        "published": "2026-04-27",
    }


def _write_transcript(
    channel_dir,
    prefix,
    text="[00:00] Speaker: hi\n",
    transcript_status="ok",
):
    """Drop a transcript artifact + meta with the given transcript_status.

    Defaults to ``"ok"``, but real production writers also produce ``"complete"``
    (single-shot success) and ``"partial"`` (salvage). Tests parametrize over
    these to lock the issue #54 / review C1 fix.
    """
    channel_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = channel_dir / f"{prefix}.transcript.md"
    transcript_path.write_text(text, encoding="utf-8")
    meta_path = channel_dir / f"{prefix}.meta.json"
    if not meta_path.exists():
        meta_path.write_text(
            json.dumps(
                {
                    "video_id": "ABC123",
                    "video_url": "https://youtu.be/ABC123",
                    "title": "Sample title",
                    "published": "2026-04-27",
                    "channel": "demo",
                    "modes_completed": ["transcript"],
                    "transcript_status": transcript_status,
                }
            ),
            encoding="utf-8",
        )
    return transcript_path, meta_path


class TestProcessMindmapFromTranscript:
    def test_writes_expected_mindmap_artifacts(self, sample_video, tmp_path, monkeypatch):
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "demo"
        prefix = "2026-04-27-sample-title"
        _write_transcript(channel_dir, prefix)

        captured = {}

        def fake_call_gemini_text(client, types, text_content, model, **kw):
            captured["text_content"] = text_content
            captured["model"] = model
            captured["kw"] = kw
            return "## Topic\n\n* **Sub**\n  - bullet (0:00)\n"

        monkeypatch.setattr(video_intel, "call_gemini_text", fake_call_gemini_text)

        prefix_out, status = process_mindmap(
            client=MagicMock(),
            types=MagicMock(),
            video=sample_video,
            prompt_text="MINDMAP-FROM-TRANSCRIPT-PROMPT",
            model="stub-model",
            output_dir=output_dir,
            channel_name="demo",
            prompt_name="mindmap-from-transcript",
            source="transcript",
            prefix=prefix,
        )

        assert status == "done", f"expected 'done', got {status!r}"
        assert prefix_out == prefix

        mindmap_path = channel_dir / f"{prefix}.mindmap.md"
        assert mindmap_path.exists()
        body = mindmap_path.read_text(encoding="utf-8")
        # Same canonical header as video path
        assert "<!-- video: https://youtu.be/ABC123 -->" in body
        assert "<!-- title: Sample title -->" in body
        assert "## Topic" in body

        meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert meta["prompt"] == "mindmap-from-transcript"
        assert meta["mindmap_source"] == "transcript"
        # Transcript was "ok", so no partial marker
        assert "mindmap_source_status" not in meta or meta["mindmap_source_status"] != "partial"

        # The Gemini text call carried both the prompt and the transcript text
        assert "MINDMAP-FROM-TRANSCRIPT-PROMPT" in captured["text_content"]
        assert "[00:00] Speaker: hi" in captured["text_content"]

    @pytest.mark.parametrize(
        "transcript_status,expect_partial",
        [
            ("ok", False),  # chunked-merge + scan single-shot writer
            ("complete", False),  # single-call success writer (C1: don't misclassify)
            ("partial", True),  # salvage writer
        ],
    )
    def test_transcript_status_inheritance(
        self,
        sample_video,
        tmp_path,
        monkeypatch,
        transcript_status,
        expect_partial,
    ):
        """Partial-source detection must accept BOTH 'ok' and 'complete' as
        healthy. Review finding C1: my first cut treated only 'ok' as healthy,
        so every successful single-shot transcript (which writes 'complete')
        was getting stamped as a partial mindmap source. Lock the contract."""
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "demo"
        prefix = f"2026-04-27-{transcript_status}-talk"
        _, meta_path = _write_transcript(
            channel_dir,
            prefix,
            transcript_status=transcript_status,
        )

        monkeypatch.setattr(
            video_intel,
            "call_gemini_text",
            lambda *a, **kw: "## Topic\n\n* **Sub**\n  - bullet (0:00)\n",
        )

        process_mindmap(
            client=MagicMock(),
            types=MagicMock(),
            video=sample_video,
            prompt_text="P",
            model="m",
            output_dir=output_dir,
            channel_name="demo",
            prompt_name="mindmap-from-transcript",
            source="transcript",
            prefix=prefix,
        )

        mindmap_body = (channel_dir / f"{prefix}.mindmap.md").read_text(encoding="utf-8")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if expect_partial:
            assert "<!-- source: partial transcript" in mindmap_body
            assert meta.get("mindmap_source_status") == "partial"
        else:
            assert "<!-- source: partial transcript" not in mindmap_body
            assert meta.get("mindmap_source_status") != "partial"

    def test_skips_when_artifact_exists_without_force(self, sample_video, tmp_path, monkeypatch):
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "demo"
        prefix = "2026-04-27-already-done"
        _write_transcript(channel_dir, prefix)
        (channel_dir / f"{prefix}.mindmap.md").write_text("<!-- video: x -->\n## existing\n", encoding="utf-8")

        gemini_calls = []
        monkeypatch.setattr(
            video_intel,
            "call_gemini_text",
            lambda *a, **kw: gemini_calls.append((a, kw)) or "## new\n",
        )

        _, status = process_mindmap(
            client=MagicMock(),
            types=MagicMock(),
            video=sample_video,
            prompt_text="P",
            model="m",
            output_dir=output_dir,
            channel_name="demo",
            prompt_name="mindmap-from-transcript",
            source="transcript",
            prefix=prefix,
        )
        assert status.startswith("skipped")
        assert gemini_calls == [], "no Gemini call should fire when mindmap exists"

    def test_missing_transcript_returns_error_status_when_source_transcript(self, sample_video, tmp_path, monkeypatch):
        """source='transcript' with no on-disk transcript returns an error
        status (FileNotFoundError is caught inside process_mindmap and the
        outer try/except records 'error: ...'). The resolver guards the
        auto/explicit ambiguity upstream; this is the lower-level safety net."""
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "demo"
        prefix = "2026-04-27-no-transcript"
        channel_dir.mkdir(parents=True, exist_ok=True)
        # Note: NO transcript written.

        monkeypatch.setattr(
            video_intel,
            "call_gemini_text",
            lambda *a, **kw: pytest.fail("Gemini should not be called"),
        )

        _, status = process_mindmap(
            client=MagicMock(),
            types=MagicMock(),
            video=sample_video,
            prompt_text="P",
            model="m",
            output_dir=output_dir,
            channel_name="demo",
            prompt_name="mindmap-from-transcript",
            source="transcript",
            prefix=prefix,
        )
        assert status.startswith("error"), f"expected error status, got {status!r}"
        assert "transcript" in status.lower()


# ---------------------------------------------------------------------------
# call_gemini_text response_mime_type
# ---------------------------------------------------------------------------


class TestCallGeminiTextResponseMime:
    def test_default_mime_is_application_json_for_back_compat(self):
        types = MagicMock()
        # GenerateContentConfig is a MagicMock that records its kwargs.
        recorded = {}

        def fake_config(**kw):
            recorded.update(kw)
            return MagicMock()

        types.GenerateContentConfig.side_effect = fake_config

        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text="{}")

        call_gemini_text(client, types, "hello", "stub-model")

        assert recorded.get("response_mime_type") == "application/json"

    def test_explicit_text_plain_overrides_default(self):
        types = MagicMock()
        recorded = {}

        def fake_config(**kw):
            recorded.update(kw)
            return MagicMock()

        types.GenerateContentConfig.side_effect = fake_config

        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text="# md\n")

        call_gemini_text(
            client,
            types,
            "hello",
            "stub-model",
            response_mime_type="text/plain",
        )

        assert recorded.get("response_mime_type") == "text/plain"


# ---------------------------------------------------------------------------
# _cmd_process_url ordering inversion (issue #54)
# ---------------------------------------------------------------------------


def _process_url_args(url, **overrides):
    """Mirrors test_chunked_transcript._process_url_args but kept local."""
    from argparse import Namespace

    base = {
        "url": url,
        "file": None,
        "channel": "demo",
        "video_id": None,
        "title": "Test Video",
        "date": "2026-04-15",
        "start": None,
        "end": None,
        "force": False,
        "model": None,
        "prompt": None,
        "chunk_minutes": 50,
    }
    base.update(overrides)
    return Namespace(**base)


def _stub_url_env(monkeypatch, tmp_path, *, duration=1800):
    """Stub external dependencies for cmd_process --url tests."""
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "create_client", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: tmp_path)
    monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(video_intel, "load_taxonomy", lambda *_a, **_kw: {"concepts": {}})
    monkeypatch.setattr(video_intel, "_lookup_video_duration_seconds", lambda *_a, **_kw: duration)


class TestCmdProcessUrlInversion:
    def test_transcript_runs_before_mindmap_when_auto(self, tmp_path, monkeypatch):
        """Issue #54 reordering: with default mindmap_source=auto and a transcript
        artifact landing on disk, mindmap is called with source='transcript' AFTER
        process_transcript wrote the file."""
        _stub_url_env(monkeypatch, tmp_path)

        call_order: list[str] = []

        def fake_transcript(*args, **kwargs):
            call_order.append("transcript")
            channel_dir = args[5]
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
            channel_dir.mkdir(parents=True, exist_ok=True)
            (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] Speaker: hi\n", encoding="utf-8")
            return prefix, "done"

        mindmap_kw_seen: dict = {}

        def fake_mindmap(*args, **kwargs):
            call_order.append("mindmap")
            mindmap_kw_seen.update(kwargs)
            video = args[2]
            ch = args[6] if len(args) > 6 else kwargs.get("channel_name")
            prefix = video_intel.video_file_prefix(video)
            (tmp_path / ch).mkdir(parents=True, exist_ok=True)
            (tmp_path / ch / f"{prefix}.mindmap.md").write_text("# stub", encoding="utf-8")
            return prefix, "done"

        def fake_concepts(*args, **kwargs):
            call_order.append("concepts")
            return kwargs.get("prefix") or "p", "done"

        monkeypatch.setattr(video_intel, "process_transcript", fake_transcript)
        monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)
        monkeypatch.setattr(video_intel, "process_concepts", fake_concepts)

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "demo", "url": "https://example.com/demo"}],
        }
        video_intel.cmd_process(_process_url_args("https://www.youtube.com/watch?v=AAAAAAAAAAA"), config)

        assert call_order == ["transcript", "mindmap", "concepts"]
        assert mindmap_kw_seen.get("source") == "transcript"
        # transcript_path threaded through so process_mindmap reads from the
        # already-written file rather than re-deriving it.
        assert mindmap_kw_seen.get("transcript_path") is not None

    def test_video_fallback_when_transcript_step_produced_no_file(self, tmp_path, monkeypatch):
        """If process_transcript returns 'done' but does not write a file (e.g.
        an upstream stub or a chunked-transcript-all-failed path), mindmap falls
        back to source='video'. This is the safety net the resolver provides."""
        _stub_url_env(monkeypatch, tmp_path)

        def fake_transcript(*args, **kwargs):
            # Return 'done' but DO NOT write the transcript file.
            return args[6] if len(args) > 6 else kwargs.get("prefix"), "done"

        mindmap_kw_seen: dict = {}

        def fake_mindmap(*args, **kwargs):
            mindmap_kw_seen.update(kwargs)
            video = args[2]
            ch = args[6] if len(args) > 6 else kwargs.get("channel_name")
            prefix = video_intel.video_file_prefix(video)
            (tmp_path / ch).mkdir(parents=True, exist_ok=True)
            (tmp_path / ch / f"{prefix}.mindmap.md").write_text("# stub", encoding="utf-8")
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_transcript", fake_transcript)
        monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)
        monkeypatch.setattr(video_intel, "process_concepts", lambda *a, **kw: ("p", "done"))

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "demo", "url": "https://example.com/demo"}],
        }
        video_intel.cmd_process(_process_url_args("https://www.youtube.com/watch?v=BBBBBBBBBBB"), config)

        assert mindmap_kw_seen.get("source") == "video"

    def test_mindmap_source_none_skips_mindmap_and_concepts(self, tmp_path, monkeypatch):
        """Channel config mindmap_source=none -> mindmap step is skipped entirely,
        concepts is also skipped because there is no mindmap to extract from."""
        _stub_url_env(monkeypatch, tmp_path)

        def fake_transcript(*args, **kwargs):
            channel_dir = args[5]
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
            channel_dir.mkdir(parents=True, exist_ok=True)
            (channel_dir / f"{prefix}.transcript.md").write_text("hi", encoding="utf-8")
            return prefix, "done"

        mindmap_calls = []
        concepts_calls = []
        monkeypatch.setattr(video_intel, "process_transcript", fake_transcript)
        monkeypatch.setattr(
            video_intel,
            "process_mindmap",
            lambda *a, **kw: mindmap_calls.append(kw) or ("p", "done"),
        )
        monkeypatch.setattr(
            video_intel,
            "process_concepts",
            lambda *a, **kw: concepts_calls.append(kw) or ("p", "done"),
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "demo",
                    "url": "https://example.com/demo",
                    "mindmap_source": "none",
                }
            ],
        }
        video_intel.cmd_process(_process_url_args("https://www.youtube.com/watch?v=CCCCCCCCCCC"), config)

        assert mindmap_calls == []
        assert concepts_calls == []

    def test_mindmap_still_runs_when_transcript_step_raises(self, tmp_path, monkeypatch):
        """Review K1: 'mindmap is the AI's discovery surface and must always run'
        (memory: feedback_long_video_keep_mindmap). If process_transcript raises
        an unexpected exception, the inverted ordering must NOT skip mindmap.
        Resolver falls back to source='video' since no transcript landed on
        disk; concepts still fires off the resulting mindmap."""
        _stub_url_env(monkeypatch, tmp_path)

        def fake_transcript_raises(*args, **kwargs):
            raise RuntimeError("simulated transcript backend failure")

        mindmap_kw_seen: dict = {}

        def fake_mindmap(*args, **kwargs):
            mindmap_kw_seen.update(kwargs)
            video = args[2]
            ch = args[6] if len(args) > 6 else kwargs.get("channel_name")
            prefix = video_intel.video_file_prefix(video)
            (tmp_path / ch).mkdir(parents=True, exist_ok=True)
            (tmp_path / ch / f"{prefix}.mindmap.md").write_text(
                "# stub mindmap from video fallback\n", encoding="utf-8"
            )
            return prefix, "done"

        concepts_calls: list[dict] = []
        monkeypatch.setattr(video_intel, "process_transcript", fake_transcript_raises)
        monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)
        monkeypatch.setattr(
            video_intel,
            "process_concepts",
            lambda *a, **kw: concepts_calls.append(kw) or ("p", "done"),
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "demo", "url": "https://example.com/demo"}],
        }
        # SystemExit must NOT fire here - mindmap should run with video fallback.
        video_intel.cmd_process(_process_url_args("https://www.youtube.com/watch?v=DDDDDDDDDDD"), config)

        assert mindmap_kw_seen.get("source") == "video", (
            "transcript exception must not block mindmap; resolver should fall back to source='video'"
        )
        assert concepts_calls, "concepts must still run off the fallback mindmap"


# ---------------------------------------------------------------------------
# cmd_scan ordering inversion (issue #54) - T1 from review
# ---------------------------------------------------------------------------


class TestCmdScanInversion:
    def test_transcript_loop_runs_before_mindmap_loop(self, tmp_path, monkeypatch):
        """Issue #54 inverted scan's loop order: transcript loop now precedes
        mindmap loop within each channel. Existing scan tests assert step
        membership but not ordering, so a future revert would slip past CI.
        Lock the ordering explicitly: every transcript call must precede every
        mindmap call for the same channel."""
        from argparse import Namespace
        from unittest.mock import MagicMock

        _stub_url_env(monkeypatch, tmp_path)

        # Stub YouTube channel discovery so cmd_scan finds a single test video.
        monkeypatch.setattr(
            video_intel,
            "require_youtube",
            lambda: lambda *_a, **_kw: MagicMock(),
        )
        monkeypatch.setattr(
            video_intel,
            "get_channel_id",
            lambda *_a, **_kw: ("UC_TEST", "TestChannel"),
        )

        fake_video = {
            "video_id": "EEEEEEEEEEE",
            "url": "https://www.youtube.com/watch?v=EEEEEEEEEEE",
            "title": "Sample for ordering test",
            "published": "2026-04-27",
            "duration_iso": "PT10M",
        }
        monkeypatch.setattr(
            video_intel,
            "fetch_channel_videos",
            lambda *_a, **_kw: [dict(fake_video)],
        )
        monkeypatch.setattr(
            video_intel,
            "enrich_with_durations",
            lambda *_a, **_kw: {fake_video["video_id"]: fake_video["duration_iso"]},
        )
        monkeypatch.setattr(video_intel, "is_short", lambda *_a, **_kw: False)
        monkeypatch.setattr(
            video_intel,
            "record_alt_title_if_rotated",
            lambda *_a, **_kw: None,
        )

        call_order: list[str] = []

        def fake_transcript(*args, **kwargs):
            call_order.append("transcript")
            channel_dir = args[5]
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
            channel_dir.mkdir(parents=True, exist_ok=True)
            (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] Speaker: hi\n", encoding="utf-8")
            return prefix, "done"

        def fake_mindmap(*args, **kwargs):
            call_order.append("mindmap")
            video = args[2]
            ch = args[6] if len(args) > 6 else kwargs.get("channel_name")
            prefix = video_intel.video_file_prefix(video)
            (tmp_path / ch).mkdir(parents=True, exist_ok=True)
            (tmp_path / ch / f"{prefix}.mindmap.md").write_text("# stub", encoding="utf-8")
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_transcript", fake_transcript)
        monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)

        scan_args = Namespace(channel=None, since=None, dry_run=False, force=False, model=None)
        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "demo",
                    "url": "https://youtube.com/@demo",
                    "auto_transcript": "all",
                }
            ],
        }
        video_intel.cmd_scan(scan_args, config)

        # Must contain at least one transcript call and one mindmap call,
        # and every transcript call must precede every mindmap call.
        assert "transcript" in call_order, f"transcript loop did not run: {call_order}"
        assert "mindmap" in call_order, f"mindmap loop did not run: {call_order}"
        first_mindmap = call_order.index("mindmap")
        last_transcript = max(i for i, x in enumerate(call_order) if x == "transcript")
        assert last_transcript < first_mindmap, f"loop ordering inverted from issue #54 spec: {call_order}"
