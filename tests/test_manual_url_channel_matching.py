"""A url-less configured channel must never break a manual `--url` run (#205).

Reported live: `transcript --url <a video from an unconfigured channel>` died
with `KeyError: 'url'`. The matcher walked every configured channel reading
`ch["url"]` unconditionally, and two channels are `enabled: false` placeholders
with no `url` key - a documented, supported shape, because a Skool or Vimeo
source is addressable by `--channel` but not scannable. The loop crashed before
`channel_name` could fall back to the slugified title or `_standalone`, so ONE
url-less entry broke every manual run against an unconfigured video.

The issue asked whether `cmd_mindmap` and `_cmd_process_url` shared the matcher.
They did: THREE byte-identical copies of the same six lines, each carrying the
same crash and the same per-channel quota cost. That duplication is the finding,
so the fix is one shared helper rather than three patched copies.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi


@pytest.fixture
def counted_lookup(monkeypatch):
    """Replaces the one function that costs YouTube quota, and counts it."""
    calls: list[str] = []

    def fake(youtube, url):
        calls.append(url)
        return (f"UC-for-{url.rsplit('@', 1)[-1]}", url)

    monkeypatch.setattr(vi, "get_channel_id", fake)
    return calls


LIVE_SHAPE = {
    "channels": [
        {"name": "natebjones", "url": "https://youtube.com/@natebjones"},
        {"name": "skool-community", "enabled": False},  # no url: the reported shape
        {"name": "vimeo-source", "enabled": False},  # no url
        {"name": "everyinc", "url": "https://youtube.com/@everyinc"},
    ]
}


class TestTheReportedCrash:
    def test_a_url_less_channel_does_not_break_an_unconfigured_video(self, counted_lookup):
        """The headline. Pre-fix this raised KeyError on the second channel."""
        assert vi.match_configured_channel(object(), LIVE_SHAPE, "UC-for-nobody") is None

    def test_a_url_less_channel_does_not_hide_a_later_real_match(self, counted_lookup):
        """Skipping must not become a `break`: `everyinc` sits AFTER both
        url-less placeholders, so a fix that stopped at the first bad entry
        would silently stop matching every channel below it."""
        assert vi.match_configured_channel(object(), LIVE_SHAPE, "UC-for-everyinc") == "everyinc"

    def test_a_url_less_channel_costs_no_api_call(self, counted_lookup):
        vi.match_configured_channel(object(), LIVE_SHAPE, "UC-for-nobody")
        assert counted_lookup == [
            "https://youtube.com/@natebjones",
            "https://youtube.com/@everyinc",
        ], "a url-less channel reached the API"


class TestProbeBeforeYouPay:
    def test_a_non_youtube_url_is_never_submitted_to_the_youtube_api(self, counted_lookup):
        """`get_channel_id` submits the last path segment for ANY host, so a
        Skool url costs a quota unit and returns nothing useful. Same gate the
        issue #113 headline path uses, reused rather than re-derived."""
        cfg = {"channels": [{"name": "skool", "url": "https://www.skool.com/some-community"}]}
        assert vi.match_configured_channel(object(), cfg, "UC-anything") is None
        assert counted_lookup == []

    def test_a_bare_uc_id_in_config_needs_no_lookup_at_all(self, counted_lookup):
        cfg = {"channels": [{"name": "prefetched", "url": "UCabcdefghijklmnopqrstuv"}]}
        assert vi.match_configured_channel(object(), cfg, "UCabcdefghijklmnopqrstuv") == "prefetched"
        assert counted_lookup == []

    def test_the_walk_stops_at_the_match(self, counted_lookup):
        """The matcher used to resolve EVERY configured channel before deciding.
        On a ~75-channel watchlist that is ~75 quota units per manual run."""
        cfg = {"channels": [{"name": f"c{i}", "url": f"https://youtube.com/@c{i}"} for i in range(50)]}
        vi.match_configured_channel(object(), cfg, "UC-for-c2")
        assert len(counted_lookup) == 3, "the walk continued past the match"

    def test_one_channel_is_resolved_once_per_call(self, counted_lookup):
        """A config listing the same url twice must not pay twice."""
        url = "https://youtube.com/@dupe"
        cfg = {"channels": [{"name": "a", "url": url}, {"name": "b", "url": url}]}
        vi.match_configured_channel(object(), cfg, "UC-for-nobody")
        assert counted_lookup == [url]


