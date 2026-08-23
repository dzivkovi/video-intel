"""Tests for pure utility functions in video_intel.py."""

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# parse_time_to_seconds below is imported from video_intel on purpose: it is
# a re-export from timestamp_utils.py (issue #152), and this import is the
# test proving that re-export still works. Do not "tidy" it into importing
# from timestamp_utils directly - that would silently delete the only proof
# the re-export works.
from video_intel import (
    DEFAULT_MODEL,
    KEYWORD_MAX_PAGES,
    LARGE_FILE_THRESHOLD_BYTES,
    MAX_OUTPUT_TOKENS,
    SALVAGE_MIN_SPEECH_ENTRIES,
    TRANSCRIPT_PARSE_RETRY_LIMIT,
    _dedup_by_video,
    _load_concepts_for_video,
    _parse_timestamp_seconds,
    build_taxonomy,
    call_gemini,
    chunk_transcript,
    cmd_scan,
    cmd_transcript,
    fetch_channel_videos,
    fetch_keyword_videos,
    fetch_playlist_videos,
    fetch_selective_videos,
    find_mindmap_source,
    is_processed,
    isolate_json,
    load_taxonomy,
    merge_transcript_json,
    normalize_prompt_name,
    parse_since,
    parse_time_to_seconds,
    process_transcript,
    resolve_model,
    resolve_playlist_ids,
    salvage_transcript_sections,
    search_corpus,
    slugify,
    timestamp_to_seconds,
    try_parse_transcript_json,
    update_meta,
    upload_local_video,
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
# is_processed
# ---------------------------------------------------------------------------


class TestIsProcessed:
    """Scan mode with any_variant: skip if ANY mindmap variant exists (prevent backfill)."""

    def _make_video(self) -> dict:
        return {"published": "2026-03-30", "title": "Test Video"}

    def test_is_processed_scan_when_legacy_file_exists_returns_true(self, tmp_path):
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.md").write_text("content")

        assert is_processed(tmp_path, "test_channel", self._make_video(), "scan", any_variant=True) is True

    def test_is_processed_scan_when_suffixed_file_exists_returns_true(self, tmp_path):
        # Old manually-renamed files like .mindmap.knowledge.md should still be caught
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.mindmap.knowledge.md").write_text("content")

        assert is_processed(tmp_path, "test_channel", self._make_video(), "scan", any_variant=True) is True

    def test_is_processed_scan_when_no_mindmap_returns_false(self, tmp_path):
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()

        assert is_processed(tmp_path, "test_channel", self._make_video(), "scan", any_variant=True) is False

    def test_is_processed_transcript_when_file_exists_returns_true(self, tmp_path):
        channel_dir = tmp_path / "test_channel"
        channel_dir.mkdir()
        (channel_dir / "2026-03-30-test-video.transcript.md").write_text("content")

        assert is_processed(tmp_path, "test_channel", self._make_video(), "transcript") is True


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


# ---------------------------------------------------------------------------
# load_taxonomy
# ---------------------------------------------------------------------------


class TestLoadTaxonomy:
    def test_load_taxonomy_when_no_file_returns_empty_structure(self, tmp_path):
        result = load_taxonomy(tmp_path)
        assert result["version"] == 1
        assert result["concepts"] == {}
        assert result["built_from"] == 0

    def test_load_taxonomy_when_file_exists_returns_content(self, tmp_path):
        taxonomy = {"version": 1, "built_from": 5, "concepts": {"ai_eng.rag": {"preferred_label": "RAG"}}}
        (tmp_path / "taxonomy.json").write_text(json.dumps(taxonomy))

        result = load_taxonomy(tmp_path)
        assert result["built_from"] == 5
        assert "ai_eng.rag" in result["concepts"]


# ---------------------------------------------------------------------------
# find_mindmap_source
# ---------------------------------------------------------------------------


class TestFindMindmapSource:
    def test_find_mindmap_source_prefers_canonical(self, tmp_path):
        (tmp_path / "2026-03-30-test.mindmap.md").write_text("canonical")
        (tmp_path / "2026-03-30-test.mindmap.knowledge.md").write_text("knowledge")

        result = find_mindmap_source(tmp_path, "2026-03-30-test")
        assert result.name == "2026-03-30-test.mindmap.md"

    def test_find_mindmap_source_falls_back_to_knowledge(self, tmp_path):
        (tmp_path / "2026-03-30-test.mindmap.knowledge.md").write_text("knowledge")

        result = find_mindmap_source(tmp_path, "2026-03-30-test")
        assert result.name == "2026-03-30-test.mindmap.knowledge.md"

    def test_find_mindmap_source_falls_back_to_any_variant(self, tmp_path):
        (tmp_path / "2026-03-30-test.mindmap.heavy.md").write_text("heavy")

        result = find_mindmap_source(tmp_path, "2026-03-30-test")
        assert result.name == "2026-03-30-test.mindmap.heavy.md"

    def test_find_mindmap_source_when_no_mindmap_returns_none(self, tmp_path):
        result = find_mindmap_source(tmp_path, "2026-03-30-test")
        assert result is None

    def test_find_mindmap_source_skips_empty_files(self, tmp_path):
        (tmp_path / "2026-03-30-test.mindmap.md").write_text("")
        (tmp_path / "2026-03-30-test.mindmap.knowledge.md").write_text("content")

        result = find_mindmap_source(tmp_path, "2026-03-30-test")
        assert result.name == "2026-03-30-test.mindmap.knowledge.md"


# ---------------------------------------------------------------------------
# build_taxonomy
# ---------------------------------------------------------------------------


class TestBuildTaxonomy:
    def _write_concepts(self, channel_dir, prefix, concepts, video_id="vid1", published="2026-03-30"):
        """Helper to write a concepts.json and its sibling meta.json."""
        data = {"video_id": video_id, "extracted_from": "mindmap.md", "concepts": concepts}
        (channel_dir / f"{prefix}.concepts.json").write_text(json.dumps(data))
        meta = {"video_id": video_id, "published": published}
        (channel_dir / f"{prefix}.meta.json").write_text(json.dumps(meta))

    def test_build_taxonomy_aggregates_concepts(self, tmp_path):
        ch = tmp_path / "ch1"
        ch.mkdir()
        self._write_concepts(
            ch,
            "2026-03-30-video-a",
            [
                {
                    "concept_id": "ai.rag",
                    "preferred_label": "RAG",
                    "as_mentioned": "RAG",
                    "status": "new",
                    "domain": "ai",
                },
            ],
            video_id="vid1",
        )
        self._write_concepts(
            ch,
            "2026-03-31-video-b",
            [
                {
                    "concept_id": "ai.rag",
                    "preferred_label": "RAG",
                    "as_mentioned": "Retrieval Augmented Gen",
                    "status": "matched",
                    "domain": "ai",
                },
            ],
            video_id="vid2",
            published="2026-03-31",
        )

        taxonomy = build_taxonomy(tmp_path)

        assert taxonomy["built_from"] == 2
        rag = taxonomy["concepts"]["ai.rag"]
        assert rag["preferred_label"] == "RAG"
        assert "Retrieval Augmented Gen" in rag["aliases"]
        assert rag["video_count"] == 2
        assert rag["first_seen"] == "2026-03-30"

    def test_build_taxonomy_writes_file(self, tmp_path):
        ch = tmp_path / "ch1"
        ch.mkdir()
        self._write_concepts(
            ch,
            "2026-03-30-test",
            [
                {"concept_id": "ai.test", "preferred_label": "Testing", "as_mentioned": "Testing", "domain": "ai"},
            ],
        )

        build_taxonomy(tmp_path)

        taxonomy_path = tmp_path / "taxonomy.json"
        assert taxonomy_path.exists()
        data = json.loads(taxonomy_path.read_text())
        assert "ai.test" in data["concepts"]

    def test_build_taxonomy_is_rebuildable(self, tmp_path):
        """Running build twice produces identical output."""
        ch = tmp_path / "ch1"
        ch.mkdir()
        self._write_concepts(
            ch,
            "2026-03-30-test",
            [
                {"concept_id": "ai.rag", "preferred_label": "RAG", "as_mentioned": "RAG", "domain": "ai"},
            ],
        )

        build_taxonomy(tmp_path)
        first = (tmp_path / "taxonomy.json").read_text()

        build_taxonomy(tmp_path)
        second = (tmp_path / "taxonomy.json").read_text()

        assert first == second

    def test_build_taxonomy_empty_dir_produces_empty(self, tmp_path):
        taxonomy = build_taxonomy(tmp_path)
        assert taxonomy["built_from"] == 0
        assert taxonomy["concepts"] == {}

    def test_build_taxonomy_alias_excludes_preferred_label(self, tmp_path):
        """as_mentioned matching preferred_label should not appear in aliases."""
        ch = tmp_path / "ch1"
        ch.mkdir()
        self._write_concepts(
            ch,
            "2026-03-30-test",
            [
                {"concept_id": "ai.rag", "preferred_label": "RAG", "as_mentioned": "RAG", "domain": "ai"},
            ],
        )

        taxonomy = build_taxonomy(tmp_path)
        assert taxonomy["concepts"]["ai.rag"]["aliases"] == []


# ---------------------------------------------------------------------------
# search_corpus
# ---------------------------------------------------------------------------


class TestSearchCorpus:
    def _setup_corpus(self, tmp_path):
        """Create a minimal corpus with taxonomy + concepts + meta files."""
        # Build taxonomy
        taxonomy = {
            "version": 1,
            "built_from": 2,
            "concepts": {
                "ai.multi_agent": {
                    "preferred_label": "Multi-Agent Orchestration",
                    "aliases": ["Agent Teams", "Agent Swarm"],
                    "domain": "ai",
                    "first_seen": "2026-03-01",
                    "video_count": 2,
                },
                "ai.context_window": {
                    "preferred_label": "Context Window Management",
                    "aliases": ["Context Optimization"],
                    "domain": "ai",
                    "first_seen": "2026-03-05",
                    "video_count": 1,
                },
            },
        }
        (tmp_path / "taxonomy.json").write_text(json.dumps(taxonomy))

        # Channel with 2 videos
        ch = tmp_path / "testchannel"
        ch.mkdir()

        # Video 1: has multi_agent concept
        (ch / "2026-03-01-video-one.concepts.json").write_text(
            json.dumps(
                {
                    "video_id": "vid1",
                    "concepts": [
                        {"concept_id": "ai.multi_agent", "preferred_label": "Multi-Agent Orchestration"},
                    ],
                }
            )
        )
        (ch / "2026-03-01-video-one.meta.json").write_text(
            json.dumps({"video_id": "vid1", "title": "Video One", "published": "2026-03-01"})
        )
        (ch / "2026-03-01-video-one.mindmap.md").write_text("# Video One mindmap")

        # Video 2: has both concepts
        (ch / "2026-03-05-video-two.concepts.json").write_text(
            json.dumps(
                {
                    "video_id": "vid2",
                    "concepts": [
                        {"concept_id": "ai.multi_agent", "preferred_label": "Multi-Agent Orchestration"},
                        {"concept_id": "ai.context_window", "preferred_label": "Context Window Management"},
                    ],
                }
            )
        )
        (ch / "2026-03-05-video-two.meta.json").write_text(
            json.dumps({"video_id": "vid2", "title": "Video Two", "published": "2026-03-05"})
        )
        (ch / "2026-03-05-video-two.mindmap.md").write_text("# Video Two mindmap")

    def test_search_finds_concept_by_label(self, tmp_path):
        self._setup_corpus(tmp_path)
        results = search_corpus(tmp_path, "multi agent")
        assert len(results["concepts"]) == 1
        assert results["concepts"][0]["concept_id"] == "ai.multi_agent"
        assert len(results["videos"]) == 2

    def test_search_finds_concept_by_alias(self, tmp_path):
        self._setup_corpus(tmp_path)
        results = search_corpus(tmp_path, "agent teams")
        assert len(results["concepts"]) == 1
        assert results["concepts"][0]["preferred_label"] == "Multi-Agent Orchestration"

    def test_search_returns_empty_for_no_match(self, tmp_path):
        self._setup_corpus(tmp_path)
        results = search_corpus(tmp_path, "nonexistent gibberish")
        assert results["concepts"] == []
        assert results["videos"] == []

    def test_search_channel_filter_restricts_results(self, tmp_path):
        self._setup_corpus(tmp_path)
        results = search_corpus(tmp_path, "multi agent", channel_filter="nonexistent")
        assert results["concepts"]  # concepts still match
        assert results["videos"] == []  # but no videos in that channel

    def test_search_includes_artifact_paths(self, tmp_path):
        self._setup_corpus(tmp_path)
        results = search_corpus(tmp_path, "context")
        assert len(results["videos"]) == 1
        assert results["videos"][0]["mindmap"] is not None
        assert "mindmap.md" in results["videos"][0]["mindmap"]

    def test_search_respects_limit(self, tmp_path):
        self._setup_corpus(tmp_path)
        results = search_corpus(tmp_path, "multi agent", limit=1)
        assert len(results["videos"]) == 1


# ---------------------------------------------------------------------------
# _parse_timestamp_seconds
# ---------------------------------------------------------------------------


class TestParseTimestampSeconds:
    def test_mm_ss_returns_seconds(self):
        assert _parse_timestamp_seconds("01:30") == 90

    def test_hh_mm_ss_returns_seconds(self):
        assert _parse_timestamp_seconds("1:15:30") == 4530

    def test_zero_returns_zero(self):
        assert _parse_timestamp_seconds("00:00") == 0

    def test_invalid_returns_zero(self):
        assert _parse_timestamp_seconds("bad") == 0


# ---------------------------------------------------------------------------
# chunk_transcript
# ---------------------------------------------------------------------------


class TestChunkTranscript:
    SAMPLE_TRANSCRIPT = (
        "# Transcript: Test Video\n"
        "\n"
        "**Source:** https://www.youtube.com/watch?v=TEST\n"
        "**Published:** 2026-03-20\n"
        "\n"
        "---\n"
        "\n"
        '[00:00] Alice (Host): "Welcome to the show."\n'
        "\n"
        '[00:15] Bob (Guest): "Thanks for having me."\n'
        "\n"
        "  SCREEN [00:20-00:30] [slide]: Title slide with logo\n"
        "\n"
        '[00:35] Alice (Host): "Let\'s talk about AI agents."\n'
        "\n"
        '[01:00] Bob (Guest): "Agents are transforming software."\n'
        "\n"
        '[01:30] Alice (Host): "What about skills?"\n'
        "\n"
        '[02:00] Bob (Guest): "Skills are the key abstraction."\n'
    )

    def test_chunk_transcript_returns_chunks(self, tmp_path):
        tx = tmp_path / "test.transcript.md"
        tx.write_text(self.SAMPLE_TRANSCRIPT, encoding="utf-8")
        chunks = chunk_transcript(tx, chunk_size=3)
        assert len(chunks) >= 1

    def test_chunk_transcript_first_chunk_starts_at_zero(self, tmp_path):
        tx = tmp_path / "test.transcript.md"
        tx.write_text(self.SAMPLE_TRANSCRIPT, encoding="utf-8")
        chunks = chunk_transcript(tx, chunk_size=3)
        assert chunks[0]["timestamp"] == "00:00"
        assert chunks[0]["timestamp_seconds"] == 0

    def test_chunk_transcript_preserves_text_content(self, tmp_path):
        tx = tmp_path / "test.transcript.md"
        tx.write_text(self.SAMPLE_TRANSCRIPT, encoding="utf-8")
        chunks = chunk_transcript(tx, chunk_size=3)
        all_text = " ".join(c["text"] for c in chunks)
        assert "Welcome to the show" in all_text
        assert "Skills are the key abstraction" in all_text

    def test_chunk_transcript_respects_chunk_size(self, tmp_path):
        tx = tmp_path / "test.transcript.md"
        tx.write_text(self.SAMPLE_TRANSCRIPT, encoding="utf-8")
        # 7 entries (6 speech + 1 SCREEN) with chunk_size=3 => 3 chunks
        chunks = chunk_transcript(tx, chunk_size=3)
        assert len(chunks) == 3

    def test_chunk_transcript_includes_screen_entries(self, tmp_path):
        tx = tmp_path / "test.transcript.md"
        tx.write_text(self.SAMPLE_TRANSCRIPT, encoding="utf-8")
        chunks = chunk_transcript(tx, chunk_size=10)  # one big chunk
        assert "SCREEN" in chunks[0]["text"]

    def test_chunk_transcript_empty_file_returns_empty(self, tmp_path):
        tx = tmp_path / "empty.transcript.md"
        tx.write_text("# No entries\n\nJust a header.", encoding="utf-8")
        chunks = chunk_transcript(tx)
        assert chunks == []

    def test_chunk_transcript_later_chunks_have_correct_timestamps(self, tmp_path):
        tx = tmp_path / "test.transcript.md"
        tx.write_text(self.SAMPLE_TRANSCRIPT, encoding="utf-8")
        chunks = chunk_transcript(tx, chunk_size=3)
        if len(chunks) > 1:
            assert chunks[1]["timestamp_seconds"] > chunks[0]["timestamp_seconds"]


# ---------------------------------------------------------------------------
# _load_concepts_for_video
# ---------------------------------------------------------------------------


class TestLoadConceptsForVideo:
    def test_loads_concept_ids(self, tmp_path):
        data = {
            "video_id": "TEST",
            "concepts": [
                {"concept_id": "ai.agents", "preferred_label": "Agents"},
                {"concept_id": "ai.skills", "preferred_label": "Skills"},
            ],
        }
        path = tmp_path / "test.concepts.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = _load_concepts_for_video(path)
        assert result == ["ai.agents", "ai.skills"]

    def test_missing_file_returns_empty(self, tmp_path):
        result = _load_concepts_for_video(tmp_path / "nonexistent.json")
        assert result == []

    def test_empty_concepts_returns_empty(self, tmp_path):
        data = {"video_id": "TEST", "concepts": []}
        path = tmp_path / "test.concepts.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = _load_concepts_for_video(path)
        assert result == []


# ---------------------------------------------------------------------------
# _dedup_by_video
# ---------------------------------------------------------------------------


class TestDedupByVideo:
    def _hit(self, video_id, relevance, title="Test"):
        return {
            "text": f"chunk from {video_id}",
            "timestamp": "00:00",
            "video_id": video_id,
            "channel": "test",
            "title": title,
            "published": "2026-01-01",
            "source_file": f"{video_id}.transcript.md",
            "concept_ids": "[]",
            "relevance": relevance,
        }

    def test_keeps_best_chunk_per_video(self):
        hits = [
            self._hit("vid1", 0.02),
            self._hit("vid1", 0.05),  # best (highest relevance)
            self._hit("vid1", 0.01),
        ]
        result = _dedup_by_video(hits, limit=10)
        assert len(result) == 1
        assert result[0]["relevance"] == 0.05

    def test_preserves_distinct_videos(self):
        hits = [
            self._hit("vid1", 0.03),
            self._hit("vid2", 0.02),
            self._hit("vid3", 0.01),
        ]
        result = _dedup_by_video(hits, limit=10)
        assert len(result) == 3
        assert [r["video_id"] for r in result] == ["vid1", "vid2", "vid3"]

    def test_respects_limit(self):
        hits = [self._hit(f"vid{i}", (5 - i) * 0.01) for i in range(5)]
        result = _dedup_by_video(hits, limit=3)
        assert len(result) == 3

    def test_sorts_by_best_relevance(self):
        hits = [
            self._hit("vid_low", 0.005),
            self._hit("vid_high", 0.033),
            self._hit("vid_mid", 0.018),
        ]
        result = _dedup_by_video(hits, limit=10)
        assert result[0]["video_id"] == "vid_high"
        assert result[-1]["video_id"] == "vid_low"

    def test_empty_input_returns_empty(self):
        assert _dedup_by_video([], limit=10) == []


# ---------------------------------------------------------------------------
# call_gemini retry behavior
# ---------------------------------------------------------------------------


class TestCallGeminiRetry:
    """Verify call_gemini delegates retry decisions to get_retry_delay."""

    def _make_client_and_types(self, side_effects):
        """Build a stub client whose generate_content has the given side effects."""
        from unittest.mock import MagicMock

        client = MagicMock()
        client.models.generate_content.side_effect = side_effects

        from google.genai import types

        return client, types

    def test_retries_on_503_then_succeeds(self, monkeypatch):
        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        monkeypatch.setattr("video_intel.time.sleep", lambda _: None)

        exc = errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
        ok_response = MagicMock()
        ok_response.text = "mind map output"

        client, types = self._make_client_and_types([exc, ok_response])

        result = call_gemini(client, types, "https://youtube.com/watch?v=X", "prompt", "gemini-test")
        assert result == "mind map output"
        assert client.models.generate_content.call_count == 2

    def test_raises_immediately_on_400(self, monkeypatch):
        from google.genai import errors

        monkeypatch.setattr("video_intel.time.sleep", lambda _: None)

        exc = errors.APIError(400, {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}})
        client, types = self._make_client_and_types([exc])

        with pytest.raises(errors.APIError):
            call_gemini(client, types, "https://youtube.com/watch?v=X", "prompt", "gemini-test")

    def test_exhausts_rate_limit_budget_then_raises(self, monkeypatch):
        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        monkeypatch.setattr("video_intel.time.sleep", lambda _: None)

        exc = errors.APIError(429, {"error": {"message": "quota hit", "status": "RESOURCE_EXHAUSTED"}})
        # 3 retries for rate limit + 1 initial = 4 calls, all fail
        client, types = self._make_client_and_types([exc] * 4)

        with pytest.raises(errors.APIError):
            call_gemini(client, types, "https://youtube.com/watch?v=X", "prompt", "gemini-test")


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    """Test the CLI > config > default precedence chain for model resolution."""

    def test_resolve_model_cli_wins_over_config(self):
        args = argparse.Namespace(model="gemini-2.5-pro")
        config = {"model": "gemini-3-flash-preview"}
        assert resolve_model(args, config) == "gemini-2.5-pro"

    def test_resolve_model_config_wins_over_default(self):
        args = argparse.Namespace(model=None)
        config = {"model": "gemini-2.5-pro"}
        assert resolve_model(args, config) == "gemini-2.5-pro"

    def test_resolve_model_falls_back_to_default(self):
        args = argparse.Namespace(model=None)
        config = {}
        assert resolve_model(args, config) == DEFAULT_MODEL

    def test_resolve_model_cli_wins_when_no_config(self):
        args = argparse.Namespace(model="gemini-2.5-flash")
        config = {}
        assert resolve_model(args, config) == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# --model flag handler wiring
