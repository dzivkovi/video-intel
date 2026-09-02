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

import contextlib
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


# ---------------------------------------------------------------------------
# Caller-level coverage.
#
# Everything above drives `match_configured_channel` directly. That is not
# enough, and the review pass proved it the only way that counts: it reverted
# `_cmd_process_url` to the verbatim pre-#205 inline loop - using a different
# loop variable (`entry["url"]`) plus one decoy mention to keep the name count
# at four - and all 21 tests stayed green while the real CLI raised
# `KeyError: 'url'`. The exact bug this ticket is about was live under a green
# suite.
#
# Both source-walk guards are structurally blind to that: one greps the literal
# substring `ch["url"]`, which a loop named `entry` evades, and the other is a
# raw `source.count("match_configured_channel(")`, which any other textual
# occurrence - a future code comment with parentheses - restores.
#
# So these tests drive the three real commands. Only the YouTube client is
# stubbed; the channel resolution under test is production code.
# ---------------------------------------------------------------------------


class _FakeYouTube:
    """Minimal stand-in for the Data API client, snippet lookup only."""

    def __init__(self, channel_id: str, channel_title: str = "Some Creator"):
        self._channel_id = channel_id
        self._channel_title = channel_title

    def videos(self):
        return self

    def list(self, **_kw):
        return self

    def execute(self):
        return {
            "items": [
                {
                    "snippet": {
                        "title": "A Talk",
                        "publishedAt": "2026-08-12T00:00:00Z",
                        "channelId": self._channel_id,
                        "channelTitle": self._channel_title,
                    }
                }
            ]
        }


@pytest.fixture
def wire_commands(monkeypatch, tmp_path):
    """Neutralize everything the three commands touch EXCEPT channel resolution."""

    def _wire(config, yt_channel_id, *, lookup=None):
        seen: dict = {}

        def fake_get_channel_id(youtube, url):
            return (f"UC-for-{url.rsplit('@', 1)[-1]}", url)

        monkeypatch.setattr(vi, "get_channel_id", lookup or fake_get_channel_id)
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: _FakeYouTube(yt_channel_id))
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
        monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _v: False)
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _v: 600)
        monkeypatch.setattr(vi, "require_channels_config", lambda _c: None)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake-yt-key")

        def record(*a, **kw):
            # Record EVERYTHING. These helpers take the channel positionally on
            # some paths and by keyword on others, and a capture that guesses
            # the wrong one silently records None - which reads as "the match
            # failed" and would make this test lie in the safe direction.
            seen.setdefault("seen_values", []).extend(str(x) for x in a)
            seen["seen_values"].extend(str(v) for v in kw.values())
            seen.setdefault("called", 0)
            seen["called"] += 1
            return ("2026-08-12-a-talk", "done")

        for name in ("process_transcript", "process_mindmap", "process_concepts"):
            monkeypatch.setattr(vi, name, record)
        return seen

    return _wire


