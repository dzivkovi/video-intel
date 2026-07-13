#!/usr/bin/env python3
"""SDSM-validated creator-creator network (issue #98, method 2 of #95).

Turns the bipartite creator-concept adoption matrix into a *validated* actor
network: which creator pairs share significantly more concepts than chance,
GIVEN both how prolific each creator is AND how popular each concept is.

Why this exists (Probe C, 2026-07-11 findings): a plain hypergeometric null
conditions only on creator degree, so in a topically homogeneous corpus where
everyone covers "ai agents", concept popularity does the sharing for you and
almost every pair comes out "significant" (314 of 465 pairs on today's store).
That is a null-model artifact, not 314 real ties.

Method (Neal 2021 SDSM / the max-entropy Bipartite Configuration Model):
  1. Bipartite 0/1 matrix B: creators x concepts, B[i,j] = 1 iff creator i has
     >= 1 dated `has_concept` row for concept j (the exact adopter definition
     used by lead_lag_report.load_first_mentions).
  2. Fit p_ij = P(B[i,j] = 1) conditioning on BOTH row sums (creator
     prolificness) and column sums (concept popularity). Two estimators, both
     logistic regression on the degree sequence:
       - `fit_bicm` (PRIMARY): per-node degree effects sigmoid(a_i + b_j). The
         a_i / b_j are the sufficient statistics of the row/column sums, so the
         fitted matrix reproduces every margin exactly - it fully conditions on
         both, which is the entire point of moving past the hypergeometric.
       - `fit_logit_degrees` (COMPARISON): Neal's 2-predictor logit on the raw
         row-sum and column-sum values. Faster but only *smooths* the margins
         (3 parameters cannot match 31 creator degrees), so it under-conditions
         on prolificness and keeps looser edges. Reported for sensitivity only.
  3. Per creator pair (i,k) the shared-concept count under the null is a
     Poisson-binomial over concepts of p_ij * p_kj; normal approximation with a
     continuity correction gives the upper-tail p-value (fine at this size).
  4. Benjamini-Hochberg across all pairs; keep edges with q < alpha (0.05).

Acceptance gate (pre-registered in docs/brainstorms/2026-07-12-null-model-
method-specs.md, Spec A): SDSM PASSES and ships the network if it prunes the
hypergeometric null to an eyeball-able edge set; it STOPS and ships a "corpus
too homogeneous for pairwise validation" finding if more than half of all
pairs still survive. `classify_gate` encodes the bands so the decision is
testable, not prose.

One read-only report script over intel.duckdb, stdlib + numpy, not a framework
(#95 guardrail). No Gemini calls. This corpus is ~100x smaller than the studies
SDSM comes from: every surviving edge is a lead for inspection, not a verdict.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from lead_lag_report import DEFAULT_DB

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

ALPHA_DEFAULT = 0.05
GATE_CLEAN_MAX = 40  # < this many surviving edges: clean pass (eyeball-able)
_ETA_CLAMP = 30.0  # keep sigmoid arguments finite for saturated margins
TOP_EDGES_DEFAULT = 30
SHARED_SAMPLE = 5  # example shared concepts shown per top edge for grounding


@dataclass(frozen=True)
class PairStat:
    a: str
    b: str
    observed: float
    expected: float
    p_value: float
    q_value: float = 1.0

    @property
    def multiple(self) -> float:
        return self.observed / self.expected if self.expected > 0 else 0.0


# ---------------------------------------------------------------------------
# Matrix + null-model fitting (pure numpy - unit-tested without a DB)
# ---------------------------------------------------------------------------


def build_bipartite(adoptions: list[tuple[str, str]]) -> tuple[list[str], list[str], np.ndarray]:
    """(concept_id, creator_id) adoption pairs -> (creators, concepts, B[creator, concept])."""
    creators = sorted({c for _, c in adoptions})
    concepts = sorted({t for t, _ in adoptions})
    ci = {c: i for i, c in enumerate(creators)}
    pj = {t: j for j, t in enumerate(concepts)}
    B = np.zeros((len(creators), len(concepts)), dtype=float)
    for concept, creator in adoptions:
        B[ci[creator], pj[concept]] = 1.0
    return creators, concepts, B


def fit_bicm(B: np.ndarray, max_iter: int = 500, tol: float = 1e-6) -> np.ndarray:
    """Max-entropy Bipartite Configuration Model: p_ij = sigmoid(a_i + b_j).

    Coordinate Newton on the per-node effects so the fitted probability matrix
    reproduces both the row sums (creator degrees) and column sums (concept
    degrees). This is the SDSM null that conditions on BOTH margins exactly -
    the a_i / b_j effects ARE the row/column-sum sufficient statistics.
    """
    row_deg = B.sum(axis=1)
    col_deg = B.sum(axis=0)
    n_co = B.shape[1]
    # init from marginal log-odds; +0.5 smoothing keeps 0/full degrees finite
    a = np.log((row_deg + 0.5) / (n_co - row_deg + 0.5))
    b = np.zeros(B.shape[1])
    for _ in range(max_iter):
        P = _sigmoid(a[:, None] + b[None, :])
        wa = np.clip((P * (1 - P)).sum(axis=1), 1e-9, None)
        a += (row_deg - P.sum(axis=1)) / wa
        P = _sigmoid(a[:, None] + b[None, :])
        wc = np.clip((P * (1 - P)).sum(axis=0), 1e-9, None)
        b += (col_deg - P.sum(axis=0)) / wc
        P = _sigmoid(a[:, None] + b[None, :])
        if max(_max_abs(row_deg - P.sum(axis=1)), _max_abs(col_deg - P.sum(axis=0))) < tol:
            break
    return _sigmoid(a[:, None] + b[None, :])


def fit_logit_degrees(B: np.ndarray, max_iter: int = 100, tol: float = 1e-8) -> np.ndarray:
    """Neal's 2-predictor SDSM logit: B[i,j] ~ intercept + row_sum_i + col_sum_j.

    The literal "logistic regression on row-sum + column-sum". Standardizes the
    two predictors for IRLS stability. Kept for sensitivity comparison; it only
    smooths the margins, so it under-conditions on creator prolificness.
    """
    n_cr, n_co = B.shape
    row_deg = B.sum(axis=1)
    col_deg = B.sum(axis=0)
    ri = np.repeat(np.arange(n_cr), n_co)
    cj = np.tile(np.arange(n_co), n_cr)
    feats = np.column_stack([row_deg[ri], col_deg[cj]])
    mu, sd = feats.mean(axis=0), feats.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    X = np.column_stack([np.ones(n_cr * n_co), (feats - mu) / sd])
    y = B.reshape(-1)
    beta = np.zeros(X.shape[1])
    for _ in range(max_iter):
        p = _sigmoid(X @ beta)
        W = np.clip(p * (1 - p), 1e-9, None)
        step = np.linalg.solve(X.T @ (X * W[:, None]) + 1e-8 * np.eye(X.shape[1]), X.T @ (y - p))
        beta += step
        if _max_abs(step) < tol:
            break
    return _sigmoid(X @ beta).reshape(n_cr, n_co)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -_ETA_CLAMP, _ETA_CLAMP)))


def _max_abs(x: np.ndarray) -> float:
    return float(np.max(np.abs(x))) if x.size else 0.0


def pairwise_significance(B: np.ndarray, P: np.ndarray, creators: list[str]) -> list[PairStat]:
    """Poisson-binomial (normal-approx) upper-tail p per creator pair.

    Null shared count for pair (a,b) is sum_j Bernoulli(p_aj * p_bj); its mean
    is sum(q) and variance sum(q(1-q)) with q = p_aj*p_bj. Upper tail with a 0.5
    continuity correction: p = P(shared >= observed).
    """
    stats: list[PairStat] = []
    for a, b in itertools.combinations(range(len(creators)), 2):
        q = P[a] * P[b]
        mean = float(q.sum())
        var = float((q * (1 - q)).sum())
        observed = float((B[a] * B[b]).sum())
        if var <= 0:
            p_value = 1.0 if observed <= mean else 0.0
        else:
            z = (observed - 0.5 - mean) / math.sqrt(var)
            p_value = _norm_sf(z)
        stats.append(PairStat(creators[a], creators[b], observed, mean, p_value))
    return stats


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def benjamini_hochberg(p_values: list[float]) -> list[float]:
    """BH step-up q-values, aligned to the input order. Monotone, capped at 1."""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    q = [1.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        i = order[rank]
        prev = min(prev, p_values[i] * m / (rank + 1))
        q[i] = prev
    return q


def significant_edges(stats: list[PairStat], alpha: float = ALPHA_DEFAULT) -> list[PairStat]:
    """Attach BH q-values and return the surviving edges, strongest multiple first."""
    q = benjamini_hochberg([s.p_value for s in stats])
    edges = [PairStat(s.a, s.b, s.observed, s.expected, s.p_value, q[i]) for i, s in enumerate(stats) if q[i] < alpha]
    return sorted(edges, key=lambda s: (-s.multiple, s.q_value))


def classify_gate(n_edges: int, n_pairs: int) -> str:
    """Pre-registered SDSM acceptance gate (Spec A). Returns pass|pass-flag|stop.

    - "pass": < GATE_CLEAN_MAX surviving edges - clean, eyeball-able network.
    - "stop": more than half of all pairs still survive - corpus too
      homogeneous for pairwise validation; ship that finding instead.
    - "pass-flag": in between - ship the network but flag density prominently.

    The spec pinned the flag/stop boundary at 175 of 351 pairs (= half). Read
    proportionally ("more than half of all pairs") so it survives the corpus
    growing from Probe C's 351 pairs to today's larger set.
    """
    if n_edges < GATE_CLEAN_MAX:
        return "pass"
    if n_edges > n_pairs / 2:
        return "stop"
    return "pass-flag"


# ---------------------------------------------------------------------------
# DB layer (read-only)
# ---------------------------------------------------------------------------


def load_adoptions(con: DuckDBPyConnection) -> list[tuple[str, str]]:
    """DISTINCT (concept_id, source_id) over dated has_concept rows.

    Mirrors lead_lag_report.load_first_mentions' adopter definition: a cell
    exists iff the creator has at least one has_concept row on a dated artifact.
    """
    return con.execute(
        """
        SELECT DISTINCT hc.concept_id, a.source_id
        FROM has_concept hc
        JOIN artifacts a ON hc.artifact_id = a.artifact_id
        WHERE a.published_at IS NOT NULL
        """
    ).fetchall()


def shared_concepts(B: np.ndarray, concepts: list[str], creators: list[str], a: str, b: str) -> list[str]:
    ia, ib = creators.index(a), creators.index(b)
    mask = (B[ia] * B[ib]) > 0
    return [concepts[j] for j in np.nonzero(mask)[0]]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _hypergeom_sf(x: float, N: int, K: int, n: int) -> float:
    """P(X >= x), X ~ Hypergeometric(N population, K successes, n draws). Log-space."""
    if x <= 0:
        return 1.0
    hi = min(K, n)
    if x > hi:
        return 0.0

    def logC(a: int, c: int) -> float:
        if c < 0 or c > a:
            return -math.inf
        return math.lgamma(a + 1) - math.lgamma(c + 1) - math.lgamma(a - c + 1)

    logden = logC(N, n)
    return min(1.0, sum(math.exp(logC(K, k) + logC(N - K, n - k) - logden) for k in range(math.ceil(x), hi + 1)))


def hypergeometric_significant(B: np.ndarray, creators: list[str], alpha: float = ALPHA_DEFAULT) -> int:
    """Probe C's plain hypergeometric null - the baseline SDSM must prune. Count of q<alpha pairs."""
    N = B.shape[1]
    deg = B.sum(axis=1).astype(int)
    pvals, obs = [], []
    for a, b in itertools.combinations(range(len(creators)), 2):
        shared = float((B[a] * B[b]).sum())
        obs.append(shared)
        pvals.append(_hypergeom_sf(shared, N, int(deg[a]), int(deg[b])))
    q = benjamini_hochberg(pvals)
    return sum(1 for v in q if v < alpha)


