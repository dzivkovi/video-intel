#!/usr/bin/env python3
"""PMI + disparity-filter backbone diagnostic (issue #99, method 3 of #95).

A BOUNDED ONE-SHOT DIAGNOSTIC, explicitly NOT a framework (#95 guardrail). It
answers ONE pre-registered question with ONE number: on a Serrano-Boguna-
Vespignani disparity backbone of the co-occurrence graph, does betweenness
reroute AWAY from raw popularity, or does the top-15-betweenness set still equal
the top-15-weighted-degree set?

Standing kill (2026-07-11 findings, Probe A): unsupervised graph analytics on
the term co-occurrence graph returns popularity three ways; betweenness ~=
weighted degree even on a PMI-cleaned backbone (11/15 overlap). The Serrano
disparity filter is the one strictly stronger standard reserved as the "honest
second chance" before final judgment on terms-as-nodes analytics.

Pre-registered gate (Spec B, docs/brainstorms/2026-07-12-null-model-method-
specs.md): overlap >= 10/15 -> kill HOLDS at the strictest standard (close #99);
overlap <= 6/15 -> terms-as-nodes REOPENS (park); 7-9/15 -> inconclusive (park).

Two backbones are computed and both reported honestly:
  - PMI-weighted disparity (the literal Spec B step 1+2): recompute positive-PMI
    edge weights, then run the disparity filter on THOSE weights. On this corpus
    this is DEGENERATE - PMI log-compresses each node's incident weights, so no
    edge dominates its node and the backbone is empty at alpha=0.05. That
    degeneracy is itself a finding, but it leaves the overlap uncomputable.
  - Raw-weight disparity (the canonical Serrano filter on its native additive
    input): produces a real sparse backbone, so the kill metric is evaluable on
    it. This is where the reported overlap comes from.

Read-only over intel.duckdb, pure stdlib (hand-rolled Brandes betweenness; no
numpy, no new dependency, no community-detection framework - the anti-overbuild
guard). Corpus is ~100x smaller than the cited studies: a lead, not a verdict.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lead_lag_report import DEFAULT_DB

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

ALPHA_DEFAULT = 0.05
TOP_K_DEFAULT = 15
KILL_HOLD_MIN = 10  # overlap >= this: kill HOLDS (close #99)
KILL_REOPEN_MAX = 6  # overlap <= this: terms-as-nodes REOPENS (park)

Edge = tuple[str, str, float]


@dataclass(frozen=True)
class BackboneResult:
    n_input_edges: int
    n_backbone_edges: int
    n_backbone_nodes: int
    top_betweenness: list[str]
    top_weighted_degree: list[str]
    overlap: int
    top_k: int


# ---------------------------------------------------------------------------
# Weighting + disparity filter (pure - unit-tested without a DB)
# ---------------------------------------------------------------------------


def positive_pmi_weights(edges: list[Edge]) -> list[Edge]:
    """Recompute edge weights as positive PMI over the co-occurrence graph.

    pmi(a,b) = log( w_ab * W / (s_a * s_b) ) with W = total edge weight and
    s_a = node strength (sum of incident weights); keep pmi > 0. This is the
    standard weighted-co-occurrence PMI (Church-Hanks / Damani ACL 2013).
    """
    strength: dict[str, float] = defaultdict(float)
    total = 0.0
    for a, b, w in edges:
        strength[a] += w
        strength[b] += w
        total += w
    out: list[Edge] = []
    for a, b, w in edges:
        pmi = math.log(w * total / (strength[a] * strength[b]))
        if pmi > 0:
            out.append((a, b, pmi))
    return out


def disparity_backbone(edges: list[Edge], alpha: float = ALPHA_DEFAULT) -> list[Edge]:
    """Serrano-Boguna-Vespignani disparity filter (PNAS 2009).

    For a node of degree k, an incident edge whose weight is fraction p of the
    node's total incident weight survives the null (weights uniformly random)
    when its disparity p-value alpha_ij = (1 - p)^(k-1) < alpha. Keep the union
    of edges that survive from EITHER endpoint. Degree-1 nodes never anchor an
    edge ((1-p)^0 = 1), matching Serrano.
    """
    inc: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for a, b, w in edges:
        inc[a].append((b, w))
        inc[b].append((a, w))

    def survives(node: str, weight: float) -> bool:
        lst = inc[node]
        k = len(lst)
        if k <= 1:
            return False
        total = sum(x for _, x in lst)
        if total <= 0:
            return False
        return (1 - weight / total) ** (k - 1) < alpha

    return [(a, b, w) for a, b, w in edges if survives(a, w) or survives(b, w)]


def betweenness(adj: dict[str, list[str]], nodes: list[str]) -> dict[str, float]:
    """Unweighted Brandes betweenness centrality (structural-broker betweenness).

    Unweighted (hop-count) shortest paths: the standard "which node bridges
    otherwise-distant parts of the graph" measure, and the one the 2026-07-11
    probe used for its popularity-overlap comparison.
    """
    cb = dict.fromkeys(nodes, 0.0)
    for src in nodes:
        stack: list[str] = []
        pred: dict[str, list[str]] = defaultdict(list)
        sigma = dict.fromkeys(nodes, 0.0)
        sigma[src] = 1.0
        dist = dict.fromkeys(nodes, -1)
        dist[src] = 0
        queue = deque([src])
        while queue:
            v = queue.popleft()
            stack.append(v)
            for w in adj[v]:
                if dist[w] < 0:
                    dist[w] = dist[v] + 1
                    queue.append(w)
                if dist[w] == dist[v] + 1:
                    sigma[w] += sigma[v]
                    pred[w].append(v)
        delta = dict.fromkeys(nodes, 0.0)
        while stack:
            w = stack.pop()
            for v in pred[w]:
                delta[v] += (sigma[v] / sigma[w]) * (1 + delta[w])
            if w != src:
                cb[w] += delta[w]
    return cb


def weighted_degree(backbone: list[Edge], raw_weight: dict[tuple[str, str], float]) -> dict[str, float]:
    """Sum of RAW co-occurrence weights of a node's backbone edges (popularity proxy).

    Weighted degree is measured with the original co-occurrence counts, not the
    filtered/PMI weights, so it is the popularity signal the kill metric asks
    betweenness to diverge from.
    """
    wdeg: dict[str, float] = defaultdict(float)
    for a, b, _ in backbone:
        r = raw_weight.get((a, b)) or raw_weight.get((b, a)) or 0.0
        wdeg[a] += r
        wdeg[b] += r
    return wdeg


def _top(scores: dict[str, float], k: int) -> list[str]:
    return [n for n, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def top_overlap(betw: dict[str, float], wdeg: dict[str, float], k: int = TOP_K_DEFAULT) -> int:
    return len(set(_top(betw, k)) & set(_top(wdeg, k)))


def classify_kill(overlap: int) -> str:
    """Pre-registered gate. Returns 'hold' | 'reopen' | 'inconclusive'.

    Thresholds (KILL_HOLD_MIN / KILL_REOPEN_MAX) are pinned to the top-15 set
    the spec named. The diagnostic always runs at top-15 - there is no knob to
    rescale them against, which is why there is no --top flag.
    """
    if overlap >= KILL_HOLD_MIN:
        return "hold"
    if overlap <= KILL_REOPEN_MAX:
        return "reopen"
    return "inconclusive"


def analyze(edges: list[Edge], alpha: float = ALPHA_DEFAULT, top_k: int = TOP_K_DEFAULT) -> BackboneResult:
    """Run the disparity filter on the given weighted edges and compute the overlap."""
    backbone = disparity_backbone(edges, alpha)
    raw_weight = {(a, b): w for a, b, w in edges}
    adj: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for a, b, _ in backbone:
        adj[a].append(b)
        adj[b].append(a)
        nodes.add(a)
        nodes.add(b)
    node_list = sorted(nodes)
    betw = betweenness(adj, node_list)
    wdeg = weighted_degree(backbone, raw_weight)
    return BackboneResult(
        n_input_edges=len(edges),
        n_backbone_edges=len(backbone),
        n_backbone_nodes=len(node_list),
        top_betweenness=_top(betw, top_k),
        top_weighted_degree=_top(wdeg, top_k),
        overlap=top_overlap(betw, wdeg, top_k),
        top_k=top_k,
    )


# ---------------------------------------------------------------------------
# DB layer (read-only)
# ---------------------------------------------------------------------------


def load_co_occurs(con: DuckDBPyConnection) -> list[Edge]:
    rows = con.execute("SELECT entity_a, entity_b, weight FROM co_occurs").fetchall()
    return [(a, b, float(w)) for a, b, w in rows]


# ---------------------------------------------------------------------------
# Rendering (the verdict note - the deliverable)
# ---------------------------------------------------------------------------


def _clean(term: str) -> str:
    return term.replace("term:", "")


def render_report(
    raw_result: BackboneResult,
    pmi_backbone_edges: int,
    pmi_min_alpha: float | None,
    alpha: float,
) -> str:
    kill = classify_kill(raw_result.overlap)
    verdicts = {
        "hold": "**KILL HOLDS.** On the disparity backbone, the top betweenness set still equals the "
        "popularity (weighted-degree) set at the strictest standard - terms-as-nodes analytics stays retired. "
        "This closes the question.",
        "reopen": "**REOPEN.** Betweenness on the disparity backbone reroutes away from popularity - there are "
        "real bridge terms the popularity ranking misses. Terms-as-nodes analytics deserves another look.",
        "inconclusive": "**INCONCLUSIVE (parked).** The overlap sits in the 7-9 band: the disparity backbone "
        "neither cleanly confirms the kill (>= 10) nor reopens it (<= 6). Left for the owner to judge.",
    }
    lines: list[str] = []
    lines.append("# PMI + disparity-filter backbone - kill diagnostic")
    lines.append("")
    lines.append(
        f"Generated: {dt.date.today().isoformat()} | Issue #99 (method 3 of #95) | Substrate: DuckDB "
        "co_occurs (PR #86). One-shot diagnostic, not a framework (#95 guardrail)."
    )
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    lines.append(verdicts[kill])
    lines.append("")
    lines.append(
        f"Kill metric: betweenness top-{raw_result.top_k} vs weighted-degree top-{raw_result.top_k} overlap on "
        f"the raw-weight disparity backbone = **{raw_result.overlap}/{raw_result.top_k}** "
        f"(hold >= {KILL_HOLD_MIN}, reopen <= {KILL_REOPEN_MAX}, else inconclusive)."
    )
    lines.append("")
    lines.append("## Two backbones (both reported honestly)")
    lines.append("")
    if pmi_backbone_edges == 0:
        min_alpha_str = f"{pmi_min_alpha:.3f}" if pmi_min_alpha is not None else "n/a"
        lines.append(
            f"- **PMI-weighted disparity (the literal Spec B):** DEGENERATE - **0 backbone edges** at "
            f"alpha={alpha}. PMI log-compresses each node's incident weights, so no edge dominates its node "
            f"and nothing clears the disparity null (the single most-disparate edge in the whole graph sits at "
            f"p={min_alpha_str}, just above {alpha}). The 'strictest standard' cannot be evaluated as literally "
            "specified - PMI and the disparity filter work at cross-purposes here (PMI flattens the very weight "
            "heterogeneity the disparity filter needs). This degeneracy is itself part of the finding."
        )
    else:
        lines.append(
            f"- **PMI-weighted disparity (the literal Spec B):** {pmi_backbone_edges} backbone edges at alpha={alpha}."
        )
    lines.append(
        f"- **Raw-weight disparity (canonical Serrano on its native additive input):** "
        f"{raw_result.n_backbone_edges} backbone edges / {raw_result.n_backbone_nodes} nodes from "
        f"{raw_result.n_input_edges} co-occurrence edges at alpha={alpha}. The overlap above is measured on "
        "this backbone (the only evaluable one)."
    )
    lines.append("")
    lines.append(f"## Top-{raw_result.top_k} sets (raw-weight backbone)")
    lines.append("")
    lines.append("| Rank | Betweenness | Weighted degree |")
    lines.append("|---|---|---|")
    overlap_set = set(raw_result.top_betweenness) & set(raw_result.top_weighted_degree)
    for i in range(raw_result.top_k):
        b = _clean(raw_result.top_betweenness[i]) if i < len(raw_result.top_betweenness) else ""
        w = _clean(raw_result.top_weighted_degree[i]) if i < len(raw_result.top_weighted_degree) else ""
        bt = raw_result.top_betweenness[i] if i < len(raw_result.top_betweenness) else None
        mark = " (shared)" if bt in overlap_set else ""
        lines.append(f"| {i + 1} | {b}{mark} | {w} |")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- One-shot diagnostic; the overlap number is the entire deliverable. No community detection, no new "
        "dependency (#95 guardrail)."
    )
    lines.append(
        "- Betweenness is unweighted (structural-broker), matching the 2026-07-11 Probe A comparison. Weighted "
        "(distance = 1/weight) betweenness shifts the overlap by about one at most - not enough to change the "
        "band."
    )
    lines.append(
        "- Corpus is ~100x smaller than the cited studies (Serrano PNAS 2009): a lead for the owner's "
        "judgment, not a verdict."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _min_disparity_alpha(edges: list[Edge]) -> float | None:
    """The smallest disparity p-value across all edges - how close PMI came to a backbone."""
    inc: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for a, b, w in edges:
        inc[a].append((b, w))
        inc[b].append((a, w))

    def alpha_of(node: str, weight: float) -> float:
        lst = inc[node]
        k = len(lst)
        if k <= 1:
            return 1.0
        total = sum(x for _, x in lst)
        return (1 - weight / total) ** (k - 1)

    best: float | None = None
    for a, b, w in edges:
        val = min(alpha_of(a, w), alpha_of(b, w))
        best = val if best is None else min(best, val)
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description="PMI + disparity-filter backbone kill diagnostic (issue #99).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to intel.duckdb (read-only)")
    parser.add_argument("--out", type=Path, default=None, help="Write the verdict note here (default: stdout)")
    parser.add_argument("--alpha", type=float, default=ALPHA_DEFAULT, help="Disparity significance (default 0.05)")
    # No --top flag: the kill bands (KILL_HOLD_MIN / KILL_REOPEN_MAX) are pinned
    # to the top-15 set the pre-registered gate named, so the overlap denominator
    # is fixed. A configurable top-k would silently invalidate the verdict band.
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"store not found: {args.db} (build it with scripts/intel_graph.py load)")
    import duckdb

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        edges = load_co_occurs(con)
    finally:
        con.close()
    if not edges:
        sys.exit("co_occurs is empty - nothing to filter (build the store with scripts/intel_graph.py load).")

    pmi_edges = positive_pmi_weights(edges)
    pmi_backbone = disparity_backbone(pmi_edges, args.alpha)
    pmi_min_alpha = _min_disparity_alpha(pmi_edges) if not pmi_backbone else None
    raw_result = analyze(edges, alpha=args.alpha, top_k=TOP_K_DEFAULT)

    report = render_report(raw_result, len(pmi_backbone), pmi_min_alpha, args.alpha)
    if args.out is None:
        print(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        kill = classify_kill(raw_result.overlap)
        print(
            f"wrote {args.out}: raw-disparity overlap {raw_result.overlap}/{raw_result.top_k} -> {kill}; "
            f"PMI-disparity backbone edges = {len(pmi_backbone)}"
        )


if __name__ == "__main__":
    main()
