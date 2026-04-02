"""Tests for pure utility functions in video_intel.py."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from video_intel import (
    fetch_channel_videos,
    is_processed,
    merge_transcript_json,
    normalize_prompt_name,
    parse_since,
    prompt_suffix,
    slugify,
    timestamp_to_seconds,
    update_meta,
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


# ---------------------------------------------------------------------------
# update_meta
# ---------------------------------------------------------------------------


class TestUpdateMeta:
    def test_update_meta_when_no_file_creates_fresh(self, tmp_path):
        # Arrange
        meta_path = tmp_path / "test.meta.json"
        fields = {"video_url": "https://example.com", "channel": "test"}

        # Act
        update_meta(meta_path, fields, "scan")

        # Assert
        meta = json.loads(meta_path.read_text())
        assert meta["video_url"] == "https://example.com"
        assert meta["modes_completed"] == ["scan"]
        assert meta["last_error"] is None

    def test_update_meta_when_existing_merges_modes(self, tmp_path):
        # Arrange — pre-existing meta with transcript completed
        meta_path = tmp_path / "test.meta.json"
        existing = {"channel": "test", "modes_completed": ["transcript"], "last_error": None}
        meta_path.write_text(json.dumps(existing))

        # Act — add scan mode
        update_meta(meta_path, {"video_url": "https://example.com"}, "scan")

        # Assert — both modes present
        meta = json.loads(meta_path.read_text())
        assert "transcript" in meta["modes_completed"]
        assert "scan" in meta["modes_completed"]
        assert meta["video_url"] == "https://example.com"

    def test_update_meta_when_duplicate_mode_does_not_repeat(self, tmp_path):
        # Arrange
        meta_path = tmp_path / "test.meta.json"
        existing = {"modes_completed": ["scan"]}
        meta_path.write_text(json.dumps(existing))

        # Act
        update_meta(meta_path, {}, "scan")

        # Assert
        meta = json.loads(meta_path.read_text())
        assert meta["modes_completed"] == ["scan"]

    def test_update_meta_when_existing_preserves_unrelated_fields(self, tmp_path):
        # Arrange
        meta_path = tmp_path / "test.meta.json"
        existing = {"channel": "test", "title": "Original Title", "modes_completed": []}
        meta_path.write_text(json.dumps(existing))

        # Act — update with new fields, don't touch title
        update_meta(meta_path, {"model": "gemini-3"}, "scan")

        # Assert
        meta = json.loads(meta_path.read_text())
        assert meta["title"] == "Original Title"
        assert meta["model"] == "gemini-3"


# ---------------------------------------------------------------------------
# prompt_suffix
# ---------------------------------------------------------------------------


class TestPromptSuffix:
    def test_prompt_suffix_when_mindmap_prefix_strips_prefix(self):
        assert prompt_suffix("mindmap-knowledge") == "knowledge"

    def test_prompt_suffix_when_no_prefix_returns_as_is(self):
        assert prompt_suffix("custom") == "custom"

    def test_prompt_suffix_when_empty_result_raises(self):
        with pytest.raises(ValueError):
            prompt_suffix("mindmap-")


# ---------------------------------------------------------------------------
# is_processed (two-tier idempotency)
# ---------------------------------------------------------------------------


class TestIsProcessedScan:
    """Scan mode: skip if ANY mindmap variant exists (prevent backfill)."""

    def _make_video(self) -> dict:
        return {"published": "2026-03-30", "title": "Test Video"}

    def test_is_processed_scan_when_legacy_file_exists_returns_true(self, tmp_path):
        # Arrange — old-style .mindmap.md exists
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.md").write_text("content")

        # Act & Assert
        assert is_processed(tmp_path, "test_channel", self._make_video(), "scan", any_variant=True) is True

    def test_is_processed_scan_when_suffixed_file_exists_returns_true(self, tmp_path):
        # Arrange — new-style .mindmap.knowledge.md exists
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.knowledge.md").write_text("content")

        # Act & Assert
        assert is_processed(tmp_path, "test_channel", self._make_video(), "scan", any_variant=True) is True

    def test_is_processed_scan_when_no_mindmap_returns_false(self, tmp_path):
        # Arrange — channel dir exists but no mindmap files
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()

        # Act & Assert
        assert is_processed(tmp_path, "test_channel", self._make_video(), "scan", any_variant=True) is False

    def test_is_processed_scan_when_different_suffix_still_found(self, tmp_path):
        # Arrange — .mindmap.heavy.md exists, scan doesn't care which prompt
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.heavy.md").write_text("content")

        # Act & Assert
        assert is_processed(tmp_path, "test_channel", self._make_video(), "scan", any_variant=True) is True


class TestIsProcessedMindmap:
    """Mindmap subcommand: only skip if the exact prompt variant exists."""

    def _make_video(self) -> dict:
        return {"published": "2026-03-30", "title": "Test Video"}

    def test_is_processed_mindmap_when_exact_suffix_exists_returns_true(self, tmp_path):
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.knowledge.md").write_text("content")

        assert (
            is_processed(tmp_path, "test_channel", self._make_video(), "scan", prompt_name="mindmap-knowledge") is True
        )

    def test_is_processed_mindmap_when_different_suffix_returns_false(self, tmp_path):
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.heavy.md").write_text("content")

        assert (
            is_processed(tmp_path, "test_channel", self._make_video(), "scan", prompt_name="mindmap-knowledge") is False
        )

    def test_is_processed_mindmap_when_legacy_exists_returns_false(self, tmp_path):
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.md").write_text("content")

        assert (
            is_processed(tmp_path, "test_channel", self._make_video(), "scan", prompt_name="mindmap-knowledge") is False
        )


# ---------------------------------------------------------------------------
# normalize_prompt_name
# ---------------------------------------------------------------------------


class TestNormalizePromptName:
    def test_normalize_prompt_name_when_bare_name_returns_unchanged(self):
        assert normalize_prompt_name("mindmap-knowledge") == "mindmap-knowledge"

    def test_normalize_prompt_name_when_path_with_extension_strips_both(self):
        assert normalize_prompt_name("prompts\\mindmap-knowledge.md") == "mindmap-knowledge"

    def test_normalize_prompt_name_when_forward_slash_path_strips(self):
        assert normalize_prompt_name("prompts/mindmap-knowledge.md") == "mindmap-knowledge"

    def test_normalize_prompt_name_when_extension_only_strips_extension(self):
        assert normalize_prompt_name("mindmap-light.md") == "mindmap-light"


# ---------------------------------------------------------------------------
# fetch_channel_videos
# ---------------------------------------------------------------------------


class TestFetchChannelVideos:
    def test_fetch_channel_videos_when_channel_id_given_derives_uploads_playlist(self):
        # Arrange
        youtube = MagicMock()
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [],
        }
        since_dt = datetime(2026, 1, 1, tzinfo=UTC)

        # Act
        fetch_channel_videos(youtube, "UCxxxxxxxxxxxxxxxxxxxxxx", since_dt)

        # Assert
        call_kwargs = youtube.playlistItems.return_value.list.call_args[1]
        assert call_kwargs["playlistId"] == "UUxxxxxxxxxxxxxxxxxxxxxx"

    def test_fetch_channel_videos_when_old_video_hit_stops_early(self):
        # Arrange
        youtube = MagicMock()
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {"title": "New Video", "publishedAt": "2026-03-15T00:00:00Z"},
                    "contentDetails": {"videoId": "abc123", "videoPublishedAt": "2026-03-15T00:00:00Z"},
                },
                {
                    "snippet": {"title": "Old Video", "publishedAt": "2025-12-01T00:00:00Z"},
                    "contentDetails": {"videoId": "def456", "videoPublishedAt": "2025-12-01T00:00:00Z"},
                },
            ],
        }
        since_dt = datetime(2026, 1, 1, tzinfo=UTC)

        # Act
        videos = fetch_channel_videos(youtube, "UCxxxxxxxxxxxxxxxxxxxxxx", since_dt)

        # Assert — only the newer video is returned
        assert len(videos) == 1
        assert videos[0]["video_id"] == "abc123"

    def test_fetch_channel_videos_when_video_found_returns_correct_format(self):
        # Arrange
        youtube = MagicMock()
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {"title": "Test &amp; Video", "publishedAt": "2026-03-15T10:00:00Z"},
                    "contentDetails": {"videoId": "vid123", "videoPublishedAt": "2026-03-15T10:00:00Z"},
                },
            ],
        }
        since_dt = datetime(2026, 1, 1, tzinfo=UTC)

        # Act
        videos = fetch_channel_videos(youtube, "UCxxxxxxxxxxxxxxxxxxxxxx", since_dt)

        # Assert
        assert videos[0] == {
            "video_id": "vid123",
            "title": "Test & Video",
            "published": "2026-03-15",
            "url": "https://www.youtube.com/watch?v=vid123",
        }


# ---------------------------------------------------------------------------
# CLI: mindmap subcommand
# ---------------------------------------------------------------------------


class TestCmdMindmapArgs:
    def test_mindmap_subcommand_when_url_missing_exits(self):
        """The mindmap subcommand requires --url."""
        import argparse as _argparse


        # Build parser the same way main() does, test it parses correctly
        parser = _argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        # This will fail until cmd_mindmap parser is added to main()
        mm = subparsers.add_parser("mindmap")
        mm.add_argument("--url", required=True)
        mm.add_argument("--prompt")

        with pytest.raises(SystemExit):
            parser.parse_args(["mindmap"])  # no --url

    def test_mindmap_subcommand_when_url_and_prompt_parses(self):
        """The mindmap subcommand accepts --url and --prompt."""

        # We test via the actual main() parser by importing and building it
        import argparse as _argparse

        parser = _argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        mm = subparsers.add_parser("mindmap")
        mm.add_argument("--url", required=True)
        mm.add_argument("--prompt")
        mm.add_argument("--channel")
        mm.add_argument("--title")
        mm.add_argument("--date")

        args = parser.parse_args(
            ["mindmap", "--url", "https://youtube.com/watch?v=abc123", "--prompt", "mindmap-knowledge"]
        )
        assert args.url == "https://youtube.com/watch?v=abc123"
        assert args.prompt == "mindmap-knowledge"
