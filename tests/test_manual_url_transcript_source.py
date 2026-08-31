"""Issue #127: manual `transcript --url` must honor a channel's transcript_source.

`cmd_transcript` used to hand `resolve_transcript_source` a literal `{}`, so a
channel configured `transcript_source: yt-captions` (usually for cost control)
still got a full Gemini call on a one-off manual run - the opposite of what
config.yaml promises. `_cmd_process_url` already read the channel dict, so the
two adjacent manual paths disagreed about the same config.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

import video_intel as vi
from video_intel import channel_config_by_name


class TestChannelConfigByName:
    def test_returns_the_matching_channel_dict(self):
        config = {"channels": [{"name": "alpha", "transcript_source": "yt-captions"}, {"name": "beta"}]}

        assert channel_config_by_name(config, "alpha") == {"name": "alpha", "transcript_source": "yt-captions"}

    @pytest.mark.parametrize(
        "channel_name",
        [None, "", "_standalone", "not-on-the-watchlist"],
        ids=["none", "empty", "standalone_sentinel", "unconfigured"],
    )
    def test_unresolvable_channel_yields_empty_dict(self, channel_name):
        """An empty dict must never be faked into carrying defaults.

        `livestream_captions_first_applies` distinguishes an ABSENT
        transcript_source key from a present one, so a synthesized default here
        would silently flip VOD routing.
        """
        config = {"channels": [{"name": "alpha", "transcript_source": "gemini"}]}

        assert channel_config_by_name(config, channel_name) == {}

    def test_no_channels_key_at_all(self):
        assert channel_config_by_name({}, "alpha") == {}


def _args(**overrides):
    base = {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "file": None,
        "channel": "alpha",
        "title": "A Talk",
        "date": "2026-08-12",
        "start": None,
        "end": None,
        "force": False,
        "transcript_source": None,
        "media_resolution": "low",
        "chunk_minutes": None,
        "prompt": None,
        "model": None,
        "video_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _RecordingTranscript:
    """Captures the transcript_source cmd_transcript resolved for the call."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return ("2026-08-12-a-talk", "done")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Neutralize everything cmd_transcript touches except source resolution."""
    recorder = _RecordingTranscript()
    monkeypatch.setattr(vi, "process_transcript", recorder)
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _vid: False)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _vid: 600)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return recorder


CONFIG_CAPTIONS = {
    "channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "yt-captions"}]
}
CONFIG_PLAIN = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha"}]}


class TestManualUrlHonorsChannelTranscriptSource:
    def test_channel_yt_captions_is_honored_with_no_cli_flag(self, wired):
        """The issue's headline case: config said captions, so no Gemini call."""
        vi.cmd_transcript(_args(), CONFIG_CAPTIONS)

        assert wired.calls, "process_transcript should have been reached"
        assert wired.calls[0]["transcript_source"] == "yt-captions"

    def test_cli_flag_beats_the_channel_config(self, wired):
        vi.cmd_transcript(_args(transcript_source="gemini"), CONFIG_CAPTIONS)

        assert wired.calls[0]["transcript_source"] == "gemini"

    def test_channel_without_the_key_keeps_the_default(self, wired):
        vi.cmd_transcript(_args(), CONFIG_PLAIN)

        assert wired.calls[0]["transcript_source"] == "gemini"

    def test_unresolvable_channel_keeps_the_default(self, wired):
        """No behavior change where there is no channel to read."""
        vi.cmd_transcript(_args(channel=None), {"channels": []})

        assert wired.calls[0]["transcript_source"] == "gemini"


class TestLivestreamProvenanceUsesTheSameChannelView:
    """The #120 asymmetry this issue names: two decisions, one config view."""

    def test_channel_level_explicit_gemini_keeps_a_vod_gemini_first(self, wired, monkeypatch):
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _vid: True)
        config = {"channels": [{"name": "alpha", "url": "u", "transcript_source": "gemini"}]}

        vi.cmd_transcript(_args(), config)

        assert wired.calls[0]["livestream_captions_first"] is False, (
            "an explicit channel-level gemini must be honored, same as the CLI flag"
        )

    def test_vod_with_no_preference_anywhere_still_routes_captions_first(self, wired, monkeypatch):
        monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _vid: True)

        vi.cmd_transcript(_args(), CONFIG_PLAIN)

        assert wired.calls[0]["livestream_captions_first"] is True, (
            "implicit default must keep issue #120's captions-first routing"
        )


