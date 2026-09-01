# Search Internals

Operational reference for the two search modes in video-intel. This document captures **how search works**, not why we chose these tools (see [ADR-0012](adr/ADR-0012-vector-search-lancedb-voyage.md) and [ADR-0013](adr/ADR-0013-hybrid-search-rrf-fusion.md) for that).

Last verified: 2026-04-03

## Two Search Modes

| Mode | Command | What it searches | API calls | Cost |
|------|---------|-----------------|-----------|------|
| **Concept** (default) | `search "query"` | `taxonomy.json` labels + aliases | None | Free |
| **Hybrid** | `search "query" --vector` | Transcript chunks + video titles via BM25 + vector + RRF | 1 Voyage embed call per query | ~$0.02/M tokens |

Concept search is keyword matching against the taxonomy. The rest of this document covers **hybrid search**.

## Hybrid Search Pipeline

### 1. Indexing (one-time, `video_intel.py index`)

```
Transcripts → chunk_transcript() → Voyage embed → LanceDB table + FTS indexes
```

- **Chunking:** Each transcript is split into groups of 5 consecutive entries (speech lines + SCREEN blocks). Each chunk preserves temporal coherence.
  - Source: `chunk_transcript()` in `video_intel.py`
  - Chunk size: 5 entries (hardcoded)

- **Embedding:** All chunks embedded with `voyage-4-large`, `input_type="document"`, 1024 dimensions.
  - Source: `build_search_index()` in `video_intel.py`
  - Batched in groups of 128, with rate-limit backoff

- **Storage:** LanceDB file-based table at `{output_dir}/.lancedb/transcript_chunks`
  - IVF cosine index created when >= 256 chunks exist
  - **Two FTS indexes:** one on `text` column, one on `title` column (BM25 keyword search)

- **Current corpus:** ~3,673 chunks across all indexed videos

### 2. Querying (`search "query" --vector`)

```
Query → Voyage embed (lite) → LanceDB hybrid (BM25 + vector + RRF) → select top-N videos → filter → display
```

Step by step:

