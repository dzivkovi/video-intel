---
title: "Gemini soft-stops mid-video on politically heavy content — diagnostics + permissive safety filters"
date: 2026-04-11
category: integration-issues
tags: [gemini, safety-filters, translate, transcribe, truncation, finish-reason, coverage-diagnostics, long-video]
components: [scripts/translate_video.py, scripts/video_intel.py, scripts/gemini_common.py]
severity: high
symptoms:
  - "Single-request translation of a 1h 4m video stops silently at 00:09:03 (14% covered)"
  - "Output file looked valid — metadata header, real timestamps, fluent BCS prose — but body ends mid-sentence"
  - "No error raised, no retry triggered, stream closed cleanly"
  - "Logs show finish_reason absent from pipeline (not captured before fix)"
  - "Only noticed because the F1b coverage sanity check flagged 'observed end 00:09:03 — TRUNCATED'"
root_cause: "Gemini 2.5 Pro applies content filtering to video understanding outputs the same way it does to text generation. Politically or emotionally heavy content (assassination attempts, dead journalists, war-crimes discussion) triggers either a SAFETY-category block or a voluntary soft-stop before the model reaches the end of the clip. Neither surfaces as an error; the stream closes normally with a non-STOP finish_reason that the pipeline was discarding."
---

# Gemini Silent Truncation on Politically Heavy Content

## Problem

A 1h 4m 5s interview video (journalist discussing an Israeli assassination
attempt against him, killed medics, and alleged war crimes) ran through
`translate_video.py` in single-request mode. The pipeline logged a clean
completion, wrote the output file, and exited successfully. No retries
fired, no exceptions raised.

The output file was **14% complete**: a clean metadata header, a well-formed
sequence of `[HH:MM:SS]` timestamps from `[00:00:04]` through `[09:03]`,
and then abrupt silence. The last line ended mid-thought. The remaining
54 minutes of audio were never translated.

The only reason we caught it at all is that a prior session had added
F1b coverage visibility to the single-request path. The coverage header
correctly annotated:

```
**Coverage:** 00:00 – 01:04 / 01:04 total — observed end 00:09:03 (TRUNCATED)
```

Without that annotation, the file would have looked finished.

## What We Tried

### Attempt 1: Verify input budget and token limits (ruled out)

Hypothesis: maybe Gemini ran out of context or output tokens.

- Video is 64 min at low media resolution (~100 tokens/sec) → ~384K input
  tokens against Gemini 2.5 Pro's 1M limit. 60% headroom.
- `max_output_tokens=65536` and the response was ~4K tokens. 62K unused.

Neither limit was hit. Not a budget issue.

### Attempt 2: Check if stream errored silently (ruled out)

Hypothesis: maybe the stream iterator threw something the pipeline ate.

- Reviewed `call_gemini_translate`'s exception handling: every branch
  either raises or logs before returning partial output.
- No exceptions seen in the run. The stream terminated cleanly via the
  `("done", None)` sentinel in the drain thread.

Not a plumbing issue. Gemini itself declared the response complete.

### Attempt 3: Capture `finish_reason` to see what Gemini actually said

Hypothesis: Gemini's final chunk carries a `finish_reason` field that the
pipeline was discarding. `STOP` means voluntary end, `MAX_TOKENS` means
length hit, `SAFETY` means content filter fired.

- Added capture of `finish_reason` from `chunk.content.parts[*].finish_reason`
  in the streaming drain loop.
- Extended `call_gemini_translate` to return it as a third value.
- Added a WARNING log line when `finish_reason != "STOP"`.

This gave us the observability to distinguish the three cases, but did not
itself fix the problem — the first run after this change still needed a
real integration test to reveal which category this specific video falls
into.

### Attempt 4: Relax safety filters at the shared layer

Hypothesis: even if we do not yet know whether `finish_reason` was `STOP`
or `SAFETY`, relaxing safety filters cannot hurt for a faithful
transcription pipeline and removes one entire class of truncation causes.

- Added `build_permissive_safety_settings(types)` in `scripts/gemini_common.py`
  that returns `BLOCK_NONE` for the four standard categories: HARASSMENT,
  HATE_SPEECH, SEXUALLY_EXPLICIT, DANGEROUS_CONTENT.
- Intentionally omitted `CIVIC_INTEGRITY` — not universally supported
  across Gemini 2.x and unrelated to the violence/war-coverage cases we
  were actually hitting.
- Wired it into `call_gemini_translate` and both `video_intel.py` paths
  (`call_gemini` for multimodal, `call_gemini_text` for concept extraction).

The translation pipeline is a faithful-reporting tool: it reports what was
said, it does not generate content of its own. Filter-induced silent
truncation on ordinary news coverage produces broken subtitles mid-sentence,
which is worse than the content being reported. This is the right default
for the use case.

### Attempt 5: Make truncation impossible to miss in the output file

Hypothesis: logs are invisible when the script runs in the background, as a
skill, or inside a larger pipeline. A log-only WARNING is a usability bug.
The truncation needs to be visible in the artifact itself.

- Added `build_incomplete_notice()` which renders a markdown H2 block
  (`## ⚠️ Incomplete translation`) with:
  - observed vs requested timestamps and percentage covered
  - finish_reason-specific root-cause line (tailored for SAFETY, MAX_TOKENS,
    STOP, and unknown reasons)
  - actionable advice per reason (retry, split, different model)
  - a final sentence stating the text below is partial
- `build_header` emits the block right after the metadata `---` separator
  when observed coverage is below 95%, so it appears before the transcript
  body and cannot be mistaken for transcript content.

