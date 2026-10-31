---
title: "feat(scan): scan ends consistent for the free derived layers, index stays a printed recipe"
issue: 283
date: 2026-10-31
status: active
---

# Plan: scan rebuilds the free derived layers and prints the index recipe (issue #283)

## Problem

`scan` writes transcripts, mind maps and `concepts.json`, then stops. `taxonomy.json`, `topics.json`, the LanceDB index and the catalog (`_atlas/atlas.json`, `catalog.html`) are separate manual commands. Only `taxonomy-build` writes `taxonomy.json`; the scan's `auto_concepts` loop accumulates new concepts in memory (ADR-0010) and never saves them, so the next scan normalizes against a file that lacks them and mints duplicates.

## Decision (from the issue)

Additive curate commands end with the FREE derived layers consistent and the paid, slow layer printed as a recipe. Destructive commands (`dedupe`, `prune-shorts`) keep printing the recipe for everything.

## Design

### 1. What counts as "this scan changed an artifact"

A per-scan `changed_channels: set[str]` and `index_channels: set[str]`, filled in `cmd_scan` from results it already has:

- a transcript status, a mind map status or a concepts status that starts with neither `skipped` nor `error` marks the channel changed. One helper, `scan_status_wrote_artifact(status)`, decides for all three loops.
- a transcript status that passes the same test also marks the channel for the index recipe, because the LanceDB index embeds transcripts. Its rows also carry `concept_ids` from `concepts.json`, but no reader consumes those from search hits today, so a channel whose only change was a mind map or concepts gets no `index` line: re-embedding a whole channel for it would cost minutes for nothing.
- `record_alt_title_if_rotated` returning `True` marks the channel changed (aliases are a catalog field).

`--dry-run` writes nothing, so both sets stay empty and nothing is rebuilt. An `error` status can still have written a meta (`last_error`), but rebuilding on an error-only run buys nothing the operator needs, so it does not count.

### 2. The rebuild, after the channel loop

When `changed_channels` is non-empty, `cmd_scan` calls one helper, `rebuild_free_derived_layers(output_dir, config)`, exactly once: after the last channel's concepts loop has finished and before the failure summary. It returns whether the catalog build found a search index (`True` / `False`), or `None` when the catalog step failed or did not run; the recipe in section 3 reads that value, so the index is read once per scan.

1. `build_taxonomy(output_dir)` (the same function `taxonomy-build` calls; it writes `taxonomy.json`).
2. `write_topics(output_dir, build_topics(output_dir))` (what `topics-build` does).
3. `atlas_build.build_catalog(output_dir, atlas_dir, index_dir)`, a library function extracted from `atlas_build.main` (which becomes a thin CLI wrapper over it) that returns `(exit code, index-read state)`, then `atlas_render.main(["--atlas", <output_dir>/_atlas/atlas.json])`. One definition of each build; both stay standalone CLIs.

