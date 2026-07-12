"""Tests for scripts/lead_lag_report.py (issue #93).

The core contract under test: coverage correction. A deep-backfill creator
whose first mention predates everyone else's coverage window must NOT be
credited as a leader, because nobody else could have been observed covering
the concept earlier. Sibling contract (PR #96 review): a sub-threshold
creator's first mention still sets the emergence date, so the second adopter
never inherits a first they did not earn.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from scripts.intel_graph import SCHEMA_SQL
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


class TestRankableGate:
    """PR #96 adversarial finding: sub-threshold creators are observed, not erased."""

    def test_subthreshold_first_mover_blocks_credit_for_second_adopter(self):
        coverage = {
            "tiny": cov("tiny", "2026-01-01", "2026-06-30", 4),
            "alpha": cov("alpha", "2026-01-01", "2026-06-30", 30),
            "bravo": cov("bravo", "2026-01-01", "2026-06-30", 30),
        }
        rankable = frozenset({"alpha", "bravo"})
        mentions = {
            "c1": [
                fm("c1", "tiny", "2026-01-10"),
                fm("c1", "alpha", "2026-01-30"),
                fm("c1", "bravo", "2026-02-15"),
            ]
        }
        stats = precursor_stats(mentions, coverage, min_eligible=2, rankable=rankable)
        # tiny led: alpha must NOT inherit the first, and tiny is not ranked
        assert "tiny" not in stats
        assert stats["alpha"].firsts == pytest.approx(0.0)
        assert stats["bravo"].firsts == pytest.approx(0.0)
        # lag is measured against the true (sub-threshold) leader
        assert stats["alpha"].lag_days == [pytest.approx(20.0)]

    def test_subthreshold_leader_still_appears_in_chain(self):
        coverage = {
            "tiny": cov("tiny", "2026-01-01", "2026-06-30", 4),
            "alpha": cov("alpha", "2026-01-01", "2026-06-30", 30),
        }
        mentions = {"c1": [fm("c1", "tiny", "2026-01-10"), fm("c1", "alpha", "2026-01-30")]}
        chains = adoption_chains(mentions, coverage, min_eligible=2, follow_window_days=90)
        assert [m.source_id for m in chains[0].mentions] == ["tiny", "alpha"]

    def test_rankable_none_ranks_everyone(self):
        mentions = {
            "c1": [
                fm("c1", "backfill", "2026-03-01"),
                fm("c1", "alpha", "2026-03-05"),
                fm("c1", "bravo", "2026-03-10"),
            ]
        }
        stats = precursor_stats(mentions, COVERAGE, min_eligible=3, rankable=None)
        assert set(stats) == {"backfill", "alpha", "bravo"}


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

    def test_rate_uses_inclusive_day_count(self):
        # 2026-01-01..2026-01-02 is a 2-day window: 4 artifacts -> 2.0/day, not 4.0/day
        c = cov("a", "2026-01-01", "2026-01-02", 4)
        assert c.rate == pytest.approx(2.0)
        # single-day window divides by 1
        assert cov("b", "2026-01-01", "2026-01-01", 3).rate == pytest.approx(3.0)


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

    def test_min_eligible_zero_never_yields_empty_chain(self):
        # PR #96 adversarial finding: min_eligible=0 plus adopters outside the
        # coverage map used to append Chain(mentions=()) and crash backfill.
        mentions = {"c1": [fm("c1", "ghost", "2026-01-01")]}  # ghost not in COVERAGE
        chains = adoption_chains(mentions, COVERAGE, min_eligible=0, follow_window_days=90)
        assert chains == []


class TestSpearman:
    def test_perfect_positive(self):
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_perfect_negative(self):
        assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_ties_use_average_ranks_exact_value(self):
        # ranks of x: [1, 2.5, 2.5, 4]; Pearson of ranks = 4.5/sqrt(4.5*5) = 0.9487
        rho = spearman([1, 2, 2, 4], [1, 2, 3, 4])
        assert rho == pytest.approx(0.9487, abs=1e-4)

    def test_fewer_than_two_points_returns_zero(self):
        assert spearman([1.0], [2.0]) == 0.0
        assert spearman([], []) == 0.0


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

    def test_normalizes_em_and_en_dashes(self):
        em, en = chr(0x2014), chr(0x2013)
        quote = extract_quote(f"AI now has{em}so many{en}subtle things", "many", width=100)
        assert em not in quote
        assert en not in quote
        assert "-" in quote


def _mini_store(tmp_path: Path, name: str = "mini.duckdb"):
    """A store built from intel_graph's canonical SCHEMA_SQL (schema-drift-proof)."""
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / name
    con = duckdb.connect(str(db))
    con.execute(SCHEMA_SQL)
    return db, con


