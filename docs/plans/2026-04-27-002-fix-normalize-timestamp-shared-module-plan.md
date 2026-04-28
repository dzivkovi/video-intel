---
title: Port normalize_timestamp to chunked transcript path
type: fix
status: active
date: 2026-04-27
issue: 58
---

# Port normalize_timestamp to chunked transcript path

## Overview

Extract four pure timestamp helpers from `scripts/translate_video.py` into a new shared module `scripts/timestamp_utils.py`, then call `normalize_timestamp` at the start of `scripts/video_intel.py:_classify_and_offset_timestamp` so Gemini's minutes-in-the-HH-field malformation (`[100:08:57]` meaning 1h48m57s) gets fixed before the absolute/relative classifier sees it. PR #51 ported the classifier but skipped the normalizer pre-pass; this is the one-line miss the Tucker/Sachs live run on 2026-04-27 surfaced via 89 `Implausible timestamp` warnings in chunk 3.

## Problem Frame

PR #51's chunked-transcript path classifies each timestamp as absolute / relative / implausible, but it does so on the raw model output. `translate_video.py:apply_timestamp_offset` (the SRT-translation analog the PR says it ported from) calls `normalize_timestamp` on line 1 to fix Gemini's known minutes-in-HH malformation FIRST. Without that pre-pass, the chunked transcript writes `[100:08:57]` to disk as-is and downstream consumers (translate-bcs, search index, future translate-from-transcript) inherit the corruption.

## Requirements Trace

- **R1** Tucker chunk-3 input `[100:08:57]` (real-input baseline at `work/2026-04-27/07-tucker-chunk3-corruption-baseline.transcript.md`) is normalized to `[01:48:57]` before classification, not warn-and-passed-through.
- **R2** `scripts/translate_video.py`'s existing public surface (`normalize_timestamp`, `timestamp_tolerance`, `normalize_mm_ss_zero_timestamp`, `should_reinterpret_part_as_mm_ss_zero`, `apply_timestamp_offset`) keeps working unchanged. `tests/test_translate_video.py` stays green.
- **R3** A regression test using actual Tucker chunk-3 timestamp values guards against re-regression.
- **R4** No skill-parity update needed (no new CLI surface). One-line CLAUDE.md guardrail records the shared-module rule.
- **R5** Re-running Tucker transcript with `--force` after the fix produces zero `Implausible timestamp [100:XX:XX]` warnings in chunk 3.

## Scope Boundaries

- **Non-goal:** repair already-corrupted transcripts on disk. Re-run with `--force` covers tonight's corpus.
- **Non-goal:** further chunked-transcript improvements (parallel chunks, alternate chunk sizing, etc.).
- **Non-goal:** the translate-bcs inversion (Option B). That's a separate issue; requirements doc at `docs/brainstorms/2026-04-27-translate-bcs-inversion-requirements.md`.

### Deferred to Separate Tasks

- Translate-bcs pipeline inversion (Option B): forthcoming GitHub issue, requirements doc already exists.
- Repair script for old corrupted transcripts: only needed if --force re-runs become impractical.

## Context & Research

### Relevant Code and Patterns

- `scripts/translate_video.py:116-134` — `normalize_timestamp` (the divmod-based malformation fixup; the function under port).
- `scripts/translate_video.py:137-143` — `timestamp_tolerance` (slack calculator; duplicate at `scripts/video_intel.py:1216-1222` as `_timestamp_chunk_tolerance`).
- `scripts/translate_video.py:146-153` — `normalize_mm_ss_zero_timestamp`.
- `scripts/translate_video.py:156-190` — `should_reinterpret_part_as_mm_ss_zero`.
- `scripts/translate_video.py:193-235` — `apply_timestamp_offset` — already calls `normalize_timestamp` as line 1 (line 203). This is the pattern the chunked path needs to mirror.
- `scripts/video_intel.py:1224-1288` — `_classify_and_offset_timestamp` — the broken caller; missing the pre-pass.
- `tests/test_translate_video.py:34, 269-291` — existing 6 unit tests for `normalize_timestamp` (cases: short timestamps, valid HH:MM:SS, malformed `[120:05:30]→[02:05:30]`, etc.). Authoritative pattern for the new test file.
- `tests/test_chunked_transcript.py` — PR #51's classifier test home; the `_classify_and_offset_timestamp` regression test belongs here.

### Institutional Learnings

