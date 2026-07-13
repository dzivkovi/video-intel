"""Tests for scripts/disparity_backbone.py (issue #99, method 3 of #95).

Core contract: the disparity filter keeps an edge only when it dominates its
node's weight distribution; a node with uniform incident weights yields nothing
(the mechanism behind the PMI-weighted degeneracy this diagnostic reports). The
kill metric is the betweenness top-15 vs weighted-degree top-15 overlap, and
classify_kill encodes the pre-registered hold/reopen/inconclusive bands so the
verdict is testable, not prose.
"""

from __future__ import annotations

from scripts.disparity_backbone import (
    KILL_HOLD_MIN,
    KILL_REOPEN_MAX,
    BackboneResult,
    betweenness,
    classify_kill,
    disparity_backbone,
    positive_pmi_weights,
    render_report,
    top_overlap,
    weighted_degree,
)


def _adj(edges):
    adj = {}
    for a, b, _ in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    return adj


class TestPositivePMI:
    def test_drops_hub_edge_keeps_surprising_pair(self):
        # H is a hub (10 weight-1 spokes); x and y also share a private edge.
        # The hub-to-spoke edge is "expected" (pmi < 0, dropped); the private
        # x-y edge is surprising (pmi > 0, kept).
        edges = [("H", f"n{i}", 1.0) for i in range(10)] + [("x", "y", 1.0)]
        # make x, y two of the hub's spokes so they have realistic strength
        edges = [("H", "x", 1.0), ("H", "y", 1.0)] + [("H", f"n{i}", 1.0) for i in range(8)] + [("x", "y", 1.0)]
        kept = {(a, b) for a, b, _ in positive_pmi_weights(edges)}
        assert ("x", "y") in kept
        assert ("H", "x") not in kept

    def test_all_weights_positive(self):
        edges = [("H", "x", 1.0), ("H", "y", 1.0), ("x", "y", 1.0)]
        for _, _, w in positive_pmi_weights(edges):
            assert w > 0


class TestDisparityFilter:
    def test_dominant_edge_survives(self):
        # star: center C with one heavy spoke and four light ones
        edges = [("C", "L1", 100.0)] + [("C", f"L{i}", 1.0) for i in range(2, 6)]
        backbone = {(a, b) for a, b, _ in disparity_backbone(edges, alpha=0.05)}
        assert ("C", "L1") in backbone
        assert ("C", "L2") not in backbone

    def test_uniform_node_keeps_nothing(self):
        # a triangle of equal weights - every node's incident weights are
        # uniform, so nothing dominates -> empty backbone. This is the exact
        # mechanism that makes PMI-weighted disparity degenerate on the corpus.
        edges = [("a", "b", 1.0), ("b", "c", 1.0), ("a", "c", 1.0)]
        assert disparity_backbone(edges, alpha=0.05) == []

    def test_degree_one_node_does_not_anchor(self):
        # a single edge: both endpoints have degree 1, so (1-p)^0 = 1 >= alpha
        # from both sides -> pruned (matches Serrano).
        assert disparity_backbone([("a", "b", 5.0)], alpha=0.05) == []


class TestBetweenness:
    def test_path_middle_node_highest(self):
        edges = [("A", "B", 1.0), ("B", "C", 1.0)]
        cb = betweenness(_adj(edges), ["A", "B", "C"])
        assert cb["B"] > cb["A"]
        assert cb["B"] > cb["C"]
        assert cb["A"] == 0.0 and cb["C"] == 0.0

    def test_star_center_highest(self):
        edges = [("C", f"L{i}", 1.0) for i in range(4)]
        cb = betweenness(_adj(edges), ["C"] + [f"L{i}" for i in range(4)])
        assert all(cb["C"] > cb[f"L{i}"] for i in range(4))


class TestWeightedDegree:
    def test_sums_raw_weights(self):
        backbone = [("a", "b", 3.0), ("a", "c", 9.9)]  # filtered weights differ from raw
        raw = {("a", "b"): 2.0, ("a", "c"): 5.0}
        wdeg = weighted_degree(backbone, raw)
        assert wdeg["a"] == 7.0  # 2 + 5, the RAW weights, not the filtered ones
        assert wdeg["b"] == 2.0


class TestOverlapAndGate:
    def test_top_overlap_counts_intersection(self):
        betw = {"a": 5, "b": 4, "c": 3, "d": 2}
        wdeg = {"a": 9, "b": 1, "c": 8, "e": 7}
        assert top_overlap(betw, wdeg, k=2) == 1  # {a,b} & {a,c} = {a}

    def test_classify_kill_bands(self):
        assert classify_kill(KILL_HOLD_MIN) == "hold"
        assert classify_kill(15) == "hold"
        assert classify_kill(KILL_HOLD_MIN - 1) == "inconclusive"  # 9
        assert classify_kill(KILL_REOPEN_MAX + 1) == "inconclusive"  # 7
        assert classify_kill(KILL_REOPEN_MAX) == "reopen"  # 6
        assert classify_kill(0) == "reopen"


class TestRender:
    def _result(self, overlap: int) -> BackboneResult:
        top_b = [f"term:b{i}" for i in range(15)]
        top_w = top_b[:overlap] + [f"term:w{i}" for i in range(15 - overlap)]
        return BackboneResult(
            n_input_edges=99577,
            n_backbone_edges=557,
            n_backbone_nodes=243,
            top_betweenness=top_b,
            top_weighted_degree=top_w,
            overlap=overlap,
            top_k=15,
        )

    def test_inconclusive_verdict_and_pmi_degeneracy(self):
        report = render_report(self._result(9), pmi_backbone_edges=0, pmi_min_alpha=0.055, alpha=0.05)
        assert "INCONCLUSIVE" in report
        assert "9/15" in report
        assert "DEGENERATE" in report and "0 backbone edges" in report
        assert "lead for the owner's judgment, not a verdict" in report

    def test_hold_verdict(self):
        report = render_report(self._result(12), pmi_backbone_edges=0, pmi_min_alpha=0.06, alpha=0.05)
        assert "KILL HOLDS" in report

    def test_reopen_verdict(self):
        report = render_report(self._result(4), pmi_backbone_edges=0, pmi_min_alpha=0.06, alpha=0.05)
        assert "REOPEN" in report
