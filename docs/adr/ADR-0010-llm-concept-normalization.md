# LLM-Powered Concept Normalization (Thesaurus Layer)

**Status:** accepted

**Date:** 2026-04-01 (updated 2026-04-02)

**Decision Maker(s):** Daniel Zivkovic

## Context

Mind maps produced by the pipeline use domain-normalized terminology (via the `mindmap-knowledge` prompt), but branch names still drift across videos. One video says "Agentic Design Patterns," another says "Agent-Centric Engineering," a third says "Multi-Agent Systems." All refer to the same concept. There is no cross-video index or canonical vocabulary — making it impossible to answer "which videos cover concept X?"

This is the vocabulary control problem: different sources use different words for the same idea. Manual taxonomies don't scale (20 years of evidence). Tag clouds create inconsistency. LLMs can judge semantic equivalence in context, making them the first practical tool for automated concept normalization.

After evaluating external tools (Neo4j GraphRAG, Cognee, LightRAG, Microsoft GraphRAG) and 3 rounds of CC-Codex architectural review, the decision is: **keep our custom extraction for Layers 1-2, complement with external tools for Layer 3 (retrieval) when benchmarks justify it.**

## Decision

### What we build (the thesaurus)

A second extraction pass reads each `.mindmap.md` and produces per-video `.concepts.json` with normalized concept entries. A master `taxonomy.json` is derived (not mutated) by aggregating all concept files. This is a **thesaurus** — canonical terms with synonyms and domain tags — not a taxonomy, ontology, or knowledge graph.

Key design decisions:

- **Stable concept IDs** (`ai_eng.multi_agent_orchestration`) — immutable once created, survives label renames.
- **Two-step pipeline** — extract per-video concepts atomically (Step 1), then rebuild taxonomy from all concept files (Step 2). No incremental state mutation.
- **taxonomy.json is derived** — always rebuildable from concept files. No stale counts, no `--force` corruption.
- **Status field** (`matched | new | uncertain`) — enables periodic human review of borderline merges without requiring upfront governance.
- **Duration-aware extraction** — concept count scales with video length (~1 concept per 3-5 minutes), not a fixed number.
- **In-memory taxonomy accumulation** — during batch processing, each video's new concepts are added to the in-memory taxonomy so subsequent videos can normalize against them.
- **Text-only Gemini calls** — reads mindmap markdown, not video. ~$0.001 per video.

### What we don't build (retrieval and relationships)

- **Entity relationships** (X enables Y, X replaces Z) — delegate to Cognee/LightRAG/Neo4j when needed.
- **Search/retrieval engine** — concepts are retrieval metadata over transcript chunk embeddings, not a standalone browsable index. Use Chroma, LanceDB, or external tools for the retrieval layer.
- **Multiple structured facets** (tasks, problems, tools, claims) — concepts are the ONE structured facet. Transcript embeddings handle unpredictable query intents. Add explicit facets only when benchmarks show a retrieval gap.

New subcommands: `concepts --backfill` (extract) and `taxonomy-build` (aggregate).

## Consequences

### Positive Consequences

- Cross-video concept correlation via `concept_id` (synonym resolution built in)
- Canonical vocabulary grows organically from the corpus, not from a committee
- Human-reviewable: plain JSON files
- Portable: `taxonomy.json` feeds into any future tool (Cognee, Neo4j, LightRAG)
- Cost: 50-400x cheaper than running external tools on the same content

### Negative Consequences

- Additional API cost per video (~$0.001, text-only Gemini call)
- LLM normalization is non-deterministic — re-extraction may produce slightly different results
- Concepts alone don't capture entity relationships or handle all retrieval intents
- Requires periodic human review of `uncertain` entries and duplicate concepts

## Alternatives Considered

- **Neo4j GraphRAG Builder:** Full pipeline with schema-guided extraction, entity resolution API, hybrid retrieval. Status: deferred as Layer 3 complement. Too much infrastructure for current scale.
- **Cognee:** Pipeline with 30+ connectors, entity extraction, KG construction, hybrid search. Status: deferred as Layer 3 complement. Best candidate when we need corpus-to-knowledge-graph. Does not handle video source.
- **LightRAG:** 70-90% of GraphRAG quality at 1/100th cost. Status: deferred as Layer 3 complement. Strong candidate for hybrid retrieval.
- **Microsoft GraphRAG:** Full community detection + hierarchical summarization. Status: rejected for now. $50-200 per 500 pages indexing cost.
- **Vector embeddings only:** No concept inventory. Rejected: cannot answer "list all concepts" or correlate by canonical term.
- **Full Ontology / OWL/SKOS:** Formal semantic web standards. Rejected: massive overhead, not justified for current scale.
- **7-facet structured extraction** (tasks, problems, tools, claims, etc.): Rejected: building an ontology by another name. Let embeddings handle unpredictable intents.

## Affects

- `scripts/video_intel.py` — `process_concepts()`, `build_taxonomy()`, `load_taxonomy()`, `find_mindmap_source()`, `call_gemini_text()`, `cmd_concepts()`, `cmd_taxonomy_build()`
- `prompts/concepts.md` — extraction prompt with duration-aware guidance
- `config.yaml` — `auto_concepts` key
- `tests/test_utils.py` — `TestLoadTaxonomy`, `TestFindMindmapSource`, `TestBuildTaxonomy`

## Research References

- Architecture decision analysis: `work/2026-04-02/02-concept-indexing-architecture-decision.md`
- Plan (CC-Codex consensus): `plans/tender-wobbling-penguin.md`
- Prompt comparison analysis: `work/2026-04-01/04-six-prompt-forensic-mcp-video.md`
- "Why LLMs Fail at Knowledge Graph Extraction" (Towards AI, Jan 2026)
- "The Extraction Debate: LLMs vs Fine-Tuning" (Graph Praxis, Jan 2026)
- "Graph RAG in 2026: What Works in Production" (Paperclipped, Mar 2026)
- "Knowledge Ontologies and Taxonomies Explained" (Knowledge Systems Authority, Mar 2026)
- RAG systematic review (MDPI, Dec 2025), OG-RAG (EMNLP 2025), HippoRAG 2 (ICML 2025)
