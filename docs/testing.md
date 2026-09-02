# Testing

Operational reference for how this project is tested. For *why* DeepEval
was chosen and the original diagnosis behind the retrieval baseline, see
[ADR-0017](adr/ADR-0017-kb-layer-strategy.md) — read it alongside
"Is the ruler intact?" below, which explains why the historic 1/25 figure
quoted there was never purely a retrieval measurement (issue #190).

Last verified: 2026-09-01

## Two Test Suites, Two Different Guarantees

| Suite | Directory | What it guarantees | How to run |
|-------|-----------|--------------------|------------|
| Unit / integration | [`tests/`](../tests/) | Code correctness — parsing, idempotency, error handling, CLI flags. Runs in seconds. | `pytest tests/ --ignore=tests/evals -v` |
| Measurability audit | [`tests/evals/test_instrument.py`](../tests/evals/test_instrument.py) | That each golden query's gating thresholds are **reachable at all**. Free — one LanceDB column scan, no Voyage call. | `pytest tests/evals/test_instrument.py -v` |
| Retrieval eval | [`tests/evals/test_search_quality.py`](../tests/evals/test_search_quality.py) | Retrieval **quality** against 25 frozen grounded queries. Runs Voyage API, ~1 min and a few cents. | `pytest tests/evals/test_search_quality.py -v -s` |
| Search CLI smoke grid | [`evals/search-eval-queries.md`](../evals/search-eval-queries.md) | Human-run sanity grid for CLI features (`--preview`, `--min-similarity`, `--limit`, dedup). Prose benchmark, not pytest. | Read the file, run queries manually, score 0/1/2. |

The first two are machine-graded. The third is narrative — an eyeball-test
grid kept alongside the code for when adding CLI features, not for
measuring retrieval quality.

## Retrieval Eval — Why DeepEval

Four criteria eliminated most of the field:

- **Pytest-native** — evals should be a `pytest` run, not a separate CLI or
  service. DeepEval metrics subclass `BaseMetric` and slot into
  `assert_test()` or bare parametrized tests.
- **Small-team overhead** — one-person maintenance. No cloud platform,
  no separate dashboard server, no account.
- **Deterministic-metric support** — the project needs recall / precision
  metrics (`video_id` overlap, channel coverage, timestamp windows), not
  just LLM-judge scoring. DeepEval natively supports non-LLM `BaseMetric`
  subclasses with `measure` / `a_measure` / `is_successful`.
- **LLM-judge escape hatch for later** — DeepEval also ships `GEval` for
  when synthesis-quality dimensions need grading (e.g., the
  `position_diversity` / `essay_coverage` dimensions declared in the
  golden dataset but not yet implemented — see
  [ADR-0017](adr/ADR-0017-kb-layer-strategy.md) Related Debt).

Alternatives surveyed (RAGAS, PromptFoo, TruLens, Inspect AI, Vectara
Eval) were either tied to a specific framework, cloud-platform-centric,
or thinner on deterministic-custom-metric support. DeepEval hit all four
criteria.

## The 25-Query Golden Dataset

Location: [`tests/evals/golden_dataset.yaml`](../tests/evals/golden_dataset.yaml).
Structure: one top-level `queries:` list. Each entry has:

| Field | Meaning |
|-------|---------|
| `id` | `Q01` … `Q25`. Stable identifier used in test IDs. |
| `query` | The user-intent phrasing sent to `hybrid_search`. |
| `query_type` | `single_channel_baseline`, `cross_channel_comparative`, or `cross_channel_synthesis`. Sets expected difficulty. |
| `concept_ids` | Taxonomy concept IDs the query targets (aids Stage-1 query-expansion work). |
| `known_good_answer` | Prose summary of what a perfect answer would include. Used by future LLM-judge metrics. |
| `expected_hits` | List of grounded hits: `video_id`, `channel`, `timestamp_range`, `key_phrase`, `position`, `provenance` (`manual_verified` vs `concept_augmented_verified`). |
| `dimensions` | Per-metric thresholds — e.g., `recall_at_k: {k: 10, threshold: 0.6}`. |

Every `expected_hit` is grounded in the actual corpus: the `video_id` must
exist in the LanceDB index, and the `timestamp_range` must correspond to
text that contains the `key_phrase`. There are no synthetic entries.

Provenance matters:

- `manual_verified` — human confirmed the passage is the right answer.
- `concept_augmented_verified` — the hit came from the concept / taxonomy
  layer and was confirmed to match the expected content; useful for
  `seankochel` entries where transcripts don't exist (mindmap-sourced).

**Timestamp precision caveat:** `seankochel` entries use bullet-level
timestamps from mindmaps, not chunk-level timestamps from transcripts.
The `timestamp_precision` thresholds are looser for those queries but may
still be systematically too strict. Tracked in
[ADR-0017](adr/ADR-0017-kb-layer-strategy.md) Related Debt.

**The dataset is a frozen contract.** Per ADR-0017, adding or changing
queries mid-experiment invalidates stage-over-stage comparison. Edits
require ADR-grade justification.

## Running the Eval

### Prerequisites

- `pip install deepeval` — not in core `pyproject.toml`; treat as optional
  dev dependency for now (candidate for a future `[eval]` extras group).
- `VOYAGE_API_KEY` — the harness embeds queries before searching.
- `GEMINI_API_KEY` — not used by current deterministic metrics, but
  reserved for future G-Eval metrics.
- LanceDB index must exist at the resolved `vector_db_dir` (see
  [ADR-0016](adr/ADR-0016-vector-db-path-config.md)). Build it with
  `python scripts/video_intel.py index` if missing.

### Full run (~1 minute, a few cents of Voyage tokens)

```bash
pytest tests/evals/ -v -s
```

The `-s` flag is important — the harness prints a per-metric report for
each query. Suppressing stdout (pytest's default) hides the diagnostic
that tells you *why* a query failed, not just that it did.

### Smoke mode (Q01 only, ~3 seconds)

```bash
VIDEO_INTEL_EVAL_SMOKE=1 pytest tests/evals/ -v -s
```

Use while iterating on the harness itself, not on retrieval quality.

## Metrics

All four metrics are deterministic, no LLM judge required. Source:
[`tests/evals/metrics.py`](../tests/evals/metrics.py).

| Metric | What it measures | Gating |
|--------|------------------|--------|
| `RecallAtKMetric(k, threshold)` | Fraction of `expected_hits` `video_id`s appearing in top-k retrieved results. | Gating — test fails below threshold. |
| `MRRMetric(threshold)` | Mean reciprocal rank of the first `expected_hit`. Score = 1/rank of first hit. | **Non-gating.** Informational signal only — dropped from pytest failure logic because a query can pass RecallAtK while MRR looks weak, and vice versa. |
| `ChannelCoverageMetric(min_channels, threshold)` | Fraction of `expected_hits` channels that appear in retrieved results; also enforces absolute `min_channels` floor. | Gating. Cross-channel queries rely on this. |
| `TimestampPrecisionMetric(tolerance_sec, threshold)` | For each expected hit, does any retrieved chunk for that video land within `[start - tol, end + tol]`? | Gating. Looser tolerance for mindmap-sourced entries. |

A query passes iff all *gating* metrics meet their per-query thresholds.
MRR is logged for diagnosis but doesn't fail the test.

Metric thresholds are per-query (in `dimensions`), not global. This lets
the dataset calibrate by query type — baselines expect higher recall
than synthesis queries.

## Is the ruler intact? (`tests/evals/test_instrument.py`)

Run this before reading any N/25. It is a separate suite from the retrieval
eval, costs nothing (no Voyage call — one LanceDB column scan), and answers a
question the retrieval eval structurally cannot: **can this golden query's
gating thresholds be reached at all**, by any retriever, given the harness
configuration and the index actually on disk?

A failure here is a broken ruler mark, not a retrieval result. Issue #190 found
two of them hiding inside one number for over a year:

1. `hybrid_search` returned one chunk per video, so a query expecting several
   timestamp windows inside a single video was capped at
   `distinct videos / expected hits` on `timestamp_precision` — below its own
   threshold for **5 of the 25 queries (Q01, Q02, Q03, Q04, Q11)**, which could
   therefore never pass no matter how good retrieval was. Fixed by running the
   harness with `dedup_by_video=False` (see `docs/search-internals.md`).
2. A golden `video_id` left the corpus (a creator re-upload), making one
   query's `recall_at_k` threshold unreachable against any index. **Corrected
   2026-09-02** - the audit is now fully green. That correction is the clearest
   evidence the audit works: Q02's `recall_at_k` went 0.000 -> 1.000 the moment
   the dataset pointed at the id that exists, so it was never a retrieval
   failure at all. See the CHANGE LOG at the top of `golden_dataset.yaml`.

Because both scored exactly like retrieval failures, **the historic 1/25
baseline was never purely a retrieval measurement.** Keep the two suites apart;
folding an instrument defect back into the retrieval score is how this rotted
unnoticed.

## Current Baseline (2026-09-01, post-#190)

**1 of 25 queries pass all gating metrics** (Q11), measured on a
2,360-video / 80,297-chunk index with the instrument defect above removed.

The number happens to match the 2026-04-19 headline but is not the same
measurement, and the two are not comparable: the corpus grew roughly 15x, the
passing query changed from Q15 to Q11, and the metric ceilings moved. The
immediately preceding run on the same corpus with the capped instrument scored
**0/25**. Treat 2026-09-01 as the new baseline and do not compare across it.

Primary failure mode is now unambiguous: **19 of 25 queries score 0.000 on
recall** — the expected videos are not retrieved at all. Every one of the 22
distinct golden videos is now in the index, so this is genuine retrieval
failure, not corpus coverage. Full diagnosis of the original vocabulary-mismatch
theory in [ADR-0017](adr/ADR-0017-kb-layer-strategy.md).

### Historic baseline (2026-04-19, capped instrument)

**1 of 25.** Q15 ("Opus 4.7, Gemma 4, Sonnet 4.6") was the only passing query —
exact model-name overlap between query and content let BM25 dominate. Retained
for lineage; read it knowing 5 of the 25 queries could not pass.

Query-type breakdown:

| Query type | Count | Avg recall | Target | Status |
|------------|-------|------------|--------|--------|
| Single-channel baseline | 10 | ~11% | 75% | Far below |
| Cross-channel comparative | 10 | ~25% | 50–70% | Below |
| Cross-channel synthesis | 5 | ~5% | 30–50% | Far below |

Primary failure mode is **vocabulary mismatch**: queries use conceptual
vocabulary ("reliable agents", "context engineering") while content uses
creator vocabulary ("Ralph Wiggum", "Factorio parallel sessions"). This
is the justification for the staged KB-layer interventions in ADR-0017.

## Adding or Changing Queries

Per ADR-0017, the dataset is frozen. If you need a new query:

1. Grep the transcript corpus for the key phrase to confirm grounding —
   don't invent hits the retrieval layer can't find even with perfect
   recall.
2. Record `manual_verified` or `concept_augmented_verified` provenance
   honestly.
3. Calibrate thresholds against the closest existing query of the same
   `query_type`.
4. Open an ADR update (or a new ADR if the change is structural) citing
   the reason. Don't silently edit the YAML.

Changes that *don't* need an ADR: fixing a typo in a query string,
correcting a wrong `video_id` (grounding error), adjusting `key_phrase`
to match actual transcript text. These are corrections, not
recalibrations.

## Extending the Harness

The harness at
[`tests/evals/test_search_quality.py`](../tests/evals/test_search_quality.py)
is parametrized over the YAML. Adding a metric is a two-step change:

1. Implement it in `tests/evals/metrics.py` as a `BaseMetric` subclass
   with `measure`, `a_measure`, `is_successful`, and a `__name__`
   property.
2. Wire it into `_build_metrics()` in the harness, reading its
   thresholds from the per-query `dimensions` dict.

For LLM-judge (G-Eval) metrics, use DeepEval's `GEval` class and pass
the `GEMINI_API_KEY`. This is Stage 3 prerequisite work per ADR-0017.

## Two eval lessons worth keeping (in plain English)

Background so future-you does not have to reverse-engineer this: to test search quality, we keep an "answer key" - a list of questions paired with the video and timestamp that *should* come back (the golden dataset). We then score how often search returns the right hit. These two lessons came out of that work (they were issues #18 and #22, now closed) and stay true no matter what eval tool we use.

**Lesson 1: when a test fails, it might be the answer key that is wrong, not the search.**
A failing score (0) has two very different causes, and they look identical:

- the search genuinely missed the right video, OR
- the answer key rotted - the video was renamed or deleted, or its timestamp no longer points where the text actually is.
Example: the answer key says "for the question 'permission problems', the right hit is video X at 3:45." Someone re-titles video X (its `video_id`/slug changes) or re-transcribes it (3:45 now lands on different words). The search is fine, but the test screams "FAIL." So: before trusting a low score, check the answer key still points at real, existing content. A wrong-test-data failure and a bad-search failure need opposite fixes, so you have to tell them apart first.

**Lesson 2: not all "correct timestamps" are measured the same way, so one tolerance does not fit all.**
Our answer-key timestamps come from two places that measure time differently:

- **mindmap bullets** (`[MM:SS] - topic`) mark the single moment a topic is *first mentioned* (a point), and
- **transcript chunks** cover a rolling ~30-second window (a range).
If you allow the same "close enough" window for both, a mindmap-sourced answer looks like a miss just because a first-mention point and a chunk window line up differently - not because search was wrong. Fix: label each answer with where its timestamp came from (a `source:` tag) and allow a different tolerance for each kind. Otherwise the answer key invents failures that are not real.

**Corollary (why we are moving to lighter evals): if plain math can compute it, do not pay an AI to guess it.** Example: "are the results spread across different videos, or all from one?" is just counting distinct videos in the result list - no LLM judge needed. Reach for an AI judge only when the thing you are measuring genuinely needs judgment.

## See Also

- [ADR-0017 — Staged KB-Layer Strategy](adr/ADR-0017-kb-layer-strategy.md)
- [ADR-0013 — Hybrid Search RRF Fusion](adr/ADR-0013-hybrid-search-rrf-fusion.md)
- [`docs/search-internals.md`](search-internals.md) — retrieval pipeline
  the eval measures against.
- [DeepEval custom metrics docs](https://deepeval.com/docs/metrics-custom)
