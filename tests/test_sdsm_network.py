"""Tests for scripts/sdsm_network.py (issue #98, method 2 of #95).

Core contract: the SDSM null conditions on BOTH creator prolificness and
concept popularity. The load-bearing invariant is that the fitted BiCM matrix
reproduces both margins exactly - that is what makes a surviving edge mean
"shares more than degree AND popularity predict", not just "is prolific". A
pair glued together beyond what its degrees explain survives; a pair whose
overlap is fully explained by popular concepts is pruned. The pre-registered
acceptance gate (pass/pass-flag/stop) is encoded in classify_gate so the
decision is testable, not prose.
"""

from __future__ import annotations

import numpy as np
import pytest

from scripts.sdsm_network import (
    GATE_CLEAN_MAX,
    PairStat,
    _hypergeom_sf,
    benjamini_hochberg,
    build_bipartite,
    classify_gate,
    fit_bicm,
    fit_logit_degrees,
    hypergeometric_significant,
    pairwise_significance,
    render_report,
    significant_edges,
)


def _planted_matrix() -> tuple[list[str], list[str], np.ndarray]:
    """8 creators x 45 concepts: one glued pair against a popularity-only background.

    Layout so that ONLY creators 6,7 form a validated tie:
    - concepts 0..4 are universal (all 8 creators) -> shared by every pair, but
      fully explained by popularity, so no pair survives on them alone.
    - background creators 0..5 each own a DISJOINT private block of 5 concepts,
      so any two background creators overlap only on the universal block ->
      expected ~= observed -> pruned.
    - creators 6,7 share a 10-concept niche block only they adopt -> an overlap
      no degree/popularity model expects -> the one surviving, strongest edge.
    """
    n_cr = 8
    B_rows: list[np.ndarray] = []
    universal = list(range(5))
    private = {c: list(range(5 + 5 * c, 10 + 5 * c)) for c in range(6)}  # concepts 5..34
    niche = list(range(35, 45))  # concepts 35..44, only creators 6,7
    n_co = 45
    for c in range(6):
        row = np.zeros(n_co)
        row[universal] = 1.0
        row[private[c]] = 1.0
        B_rows.append(row)
    for _ in (6, 7):
        row = np.zeros(n_co)
        row[universal] = 1.0
        row[niche] = 1.0
        B_rows.append(row)
    B = np.vstack(B_rows)
    creators = [f"c{i}" for i in range(n_cr)]
    concepts = [f"term:t{j}" for j in range(n_co)]
    return creators, concepts, B


class TestBipartite:
    def test_build_maps_pairs_to_cells(self):
        adoptions = [("term:a", "alice"), ("term:b", "alice"), ("term:a", "bob")]
        creators, concepts, B = build_bipartite(adoptions)
        assert creators == ["alice", "bob"]
        assert concepts == ["term:a", "term:b"]
        assert B[creators.index("alice"), concepts.index("term:a")] == 1.0
        assert B[creators.index("bob"), concepts.index("term:b")] == 0.0
        assert B.sum() == 3


class TestBiCMFit:
    def test_fit_reproduces_both_margins(self):
        # THE invariant that distinguishes SDSM from the hypergeometric null:
        # the fitted probability matrix matches row AND column degrees exactly.
        _, _, B = _planted_matrix()
        P = fit_bicm(B)
        assert np.max(np.abs(P.sum(axis=1) - B.sum(axis=1))) < 1e-3
        assert np.max(np.abs(P.sum(axis=0) - B.sum(axis=0))) < 1e-3

    def test_probabilities_bounded(self):
        _, _, B = _planted_matrix()
        P = fit_bicm(B)
        assert P.min() >= 0.0 and P.max() <= 1.0

    def test_logit_fit_returns_valid_probabilities(self):
        # Neal's 2-predictor logit is the sensitivity comparison, not the
        # headline. Its row-margin underfit is a wide-degree-spread property of
        # the real corpus (shown at Gate 1: ~55-concept misfit), not a stable
        # synthetic invariant, so we only assert it runs and is well-formed.
        _, _, B = _planted_matrix()
        P = fit_logit_degrees(B)
        assert P.shape == B.shape
        assert P.min() >= 0.0 and P.max() <= 1.0


