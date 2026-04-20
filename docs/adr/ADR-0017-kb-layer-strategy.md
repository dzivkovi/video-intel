# Staged KB-Layer Strategy, Gated by the 25-Query Eval

**Status:** accepted

**Date:** 2026-04-19

**Decision Maker(s):** Daniel Zivkovic

## Context

[ADR-0012](ADR-0012-vector-search-lancedb-voyage.md) and
[ADR-0013](ADR-0013-hybrid-search-rrf-fusion.md) shipped two generations of the
retrieval layer: vector-only (~62% precision) then BM25 + vector + RRF fusion
(~84% precision on a small ad-hoc sample). Both were evaluated on hand-picked
queries, not a frozen benchmark. By 2026-04-16 three candidate "Layer 3" KB
options were on the table — Cognee, LightRAG, and a Karpathy-style LLM Wiki —
with no objective way to choose between them. The
[knowledge-layer brainstorm](../../work/2026-04-16/04-knowledge-layer-options-brainstorm.md)
and the [architecture-futures note](../../work/2026-04-16/03-architecture-futures-cognee-lightrag-llm-wiki.md)
concluded with an explicit forcing function: *"Let the benchmark decide, not
upfront design."* The brainstorm proposed a threshold of ≥80% on a future eval
as the gate for shipping a wiki layer, with <70% triggering a revisit of
Cognee or LightRAG.

On 2026-04-19 that benchmark was built and run. The artifacts are
[`tests/evals/golden_dataset.yaml`](../../tests/evals/golden_dataset.yaml)
(25 grounded queries, 90 timestamped expected hits across 7 active channels)
and [`tests/evals/metrics.py`](../../tests/evals/metrics.py) (four deterministic
DeepEval `BaseMetric` subclasses: `RecallAtKMetric`, `MRRMetric`,
`ChannelCoverageMetric`, `TimestampPrecisionMetric`). The full harness is at
[`tests/evals/test_search_quality.py`](../../tests/evals/test_search_quality.py).

The baseline result on hybrid search (`video_intel search --vector`):

```text
1 of 25 queries passed all gating metrics (Q15 only).
Single-channel baselines:   ~11% average recall (target was 75%)
Cross-channel comparative:  ~25% average recall (target was 50–70%)
Cross-channel synthesis:     ~5% average recall (target was 30–50%)
```

That is ~4% overall. The <70% threshold from the April 16 brainstorm is not
just crossed — it's inverted: hybrid search is currently ~18× below the
revisit-graph trigger.

Diagnostic reading of the per-query trace:

- **Primary failure mode is vocabulary mismatch.** Golden queries phrase
  user-intent vocabulary ("reliable agents", "context engineering"). Content
  uses creator vocabulary ("Ralph Wiggum / force-feed", "Factorio parallel
  sessions", "Capybara"). BM25 + vector RRF fusion does not bridge the gap at
  top-K.
- **The one passing query (Q15) is the exception that proves the rule.**
  Q15's query phrase is "Opus 4.7, Gemma 4, Sonnet 4.6" — exact model-name
  overlap with transcript text. BM25 dominates, hybrid wins.
- **Data health is not the issue.** Spot-checked gold `video_id`s are all
  present in the 5,294-chunk index. This is a retrieval failure, not an
  ingestion failure.

The question is no longer *"which KB layer?"* but *"what's the cheapest
intervention that moves the 4% number, and how do we stay honest about which
intervention earned which point?"*

## Decision

**Stage KB-layer interventions from cheapest to heaviest, gating each stage
on an eval re-run against the same 25 queries.** Do not parallel-bake
options that address different failure modes as if they were competing.

### The stages

**Stage 1 — Query expansion via existing taxonomy aliases.**
Pre-process user queries through `taxonomy.json` and `concepts.json` before
BM25 / vector lookup. When a query token matches a concept alias, expand
the query to include the concept's canonical label and sibling aliases.
Uses existing artifacts only — no new infrastructure, no new dependencies,
no new data model. Expected effort: 1–2 days. Exit criterion: re-run
`pytest tests/evals/` and record the new N/25 in this ADR's Consequences
section.

**Stage 2 — LightRAG as the recall-focused graph layer.**
Only if Stage 1 uplift is insufficient. LightRAG's dual-level graph (local
entity aggregation + global community detection) directly addresses the
vocabulary-mismatch failure via alias / co-occurrence propagation, which
is exactly the mechanism Stage 1's cheap version approximates. Separate
worktree, dedicated plan via `/workflows:plan`, execution via
`/workflows:work` with swarm-mode parallel sub-agents for independent
workstreams (ingestion adapter, graph storage, retrieval integration, eval
wiring). Exit criterion: same as Stage 1 — record N/25 here.

