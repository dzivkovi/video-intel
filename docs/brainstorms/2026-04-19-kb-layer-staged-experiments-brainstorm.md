# KB Layer — Staged Experiments Brainstorm

**Date:** 2026-04-19

**Status:** consolidated into [ADR-0017](../adr/ADR-0017-kb-layer-strategy.md)

## Supersedes

This document promotes the durable reasoning from the two 2026-04-16 scratch
notes into canonical `docs/brainstorms/`. The scratch notes remain for
historical record but are no longer the authoritative reference:

- [work/2026-04-16/03-architecture-futures-cognee-lightrag-llm-wiki.md](../../work/2026-04-16/03-architecture-futures-cognee-lightrag-llm-wiki.md)
- [work/2026-04-16/04-knowledge-layer-options-brainstorm.md](../../work/2026-04-16/04-knowledge-layer-options-brainstorm.md)

If you read one file first, read this one. If you want the decision (not
the exploration), read [ADR-0017](../adr/ADR-0017-kb-layer-strategy.md).
If you want the narrative archaeology, read the two scratch notes above.

## What prompted this

On 2026-04-16 three KB-layer candidates were on the table and the
brainstorm concluded with an explicit rule: *"Let the benchmark decide,
not upfront design."* The brainstorm set 80% as the threshold for
"ship a wiki layer on top of current hybrid search," and <70% as the
trigger to revisit graph options (Cognee / LightRAG).

On 2026-04-19 the benchmark was built
([`tests/evals/golden_dataset.yaml`](../../tests/evals/golden_dataset.yaml))
and run. Result: **1 of 25** (~4%). That's well past the <70% trigger —
graph options are back on the table, but only to the extent the eval
confirms they earn their uplift.

## The four options, briefly

### Option A — Stay put, finish the existing roadmap

Keep hybrid BM25 + vector as the only retrieval. Tune prompts. Add a
`brief` subcommand that runs a query and produces a Claude-invokable
synthesis inline.

- **Effort:** Days.
- **Fits "consulting-ready" goal:** 7/10.
- **Why it's not sufficient:** The 1/25 baseline shows retrieval itself
  is the bottleneck. Adding a synthesis layer on broken retrieval
  compounds errors, not value.

### Option B — Cognee as Layer 3

Feed mindmaps, transcripts, concepts.json into Cognee. Graph relations,
remember / recall / forget / improve API. Uses LanceDB internally.

- **Effort:** 1–2 weeks + maintenance.
- **Fits goal:** 6/10.
- **Why rejected:** Forces its own extraction pipeline (duplicates
  [ADR-0010](../adr/ADR-0010-llm-concept-normalization.md)'s
  `concepts.json` work). Designed for multi-tenant enterprise. v1.0
  just shipped on release day — production risk for consulting work.

### Option C — LightRAG as Layer 3

Dual-level graph: local entity aggregation + global community
detection. EMNLP 2025 paper. Largest adoption momentum.