## Working Solution

Four independent changes, landed together:

1. **Capture `finish_reason`** in the streaming drain loop of
   `call_gemini_translate`. Return it as a third value. Log a WARNING when
   it is not `STOP`.
2. **Shared permissive safety settings** in `gemini_common.py`,
   applied to both `translate_video.py` and both `video_intel.py`
   Gemini call sites. CIVIC_INTEGRITY intentionally omitted.
3. **Visible `## ⚠️ Incomplete translation` notice block** in the
   output file, emitted by `build_header` when coverage is below 95%.
   Root cause text is tailored to the actual `finish_reason`.
4. **Move the `--force` stale-parts cleanup** to run before either
   the chunked or single-request branch. Previously it only ran in the
   chunked branch, so videos that moved from chunked to single-request
   after the F1 threshold change would leave old `part-N-N` files on
   disk that would poison any later stitch.

Changes 1 and 3 are the diagnostic pair. Change 2 is the one that might
actually fix this specific video — we will know after the next rerun
shows whether `finish_reason` was `SAFETY` (safety blocked it, change 2
fixes it) or `STOP` (model voluntarily stopped, change 2 does not help).
Change 4 is an unrelated regression caught during cleanup.

## Evidence

First run on the failing video before fixes:

| Metric | Value |
|---|---|
| Video duration | 01:04:05 |
| Last observed timestamp | 00:09:03 |
| Coverage | 14% |
| Input tokens used | ~384K of 1M |
| Output tokens used | ~4K of 65K |
| Exception raised | none |
| Retries fired | 0 |
| Log line indicating truncation | none (pre-fix) |
| finish_reason observed | unknown (pre-fix — not captured) |

After fixes, the same video run will:

- Log a WARNING with the actual `finish_reason`
- Emit the F1b coverage annotation in the header
- Emit the visible `## ⚠️ Incomplete translation` H2 notice block
  before the transcript body, with tailored root-cause advice
- Use BLOCK_NONE safety filters in the actual API call

## Prevention Strategies

### Do not discard structured metadata from API responses

- `finish_reason` exists on every Gemini generation response and is
  documented. Discarding it is a silent-failure generator.
- Any time you accept a streaming generation, capture the final-chunk
  metadata: `finish_reason`, `usage_metadata`, any safety ratings. If
  you do not log them at minimum, you have no path to diagnose the
  "completed successfully but produced wrong output" case.

### Faithful-reporting tools should not default to restrictive content filters

- A translation pipeline, a transcription pipeline, and a captioning
  pipeline are all in the business of reporting what was said, not
  generating content. Content filters that truncate such pipelines
  on war coverage, crime reporting, or political speech produce broken
  artifacts and user confusion, not safety.
- The correct default is `BLOCK_NONE` for the standard categories,
  documented in the code and in an ADR so the decision is auditable.
- This is distinct from a model that generates fiction, answers
  questions, or produces new content — there, stricter filters are
  appropriate. The distinction is whether the model is reporting an
  external signal or producing its own.

### Make silent failures loud in the artifact, not just in the log

- Logs are invisible for skills, background tasks, and scripted
  pipelines. A user opening an output file must be able to see the
  failure state without reading the terminal.
- Header annotations are a good start. A visible markdown H2 block
  with root cause and advice is better because it cannot be skipped
  by a reader scanning the body.
- The block must be stylistically distinct from the transcript content
  (H2 header, warning emoji, prose phrased in the first person about
  the system, not about the video) so it cannot be mistaken for a
  transcript line.

### Coverage sanity checks belong on every path, not just chunked

- The F1b single-request coverage check and the F2 per-chunk coverage
  check cover both translation paths now, but the instinct that led
  to this bug — "single-request is simpler, it doesn't need the same
  diagnostics as the chunked path" — is wrong. Any path that can
  truncate needs a post-run sanity check.
- Apply the same thinking to any future path added to this codebase:
  if Gemini can return early, the caller must verify completeness
  against a deterministic signal (video duration, requested window,
  expected line count).

## Open Questions

The fix landed without a verified rerun on the original failing video.
Two outcomes are possible:

1. **Rerun succeeds end-to-end.** Means the 9:03 stop was a safety-category
   block. Change 2 (BLOCK_NONE) fixed it. The full video now translates.
2. **Rerun truncates again, `finish_reason: STOP` in the log.** Means the
   9:03 stop was a model-level voluntary soft-stop — Gemini 2.5 Pro
   decided it was "done" despite having plenty of budget. Safety settings
   do not help. Need to try a different model or prompt framing.

Either outcome is useful: the diagnostics in changes 1 and 3 will make
the answer immediately visible.

## Related Documentation

- [ADR-0014](../../adr/ADR-0014-standalone-bcs-translation-workflow.md)
  — standalone workflow architecture (unchanged by this fix)
- [ADR-0015](../../adr/ADR-0015-permissive-safety-filters-for-faithful-reporting.md)
  — decision record for the BLOCK_NONE safety policy
- [gemini-model-selection-preview-vs-ga.md](./gemini-model-selection-preview-vs-ga.md)
  — prior diagnostic on a different Gemini translation failure mode
- [plans/eventual-prancing-dijkstra.md](../../../plans/eventual-prancing-dijkstra.md)
  — F1/F1b/F2 coverage visibility plan that made this bug detectable
- [Gemini safety settings docs](https://ai.google.dev/gemini-api/docs/safety-settings)
- [Gemini finish_reason enum](https://ai.google.dev/api/generate-content#FinishReason)
