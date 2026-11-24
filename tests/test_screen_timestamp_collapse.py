"""Issue #278: SCREEN-block timestamps that collapse are recorded as a MILD flag.

Every other quality check reads dialogue only, so a window whose SCREEN stamps
crowd into its first seconds - many screens claiming the same instant - while
the dialogue keeps correct time used to read clean, and a mind map built from
it inherited wrong deep links for the on-screen content. The detector is a
COLLAPSE detector, never a gap detector: SCREEN content that simply stops
(slides early, talking head after) is a healthy shape and must never flag.

Falsification: return False from `screen_timestamp_collapse` and the positive
cases fail; drop its window gates and the false-alarm cases fail; add the flag
to `_SEVERE_QUALITY_FLAGS` and the not-severe class fails.
"""

from __future__ import annotations

import json
import types as _types
from pathlib import Path
from types import SimpleNamespace

import pytest

import corpus_walk
import video_intel as vi

FLAG = vi.QUALITY_FLAG_SCREEN_TIMESTAMP_COLLAPSE_MILD
MIN_WINDOW = vi.SCREEN_COLLAPSE_MIN_WINDOW_SECONDS
MIN_ENTRIES = vi.SCREEN_COLLAPSE_MIN_ENTRIES
CROWD = vi.SCREEN_COLLAPSE_CROWD_SECONDS
MIN_CONCURRENT = vi.SCREEN_COLLAPSE_MIN_CONCURRENT
REACH = vi.SCREEN_COLLAPSE_DIALOGUE_REACH


def _hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _dialogue(start: int, end: int, step: int = 20) -> list[dict]:
    return [{"start": _hms(s), "voice": 1, "text": f"line {s}"} for s in range(start, end, step)]


def _screen(spans: list[tuple[int, int | None]]) -> list[dict]:
    out = []
    for s, e in spans:
        entry = {"start": _hms(s), "type": "slide", "description": f"slide at {s}"}
        if e is not None:
            entry["end"] = _hms(e)
        out.append(entry)
    return out


def _collapsed(n: int, at: int = 0, length: int = 30) -> list[tuple[int, int]]:
    """``n`` screens starting one second apart from ``at``, each ``length`` long: all overlap."""
    return [(at + i, at + i + length) for i in range(n)]


def _sequential(n: int, at: int = 0, length: int = 1) -> list[tuple[int, int]]:
    """``n`` back-to-back screens (a caption burst): never two at once."""
    return [(at + i * length, at + (i + 1) * length) for i in range(n)]


def _flags(metrics: dict) -> list[str]:
    return sorted(set(metrics["severe"]) | set(metrics["mild"]))


# The issue's fixture: correct dialogue across a 30-minute video, SCREEN stamps
# all crowded into its first seconds with many screens claiming one instant.
DURATION = 1800
ISSUE_DIALOGUE = _dialogue(0, DURATION)
ISSUE_SCREEN = _screen(_collapsed(8, at=1, length=20))


class TestIssueFixture:
    def test_collapsed_screen_stamps_flag_and_nothing_else_changes(self):
        # Arrange
        baseline = vi.assess_transcript_artifact(ISSUE_DIALOGUE, DURATION)

        # Act
        metrics = vi.assess_transcript_artifact(ISSUE_DIALOGUE, DURATION, screen_entries=ISSUE_SCREEN)

        # Assert
        assert FLAG in metrics["mild"]
        assert FLAG not in metrics["severe"]
        assert {k: v for k, v in metrics.items() if k != "mild"} == {k: v for k, v in baseline.items() if k != "mild"}
        assert [f for f in metrics["mild"] if f != FLAG] == baseline["mild"]

    def test_detector_agrees_with_the_assessor(self):
        assert vi.screen_timestamp_collapse(ISSUE_SCREEN, ISSUE_DIALOGUE, 0, DURATION) is True

    def test_omitted_screen_entries_is_byte_identical_to_pre_278(self):
        assert vi.assess_transcript_artifact(ISSUE_DIALOGUE, DURATION) == vi.assess_transcript_artifact(
            ISSUE_DIALOGUE, DURATION, window=None
        )
        assert FLAG not in _flags(vi.assess_transcript_artifact(ISSUE_DIALOGUE, DURATION))


