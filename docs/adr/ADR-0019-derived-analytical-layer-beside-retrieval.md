# A Derived Analytical Layer Beside Retrieval - When To Add It, When To Refuse It

**Status:** accepted (as a constrained decision record, NOT a framework proposal)

**Date:** 2026-07-13

**Decision Maker(s):** Daniel Zivkovic

**Reviewers:** Claude (Opus 4.8) drafted; Codex (GPT, thread `019f5e69`) reviewed adversarially and set the scope. This ADR deliberately encodes Codex's refusal criteria - it exists to stop the pattern from being inflated into a product architecture before a second consumer justifies it.

## Context

`video-intel` grew a second storage layer beside its vector search:

- **Vector / hybrid layer (LanceDB):** BM25 + vector + RRF over transcript chunks. Answers "find me the passage about X." Semantic retrieval. (See [ADR-0013](ADR-0013-hybrid-search-rrf-fusion.md), [ADR-0016](ADR-0016-vector-db-path-config.md).)
- **Derived analytical layer (DuckDB):** an embedded, columnar, rebuildable store of *extracted, provenance-linked observations* (artifacts, sources, dated `has_concept` rows, `co_occurs`, segments-with-timestamps, claims-with-quotes). Answers aggregate / temporal / relational / network questions retrieval is bad at: who covered a concept first (lead-lag, [issue #93]), which creators overlap more than a null model expects (SDSM, [#98]), what is spiking (Kleinberg bursts, [#103]).

The vector half has already proven portable: it was reused in a second project. The temptation is to declare the *two-layer architecture* a reusable pattern and generalize "any timestamped, attributed corpus benefits from this." That generalization is where this ADR pushes back.

External grounding (this is a recognized shape, not an invention): DuckDB is the "SQLite of OLAP" - embedded, columnar, vectorized, sub-second aggregations/joins/windows on single-machine data, reads Parquet/Arrow, complements rather than replaces OLTP stores ([MotherDuck](https://motherduck.com/learn/duckdb-vs-postgres-embedded-analytics/), [Kestra](https://kestra.io/blogs/embedded-databases)). A relational store beside vector retrieval that tracks documents, chunks, and provenance is a named RAG pattern (DuckDB's native [Lance extension](https://www.confessionsofadataguy.com/embeddings-and-vector-databases-lance-duckdb-arrow/); [cognee's DuckDB integration](https://motherduck.com/blog/duckdb-cognee-sql-analytics-graph-rag/) combining local OLAP + knowledge-graph modeling). Honest limit: DuckDB is single-writer (one writer at a time), unsuited to high-concurrency transactional / many-small-writes workloads ([DuckDB concurrency docs](https://duckdb.org/docs/current/connect/concurrency)). It fits *derived, batch-rebuilt, read-heavy* analytics - exactly how `video-intel` uses it.

## Decision

Treat "a derived analytical layer beside retrieval" as a **constrained decision pattern with refusal criteria**, not a reusable framework. It is a *substrate pattern for corpus intelligence, not a reusable analytics product.* The distinction is load-bearing: the substrate (an embedded analytical store of extracted, provenance-linked observations) is reusable; *which* analytics pay off on it is corpus-specific and must be re-earned per project with its own validation and kill criteria.

### The decisive test (apply before adding the layer)

> **Would users ask questions whose correct answer is a table, timeline, count, join, trend, or network - rather than a passage?**

- If the answer is a **passage** (find the relevant text, summarize it, ground a Q&A answer): the vector layer alone is enough. **Refuse the analytical layer.**
- If the answer is a **table/timeline/count/trend/network** (who first, how often, which overlap, how it changed, who clusters): the analytical layer has a case.

### Add the layer only when ALL of these hold

1. The corpus can be reliably transformed into **structured, provenance-linked observations** (stable entities, dates, sources, claims, relationships) - and extraction quality is good enough that the observations are not a false-confidence machine.
2. You have **concrete aggregate/temporal/relational questions known or strongly expected before you build** (not "maybe we'll find something").
3. The corpus is **single-machine scale** and analytics are **read-heavy / batch-rebuilt** (not high-concurrency transactional).
4. Each analytical feature ships with **its own validation and a pre-registered kill criterion** (see the worked sample: one method was killed on exactly this basis).

### What this pattern MAY claim

- For single-machine, read-heavy corpora with attributed source material, a derived DuckDB layer can *complement* vector retrieval.
- The vector layer is for semantic passage discovery; the DuckDB layer is for structured observations (entities, claims, timestamps, attribution, co-occurrence, counts, joins, windows, cohorts, lead-lag, bursts, provenance-aware audits).
- The layer should be **rebuildable from raw corpus + extraction code**, so it is cheap to *store* and safe to *delete*.
- DuckDB is the simpler, more inspectable substrate for aggregation/chronology/joins/provenance than bolting the same onto a vector store.

### What this pattern MUST REFUSE to claim (Codex's list, adopted)

- That every RAG system needs DuckDB, or that every timestamped corpus benefits.
- That extracted data is **"truth"** without validation. Call it an **extracted observation store / derived fact table**, never a "truth store," unless extraction is deterministic or human-validated. (This ADR corrects `video-intel`'s own earlier "truth store" wording.)
- That embeddings *fundamentally cannot* answer these questions. The claim is **comparative**, not metaphysical: embeddings are *bad at* exact aggregation, chronology, joins, counts, and provenance algebra; DuckDB is the cleaner tool for them.
- That graph/network metrics will be useful by default (they were not here - see kill below).
- That DuckDB replaces OLTP, search, queueing, streaming, or multi-user transactional stores.
- That provenance solves extraction error, or that "rebuildable" means operationally free. The real cost is schema design, extraction prompts, provenance discipline, rebuild logic, evals, false positives, stale derived data, and users learning which numbers to trust.
- That `video-intel` (n=1) proves generality beyond its corpus shape.

## Worked sample (n=1): video-intel

The honest evidence, including the failure that is *central, not a footnote*:

- **Shipped useful:** lead-lag precedence ([#93]), SDSM-validated creator network ([#98], pruned 314 noise pairs to 10), Kleinberg bursts ([#103]). Each answers a table/timeline/network question, not a passage.
- **Killed on its own kill criterion:** term-co-occurrence-graph analytics (betweenness/community detection) - it just rediscovered popularity ([#95] retired it; [#99] PMI/disparity backbone came back inconclusive and was closed won't-pursue). This is the proof that the substrate can be reusable while specific analyses are speculative. Pre-registered kill criteria are what made the retirement honest instead of sunk-cost.
- **Portability status, stated precisely:** the *vector half* was reused in a second project; the *analytical half* has **no second consumer yet**. So the two-layer architecture is, today, a disciplined hypothesis - not a validated pattern. This ADR is written now to *constrain* that hypothesis, not to certify it.

## Transfer test for the next project (e.g. a document-binder RAG)

Do NOT transfer by analogy ("it's also a document corpus"). Apply the decisive test. The DuckDB half helps a binder RAG only if its users would ask questions whose answer is a table/timeline/count/trend/network - for example: "which documents mention this obligation," "how did this policy change over time," "which sources disagree," "what entities recur across contracts," "what deadlines cluster by owner." If the binder is mostly "find the relevant passage and answer from it," the DuckDB half is **wishful transfer - refuse it** until a concrete aggregate question appears.

## Consequences

- **Promotion rule:** this ADR is promoted from "constrained hypothesis" to "validated pattern" only when a *second, independent* project ships the *analytical half* against real aggregate questions with passing validation. Until then, "no framework before a consumer needs it" applies - do not build a shared library, a generic schema, or a cross-project package for the analytical layer.
- **For `video-intel` itself:** no code change. The layer already exists and earns its place here. The one durable correction is terminology - prefer "extracted observation store" over "truth store" in future docs/comments (existing uses are grandfathered, not a refactor target).
- **Reuse mechanism:** this file is written to be copied into another repo's `docs/adr/` as the starting decision record. Keep the decisive test and the refusal list verbatim; replace the worked sample with the new project's own.
