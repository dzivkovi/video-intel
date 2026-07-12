"""Tests for scripts/lead_lag_report.py (issue #93).

The core contract under test: coverage correction. A deep-backfill creator
whose first mention predates everyone else's coverage window must NOT be
credited as a leader, because nobody else could have been observed covering
the concept earlier.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from scripts.lead_lag_report import (
    Chain,
    Coverage,
    FirstMention,
    adoption_chains,
    backfill_evidence,
    build_report_data,
    extract_quote,
    naive_leader_counts,
    precursor_stats,
    render_report,
    spearman,
)


def d(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso)


def cov(source: str, start: str, end: str, n: int) -> Coverage:
    return Coverage(source_id=source, start=d(start), end=d(end), n_artifacts=n)


def fm(concept: str, source: str, date: str, **kwargs: object) -> FirstMention:
    defaults: dict[str, object] = {
        "artifact_id": f"{source}-{date}",
        "title": f"{concept} video by {source}",
        "url": f"https://www.youtube.com/watch?v={source}-{date}",
        "start_seconds": None,
        "segment_text": None,
        "as_mentioned": concept,
    }
    defaults.update(kwargs)
    return FirstMention(concept_id=concept, source_id=source, first_date=d(date), **defaults)  # type: ignore[arg-type]


COVERAGE = {
    # deep backfill: indexed from 2024-10
    "backfill": cov("backfill", "2024-10-01", "2026-06-30", 220),
    # normal channels: indexed from 2026-01/02
    "alpha": cov("alpha", "2026-01-01", "2026-06-30", 30),
    "bravo": cov("bravo", "2026-02-01", "2026-06-30", 30),
    "charlie": cov("charlie", "2026-02-15", "2026-06-30", 30),
}


class TestEligibility:
    def test_backfill_first_before_anyone_covered_concept_skipped(self):
        # backfill "leads" in 2025-03 when nobody else was indexed -> no eligible
        # competitors -> the concept contributes nothing to leader stats.
        mentions = [
            fm("c1", "backfill", "2025-03-01"),
            fm("c1", "alpha", "2026-03-01"),
            fm("c1", "bravo", "2026-03-10"),
            fm("c1", "charlie", "2026-03-20"),
        ]
        stats = precursor_stats({"c1": mentions}, COVERAGE, min_eligible=3)
        assert stats == {}

    def test_emergence_within_shared_coverage_counts_all_adopters(self):
        # emergence 2026-03-01: all four coverage windows already active.
        mentions = [
            fm("c1", "backfill", "2026-03-01"),
            fm("c1", "alpha", "2026-03-05"),
            fm("c1", "bravo", "2026-03-10"),
            fm("c1", "charlie", "2026-03-20"),
        ]
        stats = precursor_stats({"c1": mentions}, COVERAGE, min_eligible=3)
        assert set(stats) == {"backfill", "alpha", "bravo", "charlie"}
        assert stats["backfill"].firsts == pytest.approx(1.0)
        assert stats["alpha"].firsts == pytest.approx(0.0)

    def test_partially_eligible_concept_drops_late_coverage_adopters(self):
        # emergence 2026-01-15: charlie's coverage starts 2026-02-15, so charlie
        # is not an eligible competitor (they might have covered it earlier,
        # unobserved) but backfill/alpha are.
        mentions = [
            fm("c1", "alpha", "2026-01-15"),
            fm("c1", "backfill", "2026-02-01"),
            fm("c1", "charlie", "2026-02-20"),
        ]
        stats = precursor_stats({"c1": mentions}, COVERAGE, min_eligible=2)
        assert "charlie" not in stats
        assert stats["alpha"].firsts == pytest.approx(1.0)


class TestPrecursorLift:
    def test_lift_normalizes_for_posting_rate(self):
        # Two creators, same coverage window; heavy posts 9x more than light.
        coverage = {
            "heavy": cov("heavy", "2026-01-01", "2026-06-30", 90),
            "light": cov("light", "2026-01-01", "2026-06-30", 10),
        }
        mentions = {"c1": [fm("c1", "light", "2026-02-01"), fm("c1", "heavy", "2026-02-10")]}
        stats = precursor_stats(mentions, coverage, min_eligible=2)
        # light's expected-first probability is 0.1, so being first once is a 10x lift;
        # heavy's expected is 0.9, observed 0 -> lift 0.
        assert stats["light"].lift == pytest.approx(10.0)
        assert stats["heavy"].lift == pytest.approx(0.0)

    def test_tied_first_dates_split_credit(self):
        coverage = {
            "a": cov("a", "2026-01-01", "2026-06-30", 10),
            "b": cov("b", "2026-01-01", "2026-06-30", 10),
        }
        mentions = {"c1": [fm("c1", "a", "2026-02-01"), fm("c1", "b", "2026-02-01")]}
        stats = precursor_stats(mentions, coverage, min_eligible=2)
        assert stats["a"].firsts == pytest.approx(0.5)
        assert stats["b"].firsts == pytest.approx(0.5)

    def test_mean_lag_days_measured_against_leader(self):
        coverage = {
            "a": cov("a", "2026-01-01", "2026-06-30", 10),
            "b": cov("b", "2026-01-01", "2026-06-30", 10),
        }
        mentions = {"c1": [fm("c1", "a", "2026-02-01"), fm("c1", "b", "2026-02-11")]}
        stats = precursor_stats(mentions, coverage, min_eligible=2)
        assert stats["b"].mean_lag_days == pytest.approx(10.0)
        assert stats["a"].mean_lag_days == pytest.approx(0.0)


class TestNaiveCounts:
    def test_naive_counts_ignore_eligibility(self):
        mentions = {
            "c1": [
                fm("c1", "backfill", "2025-03-01"),
                fm("c1", "alpha", "2026-03-01"),
                fm("c1", "bravo", "2026-03-10"),
            ]
        }
        counts = naive_leader_counts(mentions)
        assert counts["backfill"] == pytest.approx(1.0)


class TestAdoptionChains:
    def test_chain_orders_eligible_adopters_and_respects_window(self):
        mentions = {
            "c1": [
                fm("c1", "backfill", "2026-03-01"),
                fm("c1", "alpha", "2026-03-08"),
                # bravo is 200 days later: beyond the follow window, chain breaks there.
                fm("c1", "bravo", "2026-09-25"),
            ]
        }
        chains = adoption_chains(mentions, COVERAGE, min_eligible=2, follow_window_days=90)
        assert len(chains) == 1
        chain = chains[0]
        assert [m.source_id for m in chain.mentions] == ["backfill", "alpha", "bravo"]
        # consecutive edges within the window: backfill->alpha only
        assert [(e[0], e[1]) for e in chain.edges] == [("backfill", "alpha")]

    def test_chain_skips_concept_below_min_eligible(self):
        mentions = {"c1": [fm("c1", "backfill", "2025-01-01"), fm("c1", "alpha", "2026-02-01")]}
        chains = adoption_chains(mentions, COVERAGE, min_eligible=2, follow_window_days=90)
        assert chains == []


class TestSpearman:
    def test_perfect_positive(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_negative(self):
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_handles_ties_via_average_ranks(self):
        rho = spearman([1, 2, 2, 4], [1, 2, 3, 4])
        assert -1.0 <= rho <= 1.0


class TestExtractQuote:
    def test_finds_term_and_bounds_width(self):
        text = "x" * 500 + " the allocation economy is here " + "y" * 500
        quote = extract_quote(text, "allocation economy", width=100)
        assert "allocation economy" in quote
        assert len(quote) <= 140  # width + ellipses slack

    def test_falls_back_to_head_when_term_absent(self):
        quote = extract_quote("a" * 400, "missing term", width=100)
        assert quote.startswith("a")
        assert len(quote) <= 140

    def test_strips_newlines(self):
        quote = extract_quote("line one\nline two with term here", "term", width=100)
        assert "\n" not in quote


class TestBackfillEvidence:
    def test_ungrounded_leader_gets_quote_from_same_artifact_segment(self, tmp_path: Path):
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect(":memory:")
        con.execute(
            "CREATE TABLE segments (segment_id VARCHAR, artifact_id VARCHAR, position INTEGER, start_seconds INTEGER, text VARCHAR)"
        )
        con.execute("INSERT INTO segments VALUES ('a1:0', 'a1', 0, 0, 'intro chatter')")
        con.execute("INSERT INTO segments VALUES ('a1:1', 'a1', 1, 120, 'here the PACT pattern shows up in speech')")
        leader = fm("c1", "alpha", "2026-03-01", artifact_id="a1", as_mentioned="pact pattern")
        chain = Chain(concept_id="c1", mentions=(leader,), edges=())
        result = backfill_evidence(con, [chain])
        con.close()
        filled = result[0].mentions[0]
        assert filled.start_seconds == 120
        assert "PACT pattern" in (filled.segment_text or "")

    def test_grounded_leader_untouched(self):
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect(":memory:")
        con.execute(
            "CREATE TABLE segments (segment_id VARCHAR, artifact_id VARCHAR, position INTEGER, start_seconds INTEGER, text VARCHAR)"
        )
        leader = fm("c1", "alpha", "2026-03-01", segment_text="already quoted", start_seconds=42)
        chain = Chain(concept_id="c1", mentions=(leader,), edges=())
        result = backfill_evidence(con, [chain])
        con.close()
        assert result[0].mentions[0].segment_text == "already quoted"
        assert result[0].mentions[0].start_seconds == 42


class TestEndToEnd:
    @pytest.fixture()
    def mini_db(self, tmp_path: Path):
        duckdb = pytest.importorskip("duckdb")
        db = tmp_path / "mini.duckdb"
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE sources (source_id VARCHAR, name VARCHAR, kind VARCHAR)")
        con.execute(
            "CREATE TABLE artifacts (artifact_id VARCHAR, source_id VARCHAR, kind VARCHAR,"
            " title VARCHAR, published_at DATE, url VARCHAR)"
        )
        con.execute(
            "CREATE TABLE segments (segment_id VARCHAR, artifact_id VARCHAR, position INTEGER,"
            " start_seconds INTEGER, text VARCHAR)"
        )
        con.execute(
            "CREATE TABLE has_concept (artifact_id VARCHAR, segment_id VARCHAR, concept_id VARCHAR,"
            " entity_id VARCHAR, as_mentioned VARCHAR, confidence DOUBLE, grounded BOOLEAN,"
            " extractor_model VARCHAR, prompt_version VARCHAR, extracted_at VARCHAR)"
        )
        rows = []
        # 4 creators, one concept, staggered first mentions inside shared coverage
        for i, (src, date) in enumerate(
            [("s1", "2026-03-01"), ("s2", "2026-03-05"), ("s3", "2026-03-12"), ("s4", "2026-04-01")]
        ):
            con.execute("INSERT INTO sources VALUES (?, ?, 'youtube_channel')", [src, src])
            # anchor artifacts so every creator's coverage starts 2026-01-01
            con.execute(
                "INSERT INTO artifacts VALUES (?, ?, 'video', 'anchor', DATE '2026-01-01', 'https://yt/a')",
                [f"anchor-{src}", src],
            )
            aid = f"vid-{src}"
            con.execute(
                "INSERT INTO artifacts VALUES (?, ?, 'video', ?, ?, ?)",
                [aid, src, f"{src} covers pact", date, f"https://www.youtube.com/watch?v={aid}"],
            )
            con.execute(
                "INSERT INTO segments VALUES (?, ?, 0, ?, ?)",
                [f"{aid}:0", aid, 60 + i, f"[01:00] {src} says the pact pattern changes everything"],
            )
            rows.append((aid, f"{aid}:0"))
        for aid, seg in rows:
            con.execute(
                "INSERT INTO has_concept VALUES (?, ?, 'dom.pact', 'term:pact', 'pact pattern', 0.9, TRUE,"
                " 'm', 'p', '2026-06-01')",
                [aid, seg],
            )
        con.close()
        return db

    def test_build_report_data_and_render(self, mini_db: Path):
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect(str(mini_db), read_only=True)
        data = build_report_data(con, min_adopters=4, min_eligible=3, min_artifacts=1, follow_window_days=90)
        con.close()
        assert "s1" in data.stats
        assert data.stats["s1"].firsts == pytest.approx(1.0)
        report = render_report(data)
        # timestamped evidence link survives to the rendered report
        assert "&t=60s" in report
        assert "pact pattern" in report
        assert "Kill-criterion diagnostics" in report