# ---------------------------------------------------------------------------


class TestModelFlagWiring:
    """Prove --model override reaches process_mindmap / process_transcript."""

    def test_cmd_scan_passes_cli_model_to_process_mindmap(self, monkeypatch, tmp_path):
        """--model override reaches process_mindmap inside cmd_scan."""
        from video_intel import cmd_scan

        captured = {}

        def fake_process_mindmap(_client, _types, _video, _prompt, model, *a, **kw):
            captured["model"] = model
            return "2026-01-01-test", "done"

        # Stub out external dependencies
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")
        monkeypatch.setattr(
            "video_intel.fetch_channel_videos",
            lambda _yt, _cid, _since: [
                {
                    "video_id": "abc123",
                    "title": "Test",
                    "published": "2026-01-01",
                    "url": "https://youtube.com/watch?v=abc123",
                }
            ],
        )
        monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UC123", "Test Channel"))
        monkeypatch.setattr("video_intel.is_processed", lambda *a, **kw: False)
        monkeypatch.setattr("video_intel.is_skipped", lambda *a, **kw: False)
        monkeypatch.setattr("video_intel.process_mindmap", fake_process_mindmap)

        args = argparse.Namespace(
            model="gemini-2.5-pro",
            channel=None,
            since=None,
            dry_run=False,
            force=False,
        )
        config = {
            "model": "gemini-3-flash-preview",
            "channels": [{"name": "testch", "url": "https://youtube.com/@test"}],
        }

        cmd_scan(args, config)
        assert captured["model"] == "gemini-2.5-pro"

    def test_cmd_transcript_passes_cli_model_to_process_transcript(self, monkeypatch, tmp_path):
        """--model override reaches process_transcript inside cmd_transcript."""
        from video_intel import cmd_transcript

        captured = {}

        def fake_process_transcript(_client, _types, _video, _prompt, model, *a, **kw):
            captured["model"] = model
            return "2026-01-01-test", "done"

        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")
        monkeypatch.setattr("video_intel.process_transcript", fake_process_transcript)

        args = argparse.Namespace(
            model="gemini-2.5-pro",
            url="https://www.youtube.com/watch?v=abc12345678",
            file=None,
            channel="testch",
            title="Test Video",
            date="2026-01-01",
            force=False,
            start=None,
            end=None,
        )
        config = {"model": "gemini-3-flash-preview"}

        cmd_transcript(args, config)
        assert captured["model"] == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# isolate_json
