"""Unit tests for metric-class contracts (not per-query behavior).

Per-query scoring is tested in `test_search_quality.py`. This module
locks contracts that the parametrized harness can only verify
indirectly — in particular, which metrics are gating vs. informational.
"""

from __future__ import annotations

import pytest
from deepeval.test_case import LLMTestCase

from .metrics import (
    ChannelCoverageMetric,
    MRRMetric,
    RecallAtKMetric,
    TimestampPrecisionMetric,
    distinct_videos_in_order,
)


def _case(video_ids: list[str], expected_ids: list[str], timestamps: list[int] | None = None) -> LLMTestCase:
    return LLMTestCase(
        input="q",
        actual_output="o",
        additional_metadata={
            "retrieved_video_ids": video_ids,
            "retrieved_channels": ["ch"] * len(video_ids),
            "retrieved_timestamps": timestamps or [0] * len(video_ids),
            "expected_hits": [
                {"video_id": v, "channel": "ch", "timestamp_range": ["00:00", "00:10"]} for v in expected_ids
            ],
        },
    )


def test_gating_contract() -> None:
    """Only MRR is non-gating. A stray flip would silently start failing
    tests on an informational signal — guard the contract explicitly."""
    assert RecallAtKMetric(k=10, threshold=0.5).gating is True
    assert ChannelCoverageMetric(min_channels=1, threshold=0.5).gating is True
    assert TimestampPrecisionMetric(tolerance_sec=30, threshold=0.5).gating is True
    assert MRRMetric(threshold=0.25).gating is False


class TestDistinctVideoSemantics:
    """`k` and `rank` count VIDEOS, not array positions (issue #190).

    The harness now receives every retrieved window inside a video, so an
    array-position reading of `k` would shrink recall as a side effect of
    seeing MORE evidence. On a one-chunk-per-video list the generalization is
    the identity, which is what keeps every historic number comparable.
    """

    def test_distinct_videos_in_order_is_identity_on_deduped_input(self) -> None:
        assert distinct_videos_in_order(["a", "b", "c"]) == ["a", "b", "c"]

    def test_distinct_videos_in_order_collapses_runs_keeping_first_appearance(self) -> None:
        assert distinct_videos_in_order(["a", "a", "b", "a", "c", "b"]) == ["a", "b", "c"]

    def test_recall_is_unchanged_on_deduped_input(self) -> None:
        m = RecallAtKMetric(k=2, threshold=0.5)
        assert m.measure(_case(["a", "b", "c"], ["b"])) == 1.0
        assert m.measure(_case(["x", "y", "b"], ["b"])) == 0.0

    def test_repeated_video_chunks_do_not_consume_recall_slots(self) -> None:
        """The regression the generalization exists to prevent: three chunks of
        video `a` must not push `b` out of top-2."""
        m = RecallAtKMetric(k=2, threshold=0.5)
        assert m.measure(_case(["a", "a", "a", "b"], ["b"])) == 1.0

    def test_recall_with_k_beyond_the_result_list_degrades_not_crashes(self) -> None:
        m = RecallAtKMetric(k=50, threshold=0.5)
        assert m.measure(_case(["a"], ["a", "b"])) == 0.5

    def test_recall_vacuous_pass_on_empty_expectations(self) -> None:
        m = RecallAtKMetric(k=5, threshold=0.9)
        assert m.measure(_case(["a"], [])) == 1.0
        assert m.is_successful() is True

    def test_recall_rejects_non_positive_k_at_construction(self) -> None:
        for bad in (0, -1):
            with pytest.raises(ValueError, match="k >= 1"):
                RecallAtKMetric(k=bad, threshold=0.5)

    def test_mrr_ranks_videos_so_repeats_do_not_deflate_the_score(self) -> None:
        m = MRRMetric(threshold=0.25)
        assert m.measure(_case(["a", "a", "a", "b"], ["b"])) == 0.5
        assert m.measure(_case(["a", "b"], ["b"])) == 0.5

    def test_mrr_is_unchanged_on_deduped_input(self) -> None:
        m = MRRMetric(threshold=0.25)
        assert m.measure(_case(["a", "b", "c"], ["c"])) == pytest.approx(1 / 3)

    def test_timestamp_precision_can_satisfy_two_windows_in_one_video(self) -> None:
        """The #190 defect, stated as a metric-level fact: with one chunk per
        video only one of two windows is reachable; with both chunks present
        the query can score 1.0."""
        expected = [
            {"video_id": "a", "channel": "ch", "timestamp_range": ["00:10", "00:20"]},
            {"video_id": "a", "channel": "ch", "timestamp_range": ["10:00", "10:10"]},
        ]
        one_chunk = LLMTestCase(
            input="q",
            actual_output="o",
            additional_metadata={
                "retrieved_video_ids": ["a"],
                "retrieved_channels": ["ch"],
                "retrieved_timestamps": [15],
                "expected_hits": expected,
            },
        )
        both_chunks = LLMTestCase(
            input="q",
            actual_output="o",
            additional_metadata={
                "retrieved_video_ids": ["a", "a"],
                "retrieved_channels": ["ch", "ch"],
                "retrieved_timestamps": [15, 605],
                "expected_hits": expected,
            },
        )
        m = TimestampPrecisionMetric(tolerance_sec=5, threshold=0.8)
        assert m.measure(one_chunk) == 0.5
        assert m.measure(both_chunks) == 1.0

    def test_channel_coverage_is_untouched_by_chunk_multiplicity(self) -> None:
        m = ChannelCoverageMetric(min_channels=1, threshold=1.0)
        assert m.measure(_case(["a", "a", "a"], ["a"])) == 1.0
