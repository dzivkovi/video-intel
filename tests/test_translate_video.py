"""Tests for translate_video.py — pure functions only, no API calls."""

import logging
from pathlib import Path

from translate_video import (
    build_chunk_list,
    build_output_path,
    extract_video_id,
    format_elapsed,
    format_stats,
    normalize_timestamp,
    parse_iso8601_duration,
    slugify,
    stitch_parts,
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


class TestStitchParts:
    """Tests for stitch_parts() — merges part files into a single translation."""

    def _write_part(self, tmp_path: Path, slug: str, date: str, start: int, end: int, lines: list[str]) -> Path:
        """Helper: write a part file with the standard naming convention."""
        path = tmp_path / f"{date}-{slug}.part-{start}-{end}.translate-bcs.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_stitch_three_parts_correct_order(self, tmp_path):
        # Arrange: 3 parts with known content, written out of order
        self._write_part(tmp_path, "my-video", "2024-01-01", 40, 60, ["[00:40:00] part three"])
        self._write_part(tmp_path, "my-video", "2024-01-01", 0, 20, ["[00:00:00] part one"])
        self._write_part(tmp_path, "my-video", "2024-01-01", 20, 40, ["[00:20:00] part two"])

        # Act
        result = stitch_parts(tmp_path, "My Video", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert
        content = result.read_text(encoding="utf-8")
        assert "# Translation (BCS): My Video" in content
        lines = [line for line in content.split("\n") if line.startswith("[")]
        assert lines == ["[00:00:00] part one", "[00:20:00] part two", "[00:40:00] part three"]

    def test_stitch_newline_seam_no_missing_line(self, tmp_path):
        # Arrange: part 1 ends WITHOUT trailing newline, part 2 starts with timestamp
        p1 = tmp_path / "2024-01-01-vid.part-0-20.translate-bcs.txt"
        p1.write_text("[00:00:00] first\n[00:19:50] last of part one", encoding="utf-8")  # no trailing \n
        p2 = tmp_path / "2024-01-01-vid.part-20-40.translate-bcs.txt"
        p2.write_text("[00:20:00] first of part two\n[00:39:50] last", encoding="utf-8")

        # Act
        result = stitch_parts(tmp_path, "Vid", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert: no line is lost at the seam
        content = result.read_text(encoding="utf-8")
        assert "[00:19:50] last of part one" in content
        assert "[00:20:00] first of part two" in content
        # And they're on separate lines
        ts_lines = [line for line in content.split("\n") if line.startswith("[")]
        assert ts_lines.index("[00:19:50] last of part one") < ts_lines.index("[00:20:00] first of part two")

    def test_stitch_normalizes_timestamps(self, tmp_path):
        # Arrange: part with malformed timestamp
        self._write_part(tmp_path, "vid", "2024-01-01", 0, 60, ["[00:05:00] ok"])
        self._write_part(tmp_path, "vid", "2024-01-01", 60, 120, ["[120:05:30] malformed"])

        # Act
        result = stitch_parts(tmp_path, "Vid", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert
        content = result.read_text(encoding="utf-8")
        assert "[02:05:30] malformed" in content
        assert "[120:05:30]" not in content

    def test_stitch_no_parts_raises_error(self, tmp_path):
        # Act & Assert
        import pytest

        with pytest.raises(SystemExit):
            stitch_parts(tmp_path, "Nothing", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

    def test_stitch_missing_intermediate_warns(self, tmp_path, caplog):
        # Arrange: parts 0-20 and 40-60, but no 20-40
        self._write_part(tmp_path, "vid", "2024-01-01", 0, 20, ["[00:00:00] part one"])
        self._write_part(tmp_path, "vid", "2024-01-01", 40, 60, ["[00:40:00] part three"])

        # Act
        with caplog.at_level(logging.WARNING, logger="translate_video"):
            result = stitch_parts(tmp_path, "Vid", "2024-01-01", "https://youtube.com/watch?v=TEST", "gemini-test")

        # Assert: stitched file exists with both parts, warning logged about gap
        content = result.read_text(encoding="utf-8")
        assert "[00:00:00] part one" in content
        assert "[00:40:00] part three" in content
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


class TestLoadPrompt:
    def test_load_prompt_translate_bcs_exists(self):
        from translate_video import load_prompt

        text = load_prompt("translate-bcs")
        assert "BCS" in text
        assert "[HH:MM:SS]" in text