**Stage 3 — Karpathy-style LLM Wiki synthesis layer.**
Addresses the orthogonal synthesis-gap failure mode, not vocabulary
mismatch. The ~5% baseline on synthesis queries indicates hybrid retrieval
cannot compose answers from multiple sources even when recall succeeds.
Wiki pages pre-compute those compositions. Can run *parallel* to Stage 2
in a separate worktree — they target different failure modes, so
concurrent execution is not a bake-off, it's two independent interventions
scored against the same 25 queries. Exit criterion: same as above.

### What the eval gates, and what it does not

The eval gates *which KB-layer intervention to prioritize next*. It does
not gate whether KB-layer work is worth doing at all — that was already
settled by the 1/25 baseline. A score of 4% on a benchmark the user's own
prior rule set to ≥70% for "stay put" is sufficient justification to
proceed.

### Cognee is rejected, not staged

The [2026-04-16 brainstorm](../../work/2026-04-16/04-knowledge-layer-options-brainstorm.md)
established that Cognee duplicates the `concepts.json` extraction work
already shipped in [ADR-0010](ADR-0010-llm-concept-normalization.md), is
designed for multi-tenant enterprise (overkill for single-user research),
and was at v1.0.1.dev1 on release day (production risk for consulting
work). LightRAG has the same re-extraction cost but adds community
detection that Cognee lacks. Putting Cognee in a staged plan means
re-litigating a decision the data already made.

## Consequences

### Positive Consequences

- **No more vibes competition.** Every KB-layer experiment re-runs the
  same 25 queries. ROI is the N/25 uplift delta. Cheap-first guarantees
  the project never invests in a heavy layer before testing whether a
  light one sufficed.
- **Early-stop permission.** If Stage 1 lifts 1/25 → 8/25 or better, the
  remaining 17 failures become diagnostically cleaner (not vocabulary).
  That sharpens what Stage 2 needs to address, and may raise the bar for
  Stage 2 to justify itself.
- **Attribution is preserved.** Because Stage 2 (LightRAG) and Stage 3
  (Wiki) address orthogonal failure modes, running them concurrently
  still lets the eval attribute uplift to the right intervention.
  Attacking both simultaneously with overlapping scopes would blur
  attribution — by explicitly naming "recall" vs "synthesis" as the
  target of each stage, this ADR prevents that.
- **Keeps ADR-0010 / ADR-0013 in force.** The concept / taxonomy layer
  and the hybrid-search RRF fusion are not being replaced. Stages 1–3
  are additions, not migrations.

### Negative Consequences

- **The eval dataset becomes a frozen contract.** Adding or changing
  golden queries mid-stream invalidates stage-over-stage comparison.
  Dataset changes now need ADR-grade justification, documented at the
  same level as a metric-threshold change.
