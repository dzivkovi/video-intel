# SPEC: Weak-Signal / Commonality-Detection Layer (DuckDB truth + Neo4j-GDS lens)

**Issue:** [#85](https://github.com/dzivkovi/video-intel/issues/85)
**Date:** 2026-07-02 (dark-factory overnight run)
**Status:** executed this session; see `work/2026-07-02/` (session-local, not committed) for pen-test, gate results, and observations.

## Goal

Prove that a graph lens over the existing corpus recovers known cross-vocabulary commonalities that hybrid search misses, with a citation (quote @ video @ timestamp) on every surfaced link. Build the machine; verify it on known signals; do not hunt unknown signals.

## Non-goals

- No new Gemini extraction pass. Everything loads from existing `concepts.json`, `taxonomy.json`, `*.transcript.md`, `*.meta.json`.
- No generalization beyond video transcripts (schema core stays corpus-agnostic by construction, but no blog/code overlays now).
- No LightRAG, no 7th edge type, no upfront overlay design.
- Does not touch the scan/transcript/mindmap/search pipeline or its config.

## Architecture

One new standalone script, `scripts/intel_graph.py` (operationally separate from `video_intel.py`, same pattern as `translate_video.py`; it imports `chunk_transcript` from `video_intel.py` so segments share the exact grain of the LanceDB index). Three subcommands:

1. `load` - build the DuckDB truth store from corpus artifacts.
2. `project` - wipe and rebuild the Neo4j projection from DuckDB, then run Louvain + PageRank via GDS and write results back into DuckDB (the graph is disposable; algorithm outputs persist in truth).
3. `verify` - run the issue #85 acceptance gate against DuckDB and emit a results JSON + human-readable report.

### Data flow

```text
corpus (G: Drive)                DuckDB (local cache)             Neo4j (disposable)
meta.json ──────────┐
transcript.md ──────┤  load      sources/artifacts/segments       VI_Entity nodes
concepts.json ──────┼────────►   entities/concepts/claims  ────►  CO_OCCURS edges
taxonomy.json ──────┘            + edge tables w/ provenance      Louvain + PageRank
                                        ▲                              │
                                        └──── community/rank ──────────┘
                                             (written back)
```

### DuckDB schema (6 nodes / 6 edges per the 2026-05-28 roadmap, with two documented bends)

Node tables: `sources`, `artifacts`, `segments`, `entities`, `concepts`, `claims`.
Edge tables: `published`, `has_segment`, `mentions`, `expresses`, `about`, `has_concept`.

Natural keys everywhere (channel name, `video_id`, `video_id:position`, slugs) - deterministic regeneration beats UUIDs for a rebuildable store.

**Bend 1 (dual-grain `has_concept`):** concepts were extracted from mindmaps, so observations are video-grain. `has_concept` carries `artifact_id` (always) plus nullable `segment_id` (filled when deterministic lexical grounding finds the surface phrase in a transcript segment). Segment grain is what makes citations possible; artifact grain is what the co-occurrence projection actually needs.

**Bend 2 (degenerate claims):** with no stance extraction allowed, claims are templated discussion-claims ("{channel} discusses {concept} (as '{surface phrase}')", stance=`discusses`). The `claims`/`expresses`/`about` plumbing is exercised with real provenance columns; semantic payload waits for the roadmap Phase 3 stance schema.

**Entity = surface term.** `entities.kind = 'surface_term'`; one entity per distinct normalized `as_mentioned` phrase. This is the vocabulary layer the alias-recovery gate operates on.

### Provenance discipline (gate requirement 3)

`expresses` rows exist only where lexical grounding found a segment; they carry `quote`, `confidence` (from concepts.json), `extractor_model` (original Gemini model from meta.json + `+lexical-grounding-v1`), `prompt_version`, `extracted_at`. The verify command surfaces only claims that have at least one `expresses` row. Ungrounded claims stay in DuckDB but are never presented.

### Anti-circularity rule (gate requirement 1)

The Louvain input graph is built ONLY from shared-artifact co-occurrence between surface-term entities. Taxonomy alias sets and `concept_id` normalization NEVER contribute edges - they are the answer key. Aliases of one concept usually appear in different videos (that is the whole point), so recovery must come from shared neighborhoods, not direct co-occurrence.

### Neo4j projection

- All nodes labeled `VI_Entity` (namespaced); `project` deletes only `VI_`-prefixed labels on rebuild. Runs Louvain (seeded config, concurrency 1 for reproducibility) and PageRank via GDS, writes `community_id` and `pagerank` back to DuckDB `entities`.
- Co-occurrence edges computed in DuckDB SQL (relational strength), written as weighted `CO_OCCURS` relationships (graph strength). Weight = count of shared artifacts.
- Hub-noise lever: `--max-df` drops terms appearing in more than N artifacts before projection (generic terms glue everything into one megacommunity).

## Acceptance gate (issue #85 "what good looks like")

1. **Alias recovery:** for every taxonomy concept observed as >= 2 distinct surface terms, compute cohesion = max fraction of its terms landing in one community. Report mean cohesion vs a label-permutation baseline (honesty control).
2. **Known cross-vocab pairs, each with a citation:** a pair is recovered only if the two vocabulary sides share a **modal-anchored** community (the shared community must be the dominant community, ties included, for at least one side and contain at least one term of the other - plain any-overlap proved trivially satisfiable for high-frequency vocabularies like the 67 claude-code terms) that is also below the megacommunity cap, AND a verbatim transcript citation exists.
3. Every surfaced link prints `quote @ video @ timestamp` from a real segment; links without one are reported as not presentable.

Recovered vs missed is reported plainly either way (hard constraint from the issue).

**Amendment (executed run):** GDS Louvain proved nondeterministic (no randomSeed; 5 runs flipped the Factorio pair). The shipped canonical configuration is seeded Leiden (`--algo leiden`, default) with a deterministically ORDERed projection; `--gamma` exposes Leiden's resolution because bridge visibility proved partition-scale-dependent (gamma sweep: 1.0 -> 1/3 pairs, 0.8 -> 2/3, 0.5/0.3 -> megablob -> 0/3). Louvain remains available via `--algo louvain` for comparison. Full analysis: `work/2026-07-02/04-graph-weak-signal-observations.md`.

## Files

- `scripts/intel_graph.py` - the layer (committed).
- `tests/test_intel_graph.py` - unit tests over pure functions + in-memory DuckDB fixtures; Neo4j-dependent tests skip when `NEO4J_URI` unreachable (committed).
- `pyproject.toml` - `intelligence` extras gains `neo4j>=5` (committed).
- `CLAUDE.md` - architecture note for the new script (committed).
- `work/2026-07-02/*.md` - pen-test, gate results, observations, debrief (session artifacts in the main checkout; `work/` is gitignored).

## CLI contract

```bash
python scripts/intel_graph.py load    [--output-dir DIR] [--db PATH] [--force]
python scripts/intel_graph.py project [--db PATH] [--neo4j-uri URI] [--max-df N] [--min-shared M]
python scripts/intel_graph.py verify  [--db PATH] [--report PATH]
```

`--output-dir` falls back to `load_config()` resolution (same four-step chain as video_intel). `--db` defaults to `~/.cache/video-intel/intel.duckdb` (local NTFS per the ADR-0016 precedent: derived, rebuildable, never on a cloud mount). Neo4j creds fall back to `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` env vars.

## Risks / falsifiers

- **Megacommunity risk:** generic surface terms may collapse Louvain into one giant community, making "same community" trivially true. Controls: `--max-df` lever + the permutation baseline + community size distribution in the report. A pair only counts as recovered if its community is smaller than 30 percent of all terms.
- **Grounding sparsity:** `as_mentioned` phrases come from mindmaps and often do not literally appear in transcripts. Expected grounding rate is low; report it. Citations for the three known pairs use direct segment text search as the fallback evidence path (still real quotes with timestamps).
- **Louvain nondeterminism:** mitigated with fixed seed + concurrency 1; residual variance noted in observations if seen across runs.
