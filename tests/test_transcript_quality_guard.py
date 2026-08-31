"""Tests for issue #157: silent transcript coverage corruption.

Three corruption shapes found in a 2,049-file corpus forensics sweep
(2026-08-29, seed case uU5Gv2h8-9g): monolithic collapse, blind gaps, and
clock slip. See docs/plans/2026-08-29-001-fix-transcript-quality-guards-plan.md
for the full evidence and design decisions this file locks in.

Sections:
  1. assess_transcript_artifact - the pure assessor, executed directly.
  2. Writer integration - process_transcript (single-shot) and
     _run_chunked_transcript_url, executed for real (stubbed Gemini call
     only), never stubbed at the assessor boundary.
  3. Runt-fold / chunk_minutes boundary cases.
  4. Exit-code integration - the TestCheckerAndWriterAgreeOnPaths shape from
     tests/test_transport_retry_and_partial_exit.py: writer and checker
     paths/values derived independently, then compared.
  5. resolve_mindmap_source containment (both branches).
  6. Writer-status-literal parity for missing_pipeline_artifacts.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel as vi
from video_intel import (
    EXIT_PARTIAL,
    assess_transcript_artifact,
    missing_pipeline_artifacts,
    process_transcript,
    resolve_mindmap_source,
    transcript_quality_flags_are_severe,
)


def _mmss(secs: int) -> str:
    return f"{secs // 60:02d}:{secs % 60:02d}"


def _entries(seconds_in_order: list[int]) -> list[dict]:
    """Dialogue entries in the exact order given - never sorted here, since
    several tests below depend on emission order surviving intact."""
    return [{"start": _mmss(s), "voice": 1, "text": f"line at {s}"} for s in seconds_in_order]


def _dense_entries(span: int, count: int, start: int = 0) -> list[dict]:
    """`count` evenly-spaced entries across [start, start + span] - a stand-in
    for realistic dense dialogue (corpus healthy median: 1.79 entries/min)."""
    if count == 1:
        fractions = [0.0]
    else:
        fractions = [i / (count - 1) for i in range(count)]
    return _entries([start + int(span * f) for f in fractions])


# ---------------------------------------------------------------------------
# 1. assess_transcript_artifact - pure function
# ---------------------------------------------------------------------------


class TestSeedShapeClockSlip:
    """The seed case (uU5Gv2h8-9g): one response's timestamps jumped backward
    ~20 minutes mid-call. merge_transcript_json() sorts by timestamp, so the
    real tail was sorted INTO the middle - an interleave, no text lost, but
    only visible in RAW emission order."""

    def test_backward_jump_of_1200s_is_severe(self):
        # Dense (200s-spaced), so no gap or density flag can fire - the ONLY
        # anomaly is the 1800 -> 600 backward step (a 1200s / 20min
        # regression), in RAW EMISSION ORDER (never sorted before this call).
        raw_order = [0, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 600, 2000, 2100]
        metrics = assess_transcript_artifact(_entries(raw_order), duration_seconds=2200, window=None)

        assert metrics["max_backward_jump_seconds"] == 1200
        assert metrics["severe"] == ["backward_jump_severe"], metrics

    def test_no_text_is_lost_by_this_flag_alone(self):
        """Sanity: the assessor counts entries, it does not discard them -
        flagging is advisory, matching the "no auto-repair" design decision."""
        raw_order = [100, 1700, 600, 1900]
        metrics = assess_transcript_artifact(_entries(raw_order), duration_seconds=2000, window=None)

        assert metrics["dialogue_entries"] == 4

    def test_jump_under_severe_but_over_mild_is_label_only(self):
        # Same dense shape, backward step narrowed to 400s (1800 -> 1400).
        raw_order = [0, 200, 400, 600, 800, 1000, 1200, 1400, 1600, 1800, 1400, 2000, 2100]
        metrics = assess_transcript_artifact(_entries(raw_order), duration_seconds=2200, window=None)

        assert metrics["max_backward_jump_seconds"] == 400
        assert "backward_jump_mild" in metrics["mild"]
        assert "backward_jump_severe" not in metrics["severe"]

    def test_small_jump_under_60s_is_not_flagged_at_all(self):
        # Same dense shape, backward step narrowed to 10s (1000 -> 990).
        raw_order = [0, 200, 400, 600, 800, 1000, 990, 1200, 1400, 1600, 1800, 2000, 2100]
        metrics = assess_transcript_artifact(_entries(raw_order), duration_seconds=2200, window=None)

        assert metrics["max_backward_jump_seconds"] == 10
        assert "backward_jump_mild" not in metrics["mild"]
        assert "backward_jump_severe" not in metrics["severe"]


class TestHollowChunkSevere:
    """A hollow chunk (entries only near the very start and very end) used to
    score ~100% under the old max(ts)-min(ts) span-ratio metric despite an
    empty middle - exactly the shape 12/44 real chunked 60min+ videos hid
    behind a false "ok"."""

    def test_entries_at_0_and_49min_of_a_50min_window_is_severe(self):
        metrics = assess_transcript_artifact(
            _entries([0, 2940]),
            duration_seconds=3000,
            window=(0, 3000),  # 0min and 49min
        )
        assert "blind_gap_severe" in metrics["severe"]
        assert metrics["blind_gap_kind"] == "internal"
        assert metrics["max_blind_gap_seconds"] == 2940
        # Also monolithic (<=3 entries) - both flags legitimately co-occur.
        assert "monolithic_severe" in metrics["severe"]

    def test_a_real_hollow_chunk_with_more_entries_still_catches_the_gap(self):
        """Dense at the edges, empty in the middle: entries > 3 so the
        monolithic entry-count clause cannot be what is firing here - only
        the blind-gap check can catch this shape."""
        edges = _dense_entries(200, 5, start=0) + _dense_entries(200, 5, start=2800)
        metrics = assess_transcript_artifact(edges, duration_seconds=3000, window=(0, 3000))

        assert metrics["dialogue_entries"] == 10
        assert "monolithic_severe" not in metrics["severe"]
        assert "blind_gap_severe" in metrics["severe"]
        assert metrics["blind_gap_kind"] == "internal"


class TestMonolithicCollapse:
    def test_three_entries_for_a_60_minute_window_is_severe(self):
        metrics = assess_transcript_artifact(_entries([60, 1800, 3540]), duration_seconds=3600, window=None)
        assert "monolithic_severe" in metrics["severe"]

    def test_low_density_with_more_than_three_entries_is_severe(self):
        # 4 entries over a 2-hour (7200s) window = 0.033/min < DENSITY_SEVERE_PER_MIN.
        metrics = assess_transcript_artifact(_dense_entries(7200, 4), duration_seconds=7200, window=None)
        assert metrics["dialogue_entries"] == 4
        assert metrics["density_per_min"] < vi.DENSITY_SEVERE_PER_MIN
        assert "monolithic_severe" in metrics["severe"]

    def test_short_clip_with_few_entries_is_not_penalized(self):
        """The known-window > 5min gate: a genuinely short clip with 2 lines
        of dialogue is not evidence of collapse."""
        metrics = assess_transcript_artifact(_entries([5, 20]), duration_seconds=60, window=None)
        assert metrics["severe"] == []


class TestNinetyFivePercentSalvageNeverFalseAlarms:
    """Design decision: 'a monolithic 3-line transcript of a 60-minute video
    is not degraded but real; a 95% salvage still is, and must never
    false-alarm.' Modeled on the documented real case: content covered
    00:08-41:41 of a 42:08 (2528s) video."""

    def test_healthy_body_with_small_edge_gaps_has_no_severe_flags(self):
        entries = _dense_entries(2493, 60, start=8)  # 00:08 .. 41:41
        metrics = assess_transcript_artifact(entries, duration_seconds=2528, window=None)

        assert metrics["severe"] == []
        # Matches the documented real case's coverage (>= 0.99 last-dialogue
        # fraction on the 12 worst REAL corpus files); this synthetic fixture
        # lands a hair under that at ~0.989, which is still "the primary
        # trigger is max blind gap, not coverage" - the point of this test.
        assert metrics["last_dialogue_fraction"] > 0.98


class TestThreeMinuteOutroTailNotFlagged:
    def test_180s_trailing_silence_on_a_healthy_body_is_clean(self):
        entries = _dense_entries(2700, 60, start=0)  # dense across the first 45min
        metrics = assess_transcript_artifact(entries, duration_seconds=2880, window=None)  # 48min total, 180s tail

        assert metrics["max_blind_gap_seconds"] < 600
        assert metrics["severe"] == []
        assert metrics["mild"] == []

    def test_a_much_longer_trailing_silence_is_mild_never_severe(self):
        """Trailing gap >= 600s on an otherwise-healthy body is capped at
        MILD - unlike a leading or internal gap of the same size."""
        entries = _dense_entries(1800, 60, start=0)
        metrics = assess_transcript_artifact(entries, duration_seconds=3000, window=None)  # 1200s trailing silence

        assert metrics["blind_gap_kind"] == "trailing"
        assert "blind_gap_severe" not in metrics["severe"]
        assert "trailing_gap_mild" in metrics["mild"]

    def test_leading_or_internal_gap_of_the_same_size_is_severe(self):
        """The asymmetry is deliberate: only TRAILING gets the mild carve-out."""
        entries = _dense_entries(1800, 60, start=1200)  # 1200s LEADING silence, same magnitude
        metrics = assess_transcript_artifact(entries, duration_seconds=3000, window=None)

        assert metrics["blind_gap_kind"] == "leading"
        assert "blind_gap_severe" in metrics["severe"]


class TestUnknownDurationIsMetricsOnly:
    """No duration, no window: gap and density are unknowable and must never
    manufacture a severe verdict. Only entry count and raw backward jump -
    neither of which needs a duration - are still meaningful."""

    def test_gap_and_density_are_none(self):
        metrics = assess_transcript_artifact(_entries([10, 20, 30]), duration_seconds=None, window=None)

        assert metrics["density_per_min"] is None
        assert metrics["last_dialogue_fraction"] is None
        assert metrics["blind_gap_kind"] is None

    def test_few_entries_does_not_trigger_monolithic_without_a_known_window(self):
        metrics = assess_transcript_artifact(_entries([10, 20]), duration_seconds=None, window=None)
        assert metrics["severe"] == []
        assert metrics["mild"] == []

    def test_backward_jump_still_detected_without_a_duration(self):
        metrics = assess_transcript_artifact(_entries([2000, 500]), duration_seconds=None, window=None)
        assert metrics["max_backward_jump_seconds"] == 1500
        assert "backward_jump_severe" in metrics["severe"]

    def test_never_raises_on_malformed_entries(self):
        # None/missing "start" is skipped outright. A non-empty but
        # unparseable string ("garbage") is NOT excluded here - it is
        # timestamp_to_seconds's own pre-existing, documented contract to
        # fall through to 0 rather than raise (issue #58 Gate 3) - so it
        # still counts as one (zero-second) entry. The guarantee this locks
        # is "never raises", not "silently drops anything unparseable".
        malformed = [{"start": None}, "not a dict", {"no_start_key": True}, {"start": "garbage"}]
        metrics = assess_transcript_artifact(malformed, duration_seconds=None, window=None)
        assert metrics["dialogue_entries"] == 1


class TestZeroZeroSentinelStillAssessedWithKnownDuration:
    """Issue #157 defect #4: the (0, 0) chunk marker used to skip assessment
    entirely, conflating "no clipping" with "no assessment". Locked at the
    assess_transcript_artifact level (window=None IS what a (0, 0) caller
    now passes) and at the _assess_chunk_coverage level (its own sentinel
    branch)."""

    def test_window_none_with_known_duration_assesses_for_real(self):
        metrics = assess_transcript_artifact(_entries([30, 60]), duration_seconds=600, window=None)
        assert "monolithic_severe" in metrics["severe"]

    def test_assess_chunk_coverage_sentinel_branch_assesses_for_real(self):
        status, metrics = vi._assess_chunk_coverage(
            {"transcripts": [{"start": "00:30"}, {"start": "01:00"}]},
            start_secs=0,
            end_secs=0,
            duration_seconds=600,
            chunk_idx=1,
        )
        assert status == "thin"
        assert "monolithic_severe" in metrics["severe"]


# ---------------------------------------------------------------------------
# 2. Writer integration - real process_transcript / _run_chunked_transcript_url
# ---------------------------------------------------------------------------


def _video(video_id: str = "abc12345678") -> dict:
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "A Talk",
        "published": "2026-08-29",
    }


def _fake_gemini_response(prompt_tokens: int = 5000, candidates_tokens: int = 100, finish_reason: str = "STOP"):
    """A real (non-Mock) response shape - log_usage_metadata / hit_output_cap
    read concrete fields off this, and a MagicMock's auto-generated attribute
    chain is not JSON-serializable if it ever lands in a persisted field."""
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=candidates_tokens,
            total_token_count=prompt_tokens + candidates_tokens,
        ),
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


def _stub_single_shot(monkeypatch, response_json: str):
    def fake_call_gemini(_client, _types, _uri, _prompt, _model, **kw):
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(_fake_gemini_response())
        return response_json

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    return MagicMock(), MagicMock()


class TestMonolithicSingleShotSevere:
    def test_single_shot_healthy_parse_with_two_entries_on_a_60min_video_is_flagged(self, tmp_path, monkeypatch):
        payload = json.dumps(
            {
                "transcripts": [
                    {"start": "00:30", "voice": 1, "text": "Hi."},
                    {"start": "10:00", "voice": 1, "text": "Bye."},
                ],
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )
        client, types = _stub_single_shot(monkeypatch, payload)
        video = _video()
        channel_dir = tmp_path / "demo"
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", channel_dir, "2026-08-29-a-talk", duration_seconds=3600
        )

        assert status == "partial (quality guard)"
        meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert meta["transcript_status"] == "partial"
        assert "monolithic_severe" in meta["transcript_quality_flags"]
        # Identity is still stamped on the quality-guard path (issue #66).
        assert meta["video_id"] == video["video_id"]

    def test_healthy_dense_transcript_stays_complete(self, tmp_path, monkeypatch):
        entries = _dense_entries(3540, 90, start=30)
        payload = json.dumps(
            {
                "transcripts": entries,
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )
        client, types = _stub_single_shot(monkeypatch, payload)
        video = _video("def12345678")
        channel_dir = tmp_path / "demo"
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", channel_dir, "2026-08-29-b-talk", duration_seconds=3600
        )

        assert status == "done"
        meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert meta["transcript_status"] == "complete"
        assert meta["transcript_quality_flags"] == []


class TestSalvageNeverFalseAlarms:
    """95% salvage (a real, malformed-JSON recovery) must keep transcript_status
    from TRANSCRIPT_STATUS_TRUNCATED/partial's OWN existing reason - and must
    never ALSO pick up a severe quality flag on a healthy body."""

    def test_dense_salvaged_body_carries_no_severe_flags(self, tmp_path, monkeypatch):
        entries = _dense_entries(2493, 60, start=8)
        truncated_json = (
            '{"transcripts": ' + json.dumps(entries) + ', "screen_content": [{"start": "05:00"'
        )  # unterminated - forces the salvage path
        client, types = _stub_single_shot(monkeypatch, truncated_json)
        video = _video("ghi12345678")
        channel_dir = tmp_path / "demo"
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", channel_dir, "2026-08-29-c-talk", duration_seconds=2528
        )

        assert "salvaged" in status
        meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert meta["transcript_status"] == "partial"  # unchanged reason (issue #128 distinction preserved)
        assert meta["transcript_quality_flags"] == []


class TestSeverityIsOrderIndependentAndMalformedEntriesDegrade:
    """Issue #159 dual-review item 3: `transcript_quality_flags_are_severe`
    must reach the same verdict regardless of where a malformed entry sits
    in the list, and must never raise on one.

    Before the fix, `any(f in _SEVERE_QUALITY_FLAGS for f in flags)` would
    raise `TypeError: unhashable type: 'dict'` the moment it reached an
    unhashable entry - so `[{"x": 1}, "monolithic_severe"]` raised before
    `any()` ever reached the genuine severe string, while the reordered
    `["monolithic_severe", {"x": 1}]` returned True correctly. The verdict
    depended on entry order, and dedupe's own wrapper had to catch the
    TypeError to survive it. The fix filters to string entries before the
    membership test, so no entry shape can raise and order cannot matter."""

    def test_dict_before_severe_string_is_still_severe(self):
        assert transcript_quality_flags_are_severe([{"x": 1}, "monolithic_severe"]) is True

    def test_severe_string_before_dict_is_still_severe(self):
        assert transcript_quality_flags_are_severe(["monolithic_severe", {"x": 1}]) is True

    def test_dict_only_entries_are_clean(self):
        assert transcript_quality_flags_are_severe([{"x": 1}, {"y": 2}]) is False

    def test_none_entry_alongside_a_severe_string_is_still_severe(self):
        assert transcript_quality_flags_are_severe([None, "monolithic_severe"]) is True
        assert transcript_quality_flags_are_severe(["monolithic_severe", None]) is True

    def test_nested_list_entry_does_not_crash(self):
        assert transcript_quality_flags_are_severe([["nested"], "monolithic_severe"]) is True
        assert transcript_quality_flags_are_severe([["nested"], "density_mild"]) is False

    def test_mild_string_alongside_malformed_entries_stays_not_severe(self):
        assert transcript_quality_flags_are_severe([{"x": 1}, "density_mild", None]) is False

    def test_transcript_quality_severe_from_meta_also_inherits_the_hardened_helper(self, tmp_path):
        """The standards-reviewer note: `_transcript_quality_severe_from_meta`
        does its own read (no wrapper-level list coercion like dedupe's), so
        it must be protected purely by the shared helper's own hardening."""
        meta_path = tmp_path / "video.meta.json"
        meta_path.write_text(
            json.dumps({"transcript_quality_flags": [{"x": 1}, "monolithic_severe"]}),
            encoding="utf-8",
        )
        assert vi._transcript_quality_severe_from_meta(meta_path) is True


class TestChunkedWriterCatchesCrossChunkBlindGap:
    """The 12/44 real corpus case: every chunk individually 'ok', but the
    STITCHED transcript has a >=10min hole spanning (or hiding inside) a
    chunk boundary. Only a post-merge, whole-transcript assessment catches
    this - which is exactly what issue #157 adds to _run_chunked_transcript_url."""

    def test_gap_spanning_a_chunk_boundary_is_caught_post_merge(self, tmp_path, monkeypatch):
        # Chunk 1 (0-1800s): dense in the first half, nothing in the second.
        # Chunk 2 (1800-3600s): nothing in the first half, dense in the second.
        # Each chunk alone might read as merely "sparse"; the STITCHED gap
        # (spanning roughly 900s-2700s = 1800s) must be caught post-merge.
        chunk1 = json.dumps(
            {
                "transcripts": _dense_entries(800, 20, start=10),
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )
        chunk2 = json.dumps(
            {
                "transcripts": _dense_entries(800, 20, start=2790),
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )
        responses = [chunk1, chunk2]

        def fake_call_gemini(_client, _types, _uri, _prompt, _model, **kw):
            idx = fake_call_gemini.n
            fake_call_gemini.n += 1
            on_response = kw.get("on_response")
            if on_response is not None:
                on_response(_fake_gemini_response())
            return responses[idx]

        fake_call_gemini.n = 0
        monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)

        channel_dir = tmp_path / "demo"
        video = _video("jkl12345678")
        status = vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix="2026-08-29-d-talk",
            chunks=[(0, 1800), (1800, 3600)],
            duration_seconds=3600,
            chunk_minutes=30,
            force=False,
        )

        meta = json.loads((channel_dir / "2026-08-29-d-talk.meta.json").read_text(encoding="utf-8"))
        assert status == "partial"
        assert meta["transcript_status"] == "partial"
        assert "blind_gap_severe" in meta["transcript_quality_flags"]
        assert meta["transcript_max_blind_gap_seconds"] >= 600


# ---------------------------------------------------------------------------
# 3. Runt-fold / chunk_minutes boundary cases
# ---------------------------------------------------------------------------


class TestRuntFoldBoundaries:
    def test_3001s_at_50min_folds(self):
        assert vi._build_transcript_chunks(3001, 50) == [(0, 3001)]

    def test_3300s_at_50min_does_not_fold(self):
        # Tail = 300s, above RUNT_FOLD_MAX_SECONDS (120) - stays two chunks.
        assert vi._build_transcript_chunks(3300, 50) == [(0, 3000), (3000, 3300)]

    def test_3090s_at_30min_chunks_in_two(self):
        # The seed video's exact duration, at the NEW default chunk size:
        # tail = 3090 - 1800 = 1290s, well above the fold floor.
        assert vi._build_transcript_chunks(3090, 30) == [(0, 1800), (1800, 3090)]

    def test_seed_duration_no_longer_folds_to_a_single_call_at_the_new_default(self):
        chunks = vi._build_transcript_chunks(3090, vi.TRANSCRIPT_CHUNK_MINUTES_DEFAULT)
        assert vi.TRANSCRIPT_CHUNK_MINUTES_DEFAULT == 30
        assert len(chunks) == 2


# ---------------------------------------------------------------------------
# 4. Exit-code integration - writer and checker paths derived independently
# ---------------------------------------------------------------------------


class TestExitCodeSevereVsMild:
    """Mirrors tests/test_transport_retry_and_partial_exit.py::
    TestCheckerAndWriterAgreeOnPaths: call the REAL writer, read back what it
    actually persisted, build the step dict from that, and only THEN check
    missing_pipeline_artifacts - never hand the path/flags to a stub."""

    def test_severe_quality_flag_is_a_gap(self, tmp_path, monkeypatch):
        payload = json.dumps(
            {
                "transcripts": [{"start": "00:30", "voice": 1, "text": "Hi."}],
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )
        client, types = _stub_single_shot(monkeypatch, payload)
        video = _video("mno12345678")
        channel_dir = tmp_path / "demo"
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", channel_dir, "2026-08-29-e-talk", duration_seconds=3600
        )
        transcript_path = channel_dir / f"{prefix}.transcript.md"
        meta_path = channel_dir / f"{prefix}.meta.json"
        assert transcript_path.exists() and transcript_path.stat().st_size > 0

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        quality_severe = transcript_quality_flags_are_severe(meta.get("transcript_quality_flags"))
        assert quality_severe is True

        steps = [
            {
                "label": "transcript",
                "requested": True,
                "status": status,
                "path": transcript_path,
                "quality_severe": quality_severe,
            }
        ]
        assert missing_pipeline_artifacts(steps) == ["transcript"]

    def test_mild_only_quality_flag_is_not_a_gap(self, tmp_path, monkeypatch):
        # 10 entries over a 3600s (60min) window: density 0.167/min sits in
        # the density_mild band (< 0.25, but not < 0.1 severe), and 9 evenly
        # spaced 400s gaps stay well under BLIND_GAP_SEVERE_SECONDS (600) -
        # no severe flag from either axis.
        entries = _dense_entries(3600, 10, start=0)
        payload = json.dumps({"transcripts": entries, "screen_content": [], "speakers": [{"voice": 1, "name": "H"}]})
        client, types = _stub_single_shot(monkeypatch, payload)
        video = _video("pqr12345678")
        channel_dir = tmp_path / "demo"
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", channel_dir, "2026-08-29-f-talk", duration_seconds=3600
        )
        transcript_path = channel_dir / f"{prefix}.transcript.md"
        meta_path = channel_dir / f"{prefix}.meta.json"

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        quality_severe = transcript_quality_flags_are_severe(meta.get("transcript_quality_flags"))

        steps = [
            {
                "label": "transcript",
                "requested": True,
                "status": status,
                "path": transcript_path,
                "quality_severe": quality_severe,
            }
        ]
        assert missing_pipeline_artifacts(steps) == []

    def test_quality_severe_key_is_optional_and_defaults_false(self, tmp_path):
        """Every OTHER step (mindmap, concepts) never sets this key."""
        art = tmp_path / "a.mindmap.md"
        art.write_text("content", encoding="utf-8")
        steps = [{"label": "mindmap", "requested": True, "status": "done", "path": art}]
        assert missing_pipeline_artifacts(steps) == []

    def test_exit_partial_constant_is_still_3(self):
        assert EXIT_PARTIAL == 3


