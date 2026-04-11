# Permissive Safety Filters for Faithful Reporting Pipelines

**Status:** accepted

**Date:** 2026-04-11

**Decision Maker(s):** Daniel Zivkovic

## Context

Both the translation pipeline (`translate_video.py`) and the transcription /
mindmap / concept pipeline (`video_intel.py`) are **faithful reporting
tools**. Their role is to convert what a video already contains — spoken
audio, on-screen text, visible speakers — into textual artifacts. They do
not generate opinions, answers, or new content. They report an external
signal.

Gemini's default safety settings apply content-category filters
(HARASSMENT, HATE_SPEECH, SEXUALLY_EXPLICIT, DANGEROUS_CONTENT) at
`BLOCK_MEDIUM_AND_ABOVE`. Those filters were designed for generative
chat, Q&A, and text-production workloads where the model is producing
its own content and filtering is the correct safety posture.

Applied to faithful reporting, the same filters produce a very different
failure mode:

- A long video covering war, crime, or political events can trigger a
  mid-stream content block.
- The stream closes cleanly. No exception is raised. The pipeline accepts
  the partial output as "complete."
- The user receives an output file that looks valid — metadata header,
  real timestamps, fluent prose — but is truncated mid-sentence at
  whatever point the filter fired.

This was verified empirically on 2026-04-11 with a 1h 4m interview about
an Israeli assassination attempt against a journalist, discussion of
killed medics, and war-crimes allegations. Single-request translation
produced a file ending at `[09:03]` — 14% coverage — with no error, no
retry, no log indication. The only reason the failure was detectable at
all was that the F1b coverage sanity check had been added previously,
which flagged `observed end 00:09:03 (TRUNCATED)` in the header.

The content was ordinary news reporting. The failure was not a safety
outcome — it was a subtitle file that omitted 54 minutes of a journalist
describing events the journalist had personally witnessed. Filter-induced
silent truncation made the artifact worse, not safer.

See
[docs/solutions/integration-issues/gemini-soft-stop-political-content-20260411.md](../solutions/integration-issues/gemini-soft-stop-political-content-20260411.md)
for the full diagnostic trail.

## Decision

Set Gemini `safety_settings` to `BLOCK_NONE` for all four standard harm
categories across every Gemini API call made by this skill. The settings
are produced by a single shared helper
`build_permissive_safety_settings(types)` in
`scripts/gemini_common.py`, consumed by:

- `scripts/translate_video.py :: call_gemini_translate` — BCS subtitle
  translation streaming
- `scripts/video_intel.py :: call_gemini` — multimodal video understanding
  for transcripts and mind maps
- `scripts/video_intel.py :: call_gemini_text` — text-only concept
  extraction over existing mind maps

### Categories set to BLOCK_NONE

- `HARM_CATEGORY_HARASSMENT`
- `HARM_CATEGORY_HATE_SPEECH`
- `HARM_CATEGORY_SEXUALLY_EXPLICIT`
- `HARM_CATEGORY_DANGEROUS_CONTENT`

### Category intentionally omitted

- `HARM_CATEGORY_CIVIC_INTEGRITY` — not universally supported across
  Gemini 2.x model variants, and unrelated to the violence/war-coverage
  cases that motivated this decision. Sending it risks 400
  INVALID_ARGUMENT on some models. If a future case proves it is
  needed and supported, add it at that time.

### What this decision does not do

- It does **not** turn off finish-reason capture or coverage diagnostics.
  Both paths still log `finish_reason` on non-`STOP` terminations, and
  the translation output file still emits a visible
  `## ⚠️ Incomplete translation` markdown block in the header when
  observed coverage falls below 95%. A safety block that somehow still
  fires — or any other early termination reason — will still be
  visible to the user.
- It does **not** change any prompt content, any model default, or any
  retry or timeout behavior.
- It does **not** apply to any hypothetical future code path that uses
  Gemini as a generator rather than a reporter. Any such code path
  added later should make its own filter choice explicitly rather than
  blindly inheriting `build_permissive_safety_settings`.

## Consequences

### Positive Consequences

- Eliminates the "completed successfully but produced truncated output"
  failure class for reporting pipelines on ordinary news, historical,
  political, and war-coverage video content.
- Produces faithful transcripts/translations of videos the skill is
  explicitly designed to handle.
- Consolidates the policy in one helper, so the decision is both
  discoverable and auditable in a single location.
- Matches the user's expectation that a transcription tool should
  transcribe what was said, not refuse to transcribe.

### Negative Consequences

- The skill now ships with a non-default safety posture. A future
  maintainer must read this ADR to understand why.
- If the skill is repurposed to generate content rather than report
  external content, the helper must not be reused without review.
- The finish-reason diagnostic path becomes more important, not less:
  if Gemini ever adds a non-overridable safety layer at a higher level,
  we must still be able to see it in the logs and the output file.
- Content that a model-generation use case would correctly refuse
  (harassment, detailed CSAM descriptions, operational malware) will
  also pass through. The mitigation is that this skill is not in the
  business of producing any of those from a prompt — it is in the
  business of reporting what a real video contains. If a video input
  contains something genuinely harmful, the output is a transcript of
  that input, not an amplification of it.