class TestManualSegmentKeepsTheCliOnlyAnswer:
    """A clipped segment must not inherit a channel-level captions preference.

    Under `transcript_source: auto` every Gemini failure branch falls back to
    `_try_captions_transcript`, whose only overwrite guard is
    `exists() and not force`. So the documented high-res segment recovery
    (`transcript --url --force --start .. --end .. --media-resolution high`)
    could replace a good full multimodal transcript with a segment-clipped,
    speech-only captions one. Pre-#127 that was unreachable here because the
    source was always "gemini"; honoring channel config on `--url` made it
    reachable, so segments are exempted for the same reason `--file` is.
    """

    CONFIG_AUTO: ClassVar[dict] = {"channels": [{"name": "alpha", "url": "u", "transcript_source": "auto"}]}

    @pytest.mark.parametrize(
        ("start", "end"),
        [("00:10", "05:00"), ("00:10", None), (None, "05:00")],
        ids=["both", "start_only", "end_only"],
    )
    def test_segment_run_does_not_pick_up_channel_auto(self, wired, start, end):
        vi.cmd_transcript(_args(start=start, end=end, force=True), self.CONFIG_AUTO)

        assert wired.calls[0]["transcript_source"] == "gemini", (
            "an auto channel must not redirect a clipped segment into the captions failover"
        )

    def test_unclipped_run_on_the_same_channel_still_honors_auto(self, wired):
        """The exemption is scoped to segments, not a blanket opt-out."""
        vi.cmd_transcript(_args(), self.CONFIG_AUTO)

        assert wired.calls[0]["transcript_source"] == "auto"

    def test_cli_flag_still_wins_on_a_segment(self, wired):
        vi.cmd_transcript(_args(start="00:10", end="05:00", transcript_source="yt-captions"), self.CONFIG_AUTO)

        assert wired.calls[0]["transcript_source"] == "yt-captions"


class TestAutoChannelIsTheLiveCase:
    """Every channel configured in the live config uses `auto`; cover it."""

    CONFIG_AUTO: ClassVar[dict] = {"channels": [{"name": "alpha", "url": "u", "transcript_source": "auto"}]}

    def test_auto_reaches_process_transcript(self, wired):
        vi.cmd_transcript(_args(), self.CONFIG_AUTO)

        assert wired.calls[0]["transcript_source"] == "auto"

    def test_auto_survives_the_chunked_branch(self, wired, monkeypatch):
        """A long video routes through the chunker; the resolution must precede it."""
        chunked = []
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _v: 7000)
        monkeypatch.setattr(vi, "_run_chunked_transcript_url", lambda **kw: chunked.append(kw) or "done")

        vi.cmd_transcript(_args(), self.CONFIG_AUTO)

        assert chunked, "a 7000s video must take the chunked path"
        assert not wired.calls, "and must not also take the single-shot path"


class TestFileBranchScopeIsEnforced:
    """The --url-only scope is load-bearing, so a test must fail if it is hoisted."""

    def test_local_file_ignores_a_channel_captions_preference(self, wired, monkeypatch, tmp_path):
        mp4 = tmp_path / "talk.mp4"
        mp4.write_bytes(b"\x00" * 32)
        monkeypatch.setattr(vi, "upload_local_video", lambda _c, _p: "files/xyz")
        monkeypatch.setattr(vi, "require_channels_config", lambda _c: None)
        monkeypatch.setattr(
            vi,
            "resolve_local_file_identity",
            lambda *a, **kw: {
                "video_id": "vid",
                "url": "https://www.youtube.com/watch?v=vid",
                "title": "T",
                "published": "2026-08-12",
                "published_source": "flag",
                "channel": "alpha",
                "channel_dir": tmp_path / "alpha",
                "prefix": "2026-08-12-t",
                "meta_path": tmp_path / "alpha" / "2026-08-12-t.meta.json",
            },
        )

        vi.cmd_transcript(_args(url=None, file=str(mp4)), CONFIG_CAPTIONS)

        assert wired.calls[0]["transcript_source"] == "gemini", (
            "a local file is an explicit instruction; a channel captions preference cannot apply to it"
        )


# ---------------------------------------------------------------------------
# Issue #135: a channel-config typo must produce one actionable error, never a
# raw traceback. `resolve_transcript_source` itself already raises ValueError
# on an invalid value (protecting the CLI route via argparse `choices`); the
# CONFIG route through `cmd_transcript` --url and `_cmd_process_url` had no
# guard at all, and `cmd_scan`'s per-channel loop had no guard either - so a
# typo there killed the whole scan after quota/spend were already sunk.
# ---------------------------------------------------------------------------

