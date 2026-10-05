"""Issue #139: timestamps beyond the video's own length are recorded, not just logged.

The single-shot transcript path had no timestamp sanity check and anomalies were
only ever logged. They are now recorded as MILD quality flags decided by the one
classifier (`classify_timestamp_placement`), in two shapes (an outlier jump past
the end, or a systematic overrun), with no auto-repair and a fail-safe on unknown
duration or a per-chunk window.

Falsification: remove the `mild.append(...)` in the overrun block of
`assess_transcript_artifact` and classes 1, 4 and 5 fail; swap
`classify_timestamp_placement` for a hand-rolled `> duration` test and the
tolerance-boundary test fails.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_intel as vi

OUTLIER = "timestamp_overrun_outlier_mild"
SYSTEMATIC = "timestamp_overrun_systematic_mild"


def _stamps(seconds):
    return [{"start": f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}", "voice": 1, "text": "x"} for s in seconds]


def _gpt55_seconds():
    return list(range(0, 757, 12)) + [36060 + 12 * i for i in range(16)]


def _writing_process_seconds():
    return list(range(0, 6399, 25))


class TestAssessorRecordsOverrunShapes:
    def test_outlier_shape_gpt55(self):
        r = vi.assess_transcript_artifact(_stamps(_gpt55_seconds()), 763)
        assert r["mild"] == [OUTLIER]
        assert r["severe"] == []  # the false blind_gap_severe must NOT fire
        assert r["timestamp_overrun_entries"] == 16
        assert r["timestamp_overrun_max_seconds"] > 0

    def test_systematic_shape_writing_process(self):
        r = vi.assess_transcript_artifact(_stamps(_writing_process_seconds()), 2840)
        assert SYSTEMATIC in r["mild"]
        assert r["severe"] == []
        assert r["timestamp_overrun_entries"] > 100

    def test_known_good_has_no_overrun(self):
        r = vi.assess_transcript_artifact(_stamps(range(0, 2801, 25)), 2840)
        assert OUTLIER not in r["mild"] and SYSTEMATIC not in r["mild"]
        assert r["timestamp_overrun_entries"] == 0
        assert r["timestamp_overrun_max_seconds"] == 0

    def test_tolerance_boundary_uses_the_shared_tolerance(self):
        duration = 3000
        limit = duration + vi.timestamp_tolerance(duration)
        base = list(range(0, 2991, 30))
        inside = vi.assess_transcript_artifact(_stamps([*base, limit - 10]), duration)
        outside = vi.assess_transcript_artifact(_stamps([*base, limit + 10]), duration)
        assert inside["timestamp_overrun_entries"] == 0
        assert outside["timestamp_overrun_entries"] == 1

    def test_unknown_duration_fails_safe(self):
        r = vi.assess_transcript_artifact(_stamps(_writing_process_seconds()), None)
        assert OUTLIER not in r["mild"] and SYSTEMATIC not in r["mild"]
        assert r["timestamp_overrun_entries"] == 0

    def test_per_chunk_window_is_not_this_checks_business(self):
        r = vi.assess_transcript_artifact(_stamps([0, 60, 120, 600]), None, window=(0, 240))
        assert OUTLIER not in r["mild"] and SYSTEMATIC not in r["mild"]
        assert r["timestamp_overrun_entries"] == 0

    def test_all_stamps_over_range_is_systematic_with_leading_gap(self):
        r = vi.assess_transcript_artifact(_stamps([5000, 5100, 5200]), 600)
        assert SYSTEMATIC in r["mild"]
        assert r["blind_gap_kind"] == "leading"
        assert r["timestamp_overrun_entries"] == 3


class TestOverrunFlagsAreMildNeverSevere:
    @pytest.mark.parametrize(
        "flag",
        [vi.QUALITY_FLAG_TIMESTAMP_OVERRUN_OUTLIER_MILD, vi.QUALITY_FLAG_TIMESTAMP_OVERRUN_SYSTEMATIC_MILD],
    )
    def test_not_severe(self, flag):
        assert vi.transcript_quality_flags_are_severe([flag]) is False
        assert flag not in vi._SEVERE_QUALITY_FLAGS


def _calls_in(func, name):
    tree = ast.parse(inspect.getsource(func).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if (isinstance(f, ast.Name) and f.id == name) or (isinstance(f, ast.Attribute) and f.attr == name):
                return True
    return False


class TestClassifierHasOneDefinition:
    def test_classifier_behavior_unchanged(self):
        assert vi._classify_and_offset_timestamp("05:00", 1800, 1800) == "35:00"
        assert vi._classify_and_offset_timestamp("40:00", 1800, 1800) == "40:00"
        assert vi._classify_and_offset_timestamp("99:00", 1800, 1800) == "99:00"

    def test_offsetter_delegates_and_has_no_private_boundary(self):
        assert _calls_in(vi._classify_and_offset_timestamp, "classify_timestamp_placement")
        assert not _calls_in(vi._classify_and_offset_timestamp, "timestamp_tolerance")

    def test_assessor_uses_the_same_classifier(self):
        assert _calls_in(vi.assess_transcript_artifact, "classify_timestamp_placement")


def _video():
    return {
        "video_id": "vid123",
        "url": "https://www.youtube.com/watch?v=vid123",
        "title": "Test",
        "published": "2026-06-13",
    }


def _run_single(tmp_path, monkeypatch, seconds, duration):
    payload = {
        "transcripts": _stamps(seconds),
        "screen_content": [],
        "speakers": [{"voice": 1, "name": "A"}],
    }
    monkeypatch.setattr(vi, "call_gemini", lambda *a, **kw: json.dumps(payload))
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)
    monkeypatch.setattr(
        vi, "log_usage_metadata", lambda *a, **kw: {"prompt": 10, "cached": 0, "candidates": 5, "total": 15}
    )
    prefix = "2026-06-13-test"
    _, status = vi.process_transcript(
        object(),
        None,
        _video(),
        "prompt",
        "stub-model",
        tmp_path,
        prefix,
        transcript_source="gemini",
        media_resolution="LOW",
        duration_seconds=duration,
    )
    meta = json.loads((tmp_path / f"{prefix}.meta.json").read_text(encoding="utf-8"))
    return status, meta


class TestSingleShotWriterPersistsTheFlag:
    def test_outlier_payload_records_flag_and_count(self, tmp_path, monkeypatch):
        status, meta = _run_single(tmp_path, monkeypatch, _gpt55_seconds(), 763)
        assert "quality guard" not in status
        assert meta["transcript_status"] == "complete"
        assert OUTLIER in meta["transcript_quality_flags"]
        assert meta["transcript_timestamp_overrun_entries"] == 16
        assert meta["video_id"] == "vid123"

    def test_known_good_records_no_flag(self, tmp_path, monkeypatch):
        status, meta = _run_single(tmp_path, monkeypatch, range(0, 757, 12), 763)
        assert "quality guard" not in status
        assert OUTLIER not in meta.get("transcript_quality_flags", [])
        assert SYSTEMATIC not in meta.get("transcript_quality_flags", [])
        assert meta["transcript_timestamp_overrun_entries"] == 0

    def test_unknown_duration_fails_safe_at_the_writer(self, tmp_path, monkeypatch):
        _, meta = _run_single(tmp_path, monkeypatch, _gpt55_seconds(), None)
        assert OUTLIER not in meta.get("transcript_quality_flags", [])
        assert SYSTEMATIC not in meta.get("transcript_quality_flags", [])
        assert meta["transcript_timestamp_overrun_entries"] == 0


def _hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


class TestChunkedWriterPersistsTheFlag:
    def test_stamps_past_the_video_end_are_recorded(self, tmp_path: Path, monkeypatch):
        calls = {"n": 0}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, response_json=False, **kw):
            idx = calls["n"]
            calls["n"] += 1
            on_response = kw.get("on_response")
            if on_response is not None:
                on_response(
                    SimpleNamespace(
                        usage_metadata=SimpleNamespace(
                            prompt_token_count=100000,
                            cached_content_token_count=0,
                            thoughts_token_count=0,
                            candidates_token_count=5000,
                            total_token_count=105000,
                        )
                    )
                )
            start = idx * 1800
            marks = [start + 60 + 120 * i for i in range(14)]
            if idx == 1:
                marks += [3540, 3970, 4030, 4090]
            return json.dumps(
                {
                    "transcripts": [{"start": _hms(s), "voice": 1, "text": f"c{idx} {s}"} for s in marks],
                    "screen_content": [],
                    "speakers": [{"voice": 1, "name": "A"}],
                }
            )

        monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)
        fake_types = SimpleNamespace(
            MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH")
        )
        channel_dir = tmp_path / "demo"
        prefix = "2026-08-12-a-long-talk"
        video = {
            "video_id": "vid123",
            "url": "https://www.youtube.com/watch?v=vid123",
            "title": "A Long Talk",
            "published": "2026-08-12",
        }
        vi._run_chunked_transcript_url(
            client=object(),
            types=fake_types,
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix=prefix,
            chunks=[(0, 1800), (1800, 3600)],
            duration_seconds=3600,
            chunk_minutes=30,
            force=False,
        )
        meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        flags = meta.get("transcript_quality_flags", [])
        assert OUTLIER in flags or SYSTEMATIC in flags
        assert meta["transcript_timestamp_overrun_entries"] >= 1
        assert meta["transcript_status"] == "ok"


class TestEveryWholeVideoWriterPersistsTheCount:
    def test_literal_count(self):
        src = Path(vi.__file__).read_text(encoding="utf-8")
        # 4 writers: single-shot full-parse + salvage (process_transcript),
        # _run_chunked_transcript_url, _try_captions_transcript; plus one entry
        # in TRANSCRIPT_ARTIFACT_FIELDS. A new writer that forgets the field
        # leaves this count short.
        assert src.count('"transcript_timestamp_overrun_entries"') == 5
