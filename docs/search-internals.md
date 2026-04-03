# Search Internals

Operational reference for the two search modes in video-intel. This document captures **how search works**, not why we chose these tools (see [ADR-0012](adr/ADR-0012-vector-search-lancedb-voyage.md) for that).

Last verified: 2026-04-02

## Two Search Modes

| Mode | Command | What it searches | API calls | Cost |
|------|---------|-----------------|-----------|------|
| **Concept** (default) | `search "query"` | `taxonomy.json` labels + aliases | None | Free |
| **Vector** | `search "query" --vector` | Transcript chunks via embeddings | 1 Voyage embed call per query | ~$0.02/M tokens |

Concept search is keyword matching against the taxonomy. The rest of this document covers **vector search**.

## Vector Search Pipeline

### 1. Indexing (one-time, `video_intel.py index`)

```
Transcripts → chunk_transcript() → Voyage embed → LanceDB table
```

- **Chunking:** Each transcript is split into groups of 5 consecutive entries (speech lines + SCREEN blocks). Each chunk preserves temporal coherence.
  - Source: `chunk_transcript()` in `video_intel.py:1194`
  - Chunk size: 5 entries (hardcoded)

- **Embedding:** All chunks embedded with `voyage-4-large`, `input_type="document"`, 1024 dimensions.
  - Source: `build_search_index()` in `video_intel.py:1303`
  - Batched in groups of 10, with rate-limit backoff

- **Storage:** LanceDB file-based table at `{output_dir}/.lancedb/transcript_chunks`
  - IVF cosine index created when >= 256 chunks exist
  - Full-text index also created on the `text` column

- **Current corpus:** ~3,673 chunks across all indexed videos

### 2. Querying (`search "query" --vector`)

```
Query → Voyage embed (lite) → LanceDB cosine search → dedup by video → filter → display
```

Step by step:

