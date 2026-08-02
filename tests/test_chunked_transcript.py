"""Tests for the chunked-transcript pipeline (issue #50).

Three units mirror the implementation plan:
  Unit 1: _build_transcript_chunks, _offset_timestamp helpers
  Unit 2: merge_chunked_transcripts (JSON-aware merge with offset + dedup)
  Unit 3: cmd_transcript URL chunking integration + cmd_process --url

Single test file mirroring tests/test_skip_long_videos.py precedent.
"""

from __future__ import annotations

import json
from argparse import Namespace

import pytest

import video_intel as vi

# ---------------------------------------------------------------------------
# Unit 1: chunk helpers
# ---------------------------------------------------------------------------


class TestBuildTranscriptChunks:
    def test_short_video_single_chunk_no_clipping(self):
        """Under chunk_minutes -> single (0, 0) signaling no clipping."""
        assert vi._build_transcript_chunks(duration_seconds=600, chunk_minutes=50) == [(0, 0)]

    def test_exactly_threshold_single_chunk(self):
        """Right at threshold -> single chunk, no clipping."""
        assert vi._build_transcript_chunks(duration_seconds=3000, chunk_minutes=50) == [(0, 0)]

    def test_over_threshold_uniform_chunks(self):
        """3h video at 50min chunks -> 4 chunks of 50/50/50/30 min."""
        chunks = vi._build_transcript_chunks(duration_seconds=10800, chunk_minutes=50)
        assert chunks == [(0, 3000), (3000, 6000), (6000, 9000), (9000, 10800)]

    def test_yfjfbk8hi5o_steinberger_real_case(self):
        """The user's actual target: 3h15m52s = 11752s at default 50 min chunks."""
        chunks = vi._build_transcript_chunks(duration_seconds=11752, chunk_minutes=50)
        assert chunks == [(0, 3000), (3000, 6000), (6000, 9000), (9000, 11752)]
        assert len(chunks) == 4

    def test_chunk_minutes_validation(self):
        with pytest.raises(ValueError):
            vi._build_transcript_chunks(duration_seconds=10000, chunk_minutes=0)

    def test_negative_duration(self):
        """Defensive: negative or zero duration treated as single chunk."""
        assert vi._build_transcript_chunks(duration_seconds=0, chunk_minutes=50) == [(0, 0)]


class TestOffsetTimestamp:
    def test_mm_ss_under_one_hour(self):
        """05:30 + 0 offset -> 05:30."""
        assert vi._offset_timestamp("05:30", 0) == "05:30"

    def test_mm_ss_with_offset_pushes_into_hours(self):
        """05:30 + 1h offset -> 1:05:30."""
        assert vi._offset_timestamp("05:30", 3600) == "1:05:30"

    def test_hh_mm_ss_with_offset(self):
        """1:30:00 + 1h offset -> 2:30:00."""
        assert vi._offset_timestamp("1:30:00", 3600) == "2:30:00"

    def test_mm_ss_with_50min_chunk_offset(self):
        """Chunk-2 (offset=3000s/50min) timestamp 12:34 -> 1:02:34."""
        assert vi._offset_timestamp("12:34", 3000) == "1:02:34"

    def test_zero_offset_passthrough_for_hh_mm_ss(self):
        assert vi._offset_timestamp("2:30:45", 0) == "2:30:45"

    def test_returns_input_for_unparseable(self):
        """Pass through gibberish unchanged - merger logs but does not raise."""
        assert vi._offset_timestamp("not-a-timestamp", 1800) == "not-a-timestamp"


