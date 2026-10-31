---
title: "Changing who writes, who wins or what gets deleted is a data-flow change"
date: 2026-10-31
category: integration-issues
module: video_intel.py
problem_type: logic_error
component: tooling
symptoms:
  - "A captions-first route crashed with FileNotFoundError on any channel whose folder did not exist yet, while the fixture suite stayed green"
  - "A plain re-run could replace a multimodal transcript (SCREEN blocks, speakers) with a speech-only caption transcript"
  - "Printed recovery instructions that could not work: `Raise --limit` under an exact topic scope, `--start 40 --end 40` that selects nothing"
  - "A dedupe change scoped as ordering only surfaced P1 defects in the unchanged move and delete code"
  - "`index --channel X` after an embedding-model change erased that channel from the index, and every re-run failed the same way"
root_cause: data_integrity
resolution_type: code_fix
severity: high
related_components:
  - documentation
  - testing_framework
tags: [first-writer, identity, idempotency, force-flag, dedupe, lancedb, scoped-index, recovery-message, data-flow]
---

# Changing who writes, who wins or what gets deleted is a data-flow change

## Problem

Several changes looked local: send some videos to captions before Gemini, rank dedupe duplicates by quality, re-index one channel instead of all. Each one moved a responsibility (who writes first, which copy survives, when rows are deleted) and broke code the diff did not touch.

## Symptoms

- The captions writer became the first writer for some channels (#120) and raised `FileNotFoundError` on a brand-new channel folder, because nobody had created it yet.
- An unguarded captions call on a re-run would replace a good multimodal transcript with a speech-only one.
- Recovery messages that read well but could not recover anything when the prescribed value was fed back in.
- `index_schema_mismatch` (column names only) passed a vector dimension change, so the scoped `delete` committed, `add` failed, and the channel was gone (#183).

## What Didn't Work

- Fixture tests that pre-create the channel folder: writer order cannot fail when the fixture already did the first writer's job.
- Tests that only asserted a recovery message was present.
- Scoping the #159 dedupe change as "ordering only": a new winner shape flows straight into `_move_missing_mode_artifacts` and the loser sweep, which had only ever seen the old winners.
- A column-name schema guard as the only check before a delete-then-add replace.

## Solution

- **First-writer duties move with the route.** `_write_transcript_md` now creates its own parent folder at the shared seam, and every transcript meta writer spreads `_transcript_identity_fields` so the first writer always stamps `video_id` (identity #66).
- **A lower-fidelity writer refuses to clobber.** `_try_captions_transcript` returns without writing when a transcript exists and `force` is false; the chunk-loss failover passes `force=True` only for this run's own lost-chunk partial (#248 item 4).
- **Every printed remedy is executed in a test.** `Raise --limit` was removed from the scoped-search message (#203); `recovery_range_minutes` (in `translate_video.py`) computes a range the round-trip test feeds back through the real filter; the confabulated-mindmap cleanup lands a transcript first, because a bare `--force` re-resolves to the video source and loops; the #248 log prints two paste-ready commands.
- **Old artifacts stay as they were, so ship the lookup.** A fix that changes how artifacts are produced does not rewrite existing ones; it records a meta field (for example `transcript_source: youtube_captions`) and a documented lookup in `docs/troubleshooting.md` that finds the ones written the old way.
- **Dedupe verifies disk before trusting a meta.** `_pick_canonical` ranks not-severe first; `_verified_modes_completed` checks claims against the writer's own patterns; only a mode whose primary artifact actually moved credits `modes_completed` or copies provenance; blocked sources are kept out of the sweep.
- **Data checks before the first irreversible write.** `vector_dimension_mismatch` runs after embedding and before the scoped delete, and the delete, add and optimize block names the channel on failure (#183 invariants 5 and 5b).

## Why This Works

Each defect had one shape: a duty the old flow did implicitly (create the folder, stamp identity, never overwrite, check the new data) stayed with the old owner when the flow changed. LanceDB makes the last case worse: `add` silently null-fills a missing column and fails on the vector cast only after `delete` has committed. A duty placed at the shared seam, or just before the irreversible step, holds whatever route reaches it.

## Prevention

- When a change alters who writes first, re-list that writer's inherited duties and smoke it against an empty corpus, not a fixture tree.
- A writer of lower fidelity than what may be on disk checks for existence and needs `--force` to replace it.
- For every recovery a message prints, a test feeds the prescribed value back through the real path and asserts it recovers.
- When the winner of a duplicate selection changes, drive each new winner shape through the move and delete path with disk state that contradicts the meta.
- Severity-first ranking belongs where a repair step follows (dedupe). At a pure lookup site such as `_load_video_id_index` (first-wins, advisory today), an empty or stub meta must never outrank the duplicate that holds the work.
- In any delete-then-add replace, every check that depends on the new data runs after the paid step and before the delete.

## Related Issues

- #66, #120, #159, #183, #203, #248
- `.claude/rules/transcript.md`, `.claude/rules/scan-config.md`, `.claude/rules/search-index.md`, `.claude/rules/translate-bcs.md`