- Stochastic Gemini output means a single Gate-1 video can't characterize the full output-format space. Memory: `feedback_premature_success.md`. PR #51's Lex Fridman smoke produced clean absolute timestamps; Tucker chunk 3 trips a different malformation. Tonight's regression test should encode the specific Tucker pattern.
- Solo-user repo: no over-architecting. Memory: `feedback_coding_style.md`, `feedback_speculative_issues_pruning.md`. Don't add module constants, classes, or generalized abstractions. Just move four pure functions.
- Squash-merge convention. Memory: `project_squash_merge_convention.md`. PR will land as one squashed commit on main.

### External References

None needed. Pure refactor of existing well-tested code.

## Key Technical Decisions

- **Move the test cases, don't duplicate them.** The 6 existing `normalize_timestamp` tests in `tests/test_translate_video.py` move wholesale to `tests/test_timestamp_utils.py`. `test_translate_video.py` no longer imports `normalize_timestamp` directly — translate_video.py itself imports from `timestamp_utils` and re-exports the names by virtue of the `from X import Y` pattern, so any third-party importing `translate_video.normalize_timestamp` still works (backward compat preserved without explicit `__all__` or aliasing).
- **Drop `_timestamp_chunk_tolerance` from `video_intel.py`.** It's a verbatim duplicate of translate's `timestamp_tolerance`. Replace call sites with the shared name.
- **`_classify_and_offset_timestamp`'s pre-pass is via `normalize_timestamp("[" + ts + "]")` because the classifier receives bare `"100:08:57"` strings (no brackets), but `normalize_timestamp` expects bracketed input.** Decide at implementation time whether to refactor the classifier to take bracketed input, write a small bracket-add/strip helper, or pass through with a regex variant. The simplest path is a thin wrapper inside `_classify_and_offset_timestamp`. **Defer to Unit 3.**
- **CLAUDE.md guardrail goes in the existing "Code Review Guardrails" section** as a one-line rule: "Timestamp normalization is shared via `scripts/timestamp_utils.py`; do not duplicate `normalize_timestamp` or its siblings into other scripts."
- **Ride-along the `.gitignore` `examples/*.fixed.txt` line** — already on disk, one-line, on-topic enough for "tidying the chunked-transcript fix" since both relate to the Tucker/Sachs forensic artifacts.

## Open Questions

### Resolved During Planning

- **Re-export pattern for backward compat:** chosen via Python's import semantics — `translate_video.py` does `from timestamp_utils import normalize_timestamp, ...` which makes those names accessible as attributes of `translate_video`. No explicit `__all__` needed.
- **Test file structure:** new `tests/test_timestamp_utils.py` carries the helper tests; `tests/test_chunked_transcript.py` carries the integration regression test for `_classify_and_offset_timestamp`.
- **Skill parity:** none needed. No CLI surface change.

### Deferred to Implementation

- Final shape of the bracket handling inside `_classify_and_offset_timestamp` — wrapper, regex variant, or signature change. Pick the smallest path during Unit 3.

## Implementation Units

- [ ] **Unit 1: Create `scripts/timestamp_utils.py` and matching test file.**

**Goal:** New shared module with the four pure helpers, plus a test file that covers the existing 6 normalize_timestamp cases and adds basic coverage for the other three helpers.

**Requirements:** R2, R3 (partial — helper tests).

**Dependencies:** None.

**Files:**
- Create: `scripts/timestamp_utils.py`
- Create: `tests/test_timestamp_utils.py`

**Approach:**
- Module contains only: `normalize_timestamp`, `normalize_mm_ss_zero_timestamp`, `should_reinterpret_part_as_mm_ss_zero`, `timestamp_tolerance`. Verbatim copies from `translate_video.py:116-190` with imports trimmed to just `re`. No constants, no classes, no helpers beyond these four.
- Test file imports `from timestamp_utils import ...` and re-runs the 6 normalize_timestamp cases that currently live in `test_translate_video.py:269-291`. Adds at least one happy-path test for each of `normalize_mm_ss_zero_timestamp`, `should_reinterpret_part_as_mm_ss_zero`, and `timestamp_tolerance`.

**Execution note:** Test-first. Copy the 6 existing cases first, then write the module to satisfy them. Then write the 3 new cases for the other helpers and add their implementations.

**Patterns to follow:**
- `tests/test_translate_video.py:269-291` for the test style (`assert normalize_timestamp(...) == ...`).
- The functions in `scripts/translate_video.py:116-190` as the canonical implementations.