CONFIG_TYPO = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "captions"}]}
CONFIG_TRAILING_SPACE = {
    "channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "auto "}]
}


class TestManualUrlChannelConfigTypoIsActionable:
    """The --url branch at :6308 (issue #127) that reads channel config."""

    def test_typo_exits_cleanly_naming_valid_values_no_traceback(self, wired, caplog):
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc_info:
            vi.cmd_transcript(_args(), CONFIG_TYPO)

        assert exc_info.value.code == 1
        assert not wired.calls, "process_transcript must never be reached on a bad config value"
        messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert len(messages) == 1, f"expected exactly one ERROR log line, got: {messages}"
        assert "captions" in messages[0]
        assert "auto" in messages[0] and "gemini" in messages[0] and "yt-captions" in messages[0], (
            "the error must name the valid values, matching resolve_transcript_source's own message"
        )

    def test_trailing_space_on_auto_is_rejected_the_same_way(self, wired, caplog):
        """The issue names this exact typo shape: 'auto ' is not 'auto'."""
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc_info:
            vi.cmd_transcript(_args(), CONFIG_TRAILING_SPACE)

        assert exc_info.value.code == 1
        assert not wired.calls

    def test_valid_values_are_unaffected(self, wired):
        """The guard must not change behavior for a value that was always valid."""
        vi.cmd_transcript(_args(), CONFIG_CAPTIONS)

        assert wired.calls[0]["transcript_source"] == "yt-captions"

    def test_cli_flag_route_is_unaffected(self, wired):
        """argparse `choices` already protects the CLI flag; the guard must be
        a no-op on that path (byte-identical behavior for valid input)."""
        vi.cmd_transcript(_args(transcript_source="gemini"), CONFIG_TYPO)

        assert wired.calls[0]["transcript_source"] == "gemini"


class TestCmdProcessUrlChannelConfigTypoIsActionable:
    """The `_cmd_process_url` call site at :6698."""

    @staticmethod
    def _args(**overrides):
        base = {
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
            "channel": "alpha",
            "title": "A Talk",
            "date": "2026-08-12",
            "start": None,
            "end": None,
            "force": False,
            "transcript_source": None,
            "media_resolution": "low",
            "chunk_minutes": None,
            "prompt": None,
            "model": None,
            "video_id": None,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    @pytest.fixture
    def stubbed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "load_prompt", lambda name: f"prompt-for-{name}")
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        return tmp_path

    def test_typo_exits_cleanly_naming_valid_values_no_traceback(self, stubbed, caplog):
        with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc_info:
            vi._cmd_process_url(self._args(), CONFIG_TYPO)

        assert exc_info.value.code == 1
        messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert len(messages) == 1, f"expected exactly one ERROR log line, got: {messages}"
        assert "captions" in messages[0]
        assert "auto" in messages[0] and "gemini" in messages[0] and "yt-captions" in messages[0]

    def test_valid_config_is_unaffected(self, stubbed, monkeypatch):
        """Reaching past the guard with a valid value must not regress -
        proven by observing the resolved value the very next line reads."""
        seen: dict = {}
        real_resolver = vi.resolve_transcript_source

        def spy(channel_cfg, cli_override=None):
            result = real_resolver(channel_cfg, cli_override)
            seen["value"] = result
            return result

        monkeypatch.setattr(vi, "resolve_transcript_source", spy)
        # Let the run stop right after the guard (the next line it reaches) so
        # the test stays scoped to the guard itself, not the rest of the
        # pipeline.
        monkeypatch.setattr(
            vi,
            "_lookup_video_duration_seconds",
            lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("stop-after-guard")),
        )

        with pytest.raises(RuntimeError, match="stop-after-guard"):
            vi._cmd_process_url(self._args(), CONFIG_CAPTIONS)

        assert seen["value"] == "yt-captions"


def _scan_args(**overrides):
    base = {"dry_run": False, "channel": None, "force": False, "since": None, "model": None}
    base.update(overrides)
    return SimpleNamespace(**base)


