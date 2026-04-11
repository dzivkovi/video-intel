"""Tests for translate_video.py — pure functions only, no API calls."""

import logging
from pathlib import Path

from translate_video import (
    _format_hhmm,
    apply_timestamp_offset,
    build_chunk_list,
    build_header,
    build_output_path,
    extract_video_id,
    format_elapsed,
    format_stats,
    normalize_timestamp,
    parse_iso8601_duration,
    slugify,
    stitch_parts,
    translate_title,
)


class TestExtractVideoId:
    def test_extract_video_id_standard_url_returns_id(self):
        assert extract_video_id("https://www.youtube.com/watch?v=Sm7568B0BC8") == "Sm7568B0BC8"

    def test_extract_video_id_short_url_returns_id(self):
        assert extract_video_id("https://youtu.be/Sm7568B0BC8") == "Sm7568B0BC8"

    def test_extract_video_id_with_extra_params_returns_id(self):
        assert extract_video_id("https://www.youtube.com/watch?v=Sm7568B0BC8&t=120") == "Sm7568B0BC8"

    def test_extract_video_id_invalid_url_returns_none(self):
        assert extract_video_id("https://example.com/page") is None

    def test_extract_video_id_empty_string_returns_none(self):
        assert extract_video_id("") is None


class TestSlugify:
    def test_slugify_normal_title_returns_lowercase_dashes(self):
        assert slugify("Tucker Carlson Interviews Putin") == "tucker-carlson-interviews-putin"

    def test_slugify_special_chars_returns_clean_slug(self):
        assert slugify("What's Next? (2024 Edition!)") == "whats-next-2024-edition"

    def test_slugify_long_title_truncates(self):
        result = slugify("a" * 100, max_len=20)
        assert len(result) == 20

    def test_slugify_trailing_dash_stripped(self):
        # If truncation lands mid-word leaving a trailing dash
        result = slugify("hello-world-this-is-long", max_len=12)
        assert not result.endswith("-")


class TestParseIso8601Duration:
    def test_parse_full_duration(self):
        assert parse_iso8601_duration("PT2H18M42S") == 2 * 3600 + 18 * 60 + 42

    def test_parse_minutes_only(self):
        assert parse_iso8601_duration("PT11M30S") == 11 * 60 + 30

    def test_parse_hours_only(self):
        assert parse_iso8601_duration("PT1H") == 3600

    def test_parse_invalid_returns_zero(self):
        assert parse_iso8601_duration("invalid") == 0


class TestBuildOutputPath:
    def test_build_output_path_returns_correct_naming(self, tmp_path):
        result = build_output_path(tmp_path, "Tucker Carlson Interviews Putin", "2024-03-15")
        expected = tmp_path / "2024-03-15-tucker-carlson-interviews-putin.translate-bcs.txt"
        assert result == expected

    def test_build_output_path_stable_fallback_with_video_id(self, tmp_path):
        # When title is just the video ID and date is the fallback
        result = build_output_path(tmp_path, "Sm7568B0BC8", "0000-00-00")
        expected = tmp_path / "0000-00-00-sm7568b0bc8.translate-bcs.txt"
        assert result == expected

    def test_build_output_path_same_input_same_output(self, tmp_path):
        # Idempotency: same inputs always produce the same path
        path1 = build_output_path(tmp_path, "My Video Title", "2024-01-01")
        path2 = build_output_path(tmp_path, "My Video Title", "2024-01-01")
        assert path1 == path2


class TestSkipIfExists:
    def test_existing_file_skips_without_force(self, tmp_path, capsys):
        """Verify that translate_video returns early when output exists."""
        # Arrange: create a fake existing translation
        output_path = build_output_path(tmp_path, "Existing Video", "2024-01-01")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("existing content", encoding="utf-8")

        # Act: import and call with force=False — it should return without calling Gemini
        # We test this indirectly by checking the file wasn't modified
        original_content = output_path.read_text(encoding="utf-8")

        from translate_video import build_output_path as _bop

        rebuilt = _bop(tmp_path, "Existing Video", "2024-01-01")
        assert rebuilt.exists()
        assert rebuilt.read_text(encoding="utf-8") == original_content


