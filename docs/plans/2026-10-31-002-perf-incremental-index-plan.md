---
title: "perf(index): per-video incremental embedding instead of whole-channel re-embed"
issue: 284
date: 2026-10-31
status: active
---

# Plan: per-video incremental indexing (issue #284)

## Problem

`index --channel X` deletes a channel's rows and re-embeds every chunk of every transcript in it. Adding one talk to a large channel re-embeds tens of thousands of chunks, and the time is what makes people skip the re-index that `scan` now prints (#283).

## Decision

A new flag, `index --incremental`, embeds only transcripts the index does not already hold as they are on disk, replaces those videos' rows, and compacts once. `index` (no flags) stays the full rebuild and `index --channel X` stays the forced channel rebuild. `--incremental` may be combined with `--channel X` to limit the walk to one channel. The recipe `scan` prints switches to `index --incremental --channel <name>`.

## Design

### 1. Video identity and the content hash

- A transcript's identity is `(channel, video_id)`, falling back to `(channel, transcript file name)` when the meta has none (the live index carries some blank-id chunks). The channel is part of the key because the same `video_id` lives in two channel folders in the live corpus, with different transcript bytes; the file name, not the absolute `source_file`, because that path moves whenever the corpus is mounted elsewhere.
- Each record gains `transcript_sha256`: the SHA-256 of the transcript file's bytes, one value per video, repeated on every chunk row.
- A video is selected when its identity has no rows in the table (new), or when the table carries `transcript_sha256` and the stored set of hashes for that identity differs from the current set (changed). Two transcripts with one `video_id` in ONE channel folder (a title-rotation duplicate) are one identity with a set of two hashes.
- Only the transcript bytes are hashed. Title, `concept_ids` and `source_file` are not refreshed when only they change; title is BM25-searched and `source_file` is printed by search, so a title rotation or a `dedupe` move leaves stale ranking and paths until a full rebuild. Known limit, stated.

### 2. Older indexes without the hash column

The live index was built without `transcript_sha256`. LanceDB raises on an `add` with an extra column, and `index_schema_mismatch` refuses one before any Voyage call. So:

- `transcript_sha256` is the one optional index column (`OPTIONAL_INDEX_COLUMNS`). On every path that ADDS to an existing table (`--channel`, `--incremental`), records drop an optional column the table does not have before the schema check, so an older index keeps working for `--channel` exactly as before.
- On such a table `--incremental` still runs but detects NEW videos only (no hash to compare), and logs once: change detection needs a one-time full rebuild, `video_intel.py index --force`. That is the "say so in the log" the issue asks for, without forcing a paid re-embed on merge.
- A full rebuild always writes the column, so after one, change detection is on.

### 3. Order of operations (the existing guarantees, kept)

0. Refuse `--incremental` with `--force` at parse time (a mutually exclusive argparse group), before anything else.
1. `probe_atomic_writes` before anything that touches the index.
2. Refuse when no table exists, above the Voyage call, naming the full `index` (same shape as the `--channel` refusal, #183 invariant 3).
4. Read the table's `video_id`, `source_file` and (when present) `transcript_sha256` columns once; a projection shorter than `count_rows()` is a hard error, never a partial map (a partial map would re-embed indexed videos, i.e. silent spend).
5. Walk transcripts (optionally one channel), hash each, select new or changed identities, chunk only those.
6. Schema-name check, embed, vector-width check, then delete predicates per channel (`channel = 'c' AND (video_id IN (...) OR source_file IN (...))`, batched 200 terms each, every literal through `escape_sql_string_literal`; blank-id rows are matched on the `source_file` values READ FROM THE TABLE, never on today's paths), one `add`, one `optimize`. A failure between delete and add names how many videos may be missing, and the recovery is re-running the same command: a deleted identity reads as new.
7. Nothing selected: log the counts and return 0 without a Voyage client.

Rows for transcripts deleted from disk are not retired by `--incremental` (same as `--channel`); a full rebuild retires them.

### 4. CLI, recipe, docs

- `index --incremental [--channel X]`; the counts ("N new, M changed, K unchanged") are logged, and `cmd_index` prints "Index already current" when nothing was selected instead of the full-rebuild path's "No transcripts found".
- `log_scan_index_recipe` prints `index --incremental --channel <name>`; the confirmed-absent case still prints the full `index`.
- SKILL.md routing row AND frontmatter trigger phrases, README, CLAUDE.md `index` bullet and the #283 guardrail entry, `.claude/rules/search-index.md` invariants.

## Test contract

Real LanceDB in a tmp folder, Voyage stubbed with a deterministic fake client that records every text it embeds [core: real-caller]:

1. A second `--incremental` run on an unchanged corpus embeds nothing and builds no Voyage client calls.
2. Adding one transcript to a channel embeds exactly that video's chunks; every other row (other videos, other channels) is byte-identical.
3. Editing one transcript re-embeds only that video, and its old rows are gone.
4. Older table without `transcript_sha256`: `--incremental` embeds only the new video, logs the one-time rebuild hint, and `--channel X` still works (records conform to the table).
5. No table: refused before any embed. `--incremental --force`: refused at parse.
6. Blank `video_id` videos are keyed by `source_file` and are not re-embedded on the second run.
7. Probe-before-pay: the schema refusal happens before the fake client embeds anything.

Falsify by removing the hash comparison (test 3 fails) and the selection (test 1 fails).

## Retrieval eval

Retrieval logic is unchanged, but index construction changed, so `pytest tests/evals/test_search_quality.py` is run by name against a complete full index (the most recent `index --force` once it finishes, confirmed by row count against the transcript chunk count) and the N/25 goes in the PR body, with `tests/evals/test_instrument.py` run first.

## Out of scope

Retiring rows for deleted transcripts; re-embedding on title or concept changes; a meta whose blank `video_id` is later backfilled (zero live cases; its old blank-id rows would stay until a full rebuild); any change to the full rebuild beyond writing the `transcript_sha256` column, or to `--channel`'s meaning.

## Doc review

One combined coherence and feasibility pass. Applied: per-channel identity and channel-scoped deletes (its P1, measured on two live shared ids with different bytes); file-name identity for blank ids and deletes built from stored paths (remount test); "Index already current" message; recovery is a re-run, not a paid full rebuild (failure test); out-of-scope wording; parse-time refusal first; skill frontmatter. Deferred: blank-to-real id drift (zero live cases). Not applied: running the eval on an index this branch built. The owner's instruction for this run was to measure the existing full index and rebuild only if incomplete; the eval exercises retrieval, which this branch does not change, and the embeddings a rebuild would write are identical. Stated as a premise in the PR.