# ---------------------------------------------------------------------------
# 5. resolve_mindmap_source containment (both branches)
# ---------------------------------------------------------------------------


class TestResolverContainment:
    def test_auto_treats_a_severe_transcript_as_unavailable(self):
        assert resolve_mindmap_source({}, transcript_available=True, transcript_severe=True) == "video", (
            "auto must fall back to video rather than feed a known-corrupt transcript into mindmap generation"
        )

    def test_auto_still_uses_a_healthy_transcript(self):
        assert resolve_mindmap_source({}, transcript_available=True, transcript_severe=False) == "transcript"

    def test_explicit_transcript_is_honored_even_when_severe(self):
        assert (
            resolve_mindmap_source({"mindmap_source": "transcript"}, transcript_available=True, transcript_severe=True)
            == "transcript"
        ), "the operator asked for transcript by name - severity does not override an explicit choice"

    def test_default_parameter_preserves_pre_157_behavior(self):
        """Every call site not yet updated to pass transcript_severe keeps
        its exact prior behavior (default False)."""
        assert resolve_mindmap_source({}, transcript_available=True) == "transcript"

    def test_video_and_none_sources_are_unaffected_by_severity(self):
        assert (
            resolve_mindmap_source({"mindmap_source": "video"}, transcript_available=True, transcript_severe=True)
            == "video"
        )
        assert (
            resolve_mindmap_source({"mindmap_source": "none"}, transcript_available=True, transcript_severe=True)
            == "skip"
        )


