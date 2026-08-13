"""Issue #124: a corrupt meta.json must not mask the error a handler is recording.

The writer-side paths that merge into an existing meta.json all sit inside, or
feed, exception handlers whose job is to preserve a failure. A ``json.loads``
raising from inside one of those handlers propagates out and destroys the
original error - diagnostic blindness at precisely the moment something else has
already gone wrong. Cloud-mount corpora (the production layout) are where torn
reads actually happen.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from video_intel import _read_meta_best_effort, _record_transcript_error, update_meta

#: Contents that must all be survivable. The last two parse as valid JSON but are
#: not objects, so they would sail past json.loads and then blow up on .update()
#: - the same masking failure wearing a different exception type.
CORRUPT_META_BODIES = [
    pytest.param('{"video_id": "abc123", "transcr', id="truncated_midway"),
    pytest.param("", id="empty_file"),
    pytest.param("\x00\x00\x00", id="null_bytes"),
    pytest.param("not json at all", id="not_json"),
    pytest.param("[1, 2, 3]", id="json_list_not_object"),
    pytest.param("null", id="json_null"),
    pytest.param('"a string"', id="json_string"),
]


class TestReadMetaBestEffort:
    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_corrupt_meta_returns_empty_dict_and_warns(self, tmp_path: Path, body, caplog):
        meta_path = tmp_path / "2026-08-12-talk.meta.json"
        meta_path.write_text(body, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = _read_meta_best_effort(meta_path)

        assert result == {}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "the operator must be told which file was discarded"
        assert "2026-08-12-talk.meta.json" in warnings[0].getMessage()

    def test_missing_file_returns_empty_dict_without_warning(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = _read_meta_best_effort(tmp_path / "absent.meta.json")

        assert result == {}
        assert not [r for r in caplog.records if r.levelno == logging.WARNING], (
            "a meta that was never written is normal, not corruption"
        )

    def test_healthy_meta_is_returned_unchanged(self, tmp_path: Path):
        meta_path = tmp_path / "ok.meta.json"
        payload = {"video_id": "abc123", "modes_completed": ["transcript"], "transcript_status": "ok"}
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        assert _read_meta_best_effort(meta_path) == payload


class TestUpdateMetaSurvivesCorruption:
    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_update_meta_rewrites_corrupt_file_instead_of_raising(self, tmp_path: Path, body):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(body, encoding="utf-8")

        update_meta(meta_path, {"video_id": "abc123", "transcript_status": "ok"}, mode="transcript")

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["video_id"] == "abc123"
        assert written["modes_completed"] == ["transcript"]

    def test_healthy_meta_still_merges_rather_than_replacing(self, tmp_path: Path):
        """The guard must not turn every write into a clobber."""
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(
            json.dumps({"video_id": "abc123", "modes_completed": ["mindmap"], "title": "Keep me"}),
            encoding="utf-8",
        )

        update_meta(meta_path, {"transcript_status": "ok"}, mode="transcript")

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["title"] == "Keep me", "pre-existing fields survive a healthy merge"
        assert sorted(written["modes_completed"]) == ["mindmap", "transcript"]


class TestRecordTranscriptErrorSurvivesCorruption:
    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_original_error_is_recorded_over_a_corrupt_meta(self, tmp_path: Path, body):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(body, encoding="utf-8")

        _record_transcript_error(meta_path, "gemini error: Server disconnected without sending a response.")

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["last_error"] == "gemini error: Server disconnected without sending a response."


class TestProcessMindmapHandlerPreservesOriginalError:
    """The issue's headline case: corrupt meta + failing Gemini call.

    The handler must record the ORIGINAL failure, not raise a JSONDecodeError
    from inside itself.
    """

    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_gemini_failure_over_corrupt_meta_records_the_gemini_error(self, tmp_path: Path, body, monkeypatch):
        import video_intel as vi

        channel_dir = tmp_path / "somechannel"
        channel_dir.mkdir()
        video = {
            "url": "https://www.youtube.com/watch?v=abc123",
            "video_id": "abc123",
            "title": "A Talk",
            "published": "2026-08-12",
        }
        prefix = vi.video_file_prefix(video)
        (channel_dir / f"{prefix}.meta.json").write_text(body, encoding="utf-8")

        def _boom(*_a, **_kw):
            raise RuntimeError("Server disconnected without sending a response.")

        monkeypatch.setattr(vi, "call_gemini", _boom)

        resolved_prefix, status = vi.process_mindmap(
            client=object(),
            types=_FakeTypes(),
            video=video,
            prompt_text="x",
            model="gemini-2.5-flash",
            output_dir=tmp_path,
            channel_name="somechannel",
            source="video",
        )

        assert status.startswith("error:")
        assert "Server disconnected" in status, "the ORIGINAL error must survive, not a JSONDecodeError"

        written = json.loads((channel_dir / f"{resolved_prefix}.meta.json").read_text(encoding="utf-8"))
        assert "Server disconnected" in written["last_error"]
        assert written["video_id"] == "abc123", "identity is still stamped (issue #66 contract)"


class _FakeTypes:
    """Minimal stand-in for google.genai.types used by process_mindmap's setup."""

    class MediaResolution:
        MEDIA_RESOLUTION_LOW = "LOW"
        MEDIA_RESOLUTION_HIGH = "HIGH"