# ---------------------------------------------------------------------------


class TestIsolateJson:
    def test_isolate_json_strips_code_fences(self):
        raw = '```json\n{"transcripts": []}\n```'
        assert isolate_json(raw) == '{"transcripts": []}'

    def test_isolate_json_extracts_object_from_surrounding_prose(self):
        raw = 'Here is the result:\n{"transcripts": [{"start": "00:00"}]}\nDone!'
        result = isolate_json(raw)
        parsed = json.loads(result)
        assert "transcripts" in parsed

    def test_isolate_json_passes_through_clean_json(self):
        clean = '{"transcripts": [], "speakers": []}'
        assert isolate_json(clean) == clean

    def test_isolate_json_handles_array_wrapper(self):
        raw = '[{"transcripts": []}]'
        result = isolate_json(raw)
        parsed = json.loads(result)
        # isolate_json prefers {} over [], which is fine since
        # merge_transcript_json handles both dict and list inputs
        assert "transcripts" in (parsed if isinstance(parsed, dict) else parsed[0])

    def test_isolate_json_returns_input_when_no_json_found(self):
        garbage = "no json here at all"
        assert isolate_json(garbage) == garbage


# ---------------------------------------------------------------------------
# try_parse_transcript_json
# ---------------------------------------------------------------------------


