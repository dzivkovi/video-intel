---
title: Repair wall-of-text transcripts from caption cue timings - Plan
type: feat
date: 2026-11-23
issue: 289
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Repair wall-of-text transcripts from caption cue timings - Plan

## Goal Capsule

- **Objective:** a Gemini transcript that collapsed into one long dialogue entry gets back many timed entries, so search deep links inside it jump to the right moment again, without a Gemini call and without inventing a single timestamp.
- **Means:** a standalone aligner module maps the entry's words onto the YouTube caption track (fetched through the existing `fetch_english_captions`), and a manual `repair-timestamps` command splits the entry at its own sentence boundaries, stamping each piece with the start of the caption cue holding its first aligned word; the existing assessor then re-judges the file it just wrote.
- **Authority:** the issue #289 text and the contract decisions 1-12 win over this plan; the KTDs win on mechanism.
- **Stop conditions:** stop and report if a decision cannot be met as written, if the one live caption fetch is refused or fails, or if a doc-review P0/P1 contradicts a decision.
- **Execution profile:** TDD on synthetic caption tracks; one live caption fetch in Gate 1, no retry; no Gemini, no YouTube Data API.

---

## Product Contract

### Summary

`repair-timestamps --url URL --channel NAME [--apply]`. Dry run prints the plan per candidate entry and what the re-assessment would say. `--apply` keeps the original as a forensic sidecar, writes the repaired Markdown atomically, re-assesses from the file on disk, merges the verdict into meta without touching unrelated evidence, promotes `partial` only when nothing else says the transcript is broken, records provenance, aligns the header status line with meta, and prints the follow-up re-index commands. Separately, the single-shot writer renders its Markdown after the quality verdict so its header status matches meta.

### Requirements (from the contract, numbered for tests)

- R1. `scripts/caption_align.py`: standard library only, no `video_intel` import; `normalize_words`, `align_words_to_cues`, `entry_offsets` (reports, never edits), `plan_entry_split`.
- R2. Candidate: aligned words span more than `REPAIR_MIN_SPAN_SECONDS` (60) of caption time. Split only after `.`, `?`, `!` followed by whitespace, pieces near `REPAIR_TARGET_PIECE_SECONDS` (25). A new piece is stamped with the start of the cue holding its first aligned word, which must be among its first `REPAIR_ANCHOR_WORDS` (3) words; within a split entry each new stamp is strictly later than the previous one (equal-second boundaries are skipped); the first piece always keeps the entry's original stamp.
- R3. Every output stamp is a stamp already in the file or the exact start of a cue (truncated to the whole second the format can express). Never interpolated.
- R4. Text preserved: pieces joined with single spaces equal the original text after whitespace normalization; the speaker label repeats on every piece; SCREEN blocks byte-identical.
- R5. Coverage gate per entry (`REPAIR_MIN_ENTRY_COVERAGE`, 0.6); no repairable candidate means the run declines and changes nothing.
- R6. One body parser/renderer; parse then render with no split is byte-identical on single-shot, chunked (coverage table) and captions shapes; after a split the body is stable-sorted by stamp.
- R7. Video resolved by id through `_load_video_id_index`; no Data API call; no `channels:` required.
- R8. `--apply` order: sidecar copy (verified, never clobbered), atomic write, re-assess from disk, meta merge through the shared re-assessment flag rule, conservative promotion, provenance fields (`transcript_timestamp_repair`, `transcript_timestamp_repair_coverage`) in `TRANSCRIPT_ARTIFACT_FIELDS`, header status rewrite, paste-ready follow-ups.
- R9. Single-shot writer: Markdown rendered after the verdict, with the warning block on a severe verdict and a header status matching meta.
- R10. Exit codes: 0 repaired / dry run with a plan / nothing to repair; 3 declined (captions absent, refused, alignment below threshold); 1 cannot run. A declined or failed run leaves the tree byte-identical.
- R11. In `CONFIG_BACKUP_COMMANDS`; skill entry and trigger phrases, README, schema, troubleshooting row and guardrail entry in the same diff.

### Scope Boundaries

