---
title: "feat: KB Stage 2 - LightRAG dual-level graph retrieval"
type: feat
status: deferred
date: 2026-04-20
adr: ADR-0017
deferred_date: 2026-05-28
deferred_by: [ADR-0018, docs/brainstorms/2026-05-28-intelligence-layer-roadmap.md]
---

> **DEFERRED 2026-05-28.** This plan is preserved as the canonical record of what Stage 2 LightRAG would have looked like, but it is no longer the active next step. Two superseding artifacts:
>
> - [`ADR-0018`](../adr/ADR-0018-nugget-cli-cross-creator-synthesis.md) — formalized a three-signal gate that must fire before Stage 2 LightRAG is scheduled (3+ failed multi-hop usage queries, eval ≤3/25 after second retrieval-tuning pass, or 2+ conceptual lenses requiring schema-free extraction). Until any of those signals fires, this plan does not execute.
> - [`docs/brainstorms/2026-05-28-intelligence-layer-roadmap.md`](../brainstorms/2026-05-28-intelligence-layer-roadmap.md) — pivoted the active trajectory toward a **structured intelligence layer** (DuckDB starter schema + bidirectional Displacement/Magnet lenses + 6-tuple stance schema in `prompts/concepts.md`) instead of LightRAG retrieval bridging. The reasoning: the failure shape the 1/25 eval surfaced is sparse taxonomy coverage, but the *user-experienced gap* is aggregate / contrastive / polarity-flipped queries — which are SQL-shaped, not graph-shaped.
>
> The plan body below is unchanged from its 2026-04-20 form. Read it as the *fallback* if any of ADR-0018's three signals fires later.

# feat: KB Stage 2 - LightRAG dual-level graph retrieval

## Overview

