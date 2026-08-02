"""Tests for per-mode skip controls and the long-video transcript guard.

Covers all four units of
docs/plans/2026-04-26-001-feat-skip-modes-and-long-video-guard-plan.md.

  Unit 1: is_skipped() / is_skipped_meta() helpers (mode-aware, backward-compat)
  Unit 2: long-video transcript guard inside cmd_scan
  Unit 3: mark-skip CLI subcommand
  Unit 4: per-mode skip threaded through scan loops + auto-concepts loop

Single test file mirroring the test_skip_shorts.py precedent.
"""

import json
from argparse import Namespace
from pathlib import Path

import pytest

import video_intel as vi

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset module-level caches between tests (mirrors test_skip_shorts pattern)."""
    vi._is_youtube_short_url.cache_clear()
    vi._invalidate_video_id_cache()
    yield
    vi._is_youtube_short_url.cache_clear()
    vi._invalidate_video_id_cache()


def _write_meta(channel_dir: Path, prefix: str, data: dict) -> Path:
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = channel_dir / f"{prefix}.meta.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit 1: is_skipped_meta() helper
# ---------------------------------------------------------------------------


class TestIsSkippedMeta:
    def test_returns_false_when_meta_has_no_skip_keys(self):
        assert vi.is_skipped_meta({}, mode="transcript") is False
        assert vi.is_skipped_meta({"video_id": "abc"}, mode="transcript") is False

    def test_legacy_skip_true_returns_true_for_any_mode(self):
        meta = {"skip": True}
        assert vi.is_skipped_meta(meta, mode="mindmap") is True
        assert vi.is_skipped_meta(meta, mode="transcript") is True
        assert vi.is_skipped_meta(meta, mode="concepts") is True

    def test_legacy_skip_true_returns_true_with_no_mode(self):
        assert vi.is_skipped_meta({"skip": True}) is True

    def test_skip_modes_array_returns_true_for_listed_mode(self):
        meta = {"skip_modes": ["transcript"]}
        assert vi.is_skipped_meta(meta, mode="transcript") is True

    def test_skip_modes_array_returns_false_for_unlisted_mode(self):
        meta = {"skip_modes": ["transcript"]}
        assert vi.is_skipped_meta(meta, mode="mindmap") is False
        assert vi.is_skipped_meta(meta, mode="concepts") is False

    def test_skip_modes_takes_precedence_over_legacy_boolean(self):
        """If skip_modes is present, it wins. Legacy skip is ignored even if True."""
        meta = {"skip": True, "skip_modes": ["transcript"]}
        assert vi.is_skipped_meta(meta, mode="mindmap") is False
        assert vi.is_skipped_meta(meta, mode="transcript") is True

    def test_no_mode_arg_returns_true_when_skip_modes_nonempty(self):
        assert vi.is_skipped_meta({"skip_modes": ["transcript"]}) is True

    def test_no_mode_arg_returns_false_when_skip_modes_empty(self):
        assert vi.is_skipped_meta({"skip_modes": []}) is False

    def test_skip_false_explicit_returns_false(self):
        assert vi.is_skipped_meta({"skip": False}, mode="transcript") is False


class TestIsSkippedDiskWrapper:
    """Disk-backed is_skipped() reads meta.json then delegates to is_skipped_meta."""

    def test_returns_false_when_meta_missing(self, tmp_path):
        video = {"video_id": "vid1", "title": "t", "published": "2026-04-15"}
        assert vi.is_skipped(tmp_path, "ch", video) is False
        assert vi.is_skipped(tmp_path, "ch", video, mode="transcript") is False

    def test_legacy_skip_true_blocks_all_modes(self, tmp_path):
        video = {"video_id": "vid1", "title": "Test Video", "published": "2026-04-15"}
        prefix = vi.video_file_prefix(video)
        _write_meta(tmp_path / "ch", prefix, {"skip": True})
        assert vi.is_skipped(tmp_path, "ch", video, mode="mindmap") is True
        assert vi.is_skipped(tmp_path, "ch", video, mode="transcript") is True
        assert vi.is_skipped(tmp_path, "ch", video, mode="concepts") is True

    def test_skip_modes_blocks_only_listed_mode(self, tmp_path):
        video = {"video_id": "vid1", "title": "Test Video", "published": "2026-04-15"}
        prefix = vi.video_file_prefix(video)
        _write_meta(tmp_path / "ch", prefix, {"skip_modes": ["transcript"]})
        assert vi.is_skipped(tmp_path, "ch", video, mode="mindmap") is False
        assert vi.is_skipped(tmp_path, "ch", video, mode="transcript") is True
        assert vi.is_skipped(tmp_path, "ch", video, mode="concepts") is False

    def test_no_mode_arg_preserves_pre_issue42_callsite_semantics(self, tmp_path):
        """Existing callers without mode= keep working: any skip => True."""
        video = {"video_id": "vid1", "title": "Test", "published": "2026-04-15"}
        prefix = vi.video_file_prefix(video)
        _write_meta(tmp_path / "ch", prefix, {"skip": True})
        assert vi.is_skipped(tmp_path, "ch", video) is True


# ---------------------------------------------------------------------------
# Unit 2: long-video transcript guard in cmd_scan
# ---------------------------------------------------------------------------


def _scan_args(**overrides):
    base = {
        "dry_run": False,
        "channel": None,
        "force": False,
        "since": None,
        "model": None,
    }
    base.update(overrides)
    return Namespace(**base)


def _scan_setup_with_transcript(monkeypatch, videos, durations=None):
    """cmd_scan mocking with auto_transcript wired in (records both loops).

    Returns a dict with `mindmaps` (list of videos that hit process_mindmap)
    and `transcripts` (list of videos that hit process_transcript).
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
    monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos))

    durations = durations or {}

    def fake_enrich(_yt, video_ids):
        return dict.fromkeys(video_ids) | {k: v for k, v in durations.items() if k in video_ids}

    monkeypatch.setattr(vi, "enrich_with_durations", fake_enrich)
    # Issue #70: stub the new pre-flight metadata call (keep all videos).
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

    captured = {"mindmaps": [], "transcripts": []}

    def fake_process_mindmap(*args, **kwargs):
        video = args[2] if len(args) > 2 else kwargs.get("video")
        captured["mindmaps"].append(video)
        return (video.get("video_id", "prefix"), "done")

    def fake_process_transcript(*args, **kwargs):
        video = args[2] if len(args) > 2 else kwargs.get("video")
        captured["transcripts"].append(video)
        return (video.get("video_id", "prefix"), "done")

    monkeypatch.setattr(vi, "process_mindmap", fake_process_mindmap)
    monkeypatch.setattr(vi, "process_transcript", fake_process_transcript)
    return captured


