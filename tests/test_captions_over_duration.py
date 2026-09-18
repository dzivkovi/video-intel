"""A long video should be gettable cheaply rather than not at all (#227).

Before this, a long video on a scanned channel had three outcomes and none of
them was "cheap but present":

1. it chunked into N Gemini calls,
2. it exceeded `transcript_max_duration_seconds` and was dropped from the
   transcript loop entirely, leaving no artifact,
3. the operator blocklisted it by hand in `skip_video_ids`.

`captions_over_duration_seconds` adds the fourth: build the transcript from
the free caption track, which is whole and costs nothing, and let the rest of
the chain continue (`mindmap_source: auto` routes off whatever transcript is on
disk, so the mindmap and concepts still happen).

The incident that produced it: `saminyasar/0VghkmXfE2o`, a 1h37m course. Its
chunked transcript came back `partial` with `backward_jump_severe`, which
correctly routed the mindmap back to `source="video"` (issue #157 containment)
- and mindmap-from-video has no wall-clock deadline (issue #129), so one scan
sat on that single call for roughly 55 minutes.

The load-bearing design decision, and the thing most likely to be "corrected"
later: this beats `transcript_max_duration_seconds` when both apply. That
guard exists because a long GEMINI transcript is expensive and truncates.
Neither is true of a caption track.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi


class TestResolverPrecedence:
    def test_unset_is_none_which_means_todays_behavior(self):
        assert vi.resolve_captions_over_duration({}, {}) is None

    def test_per_channel_wins_over_top_level(self):
        assert (
            vi.resolve_captions_over_duration(
                {"captions_over_duration_seconds": 1800}, {"captions_over_duration_seconds": 3600}
            )
            == 1800
        )

    def test_top_level_applies_when_the_channel_is_silent(self):
        assert vi.resolve_captions_over_duration({}, {"captions_over_duration_seconds": 3600}) == 3600

    def test_cli_wins_over_both(self):
        assert (
            vi.resolve_captions_over_duration(
                {"captions_over_duration_seconds": 1800}, {"captions_over_duration_seconds": 3600}, 600
            )
            == 600
        )

    def test_an_explicit_null_disables_it_for_one_channel(self):
        """So a channel can opt OUT of a top-level default."""
        assert (
            vi.resolve_captions_over_duration(
                {"captions_over_duration_seconds": None}, {"captions_over_duration_seconds": 3600}
            )
            is None
        )


class TestResolverRejectsTypos:
    @pytest.mark.parametrize("bad", ["sixty", "", [3600], {"seconds": 3600}, 0, -1, 3.5e400])
    def test_a_bad_value_raises_valueerror(self, bad):
        with pytest.raises(ValueError):
            vi.resolve_captions_over_duration({"captions_over_duration_seconds": bad}, {})

    @pytest.mark.parametrize("truthy", [True, False])
    def test_a_boolean_is_rejected_before_the_int_coercion(self, truthy):
        """PyYAML types an unquoted `yes` as True, isinstance(True, int) is
        True, and int(True) == 1 would silently mean "every video longer than
        one second" - i.e. the whole channel routed to captions. The bool
        check MUST precede int(); checking the other way round never fires."""
        with pytest.raises(ValueError, match="boolean"):
            vi.resolve_captions_over_duration({"captions_over_duration_seconds": truthy}, {})

    def test_a_float_is_accepted_and_truncated(self):
        """A YAML author writing 3600.0 meant 3600 seconds."""
        assert vi.resolve_captions_over_duration({"captions_over_duration_seconds": 3600.0}, {}) == 3600


class TestCallerLevel:
    """Drives the real `cmd_scan` and captures the transcript source each
    video was actually handed. A resolver that is correct but never reached is
    the same blind spot as a stub agreeing with its own assertion."""

    @pytest.fixture
    def scan(self, monkeypatch, tmp_path):
        def _run(channel_cfg, *, durations, top_level=None, cli=None):
            videos = [
                {
                    "video_id": f"vid{i:08d}",
                    "title": f"Video {i}",
                    "published": "2026-09-11",
                    "url": f"https://www.youtube.com/watch?v=vid{i:08d}",
                    "duration_iso": iso,
                }
                for i, iso in enumerate(durations)
            ]
            seen: list[tuple[str, str]] = []

            def record(**kw):
                seen.append((kw["video"]["title"], kw["transcript_source"]))
                return (kw["prefix"], "done")

            monkeypatch.setattr(vi, "_scan_transcribe_one", record)
            monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_k: tmp_path)
            monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **k: object())
            monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
            monkeypatch.setattr(vi, "create_client", lambda *_a, **_k: object())
            monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
            monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_k: "stub-model")
            monkeypatch.setattr(vi, "get_channel_id", lambda *_a, **_k: ("UCabc", "u"))
            monkeypatch.setattr(vi, "fetch_channel_videos", lambda *_a, **_k: [dict(v) for v in videos])
            # cmd_scan OVERWRITES v["duration_iso"] from this map, so the
            # stub has to supply it or every duration reads as unknown and
            # the whole class silently tests nothing.
            duration_map = {v["video_id"]: v["duration_iso"] for v in videos}
            monkeypatch.setattr(vi, "enrich_with_durations", lambda _y, _ids: dict(duration_map))
            monkeypatch.setattr(vi, "fetch_preflight_status", lambda *_a, **_k: {})
            monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
            monkeypatch.setattr(vi, "record_alt_title_if_rotated", lambda *_a, **_k: False)
            monkeypatch.setattr(vi, "render_headline_digest", lambda *_a, **_k: None)
            monkeypatch.setattr(vi, "process_mindmap", lambda *a, **k: ("p", "done"))
            monkeypatch.setenv("GEMINI_API_KEY", "k")
            monkeypatch.setenv("YOUTUBE_API_KEY", "k")

            args = SimpleNamespace(
                channel="chan",
                since=None,
                dry_run=False,
                force=False,
                model=None,
                prompt=None,
                media_resolution="low",
                chunk_minutes=None,
                transcript_source=None,
                topic=None,
                captions_over_duration=cli,
            )
            config = {
                "channels": [
                    {
                        "name": "chan",
                        "url": "https://youtube.com/@chan",
                        "auto_transcript": "all",
                        # skip_shorts defaults true, and is_short() makes a REAL
                        # HEAD request to youtube.com/shorts/<id> for ids it
                        # cannot classify from duration. Left on, these tests
                        # reach the network and become load-sensitive - the
                        # exact flakiness that made 31 unrelated tests fail
                        # under a concurrent scan tonight.
                        "skip_shorts": False,
                        **channel_cfg,
                    }
                ],
                **(top_level or {}),
            }
            vi.cmd_scan(args, config)
            return dict(seen)

        return _run

    def test_a_long_video_routes_to_captions_and_a_short_one_does_not(self, scan):
        seen = scan(
            {"captions_over_duration_seconds": 3600},
            durations=["PT20M", "PT1H37M"],
        )
        assert seen["Video 0"] == "gemini", "a short video must keep the configured source"
        assert seen["Video 1"] == "yt-captions", "the long one was not routed to captions"

    def test_unset_leaves_routing_byte_identical(self, scan):
        """The highest regression risk in the change."""
        seen = scan({}, durations=["PT20M", "PT1H37M"])
        assert set(seen.values()) == {"gemini"}

    def test_it_beats_the_max_duration_drop(self, scan):
        """THE design decision. Without this the video is dropped before the
        captions branch is ever consulted, and the knob does nothing for
        exactly the videos it exists for. `transcript_max_duration_seconds`
        guards against an expensive, truncating GEMINI transcript; a caption
        track is free and whole."""
        seen = scan(
            {"captions_over_duration_seconds": 3600, "transcript_max_duration_seconds": 7200},
            durations=["PT3H"],
        )
        assert seen.get("Video 0") == "yt-captions", "a 3h video was dropped instead of fetched cheaply"

    def test_the_max_duration_drop_still_applies_when_the_knob_is_unset(self, scan):
        seen = scan({"transcript_max_duration_seconds": 7200}, durations=["PT3H"])
        assert seen == {}, "the long-video guard stopped working"

    def test_an_unknown_duration_is_never_guessed_at(self, scan):
        """Same rule as the chunking decision: never infer a routing change
        from a duration we could not parse."""
        seen = scan({"captions_over_duration_seconds": 3600}, durations=[None])
        assert seen.get("Video 0") == "gemini"

    def test_a_channel_already_on_captions_is_not_relabelled(self, scan):
        seen = scan(
            {"captions_over_duration_seconds": 3600, "transcript_source": "yt-captions"},
            durations=["PT1H37M"],
        )
        assert seen.get("Video 0") == "yt-captions"

    def test_a_typoed_value_skips_the_channel_and_lands_in_the_failure_summary(self, scan, caplog):
        """Issue #135/#169: a config typo must not abort the whole scan, and
        must not leave it reporting Done. with a channel silently dropped.

        The FAILURE SUMMARY half is the load-bearing one and it is separate
        from the log line: the `log.error` and the `errors.append` are
        independent statements. An earlier version of this test asserted only
        `caplog` text, and deleting the `errors.append` left the whole file
        green - exactly the blind spot CLAUDE.md #168 names.
        """
        import logging

        with caplog.at_level(logging.WARNING):
            seen = scan({"captions_over_duration_seconds": "sixty"}, durations=["PT20M"])
        assert seen == {}
        assert "invalid captions_over_duration_seconds" in caplog.text
        assert "skipping entire channel" in caplog.text
        assert "--- 1 FAILED ---" in caplog.text, (
            "the channel was skipped but never reached the end-of-scan failure summary"
        )

    def test_an_explicit_gemini_channel_is_not_overridden_by_a_top_level_threshold(self, scan):
        """Issue #120 invariant 2: an explicit `transcript_source: gemini` is
        the documented escape hatch for a channel whose captions are known
        garbage, the wrong language, or whose on-screen content IS the content.
        A top-level threshold the channel never set must not silently undo it."""
        # 1h30m: over the captions threshold, UNDER the 2h default
        # transcript_max_duration_seconds, so it still reaches the transcript
        # step and the source it reaches with is the assertion. (A 3h video
        # would be dropped by that guard instead, which is correct pre-#227
        # behavior but would not test this.)
        seen = scan(
            {"transcript_source": "gemini"},
            durations=["PT1H30M"],
            top_level={"captions_over_duration_seconds": 3600},
        )
        assert seen.get("Video 0") == "gemini", "a top-level knob overrode the #120 escape hatch"

    def test_a_channel_that_did_not_ask_for_gemini_still_gets_the_top_level_threshold(self, scan):
        """The other half: an ABSENT transcript_source must stay
        distinguishable from an explicit "gemini", or the exclusion above
        disables the top-level knob for every channel."""
        seen = scan({}, durations=["PT1H30M"], top_level={"captions_over_duration_seconds": 3600})
        assert seen.get("Video 0") == "yt-captions"

    def test_the_cli_flag_reaches_the_routing(self, scan):
        seen = scan({}, durations=["PT1H37M"], cli=3600)
        assert seen.get("Video 0") == "yt-captions"