Add LightRAG as a second retrieval path alongside hybrid BM25+vector
([ADR-0013](../adr/ADR-0013-hybrid-search-rrf-fusion.md)), gated against the
frozen 25-query golden dataset
([`tests/evals/golden_dataset.yaml`](../../tests/evals/golden_dataset.yaml)).
LightRAG's dual-level graph (local entity aggregation + global community
detection) is the heavy version of the vocabulary bridge Stage 1 approximated
cheaply, and it directly targets the recall-failure mode that the 1/25
baseline exposed ([ADR-0017 Context](../adr/ADR-0017-kb-layer-strategy.md#context)).

Per [ADR-0017's decision rule](../adr/ADR-0017-kb-layer-strategy.md#decision-rule-between-stages),
Stage 2 is justified because Stage 1 landed at **1/25 = Stage 1**, well under
the 10/25 early-exit threshold, and the Stage 1 diagnostic shows the gap is
taxonomy coverage (15/25 queries matched no taxonomy concept at all) plus
alias quality (verbose LLM-authored sibling phrases). A graph that builds
its own entity/relation vocabulary from the transcripts themselves is the
next cheapest lever that still falsifies the vocabulary-mismatch hypothesis.

The exit criterion for Stage 2 is the same shape as Stage 1: re-run
`pytest tests/evals/`, record N/25 in
[ADR-0017's Consequences table](../adr/ADR-0017-kb-layer-strategy.md#baseline-and-future-measurements),
and let the number decide whether Stage 3 (synthesis / Wiki) runs next or
whether a further recall fix is still indicated.

## Problem Statement

Stage 1 (PR #27, merged 2026-04-20) shipped `expand_query_via_taxonomy()`
inside `hybrid_search()`. Eval score moved from 1/25 to 1/25 — **no
headline uplift**. The diagnostic breakdown recorded in ADR-0017:

- **15 of 25 queries matched zero taxonomy concepts.** The taxonomy does
  not cover the vocabulary the golden queries use, so alias expansion had
  nothing to add for most queries.
- **For the 10 queries that did match, sibling quality was noisy.** The
  concept-extraction prompt generates verbose LLM-authored aliases like
  *"Automated data fetching from external MCP servers"*, and the 12-cap
  was saturated on every matched query.
- **Total failing metrics 59 - 57.** Three queries (Q04/Q06/Q13) dropped
  one failing metric each, one regressed (Q03), 21 were unchanged.

The shape of the failure confirms the ADR-0017 reading: the bottleneck is
*the vocabulary itself*, not the mechanism that uses it. A cheaper fix
applied to a thin dictionary cannot build more of the dictionary. Stage 2
addresses this by letting LightRAG extract a graph from the transcripts
themselves:

- **Entity graph from raw content.** LightRAG reads transcript text and
  produces entities + relations, independent of our `concepts.json` output.
  If "Capybara" co-occurs with "browser automation" in some video, that
  edge exists in the graph whether or not the taxonomy knows about it.
- **Dual-level retrieval.** Local mode pivots on entity neighbourhoods
  (*what's adjacent to "browser automation" in the graph?*); global mode
  traverses relationships across communities (*what do creators X/Y/Z
  agree on about agents?*). Both directly match the failure shape on the
  golden dataset's cross-channel and comparative queries.
- **No dependency on the existing taxonomy.** LightRAG's README is
  explicit that it does not accept pre-extracted entities as input hints
  ([knowledge-layer brainstorm 2026-04-16](../../work/2026-04-16/04-knowledge-layer-options-brainstorm.md)).
  This is acknowledged as cost (two parallel vocabularies), not blocker.

The open empirical question Stage 2 answers: **does building a graph
vocabulary from transcripts actually land the uplift that the sparse
taxonomy couldn't?** The eval is the forcing function.

## Proposed Solution

Add LightRAG as a **new, additive retrieval mode**, not a replacement for
hybrid search. Hybrid remains the default (`search --vector`), matches
ADR-0013 behaviour, and stays scored against the eval every release.
LightRAG is exposed via new subcommands (`lightrag-index`, `lightrag-search`)
and a new `search --graph` switch. The eval harness learns to swap its
retrieval backend via an environment variable so the same golden dataset
scores both paths without branching in application code.

### Domain model (the new entities and their invariants)

Stage 2 introduces a new subsystem. Per
[agent-rules.md §5](../../specs/agent-rules.md), new territory earns a
domain model before code:

| Entity | Shape | Invariants |
| ------ | ----- | ---------- |
| **LightRAG working directory** | filesystem directory holding NetworkX graph, NanoVectorDB, JSON KV | Derived. Rebuildable from transcripts. Lives outside `output_dir` per [ADR-0016](../adr/ADR-0016-vector-db-path-config.md). |
| **Ingested document** | one per video, composed of transcript + header metadata (channel, title, video_id, published) | Stable `ids=[video_id]`. Re-ingesting the same `video_id` with identical content is a no-op (LightRAG doc-hash dedup). |
| **Custom chunk** | 5-entry chunk from `chunk_transcript()`, stamped with `(video_id, channel, title, published, timestamp, timestamp_seconds)` metadata | Timestamp must survive into retrieval so the eval's `TimestampPrecisionMetric` can score. |
| **Extracted entity / relation** | LightRAG-owned, graph-native, parallel to our concepts.json | Not merged with `concepts.json`. Acknowledged vocabulary drift. |
| **Retrieval hit** | dict with `video_id`, `channel`, `title`, `published`, `timestamp`, `timestamp_seconds`, `relevance`, `text`, `source_file`, `concept_ids` | Schema-identical to `hybrid_search()` output. This is the contract the eval harness already consumes. |

The **boundary** between our extraction and LightRAG's:

- `concepts.json` / `taxonomy.json` remain owned by the existing pipeline
  ([ADR-0010](../adr/ADR-0010-llm-concept-normalization.md)).
- LightRAG's entity graph is a **second, independent view** of the same
  transcripts. No attempt to reconcile the two vocabularies in v1 — the
  ADR explicitly accepts this cost.
- If either vocabulary later proves sufficient alone, a future ADR can
  deprecate the other. Stage 2 does not pre-commit that decision.

### Why additive, not replacement

Three reasons, each naming the failure mode it prevents:

1. **Eval attribution.** If hybrid is replaced, the 1/25 baseline column
   in ADR-0017 loses meaning — we can no longer compare stage-over-stage.
   Keeping hybrid alive means the same 25 queries score both retrieval
   paths in the same run, and we learn which mode wins per query.
2. **Risk of regression.** LightRAG may turn out to be worse than hybrid
   on some query shapes (e.g., exact model-name matches like Q15, which
   BM25 already dominates). If it replaced hybrid, regressions on those
   queries would be silent.
3. **Cost.** Hybrid retrieval is ~$0.0001/query (one Voyage embed).
   LightRAG retrieval runs entity selection + graph traversal + optional
   chunk rerank, closer to ~$0.001-0.01/query depending on mode. Making
   it optional keeps the cheap path as default.

## Technical Approach

### Architecture

```
                     +---------------------------+
                     |   scripts/video_intel.py  |
                     |  (existing CLI dispatcher)|
                     +-------------+-------------+
                                   |
               +-------------------+-------------------+
               |                                       |
     +---------v---------+                 +-----------v-----------+
     |  hybrid_search()  |  (ADR-0013)     |  lightrag_search()    | <- NEW
     |  (LanceDB+BM25)   |                 |  (graph+vector+rerank)|
     +---------+---------+                 +-----------+-----------+
               |                                       |
               |                                       |
     +---------v-------------------------------------- v---------+
     |  Return schema (contract with eval harness):              |
     |  list[{video_id, channel, title, published,               |
     |        timestamp, timestamp_seconds, relevance, text,     |
     |        source_file, concept_ids}]                         |
     +-----------------------------------------------------------+

                                   |
                     +-------------v-------------+
                     | tests/evals/test_search_  |
                     |      quality.py           |
                     |  Env-var toggled backend  |
                     +---------------------------+
```

### New module layout

```text
scripts/
  video_intel.py          ← unchanged core + thin CLI dispatch additions
  lightrag_backend.py     ← NEW. All LightRAG-specific code.
  gemini_common.py        ← unchanged. Shared retry/client helpers.

tests/
  test_lightrag_backend.py          ← NEW. Unit tests (mocked LightRAG).
  test_lightrag_integration.py      ← NEW. Integration, marker=integration.
  evals/
    test_search_quality.py          ← EDIT. Add retrieval-backend toggle.
    results/
      <date>-lightrag-expansion.jsonl   ← NEW diagnostic artifact.
```

`lightrag_backend.py` is a new file rather than more additions to the
already-dense `video_intel.py` because:

- LightRAG introduces async code, a custom chunking callable, a custom LLM
  wrapper, a custom embedding wrapper, and new retrieval plumbing. All of
  this is self-contained and has its own import graph (heavyweight
  optional dep).
- Per [agent-rules.md §1](../../specs/agent-rules.md), cognitive load on
  the next reader is the north-star constraint. Adding 500+ lines of
  async/graph code to a 3,000-line CLI script that already hosts scan /
  transcript / mindmap / concepts / taxonomy / index / search increases
  cognitive load measurably. A new module keeps the existing file's
  mental model intact.
- A separate module makes the lazy-import boundary clean:
  `require_lightrag()` can live in `gemini_common.py` next to the
  existing `require_lancedb()` / `require_voyageai()` helpers, and the
  `lightrag_backend` module is never imported until a `lightrag-*`
  subcommand or `search --graph` runs.

### Insertion points (concrete)

**File:** [`scripts/video_intel.py`](../../scripts/video_intel.py)

| Location | Existing line | Change |
| -------- | ------------- | ------ |
| imports | top of file | `from lightrag_backend import lightrag_index, lightrag_search` (wrapped in a `try/except ImportError` that `require_lightrag()` surfaces as a clear error if the user hits a `lightrag-*` command without the extra installed) |
| `cmd_search` argparse | L2935 | Add `--graph` flag (mutually exclusive with `--vector` **and** with default concept mode) |
| `cmd_search` dispatch | L2841 | New branch: `if getattr(args, "graph", False): call lightrag_search(...)` |
| `main()` subparsers | L2985 | Register two new subparsers: `lightrag-index` and `lightrag-search` |

**File:** [`scripts/gemini_common.py`](../../scripts/gemini_common.py)

| Change |
| ------ |
| Add `require_lightrag()` lazy-import helper, mirroring the existing pattern for `require_lancedb()`. Returns the `lightrag` module or exits with an actionable install message (`pip install 'video-intel[graph]'` or `pip install lightrag-hku`). |

**File:** `scripts/lightrag_backend.py` (NEW)

| Export | Responsibility |
| ------ | -------------- |
| `lightrag_index(output_dir, config, *, channel=None, force=False, dry_run=False)` | Walk `output_dir/<channel>/*.transcript.md`, build one ingestion document per video with header metadata, call `rag.ainsert(...)` in batches. Idempotent via LightRAG doc-hash dedup unless `force=True` (which blows away the working dir first). |
| `lightrag_search(output_dir, query, *, limit, config, mode="hybrid", channel_filter=None, since_iso=None, return_diagnostics=False)` | Initialize LightRAG with the same `working_dir`, run `rag.aquery(query, param=QueryParam(mode=mode, only_need_context=True, top_k=..., chunk_top_k=limit))`, parse chunks, reshape into the hybrid-search output contract (same dict keys). |
| `_gemini_llm_func(prompt, **kwargs) -> str` | Async wrapper around `google.genai` that LightRAG calls for entity/relation extraction. Uses the same model as `config.yaml` (`model: gemini-3-flash-preview` today). |
| `_voyage_embed_func(texts) -> list[list[float]]` | Async wrapper around `voyageai.Client.embed(...)`, reusing `VOYAGE_DOC_MODEL` and `VOYAGE_QUERY_MODEL` constants. |
| `_transcript_chunking_func(text, **kwargs) -> list[dict]` | Custom chunking callable that reuses the existing `chunk_transcript()` logic from `video_intel.py`. Emits chunks with `{content, chunk_order_index, metadata: {video_id, channel, title, published, timestamp, timestamp_seconds, source_file}}`. |

### `lightrag_search()` spec

```python
# scripts/lightrag_backend.py

async def _alightrag_search(
    output_dir: Path,
    query: str,
    *,
    limit: int,
    config: dict,
    mode: str = "hybrid",
    channel_filter: str | None = None,
    since_iso: str | None = None,
) -> tuple[list[dict], dict]:
    """Run a LightRAG query and reshape the result to match hybrid_search().

    Invariants enforced:
      - Returned dicts have the exact key set that `hybrid_search()` returns.
        This is the harness contract and must not drift.
      - `timestamp_seconds` is populated for every hit. If LightRAG hands
        back a chunk whose metadata lacks it, we parse the timestamp from
        the leading `[MM:SS]` of the chunk text as a fallback.
      - Empty results return (`[]`, diagnostics) so callers can distinguish
        "no match" from "index missing".
    """

def lightrag_search(
    output_dir: Path,
    query: str,
    *,
    limit: int = 10,
    config: dict | None = None,
    mode: str = "hybrid",
    channel_filter: str | None = None,
    since_iso: str | None = None,
    return_diagnostics: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    """Sync wrapper. Runs asyncio.run(_alightrag_search(...))."""
```

Mode mapping:

- `search --graph` → `QueryParam(mode="hybrid")`  (LightRAG hybrid: local+global)
- `search --graph --mode local`  → local entities only
- `search --graph --mode global` → global relations only
- `search --graph --mode mix`    → KG + vector rerank (recommended with reranker per docs; gated on `enable_rerank`)
- `search --graph --mode naive`  → basic vector, no graph

Default mode is `hybrid` because the ADR-0017 rationale targets
vocabulary-mismatch *and* cross-channel synthesis, and `hybrid` is
LightRAG's combined local+global path.

### `lightrag_index()` spec

```python
async def _alightrag_index(
    output_dir: Path,
    config: dict,
    *,
    channel: str | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    """Build/refresh the LightRAG working directory from transcripts.

    Returns {"docs": int, "chunks": int, "entities": int, "relations": int,
             "cost_usd_est": float, "working_dir": Path, "elapsed_s": float}.

    Behaviour contract:
      - Walks output_dir/<channel>/*.transcript.md, same filter semantics
        as build_search_index().
      - Builds one document string per video: header block (channel, title,
        video_id, published) followed by the full transcript text.
      - Calls rag.ainsert(documents, file_paths=[...], ids=[video_id, ...])
        in batches of max_parallel_insert=2 (LightRAG default; recommended
        <= 10 per docs).
      - force=True deletes the working_dir before ingesting (full rebuild).
      - dry_run=True walks the inputs, computes the ingestion size and
        Flash-model cost estimate, prints the plan, and exits without any
        LightRAG call. This protects against accidentally burning $10 of
        Flash credit on a misconfiguration.

    Cost estimate formula (documented inline so it's auditable):
      est = (tokens_in / 1M) * input_price + (tokens_out / 1M) * output_price
      where tokens are estimated from transcript length and the LightRAG
      extraction prompt fan-out (~2-3 LLM calls per chunk in hybrid mode).
    """
```

`dry_run` is a new primary ergonomic for Stage 2 — naming the failure mode
it prevents: *surprise bills.* LightRAG's ingest fan-out is not obvious
from the API surface, and this repo's cost ceiling per operation has been
closer to $0.05 historically.

### Custom chunking contract

LightRAG's default chunking is token-based. Our transcripts have
*structural* chunk boundaries — timestamped speech lines and SCREEN
blocks — that the retrieval layer must preserve or the
`TimestampPrecisionMetric` cannot score hits against the golden dataset.

```python
# scripts/lightrag_backend.py

def _transcript_chunking_func(
    text: str,
    *,
    tokenizer=None,
    **kwargs,
) -> list[dict]:
    """LightRAG chunking callable. Preserves 5-entry timestamped chunks.

    Reuses chunk_transcript() from video_intel.py. The `text` passed in
    is one document (one video's transcript + header). We split it back
    into the original 5-entry chunk boundaries and stamp each chunk with
    the metadata LightRAG propagates through retrieval.

    Return shape: list of dicts with LightRAG's chunk schema
    (content, chunk_order_index, ...) plus an extended `metadata` payload
    we control.

    Invariant: every returned chunk has `metadata["timestamp_seconds"]` set.
    """
```

**Risk and mitigation.** LightRAG's chunking API may reshape or drop
custom metadata fields between releases. Mitigation, in priority order:

1. Pin the LightRAG version in `pyproject.toml` (e.g., `lightrag-hku==1.4.14`).
2. In `_alightrag_search()`, if the returned chunk metadata is missing
   `timestamp_seconds`, **fall back** to parsing the leading `[MM:SS]`
   from the chunk text using the same regex as `chunk_transcript()`.
   Silent fallback with a WARN log line — do not hide this from the
   eval diagnostic trail.
3. Integration test asserts `timestamp_seconds` is populated on at least
   one returned hit for a known query. Failure fails the PR.

### Storage location (per ADR-0016)

| Artifact | Location | Rationale |
| -------- | -------- | --------- |
| Graph + vector DB + KV store | `vector_db_dir.parent / "lightrag"` (default: `~/video-intel-cache/lightrag/`) | Same local FS constraint as LanceDB — LightRAG's NetworkX + NanoVectorDB use atomic writes that cloud-synced drives break. |
| Per-video doc hashes | inside working_dir (`doc_status.json`) | LightRAG-owned; we never read it directly. |
| Transcripts (inputs) | `output_dir/<channel>/` — unchanged | Owned by scan pipeline, read-only for Stage 2. |
| ADR-0017 Consequences table | `docs/adr/ADR-0017-kb-layer-strategy.md` | Canonical record of stage scores. |

A new `lightrag_dir` field in `config.yaml` (optional, defaults to
`resolve_vector_db_dir(...).parent / "lightrag"`) lets the user override.

### LLM + embedding wiring

- **LLM model function:** Gemini. LightRAG defaults to OpenAI; we override
  with a custom async callable that calls `google.genai` using the
  existing `gemini_common.create_client()` infrastructure. The model
  honors the `--model` CLI flag and the `config.yaml` `model` field,
  matching the existing `scan` / `transcript` / `concepts` commands for
  operational consistency.
- **Embedding function:** Voyage. Reuse `VOYAGE_DOC_MODEL = "voyage-4-large"`
  for ingestion and `VOYAGE_QUERY_MODEL = "voyage-4-lite"` for query, via
  a `lightrag.utils.EmbeddingFunc` wrapper around `voyageai.Client.embed()`.
  Embedding dim 1024, matches [ADR-0012](../adr/ADR-0012-vector-search-lancedb-voyage.md).

Rationale: using the same LLM and embedding stack as the rest of the
project keeps the operational and cost models uniform. It also means the
eval attribution claim is sharper: if LightRAG beats hybrid on the
golden dataset, the uplift is from the **graph construction**, not from
switching to a different embedding model.

### Retrieval integration (default mode + flags)

```text
# New `search --graph` switch (mutually exclusive with --vector)
video_intel search "browser automation" --graph                  # hybrid mode (default)
video_intel search "browser automation" --graph --mode global    # relations
video_intel search "browser automation" --graph --mode local     # entities
video_intel search "browser automation" --graph --mode mix       # KG + vector rerank

# Explicit subcommands also work
video_intel lightrag-index                      # build/refresh the graph
video_intel lightrag-index --channel natebjones # single channel
video_intel lightrag-index --dry-run            # preview cost without ingesting
video_intel lightrag-index --force              # full rebuild
video_intel lightrag-search "browser automation" --mode hybrid --limit 10
```

The `search --graph` sugar is a pure alias for `lightrag-search`, but it
lives under `search` for discoverability: users searching the CLI help
for "how do I search?" see every mode in one place.

### Eval harness integration

**File:** [`tests/evals/test_search_quality.py`](../../tests/evals/test_search_quality.py)

Add a retrieval-backend selector that parallels the existing
`VIDEO_INTEL_EVAL_EXPAND` toggle:

```python
# tests/evals/test_search_quality.py near line 47

_RETRIEVAL_MODE = os.environ.get("VIDEO_INTEL_EVAL_MODE", "hybrid")
# Values:
#   "hybrid"   : vi.hybrid_search(...)         (Stage 0 + Stage 1 baseline)
#   "lightrag" : vi.lightrag_search(...)       (Stage 2 under test)

# Per-query run tag logic:
#   hybrid + expand=off  ->  "YYYY-MM-DD-baseline"
#   hybrid + expand=on   ->  "YYYY-MM-DD-stage1"
#   lightrag             ->  "YYYY-MM-DD-stage2-{mode}"   (e.g. stage2-hybrid)
```

```python
# In test_retrieval_quality(), swap the call:
if _RETRIEVAL_MODE == "lightrag":
    lightrag_mode = os.environ.get("VIDEO_INTEL_LIGHTRAG_MODE", "hybrid")
    hits, diagnostics = vi.lightrag_search(
        output_dir, gold["query"], limit=limit, config=config,
        mode=lightrag_mode, return_diagnostics=True,
    )
else:
    hits, diagnostics = vi.hybrid_search(
        output_dir, gold["query"], limit=limit, config=config,
        expand=_EXPAND_ENABLED, return_diagnostics=True,
    )
```

Diagnostics file:
`tests/evals/results/<run_tag>-expansion.jsonl` for the hybrid paths (unchanged),
`tests/evals/results/<run_tag>-retrieval.jsonl` for LightRAG runs — a
symmetric per-query record with the retrieved entity / relation summaries
LightRAG exposes (so the PR can diff which queries flipped and why).

Commands:

```text
# Stage 2 (hybrid graph mode)
VIDEO_INTEL_EVAL_MODE=lightrag pytest tests/evals/ -v -s

# Stage 2 (local entity mode) — for ablation
VIDEO_INTEL_EVAL_MODE=lightrag VIDEO_INTEL_LIGHTRAG_MODE=local pytest tests/evals/ -v -s

# Stage 2 (global relation mode) — for ablation
VIDEO_INTEL_EVAL_MODE=lightrag VIDEO_INTEL_LIGHTRAG_MODE=global pytest tests/evals/ -v -s

# Stage 1 baseline for comparison (unchanged)
pytest tests/evals/ -v -s
```

### Async/sync boundary

LightRAG's public API is async-first (`ainsert`, `aquery`, `initialize_storages`,
`finalize_storages`). Our CLI is sync. The boundary is drawn cleanly:

- All async internals live in `_alightrag_*` functions.
- Public sync wrappers (`lightrag_index`, `lightrag_search`) are the *only*
  entry points from `video_intel.py`, and each one does one
  `asyncio.run(...)` call with its own LightRAG instance (init → work →
  finalize).
- The eval harness calls the sync wrapper. No asyncio leaks into
  `test_search_quality.py`.

Rationale: re-entering asyncio from inside an already-running event loop
is a footgun (and pytest-asyncio config drift would waste hours). One
entry, one exit, per CLI subcommand or eval case, is the smallest
boundary that keeps the rest of the codebase unchanged.

### Dependency management

**pyproject.toml** adds an optional extra:

```toml
[project.optional-dependencies]
graph = [
    "lightrag-hku==1.4.14",
    # Optional sub-deps pinned for reproducibility
]
```

Install path: `pip install -e ".[graph]"`. `require_lightrag()` surfaces
the install command if the extra is missing.

Rationale: LightRAG pulls in tiktoken, networkx, nano-vectordb, and
several others. Keeping it optional preserves the current install
footprint for users who never touch graph retrieval.

## Implementation Phases

Work backwards from the Stage 2 eval re-run. Every other step exists to
make that re-run trustworthy.

### Phase 0 — Spike (half a day, before committing to the full plan)

Before writing any production code, run a **timestamp-preservation spike**:

1. Pip-install LightRAG in an isolated venv.
2. Feed one transcript through `rag.ainsert()` using the default chunking
   with OpenAI (cheapest setup for a spike).
3. Query and check whether retrieved chunks carry *any* leading `[MM:SS]`
   prefix, and whether `chunk_order_index` maps back to the original
   transcript order.
4. If LightRAG drops or mangles the timestamp, confirm that a custom
   `chunking_func` can inject metadata that survives retrieval.

**Failure mode this prevents.** Building the full custom chunker and
adapters only to discover LightRAG's retrieval strips metadata — a 2-3
day waste. If the spike fails, the plan pivots: either ingest with
timestamps in the text body and parse them back out at retrieval (less
clean), or escalate to `/deepen-plan` for an alternate integration
pattern.

**Artifact:** a short `docs/solutions/rejected-paths/` note if the spike
fails, or a green-lit "proceed" line in the PR description if it passes.
This also unblocks the Related Debt item in ADR-0017 about creating
`docs/solutions/rejected-paths/`.

### Phase 1 — Foundation (lazy import + module scaffolding)

1. Add `graph` extra in `pyproject.toml` with a pinned `lightrag-hku`.
2. Add `require_lightrag()` to `scripts/gemini_common.py`.
3. Create `scripts/lightrag_backend.py` with stub async functions, LLM
   wrapper, embedding wrapper, and chunking callable. Do not wire into
   `video_intel.py` yet.
4. Unit tests under `tests/test_lightrag_backend.py` that *mock* the
   LightRAG import and verify the wrappers call it with the expected
   arguments (asserting shape of kwargs, not calling the real service).

### Phase 2 — Ingestion

1. Implement `lightrag_index()`, including the cost estimator and
   `--dry-run` mode.
2. Integration test (`@pytest.mark.integration`) that ingests a tiny
   fixture transcript into a temp working_dir and asserts
   `doc_status.json` has one entry with the video_id as key.
3. CLI wiring: register `lightrag-index` subparser, dispatch, smoke-test
   `video_intel.py lightrag-index --dry-run` locally.

### Phase 3 — Retrieval

1. Implement `lightrag_search()` including the reshape-to-hybrid-schema
   step and the `[MM:SS]` fallback parser.
2. Unit tests that mock LightRAG's query response and assert the output
   dict schema is byte-identical to `hybrid_search()` for each of the
   five modes (local, global, hybrid, naive, mix).
3. Integration test that ingests a tiny fixture and runs a known query
   that should retrieve the single fixture document. Asserts the hit's
   `timestamp_seconds` is populated.
4. CLI wiring: `search --graph` and `lightrag-search` subparsers,
   mutual-exclusivity checks against `--vector`.

### Phase 4 — Eval integration

1. Wire `VIDEO_INTEL_EVAL_MODE` into `tests/evals/test_search_quality.py`.
2. Add `run_tag` cases for `stage2-{mode}` shapes.
3. Confirm the existing Stage 1 path is unchanged when the env var is
   unset — a regression here would break the Stage 1 baseline artifact.
4. Rebuild the LightRAG working dir against the full corpus:
   `python scripts/video_intel.py lightrag-index`.

### Phase 5 — Run the eval

1. `pytest tests/evals/ -v -s` (hybrid baseline; must still produce 1/25).
2. `VIDEO_INTEL_EVAL_MODE=lightrag pytest tests/evals/ -v -s` (Stage 2
   hybrid mode).
3. `VIDEO_INTEL_EVAL_MODE=lightrag VIDEO_INTEL_LIGHTRAG_MODE=global pytest tests/evals/ -v -s`
   (ablation; informative but not gating).
4. `VIDEO_INTEL_EVAL_MODE=lightrag VIDEO_INTEL_LIGHTRAG_MODE=local pytest tests/evals/ -v -s`
   (ablation).
5. Diff the per-query JSONL files pairwise; the diff lives in the PR
   description.

### Phase 6 — Update docs and ADR

1. Update [ADR-0017 Consequences table](../adr/ADR-0017-kb-layer-strategy.md#baseline-and-future-measurements)
   with the Stage 2 score row (primary mode = hybrid).
2. Add a "Graph retrieval (Stage 2)" section to
   [`docs/search-internals.md`](../search-internals.md).
3. Update the `search` bullet in
   [`CLAUDE.md`](../../CLAUDE.md) Architecture section to mention
   `--graph` and the new `lightrag-index` / `lightrag-search` commands.
4. Propose the next-stage decision per the success-metrics table below.

### Phase 7 — Open the PR

- Title: `feat(search): LightRAG dual-level graph retrieval (KB Stage 2)`.
- Body: cites ADR-0017, includes JSONL diagnostic diffs, states the
  N/25 number, names which mode won, proposes Stage 3 decision.

## Alternative Approaches Considered

### Ingest concepts.json instead of raw transcripts

Feed LightRAG a synthesized document per video = `concepts.json` bullet
list + meta.json.

- **Pros:** Much smaller ingest (~20 concepts per video vs a full
  transcript). LightRAG's extraction produces a graph over our existing
  vocabulary, not a parallel one.
- **Cons:** The 2026-04-20 Stage 1 result pinpointed taxonomy coverage
  itself as the bottleneck. Feeding LightRAG only what the taxonomy
  already knows guarantees Stage 2 inherits that coverage gap. It also
  strips timestamp anchors entirely, breaking the
  `TimestampPrecisionMetric`.
- **Status:** rejected. Raw transcripts preserve the signal the
  taxonomy missed.

### Replace hybrid_search() with lightrag_search()

Drop BM25+vector entirely; make LightRAG the only retrieval path.

- **Pros:** Simpler call graph. One retrieval function, one eval path.
- **Cons:** Loses the baseline column in ADR-0017. Q15 (the one passing
  query) works because BM25 dominates on exact-token matches — a graph
  may be worse at that specific shape. Regression risk is one-sided.
- **Status:** rejected. Stage 2 is additive per ADR-0017 ("Stages 1-3
  are additions, not migrations").

### Use Neo4j or PostgreSQL as LightRAG backend

LightRAG supports pluggable graph/vector storage (Neo4j, PGVector,
Milvus).

- **Pros:** Production-grade durability; query UIs.
- **Cons:** New infrastructure to stand up for a single-user research
  project. Violates the "cheapest intervention first" spirit of
  ADR-0017. Default NetworkX + NanoVectorDB is filesystem-only and
  sufficient for the ~100-200 videos in the current corpus.
- **Status:** deferred. If the corpus grows or multi-user access becomes
  a need, a follow-up ADR (separate from ADR-0017) can flip the
  backend; nothing about Stage 2 locks us out of that.

### Parallel bake-off with Stage 3 (LLM Wiki)

Build LightRAG (Stage 2) and the Wiki synthesis layer (Stage 3) in
separate worktrees at the same time.

- **Pros:** Maximum calendar speed. ADR-0017 explicitly permits
  parallel execution for stages addressing orthogonal failure modes.
- **Cons:** Stage 3 depends on G-Eval metrics for
  `position_diversity` / `essay_coverage` that are still to be
  implemented (ADR-0017 Related Debt). Stage 3 also depends on *some*
  working recall layer — if Stage 2 doesn't land uplift, Stage 3's
  synthesis pages compound from broken retrieval. Better to run Stage
  2 first, read the eval, then decide.
- **Status:** deferred. Remains an explicit option if Stage 2 lands
  uplift cleanly and G-Eval metrics ship in parallel.

### Feed LightRAG manually-curated vocabulary hints

Override LightRAG's entity-extraction prompt to prefer taxonomy
concept labels / aliases.

- **Pros:** Might close the gap between the two parallel vocabularies.
- **Cons:** LightRAG README is explicit that hints are not supported.
  Any workaround (prompt injection, post-extraction merging) couples
  our extraction pipeline to LightRAG's internals. High maintenance
  cost for uncertain payoff.
- **Status:** deferred to v2. If the eval shows Stage 2's extracted
  entities systematically diverge from useful query terms, a focused
  follow-up plan can explore this.

## Acceptance Criteria

Every criterion names the failure mode it prevents, per
[agent-rules.md meta-rule](../../specs/agent-rules.md).

### Functional

- [ ] `scripts/lightrag_backend.py` exists with `lightrag_index()` and
      `lightrag_search()` public sync functions. *Prevents:* entangling
      the new subsystem with `video_intel.py`'s existing 3,000 lines,
      raising cognitive load (rules-of-engagement §1).
- [ ] `scripts/gemini_common.py` has `require_lightrag()` with an
      actionable install-hint error message on ImportError. *Prevents:*
      cryptic `ImportError: lightrag` stack traces the user can't act on.
- [ ] `cmd_search` exposes `--graph` mutually exclusive with `--vector`.
      *Prevents:* ambiguous retrieval mode selection.
- [ ] `main()` registers `lightrag-index` and `lightrag-search`
      subparsers. *Prevents:* forcing users to remember an undocumented
      env-var pathway.
- [ ] `lightrag_index` supports `--channel`, `--force`, `--dry-run`.
      *Prevents:* full-corpus LLM spend on every iteration of a
      debugging loop.
- [ ] `lightrag_search` returns dicts with the exact same key set as
      `hybrid_search()`. *Prevents:* eval harness breakage when
      `VIDEO_INTEL_EVAL_MODE` swaps backends — the metrics expect that
      schema.
- [ ] If LightRAG returns a chunk lacking `timestamp_seconds` in
      metadata, `lightrag_search` parses a fallback from the leading
      `[MM:SS]` of the chunk text and logs a WARN. *Prevents:* silent
      `TimestampPrecisionMetric` failures that look like retrieval
      quality issues but are metadata loss.
- [ ] Storage lives under `vector_db_dir.parent / "lightrag"` by
      default, overridable via `lightrag_dir` in `config.yaml`.
      *Prevents:* cloud-sync corruption of atomic commits per
      [ADR-0016](../adr/ADR-0016-vector-db-path-config.md).

### Domain invariants

- [ ] Ingesting the same video twice (same `video_id`, unchanged
      transcript) is a no-op by doc-hash. Tested in integration.
      *Prevents:* every `lightrag-index` run repeating the Flash spend.
- [ ] `force=True` deletes the working_dir before ingesting.
      *Prevents:* accumulating stale graph edges from an earlier
      extraction pass after a prompt change.
- [ ] The LightRAG graph does **not** attempt to merge with
      `concepts.json` / `taxonomy.json`. *Prevents:* a v1 coupling that
      would be expensive to untangle if either vocabulary proves
      insufficient.

### Test coverage

- [ ] `tests/test_lightrag_backend.py` (mocked LightRAG) covers:
  - `_gemini_llm_func` calls through to the configured model.
  - `_voyage_embed_func` uses doc model on ingest, query model on
    search (asymmetric retrieval per ADR-0012).
  - `_transcript_chunking_func` produces one chunk per 5 transcript
    entries with populated `timestamp_seconds`.
  - `lightrag_search` reshapes a sample LightRAG response into the
    hybrid-search schema for each of the five modes.
  - `lightrag_search` fallback-parses `[MM:SS]` when metadata is missing.
- [ ] `tests/test_lightrag_integration.py` (`@pytest.mark.integration`):
  - Ingest a single fixture transcript → query it → assert the hit's
    `video_id`, `timestamp_seconds`, and `channel` are populated.
  - Re-ingest the same fixture → assert no-op by counting doc_status
    entries before and after.
- [ ] `tests/evals/test_search_quality.py` still produces 1/25 with
      `VIDEO_INTEL_EVAL_MODE` unset. *Prevents:* silent regression of
      the Stage 0/1 baselines.

### Eval (the gate)

*Result tracked at run time. Populate before PR:*

- [ ] Stage 2 hybrid-mode score recorded: **N / 25** (target: >= 10/25
      per ADR-0017).
- [ ] Stage 2 local-mode ablation: N / 25 (informative).
- [ ] Stage 2 global-mode ablation: N / 25 (informative).
- [ ] JSONL diagnostic files written for each run under
      `tests/evals/results/2026-04-*-stage2-*-retrieval.jsonl`.
- [ ] Per-query PASS/FAIL diff between Stage 1 (`stage1`) and Stage 2
      (`stage2-hybrid`) included in the PR description.
- [ ] [ADR-0017 Consequences table](../adr/ADR-0017-kb-layer-strategy.md#baseline-and-future-measurements)
      updated with the Stage 2 row in the same PR.

### Documentation

- [ ] `docs/search-internals.md` gains a "Graph retrieval (Stage 2)"
      section: algorithm summary, mode selection, cost model,
      storage-path rules.
- [ ] `CLAUDE.md` Architecture section mentions `search --graph`,
      `lightrag-index`, `lightrag-search`, and the new `graph` extra.
- [ ] Inline comment in `_transcript_chunking_func` cites the
      timestamp-precision invariant. *Prevents:* a future edit
      simplifying the chunker without realizing the eval depends on
      this metadata field.

### Quality gates

- [ ] `ruff format . && ruff check . --fix` clean.
- [ ] `pytest -m "not integration" -q` green.
- [ ] `pytest -m "integration" -q` green (with LightRAG extra installed
      and API keys in env).
- [ ] Cost estimator's `--dry-run` output matches post-hoc actual spend
      within ±20% on the first real ingest (verify by checking
      Voyage + Gemini billing before merging).

## Success Metrics

**Primary:** N / 25 score on the frozen golden dataset via
`VIDEO_INTEL_EVAL_MODE=lightrag VIDEO_INTEL_LIGHTRAG_MODE=hybrid pytest tests/evals/ -v -s`.

**Decision rule (per ADR-0017):**

| Stage 2 score | Interpretation | Next move |
| ------------- | -------------- | --------- |
| >= 15 / 25 | Graph retrieval substantially closed the recall gap | Ship Stage 2; open planning issue for Stage 3 (Wiki) to attack remaining synthesis-shaped failures. |
| 10–14 / 25 | Moderate uplift, recall improved but ceiling hit | Ship Stage 2; open issue for G-Eval metric implementation (Stage 3 prerequisite). |
| 2–9 / 25 | Small or noisy uplift; graph produced some but not enough bridge vocabulary | Capture the per-query failure modes; open an issue naming the specific failure shape (entity recall vs relation traversal); do not yet proceed to Stage 3. |
| 0–1 / 25 | No uplift or regression | Treat Stage 2 as a negative result. File a `docs/solutions/rejected-paths/` post-mortem. Revisit the ADR-0017 decision rule: if graph + taxonomy expansion both fail to lift recall, the failure may not be vocabulary at all. |

**Secondary diagnostics** (not gating, captured in the PR):

- Per-query delta vs Stage 1 baseline (which queries flipped).
- Average `chunk_top_k` fill rate — did LightRAG actually find
  `limit` chunks, or was recall thin enough to leave slots empty?
- Mode ablation: does `local` or `global` alone outperform `hybrid` on
  any subset of queries? (If `global` wins on cross-channel queries
  and `local` wins on single-channel ones, the `hybrid` default is
  justified; otherwise consider a query-type-aware router in v2.)
- Ingest cost actual-vs-estimate.

**Gate on:** primary score only, per ADR-0017. Ablations are informative.

## Dependencies & Risks

### Dependencies

- LightRAG (`lightrag-hku==1.4.14`) behind `graph` extra in
  `pyproject.toml`. Core install is unchanged.
- Existing `GEMINI_API_KEY`, `VOYAGE_API_KEY` env vars. No new secrets.
- Built `taxonomy.json` is **not** required — Stage 2 is independent of
  the concept pipeline. But a healthy transcript corpus under
  `output_dir/<channel>/*.transcript.md` is required.
- A writable local-FS path for `lightrag_dir`. The default inherits the
  same `vector_db_dir.parent` the project already uses.

### Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| LightRAG strips custom chunk metadata in retrieval (timestamp_seconds) | Medium | High — every `TimestampPrecisionMetric` fails | Phase 0 spike confirms before committing; `[MM:SS]` text-body fallback parser; pin LightRAG version |
| Full-corpus ingest costs more than estimated | Medium | Medium — $20-50 budget surprise | `--dry-run` cost preview; `--channel` staging; document actual-vs-estimate in PR |
| LightRAG's async entity extraction throttles Gemini | Medium | Medium — ingest runtime blows up | `max_parallel_insert=2` (library default); existing retry logic in `gemini_common.get_retry_delay` handles rate-limit replies |
| Graph produces no eval uplift | Medium | High — week-of-work negative result | ADR-0017 already accepts this: the eval is the gate. Capture as rejected-path note; the data itself is value-adding. |
| Two parallel vocabularies (concepts.json vs LightRAG graph) drift further apart over time | High | Low-Medium — operational confusion | Acknowledged cost in ADR-0017. Not a v1 problem. If either vocabulary proves redundant, a follow-up ADR consolidates. |
| LightRAG upstream ships a breaking change | Low-Medium | Medium — indexer or retrieval breaks on upgrade | Pin version in extra; CI runs integration suite against the pinned version; upgrade via explicit plan, not implicit `pip install -U` |
| Async/sync boundary leaks asyncio into eval harness | Low | Medium — brittle pytest runs | One `asyncio.run()` per public sync wrapper; no asyncio imports in test files |
| Eval dataset single-author bias (ADR-0017 negative consequence) | Already-known | Medium — Stage 2 fits the dataset, not external users | Not Stage 2's job to fix. Tracked as Related Debt in ADR-0017. |

### Out of scope

- **Replacing hybrid_search.** Stage 2 is additive.
- **Integration with `concepts` / `taxonomy-build` pipelines.** The
  LightRAG graph is a second, independent view.
- **Auto-ingesting new videos after `scan`.** Manual
  `lightrag-index --channel NAME` for v1; pipeline integration is v2
  after we know whether the graph is worth keeping warm.
- **MCP server exposure of the graph.** Consulting-call use case is
  via CLI `search --graph` for v1.
- **G-Eval metrics for synthesis.** Stage 3 prerequisite, not Stage 2.
- **Neo4j / Postgres / Milvus backends.** Default filesystem for v1.
- **Cross-reconciliation of LightRAG entities with `concepts.json`.**
  v2 lever if eval surfaces a specific need.
- **Query-type-aware mode router** (local for single-channel, global
  for cross-channel). Left for v2 if ablation shows one mode
  systematically beats hybrid on a query-type subset.
- **Incremental graph update on new video ingest.** `lightrag-index`
  handles this via LightRAG's native doc-hash dedup; no special-casing
  needed. The `scan` pipeline does not auto-trigger `lightrag-index`
  in v1.

## Documentation Plan

| Doc | Change | Why |
| --- | ------ | --- |
| [`docs/adr/ADR-0017-kb-layer-strategy.md`](../adr/ADR-0017-kb-layer-strategy.md) | New row in Consequences table; update "Affects" to cite this plan | Canonical stage-score record |
| [`docs/search-internals.md`](../search-internals.md) | New "Graph retrieval (Stage 2)" section | Where search behaviour is explained operationally |
| [`CLAUDE.md`](../../CLAUDE.md) | `search` bullet gains `--graph` mention; new `lightrag-index` / `lightrag-search` bullets in Commands block | New subcommands must be discoverable from the top of the onboarding doc |
| [`pyproject.toml`](../../pyproject.toml) | Add `[project.optional-dependencies].graph = ["lightrag-hku==1.4.14", ...]` | Install-time opt-in |
| [`docs/solutions/rejected-paths/`](../solutions/) (conditional) | Create the directory + a post-mortem, only if Stage 2 lands 0–1/25 | ADR-0017 Related Debt explicitly flags this directory as missing |

## References

### Internal

- [ADR-0017: Staged KB-Layer Strategy](../adr/ADR-0017-kb-layer-strategy.md)
  — the decision this plan executes against.
- [ADR-0010: LLM Concept Normalization](../adr/ADR-0010-llm-concept-normalization.md)
  — the parallel vocabulary Stage 2 explicitly does not merge with.
- [ADR-0012: Vector Search via LanceDB + Voyage AI](../adr/ADR-0012-vector-search-lancedb-voyage.md)
  — the embedding stack reused for LightRAG.
- [ADR-0013: Hybrid Search RRF Fusion](../adr/ADR-0013-hybrid-search-rrf-fusion.md)
  — the retrieval path LightRAG is additive to.
- [ADR-0016: Vector DB Path Config](../adr/ADR-0016-vector-db-path-config.md)
  — the cloud-sync filesystem rule that also applies to `lightrag_dir`.
- [docs/plans/2026-04-20-feat-kb-stage1-query-expansion-plan.md](./2026-04-20-feat-kb-stage1-query-expansion-plan.md)
  — Stage 1 plan; the eval-harness wiring pattern Stage 2 extends.
- [docs/brainstorms/2026-04-19-kb-layer-staged-experiments-brainstorm.md](../brainstorms/2026-04-19-kb-layer-staged-experiments-brainstorm.md)
  — the consolidated brainstorm ADR-0017 sits on top of.
- [docs/testing.md](../testing.md) — how to run the eval; N/25
  interpretation.
- [docs/search-internals.md](../search-internals.md) — current
  hybrid-search mechanics; Stage 2 extends this doc.
- [scripts/video_intel.py:2365](../../scripts/video_intel.py#L2365)
  `chunk_transcript()` — reused by `_transcript_chunking_func`.
- [scripts/video_intel.py:2474](../../scripts/video_intel.py#L2474)
  `build_search_index()` — shape template for `lightrag_index()`.
- [scripts/video_intel.py:2567](../../scripts/video_intel.py#L2567)
  `hybrid_search()` — output-schema contract that
  `lightrag_search()` must match.
- [scripts/gemini_common.py](../../scripts/gemini_common.py) —
  `require_*` pattern, Gemini retry helpers, Voyage client reuse.
- [tests/evals/test_search_quality.py:79](../../tests/evals/test_search_quality.py#L79)
  — harness that gains the `VIDEO_INTEL_EVAL_MODE` toggle.
- [tests/evals/golden_dataset.yaml](../../tests/evals/golden_dataset.yaml)
  — the frozen 25-query benchmark.
- [specs/agent-rules.md](../../specs/agent-rules.md) — the
  failure-mode-first rule-writing discipline this plan follows in
  acceptance criteria.

### External

- [LightRAG GitHub](https://github.com/hkuds/lightrag) — v1.4.14
  reference; README notes "does not accept pre-extracted entities as
  input hints" (relevant to the two-vocabulary cost).
- [LightRAG paper (EMNLP 2025)](https://arxiv.org/abs/2410.05779) —
  dual-level graph retrieval rationale cited in ADR-0017.
- [LightRAG ProgrammingWithCore docs](https://github.com/hkuds/lightrag/blob/main/docs/ProgramingWithCore.md)
  — `LightRAG(...)` / `QueryParam(...)` / `ainsert` / `aquery` API.
- [LightRAG OfflineDeployment docs](https://github.com/hkuds/lightrag/blob/main/docs/OfflineDeployment.md)
  — optional extras (`[offline-storage]`, `[offline-llm]`).
- [Voyage 4 series blog](https://blog.voyageai.com/2026/01/15/voyage-4/)
  — asymmetric retrieval (doc vs query models) relied on by both
  `hybrid_search` and `lightrag_search`.

### Related work / institutional learnings

- Stage 1 post-mortem lives inline in
  [ADR-0017 Consequences](../adr/ADR-0017-kb-layer-strategy.md#baseline-and-future-measurements):
  taxonomy coverage, not the expansion mechanism, was the binding
  constraint. Stage 2 builds a new vocabulary directly from transcript
  content so it is not bottlenecked on the same artifact.
- Q15 (the one passing query from baseline) shows BM25 dominance on
  exact-token overlap. The eval will tell us if LightRAG's `mode="local"`
  keeps that query green or if graph retrieval trades precision on
  exact-match queries for recall on paraphrase queries.
- The
  [2026-04-16 architecture-futures note](../../work/2026-04-16/03-architecture-futures-cognee-lightrag-llm-wiki.md)
  is the narrative archaeology of why LightRAG won over Cognee; read
  for context, not for decision authority (that's ADR-0017).
