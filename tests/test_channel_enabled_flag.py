"""Tests for the per-channel `enabled` flag in cmd_scan.

Failure mode the flag prevents: a creator the user wants to keep in config
purely for one-off mindmap/transcript --url --channel routing (so concepts
extraction works) gets pulled into every regular `scan` run, costing Gemini
quota and YouTube API quota for content the user does not want batch-processed.

Semantics:
  - `enabled: true` (default when key absent): scan as before.
  - `enabled: false`: cmd_scan skips the channel entirely, including when
    the user passes `--channel <name>` explicitly. The flag's purpose is
    manual one-offs via mindmap/transcript --url, not bulk scan even on
    demand. Removing the flag is the way to scan such a channel.

Other commands (concepts, dedupe, mindmap/transcript --url --channel) keep
working — the flag is scoped to scan only.
"""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock

import pytest

import video_intel as vi


def _stub_scan_environment(monkeypatch, tmp_path):
    """Stub out external deps so cmd_scan reaches the channel-iteration loop.

    Centralized here so each test focuses on the enabled-flag behavior, not
    on plumbing.
    """
    monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
    monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)


def test_scan_skips_channel_when_enabled_false(monkeypatch, tmp_path, caplog):
    """A channel with `enabled: false` must not reach the YouTube API.

    The flag's whole point is to keep the creator in config (so explicit
    --channel routing and concepts extraction work) without paying a quota
    cost on every regular scan.
    """
    _stub_scan_environment(monkeypatch, tmp_path)

    def _fail_get_channel_id(*_args, **_kwargs):
        pytest.fail("get_channel_id called for an enabled:false channel; cmd_scan should skip earlier")

    monkeypatch.setattr("video_intel.get_channel_id", _fail_get_channel_id)

    config = {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "ondemand_creator", "url": "https://youtube.com/@ondemand", "enabled": False},
        ],
    }
    args = argparse.Namespace(channel=None, since=None, dry_run=True, force=False, model=None)

    with caplog.at_level("INFO", logger="video_intel"):
        vi.cmd_scan(args, config)

    assert "ondemand_creator" in caplog.text
    assert "enabled" in caplog.text.lower() or "disabled" in caplog.text.lower()


def test_scan_runs_channel_when_enabled_missing_defaults_true(monkeypatch, tmp_path):
    """Channels without an `enabled` key keep the pre-flag behavior.

    The flag is opt-in. Existing config entries (which never had the key)
    must continue to scan exactly as before, otherwise this is a breaking
    change rather than a feature.
    """
    _stub_scan_environment(monkeypatch, tmp_path)

    calls: list[tuple] = []

    def _record_get_channel_id(*args, **_kwargs):
        calls.append(args)
        return None, None

    monkeypatch.setattr("video_intel.get_channel_id", _record_get_channel_id)

    config = {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "regular_creator", "url": "https://youtube.com/@regular"},
        ],
    }
    args = argparse.Namespace(channel=None, since=None, dry_run=True, force=False, model=None)

    vi.cmd_scan(args, config)

    assert len(calls) == 1, "cmd_scan should reach get_channel_id for a default-enabled channel"


def test_scan_skips_disabled_channel_targeted_explicitly(monkeypatch, tmp_path, caplog):
    """`scan --channel X` against an enabled:false channel still skips.

    The flag means "manual one-offs only via mindmap/transcript --url --channel".
    A user who really wants to bulk-scan such a creator should remove the
    flag rather than override it on the command line. Otherwise the flag
    becomes advisory and loses its protective value.
    """
    _stub_scan_environment(monkeypatch, tmp_path)

    def _fail_get_channel_id(*_args, **_kwargs):
        pytest.fail("get_channel_id called for an enabled:false channel even with explicit --channel")

    monkeypatch.setattr("video_intel.get_channel_id", _fail_get_channel_id)

    config = {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "ondemand_creator", "url": "https://youtube.com/@ondemand", "enabled": False},
            {"name": "regular_creator", "url": "https://youtube.com/@regular"},
        ],
    }
    args = argparse.Namespace(channel="ondemand_creator", since=None, dry_run=True, force=False, model=None)

    with caplog.at_level("INFO", logger="video_intel"):
        vi.cmd_scan(args, config)

    assert "ondemand_creator" in caplog.text
