"""Measurability audit — one test per golden query, no Voyage spend.

This suite answers a question the retrieval eval structurally cannot: is the
ruler intact? A failure here means a golden query's gating threshold cannot be
reached by ANY retriever, so its result in `test_search_quality.py` is not a
retrieval measurement and must not be read as one (issue #190).

Keeping the two suites apart is the point. Folding a broken ruler mark into the
retrieval score is exactly how the historic N/25 baseline came to contain
defects nobody could see for a year.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from .instrument import (
    GOLDEN_PATH,
    HARNESS_DEDUP_BY_VIDEO,
    IndexView,
    malformed_dimensions,
    recall_ceiling,
    timestamp_precision_ceiling,
    unaddressable_hits,
    unreachable_thresholds,
)

_queries: list[dict[str, Any]] = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))["queries"]


def _gold(hits: list[tuple[str, str, str, str]], **dims: Any) -> dict[str, Any]:
    return {
        "id": "QXX",
        "expected_hits": [{"video_id": v, "channel": c, "timestamp_range": [s, e]} for v, c, s, e in hits],
        "dimensions": dims,
    }


class TestCeilingMath:
    def test_three_windows_in_one_video_cap_timestamp_precision_at_a_third(self) -> None:
        gold = _gold([("a", "c1", "00:10", "00:20"), ("a", "c1", "05:00", "05:10"), ("a", "c1", "09:00", "09:10")])
        assert timestamp_precision_ceiling(gold, dedup_by_video=True) == pytest.approx(1 / 3)
        assert timestamp_precision_ceiling(gold, dedup_by_video=False) == 1.0

    def test_one_window_per_video_is_never_capped(self) -> None:
        gold = _gold([("a", "c1", "00:10", "00:20"), ("b", "c2", "00:10", "00:20")])
        assert timestamp_precision_ceiling(gold, dedup_by_video=True) == 1.0

    def test_recall_ceiling_tracks_which_expected_videos_the_index_holds(self) -> None:
        gold = _gold([("a", "c1", "00:10", "00:20"), ("gone", "c2", "00:10", "00:20")])
        view = IndexView({"a": [15]})
        assert recall_ceiling(gold, view) == 0.5


class TestUnaddressableHits:
    def test_a_missing_video_is_named(self) -> None:
        gold = _gold(
            [("gone", "c1", "00:10", "00:20")],
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.6},
        )
        out = unaddressable_hits(gold, IndexView({"a": [15]}))
        assert len(out) == 1 and "gone" in out[0]["reason"]

    def test_a_present_video_with_no_chunk_in_the_window_is_named(self) -> None:
        gold = _gold(
            [("a", "c1", "10:00", "10:10")],
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.6},
        )
        out = unaddressable_hits(gold, IndexView({"a": [15, 40]}))
        assert len(out) == 1 and "no indexed chunk" in out[0]["reason"]

    def test_tolerance_widens_the_window(self) -> None:
        gold = _gold(
            [("a", "c1", "10:00", "10:10")],
            timestamp_precision={"tolerance_sec": 60, "threshold": 0.6},
        )
        # 9:15 is outside 10:00-10:10 but inside +/- 60s.
        assert unaddressable_hits(gold, IndexView({"a": [555]})) == []


class TestUnreachableThresholds:
    def test_a_fully_addressable_query_reports_no_problem(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20"), ("b", "c2", "00:10", "00:20")],
            recall_at_k={"k": 10, "threshold": 0.75},
            channel_coverage={"min_channels": 2, "threshold": 0.6},
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.66},
        )
        view = IndexView({"a": [15], "b": [15]})
        assert unreachable_thresholds(gold, view, dedup_by_video=False) == []

    def test_the_dedup_cap_is_reported_only_while_dedup_is_on(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20"), ("a", "c1", "05:00", "05:10")],
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.8},
        )
        view = IndexView({"a": [15, 305]})
        on = unreachable_thresholds(gold, view, dedup_by_video=True)
        assert len(on) == 1 and "dedup_by_video=True" in on[0]
        assert unreachable_thresholds(gold, view, dedup_by_video=False) == []

    def test_a_dead_video_id_is_reported_as_a_recall_ceiling(self) -> None:
        gold = _gold(
            [("gone", "c1", "00:10", "00:20")],
            recall_at_k={"k": 5, "threshold": 0.6},
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.66},
        )
        problems = unreachable_thresholds(gold, IndexView({"a": [15]}), dedup_by_video=False)
        assert any("recall_at_k" in p and "gone" in p for p in problems)

    def test_an_unreachable_channel_is_reported(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20"), ("gone", "c2", "00:10", "00:20")],
            channel_coverage={"min_channels": 2, "threshold": 1.0},
        )
        view = IndexView({"a": [15]}, channels=frozenset({"c1"}))
        problems = unreachable_thresholds(gold, view, dedup_by_video=False)
        assert any("channel_coverage" in p for p in problems)

    def test_a_dead_video_does_not_make_its_channel_unreachable(self) -> None:
        """ChannelCoverage is satisfied by ANY video from an expected channel,
        so a re-uploaded video must not be reported as a channel-level defect -
        that would be the audit crying wolf on a healthy channel."""
        gold = _gold(
            [("a", "c1", "00:10", "00:20"), ("gone", "c2", "00:10", "00:20")],
            channel_coverage={"min_channels": 2, "threshold": 1.0},
        )
        view = IndexView({"a": [15], "other": [15]}, channels=frozenset({"c1", "c2"}))
        assert unreachable_thresholds(gold, view, dedup_by_video=False) == []

    def test_no_channel_projection_never_manufactures_a_failure(self) -> None:
        """`channels=None` means the caller supplied no projection, so no
        channel judgment is possible and nothing may be reported."""
        gold = _gold(
            [("a", "c1", "00:10", "00:20")],
            channel_coverage={"min_channels": 1, "threshold": 1.0},
        )
        assert unreachable_thresholds(gold, IndexView({"a": [15]}), dedup_by_video=False) == []

    def test_a_projected_but_empty_channel_set_is_a_real_failure(self) -> None:
        """An EMPTY projection is channel data actually lost, and must NOT be
        confused with an absent projection - conflating them makes real data
        loss silently false-pass the audit."""
        gold = _gold(
            [("a", "c1", "00:10", "00:20")],
            channel_coverage={"min_channels": 1, "threshold": 1.0},
        )
        view = IndexView({"a": [15]}, channels=frozenset())
        assert any("channel_coverage" in p for p in unreachable_thresholds(gold, view, dedup_by_video=False))


class TestCeilingsAgreeWithTheMetricsTheyAudit:
    """A ceiling that disagrees with the metric is worse than no ceiling: it
    produces both false-measurable and false-unmeasurable verdicts."""

    def test_overlapping_windows_in_one_video_are_not_independent(self) -> None:
        """`TimestampPrecisionMetric` lets ONE chunk satisfy every window it
        falls inside, so two overlapping windows in one video are both
        reachable even under dedup. Assuming one-window-per-video would
        under-report the ceiling and cry wolf on a measurable query."""
        gold = _gold(
            [("a", "c1", "00:24", "00:56"), ("a", "c1", "00:39", "01:11"), ("b", "c2", "00:10", "00:20")],
            timestamp_precision={"tolerance_sec": 0, "threshold": 0.9},
        )
        # A chunk at 00:45 satisfies both of a's windows; b contributes one more.
        assert timestamp_precision_ceiling(gold, dedup_by_video=True) == 1.0

    def test_disjoint_windows_in_one_video_are_still_capped(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20"), ("a", "c1", "10:00", "10:10")],
            timestamp_precision={"tolerance_sec": 0, "threshold": 0.9},
        )
        assert timestamp_precision_ceiling(gold, dedup_by_video=True) == 0.5

    def test_tolerance_can_make_two_windows_overlap(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20"), ("a", "c1", "00:40", "00:50")],
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.9},
        )
        # +/-30s makes the two windows overlap, so one chunk reaches both.
        assert timestamp_precision_ceiling(gold, dedup_by_video=True) == 1.0

    def test_recall_ceiling_respects_the_querys_own_k(self) -> None:
        """`RecallAtKMetric` only inspects the top-k VIDEOS, so a query
        expecting more distinct videos than its k is capped at k/expected even
        against a perfect index."""
        gold = _gold(
            [("a", "c1", "00:10", "00:20"), ("b", "c2", "00:10", "00:20")],
            recall_at_k={"k": 1, "threshold": 0.75},
        )
        view = IndexView({"a": [15], "b": [15]}, channels=frozenset({"c1", "c2"}))
        assert recall_ceiling(gold, view) == 0.5
        problems = unreachable_thresholds(gold, view, dedup_by_video=False)
        assert any("recall_at_k" in p and "k=1" in p for p in problems)

    def test_channel_coverage_cannot_exceed_the_videos_the_harness_returns(self) -> None:
        hits = [(f"v{i}", f"c{i}", "00:10", "00:20") for i in range(12)]
        gold = _gold(
            hits,
            recall_at_k={"k": 5, "threshold": 0.1},
            channel_coverage={"min_channels": 12, "threshold": 1.0},
        )
        view = IndexView({v: [15] for v, *_ in hits}, channels=frozenset(c for _, c, *_ in hits))
        # harness_limit is max(k, 10) = 10, so 12 distinct channels is unreachable.
        assert any("channel_coverage" in p for p in unreachable_thresholds(gold, view, dedup_by_video=False))


class TestTheIndexCeilingBranchCanActuallyFail:
    """Regression guard: a mutation test showed the timestamp_precision
    INDEX-ceiling branch had no test able to fail it - Q02's live red was
    over-determined by the recall branch, so neutering this branch stayed
    green. The case with zero live coverage is a PRESENT video holding no chunk
    anywhere near an expected window."""

    def test_a_present_video_with_no_chunk_near_the_window_is_reported(self) -> None:
        gold = _gold(
            [("a", "c1", "10:00", "10:10"), ("a", "c1", "20:00", "20:10")],
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.9},
        )
        # `a` is in the index, but only with chunks at the very start.
        view = IndexView({"a": [0, 15, 40]}, channels=frozenset({"c1"}))
        problems = unreachable_thresholds(gold, view, dedup_by_video=False)
        assert any("timestamp_precision" in p and "no indexed chunk" in p for p in problems), problems
        # And no recall problem masks it - the video IS present.
        assert not any("recall_at_k" in p for p in problems)


class TestMalformedDimensions:
    def test_a_misspelled_dimension_is_reported_not_ignored(self) -> None:
        """`_build_metrics` reads dimensions with `.get`, so a typo silently
        drops a GATING metric and the query passes for an invisible reason."""
        gold = _gold([("a", "c1", "00:10", "00:20")], timestamp_precison={"tolerance_sec": 30, "threshold": 0.6})
        assert any("timestamp_precison" in p for p in malformed_dimensions(gold))

    def test_position_diversity_is_known_and_not_reported(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20")], position_diversity={"min_distinct_positions": 2, "threshold": 1.0}
        )
        assert malformed_dimensions(gold) == []

    def test_out_of_range_threshold_is_reported(self) -> None:
        gold = _gold([("a", "c1", "00:10", "00:20")], timestamp_precision={"tolerance_sec": 30, "threshold": -1})
        assert any("out-of-range" in p for p in malformed_dimensions(gold))

    def test_non_positive_k_is_reported(self) -> None:
        gold = _gold([("a", "c1", "00:10", "00:20")], recall_at_k={"k": 0, "threshold": 0.5})
        assert any("recall_at_k.k" in p for p in malformed_dimensions(gold))

    def test_non_positive_min_channels_is_reported(self) -> None:
        gold = _gold([("a", "c1", "00:10", "00:20")], channel_coverage={"min_channels": 0, "threshold": 0.5})
        assert any("min_channels" in p for p in malformed_dimensions(gold))

    def test_a_healthy_query_reports_nothing(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20")],
            recall_at_k={"k": 5, "threshold": 0.6},
            channel_coverage={"min_channels": 1, "threshold": 1.0},
            timestamp_precision={"tolerance_sec": 30, "threshold": 0.66},
        )
        assert malformed_dimensions(gold) == []


@pytest.mark.parametrize("gold", _queries, ids=lambda g: g["id"])
def test_golden_query_is_measurable(gold: dict[str, Any], index_view: IndexView) -> None:
    problems = unreachable_thresholds(gold, index_view, dedup_by_video=HARNESS_DEDUP_BY_VIDEO)
    if problems:
        pytest.fail(
            f"{gold['id']} is UNMEASURABLE — its retrieval-eval result is an instrument\n"
            f"artifact, not a retrieval score. Fix the ruler, not the retriever:\n  - " + "\n  - ".join(problems)
        )