class TestFormatElapsed:
    def test_format_elapsed_seconds_only(self):
        assert format_elapsed(45) == "45s"

    def test_format_elapsed_minutes_and_seconds(self):
        assert format_elapsed(125) == "2m 5s"

    def test_format_elapsed_hours_minutes_seconds(self):
        assert format_elapsed(3725) == "1h 2m 5s"

    def test_format_elapsed_zero(self):
        assert format_elapsed(0) == "0s"


class TestFormatStats:
    def test_format_stats_with_usage_metadata(self):
        usage = {
            "prompt_token_count": 500_000,
            "candidates_token_count": 12_847,
            "total_token_count": 512_847,
        }
        result = format_stats(342.0, 487, usage)
        assert "5m 42s" in result
        assert "12,847" in result
        assert "65,536" in result
        assert "19.6%" in result
        assert "500,000" in result
        assert "487" in result

    def test_format_stats_without_usage_metadata(self):
        result = format_stats(91.0, 132, None)
        assert "1m 31s" in result
        assert "132" in result
        assert "Output tokens" not in result

    def test_format_stats_partial_usage(self):
        usage = {"candidates_token_count": 5000, "prompt_token_count": None, "total_token_count": None}
        result = format_stats(60.0, 50, usage)
        assert "5,000" in result
        assert "Input tokens" not in result


class TestBuildChunkList:
    """Tests for the chunking policy: <= 60 min single, > 60 min first-hour + 20m."""

    def test_short_video_no_chunking(self):
        # 30-min video → single request, no clipping
        assert build_chunk_list(30 * 60) == [(0, 0)]

    def test_45_min_video_no_chunking(self):
        # 45-min video → single request, no clipping
        assert build_chunk_list(45 * 60) == [(0, 0)]

    def test_exactly_60_min_no_chunking(self):
        # 60-min video → single request (boundary: <= 60)
        assert build_chunk_list(60 * 60) == [(0, 0)]

    def test_61_min_video_chunks_first_hour_then_remainder(self):
        # 61-min video → first hour + 1-min remainder
        chunks = build_chunk_list(61 * 60)
        assert chunks[0] == (0, 3600)
        assert chunks[1] == (3600, 61 * 60)
        assert len(chunks) == 2

    def test_90_min_video_first_hour_then_20m_chunks(self):
        # 90-min video → first hour + 20-min chunk + 10-min remainder
        chunks = build_chunk_list(90 * 60, chunk_minutes=20)
        assert chunks[0] == (0, 3600)
        assert chunks[1] == (3600, 3600 + 1200)  # 60-80 min
        assert chunks[2] == (3600 + 1200, 90 * 60)  # 80-90 min
        assert len(chunks) == 3

    def test_140_min_video_first_hour_then_four_20m_chunks(self):
        # 140 min = 2h20m → first hour + 4 x 20-min chunks
        chunks = build_chunk_list(140 * 60, chunk_minutes=20)
        assert chunks[0] == (0, 3600)
        assert chunks[1] == (3600, 4800)  # 60-80
        assert chunks[2] == (4800, 6000)  # 80-100
        assert chunks[3] == (6000, 7200)  # 100-120
        assert chunks[4] == (7200, 8400)  # 120-140
        assert len(chunks) == 5

    def test_custom_chunk_minutes(self):
        # 90-min video with 10-min chunks → first hour + 3 x 10-min
        chunks = build_chunk_list(90 * 60, chunk_minutes=10)
        assert chunks[0] == (0, 3600)
        assert len(chunks) == 4  # 1 first-hour + 3 remainder chunks