No batch mode, no automatic invocation, no caption-text fallback, no prompt edit, no assessor threshold change, no change to scan or to any writer's automatic behavior beyond R9.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **One global alignment per run.** All dialogue entries' words, in file order, are aligned once against the caption word stream (`difflib.SequenceMatcher`, caption words as `a`, transcript words as `b`, default autojunk so very common words cannot seed spurious matches on long tracks); each matched transcript word gets the index of the cue that holds it. `caption_align.align_entries` slices that result per entry; `entry_offsets` and the command both use it, so a mid-length entry is never matched against an unrelated part of the track. `plan_entry_split` accepts the precomputed per-entry slice and aligns alone only when none is given.
- KTD2. **Boundary choice.** Valid boundaries are sentence starts whose anchor exists and whose truncated cue second is strictly later than the current piece's stamp. From the current piece, take the first valid boundary whose run reaches the target, or the valid boundary just before it if that one is closer to the target. A piece the track gives no anchor for keeps its text attached to the previous piece.
- KTD3. **Body model.** `parse_transcript_body(text)` splits on `splitlines(keepends=True)` into: header (everything before the first entry line after the body start that `dialogue_entries_from_transcript_markdown` uses), entries (dialogue or SCREEN, each owning its continuation and trailing blank lines verbatim), trailer (from the last `---` line after which no entry starts, i.e. the speaker-evidence footer). Render is concatenation, so the no-split round trip is byte-identical by construction; a drift test asserts the parser's dialogue stamps equal `dialogue_entries_from_transcript_markdown`'s. The body-start rule is extracted into one helper both use. Files are read and written as bytes so `\r\n` survives.
- KTD4. **Piece rendering.** `[stamp] label: "piece"` plus the entry's own line ending and trailing blank lines. The first piece keeps the original stamp string; a new stamp uses the entry stamp's part count (`MM:SS`, minutes allowed past 59, or `HH:MM:SS`).
- KTD5. **Meta merge.** Six fields from `assess_transcript_markdown` on the file just written; flags through `merge_reassessed_quality_flags(stored, recomputed)`, which keeps the stored members of `WRITER_ONLY_QUALITY_FLAGS` (`backward_jump_severe`, `backward_jump_mild`, `chunk_window_mismatch_severe`, `chunk_window_mismatch_mild`: emission-order and per-chunk evidence the sorted Markdown cannot recompute) and replaces everything else with the new verdict; severity from the merged list; written with `_write_meta_atomic`, never `update_meta`. The constant and helper are the ones the issue #294 branch lands for `assess-transcripts --force`; this branch carries a byte-identical copy until that merges (reported so the rebase drops one). `cmd_assess_transcripts` is not changed here.
- KTD10. **Rollback after the write.** The meta is written last. Any exception after the repaired Markdown is written (re-assessment, the header rewrite, the meta write) restores the original bytes from the verified sidecar (tmp then `os.replace`), deletes the sidecar this run created, leaves the meta untouched and exits 1. A hard kill between the two writes is an accepted residual (the sidecar survives as evidence).
- KTD11. **Outcomes.** A candidate is repairable when it passes the coverage gate AND its plan yields at least two pieces. No candidate at all: exit 0 "nothing to repair" (unless the whole run aligned below the coverage threshold, which declines, exit 3, because a non-matching track cannot show a wall of text). Candidates but none repairable: exit 3 with each candidate's reason (coverage, or no usable sentence boundary). Run coverage, persisted in `transcript_timestamp_repair_coverage`, is matched words over total words across the entries actually split.
- KTD6. **Promotion evidence.** Promote `partial` to `ok` (when `transcript_chunks` > 1) or `complete` only when the merged flags hold no severe flag and `transcript_failed_chunks`, `transcript_recovery`, `transcript_parse_error` and `transcript_thin_chunks` are all falsy. `transcript_thin_chunks` is added to the contract's list on purpose: a thin chunk is per-chunk evidence the Markdown cannot recompute, and the chunked writer demotes on it.
- KTD7. **Unknown duration is a precondition.** Without a usable `duration_seconds` the assessor cannot see a monolithic collapse, so a re-assessment would "clear" the flag by blindness. The command exits 1 before the caption fetch, naming the missing duration.
- KTD8. **Header status text has one definition.** `_transcript_header_status(meta_status)` maps healthy statuses to `Complete` and anything else to `Partial transcript`; the single-shot writer and the repair both use it. The rewrite applies only when the header carries a `**Status:**` line (the chunked coverage block has none and stays byte-identical), and it keeps the line's own ending. On promotion the repair also removes a quality-guard warning block, found structurally: from the shared `## Warning: Incomplete transcript` heading through its closing `---` line and the blank line after it, line-ending agnostic, and only when its text names the quality guard (a salvage warning is never removed, and a salvage meta is never promoted anyway).
- KTD9. **Follow-ups from one function.** `repair_follow_up_commands(channel)` returns `index --incremental --channel NAME` and `intel_graph.py load --force`; a test parses each with the real parsers. One extra printed note says an index built before content hashes cannot see this edit and `index --channel NAME` re-embeds the channel in that case.
- KTD12. **Cue adapter.** Cues come straight from `CaptionsResult.snippets` and `.durations`, never through `dedup_caption_cues` (which rounds and collapses); a stamp is the cue start truncated, not rounded, so a deep link lands at or before the speech. A dialogue entry's label and quoted text are parsed over the whole entry (first line plus continuation lines) as `<label>: "<text>"`; an entry of another shape is left whole.