class TestScanChannelConfigTypoSkipsOnlyThatChannel:
    """The per-channel loop guard at :5536 - the load-bearing case in #135:
    one channel's typo must not abort the whole scan after YouTube quota and
    Gemini spend are already sunk on other channels.

    The typo channel is listed FIRST in every config below (not last) so
    "continues to the NEXT channel" is actually exercised - with the typo
    channel last, a passing test would prove nothing about `continue` at
    all, since there is no next channel to reach.
    """

    good_video: ClassVar[dict] = {
        "video_id": "good1",
        "title": "Good video",
        "published": "2026-04-15",
        "url": "https://www.youtube.com/watch?v=good1",
    }
    typo_video: ClassVar[dict] = {
        "video_id": "typo1",
        "title": "Typo channel video",
        "published": "2026-04-15",
        "url": "https://www.youtube.com/watch?v=typo1",
    }

    def _run_scan(self, monkeypatch, caplog, tmp_path, *, bad_transcript_source):
        """Drive the real cmd_scan with only the network/paid boundary stubbed.

        Returns (transcripts_seen, mindmaps_seen, caplog) for the caller to
        assert against. `bad_transcript_source` is whatever invalid value the
        test wants to plant on the typo channel - a typo string, a mapping,
        or a sequence (issue #135 review: a non-string value fails the
        resolver's `in` check with TypeError, not ValueError, and must be
        caught the same way).
        """
        videos_by_channel_url = {
            "https://example.com/typo": [self.typo_video],
            "https://example.com/good": [self.good_video],
        }

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: (url, url))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos_by_channel_url.get(cid, [])))
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

        transcripts_seen: list[str] = []
        mindmaps_seen: list[str] = []

        def fake_transcript(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            transcripts_seen.append(video["video_id"])
            return (video.get("video_id", "prefix"), "done")

        def fake_mindmap(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            mindmaps_seen.append(video["video_id"])
            return (video.get("video_id", "prefix"), "done")

        monkeypatch.setattr(vi, "process_transcript", fake_transcript)
        monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "typo",
                    "url": "https://example.com/typo",
                    "auto_transcript": "all",
                    "transcript_source": bad_transcript_source,
                },
                {"name": "good", "url": "https://example.com/good", "auto_transcript": "all"},
            ],
        }

        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(), config)

        return transcripts_seen, mindmaps_seen

    def test_typo_channel_skipped_healthy_channel_still_processed(self, tmp_path, monkeypatch, caplog):
        transcripts_seen, mindmaps_seen = self._run_scan(
            monkeypatch, caplog, tmp_path, bad_transcript_source="captions"
        )

        assert "good1" in transcripts_seen, "the healthy channel's transcript must still run"
        assert "typo1" not in transcripts_seen, "the typo channel's transcript step must be skipped"
        # Item 4a: the mindmap loop must be proven too, not just recorded and
        # ignored - the whole point of the broad `continue` (item 3) is that
        # BOTH steps are skipped for the typo channel, and only asserting
        # transcripts would let a regression that runs mindmap anyway through.
        assert "good1" in mindmaps_seen, "the healthy channel's mindmap must still run"
        assert "typo1" not in mindmaps_seen, "the typo channel's mindmap step must also be skipped"

        error_messages = [
            r.message for r in caplog.records if r.levelname == "ERROR" and "transcript_source" in r.message
        ]
        # Issue #169: the top-of-loop preflight (validate_channel_knobs) now
        # logs this same problem once, in addition to this existing skip
        # site's own log line - two ERROR lines about the same channel, by
        # design (see tests/test_channel_knob_preflight.py for the dedicated
        # coverage of that preflight).
        assert len(error_messages) == 2, f"expected preflight + skip-site error lines, got: {error_messages}"
        assert all("typo" in m for m in error_messages), "every error must name the offending channel"
        assert any("entire channel" in m for m in error_messages), (
            "the skip-site message must say the WHOLE channel is skipped, not just transcripts"
        )

    def test_typo_channel_appears_in_the_end_of_scan_failure_summary(self, tmp_path, monkeypatch, caplog):
        """Item 2 (Codex + in-family corroborated): a `continue` with no
        `errors.append` leaves the scan reporting `Done.` and exiting 0 with
        a channel silently dropped. The skipped channel must show up in the
        `--- N FAILED ---` block, same as the mirrored mindmap guard does.
        """
        self._run_scan(monkeypatch, caplog, tmp_path, bad_transcript_source="captions")

        summary_lines = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("FAILED" in line for line in summary_lines), (
            f"expected a '--- N FAILED ---' summary line, got: {summary_lines}"
        )
        assert any("typo" in line and "transcript_source" in line.lower() for line in summary_lines), (
            f"expected the typo channel named in the failure summary, got: {summary_lines}"
        )

    @pytest.mark.parametrize(
        "bad_value",
        [{"mode": "auto"}, ["auto"]],
        ids=["yaml_mapping", "yaml_sequence"],
    )
    def test_non_string_config_value_is_caught_not_a_raw_typeerror(self, tmp_path, monkeypatch, caplog, bad_value):
        """Item 1 (P1, executed): `transcript_source: {mode: auto}` or
        `[auto]` fails the resolver's `in` membership test with TypeError,
        not ValueError - a bare `except ValueError` lets it escape and kill
        the whole scan exactly like the original bug. This test would have
        caught that hole; it did not exist before this PR's second commit.
        """
        transcripts_seen, mindmaps_seen = self._run_scan(monkeypatch, caplog, tmp_path, bad_transcript_source=bad_value)

        assert "good1" in transcripts_seen, "a non-string typo must not abort the whole scan either"
        assert "typo1" not in transcripts_seen
        assert "good1" in mindmaps_seen, "the healthy channel's mindmap must also survive a non-string typo"
        assert "typo1" not in mindmaps_seen

        error_messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert error_messages, "the non-string value must produce a clean ERROR line, not a silent pass-through"
        assert "typo" in error_messages[0]