# ---------------------------------------------------------------------------
# 6. Writer-status-literal parity
# ---------------------------------------------------------------------------


class TestWriterStatusLiteralsParametrized:
    """The reader (missing_pipeline_artifacts) must accept the union of every
    literal a healthy-or-designed-degraded writer can return, including the
    new "partial (quality guard)" single-shot literal - none of these start
    with "error", and quality_severe (not the status string) is what decides
    a genuinely severe case."""

    @pytest.mark.parametrize(
        "status",
        [
            "done",
            "ok",
            "complete",
            "partial",
            "partial (quality guard)",
            "truncated_output",
            "thin",
            "skipped (exists)",
        ],
    )
    def test_non_severe_degraded_or_healthy_statuses_are_not_gaps(self, tmp_path, status):
        art = tmp_path / "a.transcript.md"
        art.write_text("real content", encoding="utf-8")
        steps = [{"label": "transcript", "requested": True, "status": status, "path": art, "quality_severe": False}]
        assert missing_pipeline_artifacts(steps) == []

    @pytest.mark.parametrize("status", ["done", "ok", "complete", "partial", "partial (quality guard)"])
    def test_quality_severe_true_is_always_a_gap_regardless_of_status_text(self, tmp_path, status):
        art = tmp_path / "a.transcript.md"
        art.write_text("real content", encoding="utf-8")
        steps = [{"label": "transcript", "requested": True, "status": status, "path": art, "quality_severe": True}]
        assert missing_pipeline_artifacts(steps) == ["transcript"]
