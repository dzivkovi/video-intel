"""Issue #128: dense sub-threshold videos truncate at the OUTPUT cap.

Two halves:

* **Prevention** - `scan` could not chunk at all. Chunking lived only on
  `transcript --url`, `process --url` and `process --file`, so any dense video
  under `--chunk-minutes` truncated and had to be found and re-run by hand. A
  `chunk_minutes` knob now resolves per-channel > top-level > default, with a
  `--chunk-minutes` flag on `scan`.
* **Detection** (the half that compounds) - a salvage caused by the output cap
  is now `transcript_status: "truncated_output"` instead of the generic
  `"partial"`. Without that split, a salvage-from-malformed-JSON and a
  salvage-from-truncation are indistinguishable in meta.json, so there is no way
  to sweep the corpus for the videos a chunked re-run would actually fix.

Empirical anchors from the 2026-08-11/12 sessions: healthy per-chunk
`candidates` ran 1,028-10,742; the confirmed truncation reported
`candidates=65522` against the 65536 cap with `prompt=230741`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import video_intel as vi
from video_intel import MAX_OUTPUT_TOKENS, hit_output_cap, resolve_chunk_minutes

HEALTHY_CANDIDATES = [1028, 4200, 10742]


class TestResolveChunkMinutes:
    def test_default_when_nothing_configured(self):
        assert resolve_chunk_minutes({}, {}) == vi.TRANSCRIPT_CHUNK_MINUTES_DEFAULT

    def test_top_level_beats_default(self):
        assert resolve_chunk_minutes({}, {"chunk_minutes": 30}) == 30

    def test_per_channel_beats_top_level(self):
        assert resolve_chunk_minutes({"chunk_minutes": 20}, {"chunk_minutes": 30}) == 20

    def test_cli_beats_everything(self):
        assert resolve_chunk_minutes({"chunk_minutes": 20}, {"chunk_minutes": 30}, 15) == 15

    def test_zero_and_negative_are_rejected(self):
        """_build_transcript_chunks would raise later; fail at the config boundary instead."""
        for bad in (0, -5):
            with pytest.raises(ValueError):
                resolve_chunk_minutes({"chunk_minutes": bad}, {})


class TestHitOutputCap:
    @pytest.mark.parametrize("candidates", HEALTHY_CANDIDATES)
    def test_healthy_observed_counts_do_not_trip(self, candidates):
        assert hit_output_cap(candidates, "STOP", max_output_tokens=MAX_OUTPUT_TOKENS) is False

    def test_the_confirmed_truncation_trips(self):
        """candidates=65522 against a 65536 cap - the observed Garry Tan case."""
        assert hit_output_cap(65522, "STOP", max_output_tokens=MAX_OUTPUT_TOKENS) is True

    def test_finish_reason_alone_is_sufficient(self):
        """Gemini can reach MAX_TOKENS with thinking consuming the budget.

        The candidates count is then absent entirely, so a count-only check
        would miss it.
        """
        assert hit_output_cap(None, "MAX_TOKENS", max_output_tokens=MAX_OUTPUT_TOKENS) is True

    def test_unreadable_candidates_with_a_healthy_finish_reason_does_not_trip(self):
        assert hit_output_cap(None, "STOP", max_output_tokens=MAX_OUTPUT_TOKENS) is False
        assert hit_output_cap(None, None, max_output_tokens=MAX_OUTPUT_TOKENS) is False

    def test_enum_style_finish_reason_is_matched(self):
        assert hit_output_cap(None, "FinishReason.MAX_TOKENS", max_output_tokens=MAX_OUTPUT_TOKENS) is True

    def test_does_not_claim_the_monolithic_early_stop_shape(self):
        """The OTHER failure observed in the same sessions is NOT caught here.

        Gemini sometimes collapses a whole window into one monolithic block and
        stops with candidates nowhere near the cap. It has no output-budget
        signature; calling it "truncation" would be a false negative dressed up
        as coverage. This test exists so nobody later reads the detector as
        wider than it is.
        """
        assert hit_output_cap(3000, "STOP", max_output_tokens=MAX_OUTPUT_TOKENS) is False


def _resp(candidates, finish_reason="STOP", prompt=230741):
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=candidates,
            total_token_count=prompt + (candidates or 0),
        ),
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


#: Truncated mid-object, exactly as the observed failure looked.
TRUNCATED_JSON = (
    '{"transcripts": ['
    + ",".join(f'{{"start": "00:{i:02d}:10", "voice": 1, "text": "line {i}"}}' for i in range(40))
    + ',{"start": "00:41:00", "voice": 1, "text": "cut off mid'
)


@pytest.fixture
def fake_types():
    return SimpleNamespace(MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH"))


def _run_transcript(tmp_path, monkeypatch, fake_types, response):
    def fake_call_gemini(client, types, media_uri, prompt_text, model, response_json=False, **kw):
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(response)
        return TRUNCATED_JSON

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)

    channel_dir = tmp_path / "ycombinator"
    prefix = "2026-08-06-garry-tan-own-your-intelligence"
    result = vi.process_transcript(
        object(),
        fake_types,
        {
            "video_id": "eRrc1pUY5oU",
            "url": "https://www.youtube.com/watch?v=eRrc1pUY5oU",
            "title": "Garry Tan: Own Your Intelligence",
            "published": "2026-08-06",
        },
        "PROMPT",
        "stub-model",
        channel_dir,
        prefix,
        transcript_source="gemini",
    )
    meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
    return result, meta


class TestTruncatedOutputStatus:
    def test_output_cap_salvage_is_named_not_generic_partial(self, tmp_path, monkeypatch, fake_types):
        (_, status), meta = _run_transcript(tmp_path, monkeypatch, fake_types, _resp(65522))

        assert meta["transcript_status"] == "truncated_output"
        assert meta["transcript_status"] != "partial", "the whole point is that these are distinguishable"
        assert "truncated_output" in status

    def test_the_counts_are_persisted_for_the_sweep(self, tmp_path, monkeypatch, fake_types):
        """A human decides whether to pay for a re-run, so record what they need."""
        _, meta = _run_transcript(tmp_path, monkeypatch, fake_types, _resp(65522))

        assert meta["transcript_output_tokens"] == 65522
        assert meta["transcript_finish_reason"] == "STOP"

    def test_malformed_json_without_the_cap_signal_stays_generic_partial(self, tmp_path, monkeypatch, fake_types):
        """Not every salvage is a truncation; a chunked re-run would not fix this one."""
        _, meta = _run_transcript(tmp_path, monkeypatch, fake_types, _resp(4200))

        assert meta["transcript_status"] == "partial"
        assert "transcript_output_tokens" not in meta

    def test_max_tokens_finish_reason_alone_names_it_truncated(self, tmp_path, monkeypatch, fake_types):
        _, meta = _run_transcript(tmp_path, monkeypatch, fake_types, _resp(None, finish_reason="MAX_TOKENS"))

        assert meta["transcript_status"] == "truncated_output"

    def test_identity_is_still_stamped(self, tmp_path, monkeypatch, fake_types):
        _, meta = _run_transcript(tmp_path, monkeypatch, fake_types, _resp(65522))

        assert meta["video_id"] == "eRrc1pUY5oU"

    def test_truncated_output_is_not_a_healthy_status(self):
        """The mindmap resolver must treat it like partial, not like ok/complete."""
        assert vi.TRANSCRIPT_STATUS_TRUNCATED not in vi._HEALTHY_TRANSCRIPT_STATUSES


class TestScanCanChunk:
    """The prevention half: `scan` could not reach chunking at all."""

    @staticmethod
    def _scan(monkeypatch, tmp_path, channel_extra=None, top_level=None, cli_chunk_minutes=None, duration="PT1H10M"):
        videos = [
            {
                "video_id": "dense1",
                "title": "A dense keynote",
                "published": "2026-08-06",
                "url": "https://www.youtube.com/watch?v=dense1",
            }
        ]
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos))
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids, duration))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {v: {} for v in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)
        monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: ("dense1", "done"))

        seen = {"chunked": [], "single": []}
        monkeypatch.setattr(
            vi,
            "_run_chunked_transcript_url",
            lambda **kw: (seen["chunked"].append(kw["chunk_minutes"]), "done")[1],
        )
        monkeypatch.setattr(
            vi,
            "process_transcript",
            lambda *a, **kw: (seen["single"].append(True), ("dense1", "done"))[1],
        )

        channel = {"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"}
        channel.update(channel_extra or {})
        config = {"output_dir": str(tmp_path), "channels": [channel]}
        config.update(top_level or {})

        args = SimpleNamespace(
            channel=None,
            since=None,
            dry_run=False,
            force=False,
            model=None,
            prompt=None,
            transcript_source=None,
            chunk_minutes=cli_chunk_minutes,
        )
        vi.cmd_scan(args, config)
        return seen

    def test_scan_chunks_a_long_video(self, tmp_path, monkeypatch):
        seen = self._scan(monkeypatch, tmp_path)

        assert seen["chunked"] == [vi.TRANSCRIPT_CHUNK_MINUTES_DEFAULT]
        assert not seen["single"]

    def test_per_channel_knob_lowers_the_trigger(self, tmp_path, monkeypatch):
        """The conference-channel case: a dense 42-minute talk must chunk too."""
        seen = self._scan(monkeypatch, tmp_path, channel_extra={"chunk_minutes": 20}, duration="PT42M8S")

        assert seen["chunked"] == [20]

    def test_cli_flag_overrides_the_channel(self, tmp_path, monkeypatch):
        seen = self._scan(monkeypatch, tmp_path, channel_extra={"chunk_minutes": 20}, cli_chunk_minutes=15)

        assert seen["chunked"] == [15]

    def test_short_video_still_goes_single_shot(self, tmp_path, monkeypatch):
        seen = self._scan(monkeypatch, tmp_path, duration="PT20M")

        assert not seen["chunked"]
        assert seen["single"] == [True]

    def test_unknown_duration_fails_safe_to_single_shot(self, tmp_path, monkeypatch):
        """Never guess a chunk layout from a duration we could not parse."""
        seen = self._scan(monkeypatch, tmp_path, duration=None)

        assert not seen["chunked"]
        assert seen["single"] == [True]


class TestScanChunkingRespectsExistingRouting:
    @pytest.mark.parametrize(
        ("source", "captions_first"),
        [("yt-captions", False), ("gemini", True)],
        ids=["yt_captions", "livestream_captions_first"],
    )
    def test_routing_that_owns_ordering_keeps_the_single_shot_path(
        self, tmp_path, monkeypatch, fake_types, source, captions_first
    ):
        """Diverting these into the chunker would bypass decisions made elsewhere.

        `yt-captions` needs no chunking, and a livestream VOD's captions-first
        ordering is owned by `process_transcript` (issue #120) - chunking here
        would spend N Gemini calls against a URI not yet known to be fetchable.
        """
        chunked, single = [], []
        monkeypatch.setattr(vi, "_run_chunked_transcript_url", lambda **kw: chunked.append(kw) or "done")
        monkeypatch.setattr(vi, "process_transcript", lambda *a, **kw: single.append(kw) or ("p", "done"))

        vi._scan_transcribe_one(
            client=object(),
            types=fake_types,
            video={"video_id": "v", "url": "u", "title": "t", "published": "2026-08-06"},
            prompt_text="P",
            model="m",
            channel_dir=tmp_path,
            prefix="p",
            transcript_source=source,
            transcript_timeout_seconds=600,
            livestream_captions_first=captions_first,
            duration_seconds=99999,
            chunk_minutes=50,
        )

        assert not chunked
        assert len(single) == 1


class TestChunkedScanBranchHonorsTheFailureContract:
    """Codex cross-model finding on PR #134.

    `process_transcript` catches its own failures and returns an `error: ...`
    status. `_run_chunked_transcript_url` does not, and the scan's
    `future.result()` is unguarded - so routing long videos through the chunker
    made an uncaught exception able to abort the ENTIRE scan rather than fail
    one video.
    """

    @staticmethod
    def _call(tmp_path, fake_types, transcript_source="gemini"):
        return vi._scan_transcribe_one(
            client=object(),
            types=fake_types,
            video={
                "video_id": "v",
                "url": "https://www.youtube.com/watch?v=v",
                "title": "t",
                "published": "2026-08-12",
            },
            prompt_text="P",
            model="m",
            channel_dir=tmp_path / "demo",
            prefix="p",
            transcript_source=transcript_source,
            transcript_timeout_seconds=600,
            livestream_captions_first=False,
            duration_seconds=99999,
            chunk_minutes=50,
        )

    def test_a_raised_exception_becomes_an_error_status(self, tmp_path, monkeypatch, fake_types):
        def boom(**_kw):
            raise RuntimeError("400 INVALID_ARGUMENT")

        monkeypatch.setattr(vi, "_run_chunked_transcript_url", boom)

        prefix, status = self._call(tmp_path, fake_types)

        assert prefix == "p"
        assert status.startswith("error:"), "an exception here would otherwise abort the whole scan"
        assert "400 INVALID_ARGUMENT" in status

    def test_auto_still_gets_the_captions_failover_after_a_chunked_failure(self, tmp_path, monkeypatch, fake_types):
        """The single-shot path would have tried captions; the chunked path must too."""
        monkeypatch.setattr(vi, "_run_chunked_transcript_url", lambda **_kw: "error: all chunks failed parsing")
        called = {}

        def fake_failover(video, tpath, mpath, prefix, **kw):
            called["reason"] = kw.get("reason")
            return (prefix, "done (captions)")

        monkeypatch.setattr(vi, "_try_captions_transcript", fake_failover)

        _, status = self._call(tmp_path, fake_types, transcript_source="auto")

        assert status == "done (captions)"
        assert "chunked transcript failed" in called["reason"]

    def test_gemini_source_does_not_get_a_failover(self, tmp_path, monkeypatch, fake_types):
        monkeypatch.setattr(vi, "_run_chunked_transcript_url", lambda **_kw: "error: boom")
        monkeypatch.setattr(
            vi, "_try_captions_transcript", lambda *a, **kw: pytest.fail("must not run under transcript_source=gemini")
        )

        _, status = self._call(tmp_path, fake_types, transcript_source="gemini")

        assert status == "error: boom"


class TestResolverIsSharedByAllFourSites:
    """One knob, one meaning (issue #128 in-family review P1).

    An operator who sets `chunk_minutes: 20` on a conference channel must get 20
    from `scan` AND from `process --url --force`, the documented recovery
    command for the exact failure the knob exists for. The manual subparsers'
    old `default=50` masked channel config entirely: the CLI "value" always won
    even when the user never passed the flag.
    """

    def test_non_integer_values_raise_the_documented_valueerror(self):
        for bad in (["a", "list"], {"a": 1}, "20m"):
            with pytest.raises(ValueError):
                resolve_chunk_minutes({"chunk_minutes": bad}, {})


class TestScanSurvivesABadChunkMinutes:
    """A config typo on one channel must not abort the whole scan (P1 #3)."""

    def test_bad_chunk_minutes_skips_the_channel_and_continues(self, tmp_path, monkeypatch, caplog):
        videos = [
            {
                "video_id": "v1",
                "title": "talk",
                "published": "2026-08-06",
                "url": "https://www.youtube.com/watch?v=v1",
            }
        ]
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "T"))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos))
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids, "PT10M"))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {v: {} for v in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _v: False)
        seen = []
        monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: (seen.append(a[2]["video_id"]), ("p", "done"))[1])
        monkeypatch.setattr(vi, "process_transcript", lambda *a, **kw: ("p", "done"))

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {"name": "broken", "url": "u1", "auto_transcript": "all", "chunk_minutes": "20m"},
                {"name": "healthy", "url": "u2", "auto_transcript": "all"},
            ],
        }
        args = SimpleNamespace(
            channel=None,
            since=None,
            dry_run=False,
            force=False,
            model=None,
            prompt=None,
            transcript_source=None,
            chunk_minutes=None,
        )

        with caplog.at_level("ERROR", logger="video_intel"):
            vi.cmd_scan(args, config)

        assert "v1" in seen, "the HEALTHY channel must still be processed"
        errors = [r.getMessage() for r in caplog.records if r.levelno >= 40]
        assert any("chunk_minutes" in m and "broken" in m for m in errors), (
            "the skipped channel must be named in the log"
        )