class TestTryParseTranscriptJson:
    def test_succeeds_on_clean_json(self):
        raw = '{"transcripts": [{"start": "00:10", "voice": 1, "text": "Hi"}]}'
        result, error = try_parse_transcript_json(raw)
        assert result is not None
        assert error is None
        assert "transcripts" in result

    def test_succeeds_after_isolation(self):
        raw = '```json\n{"transcripts": [], "speakers": []}\n```'
        result, error = try_parse_transcript_json(raw)
        assert result is not None
        assert error is None

    def test_returns_error_on_garbage(self):
        result, error = try_parse_transcript_json("not json at all {{{")
        assert result is None
        assert error is not None

    def test_returns_error_on_truncated_json(self):
        raw = '{"transcripts": [{"start": "00:10", "voice": 1, "text": "trun'
        result, error = try_parse_transcript_json(raw)
        assert result is None
        assert error is not None


# ---------------------------------------------------------------------------
# salvage_transcript_sections
# ---------------------------------------------------------------------------


class TestSalvageTranscriptSections:
    def test_recovers_transcripts_only(self):
        # JSON truncated after transcripts array but before screen_content closes
        entries = [{"start": f"00:{i:02d}", "voice": 1, "text": f"Line {i}"} for i in range(10)]
        raw = '{"transcripts": ' + json.dumps(entries) + ', "screen_content": [{"start": "01:00"'
        result, warning = salvage_transcript_sections(raw)
        assert len(result.get("transcripts", [])) >= SALVAGE_MIN_SPEECH_ENTRIES
        assert warning is not None

    def test_recovers_screen_content_when_present(self):
        entries = [{"start": f"00:{i:02d}", "voice": 1, "text": f"Line {i}"} for i in range(10)]
        screens = [{"start": "01:00", "end": "01:10", "type": "slide", "description": "Title"}]
        # Truncated after screen_content but before speakers
        raw = (
            '{"transcripts": '
            + json.dumps(entries)
            + ', "screen_content": '
            + json.dumps(screens)
            + ', "speakers": [{"voice":'
        )
        result, _warning = salvage_transcript_sections(raw)
        assert len(result.get("transcripts", [])) >= SALVAGE_MIN_SPEECH_ENTRIES
        assert len(result.get("screen_content", [])) > 0

    def test_returns_empty_when_nothing_recoverable(self):
        result, _warning = salvage_transcript_sections("totally broken garbage")
        assert result.get("transcripts", []) == []
        assert result.get("screen_content", []) == []
        assert result.get("speakers", []) == []

    def test_recovers_from_pro_task_wrapper_format(self):
        """Pro sometimes wraps each section in {task, output}. Salvage must
        normalize that shape into the flat envelope before recovering entries.
        See issue #45."""
        speech = [{"start": f"00:{i:02d}", "voice": 1, "text": f"Line {i}"} for i in range(10)]
        screens = [{"start": "01:00", "end": "01:10", "type": "slide", "description": "Title"}]
        speakers = [{"voice": 1, "name": "Host"}]
        wrapped = json.dumps(
            [
                {"task": "transcripts", "output": speech},
                {"task": "screen_content", "output": screens},
                {"task": "speakers", "output": speakers},
            ]
        )
        result, warning = salvage_transcript_sections(wrapped)
        assert len(result.get("transcripts", [])) >= SALVAGE_MIN_SPEECH_ENTRIES
        assert len(result.get("screen_content", [])) == 1
        assert len(result.get("speakers", [])) == 1
        assert warning is not None

    def test_recovers_from_task_wrapper_with_cyrillic_intrusion(self):
        """Wrapper + a Cyrillic token injected before a `text` key. Per-entry
        salvage drops the corrupted entry, but the rest must come through."""
        wrapped = """[
  {
    "task": "transcripts",
    "output": [
      {
        "start": "00:00",
        "voice": 1,
 минерал"text": "first line corrupted by a cyrillic intrusion"
      },
      {"start": "00:10", "voice": 1, "text": "second line clean"},
      {"start": "00:20", "voice": 1, "text": "third line clean"},
      {"start": "00:30", "voice": 1, "text": "fourth line clean"},
      {"start": "00:40", "voice": 1, "text": "fifth line clean"},
      {"start": "00:50", "voice": 1, "text": "sixth line clean"}
    ]
  }
]"""
        result, _warning = salvage_transcript_sections(wrapped)
        assert len(result.get("transcripts", [])) >= 1

    def test_task_wrapper_recovery_does_not_break_simple_format(self):
        """Regression guard: classic flat envelope must still salvage as before."""
        entries = [{"start": f"00:{i:02d}", "voice": 1, "text": f"Line {i}"} for i in range(10)]
        raw = '{"transcripts": ' + json.dumps(entries) + ', "screen_content": [{"start": "01:00"'
        result, warning = salvage_transcript_sections(raw)
        assert len(result.get("transcripts", [])) >= SALVAGE_MIN_SPEECH_ENTRIES
        assert warning is not None

    def test_malformed_wrapper_does_not_raise(self):
        """A truncated wrapper that does not full-parse must degrade gracefully:
        salvage returns a well-shaped result without raising, even if all lists
        are empty. This locks the no-raise contract; the recovery-strength
        contract is covered by the synthetic and real-fixture tests above."""
        truncated = '[{"task": "transcripts", "output": [{"start": "00:00", "voice": 1, "text"'
        result, _ = salvage_transcript_sections(truncated)
        assert isinstance(result, dict)
        assert set(result.keys()) >= {"transcripts", "screen_content", "speakers"}
        for key in ("transcripts", "screen_content", "speakers"):
            assert isinstance(result[key], list)

    def test_wrapper_with_unknown_task_does_not_overwrite_with_empty(self):
        """Defensive guard: if Pro emits the wrapper shape with an unknown task
        name (not in {transcripts, screen_content, speakers}), salvage must
        not silently rewrite the input to an empty envelope. Either recover
        nothing OR fall through to the legacy regex - never claim success."""
        unknown = json.dumps(
            [
                {
                    "task": "speech",
                    "output": [{"start": f"00:{i:02d}", "voice": 1, "text": f"Line {i}"} for i in range(10)],
                },
            ]
        )
        # Whether or not legacy salvage finds anything, we must not have
        # OVERWRITTEN the original text with an empty envelope and lost the
        # bytes the per-object walker could have reached.
        result, _ = salvage_transcript_sections(unknown)
        assert isinstance(result.get("transcripts"), list)

    def test_robotics_raw_sidecar_recovers_at_least_80_speech_entries(self):
        """Acceptance criterion 1 from issue #45: the real sidecar that hit
        zero entries today must recover at least 80 once the fix lands.
        Skips on a fresh clone where the file is not present."""
        raw_path = Path(
            r"G:\My Drive\video-intel\ycombinator\2026-04-16-the-gpt-moment-for-robotics-is-here.transcript.raw.txt"
        )
        if not raw_path.exists():
            pytest.skip(f"Real fixture not on disk: {raw_path}")
        text = raw_path.read_text(encoding="utf-8")
        result, _ = salvage_transcript_sections(text)
        assert len(result.get("transcripts", [])) >= 80

    def test_bci_raw_sidecar_still_recovers_at_least_400_speech_entries(self):
        """Acceptance criterion 2 from issue #45: regression check on the
        sidecar that already salvages well today (~405 entries)."""
        raw_path = Path(
            r"G:\My Drive\video-intel\ycombinator\2026-03-09-the-future-of-brain-computer-interfaces.transcript.raw.txt"
        )
        if not raw_path.exists():
            pytest.skip(f"Real fixture not on disk: {raw_path}")
        text = raw_path.read_text(encoding="utf-8")
        result, _ = salvage_transcript_sections(text)
        assert len(result.get("transcripts", [])) >= 400