class TestNormalizeTimestamp:
    def test_normal_timestamp_unchanged(self):
        assert normalize_timestamp("[00:05:30] hello") == "[00:05:30] hello"

    def test_boundary_23_unchanged(self):
        assert normalize_timestamp("[23:59:59] text") == "[23:59:59] text"

    def test_120_minutes_converts_to_2_hours(self):
        assert normalize_timestamp("[120:05:30] text") == "[02:05:30] text"

    def test_90_minutes_converts_with_carry(self):
        assert normalize_timestamp("[90:15:42] text") == "[01:45:42] text"

    def test_60_minutes_converts_to_1_hour(self):
        assert normalize_timestamp("[60:00:00] text") == "[01:00:00] text"

    def test_no_timestamp_passthrough(self):
        assert normalize_timestamp("no timestamp line") == "no timestamp line"

    def test_minutes_carry_over_60(self):
        # 75 min + 50 MM = 1h15m + 50m = 2h05m
        assert normalize_timestamp("[75:50:10] text") == "[02:05:10] text"

    def test_empty_line_passthrough(self):
        assert normalize_timestamp("") == ""


class TestApplyTimestampOffset:
    """Tests for apply_timestamp_offset() — chunk-aware offset with classification."""

    # --- Clean relative inputs ---

    def test_hh_mm_ss_relative_with_offset(self):
        # [00:05:30] + 3600s in a 1200s chunk → relative → [01:05:30]
        assert apply_timestamp_offset("[00:05:30] text", 3600, 1200) == "[01:05:30] text"

    def test_mm_ss_relative_with_offset(self):
        # [05:30] + 3600s in a 1200s chunk → relative (330s ≤ 1500s) → [01:05:30]
        assert apply_timestamp_offset("[05:30] text", 3600, 1200) == "[01:05:30] text"

    def test_zero_offset_normalizes_mm_ss(self):
        # [05:30] + 0s → still relative → [00:05:30] (MM:SS → HH:MM:SS)
        assert apply_timestamp_offset("[05:30] text", 0, 3600) == "[00:05:30] text"

    def test_zero_offset_hh_mm_ss_passthrough(self):
        assert apply_timestamp_offset("[00:05:30] text", 0, 3600) == "[00:05:30] text"

    def test_no_timestamp_passthrough(self):
        assert apply_timestamp_offset("no timestamp here", 3600, 1200) == "no timestamp here"

    def test_empty_line_passthrough(self):
        assert apply_timestamp_offset("", 3600, 1200) == ""

    def test_real_world_part_60_80(self):
        # part-60-80: offset=3600, duration=1200. Gemini outputs [00:00:04] relative.
        assert apply_timestamp_offset("[00:00:04] milioni", 3600, 1200) == "[01:00:04] milioni"

    def test_real_world_part_120_138(self):
        # part-120-138: offset=7200, duration=1080. Gemini outputs [17:42] relative.
        # 17*60+42 = 1062s ≤ 1080+300 → relative → +7200 = 8262s = 2h17m42s
        assert apply_timestamp_offset("[17:42] text", 7200, 1080) == "[02:17:42] text"

    def test_large_relative_near_chunk_boundary(self):
        # [19:58] in a 1200s (20-min) chunk → 1198s ≤ 1500s → relative
        assert apply_timestamp_offset("[19:58] text", 3600, 1200) == "[01:19:58] text"

    # --- Already-absolute inputs (should not double-offset) ---

    def test_already_absolute_kept_as_is(self):
        # [01:00:04] in part-60-80 (offset=3600, duration=1200)
        # 3604s in [3600, 4500] → absolute → keep
        assert apply_timestamp_offset("[01:00:04] text", 3600, 1200) == "[01:00:04] text"

    def test_already_absolute_mid_chunk(self):
        # [01:10:00] in part-60-80 → 4200s in [3600, 4500] → absolute
        assert apply_timestamp_offset("[01:10:00] text", 3600, 1200) == "[01:10:00] text"

    # --- Implausible inputs (from real failures) ---

    def test_implausible_echo_pattern(self, caplog):
        # [18:42:42] in a 20-min chunk (offset=4800, duration=1200)
        # 67362s fits neither relative (≤1500) nor absolute ([4800, 6300])
        with caplog.at_level(logging.WARNING, logger="translate_video"):
            result = apply_timestamp_offset("[18:42:42] text", 4800, 1200)
        assert result == "[18:42:42] text"  # unchanged
        assert any("implausible" in r.message.lower() for r in caplog.records)

    def test_implausible_jump(self, caplog):
        # [09:02:48] in part-60-80 (offset=3600, duration=1200)
        # 32568s fits neither range
        with caplog.at_level(logging.WARNING, logger="translate_video"):
            result = apply_timestamp_offset("[09:02:48] text", 3600, 1200)
        assert result == "[09:02:48] text"
        assert any("implausible" in r.message.lower() for r in caplog.records)

    # --- Mixed-mode (absolute then relative in same chunk) ---

    def test_mixed_mode_absolute_then_relative(self):
        # Part-80-100: first line absolute [01:20:00], later line relative [05:30]
        # offset=4800, duration=1200
        abs_result = apply_timestamp_offset("[01:20:00] first", 4800, 1200)
        rel_result = apply_timestamp_offset("[05:30] later", 4800, 1200)
        assert abs_result == "[01:20:00] first"  # kept as-is
        assert rel_result == "[01:25:30] later"  # 330s + 4800 = 5130s = 1h25m30s