class TestRuntTailChunksAreFolded:
    """A video one second over the boundary must not produce a 1-second chunk.

    That chunk buys a wasted Gemini call and is then flagged thin, forcing
    transcript_status: partial on a perfectly healthy video - noise poured into
    exactly the bucket the truncation detection exists to clean up.
    """

    def test_one_second_over_the_boundary_folds_into_one_chunk(self):
        chunks = vi._build_transcript_chunks(3001, 50)

        assert chunks == [(0, 3001)], f"expected one folded chunk, got {chunks}"

    def test_the_observed_shapes_fold_too(self):
        # 50m10s and 100m30s were the review's named real cases.
        assert vi._build_transcript_chunks(3010, 50) == [(0, 3010)]
        assert vi._build_transcript_chunks(6030, 50) == [(0, 3000), (3000, 6030)]

    def test_a_substantial_tail_keeps_its_own_chunk(self):
        # 80 minutes at 50-minute chunks: the 30-minute tail is real content.
        assert vi._build_transcript_chunks(4800, 50) == [(0, 3000), (3000, 4800)]

    def test_uniform_multiples_are_untouched(self):
        assert vi._build_transcript_chunks(6000, 50) == [(0, 3000), (3000, 6000)]


class TestChunkMinutesFlagDefaultsAreNone:
    """The subparser default is load-bearing (in-family review P1 #1).

    With `default=TRANSCRIPT_CHUNK_MINUTES_DEFAULT` on the manual subparsers,
    `getattr(args, "chunk_minutes", None)` was never None, so the CLI "value"
    beat channel config even when the user never passed the flag - meaning
    `chunk_minutes: 20` on a channel worked in scan and was ignored by
    `process --url`, the documented recovery command.
    """

    def test_every_chunk_minutes_flag_defaults_to_none(self):
        import re
        from pathlib import Path as _P

        source = _P(vi.__file__).read_text(encoding="utf-8")
        blocks = re.findall(r'"--chunk-minutes",\s*type=int,\s*default=(\S+?),', source)
        assert blocks, "expected --chunk-minutes definitions"
        assert len(blocks) == 3, f"expected the scan/transcript/process flags, found {len(blocks)}"
        assert all(d == "None" for d in blocks), f"a non-None default masks channel config at that site: {blocks}"