1. **Embed query** with `voyage-4-lite`, `input_type="query"`, 1024 dimensions
   - Cross-model retrieval is supported: Voyage 4 series models share an embedding space ([Voyage 4 blog](https://blog.voyageai.com/2026/01/15/voyage-4/))

2. **LanceDB hybrid search** — runs BM25 keyword search and vector search in parallel, merges with Reciprocal Rank Fusion (RRF, K=60)
   - BM25 searches over `title` and `text` columns (FTS indexes)
   - Vector search uses cosine metric over embeddings
   - RRF merges both ranked lists: `score(doc) = Σ 1/(K + rank)`
   - Fetches `max(50, limit * 5)` raw candidates for dedup headroom

3. **Select the top-`limit` videos** — `_select_hits_by_video()` in `video_intel.py`
   - Ranks videos by their best chunk's `_relevance_score` and keeps the top `limit`
   - `dedup_by_video=True` (the default, and what every product caller uses) returns
     one chunk per video: that video's best-scoring chunk. `_dedup_by_video()` is
     retained as a thin alias of this mode.
   - `dedup_by_video=False` returns every chunk of those same videos, grouped by
     video, best chunk first inside each video. **The video set and video order
     are identical either way** — the switch only decides how many windows per
     video come back. Only the retrieval eval uses it (issue #190): a golden query
     can expect several distinct timestamp windows inside one video, and
     one-chunk-per-video capped such a query below its own threshold no matter how
     good retrieval was.
   - Do not flip the default. `search --vector` prints one line per video and
     `nugget` bills Gemini per chunk, so both are length-sensitive; the contract is
     locked by `tests/test_hybrid_search_dedup.py::TestProductionCallSitesKeepDedup`.
   - "Every chunk" means every chunk in the fetched candidate pool
     (`max(50, limit * 5)` rows), not every chunk in the index. A window whose chunk
     never entered that pool is a genuine ranking failure, and the pool is
     deliberately left at the product's own depth so the eval keeps measuring what
     the product would actually surface.

4. **Filter by `--min-relevance`** — drop results below threshold

5. **Display** top `--limit` results with chunk text and relevance score
   - Default: up to 3000 chars per chunk, newlines preserved (speaker turns, SCREEN blocks)
   - `--preview`: 200 chars, flattened to single line

## Query Expansion (Stage 1)

As of 2026-04-20 ([ADR-0017](adr/ADR-0017-kb-layer-strategy.md)), hybrid search applies a taxonomy-driven query preprocessor before the Voyage embed and BM25 calls. The goal is to bridge user vocabulary to creator vocabulary — the query "browser automation" is extended with creator-specific terms like "Capybara" when the taxonomy connects them, so BM25 can land exact-token hits it would otherwise miss.

### Algorithm

`expand_query_via_taxonomy(query, taxonomy)` in `scripts/video_intel.py`:

1. For each concept in `taxonomy.json`, scan the query for canonical-label-first, then aliases.
2. Matching uses a **punctuation-aware boundary**: `(?:^|(?<=[^\w]))TERM(?=$|[^\w])`, case-insensitive. This matches aliases that stdlib `\b` misses — `C++`, `.NET`, `(MCP)`, `k3s` — because `\b` requires a word character on at least one side.
3. When a canonical or alias matches, its *siblings* (the canonical plus the other aliases) are appended to the end of the query string. The original query stays at the prefix so BM25 TF/IDF still favors the user's terms.
4. Two guardrails limit noise and dilution:
   - `MIN_ALIAS_LEN = 2` — single-character aliases are skipped.
   - `MAX_ALIAS_ADDITIONS = 12` — at most 12 siblings appended per query (embedding-dilution cap).
5. Sibling deduplication is case-insensitive across all matched concepts.

The expanded string is sent to **both** `.text()` (BM25 FTS) and `vo.embed()` (Voyage query embed). Expanding BM25-only would miss the vector-side bridge; the shared-input approach is the simplest v1. A future v2 may split the paths if eval shows vector-side regression.

### Toggle and diagnostics

```bash
# Default: expansion ON
python scripts/video_intel.py search "MCP" --vector

# Disable expansion (A/B diagnostic)
python scripts/video_intel.py search "MCP" --vector --no-expand

# Show the expansion decision as it runs
python scripts/video_intel.py --log-level info search "MCP" --vector
# INFO  query_expansion input='MCP' matched=1 added=['Model Context Protocol', ...]
```

Per-query expansion records are also available structurally: pass `return_diagnostics=True` to `hybrid_search()` and it returns `(hits, {"expand_enabled", "original_query", "expanded_query", "matches"})`. The eval harness uses this to write `tests/evals/results/<run_tag>-expansion.jsonl` on every run.

### Worked example

- Query: `"what is MCP good for"`
- Taxonomy concept has canonical `"Model Context Protocol"` and aliases `["MCP", "mcp-server"]`
- Expanded query: `"what is MCP good for Model Context Protocol mcp-server"`
- Match record: `{"concept_id": "mcp", "matched_term": "MCP", "added": ["Model Context Protocol", "mcp-server"]}`

### Scope

Stage 1 touches **only** `hybrid_search()`. The concept-search path (`search_corpus()`) already does its own alias-aware retrieval with a `_match_score` gate and is intentionally untouched. ADR-0017's eval gate measures `--vector` only, so scoping to hybrid is sufficient.

## Display Design

### Why full chunks instead of previews

The previous 200-char preview forced a triage-then-read workflow: scan previews, pick files, read full transcripts. This was redundant — the matched chunk already contains the evidence.

Showing up to 3000 chars per chunk eliminates most follow-up transcript reads. The evidence is in the search output directly.

### Chunk size distribution (measured, 3,673 chunks)

| Metric | Chars |
|--------|-------|
| Median | 1,111 |
| p90 | 2,697 |
| p99 | 7,154 |
| Max | 43,132 |

The 3000-char cap covers ~90% of chunks completely. Outliers (>30K) are all `[00:00]` first-chunks from videos with long intro monologues — rare, and truncated with a `[see source]` note.

### Cost comparison

| Approach | Chars | Videos covered |
|----------|-------|----------------|
| 10 previews (200 chars) | ~2K | 10, but need follow-up reads |
| 10 full chunks (p90) | ~27K | 10, evidence included |
| 3 full transcript reads (old follow-up) | ~44K | 3 |

10 full chunks costs less than reading 3 transcripts but covers 3x more videos with evidence included.

### Why per-mode limits

Hybrid results are dense (up to 3000 chars each with full evidence). Concept results are compact (paths and labels). Different output density justifies different defaults:

- **Hybrid (--vector): 10** — optimizes for relevance density. Weak-tail results add noise without improving reasoning.
- **Concept: 20** — output is compact enough that more results cost almost nothing.

Users override with `--limit N` when they know a topic spans many channels.

## Score Math

### RRF Relevance Score

Hybrid search uses Reciprocal Rank Fusion (RRF) to merge BM25 and vector results. The score is:

```
score(doc) = Σ 1 / (K + rank_i)
```

Where K=60 (default, from Cormack et al. 2009) and rank_i is the document's position in each retrieval list.

| Scenario | RRF Score |
|----------|-----------|
| Doc ranked #1 in both BM25 and vector | `2/61 ≈ 0.0328` |
| Doc ranked #1 in one list only | `1/61 ≈ 0.0164` |
| Doc ranked #10 in both lists | `2/70 ≈ 0.0286` |

### Why RRF instead of score normalization

BM25 scores (term frequency) and cosine similarity scores live on completely different scales. Normalizing them introduces arbitrary scaling decisions. RRF only considers **rank position**, not raw scores. This makes it robust across different corpus sizes and query types.

### Score interpretation

**RRF scores are rank-derived and corpus-dependent — not calibrated.** Unlike cosine similarity (which has a defined mathematical range of -1 to 1), RRF scores depend on how many retrieval lists contribute and the K value. Do not set universal thresholds.

For evaluation, use qualitative ranking quality ("top result directly answers the query intent"), not numeric cutoffs.

### Why BM25 + vector is better than vector alone

| Query type | Vector only | Hybrid (BM25 + vector) |
|------------|------------|----------------------|
| "code beats markdown" (title match) | Semantic match on meaning | **BM25 hits exact title words** |
| "how long before helium runs out" (fact) | Semantic match on topic | **BM25 matches "helium" in title** |
| "MCP" (acronym) | Diluted by general "protocol" | **BM25 exact match on "MCP"** |
| "what did they say about context windows" (semantic) | Good match | Good match (vector contributes) |

Production RAG benchmarks show ~62% → ~84% retrieval precision when adding BM25 + RRF to vector search.

## Current Defaults

| Parameter | Default | Notes |
|-----------|---------|-------|
| `--limit` | 10 (hybrid) / 20 (concept) | Per-mode default; override with `--limit N` |
| display cap | 3000 chars (default) / 200 chars (`--preview`) | Full chunk with newlines; `--preview` flattens |
| `--min-relevance` | 0.0 | No floor — all results shown |
| chunk size | 5 entries | Hardcoded in `chunk_transcript()` |
| doc model | `voyage-4-large` | 1024 dims, $0.12/M tokens |
| query model | `voyage-4-lite` | 1024 dims, $0.02/M tokens |
| RRF K | 60 | LanceDB default, near-optimal per Cormack et al. |
| FTS columns | `title`, `text` | Two separate FTS indexes |
| candidate multiplier | `max(50, limit * 5)` | Fetched before video selection; also bounds how many windows per video `dedup_by_video=False` can expose |

## Tuning Levers

In order of impact and ease:

1. **`--min-relevance`** — raise from 0.0 to filter noise tail. RRF scores are rank-based, so thresholds are corpus-specific.
2. **`--limit`** — already split per mode (10 hybrid, 20 concept). Raise hybrid default if benchmarking shows >10 useful hits regularly.
3. **Chunk size** — larger chunks = more context per hit but diluted embeddings. Smaller = sharper matches but fragments.
4. **RRF K value** — K=60 is default. Lower K weights top results more heavily; higher K is more uniform. Test K=40,50,60,70,80 against eval set.
5. **Cross-encoder reranking** — `VoyageAIReranker(model_name="rerank-2.5-lite")` available in installed LanceDB. Uses same VOYAGE_API_KEY. Add only if eval shows remaining precision gaps.

## Rebuild Procedure

The index is a derived artifact. To rebuild from scratch:

```bash
python scripts/video_intel.py index          # full rebuild
python scripts/video_intel.py index --force   # drop + rebuild
```

Rebuilding is safe and idempotent. The `.lancedb/` directory can be deleted at any time.

> **Do NOT run `index --channel X` (issue #183).** Despite the flag's name and its
> help text, it is not scoped: `build_search_index` filters *collection* to channel X
> and then does an unconditional `create_table(..., mode="overwrite")`, so it replaces
> the whole shared table with only that channel's chunks. Roughly 98% of the index
> disappears, the command still prints `Indexed N chunks` and exits 0, and every later
> search silently returns single-channel results. A *mistyped* channel name is harmless
> (the `if not all_records` guard returns before the overwrite) - only a valid one
> destroys the index. Until #183 lands, the only safe rebuild is a full `index`.
>
> This section previously listed `index --channel natebjones  # single channel` as a
> normal rebuild option. It was wrong, and CLAUDE.md points every agent here before
> touching search, so the bad advice had reach. Removed 2026-08-31.

## Related Documents

- [ADR-0013](adr/ADR-0013-hybrid-search-rrf-fusion.md) — why hybrid search with RRF (decision record)
- [ADR-0012](adr/ADR-0012-vector-search-lancedb-voyage.md) — why LanceDB + Voyage (decision record)
- [ADR-0010](adr/ADR-0010-llm-concept-normalization.md) — concept layer (what concept search operates on)
- [LanceDB hybrid search](https://docs.lancedb.com/search/hybrid-search) — hybrid query API
- [Voyage AI embeddings docs](https://docs.voyageai.com/docs/embeddings) — API reference, model compatibility
- [Voyage 4 blog](https://blog.voyageai.com/2026/01/15/voyage-4/) — shared embedding spaces, asymmetric retrieval
- [Cormack et al. 2009](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) — RRF paper, K=60 near-optimal