class TestPairwiseSignificance:
    def test_far_above_expected_is_tiny_p(self):
        # two identical creators over a block, popularity held uniform low ->
        # observed shared massively exceeds the null expectation.
        creators = ["a", "b"]
        B = np.zeros((2, 20))
        B[0, 0:10] = 1.0
        B[1, 0:10] = 1.0
        P = np.full((2, 20), 0.2)
        (stat,) = pairwise_significance(B, P, creators)
        assert stat.observed == 10.0
        assert stat.expected == pytest.approx(20 * 0.04)
        assert stat.p_value < 1e-6

    def test_below_expected_is_high_p(self):
        creators = ["a", "b"]
        B = np.zeros((2, 20))  # zero overlap
        P = np.full((2, 20), 0.5)  # expected shared = 20*0.25 = 5
        (stat,) = pairwise_significance(B, P, creators)
        assert stat.observed == 0.0
        assert stat.p_value > 0.5

    def test_zero_variance_pair_does_not_crash(self):
        creators = ["a", "b"]
        B = np.array([[1.0, 1.0], [1.0, 1.0]])
        P = np.ones((2, 2))  # every prob 1 -> variance 0
        (stat,) = pairwise_significance(B, P, creators)
        assert stat.p_value == 1.0  # observed (2) not > mean (2)


class TestHypergeometricBaseline:
    """The hypergeometric null is the baseline SDSM must prune - it anchors the
    report's headline '314 -> 10' number, so its survival function is pinned."""

    def test_sf_full_draw_equals_top_term(self):
        # drawing all K successes: P(X >= K) = C(K,K)C(N-K,n-K)/C(N,n)
        import math

        N, K, n = 45, 15, 15
        assert _hypergeom_sf(K, N, K, n) == pytest.approx(1.0 / math.comb(N, n))

    def test_sf_boundaries(self):
        assert _hypergeom_sf(0, 45, 15, 15) == 1.0  # X >= 0 is certain
        assert _hypergeom_sf(16, 45, 15, 15) == 0.0  # can't draw more than min(K,n)=15

    def test_flags_universal_overlap_that_sdsm_prunes(self):
        # THE documented weakness: the degree-only hypergeometric null flags a
        # universal-overlap matrix as significant (expected 6.4, observed 8,
        # p=1/45<0.05 for every pair) - exactly the noise SDSM's popularity
        # conditioning removes (cf. test_universal_overlap_is_pruned -> []).
        B = np.zeros((5, 10))
        B[:, 0:8] = 1.0
        n_pairs = 5 * 4 // 2
        assert hypergeometric_significant(B, [f"c{i}" for i in range(5)]) == n_pairs


class TestBenjaminiHochberg:
    def test_monotone_and_capped(self):
        q = benjamini_hochberg([0.001, 0.2, 0.5, 0.9])
        assert all(0.0 <= v <= 1.0 for v in q)
        # q-values, sorted by their p-values, are non-decreasing
        assert q == sorted(q)

    def test_empty(self):
        assert benjamini_hochberg([]) == []

    def test_all_tiny_all_significant(self):
        q = benjamini_hochberg([1e-9, 1e-9, 1e-9])
        assert all(v < 0.05 for v in q)


