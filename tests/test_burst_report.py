"""Tests for scripts/burst_report.py (issue #103).

Contract: a burst is a rate jump against the concept's OWN baseline (a
uniform stream yields none), bursts carry a start date / intensity /
rising-or-cooled status, and sub-threshold concepts are skipped.
"""

from __future__ import annotations

import datetime as dt

from scripts.burst_report import (
    Event,
    detect_bursts,
    kleinberg_burst_spans,
    render_report,
)


def _events(dates: list[str], source: str = "chan") -> list[Event]:
    return [
        Event(dt.date.fromisoformat(d), source, f"video {i}", f"https://www.youtube.com/watch?v=v{i}")
        for i, d in enumerate(dates)
    ]


UNIFORM = [f"2026-{m:02d}-01" for m in range(1, 7)] + [f"2026-{m:02d}-15" for m in range(1, 7)]
# monthly baseline, then a pile-up in the final week
CLUSTERED = [
    "2026-01-01",
    "2026-02-01",
    "2026-03-01",
    "2026-04-01",
    "2026-05-01",
    "2026-06-24",
    "2026-06-25",
    "2026-06-26",
    "2026-06-27",
    "2026-06-28",
]


class TestDetection:
    def test_uniform_stream_yields_no_burst(self):
        events = {"c": _events(sorted(UNIFORM))}
        assert detect_bursts(events, dt.date(2026, 7, 1)) == []

    def test_clustered_tail_yields_rising_burst_with_correct_start(self):
        events = {"c": _events(CLUSTERED)}
        bursts = detect_bursts(events, dt.date(2026, 7, 1))
        assert len(bursts) == 1
        b = bursts[0]
        assert b.start >= dt.date(2026, 6, 24)
        assert b.end == dt.date(2026, 6, 28)
        assert b.rising is True
        assert b.weight > 0
        assert b.first_event.date == b.start

    def test_old_cluster_is_cooled_not_rising(self):
        # same shape, but the corpus continued for months after the burst
        events = {"c": _events(CLUSTERED)}
        bursts = detect_bursts(events, dt.date(2026, 12, 1))
        assert len(bursts) == 1
        assert bursts[0].rising is False

    def test_min_events_filter_skips_thin_concepts(self):
        events = {"c": _events(CLUSTERED[:5])}
        assert detect_bursts(events, dt.date(2026, 7, 1), min_events=6) == []

    def test_same_day_events_do_not_crash(self):
        events = {"c": _events(["2026-01-01"] * 4 + ["2026-06-01", "2026-06-01", "2026-06-02", "2026-06-02"])}
        bursts = detect_bursts(events, dt.date(2026, 6, 3))
        assert isinstance(bursts, list)  # no ZeroDivisionError / math domain error

    def test_rising_bursts_sort_before_cooled(self):
        # two concepts through the real pipeline: one burst cooled long ago,
        # one still rising - the rising one must come first regardless of the
        # cooled one's higher recency-free weight (no sort-key re-implementation
        # here; that made the old version of this test tautological)
        old_cluster = ["2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01", "2025-05-01"] + [
            f"2025-06-{d:02d}" for d in range(1, 6)
        ]
        events = {"cooled": _events(old_cluster), "rising": _events(CLUSTERED)}
        detected = detect_bursts(events, dt.date(2026, 7, 1))
        assert [b.concept_id for b in detected] == ["rising", "cooled"]
        assert detected[0].rising and not detected[1].rising


class TestViterbi:
    def test_no_gaps_returns_empty(self):
        assert kleinberg_burst_spans([], 1, 2.0, 1.0) == []

    def test_burst_span_indices_cover_short_gaps(self):
        # 4 long gaps (30d) then 4 short gaps (1d): burst should cover the tail
        gaps = [30.0] * 4 + [1.0] * 4
        spans = kleinberg_burst_spans(gaps, 9, 2.0, 1.0)
        assert len(spans) == 1
        i, j, weight = spans[0]
        assert i >= 4
        assert j == 7
        assert weight > 0

    def test_mid_sequence_burst_exits_back_to_baseline(self):
        # burst in the MIDDLE: exercises the state-1 -> state-0 exit transition
        # in the backtrace, which tail-anchored fixtures never touch
        gaps = [30.0] * 3 + [1.0] * 5 + [30.0] * 3
        spans = kleinberg_burst_spans(gaps, 12, 2.0, 1.0)
        assert len(spans) == 1
        i, j, _ = spans[0]
        assert (i, j) == (3, 7)


class TestRender:
    def test_report_lists_burst_with_status_and_first_coverer_link(self):
        events = {"c": _events(CLUSTERED)}
        bursts = detect_bursts(events, dt.date(2026, 7, 1))
        report = render_report(bursts, dt.date(2026, 7, 1), {"min_events": 6, "s": 2.0, "gamma": 1.0}, top=20)
        assert "Bursting now (1)" in report
        assert "still rising" in report
        assert "began 2026-06-2" in report
        assert "https://www.youtube.com/watch?v=" in report
        assert "lead for inspection, not a verdict" in report

    def test_report_shows_corpus_volume_confounder_table(self):
        events = {"c": _events(CLUSTERED)}
        bursts = detect_bursts(events, dt.date(2026, 7, 1))
        report = render_report(
            bursts, dt.date(2026, 7, 1), {}, top=20, monthly_volume=[("2026-05", 40), ("2026-06", 90)]
        )
        assert "Corpus volume context" in report
        assert "| 2026-06 | 90 |" in report
        assert "check whether its start date coincides with a volume surge" in report

    def test_report_discloses_dropped_undated_rows_and_intensity_caveat(self):
        events = {"c": _events(CLUSTERED)}
        bursts = detect_bursts(events, dt.date(2026, 7, 1))
        report = render_report(bursts, dt.date(2026, 7, 1), {}, top=20, dropped_undated=3)
        assert "3 concept-video rows were excluded for missing publish dates" in report
        assert "NOT comparable across concepts" in report

    def test_report_caps_listing_and_says_so(self):
        events = {f"c{k}": _events(CLUSTERED, source=f"s{k}") for k in range(5)}
        bursts = detect_bursts(events, dt.date(2026, 12, 1))
        assert len(bursts) == 5
        report = render_report(bursts, dt.date(2026, 12, 1), {}, top=2)
        assert "more cooled bursts" in report
        # a top-budget that empties the cooled listing must not render "(none)"
        cooled_section = report.split("## Recent bursts, cooled")[1]
        assert "(none)" not in cooled_section