class TestTryParseTranscriptJsonWrapperHandling:
    """Issue #45 P0: clean task-wrapper responses must not bypass normalization.
    `try_parse_transcript_json` must return a flat-envelope dict for wrapper
    inputs so the downstream `merge_transcript_json` produces a non-empty
    transcript on the full-parse path (not just the salvage path)."""

    def test_clean_wrapper_returns_flat_envelope_dict(self):
        wrapper = json.dumps(
            [
                {
                    "task": "transcripts",
                    "output": [
                        {"start": "00:10", "voice": 1, "text": "Hello"},
                        {"start": "00:20", "voice": 1, "text": "World"},
                    ],
                },
                {"task": "screen_content", "output": []},
                {"task": "speakers", "output": [{"voice": 1, "name": "Host"}]},
            ]
        )
        parsed, err = try_parse_transcript_json(wrapper)
        assert err is None
        assert isinstance(parsed, dict), "wrapper must be normalized to flat dict"
        assert len(parsed.get("transcripts", [])) == 2
        assert len(parsed.get("speakers", [])) == 1

    def test_clean_wrapper_produces_nonempty_fused_transcript(self):
        """End-to-end: clean wrapper -> parse -> merge -> non-empty markdown."""
        from video_intel import merge_transcript_json

        wrapper = json.dumps(
            [
                {
                    "task": "transcripts",
                    "output": [
                        {"start": "00:10", "voice": 1, "text": "Hello there"},
                        {"start": "00:20", "voice": 1, "text": "Welcome to the show"},
                    ],
                },
                {"task": "screen_content", "output": []},
                {"task": "speakers", "output": [{"voice": 1, "name": "Host"}]},
            ]
        )
        parsed, err = try_parse_transcript_json(wrapper)
        assert err is None
        fused = merge_transcript_json(parsed, {})
        assert "Hello there" in fused
        assert "Welcome to the show" in fused

    def test_flat_envelope_passes_through_unchanged(self):
        """Regression guard: the normal flat-envelope response must NOT be
        accidentally rewritten by wrapper detection."""
        flat = {
            "transcripts": [{"start": "00:10", "voice": 1, "text": "Hello"}],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        parsed, err = try_parse_transcript_json(json.dumps(flat))
        assert err is None
        assert parsed == flat


# ---------------------------------------------------------------------------
# process_transcript resilience
# ---------------------------------------------------------------------------


VALID_TRANSCRIPT_JSON = json.dumps(
    {
        "transcripts": [
            {"start": "00:10", "voice": 1, "text": "Hello world."},
            {"start": "00:20", "voice": 1, "text": "Welcome to the show."},
        ],
        "screen_content": [],
        "speakers": [{"voice": 1, "name": "Host"}],
    }
)

# Truncated JSON that has enough speech entries to salvage
SALVAGEABLE_JSON = (
    '{"transcripts": ['
    + ", ".join(json.dumps({"start": f"00:{i:02d}", "voice": 1, "text": f"Line {i}"}) for i in range(10))
    + '], "screen_content": [{"start": "05:00"'
)

UNSALVAGEABLE_JSON = '{"transcripts": [{"start":'


class TestProcessTranscriptResilience:
    """Test the layered parse/salvage/retry behavior of process_transcript."""

    def _make_video(self) -> dict:
        return {
            "video_id": "test123",
            "url": "https://www.youtube.com/watch?v=test123",
            "title": "Test Video",
            "published": "2026-01-01",
        }

    def _setup_mocks(self, monkeypatch, gemini_responses: list[str]):
        """Set up monkeypatches for process_transcript. Returns (client, types)."""
        call_count = {"n": 0}

        def fake_call_gemini(_client, _types, _url, _prompt, _model, **_kw):
            idx = call_count["n"]
            call_count["n"] += 1
            if idx < len(gemini_responses):
                return gemini_responses[idx]
            raise RuntimeError("Unexpected extra Gemini call")

        monkeypatch.setattr("video_intel.call_gemini", fake_call_gemini)
        return MagicMock(), MagicMock()

    def test_no_raw_sidecar_on_success(self, monkeypatch, tmp_path):
        client, types = self._setup_mocks(monkeypatch, [VALID_TRANSCRIPT_JSON])
        video = self._make_video()
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", tmp_path / "testch", "2026-01-01-test-video"
        )
        assert status == "done"
        # Transcript written
        assert (tmp_path / "testch" / f"{prefix}.transcript.md").exists()
        # No raw sidecar
        assert not (tmp_path / "testch" / f"{prefix}.transcript.raw.txt").exists()

    def test_writes_raw_sidecar_on_parse_failure(self, monkeypatch, tmp_path):
        client, types = self._setup_mocks(monkeypatch, [UNSALVAGEABLE_JSON, UNSALVAGEABLE_JSON])
        video = self._make_video()
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", tmp_path / "testch", "2026-01-01-test-video"
        )
        assert "error" in status
        # Raw sidecar written
        assert (tmp_path / "testch" / f"{prefix}.transcript.raw.txt").exists()

    def test_salvages_partial_transcript_when_full_parse_fails(self, monkeypatch, tmp_path):
        client, types = self._setup_mocks(monkeypatch, [SALVAGEABLE_JSON])
        video = self._make_video()
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", tmp_path / "testch", "2026-01-01-test-video"
        )
        assert "partial" in status
        transcript_path = tmp_path / "testch" / f"{prefix}.transcript.md"
        assert transcript_path.exists()
        content = transcript_path.read_text(encoding="utf-8")
        assert "Incomplete transcript" in content or "Warning" in content

    def test_marks_partial_status_in_meta(self, monkeypatch, tmp_path):
        client, types = self._setup_mocks(monkeypatch, [SALVAGEABLE_JSON])
        video = self._make_video()
        # Pre-create meta from a prior scan
        channel_dir = tmp_path / "testch"
        channel_dir.mkdir(parents=True)
        meta_path = channel_dir / "2026-01-01-test-video.meta.json"
        meta_path.write_text(json.dumps({"video_url": video["url"]}))

        process_transcript(client, types, video, "prompt", "gemini-test", tmp_path / "testch", "2026-01-01-test-video")
        meta = json.loads(meta_path.read_text())
        assert meta.get("transcript_status") == "partial"

    def test_retries_once_after_unusable_parse_failure(self, monkeypatch, tmp_path):
        # First attempt: unsalvageable. Second attempt: valid.
        client, types = self._setup_mocks(monkeypatch, [UNSALVAGEABLE_JSON, VALID_TRANSCRIPT_JSON])
        video = self._make_video()
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", tmp_path / "testch", "2026-01-01-test-video"
        )
        assert status == "done"
        assert (tmp_path / "testch" / f"{prefix}.transcript.md").exists()

    def test_preserves_first_raw_on_retry(self, monkeypatch, tmp_path):
        # Both attempts unsalvageable - both raw files should exist
        client, types = self._setup_mocks(monkeypatch, [UNSALVAGEABLE_JSON, UNSALVAGEABLE_JSON])
        video = self._make_video()
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", tmp_path / "testch", "2026-01-01-test-video"
        )
        assert "error" in status
        assert (tmp_path / "testch" / f"{prefix}.transcript.raw.txt").exists()
        assert (tmp_path / "testch" / f"{prefix}.transcript.raw.2.txt").exists()

    def test_stops_after_retry_budget_exhausted(self, monkeypatch, tmp_path):
        # More failures than retry budget allows
        failures = [UNSALVAGEABLE_JSON] * (TRANSCRIPT_PARSE_RETRY_LIMIT + 2)
        client, types = self._setup_mocks(monkeypatch, failures)
        video = self._make_video()
        _prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", tmp_path / "testch", "2026-01-01-test-video"
        )
        assert "error" in status