class TestClassifyAndOffsetTimestampNormalization:
    """Regression tests for issue #58 - normalize_timestamp pre-pass.

    Tucker/Sachs chunk 3 (chunk_start=6000s, chunk_duration=3000s) produced
    [100:08:57]-style timestamps where Gemini packed total minutes into the
    HH field. Without the normalize_timestamp pre-pass added in this issue,
    the classifier saw 100 hours, flagged "Implausible timestamp", and passed
    the corruption through to disk.
    """

    def test_tucker_chunk3_100_08_57_normalizes_and_classifies_as_absolute(self):
        # 100 minutes = 1h40m chunk start + 8m = 1h48m, 57 sec.
        # Should classify as ABSOLUTE (already inside the chunk window after
        # normalization), no warning.
        result = vi._classify_and_offset_timestamp("100:08:57", chunk_start_secs=6000, chunk_duration_secs=3000)
        assert result == "1:48:57"

    def test_tucker_chunk3_100_00_00_normalizes_to_chunk_start(self):
        # 100 minutes = exactly chunk start (1h40m).
        result = vi._classify_and_offset_timestamp("100:00:00", chunk_start_secs=6000, chunk_duration_secs=3000)
        assert result == "1:40:00"

    def test_tucker_chunk3_100_22_35_normalizes_to_chunk_end(self):
        # 100 minutes + 22 = 2h02m, 35 sec - the actual end of Tucker chunk 3.
        result = vi._classify_and_offset_timestamp("100:22:35", chunk_start_secs=6000, chunk_duration_secs=3000)
        assert result == "2:02:35"

    def test_already_normal_absolute_unchanged(self):
        # Sanity: clean input with proper HH:MM:SS still classifies as absolute.
        result = vi._classify_and_offset_timestamp("1:48:57", chunk_start_secs=6000, chunk_duration_secs=3000)
        assert result == "1:48:57"

    def test_relative_chunk_input_gets_offset_applied(self):
        # Sanity: 8:57 (relative within chunk) gets chunk_start added.
        result = vi._classify_and_offset_timestamp("08:57", chunk_start_secs=6000, chunk_duration_secs=3000)
        assert result == "1:48:57"

    def test_already_bracketed_input_does_not_corrupt(self):
        # Defensive contract test (issue #58 review feedback): if a caller
        # ever passes already-bracketed input, the wrap-and-strip pre-pass
        # must not double-bracket and lose data. The bracket-stripping at
        # entry guarantees the result is bracket-free regardless of branch.
        result = vi._classify_and_offset_timestamp("[1:30:00]", chunk_start_secs=0, chunk_duration_secs=600)
        assert "[" not in result and "]" not in result


class TestMakeThinkingConfigForTranscript:
    """Issue #58 Gate 2 mitigation: chunked transcript path constrains the
    Gemini thinking budget so dynamic-thinking can't stochastically consume
    output budget. Model-aware because Gemini 2.x and 3.x have different
    thinking-control APIs."""

    def _types_stub(self):
        # Minimal stand-in for google-genai types.ThinkingConfig — captures
        # the kwargs so we can assert on what was constructed without a real
        # SDK import.
        captured: list[dict] = []

        class _ThinkingConfig:
            def __init__(self, **kwargs):
                captured.append(kwargs)
                self.kwargs = kwargs

        types = type("Types", (), {"ThinkingConfig": _ThinkingConfig})
        return types, captured

    def test_gemini_3_flash_uses_minimal_level(self):
        # Confirmed via ai.google.dev/gemini-api/docs/thinking and Firebase AI
        # Logic guide: gemini-3-flash-preview supports MINIMAL/LOW/MEDIUM/HIGH.
        # MINIMAL is Flash-exclusive and the lowest available level.
        types, captured = self._types_stub()
        result = vi._make_thinking_config_for_transcript(types, "gemini-3-flash-preview")
        assert result is not None
        assert captured[-1] == {"thinking_level": "minimal"}

    def test_gemini_3_pro_uses_low_level(self):
        # Pro variants don't have MINIMAL; LOW is the lowest available.
        types, captured = self._types_stub()
        result = vi._make_thinking_config_for_transcript(types, "gemini-3-pro-preview")
        assert result is not None
        assert captured[-1] == {"thinking_level": "low"}

    def test_gemini_2_5_flash_disables_thinking_with_budget_zero(self):
        # 2.5 Flash range is 0-24576; 0 disables thinking entirely.
        types, captured = self._types_stub()
        result = vi._make_thinking_config_for_transcript(types, "gemini-2.5-flash")
        assert result is not None
        assert captured[-1] == {"thinking_budget": 0}

    def test_gemini_2_5_pro_uses_minimum_budget_128(self):
        # 2.5 Pro cannot disable thinking; 128 is the documented minimum.
        types, captured = self._types_stub()
        result = vi._make_thinking_config_for_transcript(types, "gemini-2.5-pro")
        assert result is not None
        assert captured[-1] == {"thinking_budget": 128}

    def test_unknown_model_returns_none(self):
        # Don't 400 on a model we don't recognize - let the SDK default apply.
        types, _captured = self._types_stub()
        result = vi._make_thinking_config_for_transcript(types, "gemini-future-9000")
        assert result is None


