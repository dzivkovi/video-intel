# Vector Search via LanceDB + Voyage AI Embeddings

**Status:** accepted

**Date:** 2026-04-02

**Decision Maker(s):** Daniel Zivkovic

## Context

Phase 1 search (commit 829efd9) provides deterministic keyword matching over taxonomy concepts. This handles ~80% of queries: "which videos cover agent skills?" matches concept labels and aliases in `taxonomy.json`.

The remaining 20% are **evidence queries** that keyword matching can't handle:
- "What did someone say about the 150-line skill limit?"
- "Find videos discussing permission problems"
- "Which creator explained why memory gets noisy over sessions?"

These queries require **semantic similarity** over actual transcript text, not just concept labels. The user needs to find specific passages, not just which videos exist.

The corpus has 350+ videos with transcripts totaling ~1M tokens. Reading the entire corpus per query is impractical (thousands of tool calls, massive context consumption).

## Decision

Add a **vector search layer** using LanceDB (file-based vector database) and Voyage AI embeddings (asymmetric retrieval).

### Architecture

1. **Chunking:** Split each transcript into groups of 5 consecutive entries (speech lines + SCREEN blocks). Each chunk preserves temporal coherence and carries concept_ids from the video's `concepts.json`.

2. **Embedding:** Use Voyage AI's asymmetric retrieval pattern:
   - **Documents** embedded once with `voyage-4-large` (1024 dims, best retrieval quality, $0.12/M tokens)
   - **Queries** embedded per-search with `voyage-4-lite` ($0.02/M tokens)

3. **Storage:** LanceDB stores embeddings in a `.lancedb/` directory inside the output dir. File-based, no server, rebuildable from transcripts at any time.

4. **Search:** Two modes via the existing `search` command:
   - `search "query"` — concept search (default, keyword matching, no API calls)
   - `search "query" --vector` — semantic search over transcript chunks

### Why Voyage AI (not Gemini Embedding)

Google offers two current embedding models — they sound like versions of the same thing, but serve different purposes:

- **gemini-embedding-001** — text-only, stable, 2,048-token context, 128–3072 dims, $0.15/M tokens ($0.075/M batch)
- **gemini-embedding-2-preview** — multimodal (text/image/video/audio/PDF), 8,192-token context, 128–3072 dims, $0.20/M tokens (text), up to $12/M for video

Our use case is **text retrieval over transcripts**, so the fair comparison is `voyage-4-large` vs `gemini-embedding-001`:

| Criterion | Voyage 4 Large | Gemini Embedding 001 |
|-----------|---------------|---------------------|
| Retrieval quality | +8.2% vs Gemini on RTEB benchmark¹ | 68.2 MTEB overall² |
| Dimensions | 1024 | 3072 (3x storage) |
| Context window | 32,000 tokens | 2,048 tokens |
| Asymmetric retrieval | Yes (large for docs, lite for queries) | No |
| LanceDB integration | Native | Native |
| Cost (documents) | $0.12/M tokens | $0.15/M ($0.075/M batch) |
| Cost (queries) | $0.02/M tokens (lite) | Same as documents |
| Free tier | 200M tokens | Generous |

¹ RTEB general-purpose retrieval benchmark (Voyage, Jan 2026). ² MTEB multilingual benchmark (Google, Jul 2025). These are different benchmarks — no independent apples-to-apples comparison exists.

Voyage's asymmetric retrieval is the decisive factor: embed documents once at maximum quality, query cheaply at $0.02/M. The 32K context window vs Gemini's 2K also matters — our transcript chunks can be longer without splitting. Anthropic points users to Voyage as their embedding provider.

### What about gemini-embedding-2-preview?

The multimodal model (`gemini-embedding-2-preview`) is a separate question. It can embed raw video, audio, and images — not just text. If we ever wanted to retrieve based on what's *shown* in a video (not just what was said), this model would be relevant. But:

- We already extract text via Gemini's multimodal generation (transcripts + screen content). The embeddings layer operates over that extracted text.
- At $0.20/M for text alone (more expensive than both Voyage and Gemini 001), there's no cost advantage.
- No independent benchmark compares it against Voyage for text retrieval quality.
- It's still in preview — not yet stable for production indexing.

**Status:** not evaluated for current use case. Worth revisiting if the skill moves toward raw video retrieval.

### Embedding landscape (April 2026)

`voyage-4-large` is new enough (Jan 2026) that no independent cross-vendor benchmark covers all contenders. The table below summarizes what each model is best at and why, based on vendor-published results:

| Model | Strongest on | video-intel relevance |
| --- | --- | --- |
| `voyage-4-large` | Retrieval (RTEB) — leads all 29 datasets, +8.2% over Gemini 001 | **Our pick.** Transcript chunk retrieval is the core use case |
| `gemini-embedding-001` | Broad text embedding (MTEB ~68.2, top multilingual) | Strong general-purpose, but not retrieval-specialized |
| `gemini-embedding-2-preview` | Multimodal — first Google model to embed video/audio/images | Only relevant for raw media retrieval, not extracted text |
| `voyage-4` / `voyage-4-lite` | Cost/latency within the Voyage family | Query-time embeddings (lite at $0.02/M) |
| `bge-m3` | Best open-source, long context, self-hostable | Deferred as offline fallback |

