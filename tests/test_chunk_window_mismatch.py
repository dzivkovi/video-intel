"""Tests for issue #158: cross-chunk timestamp misclassification is invisible
to every existing backward-jump / gap check.

`_classify_and_offset_timestamp` decides per-stamp whether a chunk's
timestamp is absolute or chunk-relative; when it guesses wrong for a BLOCK
of a chunk's stamps, the whole block lands at the wrong place in the
timeline. That shape produces no backward-jump signal (the block is
internally consistent, just shifted) and no post-merge gap/density signal
(the shifted block can land inside another chunk's healthy range).

Two layers are tested:
  1. `merge_chunked_transcripts`' `chunk_bounds` parameter - the
     post-classification, per-chunk window check itself.
  2. `_classify_chunk_window_violations` - the pure severity classifier
     (severe/mild bucket, boundary values).
  3. `_run_chunked_transcript_url`, executed for real (stubbed Gemini call
     only) - severe flows to meta.json + transcript_status + exit code,
     mirroring tests/test_transcript_quality_guard.py::TestExitCodeSevereVsMild.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import video_intel as vi
from video_intel import (
    EXIT_PARTIAL,
    _classify_chunk_window_violations,
    missing_pipeline_artifacts,
    transcript_quality_flags_are_severe,
)


def _mmss(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _entries(seconds: list[int]) -> list[dict]:
    return [{"start": _mmss(s), "voice": 1, "text": f"line at {s}"} for s in seconds]


def _chunk_json(seconds: list[int], speaker_name: str = "Host") -> dict:
    return {
        "transcripts": _entries(seconds),
        "screen_content": [],
        "speakers": [{"voice": 1, "name": speaker_name}],
    }


# ---------------------------------------------------------------------------
# 1. merge_chunked_transcripts' chunk_bounds parameter - the detector itself
# ---------------------------------------------------------------------------


class TestMergeChunkedTranscriptsWindowDetector:
    def test_healthy_multi_chunk_input_does_not_flag(self):
        """Two chunks, both with plausible absolute stamps inside their own
        actual window - zero violations anywhere."""
        chunk1 = _chunk_json([10, 200, 400, 590])
        chunk2 = _chunk_json([650, 800, 1000, 1190])
        merged = vi.merge_chunked_transcripts(
            [(0, chunk1), (600, chunk2)],
            chunk_duration_seconds=600,
            chunk_bounds=[(0, 600), (600, 1200)],
        )
        violations = merged["_chunk_window_violations"]
        assert len(violations) == 2
        assert all(v["out_of_window"] == 0 for v in violations)
        # Label-only: no entry dropped, reordered, or value-changed beyond
        # the existing classifier's own decision.
        assert len(merged["transcripts"]) == 8

    def test_double_offset_shape_flags_severe(self):
        """Chunk 2's stamps land at absolute positions that belong to a
        LATER window entirely (the double-offset shape issue #158 names):
        the classifier's own branches cannot place them inside chunk 2's
        real [540, 1260] window (with chunk_duration_seconds=600, slack=60),
        so they fall through unchanged - the resulting values are still
        squarely outside chunk 2's real window, exactly the corruption this
        detector exists to catch. 4 of 4 classified dialogue entries land
        out of window: fraction=1.0 > 0.5 with >= 4 entries -> SEVERE."""
        chunk1 = _chunk_json([10, 200, 400, 590])
        chunk2 = _chunk_json([1300, 1350, 1400, 1450])  # belongs to chunk 3's window
        merged = vi.merge_chunked_transcripts(
            [(0, chunk1), (600, chunk2)],
            chunk_duration_seconds=600,
            chunk_bounds=[(0, 600), (600, 1200)],
        )
        violations = merged["_chunk_window_violations"]
        chunk2_result = violations[1]
        assert chunk2_result["chunk_index"] == 2
        assert chunk2_result["classified_dialogue"] == 4
        assert chunk2_result["out_of_window"] == 4
        # Entries themselves are untouched - label-only.
        assert len(merged["transcripts"]) == 8
        result = _classify_chunk_window_violations(violations)
        assert vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE in result["severe"]
        assert transcript_quality_flags_are_severe(result["severe"]) is True

    def test_single_stray_stamp_inside_slack_does_not_flag(self):
        """A stamp landing right at the edge of the actual window, still
        within slack, is healthy - not a violation at all."""
        chunk1 = _chunk_json([10, 200, 400])
        # 1250s classifies as absolute (600 <= 1250 <= 600+660) and sits
        # within [540, 1260] (slack=60 off the actual end 1200).
        chunk2 = _chunk_json([650, 900, 1250])
        merged = vi.merge_chunked_transcripts(
            [(0, chunk1), (600, chunk2)],
            chunk_duration_seconds=600,
            chunk_bounds=[(0, 600), (600, 1200)],
        )
        chunk2_result = merged["_chunk_window_violations"][1]
        assert chunk2_result["classified_dialogue"] == 3
        assert chunk2_result["out_of_window"] == 0

    def test_single_stamp_beyond_slack_flags_mild(self):
        """One bad stamp among several healthy ones in the same chunk: a
        stray misclassification, not a systemic one - MILD, not SEVERE."""
        chunk1 = _chunk_json([10, 200, 400])
        # Three healthy absolute stamps + one implausible stamp (1300, which
        # exceeds chunk 2's absolute band [600, 1260] and its relative band
        # [0, 660], so the classifier passes it through unchanged and it
        # lands outside chunk 2's real window [540, 1260]).
        chunk2 = _chunk_json([700, 900, 1100, 1300])
        merged = vi.merge_chunked_transcripts(
            [(0, chunk1), (600, chunk2)],
            chunk_duration_seconds=600,
            chunk_bounds=[(0, 600), (600, 1200)],
        )
        chunk2_result = merged["_chunk_window_violations"][1]
        assert chunk2_result["classified_dialogue"] == 4
        assert chunk2_result["out_of_window"] == 1
        result = _classify_chunk_window_violations(merged["_chunk_window_violations"])
        assert result["severe"] == []
        assert vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_MILD in result["mild"]

    def test_folded_runt_chunk_over_nominal_tail_does_not_flag(self):
        """The folded-runt-tail shape (issue #128/#157): the last chunk's
        ACTUAL span (600-1290) exceeds the nominal chunk_duration_seconds
        (600) by 90s. A legitimate absolute stamp at 1280 - past the
        NOMINAL window's edge (1200+slack=1260) but well inside the ACTUAL
        window's edge (1290+slack=1350) - must not be flagged."""
        chunk1 = _chunk_json([10, 200, 400])
        chunk2 = _chunk_json([650, 900, 1100, 1280])
        merged = vi.merge_chunked_transcripts(
            [(0, chunk1), (600, chunk2)],
            chunk_duration_seconds=600,
            # Actual bounds: chunk 2 folded a runt tail, so its real end
            # (1290) is past the nominal chunk_start + chunk_duration_seconds
            # (1200) that a naive window would have used.
            chunk_bounds=[(0, 600), (600, 1290)],
        )
        chunk2_result = merged["_chunk_window_violations"][1]
        assert chunk2_result["classified_dialogue"] == 4
        assert chunk2_result["out_of_window"] == 0

    def test_legacy_two_tuple_callers_get_no_violations_key(self):
        """chunk_bounds omitted (every pre-#158 caller) - byte-identical
        output shape, no new key at all."""
        chunk1 = _chunk_json([10, 200])
        merged = vi.merge_chunked_transcripts([(0, chunk1)])
        assert "_chunk_window_violations" not in merged


# ---------------------------------------------------------------------------
# 2. _classify_chunk_window_violations - pure severity classifier
# ---------------------------------------------------------------------------


class TestClassifyChunkWindowViolations:
    def test_no_violations_is_healthy(self):
        result = _classify_chunk_window_violations([{"chunk_index": 1, "classified_dialogue": 10, "out_of_window": 0}])
        assert result == {"severe": [], "mild": [], "total_violations": 0}

    def test_majority_with_enough_entries_is_severe(self):
        result = _classify_chunk_window_violations([{"chunk_index": 2, "classified_dialogue": 4, "out_of_window": 3}])
        assert result["severe"] == [vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE]
        assert result["mild"] == []
        assert result["total_violations"] == 3

    def test_boundary_exactly_half_fraction_is_mild_not_severe(self):
        """2 of 4 = exactly 0.5 - the contract says '> 0.5', so exactly at
        the boundary must NOT be severe; it is still a violation -> MILD."""
        result = _classify_chunk_window_violations([{"chunk_index": 1, "classified_dialogue": 4, "out_of_window": 2}])
        assert result["severe"] == []
        assert result["mild"] == [vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_MILD]
        assert result["total_violations"] == 2

    def test_boundary_exactly_four_entries_qualifies_for_severe(self):
        """classified_dialogue == 4 (the floor, not merely above it) still
        qualifies when the fraction also clears 0.5."""
        result = _classify_chunk_window_violations([{"chunk_index": 1, "classified_dialogue": 4, "out_of_window": 3}])
        assert result["severe"] == [vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE]

    def test_majority_but_too_few_entries_is_mild(self):
        """3 of 3 = 100% out of window, but classified_dialogue (3) is below
        the CHUNK_WINDOW_MISMATCH_SEVERE_MIN_ENTRIES floor (4) - too little
        evidence to call it systemic -> MILD, not severe."""
        result = _classify_chunk_window_violations([{"chunk_index": 1, "classified_dialogue": 3, "out_of_window": 3}])
        assert result["severe"] == []
        assert result["mild"] == [vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_MILD]

    def test_multiple_chunks_union_and_total_violations_sums(self):
        result = _classify_chunk_window_violations(
            [
                {"chunk_index": 1, "classified_dialogue": 4, "out_of_window": 3},  # severe
                {"chunk_index": 2, "classified_dialogue": 5, "out_of_window": 1},  # mild
                {"chunk_index": 3, "classified_dialogue": 10, "out_of_window": 0},  # healthy
            ]
        )
        assert result["severe"] == [vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE]
        assert result["mild"] == [vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_MILD]
        assert result["total_violations"] == 4

    def test_zero_classified_dialogue_contributes_nothing(self):
        """No classified entries -> no evidence either way; must not raise
        a ZeroDivisionError."""
        result = _classify_chunk_window_violations([{"chunk_index": 1, "classified_dialogue": 0, "out_of_window": 0}])
        assert result == {"severe": [], "mild": [], "total_violations": 0}

    def test_severe_flag_is_a_member_of_severe_quality_flags(self):
        assert transcript_quality_flags_are_severe([vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE]) is True

    def test_mild_flag_is_not_severe(self):
        assert transcript_quality_flags_are_severe([vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_MILD]) is False


# ---------------------------------------------------------------------------
# 3. Writer integration - _run_chunked_transcript_url, executed for real
#    (stubbed Gemini call only), mirroring
#    test_transcript_quality_guard.py::TestExitCodeSevereVsMild.
# ---------------------------------------------------------------------------


def _video(video_id: str = "xyz98765432") -> dict:
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "A Chunked Talk",
        "published": "2026-08-30",
    }


def _fake_gemini_response(prompt_tokens: int = 5000, candidates_tokens: int = 100):
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=candidates_tokens,
            total_token_count=prompt_tokens + candidates_tokens,
        ),
        candidates=[SimpleNamespace(finish_reason="STOP")],
    )


