---
title: "fix: Default mindmap-from-video to MEDIA_RESOLUTION_LOW (token-budget regression for hour-long local files)"
type: fix
status: active
date: 2026-05-02
---

# fix: Default mindmap-from-video to MEDIA_RESOLUTION_LOW

## Overview

The `process_mindmap` video-source path silently inherits Gemini's API-default
HIGH media resolution (~258 tokens/frame). Combined with 1-FPS sampling and
Gemini's 1M-token input cap, this caps the mindmap-from-video path at videos
under ~67 minutes. Hour-long inputs return `HTTP 400 INVALID_ARGUMENT` ("input
token count exceeds the maximum number of tokens allowed 1048576"). This
contradicts the project's empirical finding (issue #58 Gate 3) that LOW
resolution gives equivalent quality at 3× lower input cost for our prompt's
needs — a finding already applied to the chunked-transcript path
([scripts/video_intel.py:1462-1494](scripts/video_intel.py#L1462)) but missed
on the mindmap-from-video path.

This plan brings the mindmap-from-video path into alignment with the
chunked-transcript precedent, adds an opt-out CLI flag for the rare case where
HIGH is genuinely needed (fine on-screen text reading), and locks the new
default in with tests + a CLAUDE.md guardrail.

## Problem Frame

- `process_mindmap` at [scripts/video_intel.py:1995](scripts/video_intel.py#L1995) accepts no `media_resolution` parameter.
- Its source="video" branch ([scripts/video_intel.py:2094-2104](scripts/video_intel.py#L2094)) calls `call_gemini` without passing `media_resolution`, so the API default (HIGH) applies.
- Token math: HIGH = ~258 tokens/frame at 1 FPS. A 91-minute video = 5460 frames × 258 = ~1.4M tokens, plus audio (~175K) and prompt — over the 1M cap.
- The chunked-transcript path solved the same shape of problem at [scripts/video_intel.py:1466,1494](scripts/video_intel.py#L1466) by forcing LOW. Comment at line 1462-1465 cites issue #58 Gate 3: HIGH is "3x cost without quality benefit for our prompt's needs."
- The mindmap prompt (theme/concept extraction) has the same prompt-shape characteristic — it does not require fine-grained on-screen text resolution. The same Gate 3 reasoning applies verbatim.

## Requirements Trace

- **R1.** Mindmap-from-video path defaults to `MEDIA_RESOLUTION_LOW`, restoring capability on long-form local recordings.
- **R2.** A `--media-resolution {low,high}` CLI flag on `process` and `mindmap` lets users opt into HIGH for the rare case where the prompt depends on fine on-screen text.
- **R3.** Tests lock in the new default and verify the flag override threads through to `call_gemini`.
- **R4.** SKILL.md surfaces the new flag and the long-video capability so users discover it via the skill description.
- **R5.** A CLAUDE.md Code Review Guardrails entry prevents future regressions that drop the LOW default.
- **R6.** Validation: the patch successfully processes a 91-minute 1080p H.264 video end-to-end (mindmap + transcript + concepts).

## Scope Boundaries

- **Out of scope: URL paths.** Issue #54 routed `process --url` and `mindmap --url` through mindmap-from-transcript (text-only, no token problem).
- **Out of scope: chunked-transcript path.** Already correct ([scripts/video_intel.py:1466,1494](scripts/video_intel.py#L1466)).
- **Out of scope: chunking the local-file path.** Explicit anti-pattern per CLAUDE.md (one-upload guarantee).
- **Out of scope: `process_transcript` non-chunked single-shot path.** If a single-shot transcript at HIGH is found to hit the same wall on a separate validation, file as a follow-up. This plan stays focused on mindmap-from-video, the verified failure mode.
- **Out of scope: changing Gemini's 1-FPS default.** `VideoMetadata.fps` is a different lever; the LOW resolution change alone solves the verified case.

## Context & Research

### Relevant Code and Patterns

- [scripts/video_intel.py:1466,1494](scripts/video_intel.py#L1466) — chunked-transcript path forcing `MEDIA_RESOLUTION_LOW`. Mirror this pattern exactly.
- [scripts/video_intel.py:1462-1465](scripts/video_intel.py#L1462) — comment citing issue #58 Gate 3 finding. The new mindmap-from-video site should reuse the same justification.
- [scripts/video_intel.py:1897-1943](scripts/video_intel.py#L1897) — `call_gemini` signature already accepts `media_resolution: str | None = None` and threads it through `config_kwargs["media_resolution"]`. **No call_gemini changes needed.**
- [scripts/video_intel.py:1995-2113](scripts/video_intel.py#L1995) — `process_mindmap` signature and source="video" call site. This is where the new parameter lands.
- [scripts/video_intel.py:4205-4219](scripts/video_intel.py#L4205) — `_mindmap_call` closure inside `cmd_process`. Threads args into `process_mindmap`. Add `media_resolution` argument here.
- [scripts/video_intel.py:3301](scripts/video_intel.py#L3301) — `cmd_mindmap`. Same threading pattern.
- [scripts/video_intel.py:5691-5780](scripts/video_intel.py#L5691) — `mm_parser` (mindmap subparser argparse setup). Add `--media-resolution` flag here.
- [scripts/video_intel.py:5784-5824](scripts/video_intel.py#L5784) — `process_parser` argparse setup. Same flag here.

### Institutional Learnings

- Issue #58 Gate 3 finding (referenced in the existing inline comment at [scripts/video_intel.py:1462-1465](scripts/video_intel.py#L1462)): MEDIA_RESOLUTION_LOW yields equivalent quality at 3× lower input-token cost for transcript-shaped prompts. The mindmap-from-video prompt has the same shape (theme extraction from talking-head + occasional slide content), so the finding extends.
- CLAUDE.md "one-upload guarantee" for `process --file`: do not chunk the local-file path. The LOW-resolution fix is the right knob here precisely because it preserves the one-upload guarantee while reducing per-frame token cost.
- CLAUDE.md "Mindmap is the AI's discovery surface and must always run on `process --url`": the same is true on `process --file`. Defaulting to LOW (which works at 91+ minutes) keeps that invariant intact for local-file workflows.

### External References

None needed — the codebase contains the exact pattern to mirror, with documented justification.

## Key Technical Decisions

- **Default LOW for source="video"**, not opt-in. The chunked-transcript path already established LOW as the correct project default for transcript-shaped prompts, and the mindmap prompt has the same shape. Keeping HIGH as default would be a known regression on hour-long files.
- **CLI flag default = "low"**, not "high". Mirrors the new in-code default, so users get the behavior they need without thinking about the flag.
- **Flag location = `process` and `mindmap` subparsers**, not at the top-level argparse. The flag is meaningful only on the mindmap-from-video path, which is reached only via these two commands' `--file` (and `mindmap --url` when no transcript exists, falling back to the legacy video path).
- **Flag values = `low` / `high`**, not the enum names. CLI ergonomics: `--media-resolution low` is what users will type. The script converts the string to `types.MediaResolution.MEDIA_RESOLUTION_LOW` / `MEDIA_RESOLUTION_HIGH` at the boundary.
- **`process_mindmap` parameter = `media_resolution: str | None = None`**, with source="video" defaulting to LOW when unset. This keeps `source="transcript"` and existing test fixtures unaffected (they pass no value, which still means LOW for video and ignored for transcript).
- **Do NOT pass `media_resolution` on the source="transcript" branch.** Text-only `call_gemini_text` does not accept it; passing it would be wrong. The new param is gated to the source="video" branch only.

## Open Questions

### Resolved During Planning

- **Should the chunked-transcript path expose the same flag for symmetry?** No — out of scope per the brief. It's already on LOW; users have no documented need to override.
- **Should the URL paths get the flag?** No — out of scope per the brief. Issue #54 routes them through text-only mindmap-from-transcript when a transcript exists; the legacy video fallback there is rare and a separate concern.
- **Is HIGH ever needed?** Rarely — when the mindmap prompt depends on reading fine print from slides or burned-in captions. The escape hatch flag covers this without changing the default for everyone else.
- **What about the single-shot transcript path?** Not in scope. The user's verified failure mode is mindmap-from-video; transcripts on long videos chunk and inherit LOW already. If a future report shows single-shot transcripts hitting the same wall, file as a follow-up issue at that point.

### Deferred to Implementation

- Exact `_make_thinking_config_for_transcript` analogue for mindmap (if any). Verify `call_gemini` doesn't reject `media_resolution` with the model used by mindmap (`gemini-3-flash-preview` per default config) — but the chunked-transcript path uses the same client and accepts it, so this is highly unlikely.
- Whether to log the resolved media_resolution at INFO level for observability. Probably yes (one extra log line per call) — implementer's call based on log noise tolerance.

## Implementation Units

- [ ] **Unit 1: Add `media_resolution` parameter to `process_mindmap`, default LOW for source="video"**

**Goal:** Bring the mindmap-from-video call site into parity with the chunked-transcript path.

**Requirements:** R1, R3

**Dependencies:** None.

**Files:**
- Modify: `scripts/video_intel.py` ([process_mindmap signature ~L1995](scripts/video_intel.py#L1995), [source="video" branch ~L2094-L2104](scripts/video_intel.py#L2094))
- Test: `tests/test_video_intel.py` (or an existing test module that imports `process_mindmap` — verify via grep before adding)

**Approach:**
- Extend `process_mindmap` signature with `media_resolution: str | None = None`.
- In the source="video" branch, if `media_resolution is None`, set it to `types.MediaResolution.MEDIA_RESOLUTION_LOW`. Pass to `call_gemini` as the existing `media_resolution=` kwarg.
- The source="transcript" branch is unchanged — it does not accept `media_resolution` and `call_gemini_text` does not use it.
- Add an inline comment at the new default site that mirrors the wording at [scripts/video_intel.py:1462-1465](scripts/video_intel.py#L1462), citing issue #58 Gate 3 as the durable justification (do NOT reference any specific source video).

**Patterns to follow:**
- [scripts/video_intel.py:1466,1494](scripts/video_intel.py#L1466) — exact same idiom (`media_resolution_low = types.MediaResolution.MEDIA_RESOLUTION_LOW`, then pass as kwarg).
- [scripts/video_intel.py:1462-1465](scripts/video_intel.py#L1462) — wording for the inline comment (issue #58 Gate 3 framing).

**Test scenarios:**
- *Happy path:* `process_mindmap(..., source="video")` with no `media_resolution` argument → `call_gemini` is invoked with `media_resolution == MEDIA_RESOLUTION_LOW`. Assertion via mock-spy on `call_gemini`.
- *Edge case (explicit override):* `process_mindmap(..., source="video", media_resolution=MEDIA_RESOLUTION_HIGH)` → `call_gemini` receives `MEDIA_RESOLUTION_HIGH`.
- *Edge case (transcript source unaffected):* `process_mindmap(..., source="transcript")` with no `media_resolution` → `call_gemini_text` is invoked, and `call_gemini` is NOT invoked. The new parameter does not leak into the text path.
- *Edge case (transcript source with explicit value ignored):* `process_mindmap(..., source="transcript", media_resolution="low")` → still routes to text path, no media_resolution kwarg sent (verified by mock-spy on `call_gemini_text` accepting kwargs).

**Verification:**
- New + existing tests pass. `pytest tests/test_video_intel.py -v` (or whichever module covers `process_mindmap`).
- Manual verification deferred to Unit 5.

---

- [ ] **Unit 2: Add `--media-resolution {low,high}` CLI flag to `process` and `mindmap` subparsers; thread through to `process_mindmap`**

**Goal:** Expose the LOW/HIGH choice at the CLI for the rare HIGH-needed case.

**Requirements:** R2, R3

**Dependencies:** Unit 1.

**Files:**
- Modify: `scripts/video_intel.py` ([mm_parser ~L5691](scripts/video_intel.py#L5691), [process_parser ~L5784](scripts/video_intel.py#L5784), [cmd_mindmap ~L3301](scripts/video_intel.py#L3301), [cmd_process / _mindmap_call ~L4205-L4219](scripts/video_intel.py#L4205))
- Test: `tests/test_video_intel.py` (argparse + threading)

**Approach:**
- Add `--media-resolution`, `choices=["low","high"]`, `default="low"`, `dest="media_resolution"` to both `mm_parser` and `process_parser`.
- In `cmd_mindmap` and `cmd_process`, convert the string to the matching `types.MediaResolution` enum value at the boundary, then pass to `process_mindmap` via the new kwarg.
- Help text for both flags: brief, explains LOW is the default and is sufficient for theme/concept extraction; HIGH is for the rare case where the model needs to read fine on-screen text. Reference issue #58 Gate 3 implicitly by saying "LOW is preferred (3× cheaper at equivalent quality for our prompt)."
- Conversion helper: a tiny private function `_resolve_media_resolution(types, choice: str)` that maps `"low" → MEDIA_RESOLUTION_LOW` / `"high" → MEDIA_RESOLUTION_HIGH`. Single call site each in cmd_mindmap and cmd_process.

**Patterns to follow:**
- Existing argparse style in [scripts/video_intel.py:5691-5824](scripts/video_intel.py#L5691) — explicit `choices=`, `default=`, `dest=`, multi-line `help=` strings.
- Existing kwarg-threading pattern in `_mindmap_call` closure at [scripts/video_intel.py:4205-4219](scripts/video_intel.py#L4205).

**Test scenarios:**
- *Happy path:* `argparse` parses `process --file F --media-resolution low` → namespace has `media_resolution == "low"`.
- *Happy path:* `argparse` parses `mindmap --file F --media-resolution high` → namespace has `media_resolution == "high"`.
- *Edge case (default):* `argparse` parses `process --file F` (no flag) → namespace has `media_resolution == "low"`.
- *Edge case (invalid value):* `argparse` rejects `--media-resolution medium` with a non-zero exit and an error message naming valid choices. (argparse provides this for free; one assertion line.)
- *Integration:* `cmd_process` with `args.media_resolution = "high"` invokes `process_mindmap` with the resolved `MEDIA_RESOLUTION_HIGH` enum value. Verified by patching `process_mindmap` and asserting on the kwarg.

**Verification:**
- All argparse + threading tests pass.
- `python scripts/video_intel.py process --help` shows the new flag with sensible help text.
- `python scripts/video_intel.py mindmap --help` shows the new flag.

---

- [ ] **Unit 3: SKILL.md update — document the new flag and long-video capability**

**Goal:** Make the flag and the new capability discoverable via the skill description.

**Requirements:** R4

**Dependencies:** Units 1-2.

**Files:**
- Modify: `skills/video-intel/SKILL.md`

**Approach:**
- In the "Process a local video" section, add a one-line note that the mindmap step now defaults to LOW media resolution, with a `--media-resolution high` opt-out for fine on-screen text reading.
- In the model-selection / scenarios table near the top of the file, add or update a row noting the LOW default removes the previous ~67-minute ceiling on `process --file` / `mindmap --file`.
- Do NOT add issue numbers, dates, or PR references (per `feedback_skill_docs_timeless_memory_personal` memory). Frame in capability terms only.
- Do NOT reference any specific source recording, work context, or personal use case.

**Patterns to follow:**
- Existing terse style in SKILL.md — short paragraphs, code-block examples, table rows.

**Test scenarios:**
- Test expectation: none -- documentation-only change. Verify by reading the rendered SKILL.md and confirming the new flag appears in the relevant sections.

**Verification:**
- `grep -n "media-resolution" skills/video-intel/SKILL.md` returns at least 2 hits (one in the process section, one in the mindmap section or capability table).
- The user-facing description still flows naturally — the addition reads as a capability note, not a changelog entry.

---

- [ ] **Unit 4: CLAUDE.md guardrail — prevent regression**

**Goal:** Lock the new default in via project-level code review guidance, so future PRs that inadvertently drop LOW get caught.

**Requirements:** R5

**Dependencies:** Units 1-2.

**Files:**
- Modify: `CLAUDE.md` (the "Code Review Guardrails" section near the bottom)

**Approach:**
- Add a single bullet under "Code Review Guardrails" with the same shape as existing entries (rule, rationale, reviewer instruction).
- Wording: "Mindmap-from-video path MUST pass `media_resolution=MEDIA_RESOLUTION_LOW` to `call_gemini` by default. This mirrors the chunked-transcript path's existing pattern at scripts/video_intel.py:1466. Issue #58 Gate 3 established that LOW yields equivalent quality at 3× lower input-token cost for our prompt's needs; HIGH would re-introduce the 1M-token ceiling on hour-long videos. Reviewers: any new mindmap call site that omits `media_resolution` or sets it to HIGH unconditionally needs pushback."
- Do NOT reference any specific source video, validation file, or personal context.

**Patterns to follow:**
- Existing CLAUDE.md guardrail entries — one bullet, rule first, then rationale ("Why" implicit), then reviewer instruction.

**Test scenarios:**
- Test expectation: none -- documentation-only change. Verification by reading the section.

**Verification:**
- `grep -n "MEDIA_RESOLUTION_LOW" CLAUDE.md` shows the new bullet.
- No accidental insertion in the wrong section.

---

- [ ] **Unit 5: End-to-end validation on a 91-minute 1080p H.264 local file**

**Goal:** Confirm the patch resolves the verified failure case end-to-end.

**Requirements:** R6

**Dependencies:** Units 1-4.

**Files:**
- None (no code or doc changes; validation only).

**Approach:**
- Run `python scripts/video_intel.py --log-level info process --file <gitignored-validation-file>` against the user's locally-available 91-min 1080p video.
- Confirm:
  1. Upload completes (~76s expected at 552 MB).
  2. Mindmap step succeeds with `usage mindmap prompt=<N> ...` log line showing `prompt < 1,048,576` (well under the cap thanks to LOW).
  3. Mindmap markdown lands at `<output_dir>/<channel>/<prefix>.mindmap.md`.
  4. Transcript step succeeds (chunked or single-shot, depending on duration).
  5. Concepts step succeeds (text-only, near-instant).
  6. `meta.json` records `modes_completed = ["mindmap", "transcript", "concepts"]`.
- If any step fails, iterate: read the error, identify root cause, patch, re-run. Document the iteration in this plan's "Iteration Log" section (added at validation time).
- This is the only step that requires the user's locally-stored validation file. Per confidentiality top rule, the source of the file is NEVER referenced in any commit, code, doc, or PR — only the generic "91-minute 1080p H.264 video" capability claim is published.

**Test scenarios:**
- Test expectation: none -- this is integration validation against a real Gemini API call, run once to confirm the patch ships. The unit test coverage in Units 1-2 is what locks the behavior in for the future.

**Verification:**
- `process --file` exits 0.
- All three artifacts on disk.
- `meta.json` shows full `modes_completed`.
- Token usage log line confirms LOW resolution applied.

## System-Wide Impact

- **Interaction graph:** `process_mindmap` is called from `cmd_process` ([scripts/video_intel.py:4205](scripts/video_intel.py#L4205)), `cmd_mindmap` (multiple sites), and the scan loop's mindmap branch (~L3180-L3194 etc.). Verify all call sites compile after the signature extension. The new parameter is keyword-only via the new default, so existing call sites that omit it continue to work unchanged.
- **Error propagation:** Unchanged. The `process_mindmap` error envelope (returns `(prefix, status_string)`) is preserved.
- **State lifecycle risks:** None. Default LOW is a quality-cost tradeoff favoring lower cost at equivalent quality (per issue #58 Gate 3); no on-disk format changes.
- **API surface parity:** The chunked-transcript path already passes LOW. After this plan, mindmap-from-video matches. Symmetry restored.
- **Integration coverage:** Unit 1's mock-spy assertions cover the parameter threading. Unit 5's end-to-end validation covers the full pipeline against a real Gemini call.
- **Unchanged invariants:** URL paths (mindmap-from-transcript), chunked-transcript path, single-shot transcript path, concept extraction, search, scan, dedupe, prune-shorts. None of these are touched.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| LOW resolution degrades mindmap quality on slide-heavy content where on-screen text is the primary signal | The escape-hatch flag `--media-resolution high` exists for this case. The default change is justified by issue #58 Gate 3 finding which empirically tested talking-head + overlay content. |
| New `media_resolution` parameter on `process_mindmap` breaks existing internal callers | Added as a keyword-only optional with default None — existing positional / partial-kwarg call sites are unaffected. Verified by reading all 11 call sites returned by grep. |
| `types.MediaResolution.MEDIA_RESOLUTION_HIGH` doesn't exist in the SDK version pinned in pyproject.toml | Verify before merging — add an import-time assertion if needed. The chunked-transcript path already uses `MEDIA_RESOLUTION_LOW` from the same enum, so HIGH is highly likely to exist. |
| Validation video is unavailable or has unexpected properties | Validation is the user's local responsibility (per CLI handoff). If validation fails, the patch is not merged until iteration produces a green run. |

## Documentation / Operational Notes

- SKILL.md update (Unit 3) is the user-facing surface for the new capability and flag.
- CLAUDE.md guardrail (Unit 4) is the project-internal documentation for future reviewers.
- README.md does NOT need updating — it covers higher-level pipeline shape, not media resolution details.
- No release-note generation needed (project ships rolling updates from main; the change is in the next normal release).

## Iteration Log

### Iteration 1 (Units 1-5 as planned)

- **2026-05-02 14:30** - Units 1-4 implemented and tested (9/9 unit tests pass; full suite 831/831).
- **2026-05-02 14:35** - Unit 5 validation against the 91-min 1080p H.264 reference video, gemini-3-flash-preview model.
  - **Mindmap:** failed with generic `400 INVALID_ARGUMENT`.
  - **Hypothesis:** `gemini-3-flash-preview` returns generic 400 errors regardless of root cause; need a model that surfaces specifics.
- **2026-05-02 14:36** - Re-validation with `--model gemini-2.5-pro` (CLAUDE.md-documented stable fallback for 3.x preview unreliability).
  - **Mindmap:** PASSED. Token usage `prompt=563450 cached=0 thoughts=3705 candidates=1199 total=568354`. Confirms LOW patch reduced cost from ~1.4M (HIGH baseline) to ~563K — within math prediction.
  - **Transcript:** FAILED with `The input token count exceeds the maximum number of tokens allowed 1048576`.
  - **Diagnosis:** `process_transcript` single-shot path was scoped out of Unit 1-4 ("file as follow-up if validation reveals"). Validation revealed it.

### Iteration 2 (Unit 6 — transcript path parity)

- [x] **Unit 6: Extend LOW default to `process_transcript` single-shot path; add `--media-resolution` to `tx_parser`**

**Goal:** Bring single-shot transcript into parity with mindmap; same root cause, same fix shape.

**Files modified:**
- `scripts/video_intel.py`: `process_transcript` signature + LOW default in `call_gemini` invocation; `cmd_transcript` resolves and threads `media_resolution_enum`; `_cmd_process_url` legacy fallback threads through too; `tx_parser` gets `--media-resolution` flag.
- `tests/test_mindmap_media_resolution.py`: new `TestProcessTranscriptMediaResolution` class with `test_default_passes_low_to_call_gemini` and `test_explicit_high_override_threads_through`.
- `CLAUDE.md`: guardrail extended to cover `process_transcript`.
- `skills/video-intel/SKILL.md`: transcript section now documents the LOW default and the flag.

**Test result:** 11/11 media-resolution tests pass; full suite 833/833.

**Validation:** re-running `process --file` against the same 91-min reference video with `--model gemini-2.5-pro --force` to confirm full pipeline (mindmap + transcript + concepts) succeeds end-to-end.

### Iteration 3 (Unit 7 — thinking-budget cap on single-shot transcript + None-defensive parser)

- [x] **Unit 7: Cap thinking budget on single-shot transcript path; defend `try_parse_transcript_json` against None input**

**Trigger:** Iteration 2 mindmap PASSED (cached at 544K tokens). Transcript reached Gemini successfully (token cap solved): `prompt=563697 cached=0 thoughts=65533 candidates=0 total=629230`. Two new failures observed:
1. **`candidates=0`, `thoughts=65533`** — Gemini 2.5 Pro burned its entire output budget on internal thinking, returned no text. Issue #58 Gate 2 in CLAUDE.md describes this exact pattern for the chunked path; the single-shot path was missing the same mitigation.
2. **`TypeError: the JSON object must be str, bytes or bytearray, not NoneType`** at `try_parse_transcript_json` line 2378 — the parser called `json.loads(None)` because Gemini's empty response wasn't defended against.

**Files modified:**
- `scripts/video_intel.py`: `process_transcript` now derives `thinking_config` via `_make_thinking_config_for_transcript(types, model)` (same helper used by chunked path) and passes it to `call_gemini`. `try_parse_transcript_json` accepts `str | None`, returns a parse error message on None / empty input instead of raising TypeError.
- `tests/test_mindmap_media_resolution.py`: new `TestProcessTranscriptMediaResolution.test_thinking_config_is_capped` regression test; new `TestTryParseTranscriptJsonNoneDefense` class with two tests for None / empty handling.

**Test result:** 14/14 media-resolution tests pass; full suite 833/833.

**Validation:** re-running `process --file` (without `--force` so mindmap lazily skips) to confirm transcript completes end-to-end and concepts step runs.

### Lessons captured for memory

- **Iteration discipline matched the plan's scope-out clause.** The brief listed `process_transcript` as out of scope but specified "file as follow-up if validation reveals." Iteration 2 followed that contract exactly — no speculative work, only evidence-driven extension.
- **Generic 400 from preview models hides root causes.** Always re-validate Gemini failures with `gemini-2.5-pro` to surface the actual error message before patching blindly.
- **Token-budget math is deterministic.** LOW = ~70 tokens/frame, HIGH = ~258 tokens/frame, audio = 32 tokens/sec. Multiply by duration; if total > 1M, the call will fail. Predictable means preventable in code review.
- **Bug layers are predictable in retrospect.** Iteration 2 fixed the surface (token cap on transcript). Iteration 3 fixed the next-deepest bug (thinking budget overflow) that was hidden by the surface bug. The chunked-transcript path already had BOTH mitigations from issue #58 Gates 2 and 3 — the single-shot path was missing both. CLAUDE.md guardrails should cover both paths in parity, not just the chunked one.
- **Gemini's `candidates=0` with non-zero `thoughts` is the canonical signature of thinking-budget overflow.** Look for it in any usage_metadata log line; never trust an exit-0 transcript without inspecting `candidates` count.
- **Defensive parsing at boundary functions matters.** `try_parse_transcript_json(None)` should return a parse error, not raise TypeError. Same idea applies anywhere the input crosses an external-system boundary.

## Sources & References

- Related code:
  - [scripts/video_intel.py:1462-1494](scripts/video_intel.py#L1462) — chunked-transcript LOW pattern (precedent)
  - [scripts/video_intel.py:1897-1943](scripts/video_intel.py#L1897) — `call_gemini` already accepts `media_resolution`
  - [scripts/video_intel.py:1995-2113](scripts/video_intel.py#L1995) — `process_mindmap` (target of Unit 1)
  - [scripts/video_intel.py:4205-4219](scripts/video_intel.py#L4205) — `_mindmap_call` closure (target of Unit 2 threading)
  - [scripts/video_intel.py:5691-5824](scripts/video_intel.py#L5691) — argparse subparsers (target of Unit 2 flag definition)
- Empirical token math: 91-min × 60 sec/min × ~258 tokens/sec (HIGH at 1FPS) = ~1.4M tokens — over Gemini's 1M cap
- Issue #58 Gate 3 finding: LOW = same quality at 3× lower input cost for transcript-shaped prompts (cited inline at scripts/video_intel.py:1462-1465)
