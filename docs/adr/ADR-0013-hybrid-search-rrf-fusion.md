# Hybrid Search via BM25 + Vector + Reciprocal Rank Fusion

**Status:** accepted

**Date:** 2026-04-03

**Decision Maker(s):** Daniel Zivkovic

## Context

ADR-0012 introduced vector search via LanceDB + Voyage AI embeddings. This handles ~80% of evidence queries — semantic similarity finds passages where the user's words don't match the transcript's words.

The remaining failure mode is **keyword precision**. Pure vector search struggles with:
- Exact identifiers: searching "Claude Mythos" returns semantically similar but wrong results
- Acronyms and product names: "MCP" diluted by general "protocol" mentions
- Title keywords: users often remember a video's exact title words, but vector search embeds meaning, not strings
- Quote-style queries: "code beats markdown" needs lexical match on those exact words

Production RAG systems converge on **hybrid search**: run BM25 keyword search and vector semantic search in parallel, merge with Reciprocal Rank Fusion (RRF). Benchmarks show ~62% precision (vector only) → ~84% precision (hybrid + RRF) on mixed query types (DEV.to production study, ~500K chunks on PostgreSQL).

### Current state

- Vector index exists (Voyage AI `voyage-4-large` embeddings, LanceDB `.lancedb/` directory)
- **FTS index already built** at indexing time (line 1377: `table.create_fts_index("text")`) but never queried
- All search uses `table.search(query_embedding).metric("cosine")` — pure vector only
- Score pipeline uses `_distance` (lower = better, range 0–2) throughout: dedup, filtering, display

### LanceDB already supports this

LanceDB 0.30.2 has native hybrid search with built-in RRF. The FTS index is already created during `index`. The infrastructure exists — only the query path needs changing.

## Decision

Replace **vector-only search** with **hybrid search (BM25 + vector + RRF)** in the `search --vector` command. Keep `--vector` as the CLI flag name (backward compatible) but change internal behavior.

### Implementation

1. **Hybrid query call** (replaces pure vector):
   ```python
   # Old: table.search(query_embedding).metric("cosine").limit(N)
   # New:
   table.search(query_type="hybrid").vector(query_embedding).text(query).limit(N)
   ```
   LanceDB automatically applies `RRFReranker(K=60)` when no reranker is specified.

2. **Score column migration** (`_distance` → `_relevance_score`):
   - RRF returns `_relevance_score` (higher = better), not `_distance` (lower = better)
   - With K=60, a doc ranked #1 in both lists scores `2/61 ≈ 0.033`
   - This is a fundamentally different scale than cosine similarity — not calibrated, rank-derived, corpus-dependent
   - All consumers refactored: `_dedup_by_video()`, `cmd_search()`, `--min-similarity` → `--min-relevance`

3. **FTS index enhancement**: Index over `["title", "text"]` (was `"text"` only). Video titles carry exact keywords users remember.

4. **Over-fetch preserved**: Keep `limit * 5` (or `max(50, limit * 5)`) before video-level dedup to give RRF fusion adequate headroom.

5. **Rename internal function**: `vector_search()` → `hybrid_search()` for clarity. External CLI flag `--vector` unchanged.

### What we're NOT doing (Phase 2)

- **Cross-encoder reranking**: `VoyageAIReranker(model_name="rerank-2.5-lite")` is available in the installed LanceDB, uses same `VOYAGE_API_KEY`. Add only if eval shows remaining precision gaps.
- **Query rewriting**: LLM-based query preprocessing (keyword extraction, pseudo-answer generation, entity enrichment). RRF already handles most query noise.
- **Cohere reranker**: Not needed. Stay in Voyage family if adding reranking later.

### Why RRF over score normalization

BM25 scores (term frequency) and cosine similarity scores live on completely different scales. Normalizing them introduces arbitrary scaling decisions. RRF sidesteps this by only considering **rank position**: `score(doc) = Σ 1 / (k + rank)`. A document ranked #1 in both lists always beats one ranked #5 in one and #1 in the other, regardless of raw score differences. K=60 is the near-optimal default from Cormack et al. (2009).

## Consequences

### Positive Consequences