class TestAssessChunkCoverage:
    """Issue #58 Gate 2 sanity check: detect chunks that returned valid JSON
    but transcribed only a fraction of their allotted time window."""

    def test_full_coverage_is_ok(self):
        # 50-minute chunk where Gemini transcribed entries spanning the full
        # window (~50 min observed span).
        parsed = {
            "transcripts": [
                {"start": "00:30"},
                {"start": "25:00"},
                {"start": "49:30"},
            ]
        }
        result = vi._assess_chunk_coverage(parsed, start_secs=3000, end_secs=6000, duration_seconds=11752, chunk_idx=2)
        assert result == "ok"

    def test_tucker_chunk2_thin_case_flagged(self):
        # Real-input regression: chunk 2 covered only 50:00-53:10 of its
        # 50:00-1:40:00 (3000s) allotted window. Observed span ~190s = 6.3%.
        parsed = {
            "transcripts": [
                {"start": "00:00"},
                {"start": "01:30"},
                {"start": "03:10"},
            ]
        }
        result = vi._assess_chunk_coverage(parsed, start_secs=3000, end_secs=6000, duration_seconds=7355, chunk_idx=2)
        assert result == "thin"

    def test_empty_transcripts_list_flagged_thin(self):
        result = vi._assess_chunk_coverage(
            {"transcripts": []}, start_secs=3000, end_secs=6000, duration_seconds=7355, chunk_idx=2
        )
        assert result == "thin"

    def test_single_timestamp_cannot_measure_span_flagged_thin(self):
        # Need at least 2 timestamps to compute a span.
        parsed = {"transcripts": [{"start": "00:30"}]}
        result = vi._assess_chunk_coverage(parsed, start_secs=3000, end_secs=6000, duration_seconds=7355, chunk_idx=2)
        assert result == "thin"

    def test_single_chunk_video_skips_check(self):
        # When there's no chunking (start=0, end=0), the allotted window is
        # the whole video — coverage check would be ill-defined.
        parsed = {"transcripts": [{"start": "00:30"}, {"start": "01:00"}]}
        result = vi._assess_chunk_coverage(parsed, start_secs=0, end_secs=0, duration_seconds=600, chunk_idx=1)
        assert result == "ok"

    def test_50_percent_threshold_boundary(self):
        # Boundary case: exactly 50% of allotted span = "ok".
        parsed = {"transcripts": [{"start": "00:00"}, {"start": "25:00"}]}
        # 1500s observed / 3000s allotted = 50% exactly — passes.
        result = vi._assess_chunk_coverage(parsed, start_secs=3000, end_secs=6000, duration_seconds=7355, chunk_idx=2)
        assert result == "ok"


# ---------------------------------------------------------------------------
# Unit 2: merge_chunked_transcripts
# ---------------------------------------------------------------------------


def _chunk(speakers, transcripts, screen_content=None):
    return {
        "speakers": speakers,
        "transcripts": transcripts,
        "screen_content": screen_content or [],
    }


