"""Tests for pure utility functions in video_intel.py."""

from datetime import UTC, datetime, timedelta

from video_intel import (
    merge_transcript_json,
    parse_since,
    slugify,
    timestamp_to_seconds,
    video_file_prefix,
)

# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_slugify_simple_title_returns_lowercase_slug(self):
        assert slugify("Building MCP Agents") == "building-mcp-agents"

    def test_slugify_special_chars_returns_clean_slug(self):
        assert slugify("What's New? (2026 Edition!)") == "whats-new-2026-edition"

    def test_slugify_long_title_truncates_at_max_len(self):
        result = slugify("a" * 100, max_len=20)
        assert len(result) == 20

    def test_slugify_trailing_dash_after_truncation_stripped(self):
        result = slugify("hello-world-this-is-a-long-title", max_len=12)
        assert not result.endswith("-")

    def test_slugify_multiple_spaces_collapsed_to_single_dash(self):
        assert slugify("too   many   spaces") == "too-many-spaces"

    def test_slugify_already_clean_returns_unchanged(self):
        assert slugify("clean-slug") == "clean-slug"

    def test_slugify_empty_string_returns_empty(self):
        assert slugify("") == ""


# ---------------------------------------------------------------------------
# timestamp_to_seconds
# ---------------------------------------------------------------------------


class TestTimestampToSeconds:
    def test_timestamp_mm_ss_returns_seconds(self):
        assert timestamp_to_seconds("01:30") == 90

    def test_timestamp_h_mm_ss_returns_seconds(self):
        assert timestamp_to_seconds("1:30:00") == 5400

    def test_timestamp_zero_returns_zero(self):
        assert timestamp_to_seconds("00:00") == 0

    def test_timestamp_invalid_format_returns_zero(self):
        assert timestamp_to_seconds("invalid") == 0

    def test_timestamp_large_minutes_returns_correct(self):
        assert timestamp_to_seconds("45:59") == 2759


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


class TestParseSince:
    def test_parse_since_relative_days_returns_past_datetime(self):
        result = parse_since("10d")
        expected = datetime.now(UTC) - timedelta(days=10)
        assert abs((result - expected).total_seconds()) < 2

    def test_parse_since_absolute_date_returns_datetime(self):
        result = parse_since("2026-01-15")
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 15

    def test_parse_since_absolute_date_has_utc_timezone(self):
        result = parse_since("2026-03-01")
        assert result.tzinfo == UTC


# ---------------------------------------------------------------------------
# video_file_prefix
# ---------------------------------------------------------------------------


class TestVideoFilePrefix:
    def test_video_file_prefix_formats_date_and_slug(self):
        video = {"published": "2026-03-20", "title": "Building MCP Agents"}
        assert video_file_prefix(video) == "2026-03-20-building-mcp-agents"

    def test_video_file_prefix_handles_special_chars_in_title(self):
        video = {"published": "2026-01-01", "title": "What's New? (2026!)"}
        result = video_file_prefix(video)
        assert result.startswith("2026-01-01-")
        assert "?" not in result


# ---------------------------------------------------------------------------
# merge_transcript_json
# ---------------------------------------------------------------------------


class TestMergeTranscriptJson:
    def test_merge_transcript_json_speech_entries_formatted(self):
        # Arrange
        raw = {
            "transcripts": [
                {"start": "00:10", "voice": 1, "text": "Hello world."},
            ],
            "screen_content": [],
            "speakers": [
                {"voice": 1, "name": "Alice", "role": "Host", "evidence": "Name card visible"},
            ],
        }

        # Act
        result = merge_transcript_json(raw, {})

        # Assert
        assert '[00:10] Alice (Host): "Hello world."' in result

    def test_merge_transcript_json_screen_entries_formatted(self):
        # Arrange
        raw = {
            "transcripts": [],
            "screen_content": [
                {
                    "start": "01:00",
                    "end": "01:15",
                    "type": "slide",
                    "description": "Title slide with logo",
                },
            ],
            "speakers": [],
        }

        # Act
        result = merge_transcript_json(raw, {})

        # Assert
        assert "SCREEN [01:00-01:15] [slide]: Title slide with logo" in result

    def test_merge_transcript_json_entries_sorted_by_timestamp(self):
        # Arrange
        raw = {
            "transcripts": [
                {"start": "02:00", "voice": 1, "text": "Second."},
                {"start": "00:30", "voice": 1, "text": "First."},
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Bob"}],
        }

        # Act
        result = merge_transcript_json(raw, {})

        # Assert
        first_pos = result.index("First.")
        second_pos = result.index("Second.")
        assert first_pos < second_pos

    def test_merge_transcript_json_speaker_evidence_in_footer(self):
        # Arrange
        raw = {
            "transcripts": [],
            "screen_content": [],
            "speakers": [
                {"voice": 1, "name": "Alice", "evidence": "Zoom label visible at 0:05"},
            ],
        }

        # Act
        result = merge_transcript_json(raw, {})

        # Assert
        assert "Speaker Identification Evidence" in result
        assert "Zoom label visible at 0:05" in result

    def test_merge_transcript_json_list_input_unwrapped(self):
        # Arrange — Gemini sometimes wraps response in an array
        raw = [
            {
                "transcripts": [{"start": "00:00", "voice": 1, "text": "Wrapped."}],
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        ]

        # Act
        result = merge_transcript_json(raw, {})

        # Assert
        assert "Wrapped." in result

    def test_merge_transcript_json_empty_input_returns_empty(self):
        result = merge_transcript_json({}, {})
        assert result == ""