def render_report(
    creators: list[str],
    concepts: list[str],
    B: np.ndarray,
    edges: list[PairStat],
    n_pairs: int,
    hypergeom_sig: int,
    gate: str,
    alpha: float,
    logit_edge_count: int,
    logit_row_misfit: float,
    top_edges: int = TOP_EDGES_DEFAULT,
) -> str:
    today = dt.date.today().isoformat()
    lines: list[str] = []
    lines.append("# SDSM-validated creator-creator network")
    lines.append("")
    lines.append(
        f"Generated: {today} | Issue #98 (method 2 of #95) | Substrate: DuckDB truth store (PR #86). "
        "Bipartite Configuration Model (max-entropy SDSM) null over the creator-concept adoption matrix."
    )
    lines.append("")
    lines.append(
        f"Matrix: {len(creators)} creators x {len(concepts)} concepts, {n_pairs} creator pairs. "
        f"A cell is 1 iff the creator has >= 1 dated `has_concept` row for the concept (the lead-lag adopter "
        "definition)."
    )
    lines.append("")

    # --- Gate verdict (pre-registered) ---
    lines.append("## Acceptance-gate verdict (pre-registered, Spec A)")
    lines.append("")
    verdicts = {
        "pass": f"**PASS (clean).** SDSM prunes the plain hypergeometric null's {hypergeom_sig}/{n_pairs} "
        f"significant pairs to **{len(edges)}** edges at q<{alpha} - below the {GATE_CLEAN_MAX}-edge "
        "eyeball threshold. Ship the network.",
        "pass-flag": f"**PASS (flag density).** SDSM prunes the hypergeometric {hypergeom_sig}/{n_pairs} to "
        f"**{len(edges)}** edges at q<{alpha}. That is a real prune but still dense for {len(creators)} "
        "creators - read the network as a ranked shortlist, not a sparse skeleton.",
        "stop": f"**STOP (corpus too homogeneous).** SDSM still returns **{len(edges)}** of {n_pairs} pairs "
        f"significant - more than half. Even the degree-preserving null cannot separate real ties from "
        "topical homogeneity here. The honest deliverable is this finding, not the edge list below.",
    }
    lines.append(verdicts[gate])
    lines.append("")
    lines.append(
        f"Baseline for comparison: the plain hypergeometric null (Probe C's method, conditions on creator "
        f"degree only) flags **{hypergeom_sig} of {n_pairs}** pairs on this same matrix. SDSM adds the "
        "concept-popularity condition and cuts that to the set below."
    )
    lines.append("")
    lines.append(
        f"Estimator sensitivity: the primary null above is the exact-margin BiCM. Neal's 2-predictor logit "
        f"approximation (row-sum + column-sum only) yields **{logit_edge_count}** edges on the same matrix "
        f"but misfits creator margins by up to {logit_row_misfit:.0f} concepts, so it under-conditions on "
        "prolificness and keeps looser edges; the exact-margin form is the stricter standard and is used here."
    )
    lines.append("")

    # --- Edge table (Probe C shape) ---
    lines.append(f"## Validated edges (q < {alpha}, strongest overlap-multiple first)")
    lines.append("")
    lines.append("| Creator A | Creator B | observed shared | expected shared | multiple | q-value |")
    lines.append("|---|---|---|---|---|---|")
    shown = edges[:top_edges] if top_edges else edges
    for s in shown:
        lines.append(f"| {s.a} | {s.b} | {s.observed:.0f} | {s.expected:.1f} | {s.multiple:.1f}x | {s.q_value:.2e} |")
    if top_edges and len(edges) > top_edges:
        lines.append("")
        lines.append(f"...and {len(edges) - top_edges} more edges (raise --top).")
    lines.append("")

    # --- Grounding: example shared concepts for the top few edges ---
    lines.append("## Example shared concepts (top edges, for grounding)")
    lines.append("")
    for s in shown[:5]:
        sample = shared_concepts(B, concepts, creators, s.a, s.b)[:SHARED_SAMPLE]
        pretty = ", ".join(c.replace("term:", "").replace("-", " ") for c in sample)
        lines.append(f"- **{s.a} + {s.b}**: {pretty}")
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- This corpus is ~100x smaller than the studies SDSM comes from: every edge is a lead for manual "
        "inspection, not a verdict (issue #95 guardrail)."
    )
    lines.append(
        "- The overlap multiple compresses toward 1.0 vs the hypergeometric null on purpose: SDSM bakes "
        "concept popularity into the expectation, so a surviving 1.6x edge is a *stronger* claim than a "
        "hypergeometric 6x edge (which was mostly popularity)."
    )
    lines.append(
        "- Edges are undirected co-adoption ties, not influence. 'Who leads whom' is the separate lead-lag "
        "report (issue #93)."
    )
    lines.append(
        "- A shared-concept count depends on concept-extraction granularity (issue #85 lineage); two creators "
        "sharing many generic concepts is weaker evidence than sharing specific ones."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="SDSM-validated creator network over intel.duckdb (issue #98).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to intel.duckdb (read-only)")
    parser.add_argument("--out", type=Path, default=None, help="Write markdown here (default: stdout)")
    parser.add_argument("--alpha", type=float, default=ALPHA_DEFAULT, help="BH q-value threshold (default 0.05)")
    parser.add_argument("--top", type=int, default=TOP_EDGES_DEFAULT, help="Max edges listed (0 = all)")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"store not found: {args.db} (build it with scripts/intel_graph.py load)")
    import duckdb

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        adoptions = load_adoptions(con)
    finally:
        con.close()

    creators, concepts, B = build_bipartite(adoptions)
    n_pairs = len(creators) * (len(creators) - 1) // 2
    P = fit_bicm(B)
    stats = pairwise_significance(B, P, creators)
    edges = significant_edges(stats, alpha=args.alpha)
    hypergeom_sig = hypergeometric_significant(B, creators, alpha=args.alpha)
    gate = classify_gate(len(edges), n_pairs)

    # sensitivity: Neal's 2-predictor logit
    P_logit = fit_logit_degrees(B)
    logit_edges = significant_edges(pairwise_significance(B, P_logit, creators), alpha=args.alpha)
    logit_row_misfit = _max_abs(B.sum(axis=1) - P_logit.sum(axis=1))

    report = render_report(
        creators,
        concepts,
        B,
        edges,
        n_pairs,
        hypergeom_sig,
        gate,
        args.alpha,
        len(logit_edges),
        logit_row_misfit,
        top_edges=args.top,
    )
    if args.out is None:
        print(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(
            f"wrote {args.out}: {len(edges)} edges / {n_pairs} pairs, gate={gate} "
            f"(hypergeometric baseline {hypergeom_sig}, logit sensitivity {len(logit_edges)})"
        )


if __name__ == "__main__":
    main()