class TestLongVideoTranscriptGuard:
    def test_video_over_threshold_dropped_from_transcript_only(self, tmp_path, monkeypatch, caplog):
        """A 95-minute video must skip transcript but still get a mindmap."""
        videos = [
            {
                "video_id": "long95",
                "title": "95-minute talk",
                "published": "2026-04-15",
                "url": "https://www.youtube.com/watch?v=long95",
            },
        ]
        durations = {"long95": "PT2H15M"}  # 8100 seconds, > 7200 default
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations=durations)

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"},
            ],
        }
        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(), config)

        mm_ids = [v["video_id"] for v in captured["mindmaps"]]
        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "long95" in mm_ids, "Mindmap loop must still process the long video"
        assert "long95" not in tx_ids, "Transcript loop must drop the long video"

    def test_video_under_threshold_kept_for_transcript(self, tmp_path, monkeypatch):
        videos = [
            {
                "video_id": "short70",
                "title": "70-minute talk",
                "published": "2026-04-15",
                "url": "https://www.youtube.com/watch?v=short70",
            },
        ]
        durations = {"short70": "PT1H10M"}  # 4200 sec, under 7200 default
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations=durations)

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"},
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "short70" in tx_ids, "Under-threshold video must reach the transcript loop"

    def test_warning_message_includes_url_and_recipe(self, tmp_path, monkeypatch, caplog):
        videos = [
            {
                "video_id": "long95",
                "title": 'Build Your First "SaaS" App',
                "published": "2026-04-15",
                "url": "https://www.youtube.com/watch?v=long95",
            },
        ]
        durations = {"long95": "PT2H24M42S"}  # 8682 sec
        _scan_setup_with_transcript(monkeypatch, videos, durations=durations)

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "seankochel", "url": "https://example.com/sk", "auto_transcript": "all"},
            ],
        }
        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(), config)

        warnings = "\n".join(r.message for r in caplog.records if r.levelname == "WARNING")
        assert "Skipping transcript" in warnings
        assert "seankochel" in warnings
        assert "2h24m42s" in warnings, "Duration must be formatted h/m/s"
        assert "transcript --url" in warnings, "Recovery recipe must be present"
        assert "https://www.youtube.com/watch?v=long95" in warnings
        assert "--start 0" in warnings
        assert "--end 7200" in warnings
        # Reviewer P2: warn that the recipe captures only the first segment.
        assert "first" in warnings and "later segments" in warnings, (
            "Warning must tell the user that --end clips the rest of the video"
        )

    def test_default_threshold_7200_seconds(self):
        assert vi.TRANSCRIPT_MAX_DURATION_DEFAULT == 7200

    def test_custom_threshold_in_config_respected(self, tmp_path, monkeypatch):
        """Setting transcript_max_duration_seconds=3600 lowers the cutoff to 60 min."""
        videos = [
            {
                "video_id": "v75",
                "title": "75 minutes",
                "published": "2026-04-15",
                "url": "https://www.youtube.com/watch?v=v75",
            },
        ]
        durations = {"v75": "PT1H15M"}  # 4500 sec, < 7200 default but > 3600 override
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations=durations)

        config = {
            "output_dir": str(tmp_path),
            "transcript_max_duration_seconds": 3600,
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"},
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "v75" not in tx_ids, "75-min video must be dropped under 60-min threshold"

    def test_per_channel_threshold_override_wins_over_top_level(self, tmp_path, monkeypatch):
        """Per-channel transcript_max_duration_seconds must override the
        top-level value, matching the precedence pattern used by every other
        knob in this codebase (skip_shorts, since, prompt, etc)."""
        videos = [
            {
                "video_id": "lex2h30m",
                "title": "Lex 2h30m episode",
                "published": "2026-04-15",
                "url": "https://www.youtube.com/watch?v=lex2h30m",
            },
        ]
        durations = {"lex2h30m": "PT2H30M"}  # 9000 sec
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations=durations)

        # Top-level says 7200 (would skip), but channel override says 14400 (4h, keeps it)
        config = {
            "output_dir": str(tmp_path),
            "transcript_max_duration_seconds": 7200,
            "channels": [
                {
                    "name": "lex",
                    "url": "https://example.com/lex",
                    "auto_transcript": "all",
                    "transcript_max_duration_seconds": 14400,  # channel-specific 4h override
                },
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "lex2h30m" in tx_ids, "Channel override 14400 must keep 2h30m video"

    def test_per_channel_threshold_can_be_lower_than_top_level(self, tmp_path, monkeypatch):
        """Symmetric: a tighter per-channel cap drops videos that the top-level
        would have kept."""
        videos = [
            {
                "video_id": "v90m",
                "title": "90-min talk",
                "published": "2026-04-15",
                "url": "https://www.youtube.com/watch?v=v90m",
            },
        ]
        durations = {"v90m": "PT1H30M"}  # 5400 sec
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations=durations)

        config = {
            "output_dir": str(tmp_path),
            "transcript_max_duration_seconds": 7200,  # default would keep 90m
            "channels": [
                {
                    "name": "shortform",
                    "url": "https://example.com/shortform",
                    "auto_transcript": "all",
                    "transcript_max_duration_seconds": 3600,  # 60-min cap drops 90m
                },
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "v90m" not in tx_ids

    def test_video_with_unparseable_duration_kept_fail_safe(self, tmp_path, monkeypatch):
        """If duration is None, do NOT silently drop. Better to attempt and fail visibly."""
        videos = [
            {
                "video_id": "vNone",
                "title": "Mystery duration",
                "published": "2026-04-15",
                "url": "https://www.youtube.com/watch?v=vNone",
            },
        ]
        # No duration in fake_enrich response => parsed seconds is None
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"},
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "vNone" in tx_ids, "Unparseable duration must NOT cause a silent drop"


class TestFmtHms:
    def test_formats_hours_minutes_seconds(self):
        assert vi._fmt_hms(8682) == "2h24m42s"

    def test_formats_minutes_only(self):
        assert vi._fmt_hms(720) == "12m"

    def test_formats_seconds_only(self):
        assert vi._fmt_hms(45) == "45s"

    def test_formats_one_hour(self):
        assert vi._fmt_hms(3600) == "1h"

    def test_formats_one_hour_one_minute(self):
        assert vi._fmt_hms(3660) == "1h1m"

    def test_formats_one_hour_one_second(self):
        assert vi._fmt_hms(3601) == "1h0m1s"

    def test_formats_zero(self):
        assert vi._fmt_hms(0) == "0s"


# ---------------------------------------------------------------------------
# Unit 4: per-mode skip threaded through scan loops and auto-concepts
# ---------------------------------------------------------------------------


class TestPerModeSkipCmdScan:
    def test_skip_modes_transcript_only_runs_mindmap_skips_transcript(self, tmp_path, monkeypatch):
        """A meta with skip_modes=['transcript'] but no existing mindmap must
        still hit process_mindmap, but never hit process_transcript."""
        video = {
            "video_id": "vSkipTx",
            "title": "Skip transcript only",
            "published": "2026-04-15",
            "url": "https://www.youtube.com/watch?v=vSkipTx",
        }
        prefix = vi.video_file_prefix(video)
        # Pre-existing meta with per-mode skip, but no mindmap on disk yet
        _write_meta(tmp_path / "ch", prefix, {"video_id": "vSkipTx", "skip_modes": ["transcript"]})

        videos = [video]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"vSkipTx": "PT30M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"},
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        mm_ids = [v["video_id"] for v in captured["mindmaps"]]
        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "vSkipTx" in mm_ids, "Per-mode skip='transcript' must NOT block mindmap"
        assert "vSkipTx" not in tx_ids, "Per-mode skip='transcript' must block transcript"

    def test_legacy_skip_true_blocks_all_loops(self, tmp_path, monkeypatch):
        video = {
            "video_id": "vSkipAll",
            "title": "Legacy skip",
            "published": "2026-04-15",
            "url": "https://www.youtube.com/watch?v=vSkipAll",
        }
        prefix = vi.video_file_prefix(video)
        _write_meta(tmp_path / "ch", prefix, {"video_id": "vSkipAll", "skip": True})

        videos = [video]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"vSkipAll": "PT30M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"},
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        mm_ids = [v["video_id"] for v in captured["mindmaps"]]
        tx_ids = [v["video_id"] for v in captured["transcripts"]]
        assert "vSkipAll" not in mm_ids
        assert "vSkipAll" not in tx_ids


class TestPerModeSkipAutoConcepts:
    """The auto-concepts loop reads meta.json directly. Verify it honors per-mode skip."""

    def test_concepts_runs_when_only_transcript_skipped(self, tmp_path, monkeypatch):
        """Mindmap exists on disk, skip_modes=['transcript']: concepts must still extract."""
        ch_dir = tmp_path / "ch"
        ch_dir.mkdir()
        prefix = "2026-04-15-test-video"
        _write_meta(
            ch_dir,
            prefix,
            {
                "video_id": "vConcepts",
                "video_url": "https://www.youtube.com/watch?v=vConcepts",
                "title": "Test Video",
                "published": "2026-04-15",
                "skip_modes": ["transcript"],
                "modes_completed": ["scan"],
            },
        )
        _touch(ch_dir / f"{prefix}.mindmap.md", "# Fake mindmap content")

        # Mock everything cmd_scan needs except process_concepts (we want it called)
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: [])
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

        captured = {"concepts": []}

        def fake_process_concepts(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            captured["concepts"].append(video)
            return ("prefix", "done")

        monkeypatch.setattr(vi, "process_concepts", fake_process_concepts)

        config = {
            "output_dir": str(tmp_path),
            "auto_concepts": True,
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        vi.cmd_scan(_scan_args(), config)

        ids = [v.get("video_id") for v in captured["concepts"]]
        assert "vConcepts" in ids, "Concepts loop must process video with skip_modes=['transcript']"

    def test_concepts_skipped_when_concepts_in_skip_modes(self, tmp_path, monkeypatch):
        ch_dir = tmp_path / "ch"
        ch_dir.mkdir()
        prefix = "2026-04-15-test-video"
        _write_meta(
            ch_dir,
            prefix,
            {
                "video_id": "vConcepts",
                "video_url": "https://www.youtube.com/watch?v=vConcepts",
                "title": "Test Video",
                "published": "2026-04-15",
                "skip_modes": ["concepts"],
                "modes_completed": ["scan"],
            },
        )
        _touch(ch_dir / f"{prefix}.mindmap.md", "# Fake mindmap")

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: [])
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

        captured = {"concepts": []}

        def fake_process_concepts(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            captured["concepts"].append(video)
            return ("prefix", "done")

        monkeypatch.setattr(vi, "process_concepts", fake_process_concepts)

        config = {
            "output_dir": str(tmp_path),
            "auto_concepts": True,
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        vi.cmd_scan(_scan_args(), config)

        assert captured["concepts"] == [], "skip_modes=['concepts'] must block concepts loop"


# ---------------------------------------------------------------------------
# Unit 3: mark-skip CLI subcommand
# ---------------------------------------------------------------------------


def _mark_skip_args(**overrides):
    base = {
        "url": None,
        "mode": None,
        "reason": None,
    }
    base.update(overrides)
    return Namespace(**base)


class TestMarkSkipCli:
    def test_writes_skip_modes_array_to_meta(self, tmp_path):
        ch_dir = tmp_path / "ch"
        ch_dir.mkdir()
        prefix = "2026-04-15-real-talk"
        meta_path = _write_meta(
            ch_dir,
            prefix,
            {
                "video_id": "v1234567890",
                "title": "Real Talk",
                "video_url": "https://www.youtube.com/watch?v=v1234567890",
                "published": "2026-04-15",
            },
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        args = _mark_skip_args(url="https://www.youtube.com/watch?v=v1234567890", mode=["transcript"])
        vi.cmd_mark_skip(args, config)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["skip_modes"] == ["transcript"]

    def test_appends_to_existing_skip_modes_without_duplicates(self, tmp_path):
        ch_dir = tmp_path / "ch"
        ch_dir.mkdir()
        prefix = "2026-04-15-talk"
        meta_path = _write_meta(
            ch_dir,
            prefix,
            {
                "video_id": "v1234567890",
                "video_url": "https://www.youtube.com/watch?v=v1234567890",
                "title": "Talk",
                "published": "2026-04-15",
                "skip_modes": ["transcript"],
            },
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        # Add transcript again (should not duplicate) plus concepts (new)
        args = _mark_skip_args(
            url="https://www.youtube.com/watch?v=v1234567890",
            mode=["transcript", "concepts"],
        )
        vi.cmd_mark_skip(args, config)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert sorted(meta["skip_modes"]) == ["concepts", "transcript"]

    def test_records_reason_field_when_provided(self, tmp_path):
        ch_dir = tmp_path / "ch"
        ch_dir.mkdir()
        prefix = "2026-04-15-talk"
        meta_path = _write_meta(
            ch_dir,
            prefix,
            {
                "video_id": "v1234567890",
                "video_url": "https://www.youtube.com/watch?v=v1234567890",
                "title": "Talk",
                "published": "2026-04-15",
            },
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        args = _mark_skip_args(
            url="https://www.youtube.com/watch?v=v1234567890",
            mode=["transcript"],
            reason="OOM truncation on 2h24m video",
        )
        vi.cmd_mark_skip(args, config)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("skip_reason") == "OOM truncation on 2h24m video"

    def test_handles_missing_meta_with_helpful_error(self, tmp_path, caplog):
        """11-char id is well-formed but no meta.json exists on disk for it."""
        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        args = _mark_skip_args(
            url="https://www.youtube.com/watch?v=ZZZ1234567X",
            mode=["transcript"],
        )
        with caplog.at_level("ERROR"), pytest.raises(SystemExit):
            vi.cmd_mark_skip(args, config)

        errors = "\n".join(r.message for r in caplog.records if r.levelname == "ERROR")
        # Either the parse error or the "not found" branch produces an actionable error.
        assert "ZZZ1234567X" in errors or "not found" in errors.lower() or "no meta.json" in errors.lower()

    def test_rejects_garbage_url_with_helpful_error(self, tmp_path, caplog):
        """URL without an extractable 11-char video id fails at parse stage."""
        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        args = _mark_skip_args(url="not-a-url", mode=["transcript"])
        with caplog.at_level("ERROR"), pytest.raises(SystemExit):
            vi.cmd_mark_skip(args, config)
        errors = "\n".join(r.message for r in caplog.records if r.levelname == "ERROR")
        assert "video ID" in errors or "video id" in errors.lower()

    def test_argparse_rejects_unknown_mode(self):
        """The CLI's argparse subparser must reject --mode bogus via choices=."""
        import argparse

        parser = argparse.ArgumentParser()
        # Replicate the same configuration the real subparser uses for --mode.
        parser.add_argument(
            "--mode",
            action="append",
            choices=("mindmap", "transcript", "concepts"),
            required=True,
        )
        with pytest.raises(SystemExit):
            parser.parse_args(["--mode", "transcript", "--mode", "bogus"])


# ---------------------------------------------------------------------------
# Reviewer P1 follow-up: cmd_process must honor skip_modes even when the
# corresponding artifact does NOT yet exist on disk. Without these tests, the
# upstream needs_* gate looked sufficient but process_mindmap / process_transcript
# would fall through to a Gemini call when path.exists() was False.
# ---------------------------------------------------------------------------


def _make_process_args(file, **overrides):
    base = {
        "file": str(file),
        "channel": None,
        "video_id": None,
        "title": None,
        "date": None,
        "start": None,
        "end": None,
        "force": False,
        "model": None,
        "prompt": None,
    }
    base.update(overrides)
    return Namespace(**base)


def _prep_process_env(monkeypatch, tmp_path, channel="everyinc"):
    """Mirror the stub_env fixture from test_cmd_process.py."""
    from unittest.mock import MagicMock

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr("video_intel.create_client", lambda _k: MagicMock())
    monkeypatch.setattr("video_intel.resolve_model", lambda *_: "stub-model")
    monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
    monkeypatch.setattr("video_intel.load_prompt", lambda _n: f"prompt-{_n}")
    monkeypatch.setattr("video_intel.load_taxonomy", lambda _d: {"concepts": {}})

    output_dir = tmp_path / "video-intel"
    channel_dir = output_dir / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    return output_dir, channel_dir


class TestCmdProcessPerModeSkip:
    def _wire_pipeline_recorders(self, monkeypatch, channel_dir):
        upload_calls: list = []
        mindmap_calls: list = []
        transcript_calls: list = []
        concepts_calls: list = []

        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, p: upload_calls.append(p) or "files/uploaded",
        )

        def fake_mindmap(*args, **kwargs):
            mindmap_calls.append(kwargs)
            prefix = kwargs.get("prefix") or "video"
            (channel_dir / f"{prefix}.mindmap.md").write_text("# mm", encoding="utf-8")
            return prefix, "done"

        def fake_transcript(*args, **kwargs):
            transcript_calls.append(kwargs)
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
            return prefix, "done"

        def fake_concepts(*args, **kwargs):
            concepts_calls.append(kwargs)
            return kwargs.get("prefix") or "video", "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)
        monkeypatch.setattr("video_intel.process_transcript", fake_transcript)
        monkeypatch.setattr("video_intel.process_concepts", fake_concepts)
        return upload_calls, mindmap_calls, transcript_calls, concepts_calls

    def test_skip_modes_transcript_blocks_process_transcript_call(self, monkeypatch, tmp_path):
        """skip_modes=['transcript'] with NO transcript.md on disk must NOT call
        process_transcript, even though path.exists()==False would normally fall
        through. The upstream needs_transcript gate stops the upload but is not
        sufficient on its own."""
        _output_dir, channel_dir = _prep_process_env(monkeypatch, tmp_path)
        mp4 = channel_dir / "test.mp4"
        mp4.write_bytes(b"fake mp4")
        # Pre-existing meta with skip_modes=['transcript'] but no transcript artifact.
        meta_path = channel_dir / "test.meta.json"
        meta_path.write_text(
            json.dumps({"video_id": "test", "skip_modes": ["transcript"]}),
            encoding="utf-8",
        )

        uploads, mm_calls, tx_calls, _ = self._wire_pipeline_recorders(monkeypatch, channel_dir)

        from video_intel import cmd_process

        config = {"channels": [{"name": "everyinc", "url": "https://youtube.com/@x"}]}
        cmd_process(_make_process_args(mp4, channel="everyinc"), config)

        assert len(mm_calls) == 1, "Mindmap must still run when only transcript is skipped"
        assert len(tx_calls) == 0, "process_transcript MUST NOT be called when skip_modes=['transcript']"
        assert len(uploads) == 1, "Upload happens once for the mindmap step"

    def test_skip_modes_mindmap_blocks_process_mindmap_call(self, monkeypatch, tmp_path):
        """Symmetric: skip_modes=['mindmap'] with no mindmap.md must not call
        process_mindmap. Transcript is still allowed to run."""
        _output_dir, channel_dir = _prep_process_env(monkeypatch, tmp_path)
        mp4 = channel_dir / "test.mp4"
        mp4.write_bytes(b"fake mp4")
        meta_path = channel_dir / "test.meta.json"
        meta_path.write_text(
            json.dumps({"video_id": "test", "skip_modes": ["mindmap"]}),
            encoding="utf-8",
        )

        _, mm_calls, tx_calls, _ = self._wire_pipeline_recorders(monkeypatch, channel_dir)

        from video_intel import cmd_process

        config = {"channels": [{"name": "everyinc", "url": "https://youtube.com/@x"}]}
        cmd_process(_make_process_args(mp4, channel="everyinc"), config)

        assert len(mm_calls) == 0, "process_mindmap MUST NOT be called when skip_modes=['mindmap']"
        assert len(tx_calls) == 1, "Transcript step must still run"

    def test_legacy_skip_true_still_hard_exits_cmd_process(self, monkeypatch, tmp_path):
        """Backward compat: legacy `skip: true` (no skip_modes) keeps the old
        whole-video hard-exit semantics that pre-issue-42 callers depend on."""
        _output_dir, channel_dir = _prep_process_env(monkeypatch, tmp_path)
        mp4 = channel_dir / "test.mp4"
        mp4.write_bytes(b"fake mp4")
        meta_path = channel_dir / "test.meta.json"
        meta_path.write_text(json.dumps({"video_id": "test", "skip": True}), encoding="utf-8")

        uploads, mm_calls, tx_calls, _ = self._wire_pipeline_recorders(monkeypatch, channel_dir)

        from video_intel import cmd_process

        config = {"channels": [{"name": "everyinc", "url": "https://youtube.com/@x"}]}
        cmd_process(_make_process_args(mp4, channel="everyinc"), config)

        assert len(uploads) == 0
        assert len(mm_calls) == 0
        assert len(tx_calls) == 0


# ---------------------------------------------------------------------------
# Issue #42 follow-up: declarative skip_video_ids blocklist in config.yaml.
# Verified pre-fetch filter so listed IDs never trigger any side effect.
# ---------------------------------------------------------------------------


class TestSkipVideoIdsConfig:
    def test_listed_ids_filtered_before_processing(self, tmp_path, monkeypatch):
        videos = [
            {"video_id": "skipMe1234", "title": "long workshop", "published": "2026-04-15"},
            {"video_id": "keepMe5678", "title": "regular talk", "published": "2026-04-15"},
        ]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"keepMe5678": "PT10M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "ch",
                    "url": "https://example.com/ch",
                    "skip_video_ids": ["skipMe1234"],
                },
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        mm_ids = [v["video_id"] for v in captured["mindmaps"]]
        assert "skipMe1234" not in mm_ids, "Blocklisted ID must not reach process_mindmap"
        assert "keepMe5678" in mm_ids, "Non-blocklisted ID must still process"

    def test_empty_blocklist_is_no_op(self, tmp_path, monkeypatch):
        videos = [
            {"video_id": "v1", "title": "t1", "published": "2026-04-15"},
            {"video_id": "v2", "title": "t2", "published": "2026-04-15"},
        ]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"v1": "PT10M", "v2": "PT10M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch", "skip_video_ids": []}],
        }
        vi.cmd_scan(_scan_args(), config)

        mm_ids = [v["video_id"] for v in captured["mindmaps"]]
        assert sorted(mm_ids) == ["v1", "v2"]

    def test_missing_key_is_no_op(self, tmp_path, monkeypatch):
        """Channels without skip_video_ids set must keep current behavior."""
        videos = [{"video_id": "v1", "title": "t1", "published": "2026-04-15"}]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"v1": "PT10M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        vi.cmd_scan(_scan_args(), config)

        assert [v["video_id"] for v in captured["mindmaps"]] == ["v1"]

    def test_blocklist_log_message_includes_count_and_per_video_lines(self, tmp_path, monkeypatch, caplog):
        videos = [
            {"video_id": "skipA12345X", "title": "Workshop A", "published": "2026-04-15"},
            {"video_id": "skipB67890Y", "title": "Workshop B", "published": "2026-04-15"},
            {"video_id": "keep1234567", "title": "Regular talk", "published": "2026-04-15"},
        ]
        _scan_setup_with_transcript(monkeypatch, videos, durations={"keep1234567": "PT10M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "ch",
                    "url": "https://example.com/ch",
                    "skip_video_ids": ["skipA12345X", "skipB67890Y"],
                },
            ],
        }
        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(), config)

        info_lines = "\n".join(r.message for r in caplog.records if r.levelname == "INFO")
        # Per-video log lines so silent-typo failures are visible.
        assert 'skip_video_ids: skipA12345X "Workshop A"' in info_lines
        assert 'skip_video_ids: skipB67890Y "Workshop B"' in info_lines
        assert "Filtered 2 video(s) per skip_video_ids" in info_lines

    def test_blocklist_warns_when_listed_id_does_not_match_any_fetched_video(self, tmp_path, monkeypatch, caplog):
        """User's typo / stale-config detection: if an ID is listed in
        skip_video_ids but never appears in fetched results, emit a WARNING
        so the user notices the entry is doing nothing."""
        videos = [{"video_id": "real1234567", "title": "Real video", "published": "2026-04-15"}]
        _scan_setup_with_transcript(monkeypatch, videos, durations={"real1234567": "PT10M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "ch",
                    "url": "https://example.com/ch",
                    "skip_video_ids": ["staleZZZ123"],  # not in fetched results
                },
            ],
        }
        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(), config)

        warnings_text = "\n".join(r.message for r in caplog.records if r.levelname == "WARNING")
        assert "staleZZZ123" in warnings_text
        assert "NOT in fetched videos" in warnings_text or "deleted" in warnings_text

    def test_listed_id_filter_pre_enrich_for_lex_use_case(self, tmp_path, monkeypatch):
        """Same as test_blocklist_skips_enrich_for_listed_ids but with the actual
        Lex use case shape: long-form podcast channel, one episode blocklisted,
        rest pass through normally."""
        videos = [
            {"video_id": "lexEp001", "title": "Lex Ep 1 (3h)", "published": "2026-01-15"},
            {"video_id": "skipMe1234", "title": "Lex one we hate", "published": "2026-02-01"},
        ]
        captured = _scan_setup_with_transcript(
            monkeypatch, videos, durations={"lexEp001": "PT3H", "skipMe1234": "PT3H"}
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "skip_video_ids": ["skipMe1234"]},
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        mm_ids = [v["video_id"] for v in captured["mindmaps"]]
        assert "lexEp001" in mm_ids
        assert "skipMe1234" not in mm_ids

    def test_blocklist_skips_enrich_for_listed_ids(self, tmp_path, monkeypatch):
        """The filter runs BEFORE enrich_with_durations, so listed IDs never reach
        the YouTube duration API call. Verifies cost-saving intent."""
        videos = [
            {"video_id": "skipMe1234", "title": "long workshop", "published": "2026-04-15"},
            {"video_id": "keepMe5678", "title": "regular talk", "published": "2026-04-15"},
        ]

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos))
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

        enrich_calls: list = []

        def recording_enrich(_yt, video_ids):
            enrich_calls.append(list(video_ids))
            return dict.fromkeys(video_ids, "PT10M")

        monkeypatch.setattr(vi, "enrich_with_durations", recording_enrich)
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: ("p", "done"))

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "ch", "url": "https://example.com/ch", "skip_video_ids": ["skipMe1234"]},
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        assert enrich_calls, "enrich_with_durations should have been called"
        flattened = [vid for batch in enrich_calls for vid in batch]
        assert "skipMe1234" not in flattened, "Blocklisted ID must NOT reach enrich (cost-saving invariant)"
        assert "keepMe5678" in flattened


