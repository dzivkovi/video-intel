"""Issue #293: a selective channel can opt out of the recent-uploads window.

A channel with `playlists:` or `keywords:` used to ALWAYS fetch recent uploads
on top of its playlist/keyword results whenever a top-level `default_since` was
set, because the window was chosen with an `or` chain. `since: none` (or the
YAML boolean `since: false`) on a selective channel now turns that window off;
CLI `--since` still wins over it. On a NON-selective channel the opt-out would
fetch nothing, so the channel is rejected (unless `--since` was passed) and the
`scan --dry-run` preflight reports the same thing first.

The caller-level classes drive the real `cmd_scan` with only the YouTube client
functions and the Gemini-backed writers stubbed [core: real-caller]. The
recent-uploads recorder captures EVERY argument of each `fetch_channel_videos`
call, so a test cannot pass by the stub agreeing with its own assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import yaml

import video_intel as vi
from video_intel import (
    KNOB_CONSEQUENCE_SKIPS_CHANNEL,
    channel_is_selective,
    resolve_selective_since,
    selective_since_misuse,
    selective_since_opted_out,
    validate_channel_knobs,
)

PLAYLIST_VIDEO = {
    "video_id": "vid001",
    "title": "Playlist video",
    "published": "2026-01-01",
    "url": "https://www.youtube.com/watch?v=vid001",
}
RECENT_VIDEO = {
    "video_id": "vid002",
    "title": "Recent upload",
    "published": "2026-01-02",
    "url": "https://www.youtube.com/watch?v=vid002",
}
HEALTHY_VIDEO = {
    "video_id": "vid003",
    "title": "Healthy channel video",
    "published": "2026-01-03",
    "url": "https://www.youtube.com/watch?v=vid003",
}


def _assert_window_is_days_ago(since_dt: datetime, days: int) -> None:
    expected = datetime.now(UTC) - timedelta(days=days)
    assert abs((since_dt - expected).total_seconds()) < 60, f"expected ~{days}d ago, got {since_dt}"


# ---------------------------------------------------------------------------
# Resolver: the precedence table [core: one-definition]
# ---------------------------------------------------------------------------


class TestResolveSelectiveSincePrecedence:
    def test_resolve_selective_since_cli_since_beats_the_channel_opt_out(self):
        result = resolve_selective_since({"since": "none"}, {"default_since": "10d"}, "30d")

        _assert_window_is_days_ago(result, 30)

    def test_resolve_selective_since_string_none_returns_no_window(self):
        assert resolve_selective_since({"since": "none"}, {"default_since": "10d"}, None) is None

    def test_resolve_selective_since_boolean_false_returns_no_window(self):
        assert resolve_selective_since({"since": False}, {"default_since": "10d"}, None) is None

    @pytest.mark.parametrize("raw", ["None", "NONE", "  none ", "None "])
    def test_resolve_selective_since_opt_out_ignores_case_and_whitespace(self, raw):
        assert resolve_selective_since({"since": raw}, {"default_since": "10d"}, None) is None

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", "  false ", "False "])
    def test_resolve_selective_since_quoted_false_string_is_an_opt_out(self, raw):
        """The docs advertise `false`; a quoted "false" must not reach parse_since."""
        assert selective_since_opted_out({"since": raw}) is True
        assert resolve_selective_since({"since": raw}, {"default_since": "10d"}, None) is None

    @pytest.mark.parametrize("raw", ["no", "off", "No", "OFF"])
    def test_resolve_selective_since_no_and_off_strings_keep_todays_behavior(self, raw):
        """Only `none`/`false` strings opt out; other strings still reach
        parse_since, which exits on an unparseable value exactly as before."""
        assert selective_since_opted_out({"since": raw}) is False
        with pytest.raises(SystemExit):
            resolve_selective_since({"since": raw}, {"default_since": "10d"}, None)

    def test_resolve_selective_since_integer_zero_falls_through_to_default_since(self):
        assert selective_since_opted_out({"since": 0}) is False
        _assert_window_is_days_ago(resolve_selective_since({"since": 0}, {"default_since": "10d"}, None), 10)

    def test_resolve_selective_since_channel_value_beats_default_since(self):
        result = resolve_selective_since({"since": "5d"}, {"default_since": "10d"}, None)

        _assert_window_is_days_ago(result, 5)

    @pytest.mark.parametrize("channel", [{}, {"since": None}, {"since": ""}])
    def test_resolve_selective_since_absent_null_or_empty_falls_through_to_default_since(self, channel):
        result = resolve_selective_since(channel, {"default_since": "10d"}, None)

        _assert_window_is_days_ago(result, 10)

    def test_resolve_selective_since_nothing_configured_returns_no_window(self):
        assert resolve_selective_since({}, {}, None) is None

    def test_resolve_selective_since_boolean_true_is_not_an_opt_out_and_keeps_todays_crash(self):
        """`since: true` is given no new meaning: today it reaches parse_since,
        whose regex raises TypeError on a non-string. Pinned, not fixed."""
        with pytest.raises(TypeError):
            resolve_selective_since({"since": True}, {"default_since": "10d"}, None)


class TestSelectiveSinceMisusePredicate:
    def test_misuse_non_selective_channel_with_opt_out_is_misuse(self):
        assert selective_since_misuse({"since": "none"}, None) is True

    def test_misuse_non_selective_channel_with_false_is_misuse(self):
        assert selective_since_misuse({"since": False}, None) is True

    def test_misuse_selective_channel_with_opt_out_is_not_misuse(self):
        assert selective_since_misuse({"since": "none", "playlists": ["p"]}, None) is False
        assert selective_since_misuse({"since": "none", "keywords": ["k"]}, None) is False

    def test_misuse_cli_since_lifts_the_rejection(self):
        assert selective_since_misuse({"since": "none"}, "30d") is False

    def test_misuse_non_selective_channel_without_opt_out_is_not_misuse(self):
        assert selective_since_misuse({"since": "10d"}, None) is False
        assert selective_since_misuse({}, None) is False

    @pytest.mark.parametrize(
        "empty_lists",
        [{"playlists": []}, {"keywords": []}, {"playlists": [], "keywords": []}],
        ids=["playlists", "keywords", "both"],
    )
    def test_misuse_empty_playlists_or_keywords_are_non_selective(self, empty_lists):
        assert channel_is_selective(empty_lists) is False
        assert selective_since_misuse({"since": "none", **empty_lists}, None) is True

    def test_misuse_quoted_false_string_on_non_selective_channel_is_misuse(self):
        assert selective_since_misuse({"since": "false"}, None) is True


# ---------------------------------------------------------------------------
# Preflight: validate_channel_knobs reports the misuse, never a false alarm
# ---------------------------------------------------------------------------


class TestValidateChannelKnobsSinceOptOut:
    def test_validate_non_selective_opt_out_reports_skips_channel(self):
        problems = validate_channel_knobs({"since": "none"}, {})

        assert [p[0] for p in problems] == ["since"]
        assert problems[0][2] == KNOB_CONSEQUENCE_SKIPS_CHANNEL

    def test_validate_selective_opt_out_reports_nothing(self):
        assert validate_channel_knobs({"since": "none", "playlists": ["p"]}, {}) == []

    def test_validate_non_selective_without_opt_out_reports_nothing(self):
        assert validate_channel_knobs({"since": "10d"}, {"default_since": "10d"}) == []

    def test_validate_non_selective_opt_out_under_cli_since_reports_nothing(self):
        assert validate_channel_knobs({"since": "none"}, {}, cli_since="30d") == []

    @pytest.mark.parametrize(
        "empty_lists",
        [{"playlists": []}, {"keywords": []}, {"playlists": [], "keywords": []}],
        ids=["playlists", "keywords", "both"],
    )
    def test_validate_opt_out_with_empty_lists_reports_skips_channel(self, empty_lists):
        problems = validate_channel_knobs({"since": "none", **empty_lists}, {})

        assert [(p[0], p[2]) for p in problems] == [("since", KNOB_CONSEQUENCE_SKIPS_CHANNEL)]


# ---------------------------------------------------------------------------
# Caller level: the real cmd_scan
# ---------------------------------------------------------------------------


def _scan_args(**overrides):
    base = {"dry_run": False, "channel": None, "force": False, "since": None, "model": None}
    base.update(overrides)
    return SimpleNamespace(**base)


class _ScanHarness:
    """Real cmd_scan; YouTube client functions and Gemini writers stubbed."""

    def __init__(self, monkeypatch, tmp_path):
        self.tmp_path = tmp_path
        self.recent_calls: list[tuple] = []
        self.playlist_calls: list[tuple] = []
        self.channel_id_calls: list[str] = []
        self.mindmaps_seen: list[str] = []

        recent_by_channel = {
            "https://example.com/sel": [RECENT_VIDEO],
            "https://example.com/plain": [RECENT_VIDEO],
            "https://example.com/healthy": [HEALTHY_VIDEO],
        }

        def fake_get_channel_id(_yt, url):
            self.channel_id_calls.append(url)
            return (url, url)

        def fake_fetch_channel_videos(*args, **kwargs):
            self.recent_calls.append((args, kwargs))
            channel_id = args[1] if len(args) > 1 else kwargs.get("channel_id")
            return [dict(v) for v in recent_by_channel.get(channel_id, [])]

        def fake_resolve_playlist_ids(_yt, channel_id, names):
            return [(f"PL-{channel_id}", name) for name in names]

        def fake_fetch_playlist_videos(*args, **kwargs):
            self.playlist_calls.append((args, kwargs))
            return [dict(PLAYLIST_VIDEO)]

        def fake_process_mindmap(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            self.mindmaps_seen.append(video["video_id"])
            return (video["video_id"], "done")

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "rebuild_free_derived_layers", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "render_headline_digest", lambda *a, **kw: [])
        monkeypatch.setattr(vi, "get_channel_id", fake_get_channel_id)
        monkeypatch.setattr(vi, "fetch_channel_videos", fake_fetch_channel_videos)
        monkeypatch.setattr(vi, "resolve_playlist_ids", fake_resolve_playlist_ids)
        monkeypatch.setattr(vi, "fetch_playlist_videos", fake_fetch_playlist_videos)
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)
        monkeypatch.setattr(vi, "process_mindmap", fake_process_mindmap)
        monkeypatch.setattr(vi, "process_transcript", lambda *a, **kw: pytest.fail("no transcript expected"))

    def config(self, *channels, default_since="10d"):
        cfg = {"output_dir": str(self.tmp_path), "channels": list(channels)}
        if default_since is not None:
            cfg["default_since"] = default_since
        return cfg

    def recent_calls_for(self, channel_id: str) -> list[tuple]:
        return [c for c in self.recent_calls if channel_id in c[0] or channel_id in c[1].values()]


def _selective(**extra):
    return {"name": "sel", "url": "https://example.com/sel", "playlists": ["Some Playlist"], **extra}


class TestCmdScanSelectiveOptOut:
    @pytest.mark.parametrize("opt_out", ["none", False, "None "])
    def test_scan_selective_opt_out_never_fetches_recent_uploads(self, monkeypatch, tmp_path, opt_out):
        harness = _ScanHarness(monkeypatch, tmp_path)

        vi.cmd_scan(_scan_args(), harness.config(_selective(since=opt_out)))

        assert harness.recent_calls == [], f"recent-uploads fetch must not run: {harness.recent_calls}"
        assert len(harness.playlist_calls) == 1, "the playlist fetch still runs"
        assert harness.mindmaps_seen == ["vid001"], "only the playlist's video is scanned"

    def test_scan_selective_without_since_key_still_fetches_with_default_window(self, monkeypatch, tmp_path):
        harness = _ScanHarness(monkeypatch, tmp_path)

        vi.cmd_scan(_scan_args(), harness.config(_selective()))

        assert len(harness.recent_calls) == 1
        args, kwargs = harness.recent_calls[0]
        assert args[1] == "https://example.com/sel"
        _assert_window_is_days_ago(args[2], 10)
        assert kwargs == {}
        assert sorted(harness.mindmaps_seen) == ["vid001", "vid002"]

    def test_scan_selective_opt_out_with_cli_since_fetches_with_cli_window(self, monkeypatch, tmp_path):
        harness = _ScanHarness(monkeypatch, tmp_path)

        vi.cmd_scan(_scan_args(since="30d"), harness.config(_selective(since="none")))

        assert len(harness.recent_calls) == 1
        args, _kwargs = harness.recent_calls[0]
        _assert_window_is_days_ago(args[2], 30)
        assert sorted(harness.mindmaps_seen) == ["vid001", "vid002"]

    def test_scan_dry_run_selective_opt_out_previews_only_the_playlist_set(self, monkeypatch, tmp_path, caplog):
        harness = _ScanHarness(monkeypatch, tmp_path)

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True), harness.config(_selective(since="none")))

        assert harness.recent_calls == []
        preview = [r.message for r in caplog.records if r.message.startswith("    2026-")]
        assert preview == ["    2026-01-01 - Playlist video"], preview
        assert harness.mindmaps_seen == [], "a dry run never reaches a writer"


class TestCmdScanNonSelectiveOptOutIsRejected:
    def _plain(self, **extra):
        return {"name": "plain", "url": "https://example.com/plain", **extra}

    def _healthy(self):
        return {"name": "healthy", "url": "https://example.com/healthy"}

    @pytest.mark.parametrize(
        "opt_out",
        [
            {"since": "none"},
            {"since": "false"},
            # Delta (a): YAML `false` (and its YAML 1.1 spellings) used to fall
            # through to default_since and scan; parsed here the way config.yaml is.
            yaml.safe_load("since: false"),
            yaml.safe_load("since: no"),
            yaml.safe_load("since: off"),
            # Empty lists do not make a channel selective.
            {"since": "none", "playlists": [], "keywords": []},
        ],
        ids=["none", "quoted-false", "yaml-false", "yaml-no", "yaml-off", "empty-lists"],
    )
    def test_scan_non_selective_opt_out_skips_channel_and_reports_failure(self, monkeypatch, tmp_path, caplog, opt_out):
        harness = _ScanHarness(monkeypatch, tmp_path)

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(), harness.config(self._plain(**opt_out), self._healthy()))

        # No fetch for the rejected channel after the rejection point.
        assert "https://example.com/plain" not in harness.channel_id_calls
        assert harness.recent_calls_for("https://example.com/plain") == []
        # The later healthy channel is still scanned.
        assert harness.mindmaps_seen == ["vid003"]
        # The rejection names the channel and says it is skipped.
        runtime_errors = [
            r.message
            for r in caplog.records
            if r.levelname == "ERROR" and "[plain]" in r.message and "skipping entire channel" in r.message
        ]
        assert len(runtime_errors) == 1, runtime_errors
        assert "playlists:" in runtime_errors[0] and "keywords:" in runtime_errors[0]
        # It lands in the end-of-scan failed summary.
        messages = [r.message for r in caplog.records]
        summary_start = next(i for i, m in enumerate(messages) if "FAILED ---" in m)
        assert "--- 1 FAILED ---" in messages[summary_start]
        assert any("[plain] plain: error:" in m for m in messages[summary_start + 1 :])

    def test_scan_non_selective_opt_out_with_cli_since_scans_normally(self, monkeypatch, tmp_path, caplog):
        harness = _ScanHarness(monkeypatch, tmp_path)

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(since="30d"), harness.config(self._plain(since="none")))

        calls = harness.recent_calls_for("https://example.com/plain")
        assert len(calls) == 1
        _assert_window_is_days_ago(calls[0][0][2], 30)
        assert harness.mindmaps_seen == ["vid002"]
        assert not [r for r in caplog.records if "FAILED ---" in r.message]

    def test_scan_dry_run_reports_misuse_with_skips_channel_consequence(self, monkeypatch, tmp_path, caplog):
        harness = _ScanHarness(monkeypatch, tmp_path)

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True), harness.config(self._plain(since="none"), self._healthy()))

        preflight = [
            r.message
            for r in caplog.records
            if r.levelname == "ERROR"
            and "[plain] invalid since" in r.message
            and r.message.endswith(KNOB_CONSEQUENCE_SKIPS_CHANNEL)
        ]
        assert len(preflight) == 1, preflight
        runtime = [
            r.message for r in caplog.records if r.levelname == "ERROR" and "skipping entire channel" in r.message
        ]
        assert len(runtime) == 1, runtime
        assert "https://example.com/plain" not in harness.channel_id_calls
        assert any("Healthy channel video" in r.message for r in caplog.records)
        # A preview never ends in a FAILED block whose retry/skip advice cannot apply.
        assert not [r for r in caplog.records if "FAILED ---" in r.message]
        assert caplog.records[-1].message == "Done."

    @pytest.mark.parametrize(
        ("channel", "cli_since"),
        [
            (_selective(since="none"), None),
            ({"name": "plain", "url": "https://example.com/plain", "since": "10d"}, None),
            ({"name": "plain", "url": "https://example.com/plain", "since": "none"}, "30d"),
        ],
        ids=["selective-opt-out", "non-selective-no-opt-out", "non-selective-opt-out-cli-since"],
    )
    def test_scan_dry_run_no_false_alarm(self, monkeypatch, tmp_path, caplog, channel, cli_since):
        harness = _ScanHarness(monkeypatch, tmp_path)

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True, since=cli_since), harness.config(dict(channel)))

        assert not [r for r in caplog.records if "since" in r.message and r.levelname in ("ERROR", "WARNING")]
        assert channel["url"] in harness.channel_id_calls
