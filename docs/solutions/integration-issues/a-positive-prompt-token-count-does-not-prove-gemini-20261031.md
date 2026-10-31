---
title: "A positive prompt-token count does not prove Gemini watched the video"
date: 2026-10-31
category: integration-issues
module: video_intel.py
problem_type: integration_issue
component: tooling
symptoms:
  - "A 16-minute talk produced a transcript an order of magnitude smaller than healthy ones, with invented speakers and generic content, stamped `transcript_status: complete`"
  - "The usage line reported a non-zero prompt count, so no `prompt == 0` confabulation guard fired"
  - The mindmap and concepts built from that transcript were fabricated too, and still carried the real title
root_cause: missing_validation
resolution_type: code_fix
severity: high
related_components:
  - documentation
tags:
  - gemini
  - confabulation
  - transcript
  - quality-guard
  - usage-metadata
---

# A positive prompt-token count does not prove Gemini watched the video

## Problem

Every confabulation guard in the tree keys on one signal: Gemini reporting zero prompt tokens. A fabricated transcript that arrives with a non-zero prompt count passes all of them, is written as a healthy artifact, and feeds a fabricated mindmap and concepts into the taxonomy and the search index.

## Symptoms

- A 16-minute talk came back as a transcript an order of magnitude smaller than healthy ones: invented speaker names, generic content, written with `transcript_status: complete`.
- The usage line showed a non-zero prompt count, so the guard stayed silent.
- The mindmap and concepts derived from it were fabricated as well, yet every artifact carried the correct title.

## What Didn't Work

- **The shipped `prompt == 0` guard.** It exists on all three Gemini-watches-the-video calls: single-shot `process_transcript` (#60), `process_mindmap(source="video")` (#119) and each chunk of `_run_chunked_transcript_url` (#123), all reading the count through `log_usage_metadata` and `_coerce_token_count` (#125). It catches the case where Gemini ingested no video at all. It cannot catch a response where Gemini ingested something and still wrote from priors.
- **Title-word overlap as a discriminator.** The transcript header (`# Transcript: {title}`) and the mindmap header comments are built locally from API metadata after the call returns, so a fabricated artifact always carries the real title. Overlap between title and content says nothing about whether the body is real.
- **The quality assessor alone.** `assess_transcript_artifact` (#157) flags monolithic collapse (`MONOLITHIC_MAX_ENTRIES = 3`, or density below `DENSITY_SEVERE_PER_MIN = 0.1` per minute) and leading or internal blind gaps of 600 seconds or more. It may catch some fabricated stubs, but a short invented transcript with a dozen evenly spaced entries over 16 minutes sits well above both thresholds and passes.

## Solution

Not implemented, and no issue is filed yet. The signal that separated the fabricated transcript from healthy neighbours was output size against runtime: an order of magnitude below the healthy floor for a 16-minute talk. The proposed guard compares output bytes, or dialogue entries, per known minute of video against a floor calibrated on healthy transcripts, and treats a result below that floor the way the other severe quality flags are treated (demoted status, a visible warning, a non-zero exit on `process`).

The guard depends on a known duration. `duration_seconds` is recorded on the scan path, but not on every path today (a local file with no probed duration, for example), and the assessor already treats an unknown duration as "metrics only". Recording duration on every path is a prerequisite.

## Why This Works

`prompt == 0` answers "did Gemini receive any video?". A fabrication with a positive count answers yes and still invents the body. Size per minute answers a different question, "is there as much speech here as a talk of this length contains?", and invented content is consistently short because the model has nothing to transcribe. The two checks cover different failure shapes, so the proposed guard sits beside the existing ones rather than replacing them.

## Prevention

- Treat a positive prompt count as "Gemini received input", never as "the output is grounded".
- Calibrate any size-per-minute floor on the healthy distribution before choosing a threshold, so talk-heavy and demo-heavy videos do not false-alarm.
- When the guard ships, add its review guardrail to `.claude/rules/transcript.md` in the same PR, and add a `manual` row to `docs/troubleshooting.md` until then.
- A guard test must drive the real writer and prove the fabricated shape is caught, then be falsified by removing the check.

## Related Issues

- #60, #119, #123: the `prompt == 0` guard family.
- #125: `_coerce_token_count` and the absent-versus-zero rule; `tests/test_usage_shape_guard_parity.py`.
- #157: `assess_transcript_artifact` and the monolithic, density and blind-gap flags.
- `docs/troubleshooting.md`, row "Tiny transcript, `prompt=0`, looked complete".