# ---------------------------------------------------------------------------
# Phase 2A: auto_mindmap=none for notify-only channels (Lex Fridman use case).
# ---------------------------------------------------------------------------


class TestAutoMindmapNone:
    def test_auto_mindmap_none_skips_mindmap_loop(self, tmp_path, monkeypatch, caplog):
        videos = [
            {"video_id": "lex001", "title": "Lex Episode 1", "published": "2026-04-15"},
            {"video_id": "lex002", "title": "Lex Episode 2", "published": "2026-04-16"},
        ]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"lex001": "PT3H", "lex002": "PT3H"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "lex",
                    "url": "https://example.com/lex",
                    "auto_mindmap": "none",
                    "auto_transcript": "none",
                },
            ],
        }
        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(), config)

        assert captured["mindmaps"] == [], "Mindmap loop must NOT fire when auto_mindmap=none"
        assert captured["transcripts"] == [], "Transcript loop respects its own none flag"
        info_lines = "\n".join(r.message for r in caplog.records if r.levelname == "INFO")
        assert "auto_mindmap=none" in info_lines
        assert "Lex Episode 1" in info_lines
        assert "Lex Episode 2" in info_lines

    def test_auto_mindmap_default_all_keeps_existing_behavior(self, tmp_path, monkeypatch):
        videos = [{"video_id": "v1", "title": "t1", "published": "2026-04-15"}]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"v1": "PT10M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        vi.cmd_scan(_scan_args(), config)

        # was_livestream is stamped on every kept video by the issue #120
        # pre-flight pass; False here means "regular upload, routing unchanged".
        assert captured["mindmaps"] == [
            {
                "video_id": "v1",
                "title": "t1",
                "published": "2026-04-15",
                "duration_iso": "PT10M",
                "was_livestream": False,
            }
        ]