def _stub_chunked_call_gemini(monkeypatch, responses: list[str]):
    def fake_call_gemini(_client, _types, _uri, _prompt, _model, **kw):
        idx = fake_call_gemini.n
        fake_call_gemini.n += 1
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(_fake_gemini_response())
        return responses[idx]

    fake_call_gemini.n = 0
    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)


class TestChunkedWriterCatchesWindowMismatch:
    def test_severe_window_mismatch_flows_to_meta_and_exit_code(self, tmp_path, monkeypatch):
        """Chunk 2's entries all land outside chunk 2's real window (the
        double-offset shape) - real writer path, real meta.json read back,
        checker built from that same read (never a stub-blind handoff -
        mirrors TestCheckerAndWriterAgreeOnPaths / TestExitCodeSevereVsMild)."""
        chunk1_json = _chunk_json([10, 200, 400, 590])
        chunk2_json = _chunk_json([1300, 1350, 1400, 1450])  # belongs to chunk 3+'s window
        responses = [json.dumps(chunk1_json), json.dumps(chunk2_json)]
        _stub_chunked_call_gemini(monkeypatch, responses)

        channel_dir = tmp_path / "demo"
        video = _video()
        status = vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix="2026-08-30-window-mismatch-talk",
            chunks=[(0, 600), (600, 1200)],
            duration_seconds=1200,
            chunk_minutes=10,
            force=False,
        )

        meta_path = channel_dir / "2026-08-30-window-mismatch-talk.meta.json"
        transcript_path = channel_dir / "2026-08-30-window-mismatch-talk.transcript.md"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        assert status == "partial"
        assert meta["transcript_status"] == "partial"
        assert "chunk_window_mismatch_severe" in meta["transcript_quality_flags"]
        assert meta["transcript_chunk_window_violations"] == 4

        quality_severe = transcript_quality_flags_are_severe(meta.get("transcript_quality_flags"))
        assert quality_severe is True
        steps = [
            {
                "label": "transcript",
                "requested": True,
                "status": status,
                "path": transcript_path,
                "quality_severe": quality_severe,
            }
        ]
        assert missing_pipeline_artifacts(steps) == ["transcript"]
        assert EXIT_PARTIAL == 3

    def test_healthy_chunks_do_not_flag_and_exit_clean(self, tmp_path, monkeypatch):
        """Sanity control: both chunks healthy - no window-mismatch flag,
        transcript_status stays ok, not a pipeline gap."""
        chunk1_json = _chunk_json([10, 200, 400, 590])
        chunk2_json = _chunk_json([650, 800, 1000, 1190])
        responses = [json.dumps(chunk1_json), json.dumps(chunk2_json)]
        _stub_chunked_call_gemini(monkeypatch, responses)

        channel_dir = tmp_path / "demo"
        video = _video("hea11111111")
        status = vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix="2026-08-30-healthy-talk",
            chunks=[(0, 600), (600, 1200)],
            duration_seconds=1200,
            chunk_minutes=10,
            force=False,
        )

        meta_path = channel_dir / "2026-08-30-healthy-talk.meta.json"
        transcript_path = channel_dir / "2026-08-30-healthy-talk.transcript.md"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))

        assert status == "done"
        assert meta["transcript_status"] == "ok"
        assert "chunk_window_mismatch_severe" not in meta["transcript_quality_flags"]
        assert "chunk_window_mismatch_mild" not in meta["transcript_quality_flags"]
        assert meta["transcript_chunk_window_violations"] == 0

        quality_severe = transcript_quality_flags_are_severe(meta.get("transcript_quality_flags"))
        assert quality_severe is False
        steps = [
            {
                "label": "transcript",
                "requested": True,
                "status": status,
                "path": transcript_path,
                "quality_severe": quality_severe,
            }
        ]
        assert missing_pipeline_artifacts(steps) == []
