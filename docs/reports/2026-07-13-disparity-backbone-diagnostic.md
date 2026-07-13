# PMI + disparity-filter backbone - kill diagnostic

Generated: 2026-07-13 | Issue #99 (method 3 of #95) | Substrate: DuckDB co_occurs (PR #86). One-shot diagnostic, not a framework (#95 guardrail).

## Verdict

**INCONCLUSIVE (parked).** The overlap sits in the 7-9 band: the disparity backbone neither cleanly confirms the kill (>= 10) nor reopens it (<= 6). Left for the owner to judge.

Kill metric: betweenness top-15 vs weighted-degree top-15 overlap on the raw-weight disparity backbone = **9/15** (hold >= 10, reopen <= 6, else inconclusive).

## Two backbones (both reported honestly)

- **PMI-weighted disparity (the literal Spec B):** DEGENERATE - **0 backbone edges** at alpha=0.05. PMI log-compresses each node's incident weights, so no edge dominates its node and nothing clears the disparity null (the single most-disparate edge in the whole graph sits at p=0.055, just above 0.05). The 'strictest standard' cannot be evaluated as literally specified - PMI and the disparity filter work at cross-purposes here (PMI flattens the very weight heterogeneity the disparity filter needs). This degeneracy is itself part of the finding.
- **Raw-weight disparity (canonical Serrano on its native additive input):** 557 backbone edges / 243 nodes from 99577 co-occurrence edges at alpha=0.05. The overlap above is measured on this backbone (the only evaluable one).

## Top-15 sets (raw-weight backbone)

| Rank | Betweenness | Weighted degree |
|---|---|---|
| 1 | agent-teams (shared) | agent-teams |
| 2 | gemini-cli (shared) | compaction |
| 3 | compaction (shared) | post-training-1ab7e0 |
| 4 | semantic-search (shared) | semantic-search |
| 5 | swe-bench-verified-7ba581 (shared) | swe-bench-verified-7ba581 |
| 6 | post-training-1ab7e0 (shared) | function-calling |
| 7 | sandboxing (shared) | gemini-cli |
| 8 | function-calling (shared) | agent-harness |
| 9 | governance | memory-systems |
| 10 | project-management | sandboxing |
| 11 | agent-harness (shared) | memory-layer |
| 12 | n8n-workflow | system-architecture |
| 13 | spec-driven-development-23a8ab | memory-files |
| 14 | browser-automation | agent-orchestration |
| 15 | task-management | design-systems |

## Caveats

- One-shot diagnostic; the overlap number is the entire deliverable. No community detection, no new dependency (#95 guardrail).
- Betweenness is unweighted (structural-broker), matching the 2026-07-11 Probe A comparison. Weighted (distance = 1/weight) betweenness shifts the overlap by about one at most - not enough to change the band.
- Corpus is ~100x smaller than the cited studies (Serrano PNAS 2009): a lead for the owner's judgment, not a verdict.