# ---------------------------------------------------------------------------
# Phase 2B: per-channel min_duration_seconds filter (Lex's "shorts < 30 min").
# ---------------------------------------------------------------------------


class TestMinDurationSeconds:
    def test_drops_videos_under_threshold(self, tmp_path, monkeypatch, caplog):
        videos = [
            {"video_id": "vshort1", "title": "Khabib clip", "published": "2026-04-15"},
            {"video_id": "vlong1", "title": "Full episode", "published": "2026-04-15"},
        ]
        captured = _scan_setup_with_transcript(
            monkeypatch, videos, durations={"vshort1": "PT22M9S", "vlong1": "PT2H30M"}
        )

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "lex",
                    "url": "https://example.com/lex",
                    "min_duration_seconds": 1800,  # 30 min
                },
            ],
        }
        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(), config)

        mm_ids = [v["video_id"] for v in captured["mindmaps"]]
        assert "vshort1" not in mm_ids, "22-min video must be dropped under 30-min threshold"
        assert "vlong1" in mm_ids, "Long episode must pass through"
        info_lines = "\n".join(r.message for r in caplog.records if r.levelname == "INFO")
        assert "min_duration_seconds: dropped vshort1" in info_lines
        assert "Filtered 1 video(s) under 30m" in info_lines

    def test_unparseable_duration_kept_fail_safe(self, tmp_path, monkeypatch):
        """Unparseable duration must NOT cause a silent drop. Mirrors the
        invariant for transcript_max_duration_seconds."""
        videos = [{"video_id": "vNone", "title": "Mystery", "published": "2026-04-15"}]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={})

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "lex",
                    "url": "https://example.com/lex",
                    "min_duration_seconds": 1800,
                },
            ],
        }
        vi.cmd_scan(_scan_args(), config)

        assert "vNone" in [v["video_id"] for v in captured["mindmaps"]]

    def test_missing_key_is_no_op(self, tmp_path, monkeypatch):
        videos = [{"video_id": "vshort", "title": "30s talk", "published": "2026-04-15"}]
        captured = _scan_setup_with_transcript(monkeypatch, videos, durations={"vshort": "PT5M"})

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch"}],
        }
        vi.cmd_scan(_scan_args(), config)

        assert "vshort" in [v["video_id"] for v in captured["mindmaps"]]
