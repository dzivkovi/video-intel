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
    IndexView,
    recall_ceiling,
    timestamp_precision_ceiling,
    unaddressable_hits,
    unreachable_thresholds,
)
from .test_search_quality import GOLDEN_PATH, HARNESS_DEDUP_BY_VIDEO

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

    def test_an_empty_channel_projection_never_manufactures_a_failure(self) -> None:
        gold = _gold(
            [("a", "c1", "00:10", "00:20")],
            channel_coverage={"min_channels": 1, "threshold": 1.0},
        )
        assert unreachable_thresholds(gold, IndexView({"a": [15]}), dedup_by_video=False) == []


@pytest.mark.parametrize("gold", _queries, ids=lambda g: g["id"])
def test_golden_query_is_measurable(gold: dict[str, Any], index_view: IndexView) -> None:
    problems = unreachable_thresholds(gold, index_view, dedup_by_video=HARNESS_DEDUP_BY_VIDEO)
    if problems:
        pytest.fail(
            f"{gold['id']} is UNMEASURABLE — its retrieval-eval result is an instrument\n"
            f"artifact, not a retrieval score. Fix the ruler, not the retriever:\n  - " + "\n  - ".join(problems)
        )