class TestMergeChunkedTranscripts:
    def test_relative_timestamps_get_offset_applied(self):
        """When Gemini returns chunk-relative timestamps (e.g. '12:34' for
        content 12 min into chunk 2), the classifier detects the value is
        within chunk_duration tolerance of zero and adds the offset."""
        c1 = _chunk(
            speakers=[{"voice": 1, "name": "Lex"}],
            transcripts=[{"start": "00:30", "voice": 1, "text": "first chunk"}],
        )
        # Chunk 2 (start=3000) Gemini returned relative "12:34"
        c2 = _chunk(
            speakers=[{"voice": 1, "name": "Lex"}],
            transcripts=[{"start": "12:34", "voice": 1, "text": "second chunk"}],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        starts = [t["start"] for t in merged["transcripts"]]
        # 12:34 in chunk 2 (offset=3000s) classified as relative, becomes
        # "1:02:34" (3000s + 754s = 3754s = 1h2m34s).
        assert starts == ["00:30", "1:02:34"]

    def test_absolute_timestamps_not_double_offset(self):
        """When Gemini returns ABSOLUTE timestamps (e.g. '1:02:34' already
        meaning 1h2m34s of full video for chunk 2), the classifier detects
        the value falls in [chunk_start, chunk_start + chunk_duration]
        and leaves it alone."""
        c1 = _chunk(
            speakers=[{"voice": 1, "name": "Lex"}],
            transcripts=[{"start": "00:30", "voice": 1, "text": "first chunk"}],
        )
        # Chunk 2 (start=3000) Gemini returned absolute "1:02:34" already.
        c2 = _chunk(
            speakers=[{"voice": 1, "name": "Lex"}],
            transcripts=[{"start": "1:02:34", "voice": 1, "text": "second chunk"}],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        starts = [t["start"] for t in merged["transcripts"]]
        # Absolute already - no double-offset. 1:02:34 stays 1:02:34.
        assert starts == ["00:30", "1:02:34"]

    def test_implausible_timestamps_pass_through_with_warning(self, caplog):
        """Timestamps that fit neither classification (e.g. '5:30:00' for
        chunk 2 covering 50:00-1:40:00) get logged and passed through."""
        c1 = _chunk(speakers=[], transcripts=[])
        c2 = _chunk(
            speakers=[{"voice": 1, "name": "X"}],
            transcripts=[{"start": "5:30:00", "voice": 1, "text": "way past chunk 2"}],
        )
        with caplog.at_level("WARNING"):
            merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        assert merged["transcripts"][0]["start"] == "5:30:00"
        assert "Implausible timestamp" in "\n".join(r.message for r in caplog.records)

    def test_screen_content_classifier_handles_both_modes(self):
        c1 = _chunk(speakers=[], transcripts=[], screen_content=[])
        # Chunk 2 covers 50:00-1:40:00 absolute. SCREEN content emitted
        # absolute "55:00-56:00".
        c2_absolute = _chunk(
            speakers=[],
            transcripts=[],
            screen_content=[{"start": "55:00", "end": "56:00", "type": "slide", "description": "Title"}],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2_absolute)])
        sc = merged["screen_content"][0]
        assert sc["start"] == "55:00"
        assert sc["end"] == "56:00"
        # Same chunk if Gemini emitted relative "05:00-06:00" instead -
        # classifier should also produce "55:00-56:00".
        c2_relative = _chunk(
            speakers=[],
            transcripts=[],
            screen_content=[{"start": "05:00", "end": "06:00", "type": "slide", "description": "Title"}],
        )
        merged2 = vi.merge_chunked_transcripts([(0, c1), (3000, c2_relative)])
        sc2 = merged2["screen_content"][0]
        assert sc2["start"] == "55:00"
        assert sc2["end"] == "56:00"

    def test_speakers_dedup_by_name_across_chunks(self):
        """Lex appears in both chunks; merged should have exactly one Lex record
        with a globally unique voice id."""
        c1 = _chunk(
            speakers=[{"voice": 1, "name": "Lex Fridman"}],
            transcripts=[{"start": "00:30", "voice": 1, "text": "From Lex chunk 1"}],
        )
        c2 = _chunk(
            speakers=[{"voice": 1, "name": "Lex Fridman"}, {"voice": 2, "name": "Peter"}],
            transcripts=[
                {"start": "01:00", "voice": 1, "text": "From Lex chunk 2"},
                {"start": "01:30", "voice": 2, "text": "From Peter"},
            ],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        names = sorted(s["name"] for s in merged["speakers"])
        assert names == ["Lex Fridman", "Peter"]
        # Both Lex transcript entries share the same voice id in merged output
        lex_voice = next(s["voice"] for s in merged["speakers"] if s["name"] == "Lex Fridman")
        peter_voice = next(s["voice"] for s in merged["speakers"] if s["name"] == "Peter")
        assert lex_voice != peter_voice
        for t in merged["transcripts"]:
            if "Peter" in t["text"]:
                assert t["voice"] == peter_voice
            else:
                assert t["voice"] == lex_voice

    def test_voice_collision_across_chunks_resolved_by_name(self):
        """Chunk 1's voice=1 is Lex, chunk 2's voice=1 is Peter (Gemini renumbered).
        Merger must NOT collapse them into the same speaker."""
        c1 = _chunk(
            speakers=[{"voice": 1, "name": "Lex"}],
            transcripts=[{"start": "00:30", "voice": 1, "text": "Lex talking"}],
        )
        c2 = _chunk(
            speakers=[{"voice": 1, "name": "Peter"}],
            transcripts=[{"start": "01:00", "voice": 1, "text": "Peter talking"}],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        names = sorted(s["name"] for s in merged["speakers"])
        assert names == ["Lex", "Peter"]
        # The two transcript lines must have DIFFERENT voice ids despite both
        # being voice=1 in their original chunks.
        lex_line = next(t for t in merged["transcripts"] if "Lex" in t["text"])
        peter_line = next(t for t in merged["transcripts"] if "Peter" in t["text"])
        assert lex_line["voice"] != peter_line["voice"]

    def test_empty_chunk_list_returns_empty_merged(self):
        merged = vi.merge_chunked_transcripts([])
        assert merged == {"transcripts": [], "screen_content": [], "speakers": []}

    def test_chronological_order_preserved(self):
        """transcripts list across all chunks must preserve chronological order
        once offsets are applied."""
        c1 = _chunk(
            speakers=[{"voice": 1, "name": "X"}],
            transcripts=[{"start": "00:30", "voice": 1, "text": "first"}],
        )
        c2 = _chunk(
            speakers=[{"voice": 1, "name": "X"}],
            transcripts=[{"start": "00:30", "voice": 1, "text": "second"}],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        texts = [t["text"] for t in merged["transcripts"]]
        assert texts == ["first", "second"]


# ---------------------------------------------------------------------------
# Unit 3: cmd_transcript URL chunking integration
# ---------------------------------------------------------------------------


def _stub_gemini_calls(monkeypatch, chunk_responses):
    """Capture each Gemini transcript call and return the corresponding stub
    JSON string from chunk_responses (list, indexed by call order)."""
    calls = []

    def fake_call_gemini(client, types, media_uri, prompt, model, response_json=False, **kw):
        idx = len(calls)
        calls.append({"start": kw.get("start_offset"), "end": kw.get("end_offset"), "media_uri": media_uri})
        if idx < len(chunk_responses):
            return json.dumps(chunk_responses[idx])
        return json.dumps({"transcripts": [], "speakers": [], "screen_content": []})

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    return calls


def _transcript_url_args(url, **overrides):
    base = {
        "url": url,
        "file": None,
        "channel": "ch",
        "video_id": None,
        "title": "Test Video",
        "date": "2026-04-15",
        "start": None,
        "end": None,
        "force": False,
        "model": None,
        "chunk_minutes": 50,
    }
    base.update(overrides)
    return Namespace(**base)


class TestCmdTranscriptUrlChunking:
    def test_under_threshold_runs_single_call(self, tmp_path, monkeypatch):
        """For a 30-min video, no chunking; one Gemini call."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: MagicMock())
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr(vi, "load_prompt", lambda *_a, **_kw: "transcript prompt")
        monkeypatch.setattr(
            vi,
            "_lookup_video_duration_seconds",
            lambda *_a, **_kw: 1800,  # 30 min
        )
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda *_a, **_kw: False)

        chunk_calls = _stub_gemini_calls(
            monkeypatch,
            [{"transcripts": [{"start": "00:10", "voice": 1, "text": "x"}], "speakers": [{"voice": 1, "name": "S"}]}],
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        vi.cmd_transcript(_transcript_url_args("https://www.youtube.com/watch?v=under1234567"), config)

        assert len(chunk_calls) == 1, "Under-threshold video must run as single call"
        assert chunk_calls[0]["start"] is None and chunk_calls[0]["end"] is None

    def test_over_threshold_chunks_match_build_chunk_list(self, tmp_path, monkeypatch):
        """3h15m52s video at 50min chunks -> 4 Gemini calls with the exact
        --start/--end pairs from _build_transcript_chunks."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: MagicMock())
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr(vi, "load_prompt", lambda *_a, **_kw: "transcript prompt")
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda *_a, **_kw: 11752)
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda *_a, **_kw: False)

        chunk_response_template = {"transcripts": [], "speakers": [{"voice": 1, "name": "S"}], "screen_content": []}
        chunk_calls = _stub_gemini_calls(
            monkeypatch,
            [chunk_response_template, chunk_response_template, chunk_response_template, chunk_response_template],
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "lexfridman", "url": "https://example.com/lex"}],
        }
        args = _transcript_url_args(
            "https://www.youtube.com/watch?v=YFjfBk8HI5o",
            channel="lexfridman",
        )
        vi.cmd_transcript(args, config)

        # 4 chunks expected
        assert len(chunk_calls) == 4
        starts = [c["start"] for c in chunk_calls]
        ends = [c["end"] for c in chunk_calls]
        assert starts == [0, 3000, 6000, 9000]
        assert ends == [3000, 6000, 9000, 11752]

    def test_chunked_output_writes_single_transcript_md_with_coverage(self, tmp_path, monkeypatch):
        """End-to-end: 4 chunks -> single .transcript.md with coverage table."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: MagicMock())
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr(vi, "load_prompt", lambda *_a, **_kw: "transcript prompt")
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda *_a, **_kw: 7200)
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda *_a, **_kw: False)

        responses = [
            {
                # Chunk 1 covers 0-1h absolute. Gemini returns absolute
                # timestamps; for a 0-1h chunk, "00:30" is at 30s of video.
                "transcripts": [{"start": "00:30", "voice": 1, "text": "chunk-1 hello"}],
                "speakers": [{"voice": 1, "name": "Lex"}],
                "screen_content": [],
            },
            {
                # Chunk 2 covers 1h-2h absolute. Real Gemini returns absolute
                # timestamps (e.g. "1:00:30" not "00:30") - that's the
                # Gate-1 finding. Merger preserves them as-is.
                "transcripts": [{"start": "1:00:30", "voice": 1, "text": "chunk-2 hello"}],
                "speakers": [{"voice": 1, "name": "Lex"}],
                "screen_content": [],
            },
        ]
        _stub_gemini_calls(monkeypatch, responses)

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "lex", "url": "https://example.com/lex"}],
        }
        args = _transcript_url_args(
            "https://www.youtube.com/watch?v=long1234567",
            channel="lex",
            chunk_minutes=60,  # 60-min chunks for a 2h video -> 2 chunks
        )
        vi.cmd_transcript(args, config)

        # transcript.md should land under the channel folder
        outputs = list((tmp_path / "lex").glob("*.transcript.md"))
        assert len(outputs) == 1
        body = outputs[0].read_text(encoding="utf-8")
        assert "chunk-1 hello" in body
        assert "chunk-2 hello" in body
        # Coverage table heading must be present
        assert "chunked transcript" in body.lower() or "Coverage" in body
        # Chunk-2's already-absolute timestamp passes through.
        assert "1:00:30" in body
        # And critically: chunk-1's "00:30" must NOT have been doubled
        # into "00:30 + 0" (still 00:30) - just confirms it's there.
        assert "00:30" in body


# ---------------------------------------------------------------------------
# Unit 4: process --url orchestrator (mindmap + transcript + concepts)
# ---------------------------------------------------------------------------


def _process_url_args(url, **overrides):
    base = {
        "url": url,
        "file": None,
        "channel": "ch",
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


class TestCmdProcessUrl:
    def test_url_orchestrates_mindmap_transcript_concepts(self, tmp_path, monkeypatch):
        """process --url runs mindmap, then transcript (single-call here, under
        threshold), then concepts. All three steps fire on one invocation."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: MagicMock())
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr(vi, "load_prompt", lambda *_a, **_kw: "prompt")
        monkeypatch.setattr(vi, "load_taxonomy", lambda *_a, **_kw: {"concepts": {}})
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda *_a, **_kw: 1800)
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda *_a, **_kw: False)

        calls = {"mindmap": 0, "transcript": 0, "concepts": 0}

        def fake_mindmap(*args, **kwargs):
            calls["mindmap"] += 1
            video = args[2]
            ch = args[6] if len(args) > 6 else kwargs.get("channel_name")
            prefix = video_file_prefix_for_test(video)
            (tmp_path / ch).mkdir(parents=True, exist_ok=True)
            (tmp_path / ch / f"{prefix}.mindmap.md").write_text("# stub mindmap", encoding="utf-8")
            return prefix, "done"

        def video_file_prefix_for_test(video):
            return vi.video_file_prefix(video)

        def fake_transcript(*args, **kwargs):
            calls["transcript"] += 1
            return args[6] if len(args) > 6 else kwargs.get("prefix"), "done"

        def fake_concepts(*args, **kwargs):
            calls["concepts"] += 1
            return kwargs.get("prefix") or "p", "done"

        monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)
        monkeypatch.setattr(vi, "process_transcript", fake_transcript)
        monkeypatch.setattr(vi, "process_concepts", fake_concepts)

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        vi.cmd_process(_process_url_args("https://www.youtube.com/watch?v=ZZZ12345678"), config)

        assert calls["mindmap"] == 1
        assert calls["transcript"] == 1
        assert calls["concepts"] == 1

    def test_url_with_long_video_uses_chunked_transcript(self, tmp_path, monkeypatch):
        """process --url on a 3h video calls process_mindmap once (mindmap is
        always single-call) and call_gemini multiple times for chunked
        transcript."""
        from unittest.mock import MagicMock

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: MagicMock())
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr(vi, "load_prompt", lambda *_a, **_kw: "prompt")
        monkeypatch.setattr(vi, "load_taxonomy", lambda *_a, **_kw: {"concepts": {}})
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda *_a, **_kw: 11752)
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda *_a, **_kw: False)

        # Mindmap stub - writes a fake artifact so concepts step finds something
        def fake_mindmap(*args, **kwargs):
            video = args[2]
            ch = args[6] if len(args) > 6 else kwargs.get("channel_name")
            prefix = vi.video_file_prefix(video)
            (tmp_path / ch).mkdir(parents=True, exist_ok=True)
            (tmp_path / ch / f"{prefix}.mindmap.md").write_text("# stub", encoding="utf-8")
            return prefix, "done"

        monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)
        monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: ("p", "done"))

        chunk_response = {"transcripts": [], "speakers": [{"voice": 1, "name": "S"}], "screen_content": []}
        chunk_calls = _stub_gemini_calls(monkeypatch, [chunk_response] * 4)

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "lex", "url": "https://example.com/lex"}],
        }
        args = _process_url_args("https://www.youtube.com/watch?v=YFjfBk8HI5o", channel="lex")
        vi.cmd_process(args, config)

        # 4 chunks invoked for transcript (mindmap goes through process_mindmap stub, not call_gemini)
        assert len(chunk_calls) == 4
        starts = [c["start"] for c in chunk_calls]
        assert starts == [0, 3000, 6000, 9000]
