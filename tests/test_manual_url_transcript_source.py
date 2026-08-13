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