- **Effort:** ~1 week + maintenance.
- **Fits goal:** 8/10 (global mode answers the "what do creators X, Y,
  Z agree on?" question natively).
- **Caveat:** Re-runs its own entity extraction on top of ours. Two
  parallel vocabularies. Graph alias propagation is the mechanism
  that directly addresses the measured failure (vocabulary mismatch).

### Option D — LLM Wiki on existing artifacts

The project has ~70% of a Karpathy-style LLM Wiki already: raw sources
(videos + meta.json), LLM pages (mindmaps + transcripts), canonical
vocabulary (taxonomy.json), hybrid query layer
([ADR-0013](../adr/ADR-0013-hybrid-search-rrf-fusion.md)). Missing:
cross-references, synthesis pages, index.md/log.md.

- **Effort:** ~1 week for v1.
- **Fits goal:** 9/10. Zero new vendors. Obsidian-compatible output.
- **Limitation:** Pre-computed synthesis solves the *synthesis* failure
  mode (5% baseline on synthesis queries), not the *recall* failure
  mode (primary bottleneck in the 1/25 result).

## The shift between 2026-04-16 and 2026-04-19

On 2026-04-16 the recommendation was **D + eval + A as scaffolding**,
with B/C deferred. That recommendation was eval-blind — it was the best
guess given no measurement.

On 2026-04-19 the eval tells us *which* failure mode dominates:
**vocabulary mismatch** (primary, affects ~90% of queries) far outweighs
**synthesis gap** (secondary, affects ~20% of queries — and even those
are also bottlenecked on recall since you can't synthesize what you
can't retrieve).

The recommendation therefore sharpens into a **staged order**, not a
different choice:

- **Stage 1:** Cheapest test of the vocabulary-mismatch hypothesis —
  query expansion via existing `taxonomy.json` aliases. 1–2 days.
- **Stage 2:** LightRAG (Option C). The graph alias propagation is the
  heavy version of what Stage 1 approximates cheaply. Only if Stage 1
  uplift is insufficient.
- **Stage 3:** Wiki (Option D). Synthesis is orthogonal to recall —
  this addresses the 5% baseline on synthesis queries. Parallel-safe
  with Stage 2.
- **Rejected:** Option B (Cognee) — duplicates existing work, overkill
  for single-user, pre-1.0 risk.

## Four-futures table, re-annotated with 2026-04-19 eval data

| Option | Pre-eval fit | Post-eval role | Stage |
|--------|--------------|---------------|-------|
| A — Stay put | 7/10 (scaffolding) | Insufficient alone (1/25) | Baseline (gate) |
| B — Cognee | 6/10 | Duplicates ADR-0010; no unique value | Rejected |
| C — LightRAG | 8/10 | Directly addresses primary failure (vocabulary) | Stage 2 |
| D — LLM Wiki | 9/10 (synthesis) | Addresses secondary failure (synthesis gap) | Stage 3 |
| —  | — | Cheap upper-bound test before Stage 2 | Stage 1 (query expansion, new) |

## Key decisions captured here (for searchability)

- **The eval is the gate.** Every stage re-runs `pytest tests/evals/`.
  ROI is the N/25 uplift delta. Dataset changes require ADR-grade
  justification.
- **Cheapest-first.** Before investing in a graph DB (Stage 2), try
  query expansion via existing `taxonomy.json` (Stage 1). If the cheap
  fix hits ≥10/25 or so, the graph layer may not be needed.
- **Orthogonal failure modes can run parallel.** Stage 2 (recall) and
  Stage 3 (synthesis) target different metrics and can be built
  concurrently in separate worktrees without blurring attribution.
- **No parallel bake-off of Cognee vs LightRAG.** Cognee is rejected;
  there's no competition to run.
- **The eval dataset is a frozen contract.** Per
  [ADR-0017](../adr/ADR-0017-kb-layer-strategy.md), edits to
  `tests/evals/golden_dataset.yaml` need an ADR update.

## Open questions still relevant

- **When is Stage 1 "enough" to skip Stage 2?** The brainstorm's
  original threshold was ≥80% = ship wiki / <70% = revisit graph.
  Stage 1 likely won't hit 80% — it's a cheap fix, not a
  silver-bullet. A more useful heuristic after Stage 1:
  - If the *remaining* failures are recall-shaped (wrong/missing
    videos) → proceed to Stage 2.
  - If they're synthesis-shaped (right videos, can't compose) →
    skip to Stage 3.
- **G-Eval metrics for synthesis.** `position_diversity` and
  `essay_coverage` dimensions are defined in the golden dataset but
  not implemented in `tests/evals/metrics.py`. Stage 3 cannot be
  fairly scored without them.
- **Timestamp precision for mindmap-sourced gold entries
  (`seankochel`).** May be systematically too strict since mindmap
  timestamps are bullet-level, not chunk-level. Could mask real
  recall if Stage 2 produces mindmap-level hits.

## What to read next

- [ADR-0017 — Staged KB-Layer Strategy](../adr/ADR-0017-kb-layer-strategy.md) — the canonical decision record.
- [docs/testing.md](../testing.md) — how to run the eval; how to interpret the 1/25 baseline.
- `docs/plans/2026-04-19-feat-kb-stage1-query-expansion-plan.md` *(not yet written)* — Stage 1 tactical plan, produced via `/workflows:plan` after this brainstorm ratifies.