class TestBackfillEvidence:
    def test_ungrounded_leader_gets_quote_from_same_artifact_segment(self, tmp_path: Path):
        _, con = _mini_store(tmp_path)
        con.execute(
            "INSERT INTO segments (segment_id, artifact_id, position, start_seconds, text)"
            " VALUES ('a1:0', 'a1', 0, 0, 'intro chatter'),"
            " ('a1:1', 'a1', 1, 120, 'here the PACT pattern shows up in speech')"
        )
        leader = fm("c1", "alpha", "2026-03-01", artifact_id="a1", as_mentioned="pact pattern")
        chain = Chain(concept_id="c1", mentions=(leader,), edges=())
        result = backfill_evidence(con, [chain])
        con.close()
        filled = result[0].mentions[0]
        assert filled.start_seconds == 120
        assert "PACT pattern" in (filled.segment_text or "")

    def test_tier2_mentions_table_entity_link_used_when_term_absent(self, tmp_path: Path):
        # term never appears verbatim, but the mentions table links the concept's
        # entity to a segment of the same artifact.
        _, con = _mini_store(tmp_path)
        con.execute(
            "INSERT INTO segments (segment_id, artifact_id, position, start_seconds, text)"
            " VALUES ('a1:0', 'a1', 0, 30, 'we discuss delegating work to background workers here')"
        )
        con.execute("INSERT INTO mentions (segment_id, entity_id) VALUES ('a1:0', 'term:remote-sessions')")
        con.execute(
            "INSERT INTO has_concept (artifact_id, segment_id, concept_id, entity_id, as_mentioned,"
            " confidence, grounded, extractor_model, prompt_version, extracted_at)"
            " VALUES ('a1', NULL, 'dom.remote_ai', 'term:remote-sessions', 'remote ai coding sessions',"
            " 0.9, FALSE, 'm', 'p', '2026-06-01')"
        )
        leader = fm("dom.remote_ai", "alpha", "2026-03-01", artifact_id="a1", as_mentioned="remote ai coding sessions")
        result = backfill_evidence(con, [Chain(concept_id="dom.remote_ai", mentions=(leader,), edges=())])
        con.close()
        filled = result[0].mentions[0]
        assert filled.start_seconds == 30
        assert "background workers" in (filled.segment_text or "")

    def test_tier3_token_match_used_when_entity_link_absent(self, tmp_path: Path):
        _, con = _mini_store(tmp_path)
        con.execute(
            "INSERT INTO segments (segment_id, artifact_id, position, start_seconds, text)"
            " VALUES ('a1:0', 'a1', 0, 45, 'the disposable nature of modern software is the theme')"
        )
        leader = fm("c1", "alpha", "2026-03-01", artifact_id="a1", as_mentioned="disposable software")
        result = backfill_evidence(con, [Chain(concept_id="c1", mentions=(leader,), edges=())])
        con.close()
        filled = result[0].mentions[0]
        assert filled.start_seconds == 45

    def test_grounded_leader_untouched(self, tmp_path: Path):
        _, con = _mini_store(tmp_path)
        leader = fm("c1", "alpha", "2026-03-01", segment_text="already quoted", start_seconds=42)
        chain = Chain(concept_id="c1", mentions=(leader,), edges=())
        result = backfill_evidence(con, [chain])
        con.close()
        assert result[0].mentions[0].segment_text == "already quoted"
        assert result[0].mentions[0].start_seconds == 42

    def test_empty_mentions_chain_does_not_crash(self, tmp_path: Path):
        _, con = _mini_store(tmp_path)
        result = backfill_evidence(con, [Chain(concept_id="c1", mentions=(), edges=())])
        con.close()
        assert result[0].mentions == ()