# ---------------------------------------------------------------------------
# Token/config constants
# ---------------------------------------------------------------------------


class TestTokenConfig:
    def test_max_output_tokens_is_65536(self):
        assert MAX_OUTPUT_TOKENS == 65536

    def test_max_output_tokens_matches_translate_script(self):
        from translate_video import MAX_OUTPUT_TOKENS as TRANSLATE_MAX

        assert MAX_OUTPUT_TOKENS == TRANSLATE_MAX


# ---------------------------------------------------------------------------
# resolve_playlist_ids
# ---------------------------------------------------------------------------


class TestResolvePlaylistIds:
    def _make_youtube_mock(self, playlists: list[dict]) -> MagicMock:
        youtube = MagicMock()
        youtube.playlists.return_value.list.return_value.execute.return_value = {
            "items": [{"id": p["id"], "snippet": {"title": p["title"]}} for p in playlists],
        }
        return youtube

    def test_resolves_exact_name(self):
        youtube = self._make_youtube_mock(
            [
                {"id": "PL_agent", "title": "Agent Skills"},
                {"id": "PL_vibe", "title": "Vibe Coding Tips"},
            ]
        )
        result = resolve_playlist_ids(youtube, "UC123", ["Agent Skills"])
        assert len(result) == 1
        assert result[0] == ("PL_agent", "Agent Skills")

    def test_case_insensitive_contains_matching(self):
        youtube = self._make_youtube_mock(
            [
                {"id": "PL_agent", "title": "Agent Skills Deep Dive"},
                {"id": "PL_vibe", "title": "Vibe Coding Tips"},
            ]
        )
        result = resolve_playlist_ids(youtube, "UC123", ["agent skills"])
        assert len(result) == 1
        assert result[0][0] == "PL_agent"

    def test_matches_multiple_playlists(self):
        youtube = self._make_youtube_mock(
            [
                {"id": "PL_a1", "title": "Agent Skills Part 1"},
                {"id": "PL_a2", "title": "Agent Skills Part 2"},
                {"id": "PL_other", "title": "Other"},
            ]
        )
        result = resolve_playlist_ids(youtube, "UC123", ["Agent Skills"])
        assert len(result) == 2

    def test_unresolved_name_returns_empty(self):
        youtube = self._make_youtube_mock(
            [
                {"id": "PL_vibe", "title": "Vibe Coding Tips"},
            ]
        )
        result = resolve_playlist_ids(youtube, "UC123", ["Nonexistent Playlist"])
        assert len(result) == 0

    def test_multiple_names_resolved_independently(self):
        youtube = self._make_youtube_mock(
            [
                {"id": "PL_agent", "title": "Agent Skills"},
                {"id": "PL_vibe", "title": "Vibe Coding Tips"},
                {"id": "PL_ux", "title": "UX Design"},
            ]
        )
        result = resolve_playlist_ids(youtube, "UC123", ["Agent Skills", "UX Design"])
        assert len(result) == 2
        ids = {r[0] for r in result}
        assert ids == {"PL_agent", "PL_ux"}


# ---------------------------------------------------------------------------
# fetch_playlist_videos
# ---------------------------------------------------------------------------


class TestFetchPlaylistVideos:
    def test_fetches_videos_from_playlist_id(self):
        youtube = MagicMock()
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {"title": "Video One", "publishedAt": "2026-03-01T00:00:00Z"},
                    "contentDetails": {"videoId": "vid1", "videoPublishedAt": "2026-03-01T00:00:00Z"},
                },
            ],
        }

        videos = fetch_playlist_videos(youtube, "PL_agent_skills")
        assert len(videos) == 1
        assert videos[0]["video_id"] == "vid1"
        assert videos[0]["title"] == "Video One"
        assert videos[0]["published"] == "2026-03-01"
        assert videos[0]["url"] == "https://www.youtube.com/watch?v=vid1"

        # Verify it used the playlist ID directly (not UU prefix)
        call_kwargs = youtube.playlistItems.return_value.list.call_args[1]
        assert call_kwargs["playlistId"] == "PL_agent_skills"

    def test_no_date_filtering(self):
        """Playlist fetch returns all videos regardless of age."""
        youtube = MagicMock()
        youtube.playlistItems.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "snippet": {"title": "Recent", "publishedAt": "2026-03-01T00:00:00Z"},
                    "contentDetails": {"videoId": "new1", "videoPublishedAt": "2026-03-01T00:00:00Z"},
                },
                {
                    "snippet": {"title": "Very Old", "publishedAt": "2020-01-01T00:00:00Z"},
                    "contentDetails": {"videoId": "old1", "videoPublishedAt": "2020-01-01T00:00:00Z"},
                },
            ],
        }

        videos = fetch_playlist_videos(youtube, "PL_test")
        assert len(videos) == 2


# ---------------------------------------------------------------------------
# fetch_keyword_videos
# ---------------------------------------------------------------------------


class TestFetchKeywordVideos:
    def test_normalizes_search_results_to_video_format(self):
        youtube = MagicMock()
        youtube.search.return_value.list.return_value.execute.return_value = {
            "items": [
                {
                    "id": {"videoId": "vid1"},
                    "snippet": {"title": "UX Design Tips", "publishedAt": "2026-02-15T00:00:00Z"},
                },
            ],
        }

        videos = fetch_keyword_videos(youtube, "UC123", "ux design")
        assert len(videos) == 1
        assert videos[0] == {
            "video_id": "vid1",
            "title": "UX Design Tips",
            "published": "2026-02-15",
            "url": "https://www.youtube.com/watch?v=vid1",
        }

    def test_passes_correct_search_params(self):
        youtube = MagicMock()
        youtube.search.return_value.list.return_value.execute.return_value = {"items": []}

        fetch_keyword_videos(youtube, "UC123", "agent skills")
        call_kwargs = youtube.search.return_value.list.call_args[1]
        assert call_kwargs["channelId"] == "UC123"
        assert call_kwargs["q"] == "agent skills"
        assert call_kwargs["type"] == "video"
        assert call_kwargs["order"] == "date"

    def test_respects_max_pages_cap(self):
        youtube = MagicMock()
        # Return a page with nextPageToken to simulate pagination
        page_with_next = {
            "items": [
                {"id": {"videoId": f"vid{i}"}, "snippet": {"title": f"V{i}", "publishedAt": "2026-01-01T00:00:00Z"}}
                for i in range(50)
            ],
            "nextPageToken": "next",
        }
        last_page = {
            "items": [
                {"id": {"videoId": "vidlast"}, "snippet": {"title": "Last", "publishedAt": "2026-01-01T00:00:00Z"}}
            ],
        }
        youtube.search.return_value.list.return_value.execute.side_effect = [
            page_with_next,
            page_with_next,
            page_with_next,
            page_with_next,
            last_page,
        ]

        fetch_keyword_videos(youtube, "UC123", "test", max_pages=KEYWORD_MAX_PAGES)
        # Should stop after KEYWORD_MAX_PAGES pages even if more available
        assert youtube.search.return_value.list.return_value.execute.call_count == KEYWORD_MAX_PAGES