def _args(**overrides):
    from types import SimpleNamespace

    base = {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "file": None,
        "channel": None,
        "title": None,
        "date": None,
        "start": None,
        "end": None,
        "force": False,
        "prompt": None,
        "model": None,
        "video_id": None,
        "media_resolution": "low",
        "chunk_minutes": None,
        "transcript_source": None,
        "topic": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEveryManualUrlCommandSurvivesTheReportedConfig:
    """LIVE_SHAPE has two url-less `enabled: false` placeholders ABOVE a real
    channel - the config that produced the reported `KeyError: 'url'`."""

    @pytest.mark.parametrize("command", ["transcript", "mindmap", "process"])
    def test_an_unconfigured_video_does_not_crash(self, command, wire_commands, monkeypatch):
        wire_commands(LIVE_SHAPE, "UC-for-nobody")
        fn = {
            "transcript": vi.cmd_transcript,
            "mindmap": vi.cmd_mindmap,
            "process": vi.cmd_process,
        }[command]
        # Any exception is the defect; a clean return or SystemExit is not.
        try:
            fn(_args(), dict(LIVE_SHAPE))
        except SystemExit:
            pass
        except KeyError as e:  # the reported crash
            pytest.fail(f"{command} still raises the #205 crash: KeyError({e})")

    @pytest.mark.parametrize("command", ["transcript", "mindmap", "process"])
    def test_a_configured_video_below_the_placeholders_still_resolves(self, command, wire_commands, monkeypatch):
        """`everyinc` sits AFTER both url-less entries. A fix that stopped at
        the first bad entry would silently stop matching everything below it."""
        seen = wire_commands(LIVE_SHAPE, "UC-for-everyinc")
        fn = {
            "transcript": vi.cmd_transcript,
            "mindmap": vi.cmd_mindmap,
            "process": vi.cmd_process,
        }[command]
        with contextlib.suppress(SystemExit):
            fn(_args(), dict(LIVE_SHAPE))
        assert seen.get("called"), f"{command} never reached a processing step"
        values = seen.get("seen_values", [])
        assert any("everyinc" in v for v in values), f"{command} did not resolve the configured channel; saw: {values}"
        assert not any("some-creator" in v.lower() for v in values), (
            f"{command} fell back to the slugified title instead of matching: {values}"
        )


class TestDegenerateConfigsDoNotCrashTheCommands:
    """The helper-level suite proves the MATCHER survives these. The commands
    did not: `channel_config_by_name` read `c.get("name")` with no dict check,
    so three ordinary YAML mistakes raised AttributeError one function past the
    hardened matcher, and `mindmap` had its own inline copy that broke on
    `channels: None` too.
    """

    @pytest.mark.parametrize(
        ("label", "config"),
        [
            ("channels is None", {"channels": None}),
            ("channels is a mapping (dashes omitted)", {"channels": {"name": "alpha"}}),
            ("channels is a bare string", {"channels": "alpha"}),
            ("a scalar list entry", {"channels": ["alpha", {"name": "beta", "url": "https://youtube.com/@beta"}]}),
        ],
    )
    @pytest.mark.parametrize("command", ["transcript", "mindmap", "process"])
    def test_no_traceback(self, label, config, command, wire_commands):
        wire_commands(config, "UC-for-nobody")
        fn = {
            "transcript": vi.cmd_transcript,
            "mindmap": vi.cmd_mindmap,
            "process": vi.cmd_process,
        }[command]
        try:
            fn(_args(), dict(config))
        except SystemExit:
            pass
        except (AttributeError, TypeError, KeyError) as e:
            pytest.fail(f"{command} crashed on {label}: {type(e).__name__}: {e}")


class TestSkippingIsAlwaysContinueNeverBreak:
    """Invariant 2, applied to every skip reason - not just the url-less one.
    The review found the non-dict skip and the nameless-match skip both
    unpinned; the latter was a `return None`, i.e. a `break` in disguise."""

    def test_a_non_dict_entry_does_not_hide_a_later_real_match(self, counted_lookup):
        cfg = {"channels": ["oops", {"name": "real", "url": "https://youtube.com/@real"}]}
        assert vi.match_configured_channel(object(), cfg, "UC-for-real") == "real"

    def test_a_nameless_entry_does_not_hide_a_later_real_match(self, counted_lookup):
        """Both entries resolve to the SAME channel id. The nameless one is
        found first and has no usable name; returning None there would report
        "unconfigured" for a video whose channel IS configured, one line down."""
        url = "https://youtube.com/@twin"
        cfg = {"channels": [{"url": url}, {"name": "twin", "url": url}]}
        assert vi.match_configured_channel(object(), cfg, "UC-for-twin") == "twin"


class TestBothChannelsNoneGuardsAreLoadBearing:
    """`config.get("channels") or []` and the `isinstance(c, dict)` check guard
    DIFFERENT shapes. The review found that removing either one alone left all
    21 tests green - only removing both failed anything, which means neither
    was actually pinned."""

    def test_the_or_empty_list_guard_is_pinned(self):
        """Falsified by reverting to `config.get("channels", [])`."""
        assert vi.match_configured_channel(object(), {"channels": None}, "UC-x") is None
        assert vi.channel_config_by_name({"channels": None}, "alpha") == {}

    def test_the_isinstance_dict_guard_is_pinned(self):
        """Falsified by dropping `isinstance(c, dict)`. A bare string entry is
        an ordinary YAML mistake (`- alpha` for `- name: alpha`)."""
        cfg = {"channels": ["alpha"]}
        assert vi.match_configured_channel(object(), cfg, "UC-x") is None
        assert vi.channel_config_by_name(cfg, "alpha") == {}


class TestTheLookupIsSharedNotCopied:
    """The #205 finding was that ONE loop existed as three copies. The review
    then found a FOURTH copy of the adjacent channel-config lookup in
    `_cmd_mindmap_impl` and a fifth in `_cmd_process_impl`, both using
    `config.get("channels", [])` - verbatim the shape the matcher's own comment
    calls out as the `channels: None` crash. Both now route through
    `channel_config_by_name`."""

    def test_only_the_shared_helper_looks_a_channel_up_by_name(self):
        source = (Path(__file__).resolve().parent.parent / "scripts" / "video_intel.py").read_text(encoding="utf-8")
        hits = [
            f"{n}: {line.strip()}"
            for n, line in enumerate(source.split("\n"), 1)
            if 'c.get("name") == channel_name' in line
        ]
        assert len(hits) == 1, f"expected only channel_config_by_name's own lookup, found: {hits}"

    def test_the_walk_is_not_vacuous(self):
        assert callable(vi.channel_config_by_name)
        assert vi.channel_config_by_name({"channels": [{"name": "a", "x": 1}]}, "a") == {"name": "a", "x": 1}