## Alternatives Considered

- **Option:** Leave filters at the Gemini default
  (`BLOCK_MEDIUM_AND_ABOVE`).
  - **Pros:** No non-default safety posture to explain. Matches other
    skills that use Gemini for generative work.
  - **Cons:** Produces silently truncated output on ordinary news
    coverage. Directly observed on the 2026-04-11 journalist interview.
    Fundamentally mismatched to a faithful reporting workload.
  - **Status:** rejected.

- **Option:** Filter only `translate_video.py`, leave `video_intel.py`
  at the Gemini default.
  - **Pros:** Narrower surface area, one script at a time.
  - **Cons:** Both scripts have the same workload profile (faithful
    reporting of external video content). The failure mode applies
    equally to transcription, mind-map generation, and concept
    extraction. Splitting the policy would create a maintenance trap
    where the two scripts drift.
  - **Status:** rejected.

- **Option:** Inline the safety settings at each call site instead of
  extracting a helper.
  - **Pros:** No new abstraction.
  - **Cons:** Three copies of the same block to maintain, and the
    decision becomes harder to find for a future reader. The helper
    is tiny and does one thing.
  - **Status:** rejected.

- **Option:** Include `CIVIC_INTEGRITY` in the `BLOCK_NONE` set.
  - **Pros:** Broader coverage if it ever fires.
  - **Cons:** Not universally supported on Gemini 2.x, risks
    INVALID_ARGUMENT at runtime, and is unrelated to the war/violence
    cases actually hitting the pipeline. Adds breakage risk without
    addressing a known failure.
  - **Status:** rejected. May be reconsidered if a concrete case
    emerges where CIVIC_INTEGRITY is the confirmed block reason.

- **Option:** Detect `finish_reason: SAFETY` and auto-retry with more
  permissive prompt framing instead of disabling the filter.
  - **Pros:** Keeps filters on by default. Only relaxes them on proven
    hits.
  - **Cons:** Adds state machine and retry budget to an already complex
    streaming path. Still produces a partial output on the first try
    that has to be discarded. Does not solve the "completed
    successfully but is truncated" failure class because the first
    output would still look valid to downstream consumers.
  - **Status:** rejected.

## Affects

Source files changed by this decision:

- `scripts/gemini_common.py` (`build_permissive_safety_settings()`)
- `scripts/translate_video.py` (`call_gemini_translate()`)
- `scripts/video_intel.py` (`call_gemini()`, `call_gemini_text()`)
- `tests/test_gemini_common.py` (`TestBuildPermissiveSafetySettings`)

## Related Debt

- If Gemini ships a non-overridable higher-layer safety posture that
  fires independently of `safety_settings`, revisit the finish-reason
  diagnostic path to make sure it still catches the case. Specifically,
  watch for `finish_reason` values like `BLOCKLIST`, `PROHIBITED_CONTENT`,
  and `SPII` that are not part of the overridable category filters.
- If `CIVIC_INTEGRITY` becomes universally supported on the model
  defaults used here and a real case shows it firing, reopen this ADR
  and add the category.
- If the skill ever acquires a generative code path (e.g. a chat-style
  command, a content suggestion feature), the helper must not be reused
  for it without a deliberate decision.

## Research References

- [docs/solutions/integration-issues/gemini-soft-stop-political-content-20260411.md](../solutions/integration-issues/gemini-soft-stop-political-content-20260411.md)
  — diagnostic trail for the 2026-04-11 journalist-interview failure
  that motivated this ADR
- [ADR-0014](ADR-0014-standalone-bcs-translation-workflow.md)
  — prior ADR on the standalone translation workflow (unchanged by
  this decision)
- [plans/eventual-prancing-dijkstra.md](../../plans/eventual-prancing-dijkstra.md)
  — F1/F1b/F2 coverage visibility plan that made the failure detectable
- [Gemini safety settings docs](https://ai.google.dev/gemini-api/docs/safety-settings)
- [Gemini finish_reason enum](https://ai.google.dev/api/generate-content#FinishReason)

## Notes

The honest framing of this decision is important. An earlier draft of
this change considered rewriting ADR-0014 to retroactively pretend
safety filters had always been a workflow-level concern. That would
have been dishonest and is explicitly rejected here. ADR-0014 records
the standalone-workflow decision as it was made on 2026-04-11 and
should stay unchanged. This ADR records a separate decision made on
the same day, triggered by a concrete failure mode, and is the first
and only authoritative reference for why filters are permissive in
this codebase.

Decision records are append-only. When a later diagnostic run reveals
that the 2026-04-11 journalist-interview failure was actually a
voluntary soft-stop (`finish_reason: STOP`) rather than a safety block
(`finish_reason: SAFETY`), that finding should be added as a follow-up
note at the end of the companion solution document, not by rewriting
this ADR's context. The decision to relax filters is still correct
regardless of which one it turns out to be, because both failure modes
are outcomes to surface to the user, not outcomes to hide.