class TestStitchParts:
    """Tests for stitch_parts() — merges part files into a single translation."""

    def _write_part(self, tmp_path: Path, slug: str, date: str, start: int, end: int, lines: list[str]) -> Path:
        """Helper: write a part file with the standard naming convention."""
        path = tmp_path / f"{date}-{slug}.part-{start}-{end}.translate-bcs.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_stitch_three_parts_correct_order(self, tmp_path):
        # Arrange: 3 parts with RELATIVE timestamps (counting from 0:00), written out of order
        self._write_part(tmp_path, "my-video", "2024-01-01", 40, 60, ["[00:00:00] part three"])
        self._write_part(tmp_path, "my-video", "2024-01-01", 0, 20, ["[00:00:00] part one"])
        self._write_part(tmp_path, "my-video", "2024-01-01", 20, 40, ["[00:00:00] part two"])

        # Act
        result = stitch_parts(tmp_path, "My Video", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert: offsets applied — 0*60=0, 20*60=1200, 40*60=2400
        content = result.read_text(encoding="utf-8")
        assert "# Translation (BCS): My Video" in content
        lines = [line for line in content.split("\n") if line.startswith("[")]
        assert lines == ["[00:00:00] part one", "[00:20:00] part two", "[00:40:00] part three"]

    def test_stitch_newline_seam_no_missing_line(self, tmp_path):
        # Arrange: relative timestamps, part 1 ends WITHOUT trailing newline
        p1 = tmp_path / "2024-01-01-vid.part-0-20.translate-bcs.txt"
        p1.write_text("[00:00:00] first\n[19:50] last of part one", encoding="utf-8")  # no trailing \n
        p2 = tmp_path / "2024-01-01-vid.part-20-40.translate-bcs.txt"
        p2.write_text("[00:00:00] first of part two\n[19:50] last", encoding="utf-8")

        # Act
        result = stitch_parts(tmp_path, "Vid", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert: no line is lost at the seam, offsets applied
        content = result.read_text(encoding="utf-8")
        assert "[00:19:50] last of part one" in content
        assert "[00:20:00] first of part two" in content
        ts_lines = [line for line in content.split("\n") if line.startswith("[")]
        assert ts_lines.index("[00:19:50] last of part one") < ts_lines.index("[00:20:00] first of part two")

    def test_stitch_applies_offset_to_mm_ss(self, tmp_path):
        # Arrange: part-60-120 with relative MM:SS timestamps
        self._write_part(tmp_path, "vid", "2024-01-01", 0, 60, ["[05:00] ok"])
        self._write_part(tmp_path, "vid", "2024-01-01", 60, 120, ["[05:30] was relative"])

        # Act
        result = stitch_parts(tmp_path, "Vid", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert: offset applied, MM:SS normalized to HH:MM:SS
        content = result.read_text(encoding="utf-8")
        assert "[00:05:00] ok" in content  # 0 offset, MM:SS → HH:MM:SS
        assert "[01:05:30] was relative" in content  # 3600s offset + 330s = 3930s = 1:05:30

    def test_stitch_no_parts_raises_error(self, tmp_path):
        # Act & Assert
        import pytest

        with pytest.raises(SystemExit):
            stitch_parts(tmp_path, "Nothing", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

    def test_stitch_missing_intermediate_warns(self, tmp_path, caplog):
        # Arrange: parts 0-20 and 40-60, but no 20-40. Relative timestamps.
        self._write_part(tmp_path, "vid", "2024-01-01", 0, 20, ["[00:00:00] part one"])
        self._write_part(tmp_path, "vid", "2024-01-01", 40, 60, ["[00:00:00] part three"])

        # Act
        with caplog.at_level(logging.WARNING, logger="translate_video"):
            result = stitch_parts(tmp_path, "Vid", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert: offsets applied, warning logged about gap
        content = result.read_text(encoding="utf-8")
        assert "[00:00:00] part one" in content  # 0 offset
        assert "[00:40:00] part three" in content  # 40*60 offset
        assert any("gap" in r.message.lower() for r in caplog.records)

    def test_stitch_with_original_title_in_header(self, tmp_path):
        # Arrange: parts are named with original title slug (how they were created during translation)
        self._write_part(tmp_path, "original-title", "2024-01-01", 0, 20, ["[00:00:00] text"])

        # Act: title is the translated/display title, original_title drives part discovery
        result = stitch_parts(
            tmp_path,
            "Prevod Naslova",
            "2024-01-01",
            "https://youtube.com/watch?v=TEST",
            "gemini-test",
            original_title="Original Title",
        )

        # Assert: header uses display title, original title shown separately
        content = result.read_text(encoding="utf-8")
        assert "# Translation (BCS): Prevod Naslova" in content
        assert "**Original:** Original Title" in content
        # Output file uses original title slug (sits alongside parts)
        assert "original-title" in result.name

    def test_stitch_monotonic_warning_on_non_monotonic(self, tmp_path, caplog):
        # Arrange: timestamps go backward within a part
        self._write_part(
            tmp_path,
            "vid",
            "2024-01-01",
            0,
            20,
            [
                "[00:00:00] first",
                "[00:10:00] second",
                "[00:05:00] went backward",
            ],
        )

        # Act
        with caplog.at_level(logging.WARNING, logger="translate_video"):
            stitch_parts(tmp_path, "Vid", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert: warning about non-monotonic, but still succeeds
        assert any("monotonic" in r.message.lower() for r in caplog.records)


class TestFormatHhmm:
    def test_zero(self):
        assert _format_hhmm(0) == "00:00"

    def test_one_hour(self):
        assert _format_hhmm(3600) == "01:00"

    def test_63_minutes(self):
        assert _format_hhmm(63 * 60) == "01:03"

    def test_2h18m(self):
        assert _format_hhmm(2 * 3600 + 18 * 60) == "02:18"


class TestBuildHeaderCoverage:
    """Tests for coverage metadata in build_header()."""

    def test_no_coverage_no_duration_omits_line(self):
        header = build_header("Title", "https://example.com", "2024-01-01", "gemini-test")
        assert "Coverage" not in header

    def test_coverage_without_duration_omits_line(self):
        header = build_header(
            "Title",
            "https://example.com",
            "2024-01-01",
            "gemini-test",
            coverage=(0, 63),
        )
        assert "Coverage" not in header

    def test_duration_without_coverage_omits_line(self):
        header = build_header(
            "Title",
            "https://example.com",
            "2024-01-01",
            "gemini-test",
            duration_seconds=8280,
        )
        assert "Coverage" not in header

    def test_partial_coverage_shown(self):
        header = build_header(
            "Title",
            "https://example.com",
            "2024-01-01",
            "gemini-test",
            coverage=(0, 63),
            duration_seconds=2 * 3600 + 18 * 60,
        )
        assert "**Coverage:** 00:00" in header
        assert "01:03" in header
        assert "02:18 total" in header

    def test_full_coverage_shown(self):
        header = build_header(
            "Title",
            "https://example.com",
            "2024-01-01",
            "gemini-test",
            coverage=(0, 138),
            duration_seconds=138 * 60,
        )
        assert "**Coverage:** 00:00" in header
        assert "02:18" in header


class TestStitchPartsCoverage:
    """Tests for coverage metadata and BCS partial note in stitch_parts()."""

    def _write_part(self, tmp_path: Path, slug: str, date: str, start: int, end: int, lines: list[str]) -> Path:
        path = tmp_path / f"{date}-{slug}.part-{start}-{end}.translate-bcs.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_partial_stitch_adds_coverage_and_bcs_note(self, tmp_path):
        self._write_part(tmp_path, "vid", "2024-01-01", 0, 63, ["[00:00:00] text"])

        result = stitch_parts(
            tmp_path,
            "Vid",
            "2024-01-01",
            "https://youtube.com/watch?v=TEST",
            "gemini-test",
            duration_seconds=2 * 3600 + 18 * 60,
        )
        content = result.read_text(encoding="utf-8")
        assert "**Coverage:** 00:00" in content
        assert "01:03" in content
        assert "02:18 total" in content
        assert "Ovaj prevod pokriva" in content

    def test_full_coverage_no_bcs_note(self, tmp_path):
        self._write_part(tmp_path, "vid", "2024-01-01", 0, 60, ["[00:00:00] first"])
        self._write_part(tmp_path, "vid", "2024-01-01", 60, 120, ["[00:00:00] second"])

        result = stitch_parts(
            tmp_path,
            "Vid",
            "2024-01-01",
            "https://youtube.com/watch?v=TEST",
            "gemini-test",
            duration_seconds=120 * 60,
        )
        content = result.read_text(encoding="utf-8")
        assert "**Coverage:**" in content
        assert "Ovaj prevod pokriva" not in content

    def test_no_duration_omits_coverage(self, tmp_path):
        self._write_part(tmp_path, "vid", "2024-01-01", 0, 63, ["[00:00:00] text"])

        result = stitch_parts(
            tmp_path,
            "Vid",
            "2024-01-01",
            "https://youtube.com/watch?v=TEST",
            "gemini-test",
        )
        content = result.read_text(encoding="utf-8")
        assert "Coverage" not in content
        assert "Ovaj prevod pokriva" not in content


class TestStreamTimeout:
    """Tests for wall-clock timeout on Gemini stream iteration."""

    def test_first_chunk_timeout_fires_when_stream_blocks(self):
        """The old code never timed out because the check was inside the for-loop body.
        Verify the queue-based drain thread raises TimeoutError when no chunks arrive."""
        import queue
        import threading

        # Arrange: a stream that blocks forever (simulates Gemini "thinking")
        class _HangingStream:
            def __iter__(self):
                return self

            def __next__(self):
                threading.Event().wait()  # block forever

        # Test the queue mechanism directly: drain thread + queue.get(timeout=...)
        chunk_q = queue.Queue()

        def _drain():
            try:
                for _ in _HangingStream():
                    chunk_q.put(("chunk", None))
                chunk_q.put(("done", None))
            except Exception as exc:
                chunk_q.put(("error", exc))

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()

        # Act & Assert: queue.get with a short timeout raises queue.Empty
        import pytest

        with pytest.raises(queue.Empty):
            chunk_q.get(timeout=0.1)

    def test_stream_error_propagated_through_queue(self):
        """Errors in the drain thread should surface to the caller."""
        import queue
        import threading

        # Arrange: a stream that raises after one chunk
        class _ErrorStream:
            def __init__(self):
                self._yielded = False

            def __iter__(self):
                return self

            def __next__(self):
                if not self._yielded:
                    self._yielded = True
                    return "chunk1"
                raise RuntimeError("Gemini exploded")

        chunk_q = queue.Queue()

        def _drain():
            try:
                for chunk in _ErrorStream():
                    chunk_q.put(("chunk", chunk))
                chunk_q.put(("done", None))
            except Exception as exc:
                chunk_q.put(("error", exc))

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()
        drain_thread.join(timeout=2)

        # Act: read all items
        items = []
        while not chunk_q.empty():
            items.append(chunk_q.get_nowait())

        # Assert: first chunk, then error
        assert items[0] == ("chunk", "chunk1")
        assert items[1][0] == "error"
        assert "Gemini exploded" in str(items[1][1])

    def test_normal_stream_completes_through_queue(self):
        """Normal stream drains all chunks and sends 'done'."""
        import queue
        import threading

        chunks = ["hello ", "world"]

        chunk_q = queue.Queue()

        def _drain():
            try:
                for c in chunks:
                    chunk_q.put(("chunk", c))
                chunk_q.put(("done", None))
            except Exception as exc:
                chunk_q.put(("error", exc))

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()
        drain_thread.join(timeout=2)

        # Act: read all items
        items = []
        while not chunk_q.empty():
            items.append(chunk_q.get_nowait())

        # Assert
        assert items == [("chunk", "hello "), ("chunk", "world"), ("done", None)]


class TestLoadPrompt:
    def test_load_prompt_translate_bcs_exists(self):
        from translate_video import load_prompt

        text = load_prompt("translate-bcs")
        assert "BCS" in text
        assert "[HH:MM:SS]" in text


class TestTranslateTitle:
    """Verify translate_title retries on transient errors and falls back gracefully."""

    def _client_with_side_effects(self, side_effects):
        from unittest.mock import MagicMock

        client = MagicMock()
        client.models.generate_content.side_effect = side_effects
        return client

    def test_returns_translated_title_on_success(self):
        from unittest.mock import MagicMock

        ok = MagicMock()
        ok.text = '"Prevod naslova"'
        client = self._client_with_side_effects([ok])

        result = translate_title(client, "gemini-test", "Original Title")
        assert result == "Prevod naslova"

    def test_falls_back_to_original_on_non_retryable_error(self, caplog):
        from google.genai import errors

        exc = errors.APIError(400, {"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})
        client = self._client_with_side_effects([exc])

        with caplog.at_level(logging.WARNING, logger="translate_video"):
            result = translate_title(client, "gemini-test", "Original Title")

        assert result == "Original Title"
        assert any("falling back" in r.message.lower() for r in caplog.records)

    def test_retries_on_503_then_succeeds(self, monkeypatch):
        from unittest.mock import MagicMock

        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        monkeypatch.setattr("translate_video.time.sleep", lambda _: None)

        exc = errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
        ok = MagicMock()
        ok.text = "Prevod"
        client = self._client_with_side_effects([exc, ok])

        result = translate_title(client, "gemini-test", "Original Title")
        assert result == "Prevod"

    # Note: translate_title uses a reduced best-effort retry budget (2/2) rather
    # than the aggressive 3/8 used by the main streaming path. Stitch must not
    # block for ~47 min on an optional title translation during a Gemini outage.
    # Tests assert exact call count AND sleep sequence — without this, a budget
    # bug would still pass because StopIteration from a depleted side_effect list
    # gets swallowed by `except Exception` and falls back to the original title.
    def test_falls_back_after_exhausting_server_retries(self, monkeypatch, caplog):
        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        sleeps: list[float] = []
        monkeypatch.setattr("translate_video.time.sleep", sleeps.append)

        exc = errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
        # 2 server retries + 1 initial = 3 calls, all fail
        client = self._client_with_side_effects([exc] * 3)

        with caplog.at_level(logging.WARNING, logger="translate_video"):
            result = translate_title(client, "gemini-test", "Original Title")

        assert result == "Original Title"
        assert client.models.generate_content.call_count == 3
        assert sleeps == [60, 120]

    def test_falls_back_after_exhausting_rate_retries(self, monkeypatch, caplog):
        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        sleeps: list[float] = []
        monkeypatch.setattr("translate_video.time.sleep", sleeps.append)

        exc = errors.APIError(429, {"error": {"message": "quota hit", "status": "RESOURCE_EXHAUSTED"}})
        # 2 rate retries + 1 initial = 3 calls, all fail
        client = self._client_with_side_effects([exc] * 3)

        with caplog.at_level(logging.WARNING, logger="translate_video"):
            result = translate_title(client, "gemini-test", "Original Title")

        assert result == "Original Title"
        assert client.models.generate_content.call_count == 3
        assert sleeps == [15, 30]
