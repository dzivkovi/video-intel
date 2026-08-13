"""Issue #124: a corrupt meta.json must not mask the error a handler is recording.

The writer-side paths that merge into an existing meta.json all sit inside, or
feed, exception handlers whose job is to preserve a failure. A read raising from
inside one of those handlers propagates out and destroys the original error -
diagnostic blindness at exactly the moment something else has already gone
wrong. Cloud-mount corpora (the production layout) are where torn reads happen.

Two failure classes are kept apart deliberately, and the tests below pin both:
unusable CONTENT is quarantined and replaced, while a failed READ propagates on
the success path (the bytes may be intact, and overwriting them is data loss).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import video_intel as vi
from video_intel import _read_meta_best_effort, _record_transcript_error, update_meta

ORIGINAL_ERROR = "Server disconnected without sending a response."

#: Contents that must all be survivable. The last three parse as valid JSON but
#: are not objects, so they would sail past json.loads and then blow up on
#: .update() - the same masking failure wearing a different exception.
CORRUPT_META_BODIES = [
    pytest.param('{"video_id": "abc123", "transcr', id="truncated_midway"),
    pytest.param("", id="empty_file"),
    pytest.param("not json at all", id="not_json"),
    pytest.param("[1, 2, 3]", id="json_list_not_object"),
    pytest.param("null", id="json_null"),
    pytest.param('"a string"', id="json_string"),
]

#: Bodies that must be written as raw BYTES. A write torn mid-multibyte
#: character is the normal shape of a truncated write on a corpus carrying
#: Cyrillic/BCS titles, and UnicodeDecodeError subclasses ValueError, NOT
#: OSError. Catching only (JSONDecodeError, OSError) let exactly this case
#: raise straight through the guard and mask the original error.
CORRUPT_META_BYTES = [
    pytest.param(b'{"title": "\xd0\x9f\xd1\x80\xd0', id="torn_mid_utf8_cyrillic"),
    pytest.param(b"\xff\xfe\x00\x00", id="invalid_utf8_fragment"),
]


def _boom(*_a, **_kw):
    raise RuntimeError(ORIGINAL_ERROR)


class _FakeTypes:
    """Minimal stand-in for google.genai.types used by process_mindmap's setup."""

    class MediaResolution:
        MEDIA_RESOLUTION_LOW = "LOW"
        MEDIA_RESOLUTION_HIGH = "HIGH"


def _video():
    return {
        "url": "https://www.youtube.com/watch?v=abc123",
        "video_id": "abc123",
        "title": "A Talk",
        "published": "2026-08-12",
    }


def _run_failing_mindmap(tmp_path: Path, meta_body, monkeypatch):
    """Drive the REAL process_mindmap exception handler with a failing Gemini call."""
    channel_dir = tmp_path / "somechannel"
    channel_dir.mkdir(exist_ok=True)
    video = _video()
    prefix = vi.video_file_prefix(video)
    meta_path = channel_dir / f"{prefix}.meta.json"
    if isinstance(meta_body, bytes):
        meta_path.write_bytes(meta_body)
    else:
        meta_path.write_text(meta_body, encoding="utf-8")
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
    return status, channel_dir / f"{resolved_prefix}.meta.json"


