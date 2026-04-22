# Nugget CLI for Cross-Creator Synthesis — Path 1 Before LightRAG Stage 2

**Status:** accepted

**Date:** 2026-04-22

**Decision Maker(s):** Daniel Zivkovic

## Context

The skill's actual end-goal isn't video summarization or search — it's **cross-creator nugget synthesis**: surfacing specific mental models, metaphors, warnings, clever workarounds, and business psychology by comparing how multiple creators discuss the same topic, with grounded attribution suitable for downstream client recommendations. Call it a "nugget radar."

Before this ADR the pipeline could retrieve evidence chunks ([`hybrid_search`](../../scripts/video_intel.py), BM25 + vector + RRF + Stage-1 query expansion per [ADR-0013](ADR-0013-hybrid-search-rrf-fusion.md) / [ADR-0017](ADR-0017-kb-layer-strategy.md)), but each query returned *raw chunks* — not synthesized briefings. Converting chunks to nuggets still required manual work: reading each result, extracting specifics, comparing across creators, assembling the narrative.

A private **Consultant-Grade Summary Prompt v4.1** — battle-tested for single-transcript extraction — was available as a foundation. It defines nuggets as mental models, metaphors, warnings, workarounds, and business psychology, with strict constraints (insight fidelity, traceability, "bold the so-what"). Mature extraction schema; the gap was multi-creator synthesis over retrieved chunks rather than a single transcript.

Three candidate paths for closing the gap from retrieval to nugget synthesis were evaluated:

| Path | Approach | Effort | Success probability |
|---|---|---|---|
| **1** | Adapt the v4.1 prompt for multi-chunk / multi-creator input and wire it to existing hybrid retrieval | ~4-8 hrs | ~90% |
| **2** | LightRAG Stage 2 per [ADR-0017](ADR-0017-kb-layer-strategy.md): graph storage + entity extraction + dual-level retrieval | 2-4 weeks | ~70% |
| **3** | Custom claim-evidence argument graph with AGREES_WITH / CONTRADICTS / REFINES relations | 3-6 months | ~40-50% |

Constraint: the skill is maintained solo alongside other projects. A research-grade build cannot precede value validation. Explicit guiding principle: pragmatic 80/20 — ship something that works now and can be shared with others before investing in graph-shaped infrastructure.

The 25-query golden dataset eval baseline stands at **1/25** ([ADR-0017](ADR-0017-kb-layer-strategy.md) 2026-04-19 baseline, confirmed unchanged after 2026-04-20 Stage 1 query expansion). Stage 2 LightRAG remains the [ADR-0017](ADR-0017-kb-layer-strategy.md) default next move once evidence justifies the cost.

## Decision

Ship **Path 1** as a new `nugget` CLI subcommand before any Stage 2 work.

Concretely:

- **New subcommand**: `python scripts/video_intel.py nugget "<query>"` — retrieves top-K hybrid-search excerpts, feeds them through a consultant-grade prompt, returns a multi-creator briefing.
- **New prompt**: [`prompts/nugget-brief.md`](../../prompts/nugget-brief.md) adapts the private v4.1 extraction schema for multi-chunk input. Adds four contracts not present in the single-transcript version:
  1. **Attribution is non-negotiable** — every claim cites `[creator @ HH:MM]` with video title
  2. **Divergence surfaces frame-of-reference differences** — name *why* creators disagree (underlying assumption), not just *what* they disagree on
  3. **Emergent Synthesis (1+1=3)** — dedicated output section for insights arising from comparing creators' positions that no single creator stated explicitly
  4. **Follow-Up Questions** reframed as client-context probes rather than actionable task lists
- **Reuses existing infrastructure**: `hybrid_search()` unchanged, `load_prompt()` helper unchanged, Gemini client factory unchanged, retry budgets unchanged. No new dependencies.
- **Skill-parity in the same change**: frontmatter triggers, triage table, intent table, how-to section all updated in [`skills/video-intel/SKILL.md`](../../skills/video-intel/SKILL.md) so the natural-language skill surface matches the new CLI surface.
- **Unit-tested at the pure contract**: [`tests/test_nugget_brief.py`](../../tests/test_nugget_brief.py) — 13 tests covering excerpt formatting (attribution fields, URL deep-linking with `&t=seconds`, URL omission when video_id missing, body whitespace handling) and template substitution (QUERY / NUM_CHUNKS / EXCERPTS replacement, empty-hits edge case, retrieval-order preservation, substitution-order documentation).
- **Smoke-tested end-to-end**: first real run checked in at [`examples/nugget-lightrag-vs-openbrain-architectural-tension.md`](../../examples/nugget-lightrag-vs-openbrain-architectural-tension.md) — documented query, documented pipeline, publicly inspectable.