class TestScanMindmapSourceNonStringConfigValueInThreadedClosure:
    """Coordinator follow-up (third commit): a fourth `resolve_mindmap_source`
    call site was missed in the first two commits - `cmd_scan`'s per-video
    mindmap closure at ~:5709. It matters MORE than the ones already guarded:
    the closure runs inside a `ThreadPoolExecutor` worker, so an escaping
    `TypeError` does not just fail one video the way the closure's normal
    `return v_prefix, f"error: {exc}"` does - `future.result()` re-raises it
    with nothing above catching it, taking down the whole mindmap stage for
    the channel (and the scan run) after transcript work and quota are
    already spent. That is the exact "one typo must not cost the run"
    failure #135 exists to prevent.
    """

    def test_non_string_mindmap_source_yields_a_per_video_error_not_an_escaping_traceback(
        self, tmp_path, monkeypatch, caplog
    ):
        typo_video = {
            "video_id": "typo-mm1",
            "title": "Typo mindmap_source video",
            "published": "2026-04-15",
            "url": "https://www.youtube.com/watch?v=typo-mm1",
        }
        good_video = {
            "video_id": "good-mm1",
            "title": "Good mindmap video",
            "published": "2026-04-15",
            "url": "https://www.youtube.com/watch?v=good-mm1",
        }
        videos_by_channel_url = {
            "https://example.com/typo-mindmap": [typo_video],
            "https://example.com/good-mindmap": [good_video],
        }

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: (url, url))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos_by_channel_url.get(cid, [])))
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

        mindmaps_seen: list[str] = []

        def fake_mindmap(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            mindmaps_seen.append(video["video_id"])
            return (video.get("video_id", "prefix"), "done")

        monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)

        # auto_transcript defaults to "none" on both channels - this test is
        # scoped to the mindmap-loop guard alone, not the transcript-loop
        # guard already covered by TestScanChannelConfigTypoSkipsOnlyThatChannel.
        # The typo channel is listed FIRST so a pre-fix escaping TypeError
        # (which future.result() re-raises with nothing above it to catch)
        # would abort the run before the good channel is ever reached -
        # proving "one typo must not cost the run", not just "this channel
        # fails".
        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "typo-mindmap",
                    "url": "https://example.com/typo-mindmap",
                    "mindmap_source": {"mode": "auto"},
                },
                {"name": "good-mindmap", "url": "https://example.com/good-mindmap"},
            ],
        }

        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(), config)

        assert "good-mm1" in mindmaps_seen, (
            "the healthy channel's mindmap must still run - a non-string mindmap_source on "
            "an earlier channel must not abort the whole scan"
        )
        assert "typo-mm1" not in mindmaps_seen, "the typo video's mindmap call must never be reached"

        # The closure's existing `return v_prefix, f"error: {exc}"` shape is
        # unchanged: it lands in the failure summary through the normal
        # task-result path (`if status.startswith("error"): errors.append(...)`),
        # not through a converted errors.append call site of its own.
        summary_lines = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("FAILED" in line for line in summary_lines), (
            f"expected a '--- N FAILED ---' summary line, got: {summary_lines}"
        )
        assert any("typo-mindmap" in line and "unhashable type" in line for line in summary_lines), (
            f"expected the typo channel's video named in the failure summary, got: {summary_lines}"
        )