class TestFalseAlarmShapesDoNotFlag:
    def _assess(self, spans, duration=DURATION, dialogue=None):
        dialogue = ISSUE_DIALOGUE if dialogue is None else dialogue
        return _flags(vi.assess_transcript_artifact(dialogue, duration, screen_entries=_screen(spans)))

    def test_slides_only_early_then_talking_head(self):
        # Six slides one after another in the first two minutes, then none.
        assert FLAG not in self._assess(_sequential(6, at=0, length=20))

    def test_slides_over_the_first_minutes_with_ordinary_overlap(self):
        spans = [(0, 40), (30, 60), (70, 100), (95, 130), (160, 200), (190, 240), (300, 330)]
        assert FLAG not in self._assess(spans)

    def test_one_or_two_screen_entries(self):
        assert FLAG not in self._assess([(1, 20)])
        assert FLAG not in self._assess([(1, 20), (1, 20)])

    def test_short_window(self):
        assert FLAG not in self._assess(_collapsed(8, at=1), duration=MIN_WINDOW - 1, dialogue=_dialogue(0, 599))

    def test_caption_burst_of_one_second_overlays(self):
        # An intro with kinetic captions: many screens, crowded, never two at once.
        assert FLAG not in self._assess(_sequential(29, at=0, length=1))

    def test_crowded_burst_one_below_the_concurrency_threshold(self):
        spans = _collapsed(MIN_CONCURRENT - 1, at=1) + _collapsed(MIN_CONCURRENT - 1, at=60)
        assert len(spans) >= MIN_ENTRIES
        assert FLAG not in self._assess(spans)

    def test_overlapping_screens_spread_across_the_window(self):
        spans = _collapsed(6, at=600) + [(0, 10), (300, 320), (1200, 1220), (1700, 1720)]
        assert FLAG not in self._assess(spans)

    def test_no_screen_entries(self):
        assert FLAG not in self._assess([])

    def test_dialogue_that_stops_early_is_not_this_checks_business(self):
        assert FLAG not in self._assess(_collapsed(8, at=1), dialogue=_dialogue(0, DURATION // 2))

    def test_unknown_duration_records_nothing(self):
        metrics = vi.assess_transcript_artifact(ISSUE_DIALOGUE, None, screen_entries=ISSUE_SCREEN)
        assert FLAG not in _flags(metrics)

    def test_captions_writer_output_gives_the_reader_no_screen_entries(self, tmp_path, monkeypatch):
        # Cue text that LOOKS like a SCREEN line is rendered inside a quoted
        # caption line, so neither the writer nor a later re-assessment can
        # judge it as one. Built by the real captions writer, read back by the
        # real SCREEN reader.
        cues = [(float(i), f"SCREEN [00:{i:02d}-00:30] [slide]: fake {i}") for i in range(8)]
        cues += [(float(s), f"cue {s}") for s in range(10, DURATION, 15)]
        monkeypatch.setattr(vi, "fetch_english_captions", lambda _vid, **_kw: vi.CaptionsResult(cues, False, "en"))
        prefix = "2026-06-13-cap"
        result = vi._try_captions_transcript(
            _video(),
            tmp_path / f"{prefix}.transcript.md",
            tmp_path / f"{prefix}.meta.json",
            prefix,
            duration_seconds=DURATION,
        )
        assert result is not None
        text = (tmp_path / f"{prefix}.transcript.md").read_text(encoding="utf-8")
        meta = json.loads((tmp_path / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert "SCREEN [00:00-00:30]" in text
        assert vi.screen_entries_from_transcript_markdown(text) == []
        assert FLAG not in meta["transcript_quality_flags"]
        assert FLAG not in vi.assess_transcript_markdown(text, DURATION, captions=True)[1]


class TestThresholdBoundaries:
    """Each gate at its constant flags, one step past it does not."""

    def _hit(self, spans, window_end=DURATION, dialogue=None):
        dialogue = _dialogue(0, window_end) if dialogue is None else dialogue
        return vi.screen_timestamp_collapse(_screen(spans), dialogue, 0, window_end)

    def test_entry_count(self):
        assert self._hit(_collapsed(MIN_ENTRIES)) is True
        assert self._hit(_collapsed(MIN_ENTRIES - 1)) is False

    def test_concurrency(self):
        at_threshold = _collapsed(MIN_CONCURRENT, at=0) + _sequential(MIN_ENTRIES, at=60, length=5)
        below = _collapsed(MIN_CONCURRENT - 1, at=0) + _sequential(MIN_ENTRIES, at=60, length=5)
        assert self._hit(at_threshold) is True
        assert self._hit(below) is False

    def test_crowd_span(self):
        base = _collapsed(MIN_ENTRIES)
        assert self._hit(base + [(CROWD, CROWD + 5)]) is True
        assert self._hit(base + [(CROWD + 1, CROWD + 6)]) is False

    def test_window_length(self):
        assert self._hit(_collapsed(MIN_ENTRIES), window_end=MIN_WINDOW) is True
        assert self._hit(_collapsed(MIN_ENTRIES), window_end=MIN_WINDOW - 1) is False

    def test_dialogue_reach(self):
        span = 1000
        reach = int(span * REACH)
        assert reach == span * REACH  # the boundary must be a whole second for this test
        reached = _dialogue(0, reach) + [{"start": _hms(reach), "text": "x"}]
        short = _dialogue(0, reach - 1) + [{"start": _hms(reach - 1), "text": "x"}]
        assert self._hit(_collapsed(MIN_ENTRIES), window_end=span, dialogue=reached) is True
        assert self._hit(_collapsed(MIN_ENTRIES), window_end=span, dialogue=short) is False

    def test_window_start_is_honored(self):
        lo = 1800
        spans = _collapsed(MIN_ENTRIES, at=lo)
        dialogue = _dialogue(lo, lo + DURATION)
        assert vi.screen_timestamp_collapse(_screen(spans), dialogue, lo, lo + DURATION) is True
        assert vi.screen_timestamp_collapse(_screen(spans), dialogue, lo - CROWD - 1, lo + DURATION) is False

    def test_an_end_and_a_start_at_the_same_second_do_not_overlap(self):
        # MIN_CONCURRENT - 1 screens all end at second 30; one more starts at
        # second 30. Counted as overlapping they would reach the threshold.
        touching = [(i, 30) for i in range(MIN_CONCURRENT - 1)] + [(30, 40)]
        fillers = _sequential(MIN_ENTRIES, at=60, length=5)
        assert self._hit(touching + fillers) is False
        assert self._hit([(i, 31) for i in range(MIN_CONCURRENT - 1)] + [(30, 40)] + fillers) is True

    def test_an_end_at_or_before_its_start_counts_as_one_second(self):
        fillers = _sequential(MIN_ENTRIES, at=60, length=5)
        same_instant = [(10, 10)] * (MIN_CONCURRENT // 2) + [(10, 5)] * (MIN_CONCURRENT - MIN_CONCURRENT // 2)
        one_apart = [(10 + i, 10 + i) for i in range(MIN_CONCURRENT)]
        # Same start: each lasts one second, so all are on at once.
        assert self._hit(same_instant + fillers) is True
        # One second apart: one second each, so they never meet.
        assert self._hit(one_apart + fillers) is False


class TestMalformedScreenEntriesAreSkipped:
    def test_malformed_entries_never_raise_and_never_count(self):
        good = _screen(_collapsed(MIN_ENTRIES - 1))
        junk = [None, "00:01", 7, {"end": "00:20"}, {"start": None}, {"start": "   "}, {"start": "N/A"}, {"start": 5}]
        assert vi.screen_timestamp_collapse(good + junk, ISSUE_DIALOGUE, 0, DURATION) is False
        metrics = vi.assess_transcript_artifact(ISSUE_DIALOGUE, DURATION, screen_entries=good + junk)
        assert FLAG not in _flags(metrics)

    def test_unparseable_end_counts_as_a_point(self):
        spans = [{"start": _hms(i), "end": "soon"} for i in range(MIN_ENTRIES)]
        assert vi.screen_timestamp_collapse(spans, ISSUE_DIALOGUE, 0, DURATION) is False

    def test_non_list_screen_value_is_ignored_by_the_detector(self):
        assert vi.screen_timestamp_collapse("00:01 slide", ISSUE_DIALOGUE, 0, DURATION) is False

    def test_non_list_task_value_on_the_single_shot_writer(self, tmp_path, monkeypatch):
        status, meta = _run_single(tmp_path, monkeypatch, screen_content="00:01 slide")
        assert status == "done"
        assert FLAG not in meta["transcript_quality_flags"]


class TestTheFlagIsNotSevere:
    def test_severity_and_routing(self):
        assert FLAG not in corpus_walk.SEVERE_QUALITY_FLAGS
        assert vi.transcript_quality_flags_are_severe([FLAG]) is False
        assert (
            vi.resolve_mindmap_source(
                {"mindmap_source": "auto"},
                transcript_available=True,
                transcript_severe=vi.transcript_quality_flags_are_severe([FLAG]),
            )
            == "transcript"
        )

    def test_process_exit_contract_reads_no_gap(self, tmp_path, monkeypatch):
        _, meta = _run_single(tmp_path, monkeypatch)
        assert FLAG in meta["transcript_quality_flags"]
        meta_path = tmp_path / f"{PREFIX}.meta.json"
        severe = vi._transcript_quality_severe_from_meta(meta_path)
        step = {
            "label": "transcript",
            "requested": True,
            "status": "done",
            "path": tmp_path / f"{PREFIX}.transcript.md",
            "quality_severe": severe,
        }
        assert severe is False
        assert vi.missing_pipeline_artifacts([step]) == []
        vi.finish_pipeline_run([step], label="x")  # returns normally: exit 0

    def test_dedupe_ranking_unchanged(self):
        newer = (Path("b.meta.json"), {"processed": "2026-02-01", "transcript_quality_flags": [FLAG]})
        older = (Path("a.meta.json"), {"processed": "2026-01-01", "transcript_quality_flags": []})
        assert vi._pick_canonical([older, newer]) == newer


# --- single-shot writers -----------------------------------------------------

PREFIX = "2026-06-13-test"


def _video():
    return {
        "video_id": "vid001",
        "url": "https://www.youtube.com/watch?v=vid001",
        "title": "Test",
        "published": "2026-06-13",
    }


def _run_single(tmp_path, monkeypatch, *, screen_content=None, malformed=False, duration=DURATION):
    payload = {
        "transcripts": ISSUE_DIALOGUE,
        "screen_content": ISSUE_SCREEN if screen_content is None else screen_content,
        "speakers": [{"voice": 1, "name": "A"}],
    }
    raw = json.dumps(payload)
    if malformed:  # complete transcripts and screen arrays, truncated speakers: salvage path
        raw = (
            '{"transcripts": '
            + json.dumps(payload["transcripts"])
            + ', "screen_content": '
            + json.dumps(payload["screen_content"])
            + ', "speakers": [{"voice": 1'
        )
    monkeypatch.setattr(vi, "call_gemini", lambda *a, **kw: raw)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)
    monkeypatch.setattr(
        vi, "log_usage_metadata", lambda *a, **kw: {"prompt": 10, "cached": 0, "candidates": 5, "total": 15}
    )
    _, status = vi.process_transcript(
        object(),
        None,
        _video(),
        "prompt",
        "stub-model",
        tmp_path,
        PREFIX,
        transcript_source="gemini",
        media_resolution="LOW",
        duration_seconds=duration,
    )
    meta = json.loads((tmp_path / f"{PREFIX}.meta.json").read_text(encoding="utf-8"))
    return status, meta


class TestSingleShotWriters:
    def test_success_path_records_the_flag_and_keeps_status(self, tmp_path, monkeypatch):
        status, meta = _run_single(tmp_path, monkeypatch)
        assert status == "done"
        assert meta["transcript_status"] == "complete"
        assert FLAG in meta["transcript_quality_flags"]
        assert meta["transcript_quality_flags"] == sorted(meta["transcript_quality_flags"])

    def test_success_path_healthy_screens_record_nothing(self, tmp_path, monkeypatch):
        spread = _screen([(s, s + 10) for s in range(0, DURATION, 120)])
        _, meta = _run_single(tmp_path, monkeypatch, screen_content=spread)
        assert FLAG not in meta["transcript_quality_flags"]

    def test_success_path_unknown_duration_records_nothing(self, tmp_path, monkeypatch):
        _, meta = _run_single(tmp_path, monkeypatch, duration=None)
        assert FLAG not in meta["transcript_quality_flags"]

    def test_salvage_path_records_the_flag_and_keeps_status(self, tmp_path, monkeypatch):
        status, meta = _run_single(tmp_path, monkeypatch, malformed=True)
        assert "salvaged" in status
        assert meta["transcript_recovery"] == "salvaged_sections"
        assert meta["transcript_status"] == "partial"
        assert FLAG in meta["transcript_quality_flags"]


# --- chunked writer ----------------------------------------------------------


def _usage():
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=100000,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=5000,
            total_token_count=105000,
        )
    )


def _run_chunked(tmp_path, monkeypatch, responses, chunks, duration):
    calls = {"n": 0}

    def fake_call_gemini(client, types, media_uri, prompt_text, model, response_json=False, **kw):
        idx = calls["n"]
        calls["n"] += 1
        if kw.get("on_response") is not None:
            kw["on_response"](_usage())
        return json.dumps(responses[idx])

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)
    fake_types = SimpleNamespace(
        MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH")
    )
    channel_dir = tmp_path / "chan"
    prefix = "2026-08-12-a-long-talk"
    status = vi._run_chunked_transcript_url(
        client=object(),
        types=fake_types,
        video={**_video(), "title": "A Long Talk"},
        prompt_text="PROMPT",
        model="stub-model",
        channel_dir=channel_dir,
        prefix=prefix,
        chunks=chunks,
        duration_seconds=duration,
        chunk_minutes=30,
        force=False,
    )
    meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
    return status, meta, channel_dir / f"{prefix}.transcript.md"


def _chunk_response(lo: int, hi: int, screen_spans: list[tuple[int, int | None]]) -> dict:
    return {
        "transcripts": _dialogue(lo, hi),
        "screen_content": _screen(screen_spans),
        "speakers": [{"voice": 1, "name": "A"}],
    }


THREE_CHUNKS = [(0, 1800), (1800, 3600), (3600, 5400)]


def _healthy_spans(lo: int, hi: int) -> list[tuple[int, int]]:
    return [(s, s + 10) for s in range(lo + 5, hi, 150)]


class TestChunkedWriter:
    def _responses(self, chunk2_spans):
        return [
            _chunk_response(0, 1800, _healthy_spans(0, 1800)),
            _chunk_response(1800, 3600, chunk2_spans),
            _chunk_response(3600, 5400, _healthy_spans(3600, 5400)),
        ]

    def test_collapsed_middle_chunk_flags_and_nothing_else_changes(self, tmp_path, monkeypatch):
        # Arrange: chunk 2's SCREEN stamps come back chunk-relative and all in
        # its first seconds; its dialogue stamps are correct and absolute.
        collapsed = [(s - 1800, e - 1800) for s, e in _collapsed(8, at=1801, length=20)]
        control_dir = tmp_path / "control"
        control_dir.mkdir()

        # Act
        status, meta, _ = _run_chunked(tmp_path, monkeypatch, self._responses(collapsed), THREE_CHUNKS, 5400)
        _, control, _ = _run_chunked(
            control_dir, monkeypatch, self._responses(_healthy_spans(1800, 3600)), THREE_CHUNKS, 5400
        )

        # Assert
        assert status == "done"
        assert meta["transcript_status"] == "ok"
        assert FLAG in meta["transcript_quality_flags"]
        assert FLAG not in control["transcript_quality_flags"]
        assert [f for f in meta["transcript_quality_flags"] if f != FLAG] == control["transcript_quality_flags"]

    def test_absolute_caption_burst_in_a_later_chunk_does_not_flag(self, tmp_path, monkeypatch):
        # A legitimate one-second caption burst at chunk 2's start, stamped
        # absolute: its END stamps must be brought into the chunk's frame with
        # its starts, or every screen would seem to run to the chunk's end.
        burst = _sequential(10, at=1800, length=1)

        _, meta, _ = _run_chunked(tmp_path, monkeypatch, self._responses(burst), THREE_CHUNKS, 5400)

        assert FLAG not in meta["transcript_quality_flags"]

    def test_runt_folded_tail_screens_past_the_nominal_boundary_do_not_flag(self, tmp_path, monkeypatch):
        # Arrange: a 61.5-minute video folds its 90-second runt into chunk 2,
        # whose ACTUAL window is 1890s against a 1800s nominal chunk. Chunk 2
        # opens with overlapping screens and has one more past the nominal end
        # (absolute 3670s): judged against the actual window it is not crowded.
        duration = 3690
        chunks = vi._build_transcript_chunks(duration, 30)
        assert chunks == [(0, 1800), (1800, duration)]
        tail_spans = _collapsed(MIN_ENTRIES, at=1801, length=20) + [(3670, 3680)]
        responses = [
            _chunk_response(0, 1800, _healthy_spans(0, 1800)),
            _chunk_response(1800, duration, tail_spans),
        ]

        # Act
        _, meta, _ = _run_chunked(tmp_path, monkeypatch, responses, chunks, duration)

        # Assert
        assert FLAG not in meta["transcript_quality_flags"]

    def test_absolute_collapsed_stamps_at_a_later_chunk_start_flag(self, tmp_path, monkeypatch):
        # Chunk 2's SCREEN stamps come back ABSOLUTE, all crowded at its real
        # start (30:01 onwards): relativization must bring them into the frame.
        collapsed = _collapsed(8, at=1801, length=20)

        status, meta, _ = _run_chunked(tmp_path, monkeypatch, self._responses(collapsed), THREE_CHUNKS, 5400)

        assert status == "done"
        assert FLAG in meta["transcript_quality_flags"]


class TestChunkRelativizationSkipsUnplaceableScreenStamps:
    """A SCREEN stamp the chunk cannot place is skipped, never piled onto the
    chunk start. Driven through `_assess_chunk_coverage` with a chunk start > 0."""

    def _flags(self, screen_content):
        parsed = {"transcripts": _dialogue(1800, 3600), "screen_content": screen_content, "speakers": []}
        _, metrics = vi._assess_chunk_coverage(parsed, 1800, 3600, 5400, 2)
        return _flags(metrics)

    def test_unparseable_stamps(self):
        assert FLAG not in self._flags([{"start": "N/A", "end": "N/A"}] * (MIN_ENTRIES + 3))

    def test_relative_fractional_stamps_spread_over_the_chunk(self):
        spread = [
            {"start": f"{m:02d}:{s:02d}.5", "end": f"{m:02d}:{s + 5:02d}.5"}
            for m, s in ((i * 3, 10) for i in range(MIN_ENTRIES + 4))
        ]
        assert FLAG not in self._flags(spread)

    def test_stamps_that_land_before_the_chunk_start(self):
        # Fractional, so the classifier leaves them unplaced: they parse to the
        # chunk's first seconds of the VIDEO, not of the chunk.
        early = [{"start": f"00:{i:02d}.0", "end": "00:40.0"} for i in range(MIN_ENTRIES + 3)]
        assert FLAG not in self._flags(early)

    def test_implausible_stamps(self):
        assert FLAG not in self._flags([{"start": "9:00:00", "end": "9:00:30"}] * (MIN_ENTRIES + 3))

    def test_an_unplaceable_end_is_dropped_not_clamped(self):
        entries = vi._relativize_chunk_entries(
            [{"start": "30:05", "end": "N/A"}, {"start": "N/A"}],
            1800,
            1800,
            keys=("start", "end"),
            drop_unplaceable=True,
        )
        assert entries == [{"start": "0:00:05"}]

    def test_placeable_stamps_still_flag(self):
        assert FLAG in self._flags(_screen(_collapsed(8, at=1801, length=20)))

    def test_dialogue_mode_is_unchanged(self):
        dialogue = [{"start": "N/A"}, {"start": "00:05.5"}, {"start": "30:10"}, {"start": "9:00:00"}]
        assert vi._relativize_chunk_entries(dialogue, 1800, 1800) == [
            {"start": "0:00:00"},
            {"start": "0:00:00"},
            {"start": "0:00:10"},
            {"start": "8:30:00"},
        ]


class TestWriterOutputRoundTripsThroughTheReaders:
    """[core: writer's-path] The readers see what the real writers wrote."""

    def test_stitched_transcript_from_the_real_chunked_writer(self, tmp_path, monkeypatch):
        # Arrange: chunk 2 names no speakers (an empty speaker cell) and
        # carries a start-only SCREEN entry.
        responses = [
            _chunk_response(0, 1800, [(5, 15), (600, None)]),
            {"transcripts": _dialogue(1800, 3725), "screen_content": _screen([(1805, 1820), (3000, None)])},
        ]
        chunks = [(0, 1800), (1800, 3725)]

        # Act
        _, _, transcript_path = _run_chunked(tmp_path, monkeypatch, responses, chunks, 3725)
        text = transcript_path.read_text(encoding="utf-8")

        # Assert
        assert "| 2 | 30:00 - 1:02:05 | ok | — |" in text
        assert vi.chunk_windows_from_transcript_markdown(text) == chunks
        assert vi.screen_entries_from_transcript_markdown(text) == [
            {"start": "00:05", "end": "00:15"},
            {"start": "10:00"},
            {"start": "30:05", "end": "30:20"},
            {"start": "50:00"},
        ]

    def test_coverage_block_writer_rows_parse(self):
        rows = [
            {"range": vi._format_chunk_range_label(0, 1800), "status": "ok", "speakers": ["A"]},
            {"range": vi._format_chunk_range_label(1800, 3600), "status": "FAILED (timeout)", "speakers": []},
            {"range": vi._format_chunk_range_label(3600, 3725), "status": "thin", "speakers": []},
        ]
        block = vi._build_chunked_transcript_coverage_block(3725, 30, rows)
        body = vi.merge_transcript_json(
            {"transcripts": _dialogue(0, 60), "screen_content": [{"start": "00:07", "type": "slide"}], "speakers": []},
            {},
        )
        text = block + "\n" + body
        assert vi.chunk_windows_from_transcript_markdown(text) == [(0, 1800), (1800, 3600), (3600, 3725)]
        assert vi.screen_entries_from_transcript_markdown(text) == [{"start": "00:07"}]


# --- assess-transcripts over Markdown -----------------------------------------


def _md(body_lines: list[str]) -> str:
    header = (
        "# Transcript: T\n\n"
        "**Source:** https://www.youtube.com/watch?v=vid001\n"
        "**Published:** 2026-06-01\n"
        "**Status:** Complete\n\n"
        "---\n\n"
    )
    return header + "\n\n".join(body_lines) + "\n"


def _md_stamp(secs: int) -> str:
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def _body(dialogue_seconds, screen_spans) -> list[str]:
    """Rendered the way `merge_transcript_json` renders: one list sorted by start."""
    items = [(s, f'[{_md_stamp(s)}] Host (Speaker): "line {s}"') for s in dialogue_seconds]
    for s, e in screen_spans:
        rng = _md_stamp(s) if e is None else f"{_md_stamp(s)}-{_md_stamp(e)}"
        items.append((s, f"  SCREEN [{rng}] [slide]: slide at {s}"))
    return [line for _, line in sorted(items, key=lambda x: x[0])]


def _chunked_md(windows, dialogue_seconds, screen_spans, duration) -> str:
    rows = [
        "**Source:** chunked transcript",
        f"**Total duration:** {duration}s",
        "**Chunk size:** 30 minutes",
        f"**Segments:** {len(windows)}",
        "",
        "| Segment | Range | Status | Speakers |",
        "|---|---|---|---|",
    ]
    for i, (lo, hi) in enumerate(windows, start=1):
        rows.append(f"| {i} | {vi._format_chunk_range_label(lo, hi)} | ok | A |")
    return "\n".join(rows) + "\n\n" + "\n\n".join(_body(dialogue_seconds, screen_spans)) + "\n"


def _meta(**extra):
    base = {
        "video_id": "vid001",
        "channel": "chan",
        "title": "T",
        "transcript_status": "complete",
        "duration_seconds": DURATION,
        "modes_completed": ["transcript"],
    }
    base.update(extra)
    return base


def _write(chan: Path, prefix: str, meta: dict, transcript: str) -> Path:
    chan.mkdir(exist_ok=True)
    meta_path = chan / f"{prefix}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (chan / f"{prefix}.transcript.md").write_text(transcript, encoding="utf-8")
    return meta_path


def _run(tmp_path, **kw):
    args = _types.SimpleNamespace(
        apply=kw.get("apply", False), force=kw.get("force", False), channel=None, backup_dir=kw.get("backup_dir")
    )
    return vi.cmd_assess_transcripts(args, {"output_dir": str(tmp_path / "corpus")})


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    return root


class TestMarkdownParsers:
    def test_screen_lines_are_read_with_start_and_end(self):
        text = _md(_body([0, 20], [(1, 25), (3, None), (3725, 3730)]))
        assert vi.screen_entries_from_transcript_markdown(text) == [
            {"start": "00:01", "end": "00:25"},
            {"start": "00:03"},
            {"start": "1:02:05", "end": "1:02:10"},
        ]

    def test_screen_lines_in_the_header_are_not_entries(self):
        text = "# Transcript: T\n\n  SCREEN [00:01] [slide]: header\n\n---\n\n  SCREEN [00:02] [slide]: body\n"
        assert vi.screen_entries_from_transcript_markdown(text) == [{"start": "00:02"}]

    def test_coverage_table_windows(self):
        windows = [(0, 1800), (1800, 3600), (3600, 3725)]
        text = _chunked_md(windows, [0], [], 3725)
        assert vi.chunk_windows_from_transcript_markdown(text) == windows

    def test_no_coverage_table_is_none(self):
        assert vi.chunk_windows_from_transcript_markdown(_md(_body([0], []))) is None

    def test_an_unparseable_row_is_none(self):
        text = _chunked_md([(0, 1800), (1800, 3600)], [0], [], 3600).replace("30:00 - 1:00:00", "30:00 - soon")
        assert vi.chunk_windows_from_transcript_markdown(text) is None

    def test_windows_out_of_order_are_none(self):
        assert vi.chunk_windows_from_transcript_markdown(_chunked_md([(1800, 3600), (0, 1800)], [0], [], 3600)) is None
        assert vi.chunk_windows_from_transcript_markdown(_chunked_md([(0, 1800), (1700, 3600)], [0], [], 3600)) is None


class TestAssessTranscriptsFromMarkdown:
    def test_whole_video_collapse_is_flagged(self, tmp_path, corpus):
        md = _md(_body(range(0, DURATION, 20), _collapsed(8, at=1, length=20)))
        meta_path = _write(corpus / "chan", "v", _meta(), md)

        counts = _run(tmp_path, force=True, apply=True, backup_dir=str(tmp_path / "bk"))

        stored = json.loads(meta_path.read_text(encoding="utf-8"))
        assert FLAG in stored["transcript_quality_flags"]
        assert stored["transcript_status"] == "complete"
        assert counts["mild"] == 1

    def test_chunked_transcript_is_judged_per_coverage_window(self, tmp_path, corpus):
        # Chunk 2 collapsed (absolute stamps at its own start), chunks 1 and 3 healthy.
        windows = [(0, 1800), (1800, 3600), (3600, 5400)]
        spans = _healthy_spans(0, 1800) + _collapsed(8, at=1801, length=20) + _healthy_spans(3600, 5400)
        md = _chunked_md(windows, range(0, 5400, 20), spans, 5400)
        meta_path = _write(corpus / "chan", "v", _meta(duration_seconds=5400), md)

        _run(tmp_path, force=True, apply=True, backup_dir=str(tmp_path / "bk"))

        assert FLAG in json.loads(meta_path.read_text(encoding="utf-8"))["transcript_quality_flags"]

    def test_chunked_collapse_is_invisible_to_a_whole_video_window(self):
        # Why the coverage table matters: the same body judged whole-video is not crowded.
        spans = _healthy_spans(0, 1800) + _collapsed(8, at=1801, length=20) + _healthy_spans(3600, 5400)
        md = _md(_body(range(0, 5400, 20), spans))
        _, flags = vi.assess_transcript_markdown(md, 5400, captions=False)
        assert FLAG not in flags

    def test_unknown_duration_writes_nothing(self, tmp_path, corpus):
        md = _md(_body(range(0, DURATION, 20), _collapsed(8, at=1, length=20)))
        meta_path = _write(corpus / "chan", "v", _meta(duration_seconds=None), md)
        before = meta_path.read_bytes()

        counts = _run(tmp_path, force=True, apply=True, backup_dir=str(tmp_path / "bk"))

        assert meta_path.read_bytes() == before
        assert counts["unassessable"] == 1

    def test_chunked_file_with_unknown_duration_records_nothing(self, tmp_path, corpus):
        # The coverage table alone would give windows; without a known duration
        # nothing is judged, at the command and in the Markdown assessor.
        windows = [(0, 1800), (1800, 3600)]
        md = _chunked_md(windows, range(0, 3600, 20), _collapsed(8, at=1801, length=20), 3600)
        meta = _meta()
        del meta["duration_seconds"]
        meta_path = _write(corpus / "chan", "v", meta, md)
        before = meta_path.read_bytes()

        counts = _run(tmp_path, force=True, apply=True, backup_dir=str(tmp_path / "bk"))

        assert meta_path.read_bytes() == before
        assert counts["unassessable"] == 1
        assert FLAG not in vi.assess_transcript_markdown(md, None, captions=False)[1]
        assert FLAG in vi.assess_transcript_markdown(md, 3600, captions=False)[1]

    def test_forced_reassessment_replaces_a_stale_flag(self, tmp_path, corpus):
        # The flag is recomputable from the Markdown, so it is NOT writer-only.
        assert FLAG not in vi.WRITER_ONLY_QUALITY_FLAGS
        md = _md(_body(range(0, DURATION, 20), _healthy_spans(0, DURATION)))
        meta_path = _write(corpus / "chan", "v", _meta(transcript_quality_flags=[FLAG]), md)

        _run(tmp_path, force=True, apply=True, backup_dir=str(tmp_path / "bk"))

        assert json.loads(meta_path.read_text(encoding="utf-8"))["transcript_quality_flags"] == []


class TestTheSharedBodyParserOwnsScreenBlocks:
    def test_a_screen_shaped_line_inside_a_block_is_that_blocks_content(self):
        # An on-screen quote that itself holds a SCREEN-looking line belongs to
        # its block (the body parser's rule), so it is not a second entry.
        text = _md(
            [
                '[00:00] Host (Speaker): "line 0"',
                '  SCREEN [00:05-00:20] [slide]: a slide\n  On-screen text: "first line\n  SCREEN [00:06-00:21] [slide]: quoted\nlast line"',
                '[00:30] Host (Speaker): "line 30"',
            ]
        )
        assert vi.screen_entries_from_transcript_markdown(text) == [{"start": "00:05", "end": "00:20"}]


class TestRepairTimestampsJudgesScreenStampsLikeAssessTranscripts:
    """`repair-timestamps` re-assesses the file it wrote through
    `assess_transcript_markdown`, the function `assess-transcripts` calls, so
    the two judge SCREEN stamps the same way. The mild flag never blocks the
    repair or the status promotion."""

    def test_repair_records_the_flag_and_still_promotes_the_status(self, tmp_path, monkeypatch):
        # Arrange: the repair suite's monolithic single-shot shape, with its
        # SCREEN blocks collapsed into the first seconds.
        import test_repair_timestamps as rt

        monkeypatch.setattr(rt, "SCREENS", _screen(_collapsed(8, at=1, length=20)))
        corpus_root = tmp_path / "corpus"
        (corpus_root / "chan").mkdir(parents=True)
        p = rt._write_corpus(corpus_root)
        rt._stub_captions(monkeypatch, rt._talk()[1])
        vi._invalidate_video_id_cache()

        # Act
        result = rt._run(corpus_root, apply=True)

        # Assert
        assert result["outcome"] == "repaired"
        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        written = p.transcript.read_bytes().decode("utf-8")
        fields, flags = vi.assess_transcript_markdown(written, 600, captions=False)
        assert FLAG in flags
        assert meta["transcript_quality_flags"] == fields["transcript_quality_flags"]
        assert FLAG in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "complete"
        vi._invalidate_video_id_cache()