1. **Embed query** with `voyage-4-lite`, `input_type="query"`, 1024 dimensions
   - Source: `vector_search()` in `video_intel.py:1409`
   - Cross-model retrieval is supported: Voyage 4 series models share an embedding space ([Voyage 4 blog](https://blog.voyageai.com/2026/01/15/voyage-4/))

2. **LanceDB search** with cosine metric, fetches `limit * 5` raw candidates
   - Source: `video_intel.py:1413`
   - Returns `_distance` column (cosine distance)

3. **Dedup by video** — keep only the best-scoring chunk per `video_id`
   - Source: `_dedup_by_video()` in `video_intel.py:1439`
   - "Best" = lowest cosine distance

4. **Filter by `--min-similarity`** — drop results below threshold
   - Source: `cmd_search()` in `video_intel.py:1580-1582`

5. **Display** top `--limit` results with chunk text and similarity score
   - Default: up to 3000 chars per chunk, newlines preserved (speaker turns, SCREEN blocks)
   - `--preview`: 200 chars, flattened to single line

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

Vector results are dense (up to 3000 chars each with full evidence). Concept results are compact (paths and labels). Different output density justifies different defaults:

- **Vector: 10** — optimizes for relevance density. Weak-tail results (ranks 11-20) add noise without improving reasoning.
- **Concept: 20** — output is compact enough that more results cost almost nothing.

Users override with `--limit N` when they know a topic spans many channels.

## Score Math

This is the conversion chain, verified against documentation:

### LanceDB returns: cosine distance

> "For cosine similarity, distances range from 0 (identical) to 2 (maximally dissimilar)."
> — [LanceDB vector index docs](https://docs.lancedb.com/indexing/vector-index)

| `_distance` | Meaning |
|-------------|---------|
| 0 | Vectors point in the same direction (identical) |
| 1 | Vectors are orthogonal (unrelated) |
| 2 | Vectors point in opposite directions |

### Code converts to: cosine similarity

```python
similarity = 1 - hit["distance"]    # video_intel.py:1591
```

This gives standard cosine similarity:

| Cosine similarity | Meaning |
|-------------------|---------|
| 1.0 | Identical direction |
| 0.0 | Orthogonal / unrelated |
| -1.0 | Opposite direction |

### Why the conversion is valid

Voyage AI embeddings are normalized to length 1 ([Voyage quickstart](https://docs.voyageai.com/docs/quickstart-tutorial)):

> "Voyage embeddings are normalized to length 1, therefore dot-product and cosine similarity are the same."

For unit vectors: `cosine_distance = 1 - cosine_similarity`, so `similarity = 1 - distance` is mathematically correct.

## Score Interpretation

### What the docs say

- Higher similarity = closer match ([Voyage quickstart](https://docs.voyageai.com/docs/quickstart-tutorial): "A bigger cosine similarity means the two vectors are closer.")
- The range is -1.0 to 1.0 (mathematical property of cosine similarity)
- **No vendor publishes "good/bad" score thresholds.** Absolute scores depend on corpus, query style, chunk length, and embedding model.

### What the docs do NOT say

Neither Voyage AI nor LanceDB documentation defines expected score ranges for real retrieval tasks. Statements like "0.10+ is good" or "below 0.03 is noise" are **corpus-specific observations, not universal rules.**

### Initial observations from this corpus (empirical, not guaranteed)

Based on testing "Claude skills explained" against 3,673 chunks (April 2026):

| Similarity range | Observed behavior | Example |
|-----------------|-------------------|---------|
| ~0.10+ | On-topic: videos directly about the query subject | Skills strategy/creation videos |
| ~0.05-0.10 | Adjacent: videos that discuss the topic in context | Skills-in-practice, related tools |
| ~0.01-0.05 | Weak: tangential mentions, broad topic overlap | General Claude Code update videos |
| < 0.01 | Noise: no meaningful semantic relationship | Unrelated content |

**These thresholds are local heuristics, not documented properties of Voyage or LanceDB.** They will shift as the corpus grows, chunk sizes change, or embedding models are updated. Validate against representative queries before hardcoding.

### Why scores are low in absolute terms

Short queries (2-4 words) matched against long passages (200+ word transcript chunks mixing speech, screen content, and UI descriptions) produce low absolute cosine similarity even for relevant matches. This is a general property of asymmetric retrieval, not a bug. **The ranking quality matters more than absolute values.**

## Current Defaults

| Parameter | Default | Source | Notes |
|-----------|---------|--------|-------|
| `--limit` | 10 (vector) / 20 (concept) | `video_intel.py:1574` | Per-mode default; override with `--limit N` |
| display cap | 3000 chars (default) / 200 chars (`--preview`) | `video_intel.py:1598` | Full chunk with newlines; `--preview` flattens to single line |
| `--min-similarity` | 0.0 | `video_intel.py:1731` | No floor — all positive matches shown |
| chunk size | 5 entries | `chunk_transcript()` | Hardcoded |
| doc model | `voyage-4-large` | `VOYAGE_DOC_MODEL` | 1024 dims, $0.12/M tokens |
| query model | `voyage-4-lite` | `VOYAGE_QUERY_MODEL` | 1024 dims, $0.02/M tokens |
| LanceDB index | IVF cosine | `video_intel.py:1376` | Only if >= 256 chunks |
| candidate multiplier | 5x | `video_intel.py:1413` | Fetches `limit * 5` raw hits before dedup |

## Tuning Levers

In order of impact and ease:

1. **`--min-similarity`** — raise from 0.0 to filter noise tail. Requires empirical testing across representative queries.
2. **`--limit`** — already split per mode (10 vector, 20 concept). Raise vector default if benchmarking shows >10 useful hits regularly.
3. **Chunk size** — larger chunks = more context per hit but diluted embeddings. Smaller = sharper matches but fragments.
4. **Query model** — `voyage-4-lite` (current) vs `voyage-4-large` for queries. Same embedding space, potentially different retrieval quality. Benchmark empirically (see ADR-0012 Related Debt).
5. **Reranking** — add a Voyage reranker pass over top-N candidates. Not implemented.

## Rebuild Procedure

The index is a derived artifact. To rebuild from scratch:

```bash
python scripts/video_intel.py index          # full rebuild
python scripts/video_intel.py index --force   # drop + rebuild
python scripts/video_intel.py index --channel natebjones  # single channel
```

Rebuilding is safe and idempotent. The `.lancedb/` directory can be deleted at any time.

## Related Documents

- [ADR-0012](adr/ADR-0012-vector-search-lancedb-voyage.md) — why LanceDB + Voyage (decision record)
- [ADR-0010](adr/ADR-0010-llm-concept-normalization.md) — concept layer (what concept search operates on)
- [Voyage AI embeddings docs](https://docs.voyageai.com/docs/embeddings) — API reference, model compatibility
- [Voyage 4 blog](https://blog.voyageai.com/2026/01/15/voyage-4/) — shared embedding spaces, asymmetric retrieval
- [LanceDB vector search](https://docs.lancedb.com/search/vector-search) — distance metrics, range filtering
- [LanceDB vector index](https://docs.lancedb.com/indexing/vector-index) — cosine distance range definition
