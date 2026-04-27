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
    def test_offsets_applied_to_all_transcripts(self):
        c1 = _chunk(
            speakers=[{"voice": 1, "name": "Lex"}],
            transcripts=[{"start": "00:30", "voice": 1, "text": "Hello."}],
        )
        c2 = _chunk(
            speakers=[{"voice": 1, "name": "Lex"}],
            transcripts=[{"start": "12:34", "voice": 1, "text": "Goodbye."}],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        starts = [t["start"] for t in merged["transcripts"]]
        assert starts == ["00:30", "1:02:34"]
        assert merged["transcripts"][1]["text"] == "Goodbye."

    def test_offsets_applied_to_screen_content_start_and_end(self):
        c1 = _chunk(speakers=[], transcripts=[], screen_content=[])
        c2 = _chunk(
            speakers=[],
            transcripts=[],
            screen_content=[{"start": "05:00", "end": "06:00", "type": "slide", "description": "Title"}],
        )
        merged = vi.merge_chunked_transcripts([(0, c1), (3000, c2)])
        sc = merged["screen_content"][0]
        assert sc["start"] == "55:00"
        assert sc["end"] == "56:00"

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

        responses = [
            {
                "transcripts": [{"start": "00:30", "voice": 1, "text": "chunk-1 hello"}],
                "speakers": [{"voice": 1, "name": "Lex"}],
                "screen_content": [],
            },
            {
                "transcripts": [{"start": "00:30", "voice": 1, "text": "chunk-2 hello"}],
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
        # Chunk-2's offset-applied timestamp is 1:00:30, not 00:30
        assert "1:00:30" in body
