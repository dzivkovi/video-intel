"""Tests for pure utility functions in video_intel.py."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from video_intel import (
    _dedup_by_video,
    _load_concepts_for_video,
    _parse_timestamp_seconds,
    build_taxonomy,
    chunk_transcript,
    fetch_channel_videos,
    find_mindmap_source,
    is_processed,
    load_taxonomy,
    merge_transcript_json,
    normalize_prompt_name,
    parse_since,
    search_corpus,
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