Each step is wrapped on its own: a failure (any `Exception`, plus `SystemExit` from the scripts' ownership refusals) logs one WARNING naming the step and the manual command that redoes it, and the next step still runs. A non-zero exit from the build or the render counts as a failure too (`atlas_render.main` catches its own refusals and returns 2), and the render is skipped when the build did not succeed. The scan never aborts on a derived-layer failure: the per-video work it paid for is already on disk.

No flag, as the issue decided. `scan --channel X` rebuilds the same whole-corpus files, because none of them has a per-channel mode. They are free but not instant: each walks the whole corpus, and `docs/atlas.md` measured the catalog build alone at about four minutes on a Drive-mounted corpus, so each step logs its elapsed time and the night summary records this as a decision for the owner.

### 3. The index recipe

After the failure summary, when `index_channels` is non-empty, the scan prints the exact commands, in the dedupe recipe's style:

```text
Search index not rebuilt by scan (it is paid and slow). To refresh it for the channels whose transcripts changed:
  python scripts/video_intel.py index --channel alpha
  python scripts/video_intel.py index --channel beta
```

The index read has three answers: `read` (ids), `absent` (no folder or no table: confirmed) and `unknown` (no `lancedb` package, an unreadable index, a short projection). Only `absent` switches the recipe to the full `python scripts/video_intel.py index`, because `index --channel` is refused there by the #183 guard. `unknown`, and a failed catalog step, keep the per-channel form: it is right whenever an index exists, and its refusal names the full build when one does not, so a read failure never prescribes a paid whole-corpus re-embed. Scan never constructs a Voyage client and never calls `build_search_index`.

The recipe ends with the two catalog commands (`atlas_build.py --corpus ... --index-dir ...`, then `atlas_render.py --atlas ...`), because the catalog was built before the operator runs the index lines and its "in search index" facet would otherwise stay stale until the next scan.

**Narrowing of the issue's wording, on purpose.** Issue item 2 says "each channel it touched". The recipe lists each channel whose TRANSCRIPT changed, because the index is built from transcripts only; a channel whose only change was a mind map or concepts would re-embed every chunk for nothing. The full-`index` fallback when no index exists is likewise a refinement the issue did not spell out.

### 4. The "in search index" catalog facet

- `atlas_build` gains `--index-dir PATH` (default `<corpus>/.lancedb`, the same default `resolve_vector_db_dir` uses) and a function `read_search_index(index_dir) -> IndexRead(state, video_ids)`. It imports `lancedb` inside the function (the module stays stdlib-only at import, the #152 contract), and reads only the `video_id` column (`table.search().select(["video_id"]).limit(0).to_arrow()`, the projection the eval conftest uses). An unreadable index logs one WARNING and returns `unknown`; a projection shorter than `count_rows()` is `unknown` too, never a partial set. No embedding call, no write. The folder and table names move to `corpus_walk` (`SEARCH_INDEX_DIRNAME`, `SEARCH_INDEX_TABLE`) so `video_intel` and `atlas_build` share one definition across the import firebreak.
- Each row gains a 19th key, `in_search_index`: `true` / `false` when the index was read, `null` when it was not. Appended at the end so every earlier key keeps its position.
- `schemas/atlas.schema.json` and `docs/atlas.md` (the row list) gain the field. The summary block is unchanged. `docs/atlas.md` states that the facet reflects the index at the last catalog build.
- `atlas_render`'s template adds a "In search index" yes/no facet (hash key `ix`), shown only when at least one row carries a boolean, like the transcript-source facet. An older atlas without the key shows no facet.
- `atlas_export.PUBLIC_ROW_KEYS` is an allowlist and does not gain the key, so the public subset never carries it. The committed demo page (`examples/catalog/catalog.html`) is re-rendered from its own `atlas.json` because the template changed.

### 5. The rule line

`CLAUDE.md` gets one entry under Code Review Guardrails ("scan ends consistent for the free derived layers"), and the `scan` bullet in Architecture is updated, so a future change can neither add the index to scan nor drop the taxonomy rebuild without contradicting a written rule. `skills/video-intel/SKILL.md` and README's scan description are updated in the same diff (skill parity).

## Test contract

`tests/test_scan_derived_layers.py`:

1. **Guard, real caller.** Drive the real `cmd_scan` over a tmp corpus with Gemini stubbed (a fake `process_mindmap` and `process_concepts` that write real artifacts) and `voyageai` replaced by a module whose `Client` records construction. Assert `taxonomy.json` gained the new concept, `_atlas/atlas.json` and `_atlas/catalog.html` exist and changed, `topics.json` was written, and the `Client` was never constructed. Falsify by deleting the rebuild call and confirming this test fails.
2. A scan that changed nothing (every video already processed) leaves `taxonomy.json` byte-identical and writes no atlas.
3. `--dry-run` rebuilds nothing.
4. The recipe lists exactly the channels whose transcript changed, as `index --channel <name>` lines; a mind-map-only change prints no recipe; no index present prints the full `index` line.
5. A derived step that raises logs the WARNING and the later steps still run; the scan still reaches `Done.`.

`tests/test_atlas_build.py` additions: `index_video_ids` returns `None` for a missing folder and for a folder with no table, and the real membership set for a small real LanceDB table (skipped when `lancedb` is absent); rows carry `in_search_index` true/false against that table and `null` without one; the module still imports nothing heavy (existing isolation test).

Template: the existing `TestPageScript` (`node --check`) and the browser smoke cover the new facet; one text test pins that the facet is gated on a boolean being present.

## Out of scope

Per-video incremental embedding (#284). Any change to what `dedupe` and `prune-shorts` print or do after `--apply`.

## Doc review (2026-10-31)

Coherence and feasibility reviewers ran on this plan. Applied: the helper returns the index state; the issue's "each channel it touched" narrowed on purpose; the recipe ends with the catalog commands so the facet can be refreshed; the rebuild runs once after all channels; a three-way index read so an unknown state never prescribes a full re-embed; `build_catalog` instead of calling `main` with argv; non-zero exits count as failures; the measured cost replaces "cheap"; one home for the index names. Deferred: `build_taxonomy` still raises on one unreadable `concepts.json` (a pre-existing behaviour of `taxonomy-build`; scan now logs it with the command instead of crashing) - follow-up candidate.