Stage 2 (LightRAG) is **deferred, not cancelled**. See Gating Criteria below for the specific signals that will promote it.

## Consequences

### Positive

- **Delivers ~60-70% of the nugget-finding end-goal immediately** on the existing corpus (9,995 chunks across 9 channels as of 2026-04-22).
- **No new infrastructure.** No graph DB, no entity extraction pipeline, no Cypher. Adds ~100 lines of Python + 1 prompt file + 13 tests + doc updates.
- **Per-query cost is Flash-range**: ~$0.001–0.01 with `gemini-3-flash-preview` for 15-excerpt synthesis. Even 20+ queries per week is a rounding error on the monthly Gemini bill.
- **Attribution contract is enforced in the prompt**, not relied on from LLM defaults — "every claim cites `[creator @ HH:MM]` with video title" is explicit, and the smoke-test output verifies it holds.
- **Reusable eval infrastructure.** Retrieval quality ([ADR-0017](ADR-0017-kb-layer-strategy.md) 25-query eval) already measures the layer the nugget depends on. Synthesis quality can be qualitatively judged via the 1+1=3 section's ability to produce emergent insights (verified present in the smoke-test output).
- **Ownership preserved.** Prompts live in `prompts/` (editable, on maintainer's drive), output in `examples/` or stdout, no SaaS dependency beyond the Gemini API itself.
- **Retroactive spec is durable.** This ADR captures the three-paths framing so the *rejected* alternatives are traceable, not just the chosen one.

### Negative

- **Still query-time synthesis.** Every nugget brief re-retrieves and re-synthesizes from chunks. No compiled knowledge graph. Multi-hop concept traversal ("how does creator A's framework refine creator B's metaphor through creator C's warning?") remains weak.
- **Prompt quality bounds output quality.** If use cases drift from the v4.1 extraction schema (e.g., temporal evolution of ideas, network-density analysis), the prompt needs re-tuning.
- **No structural relation model.** AGREES_WITH / CONTRADICTS / REFINES show up in prose output but aren't first-class queryable relations. The 1+1=3 emergent synthesis is prompt-driven, not graph-driven.
- **Implicit dependency on retrieval quality.** With the 1/25 eval baseline, retrieval sometimes misses the right excerpts. Garbage in, garbage out: a nugget brief over wrong retrieval is worse than no brief.
- **Not yet measured against a held-out eval set.** Synthesis output quality is currently only smoke-tested qualitatively. If future usage reveals systematic gaps, an eval for synthesis quality will need designing (harder than the retrieval eval — synthesis is narrative, not ranked list).

## Alternatives Considered

- **Path 2: LightRAG Stage 2 (per [ADR-0017](ADR-0017-kb-layer-strategy.md))**
  - **Pros:** Graph traversal answers multi-hop queries natively. Incremental updates O(1) per doc. Matches the HKUDS LightRAG foundation described by the `engineerprompt` creator's 2024-10 and 2025-08 videos indexed in this corpus. Dual-level retrieval (entity+neighbors, global themes) built-in.
  - **Cons:** 2-4 weeks of focused work. Real failure modes: entity resolution errors, schema drift with monthly re-lensing, default entity types (`organization, person, location, event`) too narrow for concept-heavy research content. No eval data yet showing the per-query quality lift that would justify the investment.
  - **Status:** Deferred. [ADR-0017](ADR-0017-kb-layer-strategy.md) Stage 2 remains the plan once Path 1 surfaces signals showing graph is necessary.

- **Path 3: Custom claim-evidence argument graph**
  - **Pros:** ~90% of the nugget-finding goal if it works. Explicitly models disagreement structure. Debate briefings with typed relations would be more rigorous than prose synthesis.
  - **Cons:** Research project. No packaged library, custom extraction pipeline, community knowledge is thin. Incompatible with the solo-maintenance agility constraint.
  - **Status:** Rejected for v1. Parked indefinitely.

- **Use the private v4.1 Consultant-Grade prompt unchanged**
  - **Pros:** No prompt engineering work.
  - **Cons:** v4.1 was tuned for *single transcript* input. Multi-chunk multi-creator use requires attribution rules, explicit divergence / frame-of-reference surfacing, and a dedicated 1+1=3 section. Running v4.1 unchanged on concatenated chunks would lose creator attribution and miss the emergent-synthesis step — exactly the value this decision targets.
  - **Status:** Rejected. Adapted into [`prompts/nugget-brief.md`](../../prompts/nugget-brief.md) with the multi-creator-specific sections added.

## Gating Criteria (for Stage 2 promotion)

Stage 2 (LightRAG per [ADR-0017](ADR-0017-kb-layer-strategy.md)) promotes from *deferred* to *scheduled* when any of:

1. **Usage signal**: 3+ real queries return nugget briefs that materially miss multi-hop connections that exist in the corpus (verifiable by grep of relevant terms in transcripts).
2. **Eval signal**: 25-query golden dataset baseline remains ≤3/25 after any second retrieval-tuning pass, indicating the bottleneck is structural (graph-shaped) not lexical (query-shaped).
3. **Re-lensing signal**: the maintenance workflow starts running the same corpus through 2+ different conceptual lenses per month, revealing schema-free extraction as the cheaper pattern than per-lens prompt engineering of `nugget-brief.md`.

Until any of the above fires, Path 1 is the system. Path 2 stays deferred.

## Affects

Source files changed by this decision:

- [`scripts/video_intel.py`](../../scripts/video_intel.py) — `cmd_nugget()`, `build_nugget_prompt()`, `_format_nugget_excerpt()`, argparse subparser, dispatch entry (~100 lines net addition)
- [`prompts/nugget-brief.md`](../../prompts/nugget-brief.md) — new, ~85 lines
- [`skills/video-intel/SKILL.md`](../../skills/video-intel/SKILL.md) — frontmatter triggers, triage workflow table row, intent mapping table row, "Synthesize a consultant-grade nugget brief" How-to section, prompts list entry
- [`README.md`](../../README.md) — Usage section nugget block, Prompt Customization table row, link to `examples/nugget-lightrag-vs-openbrain-architectural-tension.md`
- [`tests/test_nugget_brief.py`](../../tests/test_nugget_brief.py) — new, 13 tests, 0.2s runtime
- [`examples/nugget-lightrag-vs-openbrain-architectural-tension.md`](../../examples/nugget-lightrag-vs-openbrain-architectural-tension.md) — new, smoke-test evidence

## Related Debt

- **Stage 2 signal collection**: as `nugget` is used on real queries, capture "falls-short" cases in session notes so the Gating Criteria above can fire on concrete evidence rather than intuition.
- **One pending concept extraction retry**: `engineerprompt/2025-11-12-this-is-the-fastest-voice-to-text-and-speaker-diarization-app` failed with `Server disconnected` during the 2026-04-22 scan. Non-blocking; next scan auto-retries.
- **Synthesis-quality eval**: if Path 1 usage proves valuable, design a small held-out set of "ideal nugget briefings" to guard against prompt regressions. Not worth investing in until Path 1's value is established.
- **Compound-engineering learning** (optional, later): `docs/solutions/` entry capturing the meta-pattern *"existing-prompt + existing-retrieval = 30-min implementation when prompt already fits data shape"* — defer until the pattern has been reused.

## Research References

- Private **Consultant-Grade Summary Prompt v4.1** — the foundational extraction schema this work adapts. Public adaptation lives at [`prompts/nugget-brief.md`](../../prompts/nugget-brief.md); the v4.1 original retains production-tuning details (specific few-shot examples, Q&A-segment emphasis, client-deliverable Next Steps format) not ported to the public version.
- Private session notes in `work/2026-04-22/` on: (01) the LightRAG-vs-OpenBrain-vs-Karpathy architectural comparison through Nate B Jones' ingest-vs-query timing prism, (02) which followed creators cover LightRAG (fact-check informing Stage 2 timing), (03) why the skill's semantic-concept data shape specifically needs a graph layer eventually, (04) schema-free vs schema-first research comparing LightRAG with Neo4j LLM Graph Builder, (06) implementation log with quality assessment of the smoke test.
- [ADR-0017](ADR-0017-kb-layer-strategy.md) — parent decision: staged KB-layer strategy with eval-gated Stage 2.
- [ADR-0013](ADR-0013-hybrid-search-rrf-fusion.md) — the retrieval layer Path 1 reuses.

## Notes

The value of Path 1 wasn't just speed — it was that the decision to *defer* Stage 2 is now explicit and conditioned on observable signals rather than drift. The skill ships a working nugget capability today and retains the option to escalate architecture only when evidence demands it. That's the spirit of [ADR-0017](ADR-0017-kb-layer-strategy.md)'s gating discipline applied one level up.

The smoke-test output produced an emergent *"Graph-over-SQL Hybrid → Audit-Ready Synthesis"* insight neither source creator stated directly — combining Nate B Jones' SQL-source framing with Chase's wiki-frontend framing. That quality of emergent synthesis is the clearest early indicator Path 1 already earns its cost. If subsequent usage keeps producing that quality of output, Stage 2 stays deferred longer than originally modeled in [ADR-0017](ADR-0017-kb-layer-strategy.md).
