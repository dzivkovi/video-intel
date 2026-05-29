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
LightRAG's dual-level graph (local entity aggregation + global community
detection) directly addresses the vocabulary-mismatch failure via alias /
co-occurrence propagation, which is exactly the mechanism Stage 1's cheap
version approximates. Separate worktree, dedicated plan via
`/workflows:plan`, execution via `/workflows:work` with swarm-mode
parallel sub-agents for independent workstreams (ingestion adapter,
graph storage, retrieval integration, eval wiring). Exit criterion: same
as Stage 1 — record N/25 here.

### Decision rule between stages

"Stage 1 uplift is insufficient" is defined as: **< 10 of 25 queries
passing all gating metrics** *and* **the shape of remaining failures is
still vocabulary-mismatch-dominated** (per-query diagnostic trace on the
failed queries still shows query-token / content-token divergence as the
primary signal). If Stage 1 clears 10/25 *and* the remaining failures
shift to a synthesis-shaped mode (failed queries are `cross_channel_synthesis`
type with adequate recall but poor composition), skip Stage 2 and go
straight to Stage 3. The 10/25 threshold is calibrated against the
original April-16 brainstorm's "≥80% passes = ship wiki" rule, scaled
down to "a recall intervention earns its week of infrastructure only if
it closes at least a third of the gap."

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
  target of each stage, this ADR prevents that. *Partial overlap is
  acknowledged*: LightRAG's graph community detection may also surface
  co-occurrence signal that aids synthesis queries, so if the eval
  shows synthesis-query uplift during Stage 2 the attribution claim
  softens — a clean Stage-3 before/after score remains the tiebreaker.
- **Keeps ADR-0010 / ADR-0013 in force.** The concept / taxonomy layer
  and the hybrid-search RRF fusion are not being replaced. Stages 1–3
  are additions, not migrations.

### Negative Consequences

- **The eval dataset becomes a frozen contract.** Adding or changing
  golden queries mid-stream invalidates stage-over-stage comparison.
  Dataset changes now need ADR-grade justification, documented at the
  same level as a metric-threshold change. **Corrections are exempt:**
  fixing a typo in a query string, correcting a wrong `video_id`
  (grounding error), or adjusting a `key_phrase` to match actual
  transcript text are corrections — they remove measurement noise, not
  recalibrate difficulty — and do not require a new ADR. What needs an
  ADR is adding / removing queries, shifting a `query_type`, or changing
  a per-query threshold.
- **The dataset reflects a single author's framing.** The 25 queries
  were written by the same person now designing the KB-layer
  interventions. There is a latent risk that Stage 1–3 work subtly fits
  this dataset rather than generalizing to external users. Mitigations
  tracked in Related Debt: rotate in adversarial queries at stage
  boundaries, and solicit external raters once a KB layer is live.
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
| 2026-04-20 | Stage 1: Query expansion via taxonomy aliases | 1 / 25 | Q15 only. Headline unchanged, but **shape** shifted: 10/25 queries expanded at all (15 had no concept matches → taxonomy coverage is the real bottleneck); of the 10, Q04/Q06/Q13 dropped one failing metric each while Q03 gained one. Total failing metrics: 59 → 57. Sibling quality is noisy (verbose LLM-generated concept-extraction labels, e.g. *"Automated data fetching from external MCP servers"* as an alias of MCP) and hits the 12-cap on every matched query. **Per decision rule: `< 10 / 25` → proceed to Stage 2 (LightRAG).** JSONL diagnostics: `tests/evals/results/2026-04-20-{baseline,stage1}-expansion.jsonl`. |
| *Stage 2 TBD* | LightRAG layer | — | Justified by Stage 1 result. Build own knowledge graph instead of relying on noisy concept-extraction output. |
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

## Subsequent Refinements

> Added 2026-05-28. The staged-strategy decision above remains accepted.
> Two subsequent decisions refine — but do not invalidate — the trajectory.

### 1. Stage 2 promotion is now gated by ADR-0018's three named signals

[`ADR-0018`](ADR-0018-nugget-cli-cross-creator-synthesis.md) (accepted 2026-04-22)
shipped the `nugget` CLI for cross-creator synthesis as Path 1 against the
nugget-finding goal. The decision deferred Stage 2 LightRAG behind **three
specific signals**, any of which promotes it from *deferred* to *scheduled*:

1. **Usage signal** — 3+ real queries return nugget briefs that materially
   miss multi-hop connections that exist in the corpus (verifiable by grep).
2. **Eval signal** — 25-query golden dataset baseline remains ≤3/25 after
   any second retrieval-tuning pass.
3. **Re-lensing signal** — 2+ different conceptual lenses per month, where
   schema-free extraction is cheaper than per-lens prompt engineering of
   `nugget-brief.md`.

Until any of those fires, the original "proceed to Stage 2 because Stage 1
landed at 1/25" reasoning in §"Decision rule between stages" above is
**superseded** by ADR-0018's gate. The
[`docs/plans/2026-04-20-feat-kb-stage2-lightrag-plan.md`](../plans/2026-04-20-feat-kb-stage2-lightrag-plan.md)
plan is preserved with `status: deferred`.

### 2. The 2026-05-28 roadmap pivots the active direction to a structured intelligence layer

[`docs/brainstorms/2026-05-28-intelligence-layer-roadmap.md`](../brainstorms/2026-05-28-intelligence-layer-roadmap.md)
diagnosed that the user-experienced gap is **not** what the 1/25 eval
measures. The eval measures recall of similar passages; the example
questions the user actually wants answered (*"which AI tools have creators
moved away from in the last 90 days, and what did they move to"*, *"which
concepts went silent across the corpus that were weekly through April"*)
are **aggregate + polarity + causation** questions. Top-k passage retrieval
is the wrong primitive for them — vector embeddings collapse negation
(NevIR, EACL 2024), so even a perfect retriever cannot serve "no longer X"
queries.

The roadmap proposes a phased path:

- **Phase 0a** — manual `prompts/discovery-brief-window.md` validation
  ([shipped via PR #62 / issue #61](https://github.com/dzivkovi/video-intel/pull/62))
- **Phase 1** — DuckDB structured backbone, 6-node / 6-edge calming starter
  schema, silent-fading detector
- **Phase 1.5** (parallel) — Neo4j Graph Builder learning spike on 2–3
  known videos, written observations as the deliverable
  ([issue #63](https://github.com/dzivkovi/video-intel/issues/63))
- **Phase 2** — bidirectional Displacement + Magnet lenses, receipts-vs-synthesis contract, PROCEED/CAVEAT/REFUSE health signal
- **Phase 3** — 6-tuple stance schema as one sentence added to
  `prompts/concepts.md`
- **Phase 4** — top-20 stable-topic wiki pre-bake (later)
- **Phase F** — LightRAG, gated by the three signals above

The roadmap is a brainstorm, not yet an ADR. Specific decisions graduate
into ADRs as Phase 1.5 spike + Phase 2 produce evidence. The "let the
benchmark decide" rule still holds — Phase 0b will extend
`golden_dataset.yaml` with a `query_type: aggregate` cohort so the
new question class can be measured.

### What is unchanged

- ADR-0017's eval-as-gate discipline is intact.
- ADR-0010 (concept normalization), ADR-0012 (LanceDB vector), ADR-0013
  (hybrid RRF) all remain in force.
- The 25-query golden dataset stays a frozen contract.
- Stage 3 (Wiki) remains a real future option, partially realized
  already as the `nugget` CLI per ADR-0018.
