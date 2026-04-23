# Video ID Deduplication — Requirements

**Date:** 2026-04-22

**Status:** drafted autonomously on the user's instruction ("trust your
judgment, run the loop with less of my attention"). Decisions 1-5 below
reflect defensible defaults; user can override before or after
implementation.

## Problem

YouTube creators increasingly A/B test video titles for SEO. When a
creator rotates the title of an already-published video, our scan
pipeline treats it as a new video because `is_processed()`
(`scripts/video_intel.py:826`) checks for existing artifacts by
`{date}-{slug(title)}` prefix, and the slug changes when the title
changes. Result: the same `video_id` is fully re-processed under a
second prefix, producing duplicate mindmaps, transcripts,
`concepts.json`, and vector-index chunks.

Example pair (both `video_id=aIy85-gIDzI`):

- `2026-04-15-7-things-for-agents-in-production.meta.json`
- `2026-04-15-must-haves-for-agents-in-production.meta.json`

## Evidence (sweep of production corpus, 2026-04-22)

- 1,022 unique `video_id` values across 9 channels
- 1,028 `meta.json` files
- **6 duplicate groups** (video_ids with >1 meta) across 4 channels:
  graceleungyl, natebjones (3 groups), samwitteveenai, seankochel
- 6 excess files to clean up
- Rotation gap between duplicates ranges from 38 minutes to 16 days —
  creators rotate both during a single day and across weeks, so
  prevention needs to cover both windows
- Pollution rate today: ~0.6%, but trending up with the A/B-titling
  trend (two of six duplicate groups appeared today)

## Goal

1. **Prevent** future duplicates: same `video_id` must never be scanned
   or transcribed twice regardless of title change.
2. **Clean up** existing duplicates without losing SEO-valuable
   alternative-title signal.
3. **Preserve search integrity**: after cleanup, taxonomy counts and
   LanceDB chunks must reflect one entry per real video.

## Scope decisions

### 1. Canonical title rule

**Keep the meta with the latest `processed` timestamp.** The most
recent scan reflects the title the creator had set most recently, which
is the closest proxy we have to "what the creator currently wants this
video called." The sweep confirms `processed` is always populated and
timestamps differ across pairs.

Tie-break if timestamps are identical to the second (unlikely): keep
the one whose `modes_completed` set is larger; further tie-break on
alphabetical prefix for determinism.

### 2. `alt_titles` field

Losing titles are merged into the canonical meta as
`alt_titles: [str, ...]` — ordered by `processed` timestamp ascending,
deduped, canonical title excluded. The field is omitted when the list
would be empty.

This preserves the creator's SEO experimentation signal as metadata and
lets later search layers (FTS over title+alt_titles) match queries that
use discarded wording.

### 3. Loser artifacts

Losing prefix's files are **deleted** after merging: `.meta.json`,
`.mindmap*.md`, `.transcript.md`, `.transcript.raw.txt`,
`.concepts.json`, any other `{loser_prefix}.*` siblings. Same
`video_id` = same video content = same frames, same OCR, same audio —
keeping both transcripts is double-counting, not preservation.

**Exception:** if the canonical meta is missing a mode the loser has
completed (e.g., canonical has scan only, loser has scan+transcript),
copy the loser's artifact for that mode to canonical's prefix before
deletion, and union `modes_completed`. Protects against accidental
content loss when the "latest" meta happens to be the less complete
one.

### 4. Automation posture

- **Prevention (automatic):** `is_processed()` gains a
  `video_id`-based check via a per-channel cache built from meta.json
  files. Zero new CLI surface; transparent to users.
- **Cleanup (opt-in, dry-run by default):** new `dedupe` subcommand.
  Runs report-only by default; `--apply` actually mutates disk.
  Destructive, so explicit invocation is required per
  `specs/agent-rules.md` §7 ("stop and ask when the change touches
  shared state").

### 5. Taxonomy and vector-index impact

Both are **derived artifacts** per CLAUDE.md. After `dedupe --apply`:

- Rebuild `taxonomy.json` by re-running `taxonomy-build`.
- Rebuild the LanceDB vector index via `index` (current index has
  duplicate chunks for the removed videos; incremental delete is harder
  than a full rebuild and rebuild is idempotent).

Both rebuild steps are triggered by the user or a follow-up command,
not auto-invoked inside `dedupe`, to keep the subcommand's blast
radius predictable.

## Success criteria

1. Sweep after implementation reports 0 duplicate groups across all
   channels.
2. `is_processed()` returns True for a video whose `video_id` is
   already represented in the channel directory under any title slug.
3. Unit tests cover: single video_id with multiple titles, completeness
   tie-break, empty channel dir, 3+ duplicates in one group, artifact
   file enumeration.
4. `ruff format` + `ruff check` + `pytest -m "not integration"` pass.
5. After `dedupe --apply` + `taxonomy-build` + `index` rebuild, the
   hybrid search eval at `tests/evals/` returns a score no worse than
   the 2026-04-20 baseline (1/25). Eval may improve marginally because
   duplicate chunks no longer dilute embeddings, but no regression is
   the actual bar.

## Non-goals

- Fetching live titles from YouTube Data API to pick canonical. Extra
  quota cost, extra network dependency, and "latest processed" is good
  enough for the pollution rate we observe.
- Indexing `alt_titles` into BM25 FTS in this change. Plumbing exists
  for a later PR; keeping scope tight.
- Renaming files to `{date}-{video_id}` canonical naming. Human-
  browsable slugs are a deliberate ergonomics choice; the prevention
  check gives us correctness without the filename migration.
- Auto-invoking taxonomy and index rebuild from inside `dedupe`. Keep
  cleanup commands narrowly scoped.

## References

- `scripts/video_intel.py:826` — `is_processed()` current implementation
- `scripts/video_intel.py:592` — `video_file_prefix()` slug derivation
- Memory: `project_concept_indexing.md` — cross-video concept layer
  that directly ingests duplicate concepts today
- Memory: `project_eval_stage1_2026-04-20.md` — current 1/25 baseline
  used as the no-regression floor
- [ADR-0017](../adr/ADR-0017-kb-layer-strategy.md) — KB layer strategy;
  cleanup unblocks a less-noisy corpus for Stage 2 LightRAG work