**Test scenarios:**
- Happy path: `normalize_timestamp("[00:05:30] text")` returns `"[00:05:30] text"` (a <= 23, no change).
- Happy path: `normalize_timestamp("[120:05:30] text")` returns `"[02:05:30] text"` (a > 23, divmod kicks in).
- Happy path: `normalize_timestamp("[100:08:57] x")` returns `"[01:48:57] x"` — the **Tucker chunk-3 case**.
- Edge case: `normalize_timestamp("[60:00:00] text")` returns `"[01:00:00] text"` (a == 60, exact-hour case).
- Edge case: `normalize_timestamp("")` returns `""` (empty input).
- Edge case: `normalize_timestamp("no timestamp line")` returns the input unchanged (no match).
- Happy path: `normalize_mm_ss_zero_timestamp("[5:30:00]")` returns `"[05:30]"` (drops the trailing `:00` and re-pads).
- Happy path: `should_reinterpret_part_as_mm_ss_zero(...)` returns `True` only when the alt interpretation explains strictly more candidates than standard (use a fixture with 3+ such timestamps).
- Happy path: `timestamp_tolerance(3000)` returns `300` (chunk_duration // 10, capped at 300).
- Edge case: `timestamp_tolerance(60)` returns `30` (floor of 30s slack on tiny chunks).

**Verification:**
- `pytest tests/test_timestamp_utils.py -v` is green.

- [ ] **Unit 2: Migrate `scripts/translate_video.py` to import from `timestamp_utils`.**

**Goal:** Delete the four local function bodies; replace with a single `from timestamp_utils import ...` line. Existing `tests/test_translate_video.py` stays green via the re-export.

**Requirements:** R2.

**Dependencies:** Unit 1.

**Files:**
- Modify: `scripts/translate_video.py`
- Modify: `tests/test_translate_video.py` (delete the 6 normalize_timestamp test cases that moved in Unit 1; the file's other tests remain).

**Approach:**
- Add `from timestamp_utils import normalize_timestamp, normalize_mm_ss_zero_timestamp, should_reinterpret_part_as_mm_ss_zero, timestamp_tolerance` near the top imports of `translate_video.py`.
- Delete the original function bodies (translate_video.py:116-190).
- `apply_timestamp_offset` (line 193) keeps its body; its `normalize_timestamp(line)` call now resolves through the import.
- In `tests/test_translate_video.py`: delete the 6 `normalize_timestamp` test cases that were moved in Unit 1. The `from translate_video import normalize_timestamp` line at test_translate_video.py:34 stays — translate_video re-exports the name via the import.

**Patterns to follow:**
- `scripts/gemini_common.py` for the precedent of a small shared-utility module imported by both scripts.

**Test scenarios:**
- Test expectation: none for the script edit itself — Unit 1 already covers the helpers. The migration is verified by `tests/test_translate_video.py` staying green.

**Verification:**
- `pytest tests/test_translate_video.py -v` is green (line count drops by ~25 lines from the deleted normalize_timestamp tests; no other test should fail).
- `python -c "from scripts.translate_video import normalize_timestamp; print(normalize_timestamp('[120:05:30]'))"` outputs `[02:05:30]`.

- [ ] **Unit 3: Fix `scripts/video_intel.py` chunked transcript path.**

**Goal:** Delete the `_timestamp_chunk_tolerance` duplicate. Add the `normalize_timestamp` pre-pass to `_classify_and_offset_timestamp`. Add a Tucker chunk-3 regression test to the existing chunked-transcript test file.

**Requirements:** R1, R3, R5.

**Dependencies:** Unit 1.

**Files:**
- Modify: `scripts/video_intel.py`
- Modify: `tests/test_chunked_transcript.py`

**Approach:**
- Import `normalize_timestamp, timestamp_tolerance` from `timestamp_utils` at the top of `video_intel.py`.
- Delete `_timestamp_chunk_tolerance` (`scripts/video_intel.py:1216-1222`); update its single call site inside `_classify_and_offset_timestamp` to `timestamp_tolerance(chunk_duration_secs)`.
- At the top of `_classify_and_offset_timestamp` (after the docstring, before any logic), call the normalizer. Since the classifier receives a bare timestamp string like `"100:08:57"` (no brackets), wrap it: form `"[" + ts + "]"`, run through `normalize_timestamp`, then strip the brackets back. Or write a 5-line bracket-free variant inline. **Pick whichever is smaller** during implementation. Confirm the test passes either way.
- Add a regression test in `tests/test_chunked_transcript.py`: `_classify_and_offset_timestamp("100:08:57", chunk_start_secs=6000, chunk_duration_secs=3000)` returns a string parseable as ~1:48:57 absolute, NOT a warning-and-pass-through of `"100:08:57"`.

**Execution note:** Test-first. Write the regression test first; watch it fail with the current code; then add the normalize call.

**Patterns to follow:**
- `scripts/translate_video.py:193-235` `apply_timestamp_offset` is the analog implementation — it does the bracket handling already. Read it before deciding the bracket-handling shape.

**Test scenarios:**
- Regression (the bug): `_classify_and_offset_timestamp("100:08:57", 6000, 3000)` returns a normalized absolute (~`"1:48:57"` or `"01:48:57"`), no warning emitted.
- Regression edge: `_classify_and_offset_timestamp("100:00:00", 6000, 3000)` returns `~"1:40:00"` (chunk start, normalized).
- Regression edge: `_classify_and_offset_timestamp("100:22:35", 6000, 3000)` returns `~"2:02:35"` (chunk end, normalized).
- Existing classifier behavior unchanged: every test currently in `tests/test_chunked_transcript.py` for `_classify_and_offset_timestamp` (absolute, relative, implausible branches) stays green.

**Verification:**
- `pytest tests/test_chunked_transcript.py -v` green.
- The 3 new regression assertions confirm Tucker corruption is fixed.

- [ ] **Unit 4: CLAUDE.md guardrail and `.gitignore` ride-along.**

**Goal:** One-line guardrail in CLAUDE.md's Code Review Guardrails section recording the shared-module rule. Ride along the `examples/*.fixed.txt` `.gitignore` rule already on disk.

**Requirements:** R4.

**Dependencies:** Units 1-3 (semantically — guardrail describes the post-fix invariant).

**Files:**
- Modify: `CLAUDE.md` (add one bullet to the Code Review Guardrails section)
- Modify: `.gitignore` (already modified locally; this unit just stages it for commit)

**Approach:**
- Add a guardrail bullet near the existing transcript-related rules: `**Timestamp normalization lives in scripts/timestamp_utils.py.** Do not duplicate normalize_timestamp, normalize_mm_ss_zero_timestamp, should_reinterpret_part_as_mm_ss_zero, or timestamp_tolerance into other scripts. Reviewers: grep for these names in any new diff outside scripts/timestamp_utils.py and scripts/translate_video.py / scripts/video_intel.py imports.`
- The `.gitignore` line `examples/*.fixed.txt` is already on disk; just include it in the commit.

**Test scenarios:**
- Test expectation: none — pure documentation and gitignore tweak.

**Verification:**
- `git diff CLAUDE.md` shows one new bullet.
- `git diff .gitignore` shows the `examples/*.fixed.txt` line.

## System-Wide Impact

- **Interaction graph:** `scripts/translate_video.py` and `scripts/video_intel.py` both import from the new module. No other scripts touched.
- **Error propagation:** behavior is strictly additive — input that was previously warn-and-passed-through is now normalized into a valid timestamp. Inputs that were already correct stay unchanged (the `a <= 23` early-return in `normalize_timestamp`).
- **State lifecycle risks:** none. Pure functions, no I/O, no shared state.
- **API surface parity:** `scripts/translate_video.py`'s public symbols stay accessible because Python re-exports imported names. Third-party importers (none known in this repo) keep working.
- **Integration coverage:** the Tucker chunk-3 regression in `tests/test_chunked_transcript.py` covers the integration seam between the classifier and the new pre-pass.
- **Unchanged invariants:** `apply_timestamp_offset` in `translate_video.py` behaves identically (the function it imports `normalize_timestamp` from now resolves to the shared module instead of a local definition; same code).

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| The bracket-handling shape inside `_classify_and_offset_timestamp` introduces a subtle parsing bug. | Test-first regression. The Tucker case fails before the fix; passes after. Pattern mirrors `apply_timestamp_offset:203` exactly. |
| Existing translate_video.py tests break because of import-path changes. | Unit 2 explicitly verifies via `pytest tests/test_translate_video.py -v`. Re-export via Python import semantics keeps the public surface intact. |
| `_timestamp_chunk_tolerance` deletion misses a call site. | grep before deletion, replace all call sites, run full test suite. |

## Documentation / Operational Notes

- After merge, re-run Tucker transcript with `--force` to confirm zero `[100:XX:XX]` warnings on the canonical artifact. Treat this as the post-merge Gate-1 check (mentioned in the PR body).
- No release notes needed (solo-user repo, internal cleanup).

## Sources & References

- Issue: [#58](https://github.com/dzivkovi/video-intel/issues/58)
- Predecessor PR: [#51](https://github.com/dzivkovi/video-intel/pull/51)
- Real-input baseline: `work/2026-04-27/07-tucker-chunk3-corruption-baseline.transcript.md`
- Sibling brainstorm (Option B, separate issue): `docs/brainstorms/2026-04-27-translate-bcs-inversion-requirements.md`