class TestPlantedNetwork:
    def test_glued_niche_pair_is_the_strongest_edge(self):
        # creators 6,7 share 15 niche concepts only they adopt - no degree or
        # popularity model expects that overlap, so the pair must survive and
        # be the single most-surprising (smallest-q) edge in the network.
        creators, _concepts, B = _planted_matrix()
        edges = significant_edges(pairwise_significance(B, fit_bicm(B), creators), alpha=0.05)
        surviving = {frozenset((e.a, e.b)) for e in edges}
        assert frozenset(("c6", "c7")) in surviving
        strongest = min(edges, key=lambda e: e.q_value)
        assert {strongest.a, strongest.b} == {"c6", "c7"}

    def test_universal_overlap_is_pruned(self):
        # a pair whose entire overlap is concepts EVERY creator adopts is fully
        # explained by popularity (p_ij ~ 1) -> expected ~= observed -> pruned.
        # This is the headline SDSM claim: popularity-explained ties do not
        # survive.
        creators = [f"c{i}" for i in range(5)]
        B = np.zeros((5, 10))
        B[:, 0:8] = 1.0  # 8 universal concepts, adopted by everyone
        edges = significant_edges(pairwise_significance(B, fit_bicm(B), creators), alpha=0.05)
        assert edges == []

    def test_edge_count_far_below_all_pairs(self):
        creators, _concepts, B = _planted_matrix()
        edges = significant_edges(pairwise_significance(B, fit_bicm(B), creators))
        n_pairs = len(creators) * (len(creators) - 1) // 2
        assert len(edges) < n_pairs  # the whole point: prune, don't pass everything


class TestGate:
    def test_clean_pass_below_threshold(self):
        assert classify_gate(GATE_CLEAN_MAX - 1, 465) == "pass"

    def test_stop_when_more_than_half(self):
        assert classify_gate(300, 465) == "stop"  # > 232

    def test_flag_in_between(self):
        assert classify_gate(106, 465) == "pass-flag"

    def test_boundary_exactly_half_is_not_stop(self):
        # "more than half" - exactly half is still a (flagged) pass
        assert classify_gate(200, 400) == "pass-flag"

    def test_boundary_at_clean_threshold_flags(self):
        assert classify_gate(GATE_CLEAN_MAX, 465) == "pass-flag"

    def test_tiny_corpus_more_than_half_is_stop_not_clean_pass(self):
        # 20 edges of 28 pairs is BOTH < 40 (clean band) AND > half (stop band);
        # "more than half survive" must win or the gate ships a clean verdict on
        # a corpus its own stop-criterion calls too homogeneous (Codex + correctness).
        assert classify_gate(20, 28) == "stop"


class TestRender:
    def _report(self, gate: str, edges: list[PairStat], hypergeom: int) -> str:
        creators, concepts, B = _planted_matrix()
        return render_report(
            creators,
            concepts,
            B,
            edges,
            n_pairs=28,
            hypergeom_sig=hypergeom,
            gate=gate,
            alpha=0.05,
            logit_edge_count=5,
            logit_row_misfit=12.3,
        )

    def test_edge_table_has_probe_c_columns(self):
        edges = [PairStat("c6", "c7", 15.0, 3.2, 1e-6, 1e-5)]
        report = self._report("pass", edges, hypergeom=20)
        assert "observed shared" in report and "expected shared" in report
        assert "multiple" in report and "q-value" in report
        assert "| c6 | c7 |" in report

    def test_gate_verdict_and_baseline_and_sensitivity(self):
        report = self._report("pass", [PairStat("c6", "c7", 15.0, 3.2, 1e-6, 1e-5)], hypergeom=20)
        assert "PASS (clean)" in report
        assert "20" in report  # hypergeometric baseline surfaced
        assert "sensitivity" in report.lower()  # logit comparison surfaced

    def test_stop_verdict_names_homogeneity(self):
        report = self._report("stop", [], hypergeom=300)
        assert "STOP" in report
        assert "homogeneous" in report

    def test_has_honesty_caveat(self):
        report = self._report("pass", [PairStat("c6", "c7", 15.0, 3.2, 1e-6, 1e-5)], hypergeom=20)
        assert "lead for manual inspection, not a verdict" in report