class TestReadMetaBestEffort:
    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_unusable_content_returns_empty_dict_and_warns(self, tmp_path: Path, body, caplog):
        meta_path = tmp_path / "2026-08-12-talk.meta.json"
        meta_path.write_text(body, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = _read_meta_best_effort(meta_path, raise_on_os_error=False)

        assert result == {}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "the operator must be told which file was discarded"
        assert "2026-08-12-talk.meta.json" in warnings[0].getMessage()

    def test_missing_file_returns_empty_dict_without_warning(self, tmp_path: Path, caplog):
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = _read_meta_best_effort(tmp_path / "absent.meta.json", raise_on_os_error=True)

        assert result == {}
        assert not [r for r in caplog.records if r.levelno == logging.WARNING], (
            "a meta that was never written is normal, not corruption"
        )

    def test_healthy_meta_is_returned_unchanged(self, tmp_path: Path):
        meta_path = tmp_path / "ok.meta.json"
        payload = {"video_id": "abc123", "modes_completed": ["transcript"], "transcript_status": "ok"}
        meta_path.write_text(json.dumps(payload), encoding="utf-8")

        assert _read_meta_best_effort(meta_path, raise_on_os_error=True) == payload


class TestInvalidUtf8IsSurvivable:
    """UnicodeDecodeError subclasses ValueError, not OSError.

    This is the case that kept issue #124's headline scenario open through the
    first fix attempt: the guard caught (JSONDecodeError, OSError), so a torn
    multibyte write raised straight through it.
    """

    @pytest.mark.parametrize("raw", CORRUPT_META_BYTES)
    def test_helper_survives_invalid_utf8_under_both_policies(self, tmp_path: Path, raw):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_bytes(raw)

        assert _read_meta_best_effort(meta_path, raise_on_os_error=False) == {}
        assert _read_meta_best_effort(meta_path, raise_on_os_error=True) == {}, (
            "invalid CONTENT is survivable either way; only a failed READ differs"
        )

    @pytest.mark.parametrize("raw", CORRUPT_META_BYTES)
    def test_update_meta_survives_invalid_utf8(self, tmp_path: Path, raw):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_bytes(raw)

        update_meta(meta_path, {"video_id": "abc123"}, mode="transcript")

        assert json.loads(meta_path.read_text(encoding="utf-8"))["video_id"] == "abc123"

    @pytest.mark.parametrize("raw", CORRUPT_META_BYTES)
    def test_mindmap_handler_preserves_the_original_error(self, tmp_path: Path, raw, monkeypatch):
        status, meta_path = _run_failing_mindmap(tmp_path, raw, monkeypatch)

        assert ORIGINAL_ERROR in status
        assert ORIGINAL_ERROR in json.loads(meta_path.read_text(encoding="utf-8"))["last_error"]


class TestUpdateMetaSurvivesCorruption:
    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_update_meta_rewrites_unusable_content_instead_of_raising(self, tmp_path: Path, body):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(body, encoding="utf-8")

        update_meta(meta_path, {"video_id": "abc123", "transcript_status": "ok"}, mode="transcript")

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["video_id"] == "abc123"
        assert written["modes_completed"] == ["transcript"]

    def test_healthy_meta_still_merges_rather_than_replacing(self, tmp_path: Path):
        """The guard must not degrade into 'every write clobbers'."""
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(
            json.dumps({"video_id": "abc123", "modes_completed": ["mindmap"], "title": "Keep me"}),
            encoding="utf-8",
        )

        update_meta(meta_path, {"transcript_status": "ok"}, mode="transcript")

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["title"] == "Keep me", "pre-existing fields survive a healthy merge"
        assert sorted(written["modes_completed"]) == ["mindmap", "transcript"]


class TestReadFailureIsNotContentCorruption:
    """An OSError means the bytes may be fine, so overwriting them is data loss.

    `update_meta` is on the SUCCESS path. Returning {} there would clobber a
    healthy file with whatever handful of fields the caller re-supplies,
    destroying `alt_titles` (title-rotation history lives nowhere else) and
    `skip_modes` (the operator's deliberate stage suppression, issue #42).
    """

    @staticmethod
    def _make_unreadable(monkeypatch, target: Path):
        """Fail the I/O layer only, leaving the bytes on disk intact."""
        real_read_bytes = Path.read_bytes

        def _failing_read(self, *a, **kw):
            if self == target:
                raise OSError(5, "Input/output error")
            return real_read_bytes(self, *a, **kw)

        monkeypatch.setattr(Path, "read_bytes", _failing_read)

    def test_update_meta_refuses_to_overwrite_a_file_it_could_not_read(self, tmp_path: Path, monkeypatch):
        meta_path = tmp_path / "v.meta.json"
        healthy = {
            "video_id": "abc123",
            "alt_titles": ["An Earlier Title"],
            "skip_modes": ["transcript"],
            "skip_reason": "burned 6.5h once",
            "modes_completed": ["mindmap"],
        }
        meta_path.write_text(json.dumps(healthy), encoding="utf-8")
        self._make_unreadable(monkeypatch, meta_path)

        with pytest.raises(OSError):
            update_meta(meta_path, {"processed": "2026-08-12"}, mode="concepts")

        monkeypatch.undo()
        assert json.loads(meta_path.read_text(encoding="utf-8")) == healthy, (
            "the file must survive intact - loud beats lossy on the success path"
        )

    def test_error_paths_still_swallow_a_failed_read(self, tmp_path: Path, monkeypatch):
        """Inside an error handler the alternative is destroying the error itself."""
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(json.dumps({"video_id": "abc123"}), encoding="utf-8")
        self._make_unreadable(monkeypatch, meta_path)

        assert _read_meta_best_effort(meta_path, raise_on_os_error=False) == {}


class TestQuarantineSidecar:
    """A rewrite must never be a silent loss of the bytes it discarded."""

    def test_unusable_content_is_copied_aside_before_the_rewrite(self, tmp_path: Path):
        meta_path = tmp_path / "2026-08-12-talk.meta.json"
        meta_path.write_text('{"video_id": "abc123", "alt_titles": ["Old"], "trunc', encoding="utf-8")

        update_meta(meta_path, {"video_id": "abc123"}, mode="transcript")

        sidecar = tmp_path / "2026-08-12-talk.meta.corrupt.json"
        assert sidecar.exists(), "the discarded bytes must stay recoverable by hand"
        assert "alt_titles" in sidecar.read_text(encoding="utf-8")

    def test_quarantine_suffix_is_in_the_prune_allowlist(self):
        assert any("meta.corrupt.json" in p for p in vi.PRUNE_SHORTS_DELETION_PATTERNS)


class TestModesCompletedTypeGuard:
    """`.append` on a str raises out of the writer - the same masking class.

    Hand-editing meta.json is this project's documented skip_modes recovery
    flow, so a scalar here is a realistic typo rather than a theoretical one.
    """

    @pytest.mark.parametrize("bad", ["transcript", 3, {"a": 1}], ids=["str", "int", "dict"])
    def test_non_list_modes_completed_is_rebuilt(self, tmp_path: Path, bad):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(json.dumps({"video_id": "abc123", "modes_completed": bad}), encoding="utf-8")

        update_meta(meta_path, {"transcript_status": "ok"}, mode="transcript")

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["modes_completed"] == ["transcript"]
        assert written["video_id"] == "abc123"


class TestRecordTranscriptError:
    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_original_error_is_recorded_over_unusable_content(self, tmp_path: Path, body):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(body, encoding="utf-8")

        _record_transcript_error(meta_path, f"gemini error: {ORIGINAL_ERROR}")

        assert ORIGINAL_ERROR in json.loads(meta_path.read_text(encoding="utf-8"))["last_error"]

    def test_healthy_meta_keeps_its_other_fields(self, tmp_path: Path):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text(
            json.dumps({"video_id": "abc123", "alt_titles": ["Old"], "modes_completed": ["mindmap"]}),
            encoding="utf-8",
        )

        _record_transcript_error(meta_path, "gemini error: boom")

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["alt_titles"] == ["Old"]
        assert written["video_id"] == "abc123"
        assert written["last_error"] == "gemini error: boom"


class TestProcessMindmapHandlerPreservesOriginalError:
    """The issue's headline case: corrupt meta + failing Gemini call."""

    @pytest.mark.parametrize("body", CORRUPT_META_BODIES)
    def test_gemini_failure_over_unusable_meta_records_the_gemini_error(self, tmp_path: Path, body, monkeypatch):
        status, meta_path = _run_failing_mindmap(tmp_path, body, monkeypatch)

        assert status.startswith("error:")
        assert ORIGINAL_ERROR in status, "the ORIGINAL error must survive, not a decode error"

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert ORIGINAL_ERROR in written["last_error"]
        assert written["video_id"] == "abc123", "identity is still stamped (issue #66 contract)"

    def test_healthy_meta_is_merged_not_replaced_by_the_handler(self, tmp_path: Path, monkeypatch):
        healthy = json.dumps({"video_id": "abc123", "alt_titles": ["Old"], "duration_seconds": 2528})

        _, meta_path = _run_failing_mindmap(tmp_path, healthy, monkeypatch)

        written = json.loads(meta_path.read_text(encoding="utf-8"))
        assert written["alt_titles"] == ["Old"], "a healthy meta must still be MERGED, not replaced"
        assert written["duration_seconds"] == 2528


class TestConceptsWriterStampsIdentity:
    """Issue #66 contract: no writer may leave an identity-less meta.

    `update_meta(meta_path, {"processed": ...}, "concepts")` was only safe while
    the existing meta stayed readable. If it ever is not, the file loses
    `video_id`, `_load_video_id_index` skips it, and the video is re-queued for
    a full re-transcribe.
    """

    def test_concepts_meta_carries_video_id_even_over_unusable_content(self, tmp_path: Path, monkeypatch):
        channel_dir = tmp_path / "somechannel"
        channel_dir.mkdir()
        video = _video()
        prefix = vi.video_file_prefix(video)
        (channel_dir / f"{prefix}.meta.json").write_text('{"video_id": "abc123", "trunc', encoding="utf-8")

        monkeypatch.setattr(
            vi,
            "call_gemini_text",
            lambda *_a, **_kw: json.dumps(
                {"concepts": [{"concept_id": "agents", "preferred_label": "Agents", "status": "new"}]}
            ),
        )

        vi.process_concepts(
            client=object(),
            types=_FakeTypes(),
            video=video,
            mindmap_text="# A Talk",
            taxonomy={"concepts": {}},
            model="gemini-2.5-flash",
            output_dir=tmp_path,
            channel_name="somechannel",
        )

        written = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert written["video_id"] == "abc123"
        assert "concepts" in written["modes_completed"]


class TestQuarantineIsRaceSafeAndNonClobbering:
    """Codex cross-model finding on PR #131.

    The first version re-read the live file instead of using the bytes it had
    already captured. A concurrent healthy writer landing between the two reads
    would get its fresh bytes quarantined and then overwritten by the caller -
    turning the recovery mechanism into the data loss it exists to prevent.
    """

    def test_quarantine_saves_the_bytes_that_were_actually_read(self, tmp_path: Path, monkeypatch):
        meta_path = tmp_path / "v.meta.json"
        meta_path.write_text('{"video_id": "abc123", "trunc', encoding="utf-8")

        real_read_bytes = Path.read_bytes
        healthy = json.dumps({"video_id": "abc123", "alt_titles": ["Recovered"]}).encode()
        state = {"n": 0}

        def racing_read(self, *a, **kw):
            if self == meta_path:
                state["n"] += 1
                if state["n"] > 1:
                    return healthy  # a concurrent writer landed after our first read
            return real_read_bytes(self, *a, **kw)

        monkeypatch.setattr(Path, "read_bytes", racing_read)
        _read_meta_best_effort(meta_path, raise_on_os_error=False)
        monkeypatch.undo()

        sidecar = tmp_path / "v.meta.corrupt.json"
        assert sidecar.exists()
        assert "Recovered" not in sidecar.read_text(encoding="utf-8"), (
            "the quarantine must hold the CORRUPT bytes we rejected, never a concurrent healthy write"
        )

    def test_a_second_corruption_does_not_erase_the_first_quarantine(self, tmp_path: Path):
        meta_path = tmp_path / "v.meta.json"

        meta_path.write_text('{"first": "corruption', encoding="utf-8")
        _read_meta_best_effort(meta_path, raise_on_os_error=False)
        meta_path.write_text('{"second": "corruption', encoding="utf-8")
        _read_meta_best_effort(meta_path, raise_on_os_error=False)

        first = (tmp_path / "v.meta.corrupt.json").read_text(encoding="utf-8")
        assert "first" in first, "the original evidence must survive"
        assert (tmp_path / "v.2.meta.corrupt.json").exists(), "the second goes to its own sidecar"