- **Deterministic metrics don't measure synthesis quality.** The current
  four metrics (`RecallAtKMetric`, `MRRMetric`, `ChannelCoverageMetric`,
  `TimestampPrecisionMetric`) are all recall / precision metrics. The
  golden dataset declares `position_diversity` and `essay_coverage`
  dimensions that require G-Eval (LLM-judge) metrics still to be
  implemented. Stage 3 (Wiki) in particular will need those metrics to
  be measurable — expect a follow-up work item to wire G-Eval before
  Stage 3 exits.
- **Staged execution is calendar-slower than a parallel bake-off.**
  Stage 1 has to finish before Stage 2 decision. In exchange we avoid
  building heavy infrastructure that the cheap fix might have
  rendered unnecessary. The trade is discipline over speed.
- **Some eval coverage assumptions are untested.** Timestamp-precision
  thresholds were calibrated against transcript-sourced gold entries;
  `seankochel` channel entries use mindmap-sourced timestamps
  (bullet-level, not chunk-level), and may be systematically
  pessimistic. Treat any Seankochel-only uplift signal as suspect
  until a separate audit confirms thresholds are fair.

### Baseline and future measurements

| Date | Intervention | Score | Notes |
| ---- | ------------ | ----- | ----- |
| 2026-04-19 | Hybrid search (ADR-0012 + ADR-0013) | 1 / 25 | Q15 only. Vocabulary mismatch is primary failure. |
| *Stage 1 TBD* | Query expansion via taxonomy aliases | — | Record here after `/workflows:work` completes. |
| *Stage 2 TBD* | LightRAG layer | — | Only if Stage 1 insufficient. |
| *Stage 3 TBD* | LLM Wiki synthesis | — | Parallel-safe with Stage 2. |

## Alternatives Considered

- **Parallel bake-off of LightRAG vs Wiki.** Run both in separate
  worktrees from the start, pick the winner.
  - **Pros:** Fastest calendar time. Maximizes MAX-plan parallel
    compute.
  - **Cons:** They solve different failure modes. "Winner" is a
    category error — the eval score could lift for either, for
    different reasons. Also skips Stage 1, which is the cheapest
    falsifier of the vocabulary-mismatch hypothesis.
  - **Status:** rejected. Parallel execution is retained as an
    option at Stages 2 & 3, but only after Stage 1 results.

- **Pick LightRAG outright, skip Stage 1.** Commit to the heavy
  intervention the April 16 brainstorm conditionally recommended for
  sub-70% eval scores.
  - **Pros:** Most direct attack on the measured failure (vocabulary
    mismatch via graph).
  - **Cons:** Violates the "let the benchmark decide" rule in a
    subtle way — it skips testing whether a cheap solution would
    have been enough. One to two days of Stage 1 work is a small
    price for the signal of whether a one-week LightRAG integration
    is justified.
  - **Status:** rejected.