Key caveat: RTEB (Voyage's retrieval eval) and MTEB (broad embedding tasks) are **different benchmarks from different organizations**. The +8.2% figure is Voyage's own claim on their own benchmark. No independent test covers all five models above on the same retrieval task.

The choice reads: *"retrieval-specialized managed embeddings over Google's general-purpose text leader"* — not *"Google embeddings are bad."*

### Why LanceDB (not Chroma, FAISS, Pinecone)

- **File-based** — no server, no Docker, same deployment pattern as all other artifacts
- **Cognee-compatible** — Cognee's default vector store is LanceDB
- **Hybrid search** — supports vector + full-text + SQL filtering in one query
- **Rebuildable** — the `.lancedb/` directory is a derived artifact, delete and re-index at any time

## Consequences

### Positive Consequences

- Evidence queries now work: "what did they say about the 150-line limit?" returns ranked transcript passages with timestamps
- Concept-tagged chunks enable hybrid filtering: semantic search + concept constraint
- Index is rebuildable — no risk of data loss, just re-run `index`
- Asymmetric embedding minimizes per-query cost ($0.02/M vs $0.12/M)
- Entire corpus indexes for ~$0.12 (or free under 200M token tier)
- File-based storage follows existing artifact pattern (no infrastructure)

### Negative Consequences

- Two new dependencies: `lancedb`, `voyageai` (optional, gated behind `pip install 'video-intel[vector]'`)
- Requires `VOYAGE_API_KEY` environment variable
- Free tier without payment method has 3 RPM / 10K TPM rate limits — initial indexing is slow (~25 min for 768 chunks)
- Index must be rebuilt when transcripts change (not incremental yet)
- pandas pulled in as transitive dependency (via LanceDB results)

## Alternatives Considered

- **Embed with Gemini Embedding 001 (text-only).** Already have the API key. But 3072 dims (3x storage), only 2,048-token context window (vs Voyage's 32K), no asymmetric retrieval, lower retrieval benchmarks on RTEB. Batch pricing ($0.075/M) is cheaper than Voyage for one-time indexing, but no query-cost savings. Status: rejected.
- **Embed with Gemini Embedding 2 Preview (multimodal).** Could embed raw video/audio, but our pipeline already extracts text via Gemini generation — embedding the extracted text is sufficient. Higher cost ($0.20/M text), still in preview, no benchmark comparison vs Voyage for text retrieval. Status: not evaluated — different use case (raw media retrieval).
- **Local embeddings via FastEmbed (BGE-M3).** Zero API cost, works offline. But 512-token context too short for transcript chunks, lower retrieval quality. Status: deferred as offline fallback.
- **Embed mindmap bullets instead of transcript chunks.** Smaller index (~4K entries vs ~15K). But mindmaps are compressed summaries — evidence queries miss detail. Status: rejected.
- **Full-text search only (no embeddings).** Zero cost, no dependencies. But "permission problems" won't match "access control issues." Status: rejected — doesn't solve the core problem.

## Affects

- `scripts/video_intel.py` — new functions: `chunk_transcript()`, `build_search_index()`, `vector_search()`, `cmd_index()`; updated `cmd_search()` with `--vector` flag
- `pyproject.toml` — added `[vector]` optional dependency group
- `tests/test_utils.py` — tests for chunking and concept loading
- `SKILL.md` — documented `index` command and `--vector` flag
- `README.md` — documented vector search in architecture and quick start
- `CLAUDE.md` — documented `index` subcommand and `VOYAGE_API_KEY`

## Related Debt

- Incremental indexing (add new transcripts without full rebuild)
- Concept-filtered vector search (`.where("concept_ids LIKE '%agent_skills%'")`)
- Benchmark eval set: 25 frozen queries to measure retrieval quality

## Research References

- [Voyage AI embeddings docs](https://docs.voyageai.com/docs/embeddings) — embed API, asymmetric retrieval, input_type parameter
- [Voyage AI pricing](https://docs.voyageai.com/docs/pricing) — per-model token costs
- [Voyage 4 benchmarks](https://blog.voyageai.com/2026/01/15/voyage-4/) — RTEB results vs Gemini Embedding 001
- [Voyage rate limits](https://docs.voyageai.com/docs/rate-limits) — tier-based throttling, payment method unlocks
- [Google embeddings overview](https://ai.google.dev/gemini-api/docs/embeddings) — current model lineup (001 + 2-preview)
- [Google embedding pricing](https://ai.google.dev/gemini-api/docs/pricing) — per-model, per-modality costs
- [LanceDB docs](https://lancedb.github.io/lancedb/) — Python API, hybrid search
- [LanceDB Voyage integration](https://docs.lancedb.com/integrations/embedding/voyageai) — native VoyageAI support
- [LanceDB Gemini integration](https://docs.lancedb.com/integrations/embedding/gemini) — native Gemini support
- [RTEB benchmark](https://huggingface.co/blog/rteb) — retrieval-specific eval methodology (distinct from MTEB)
- Plan file: `plans/tender-wobbling-penguin.md` — Phase 2 design decisions
- ADR-0010: LLM-powered concept normalization (the concept layer this builds on)

## Notes

Voyage rate limits are tied to **usage tiers**, not just free vs paid tokens. Without a payment method (Tier 1), limits are 3 RPM / 10K TPM. Adding a payment method unlocks higher tiers even if you're still using free token allocation — the 200M free tokens still apply. The code handles rate limits with exponential backoff and works on all tiers — just slower on Tier 1.

LanceDB's `create_index("vector")` requires >= 256 rows for IVF index creation. Below that threshold, brute-force search is used (fast enough for our scale).