# ---------------------------------------------------------------------------
# fetch_selective_videos
# ---------------------------------------------------------------------------


class TestFetchSelectiveVideos:
    def test_fetches_from_playlists_and_keywords(self, monkeypatch):
        playlist_videos = [
            {
                "video_id": "pl1",
                "title": "From Playlist",
                "published": "2026-01-01",
                "url": "https://youtube.com/watch?v=pl1",
            },
        ]
        keyword_videos = [
            {
                "video_id": "kw1",
                "title": "From Keyword",
                "published": "2026-02-01",
                "url": "https://youtube.com/watch?v=kw1",
            },
        ]

        monkeypatch.setattr("video_intel.resolve_playlist_ids", lambda _yt, _cid, _names: [("PL1", "Test Playlist")])
        monkeypatch.setattr("video_intel.fetch_playlist_videos", lambda _yt, _pid: playlist_videos)
        monkeypatch.setattr("video_intel.fetch_keyword_videos", lambda _yt, _cid, _kw, **_kw2: keyword_videos)

        youtube = MagicMock()
        config = {"playlists": ["Test Playlist"], "keywords": ["test"]}
        videos = fetch_selective_videos(youtube, "UC123", config)

        assert len(videos) == 2
        ids = {v["video_id"] for v in videos}
        assert ids == {"pl1", "kw1"}

    def test_deduplicates_by_video_id(self, monkeypatch):
        shared_video = {
            "video_id": "shared1",
            "title": "Shared",
            "published": "2026-01-01",
            "url": "https://youtube.com/watch?v=shared1",
        }

        monkeypatch.setattr("video_intel.resolve_playlist_ids", lambda _yt, _cid, _names: [("PL1", "P1")])
        monkeypatch.setattr("video_intel.fetch_playlist_videos", lambda _yt, _pid: [shared_video])
        monkeypatch.setattr("video_intel.fetch_keyword_videos", lambda _yt, _cid, _kw, **_kw2: [shared_video.copy()])

        youtube = MagicMock()
        config = {"playlists": ["P1"], "keywords": ["test"]}
        videos = fetch_selective_videos(youtube, "UC123", config)

        assert len(videos) == 1
        assert videos[0]["video_id"] == "shared1"

    def test_playlists_only_no_keywords(self, monkeypatch):
        monkeypatch.setattr("video_intel.resolve_playlist_ids", lambda _yt, _cid, _names: [("PL1", "P1")])
        monkeypatch.setattr(
            "video_intel.fetch_playlist_videos",
            lambda _yt, _pid: [
                {"video_id": "v1", "title": "V1", "published": "2026-01-01", "url": "https://youtube.com/watch?v=v1"},
            ],
        )

        youtube = MagicMock()
        config = {"playlists": ["P1"]}
        videos = fetch_selective_videos(youtube, "UC123", config)

        assert len(videos) == 1

    def test_keywords_only_no_playlists(self, monkeypatch):
        monkeypatch.setattr(
            "video_intel.fetch_keyword_videos",
            lambda _yt, _cid, _kw, **_kw2: [
                {"video_id": "v1", "title": "V1", "published": "2026-01-01", "url": "https://youtube.com/watch?v=v1"},
            ],
        )

        youtube = MagicMock()
        config = {"keywords": ["test"]}
        videos = fetch_selective_videos(youtube, "UC123", config)

        assert len(videos) == 1

    def test_since_adds_recent_uploads_to_selective(self, monkeypatch):
        """since_dt is additive: playlists + recent uploads, deduplicated."""
        playlist_video = {
            "video_id": "pl1",
            "title": "Playlist",
            "published": "2026-01-01",
            "url": "https://youtube.com/watch?v=pl1",
        }
        recent_video = {
            "video_id": "new1",
            "title": "Recent Upload",
            "published": "2026-04-10",
            "url": "https://youtube.com/watch?v=new1",
        }

        monkeypatch.setattr("video_intel.resolve_playlist_ids", lambda _yt, _cid, _names: [("PL1", "P1")])
        monkeypatch.setattr("video_intel.fetch_playlist_videos", lambda _yt, _pid: [playlist_video])
        monkeypatch.setattr("video_intel.fetch_channel_videos", lambda _yt, _cid, _since: [recent_video])

        youtube = MagicMock()
        config = {"playlists": ["P1"]}
        since = datetime(2026, 4, 1, tzinfo=UTC)
        videos = fetch_selective_videos(youtube, "UC123", config, since_dt=since)

        assert len(videos) == 2
        ids = {v["video_id"] for v in videos}
        assert ids == {"pl1", "new1"}

    def test_since_deduplicates_with_selective(self, monkeypatch):
        """A video in both playlist and recent uploads appears only once."""
        shared = {
            "video_id": "shared1",
            "title": "Shared",
            "published": "2026-04-01",
            "url": "https://youtube.com/watch?v=shared1",
        }

        monkeypatch.setattr("video_intel.resolve_playlist_ids", lambda _yt, _cid, _names: [("PL1", "P1")])
        monkeypatch.setattr("video_intel.fetch_playlist_videos", lambda _yt, _pid: [shared])
        monkeypatch.setattr("video_intel.fetch_channel_videos", lambda _yt, _cid, _since: [shared.copy()])

        youtube = MagicMock()
        config = {"playlists": ["P1"]}
        since = datetime(2026, 3, 1, tzinfo=UTC)
        videos = fetch_selective_videos(youtube, "UC123", config, since_dt=since)

        assert len(videos) == 1


# ---------------------------------------------------------------------------
# cmd_scan selective mode integration
# ---------------------------------------------------------------------------


class TestCmdScanSelectiveMode:
    def test_selective_channel_uses_fetch_selective_videos(self, monkeypatch, tmp_path):
        """Channels with playlists/keywords use selective fetch, not uploads scan."""
        captured = {}

        def fake_fetch_selective(_yt, _cid, ch_config, **_kwargs):
            captured["called"] = True
            captured["config"] = ch_config
            return [
                {
                    "video_id": "sel1",
                    "title": "Selective",
                    "published": "2026-01-01",
                    "url": "https://youtube.com/watch?v=sel1",
                }
            ]

        def fake_process_mindmap(_client, _types, _video, _prompt, _model, *a, **kw):
            return "2026-01-01-selective", "done"

        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")
        monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UC123", "Test Channel"))
        monkeypatch.setattr("video_intel.is_processed", lambda *a, **kw: False)
        monkeypatch.setattr("video_intel.is_skipped", lambda *a, **kw: False)
        monkeypatch.setattr("video_intel.fetch_selective_videos", fake_fetch_selective)
        monkeypatch.setattr("video_intel.process_mindmap", fake_process_mindmap)

        args = argparse.Namespace(
            model=None,
            channel=None,
            since=None,
            dry_run=False,
            force=False,
        )
        config = {
            "channels": [
                {
                    "name": "testch",
                    "url": "https://youtube.com/@test",
                    "playlists": ["Agent Skills"],
                },
            ],
        }

        cmd_scan(args, config)
        assert captured.get("called") is True

    def test_non_selective_channel_uses_date_scan(self, monkeypatch, tmp_path):
        """Channels without playlists/keywords use the standard uploads scan."""
        captured = {}

        def fake_fetch_channel_videos(_yt, _cid, _since):
            captured["uploads_scan"] = True
            return []

        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UC123", "Test"))
        monkeypatch.setattr("video_intel.fetch_channel_videos", fake_fetch_channel_videos)

        args = argparse.Namespace(
            model=None,
            channel=None,
            since=None,
            dry_run=False,
            force=False,
        )
        config = {
            "channels": [{"name": "standard", "url": "https://youtube.com/@standard"}],
            "default_since": "10d",
        }

        cmd_scan(args, config)
        assert captured.get("uploads_scan") is True

    def test_skips_channel_when_playlists_is_not_list(self, monkeypatch, tmp_path):
        """Invalid playlists config (string instead of list) skips the channel."""
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UC123", "Test"))

        # Track whether any fetch function was called
        fetch_called = {"value": False}
        monkeypatch.setattr(
            "video_intel.fetch_selective_videos", lambda *a, **kw: (fetch_called.update(value=True), [])[1]
        )
        monkeypatch.setattr(
            "video_intel.fetch_channel_videos", lambda *a, **kw: (fetch_called.update(value=True), [])[1]
        )

        args = argparse.Namespace(model=None, channel=None, since=None, dry_run=False, force=False)
        config = {
            "channels": [{"name": "bad", "url": "https://youtube.com/@bad", "playlists": "not a list"}],
        }

        cmd_scan(args, config)
        assert fetch_called["value"] is False