- **Pick Wiki outright, as the April 16 brainstorm recommended.**
  Ship synthesis pages on top of current hybrid search without
  addressing recall failure first.
  - **Pros:** Aligns with the project's stated consulting-ready goal
    ("here's what creators say about X" > "here are 47 search
    results").
  - **Cons:** Wiki pages synthesize from retrieved sources. If
    retrieval misses 96% of relevant material (the 1/25 baseline),
    the synthesis layer is compounding from a broken foundation. Fix
    recall first, synthesize second.
  - **Status:** deferred to Stage 3.

- **Do nothing, accept the 1/25 baseline.** Declare hybrid search
  sufficient and ship the project as-is.
  - **Pros:** Zero effort.
  - **Cons:** Fails the stated project goal (surfacing non-obvious
    cross-channel insights), and fails the user's own explicit
    threshold from April 16 (<70% triggers a revisit).
  - **Status:** rejected.

- **Rewrite the eval dataset to be less demanding.** Lower thresholds
  until the current system passes.
  - **Pros:** Instantly improves the score.
  - **Cons:** Adversarial to the purpose of having an eval. Treated
    as a failure mode of the decision process to guard against, not
    an option.
  - **Status:** rejected with prejudice. Future dataset edits require
    an ADR.

## Affects

Source files and artifacts that this decision governs going forward:

- `tests/evals/golden_dataset.yaml` — frozen contract; edits require
  ADR-level justification per Consequences above.
- `tests/evals/metrics.py` — adding G-Eval metrics for synthesis
  dimensions is Stage 3 prerequisite work.
- `tests/evals/test_search_quality.py` — harness; may add a CSV-writing
  hook to record stage-over-stage scores automatically.
- `scripts/video_intel.py` — Stage 1 adds a query-expansion preprocessor
  (exact location TBD in the Stage 1 plan).
- `docs/plans/2026-04-19-feat-kb-stage1-query-expansion-plan.md` (TBD) —
  Stage 1 tactical plan, produced via `/workflows:plan`.

## Related Debt

Follow-ups spawned by this decision, to track in GitHub Issues per
`CLAUDE.md` backlog convention:

- **G-Eval metrics for `position_diversity` and `essay_coverage`.**
  Declared in the golden dataset, not yet implemented in
  `tests/evals/metrics.py`. Prerequisite for Stage 3 scoring.
- **Timestamp-precision audit for `seankochel` mindmap-sourced gold
  entries.** Thresholds may be systematically too strict; could be
  masking real recall.
- **CSV / structured log of stage-over-stage eval scores.** Currently
  scores are captured in memory and in this ADR's Consequences
  table. A machine-readable log would make regression detection
  automatic.
- **`docs/solutions/rejected-paths/` directory.** Flagged as missing
  in the
  [2026-04-16 brainstorm](../../work/2026-04-16/04-knowledge-layer-options-brainstorm.md).
  Create it when the first post-mortem needs it (e.g., if a staged
  experiment itself fails and we want the lesson on record).

## Research References

- [Plan: 2026-04-19-testing-and-kb-layer-strategy (session artifact)](../../plans/humming-jumping-lollipop.md)
- [ADR-0010: LLM Concept Normalization](ADR-0010-llm-concept-normalization.md) — the concept / thesaurus substrate Stage 1 reuses.
- [ADR-0012: Vector Search via LanceDB + Voyage AI](ADR-0012-vector-search-lancedb-voyage.md) — the index the eval runs against.
- [ADR-0013: Hybrid Search RRF Fusion](ADR-0013-hybrid-search-rrf-fusion.md) — the retrieval path the 1/25 baseline measures.
- [Knowledge-layer options brainstorm](../../work/2026-04-16/04-knowledge-layer-options-brainstorm.md) — the "let the benchmark decide" rule originates here.
- [Architecture futures: Cognee vs LightRAG vs LLM Wiki](../../work/2026-04-16/03-architecture-futures-cognee-lightrag-llm-wiki.md) — comparative option analysis.
- [DeepEval docs — custom non-LLM metrics](https://deepeval.com/docs/metrics-custom) — `BaseMetric` contract used by `tests/evals/metrics.py`.
- [LightRAG paper (EMNLP 2025)](https://arxiv.org/abs/2410.05779) — dual-level graph retrieval, cited in Stage 2 rationale.
- [Karpathy's LLM Wiki gist](https://gist.github.com/karpathy) — pattern cited in Stage 3 rationale.

## Notes

This ADR does *not* supersede ADR-0010, 0012, or 0013. The concept layer,
vector index, and hybrid retrieval fusion all remain in force. Stage 1
extends the retrieval preprocessor; Stages 2 and 3 add new retrieval /
synthesis layers alongside hybrid. The existing layers keep being
scored against the same eval at every stage so regressions are visible.

The April 16 brainstorm's recommendation (Future D + eval gate) is not
discarded. It is being executed literally: the eval is the gate. The
gate said *recall is broken more than synthesis*, so the ordering
changes, and Future D becomes Stage 3 instead of Stage 1. The
philosophical direction — synthesis over pure retrieval — is preserved.