- **Keyword precision**: Exact-match queries (acronyms, titles, product names) now work via BM25 component
- **No regression on semantic**: Vector component still handles paraphrases and conceptual matches
- **Simpler SKILL.md routing**: Agent doesn't need to classify "evidence vs discovery" — hybrid handles both lexical and semantic automatically
- **Zero new infrastructure**: FTS index already exists; LanceDB RRF is built-in
- **Backward compatible CLI**: `--vector` flag works identically from user perspective

### Negative Consequences

- **Score semantics change**: All code consuming `_distance` must migrate to `_relevance_score` (higher = better). Display changes from "Similarity: 0.258" to "Relevance: 0.033". Users familiar with cosine similarity scale need to recalibrate expectations.
- **RRF scores are not calibrated**: Cannot set universal thresholds. "Good" scores depend on corpus size, chunk distribution, and query overlap. The `--min-relevance` flag is less intuitive than `--min-similarity`.
- **FTS index rebuild required**: Adding `title` to FTS scope requires re-running `index` command.

## Alternatives Considered

- **Option:** Keep vector-only, improve concept search instead
  - **Pros:** No score migration, simpler code
  - **Cons:** Keyword precision gap persists; concept search is video-level, not passage-level
  - **Status:** rejected — hybrid solves both keyword and semantic in one query

- **Option:** Add BM25 as a separate `--keyword` mode
  - **Pros:** No score migration for existing vector path
  - **Cons:** User must choose between three modes; violates "one mode" goal
  - **Status:** rejected — hybrid subsumes both

- **Option:** Use Cohere reranker instead of RRF
  - **Pros:** Cohere has strong reranking models
  - **Cons:** Extra vendor dependency; LanceDB eval shows reranker choice is dataset-specific; no evidence our corpus needs it over RRF
  - **Status:** deferred to Phase 2, evaluate with corpus-specific eval set first

## Affects

Source files changed by this decision:

- `scripts/video_intel.py` (`vector_search()` → `hybrid_search()`, `_dedup_by_video()`, `cmd_search()`, `build_search_index()`, CLI arg definitions)
- `SKILL.md` (triage workflow, mode reference)
- `docs/search-internals.md` (score math section, display design)
- `evals/search-eval-queries.md` (score expectations for RRF scale)

## Related Debt

- Evaluate `VoyageAIReranker(model_name="rerank-2.5-lite")` against corpus eval set — add if precision still lacking
- Query rewriting (HyDE / keyword extraction) — test whether LLM preprocessing improves retrieval on noisy user queries
- RRF K-value tuning — evaluate K=40,50,60,70,80 against 25-query eval set

## Research References

- [Hybrid Search for RAG: BM25 + Vector (ShinRAG)](https://www.shinrag.com/blog/hybrid-search-rag-bm25-vector-semantic-keyword) — architecture overview
- [Building Hybrid Search with pgvector + RRF (DEV.to)](https://dev.to/lpossamai/building-hybrid-search-for-rag-combining-pgvector-and-full-text-search-with-reciprocal-rank-fusion-6nk) — ~62% → ~84% precision numbers
- [Query rewriting strategies (Elasticsearch Labs)](https://www.elastic.co/search-labs/blog/query-rewriting-llm-search-improve) — keyword enrichment, pseudo-answer, tested with Claude 3.5 Sonnet
- [Hybrid Search + Reranking Playbook (OptyxStack)](https://optyxstack.com/rag-reliability/hybrid-search-reranking-playbook) — production patterns
- [LanceDB Hybrid Search docs](https://docs.lancedb.com/search/hybrid-search) — native API reference
- [Voyage AI Rerankers](https://docs.voyageai.com/docs/reranker) — rerank-2.5 / rerank-2.5-lite models
- [Cormack et al. 2009 — RRF paper](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) — K=60 near-optimal
- `work/2026-04-03/04-hybrid-search-research.md` — session research notes
- `plans/kind-purring-snowflake.md` — planning doc with Codex review corrections

## Notes

The FTS index has been built alongside the vector index since ADR-0012 (line 1377) but was never used. This ADR activates it. Users must re-run `video_intel.py index` after this change to include `title` in the FTS scope.

RRF score ranges (`~0.016–0.033`) are useful as intuition but must not become thresholding contracts. For evaluation, use qualitative ranking quality ("top result directly answers query") not numeric cutoffs.