class TestDegenerateConfigsDoNotCrash:
    """A manual `--url` run must survive whatever the config happens to be:
    matching a channel is a convenience, and `_standalone` is always available."""

    @pytest.mark.parametrize(
        ("label", "config"),
        [
            ("no channels key", {}),
            ("channels is None", {"channels": None}),
            ("channels is a dict", {"channels": {"name": "x"}}),
            ("channels is a string", {"channels": "natebjones"}),
            ("a non-dict entry", {"channels": ["oops", {"name": "ok", "url": "https://youtube.com/@ok"}]}),
            ("url is not a string", {"channels": [{"name": "x", "url": 42}]}),
            ("url is empty", {"channels": [{"name": "x", "url": "   "}]}),
        ],
    )
    def test_no_crash(self, label, config, counted_lookup):
        vi.match_configured_channel(object(), config, "UC-for-nobody")

    def test_channels_none_is_the_yaml_empty_key_shape(self, counted_lookup):
        """`channels:` with nothing under it parses as None, and
        `config.get("channels", [])` hands that None straight to `for`. Caught
        by Gate 1 on this very fix, not by reading it."""
        assert vi.match_configured_channel(object(), {"channels": None}, "UC-x") is None

    def test_a_matched_channel_with_no_usable_name_is_ignored_not_returned(self, counted_lookup):
        """Returning None (falling back to the slugified title) beats returning
        an unusable name that would become a directory."""
        cfg = {"channels": [{"url": "https://youtube.com/@ok"}]}
        assert vi.match_configured_channel(object(), cfg, "UC-for-ok") is None

    def test_a_lookup_that_raises_does_not_kill_the_run(self, monkeypatch):
        """A mid-walk quotaExceeded must degrade to "no match", not a traceback
        - the whole point is that this is a convenience lookup."""

        def raising(youtube, url):
            raise RuntimeError("quotaExceeded")

        monkeypatch.setattr(vi, "get_channel_id", raising)
        assert vi.match_configured_channel(object(), LIVE_SHAPE, "UC-for-everyinc") is None

    def test_no_channel_id_short_circuits_before_any_work(self, counted_lookup):
        assert vi.match_configured_channel(object(), LIVE_SHAPE, None) is None
        assert vi.match_configured_channel(object(), LIVE_SHAPE, "") is None
        assert counted_lookup == []


class TestOneMatcherNotThree:
    """The duplication IS the finding. Three copies carried one crash."""

    def test_no_command_still_reads_ch_url_unconditionally(self):
        """`cmd_scan` keeps its own `ch["url"]` read - a SCANNABLE channel must
        have a url, and that read is not the matcher. Every other one is gone."""
        import re

        source = (Path(__file__).resolve().parent.parent / "scripts" / "video_intel.py").read_text(encoding="utf-8")
        hits = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(source.split("\n"), 1)
            if re.search(r'ch\["url"\]', line) and not line.strip().startswith(("#", "*"))
        ]
        assert len(hits) == 1, f"expected only cmd_scan's own read, found: {hits}"
        assert "ch_url = " in hits[0]

    def test_every_manual_url_command_routes_through_the_shared_matcher(self):
        """A fourth `--url` command must reuse this, not paste a fourth copy."""
        source = (Path(__file__).resolve().parent.parent / "scripts" / "video_intel.py").read_text(encoding="utf-8")
        # One definition, three call sites.
        assert source.count("def match_configured_channel(") == 1
        assert source.count("match_configured_channel(") == 4

    def test_the_source_walk_is_not_vacuous(self):
        """Companion: if the helper were renamed, the counts above would both
        drop to 0 and the test would pass by finding nothing."""
        assert callable(vi.match_configured_channel)
        assert callable(vi._configured_channel_id)
