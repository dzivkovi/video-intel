# Intelligence Layer Roadmap — From Hybrid Retrieval to Discovery & Synthesis

**Date:** 2026-05-28

**Status:** brainstorm — supersedes the session notes in `work/2026-05-28/` as the public-facing forward direction. Promoted from work-note draft after a multi-source consolidation (today's brainstorm + Codex deep-research + the 2026-04-28 7-agent synthesis + a parallel ChatGPT chat focused on graph-schema modeling + the 2026-04-22 nugget brief). Not yet an ADR; ADR-grade decisions will graduate out of this doc as evidence accumulates.

## What this is

A forward-facing guide to where the video-intel knowledge layer is going next, why, and in what order. Designed to be readable cold by future-you in three months — or by anyone curious about the project — without re-reading the four session notes that fed it.

If you want the day-by-day exploration that produced this, see `work/2026-05-28/02-intelligence-layer-futures-aggregate-contrastive-queries.md` (full brainstorm), `work/2026-05-28/03-codex-executive-summary-intelligence-layer.md` (executive summary), `work/2026-05-28/04-deltas-vs-april-28-synthesis.md` (audit trail of corrections), and `work/2026-04-28/03-knowledge-discovery-synthesis-paths-forward.md` (7-agent convergence one month earlier).

If you want the operative decision on LightRAG, see [`ADR-0018`](../adr/ADR-0018-nugget-cli-cross-creator-synthesis.md). On the staged KB-layer strategy, see [`ADR-0017`](../adr/ADR-0017-kb-layer-strategy.md). On the concept thesaurus that this layer extends, see [`ADR-0010`](../adr/ADR-0010-llm-concept-normalization.md).

## The question class we're solving

The current hybrid search (BM25 + vector + RRF) is excellent at finding passages similar to a query. It cannot answer questions like:

- *"Which AI tools have creators moved away from in the last 90 days, and what did they move to?"*
- *"Which concepts went silent across the corpus that were weekly through April?"*
- *"What's emerging that only one channel is covering — worth a look?"*

These are not retrieval questions. They are **aggregation + polarity + causation + trend** questions. Top-k passage retrieval is the wrong primitive. Vector embeddings collapse negation (NevIR, EACL 2024) — *"love"* and *"don't love"* sit close in embedding space, so any question with "no longer" or "stopped" is structurally blind to the very signal it asks for.

The fix is not a better retriever. It is a **structured intelligence layer** sitting alongside hybrid search, that turns transcripts into typed claims and answers aggregate questions in SQL rather than top-k.

## The architectural answer in one paragraph

A DuckDB structured backbone holds the corpus as a graph-shaped relational store (Source → Artifact → Segment → Entity / Concept / Claim) with every claim provenance-linked to the exact transcript span that supports it. Discovery is bidirectional (Displacement + Magnet lenses) and produces *receipts plus a health signal*, not a finished brief. The host LLM (you, or Claude in a session) owns the synthesis layer. The existing LanceDB hybrid index remains in force as the retrieval primitive for evidence look-up. The schema is deliberately minimal at start — six node types, six edge types — and evolves via multi-label specialization rather than rebuild.

## The cross-corpus unification

The two questions that motivated this — *"where are people no longer flying"* and *"where are people suddenly wanting to travel"* — are the literal frozen prompts in a sibling skill, [`xb-travel`](https://github.com/dzivkovi/horizon-scanner). That skill answers them against travel discourse via `/last30days`. This project will answer the same shape of question against the video corpus.

The two projects are **siblings under the same contract**:

| | xb-travel | video-intel (after this roadmap) |
|---|---|---|
| Question class | Displacement + Magnet | Displacement + Magnet |
| Retrieval backend | `/last30days` (web + social) | LanceDB hybrid + DuckDB analytical |
| Output contract | Per-lens markdown receipts + audit JSON | Per-lens markdown receipts + audit JSON |
| Health signal | PROCEED / CAVEAT / REFUSE | PROCEED / CAVEAT / REFUSE |
| Synthesis | Host LLM owns it | Host LLM owns it (`nugget` CLI already does this) |
| Schema | implicit in receipts | Generic schema below, with Video overlay |

The unification is not "one tool that does both." It is "**two retrieval engines, one output contract, one lens vocabulary.**" Later, a thin orchestrator could run both against the same question and combine the receipts into a cross-corpus brief.

## The schema — calming starter

Deliberately minimal. Six node types. Six edges. *That is enough to start.*

### Graph view (Cypher)

```text
(:Source)-[:PUBLISHED]->(:Artifact)
(:Artifact)-[:HAS_SEGMENT]->(:Segment)
(:Segment)-[:MENTIONS]->(:Entity)
(:Segment)-[:EXPRESSES]->(:Claim)
(:Claim)-[:ABOUT]->(:Concept)
(:Segment)-[:HAS_CONCEPT]->(:Concept)
```

### SQL view (DuckDB) — same shape, relational

Tables for nodes:

```sql
CREATE TABLE sources    (source_id UUID PRIMARY KEY, name VARCHAR, kind VARCHAR);
CREATE TABLE artifacts  (artifact_id UUID PRIMARY KEY, source_id UUID, kind VARCHAR, title VARCHAR, published_at DATE, url VARCHAR);
CREATE TABLE segments   (segment_id UUID PRIMARY KEY, artifact_id UUID, position INTEGER, start_seconds INTEGER, text TEXT);
CREATE TABLE entities   (entity_id UUID PRIMARY KEY, canonical VARCHAR, kind VARCHAR);
CREATE TABLE concepts   (concept_id UUID PRIMARY KEY, canonical VARCHAR, first_seen_at DATE);
CREATE TABLE claims     (claim_id UUID PRIMARY KEY, statement TEXT, target VARCHAR, stance VARCHAR, time_horizon VARCHAR);
```

Tables for edges (join tables — these are where provenance lives, see next section):

```sql
CREATE TABLE published       (source_id UUID, artifact_id UUID, PRIMARY KEY (source_id, artifact_id));
CREATE TABLE has_segment     (artifact_id UUID, segment_id UUID, PRIMARY KEY (artifact_id, segment_id));
CREATE TABLE mentions        (segment_id UUID, entity_id UUID, PRIMARY KEY (segment_id, entity_id));
CREATE TABLE expresses       (segment_id UUID, claim_id UUID, /* + provenance columns */ );
CREATE TABLE about           (claim_id UUID, concept_id UUID, PRIMARY KEY (claim_id, concept_id));
CREATE TABLE has_concept     (segment_id UUID, concept_id UUID, PRIMARY KEY (segment_id, concept_id));
```

Both views describe the **same shape**. Graph databases give you visual exploration and traversal; DuckDB gives you SQL aggregates and full-text. Either can answer the questions we need, and the schema is portable between them.

### Multi-label evolution — how schemas grow without rebuild

When a vertical specialization emerges (travel data joins, blog posts get ingested), we **add labels to existing nodes** rather than build new tables:

```text
(:Artifact:Video)      — video, the bootstrap case
(:Artifact:BlogPost)   — added when blog posts arrive
(:Artifact:CodeRepo)   — added when source READMEs join
(:Concept:TravelTrend) — Travel overlay on a generic Concept
(:Entity:Destination)  — Travel-specific entity subtype
(:Claim:PolicyClaim)   — Legal/policy overlay
```

In SQL, this translates to **kind columns** on the node tables plus optional overlay tables for specialization fields when needed. Same principle: existing data isn't migrated; new shape is added.

### Stable core, evolving overlays

The six-node / six-edge starter is the **stable core**. It is the same across every corpus we'll ever ingest — video, travel discourse, blogs, code, policy docs.

Overlays are the **vertical specializations** added as the corpus grows:

- **Video overlay** — Creator (`:Source:Creator`), Video (`:Artifact:Video`), TranscriptSegment (`:Segment:TranscriptSegment` with timestamp_seconds)
- **Travel overlay** — TravelTrend (`:Concept:TravelTrend`), Destination (`:Entity:Destination`), Origin (`:Entity:Origin`)
- Future overlays (Legal, Medical, Corporate) follow the same pattern

You do not design overlays up front. You observe what your data wants to become and add labels as patterns emerge. The starter schema is **a hypothesis you collect evidence against**, not architecture carved in stone.

## Provenance — how every claim links to its evidence

This is the section most likely to be skimmed and shouldn't be. Provenance is the contract that makes everything else trustworthy. Without it, briefs are confident-looking summaries with no audit trail. With it, every claim a brief surfaces can be opened to its exact verbatim source.

### The principle

**Every claim the system surfaces cites the exact transcript span that supports it, the model that extracted it, the prompt version used, and the confidence with which it was extracted.** No claim is ever presentable without `quote @ source @ timestamp`. This isn't "later if we have time" — it is the foundation that distinguishes a real brief from a hallucination that looks like one.

### The mechanism — `:EXPRESSES` as the provenance edge

The 6-edge starter has one edge that does double duty:

```text
(:Segment)-[:EXPRESSES]->(:Claim)
```

Read forward: *"this transcript segment expresses this claim."*
Read backward: *"this claim is supported by these segments."*

Edges can carry properties. The `:EXPRESSES` edge gets enriched with extraction metadata:

```cypher
(:Segment {segment_id, video_id, timestamp_seconds, text})
  -[:EXPRESSES {
    quote: "verbatim transcript span",
    confidence: 0.0..1.0,
    extractor_model: "gemini-2.5-flash-preview-04-17",
    prompt_version: "concepts_v3.1",
    extracted_at: "2026-05-15T14:23:00Z"
  }]->
(:Claim {claim_id, statement, target, stance, time_horizon})
```

In DuckDB, the same shape lives as columns on the `expresses` join table:

```sql
CREATE TABLE expresses (
  segment_id      UUID NOT NULL REFERENCES segments(segment_id),
  claim_id        UUID NOT NULL REFERENCES claims(claim_id),
  quote           TEXT NOT NULL,            -- verbatim transcript span
  confidence      FLOAT NOT NULL,            -- LLM-extracted, 0..1
  extractor_model VARCHAR NOT NULL,          -- "gemini-2.5-flash..."
  prompt_version  VARCHAR NOT NULL,          -- "concepts_v3.1"
  extracted_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (segment_id, claim_id, prompt_version, extractor_model)
);
```

### A concrete example end-to-end

A weekly brief surfaces the claim *"Developers shifted from Cursor toward Claude Code in early 2026."* Behind that single sentence, in the database:

| Field | Value |
|---|---|
| `claim.statement` | *"Developers shifted from Cursor to Claude Code in early 2026"* |
| `claim.target` | `Cursor` |
| `claim.stance` | `critiques` (the speaker is reporting a shift away) |
| `claim.time_horizon` | `short` |
| `expresses.segment_id` | Y Combinator channel video (`qwmmWzPnhog`), transcript chunk at 03:15 |
| `expresses.quote` | *"For a while I was deeply in Cursor… Then I kind of moved over to Claude Code, especially with Opus."* |
| `expresses.confidence` | 0.85 (illustrative — would be populated by the actual extraction pipeline) |
| `expresses.extractor_model` | `gemini-2.5-flash-preview-04-17` |
| `expresses.prompt_version` | `concepts_v3.1` |
| `expresses.extracted_at` | `2026-05-28T14:23Z` |

That single row of metadata is the full audit trail. Any UI rendering the claim shows:

> *Developers shifted from Cursor to Claude Code* — [Calvin French-Owen on YC @ 03:15](https://www.youtube.com/watch?v=qwmmWzPnhog&t=195): *"For a while I was deeply in Cursor… Then I kind of moved over to Claude Code, especially with Opus."* (confidence 0.85)

The quote is verbatim from Calvin French-Owen's appearance on YC's *"We're All Addicted To Claude Code"* (published 2026-02-06) — surfaced via hybrid search against the existing corpus. The extraction-pipeline metadata (`confidence`, `extractor_model`, `prompt_version`, `extracted_at`) is illustrative; those values would be populated when the actual `concepts.md` extractor runs against this segment.

When two segments support the same claim, you get two rows in `expresses`. The brief can show both quotes, pick the highest-confidence one, or aggregate counts. The data is always there.

### Why this preserves minimalism

We did not add a 7th edge type (`:SUPPORTED_BY`) to carry provenance. The 6-edge starter stays intact. We did not add a separate `provenance` table parallel to `expresses`. The metadata lives on the edge it describes.

The insight: **provenance is not a separate concern. It is a property of the relationship that connects evidence to assertion.** The starter schema already has that edge; we give it the columns it needs.

If we had modeled provenance as a sibling edge with the same metadata, we'd have:

- 7 edge types instead of 6 (worse minimalism)
- The same data on two edges (duplication risk)
- Two queries to render one claim with its citations (worse ergonomics)

Provenance-as-edge-property keeps everything in one place.

### Reading provenance backwards — the query that powers every brief

Given a claim, find the supporting evidence:

**Cypher:**

```cypher
MATCH (s:Segment)-[e:EXPRESSES]->(c:Claim {claim_id: $cid})
RETURN s.video_id, s.timestamp_seconds, e.quote, e.confidence
ORDER BY e.confidence DESC
```

**DuckDB SQL:**

```sql
SELECT s.artifact_id  AS video_id,
       s.start_seconds AS timestamp_seconds,
       e.quote,
       e.confidence
FROM expresses e
JOIN segments s ON s.segment_id = e.segment_id
WHERE e.claim_id = $claim_id
ORDER BY e.confidence DESC;
```

Both return the same shape: segments, quotes, confidence scores, ordered by evidence strength. That is the input every brief and every UI needs to render a claim with its receipts.

## The two-lens discovery framework

Single-direction queries ("top concepts this week") return lists. Bidirectional queries return **substitution maps**, which are what actually answer the questions we care about. Borrowed verbatim from the sibling `xb-travel` skill:

| Lens | Strategic question | What it computes (video-intel) |
|---|---|---|
| **Displacement** | *Where is attention shifting from, and where is it landing instead?* | Concepts whose mention-frequency dropped sharply ("silently fading") + emerging concepts in the same channels that arrived in their place. Output: pairs `(faded_concept, emerging_concept, channels_overlap, evidence_quotes)`. |
| **Magnet** | *What's newly hot and why?* | Concepts with sharp positive EWMA momentum, multi-channel adoption, plus zero-shot NLI on transcript excerpts to surface the *why*. Output: ranked emerging concepts with discussed reasons and citations. |

The lenses are **frozen prompts** — not user-tunable per run. This is on purpose: the same two questions every week means weekly briefs are diffable. *"Two earns the right to become three only after quarterly review identifies a recurring blind spot."*

## The receipts-vs-synthesis contract

Mirrors the xb-travel architecture verbatim. Two separable layers:

1. **Runner produces receipts.** Per-lens markdown documents (one for Displacement, one for Magnet) with citations + timestamps + verbatim quotes. Plus a structured audit JSON capturing what ran, what was found, and how strong the evidence is.
2. **Host LLM owns synthesis.** A separate LLM call reads the receipts and writes the human-facing brief. Optional. The receipts alone are usable.

Why split? Because synthesis is high-variance ("the same data, written 3 different ways, produces 3 different stories") and receipts are deterministic. Separating them means:

- The receipts are the audit trail. Re-running on the same data produces identical receipts.
- The synthesis layer can be swapped (different prompts, different models) without changing the underlying intelligence.
- A failed synthesis call doesn't destroy the week's work — receipts persist.

This is also the same architecture the existing [`nugget` CLI (ADR-0018)](../adr/ADR-0018-nugget-cli-cross-creator-synthesis.md) follows: structured retrieval + LLM prompt for synthesis. Now made explicit and extended to discovery, not just query-time.

## The PROCEED / CAVEAT / REFUSE health signal

Borrowed verbatim from xb-travel. The audit JSON has a `health_summary` block positioned early in the file so consumers can branch on REFUSE without parsing the bulky payload.

```jsonc
{
  "health_summary": {
    "run_status": "ok | degraded | failed",
    "action": "PROCEED | CAVEAT | REFUSE",
    "lenses_passed": ["displacement", "magnet"],
    "lenses_thin": [],
    "lenses_failed": [],
    "decision_guidance": "..."
  },
  ...
}
```

State machine for video-intel:

| | magnet=pass | magnet=thin | magnet=fail |
|---|---|---|---|
| **displacement=pass** | PROCEED | CAVEAT | REFUSE |
| **displacement=thin** | CAVEAT | CAVEAT | REFUSE |
| **displacement=fail** | REFUSE | REFUSE | REFUSE |

Triggers:

- `PROCEED` — both lenses surfaced ≥3 high-confidence concepts.
- `CAVEAT` — thin evidence on one lens (e.g., silent-fading returned nothing — perhaps corpus too stable that week, or threshold needs tuning).
- `REFUSE` — `taxonomy.json` missing/stale, scan window too short, or zero new artifacts in window.

This block prevents the *confident-looking weekly brief over an empty data week* failure mode. A brief with a missing lens is not a worse brief — it is a different category of artifact, a hallucination that *looks* like a brief.

## The phased plan — stoppable at every boundary

| Phase | What | Effort | Why |
|---|---|---|---|
| **0a** | Write `prompts/discovery-brief-window.md`. Run manually against the most-recent scan output. | 30 min | Falsifies whether a briefing produces signal beyond scan logs *before* any infrastructure. Tracked as [#61](https://github.com/dzivkovi/video-intel/issues/61) — *shipped via [PR #62](https://github.com/dzivkovi/video-intel/pull/62) (the prompt now exists; manual run + iteration is the human next step)*. |
| **0b** | Extend `tests/evals/golden_dataset.yaml` with 5–10 contrastive / aggregate / polarity queries under `query_type: aggregate`. ADR-0017 amendment. Conditional on 0a producing signal. | 1 day | Gate for Phases 1–3. Without a measurable target, ranking is vibes. |
| **0c** | If 0a fails to surface signal → pivot to **Path B**: aggregate existing `skip_video_ids` / `enabled:false` / `skip_modes` signals into a negative-filter intelligence layer. | 0.5–1 day | Compounds independently of discovery. The skip-corpus is silently-labeled training data nothing currently consumes. |
| **1** | DuckDB structured backbone with the 6-node / 6-edge starter (above). Authority inversion: concepts become the source of truth; mindmaps become regenerable. Silent-fading detector ships here. | 2–4 days | Foundation for everything analytical. Already answers Displacement-half ("what stopped") with zero new LLM passes. |
| **1.5** | **Parallel** Neo4j Graph Builder learning spike on 2–3 known videos via knowledge packets. Deliverable: written observations document (useful labels / useless labels / useful edges / missing provenance / surprising connections). | 1 afternoon | Visually validates the starter schema. Breaks the schema-fear stall. Spike-only — does not affect Phase 1 production decisions. Tracked as [#63](https://github.com/dzivkovi/video-intel/issues/63), parallel to the now-shipped #61. |
| **2** | Two-lens briefing runner: produces per-lens markdown receipts + audit JSON with `health_summary`. Operations underneath the lenses: EWMA momentum, silent-fading detector, cross-creator NLI (`cross-encoder/nli-deberta-v3-small`, local CPU). | 3–5 days | The receipts layer. Host LLM owns synthesis. Same contract as xb-travel. |
| **3** | Add the 6-tuple stance schema as **one sentence in `prompts/concepts.md`**: `{claim, target, time_horizon, confidence, evidence_span, counterevidence}`. Stored as properties on the `expresses` join (DuckDB) / edge (Neo4j). | 1 afternoon | Closes the polarity gap. Cheap because piggybacks on existing extraction call. |
| **4 (later)** | Pre-bake top-20 stable / evergreen topics from `nugget` query patterns into wiki pages with citation discipline. Long-tail stays query-time via `nugget`. | 1 week | Consulting UX surface — when query traffic has shape. |
| **F (deferred per ADR-0018)** | LightRAG — gated on three signals (see ADR). | — | Heavy retrieval graph layer; not yet justified. |

Total elapsed if everything ships through Phase 4: **~2–3 weeks of evenings**. Stoppable at every phase boundary.

## Three-question acceptance gate

A practical test the schema and Phase 1+2 capability must pass. From the 2026-05-28 ChatGPT chat verbatim — questions any first useful intelligence layer must answer:

1. *Which concepts are appearing across multiple creators?*
2. *Which creators use different words for the same idea?*
3. *Which claims or themes are emerging in the last 30 days?*

Mapping to the schema:

- Q1 → `SELECT concept_id, COUNT(DISTINCT source_id) FROM has_concept JOIN segments JOIN artifacts JOIN published WHERE published_at > now() - interval '30 days' GROUP BY 1 HAVING COUNT(DISTINCT source_id) >= 3`
- Q2 → alias graph: `concepts` table + `expresses` quotes show different surface vocabulary; existing `taxonomy.json` aliases populate this
- Q3 → Magnet lens (Phase 2)

If the Phase 1 starter + Phase 2 operators cannot answer these three, the schema isn't right yet and we iterate before building further.

## Open design questions to resolve as work unfolds

These are not blockers. They emerge from running the spike and Phase 1, not from upfront analysis:

- **Identity keys.** What's the unique ID for an `:Entity`? Canonical label slug? URI? Hash of canonical + kind? Decide when alias-resolution rules concretize.
- **Deduplication.** When two extractions produce the same `:Claim` with slightly different wording, do they merge or stay separate? Probably stay separate at write-time; merge during querying via canonical statement.
- **Alias policy.** How does Entity dedup work — fuzzy match? Canonical lookup against existing `taxonomy.json`? Periodic LLM canonicalization pass?
- **Index / constraint design.** Which columns get DuckDB indexes? Where do unique constraints go? Emerges from query patterns, not specs.
- **Reconciliation.** If the Neo4j Graph Builder spike (Phase 1.5) extracts entities and claims, do those feed forward into the controlled DuckDB store, or stay quarantined as learning artifacts? Default for v1: quarantine. Reconciliation is its own design problem.

These get captured in `work/<date>/` notes as the spike and Phase 1 surface them.

## Project principles adopted from this brainstorm

Two reframes worth pinning, both from the 2026-05-28 ChatGPT chat:

1. **"The graph is not only something you design. It is something you observe."** The starter schema is a hypothesis. Running the Graph Builder spike on real videos surfaces which node labels are useful, which edges are noise, which concepts should be aliases. We design *less* and observe *more*.
2. **"I am not designing the final graph schema; I am collecting evidence for the first useful graph schema."** The minimal starter is deliberate. It is the smallest schema that can answer the three-question gate. Specialization is added when the data demands it, not before.

Combined with two existing project disciplines:

3. **"Stable core, evolving overlays."** Source → Artifact → Segment → Claim/Entity/Concept stays stable across every corpus and vertical. Overlays (Video, Travel, Legal, Medical) layer on via multi-label specialization.
4. **"Two earns the right to become three only after quarterly review identifies a recurring blind spot."** Borrowed from xb-travel. Lenses, prompts, schema features — none earn third-instance status without empirical evidence of need.

## Tools — adopt / defer / reject

### Adopt

- **DuckDB** — the structured intelligence store. Embedded, single-file, columnar. Arrow zero-copy bridge to LanceDB (proven for years).
- **LanceDB** — existing hybrid retrieval; not replaced. Stays the primitive for evidence look-up.
- **Voyage AI embeddings** — existing; not replaced.
- **Neo4j Graph Builder** — learning / visualization spike only (not production). User already has Docker + Neo4j running; ops cost is real but bounded.
- **arrows.app** — sketch the *believed* schema before feeding the Graph Builder.
- **Neo4j Bloom** — inspect what got loaded into the graph after the spike.
- **`cross-encoder/nli-deberta-v3-small`** — zero-shot NLI for the "why" surface on the Magnet lens (Phase 2). Local CPU, no API cost.

### Defer

- **LightRAG** — per [ADR-0018](../adr/ADR-0018-nugget-cli-cross-creator-synthesis.md). Three named signals gate promotion.
- **LangExtract (Google)** — Phase-5 escape hatch only. Adopt if the 6-tuple-in-`prompts/concepts.md` proves structurally insufficient.
- **LlamaIndex `DynamicLLMPathExtractor`** — explore during Phase 1.5 spike as comparison to Graph Builder. Do not adopt as primary extractor; the 6-tuple-in-concepts.md path ships first.
- **DuckPGQ** — the escape hatch if SQL aggregates ever need real graph traversal. Stays inside DuckDB; no new vendor.
- **Neo4j Data Importer** — named in the brainstorm; no role in tomorrow's spike. Revisit if a structured-import workflow becomes necessary.

### Reject

- **Microsoft GraphRAG as production pipeline** — reference architecture only. Documentation is useful; methodology bend is not.
- **Cognee** — duplicates existing concept extraction; multi-tenant overkill for solo research use; pre-1.0 dependency risk. Rejected in 2026-04-16 brainstorm, reaffirmed here.
- **Kuzu** — archived October 10, 2025. The escape hatch is DuckPGQ inside DuckDB, not a separate graph DB.

## How this relates to prior thinking

This roadmap stands on:

- [`ADR-0010`](../adr/ADR-0010-llm-concept-normalization.md) — the LLM concept thesaurus that becomes the seed vocabulary for the schema.
- [`ADR-0012`](../adr/ADR-0012-vector-search-lancedb-voyage.md) — LanceDB + Voyage AI vector retrieval (not replaced).
- [`ADR-0013`](../adr/ADR-0013-hybrid-search-rrf-fusion.md) — BM25 + vector + RRF hybrid (not replaced).
- [`ADR-0017`](../adr/ADR-0017-kb-layer-strategy.md) — the staged KB-layer strategy that this roadmap operationalizes.
- [`ADR-0018`](../adr/ADR-0018-nugget-cli-cross-creator-synthesis.md) — the `nugget` CLI receipts→synthesis architecture and the three-signal LightRAG gate (operative rule).
- [`docs/brainstorms/2026-04-19-kb-layer-staged-experiments-brainstorm.md`](2026-04-19-kb-layer-staged-experiments-brainstorm.md) — the precursor brainstorm that promoted "let the benchmark decide" into ADR-0017.
- [`examples/nugget-lightrag-vs-openbrain-architectural-tension.md`](../../examples/nugget-lightrag-vs-openbrain-architectural-tension.md) — the user's "Compiled Graph on SQL" thesis. The architectural pattern is named there: *"SQL = immutable source of truth, Graph/Wiki = disposable regenerable presentation layer."*
- The sibling [`xb-travel` skill](https://github.com/dzivkovi/horizon-scanner) — provides the lens vocabulary, the receipts/synthesis contract, and the PROCEED/CAVEAT/REFUSE health-summary pattern.

When this roadmap's decisions firm up (likely after Phase 1.5 spike + Phase 2 ships), they graduate into ADRs and this doc gets a "consolidated into ADR-XXXX" status block at top — matching the existing pattern from `docs/brainstorms/2026-04-19-kb-layer-staged-experiments-brainstorm.md`.

## Tomorrow morning — the concrete first moves

Two issues, parallel, both small:

1. **[GH issue #61 — `feat(prompts): add discovery-brief-window for Phase 0a manual validation`](https://github.com/dzivkovi/video-intel/issues/61).** Write the prompt. Run it manually on the last week of scan output. Iterate ≤3 times. Capture the briefs in the PR description. Binary outcome: did the brief surface 2–3 things you'd miss from scan logs? If yes, Phase 1 unfolds next week. If no, pivot to Phase 0c.
2. **[GH issue #63 — `feat(spike): Neo4j Graph Builder schema-exploration on 2–3 known videos`](https://github.com/dzivkovi/video-intel/issues/63).** Build one knowledge packet per video (Metadata + Existing Concept Map + Existing Canonical Concepts + Rich Transcript per Artifact 40 of the May-28 ChatGPT chat). Feed to Graph Builder. Visually inspect. Write observations to `work/<date>/NN-graph-builder-spike-observations.md` with five sections: useful labels / useless labels / useful edges / missing provenance / surprising connections. Do NOT refactor video-intel.

Both deliverables produce evidence. Phase 1 ships against the union of that evidence — not against a pre-design.

> *"You are not stalled because you lack skill. You were stalled because you were trying to design a future-proof model before seeing what the data wants to become. Tomorrow, let the data speak first. Then we tighten it."*

—

*This brainstorm will be revised as Phase 0a and Phase 1.5 produce evidence. The schema, the lenses, and the phase plan are all subject to refinement based on what the spike actually surfaces. Decisions firm up in ADRs; brainstorms hold the trajectory.*