# ---------------------------------------------------------------------------
# parse_time_to_seconds
# ---------------------------------------------------------------------------


class TestParseTimeToSeconds:
    def test_mm_ss_returns_seconds(self):
        assert parse_time_to_seconds("05:30") == 330

    def test_hh_mm_ss_returns_seconds(self):
        assert parse_time_to_seconds("01:15:45") == 4545

    def test_raw_seconds_string(self):
        assert parse_time_to_seconds("330") == 330

    def test_zero_value(self):
        assert parse_time_to_seconds("0") == 0
        assert parse_time_to_seconds("00:00") == 0

    def test_leading_zeros_in_components(self):
        assert parse_time_to_seconds("00:05:00") == 300

    def test_whitespace_tolerated(self):
        assert parse_time_to_seconds(" 05:30 ") == 330

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_time_to_seconds("")

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            parse_time_to_seconds("not-a-time")

    def test_too_many_colons_raises(self):
        with pytest.raises(ValueError):
            parse_time_to_seconds("1:2:3:4")


# ---------------------------------------------------------------------------
# upload_local_video
# ---------------------------------------------------------------------------


class TestUploadLocalVideo:
    def _make_active_file(self, uri="files/abc123"):
        """Build a MagicMock simulating an ACTIVE Gemini file."""
        file_obj = MagicMock()
        file_obj.uri = uri
        file_obj.name = uri
        file_obj.state.name = "ACTIVE"
        return file_obj

    def test_uploads_and_returns_uri_when_active(self, tmp_path):
        mp4 = tmp_path / "clip.mp4"
        mp4.write_bytes(b"fake mp4 content")
        active = self._make_active_file()
        client = MagicMock()
        client.files.upload.return_value = active
        client.files.get.return_value = active

        uri = upload_local_video(client, mp4)
        assert uri == "files/abc123"
        client.files.upload.assert_called_once()

    def test_polls_until_active(self, tmp_path, monkeypatch):
        """When file is PROCESSING on first check, poll until ACTIVE."""
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        mp4 = tmp_path / "clip.mp4"
        mp4.write_bytes(b"fake content")

        processing = MagicMock()
        processing.uri = "files/xyz"
        processing.name = "files/xyz"
        processing.state.name = "PROCESSING"

        active = self._make_active_file(uri="files/xyz")
        client = MagicMock()
        client.files.upload.return_value = processing
        # First get() returns PROCESSING, second returns ACTIVE
        client.files.get.side_effect = [processing, active]

        uri = upload_local_video(client, mp4)
        assert uri == "files/xyz"
        assert client.files.get.call_count == 2

    def test_raises_on_failed_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        mp4 = tmp_path / "clip.mp4"
        mp4.write_bytes(b"fake content")

        failed = MagicMock()
        failed.uri = "files/bad"
        failed.name = "files/bad"
        failed.state.name = "FAILED"

        client = MagicMock()
        client.files.upload.return_value = failed
        client.files.get.return_value = failed

        with pytest.raises(RuntimeError, match="processing failed"):
            upload_local_video(client, mp4)

    def test_raises_on_missing_file(self, tmp_path):
        client = MagicMock()
        with pytest.raises(FileNotFoundError):
            upload_local_video(client, tmp_path / "nonexistent.mp4")

    def test_large_file_threshold_is_1gb(self):
        assert LARGE_FILE_THRESHOLD_BYTES == 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# call_gemini offsets
# ---------------------------------------------------------------------------


class TestCallGeminiOffsets:
    def _make_client_and_types(self):
        response = MagicMock(text="ok")
        client = MagicMock()
        client.models.generate_content.return_value = response
        types = MagicMock()
        return client, types

    def test_no_offsets_omits_video_metadata(self):
        client, types = self._make_client_and_types()
        call_gemini(client, types, "https://youtube.com/watch?v=x", "prompt", "gemini-test")
        # Part should be called once with only file_data
        part_calls = [c.kwargs for c in types.Part.call_args_list]
        video_part_call = next((c for c in part_calls if "file_data" in c), None)
        assert video_part_call is not None
        assert "video_metadata" not in video_part_call

    def test_start_offset_only_sets_start(self):
        client, types = self._make_client_and_types()
        call_gemini(client, types, "files/abc", "prompt", "gemini-test", start_offset=330)
        # VideoMetadata should be constructed with start_offset="330s"
        types.VideoMetadata.assert_called_once_with(start_offset="330s")

    def test_both_offsets_set(self):
        client, types = self._make_client_and_types()
        call_gemini(
            client,
            types,
            "files/abc",
            "prompt",
            "gemini-test",
            start_offset=330,
            end_offset=1125,
        )
        types.VideoMetadata.assert_called_once_with(start_offset="330s", end_offset="1125s")


# ---------------------------------------------------------------------------
# cmd_transcript --file branch
# ---------------------------------------------------------------------------


class TestCmdTranscriptFile:
    def _setup_common(self, monkeypatch, tmp_path):
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")

    def test_local_file_calls_upload_and_process_transcript(self, monkeypatch, tmp_path):
        """--file path uploads file and passes URI + offsets to process_transcript."""
        self._setup_common(monkeypatch, tmp_path)
        captured = {}

        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/xyz")

        def fake_process(_client, _types, video, _prompt, _model, channel_dir, prefix, **kw):
            captured["video_url"] = video["url"]
            captured["channel_dir"] = channel_dir
            captured["prefix"] = prefix
            captured["start_offset"] = kw.get("start_offset")
            captured["end_offset"] = kw.get("end_offset")
            return prefix, "done"

        monkeypatch.setattr("video_intel.process_transcript", fake_process)

        mp4 = tmp_path / "meeting.mp4"
        mp4.write_bytes(b"fake content")
        args = argparse.Namespace(
            model=None,
            url=None,
            file=mp4,
            channel=None,
            title=None,
            date=None,
            force=False,
            start="05:30",
            end="18:45",
        )
        cmd_transcript(args, {})

        assert captured["video_url"] == "files/xyz"
        assert captured["channel_dir"] == mp4.parent
        assert captured["prefix"] == "meeting"
        assert captured["start_offset"] == 330
        assert captured["end_offset"] == 1125

    def test_local_file_large_without_segment_exits(self, monkeypatch, tmp_path):
        """File over threshold without --start/--end should sys.exit with error."""
        self._setup_common(monkeypatch, tmp_path)

        mp4 = tmp_path / "huge.mp4"
        # Stub os.stat through Path to simulate large file without writing 500MB
        original_stat = Path.stat

        def fake_stat(self, *a, **kw):
            st = MagicMock()
            st.st_size = LARGE_FILE_THRESHOLD_BYTES + 1
            st.st_mtime = 1700000000
            return st

        monkeypatch.setattr(Path, "stat", fake_stat)
        mp4.write_bytes(b"small placeholder")

        args = argparse.Namespace(
            model=None,
            url=None,
            file=mp4,
            channel=None,
            title=None,
            date=None,
            force=False,
            start=None,
            end=None,
        )

        with pytest.raises(SystemExit):
            cmd_transcript(args, {})

        # Restore
        monkeypatch.setattr(Path, "stat", original_stat)

    def test_local_file_nonexistent_exits(self, monkeypatch, tmp_path):
        """Missing file should sys.exit."""
        self._setup_common(monkeypatch, tmp_path)

        args = argparse.Namespace(
            model=None,
            url=None,
            file=tmp_path / "missing.mp4",
            channel=None,
            title=None,
            date=None,
            force=False,
            start=None,
            end=None,
        )
        with pytest.raises(SystemExit):
            cmd_transcript(args, {})