---

## Implementation Units

### U1. Aligner module

- **Files:** `scripts/caption_align.py` (new), `tests/test_caption_align.py` (new).
- **Approach:** `Cue` dataclass; the four public functions plus `align_entries`; constants named in R2/R5.
- **Tests:** normalization (case, punctuation, apostrophes); alignment with shifted, missing and extra words; offsets including `None`; split plan (target spacing, anchor within three words, no-anchor boundary skipped, backwards boundary skipped, first piece keeps the original stamp, text preserved); thresholds at their boundaries (span 60 vs 61, coverage 0.6 vs below); import isolation with `video_intel` blocked.

### U2. Body parser/renderer and the shared body-start helper

- **Files:** `scripts/video_intel.py`, `tests/test_repair_timestamps.py` (new).
- **Tests:** byte-identical round trip on single-shot, chunked-with-coverage-table, captions and `\r\n` fixtures, the single-shot one built through the real `merge_transcript_json` and `_write_transcript_md`; dialogue stamps agree with `dialogue_entries_from_transcript_markdown`.

### U3. `repair-timestamps` command

- **Files:** `scripts/video_intel.py`, `tests/test_repair_timestamps.py`.
- **Approach:** resolve, preconditions (exit 1), caption fetch with a fresh `reason_sink`, one adapter from `CaptionsResult` to cues, plan, print, then dry-run exit or the R8 apply sequence; parser entry, dispatch, `CONFIG_BACKUP_COMMANDS`.
- **Tests (real `cmd_repair_timestamps`, `fetch_english_captions` stubbed):** dry run byte-identical; apply happy path (many entries, `monolithic_severe` cleared by the assessor, status promoted, header matches meta, sidecar holds the original bytes, provenance set); low coverage exits 3 untouched; captions absent vs refused; failed chunks keep `partial` and the field; writer-only flags preserved; SCREEN blocks byte-identical and interleaved; the two invariants on several shapes; follow-ups parse; every exit code; idempotence; existing sidecar not clobbered.

### U4. Single-shot header status (R9)

- **Files:** `scripts/video_intel.py`, `tests/test_repair_timestamps.py`.
- **Tests:** the real `process_transcript` with Gemini stubbed to a monolithic payload: header says `Partial transcript`, warning block present, meta `partial`; a healthy payload keeps `Complete`. Chunked writer: it renders no `**Status:**` line (only the coverage block), so there is no mismatch to fix; stated in the report.

### U5. Docs and guardrail

- **Files:** `skills/video-intel/SKILL.md`, `README.md`, `docs/meta-json-schema.md`, `docs/troubleshooting.md`, `.claude/rules/transcript.md` (entry plus `paths:`). Root `CLAUDE.md` untouched.

### U6. Gate 1

- Scratch copies under the temp `vi289` folder, `duration_seconds: 848` written by hand into each copy's meta (the duration backfill command is not in this tree), harness with a record-once/replay-forever wrapper around `fetch_english_captions`; dry run (the one fetch), apply on a fresh copy, second apply, `assess-transcripts` dry run.

---

## Verification Contract

| Gate | Command |
|---|---|
| Format changed files | `ruff format <changed files>` |
| Format check | `ruff format --check .` |
| Lint | `ruff check scripts tests` |
| Suite | `python -m pytest tests/ -q --ignore=tests/evals -p no:cacheprovider` |

Falsify separately: re-assessment removed with flags cleared by hand; an interpolated stamp; the sidecar dropped; promotion despite `transcript_failed_chunks`.

## Document review (non-interactive, coherence + feasibility)

Applied: rollback after the write (KTD10), the outcome table and run-coverage definition (KTD11), the named writer-only flag set (KTD5), the header rewrite only where a status line exists and structural warning-block removal (KTD8), the older-index note (KTD9), the cue adapter and whole-entry label parse (KTD12), strictly-later stamps (R2).

Deferred, one line each:
- Whether `assess-transcripts --force` shares the flag-merge rule: owned by the issue #294 branch, not this diff.
- Adding a warning block when a repaired transcript stays `partial`: out of scope; only the status line is rewritten (decision 8g).
- A hard kill between the Markdown write and the meta write: accepted residual; the sidecar stays as evidence.
- A bare `---` line inside the final SCREEN block with no evidence footer: accepted; it only moves text if a later piece sorts after that block.
- SequenceMatcher runtime on multi-hour transcripts: unmeasured; the pilot is 14 minutes.
- Fillers that captions drop costing sentence-start anchors: measured on the pilot through the reported coverage.

## Definition of Done

- R1-R11 each proven by a named test; all gates pass; Gate 1 run with exactly one real caption fetch.