class TestRenderEdgeCases:
    def test_fewer_than_two_ranked_creators_renders_na_diagnostics(self):
        data_coverage = {"a": cov("a", "2026-01-01", "2026-06-30", 10)}
        from scripts.lead_lag_report import CreatorStats, ReportData

        stats = {"a": CreatorStats(source_id="a", firsts=2.0, expected=1.0, eligible_concepts=9)}
        data = ReportData(
            coverage=data_coverage,
            rankable=frozenset({"a"}),
            stats=stats,
            naive={"a": 2.0},
            chains=[],
            n_concepts_total=10,
            n_concepts_eligible=0,
            params={"min_adopters": 4, "min_eligible": 3, "min_artifacts": 5, "follow_window_days": 90},
        )
        report = render_report(data)
        assert "n/a (fewer than 2 ranked creators" in report

    def test_ranked_table_discloses_omission_threshold(self):
        from scripts.lead_lag_report import CreatorStats, ReportData

        # "c" is rankable but has ZERO eligible concepts, so it never enters
        # stats - it must still count as omitted (Codex peer-review finding).
        coverage = {
            "a": cov("a", "2026-01-01", "2026-06-30", 10),
            "b": cov("b", "2026-01-01", "2026-06-30", 10),
            "c": cov("c", "2026-01-01", "2026-06-30", 10),
        }
        stats = {
            "a": CreatorStats(source_id="a", firsts=3.0, expected=2.0, eligible_concepts=9),
            "b": CreatorStats(source_id="b", firsts=1.0, expected=2.0, eligible_concepts=2),
        }
        data = ReportData(
            coverage=coverage,
            rankable=frozenset({"a", "b", "c"}),
            stats=stats,
            naive={},
            chains=[],
            n_concepts_total=10,
            n_concepts_eligible=0,
            params={"min_adopters": 4, "min_eligible": 3, "min_artifacts": 5, "follow_window_days": 90},
        )
        report = render_report(data)
        assert "2 rankable creators omitted" in report
        assert ">= 5 eligible concepts" in report


class TestEndToEnd:
    @pytest.fixture()
    def mini_db(self, tmp_path: Path):
        db, con = _mini_store(tmp_path)
        rows = []
        # 4 creators, one concept, staggered first mentions inside shared coverage
        for i, (src, date) in enumerate(
            [("s1", "2026-03-01"), ("s2", "2026-03-05"), ("s3", "2026-03-12"), ("s4", "2026-04-01")]
        ):
            con.execute("INSERT INTO sources (source_id, name, kind) VALUES (?, ?, 'youtube_channel')", [src, src])
            # anchor artifacts so every creator's coverage starts 2026-01-01
            con.execute(
                "INSERT INTO artifacts (artifact_id, source_id, kind, title, published_at, url)"
                " VALUES (?, ?, 'video', 'anchor', DATE '2026-01-01', 'https://yt/a')",
                [f"anchor-{src}", src],
            )
            aid = f"vid-{src}"
            con.execute(
                "INSERT INTO artifacts (artifact_id, source_id, kind, title, published_at, url)"
                " VALUES (?, ?, 'video', ?, ?, ?)",
                [aid, src, f"{src} covers pact", date, f"https://www.youtube.com/watch?v={aid}"],
            )
            con.execute(
                "INSERT INTO segments (segment_id, artifact_id, position, start_seconds, text) VALUES (?, ?, 0, ?, ?)",
                [f"{aid}:0", aid, 60 + i, f"[01:00] {src} says the pact pattern changes everything"],
            )
            rows.append((aid, f"{aid}:0"))
        for aid, seg in rows:
            con.execute(
                "INSERT INTO has_concept (artifact_id, segment_id, concept_id, entity_id, as_mentioned,"
                " confidence, grounded, extractor_model, prompt_version, extracted_at)"
                " VALUES (?, ?, 'dom.pact', 'term:pact', 'pact pattern', 0.9, TRUE, 'm', 'p', '2026-06-01')",
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
        report = render_report(data, min_ranked_concepts=1)
        # timestamped evidence link survives to the rendered report
        assert "&t=60" in report
        assert "pact pattern" in report
        assert "Kill-criterion diagnostics" in report
        # with min_ranked_concepts=1 all four creators are ranked
        assert "| 1 | s1 |" in report

    def test_grounded_row_beats_ungrounded_for_evidence(self, mini_db: Path):
        # add an ungrounded duplicate row for s1's first-date artifact: the
        # grounded row (with segment) must still win the evidence slot.
        duckdb = pytest.importorskip("duckdb")
        con = duckdb.connect(str(mini_db))
        con.execute(
            "INSERT INTO has_concept (artifact_id, segment_id, concept_id, entity_id, as_mentioned,"
            " confidence, grounded, extractor_model, prompt_version, extracted_at)"
            " VALUES ('vid-s1', NULL, 'dom.pact', 'term:pact', 'pact pattern', 0.9, FALSE, 'm', 'p', '2026-06-02')"
        )
        con.close()
        con = duckdb.connect(str(mini_db), read_only=True)
        data = build_report_data(con, min_adopters=4, min_eligible=3, min_artifacts=1, follow_window_days=90)
        con.close()
        s1_mention = next(m for m in data.chains[0].mentions if m.source_id == "s1")
        assert s1_mention.segment_text is not None
        assert s1_mention.start_seconds == 60
