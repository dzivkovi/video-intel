---
title: Gemini 2.5 Pro injects task-wrapper format and Cyrillic-token intrusions into transcript JSON
date: 2026-04-26
category: integration-issues
module: salvage_transcript_sections
problem_type: integration_issue
component: tooling
symptoms:
  - "transcript --url on long political/business interviews intermittently produces .transcript.raw.txt sidecars that fail full-parse"
  - "salvage_transcript_sections returns 0 entries on some malformed responses despite a perfectly recoverable structure"
  - "Single Cyrillic words (минерал, хронический) appear at structural positions inside JSON output"
  - "Same prompt + same model + similar video occasionally returns [{task, output}, ...] instead of {transcripts, screen_content, speakers}"
root_cause: model_output_drift
resolution_type: defensive_normalization
severity: medium
tags:
  - gemini
  - gemini-2.5-pro
  - transcript
  - json
  - salvage
  - cyrillic
  - language-leak
  - structured-output
related_components:
  - tests
  - documentation
---

# Gemini 2.5 Pro injects task-wrapper format and Cyrillic-token intrusions into transcript JSON

## Problem

During a 4-month YCombinator backfill on 2026-04-25 (23 videos), two
Pro responses corrupted the JSON envelope `transcript` expects. The
two corruptions are independent and reproduce in different
combinations.

### Failure mode A: simple format + Cyrillic intrusion

`The Future Of Brain-Computer Interfaces` raw response, around line
96:

```json
      {
        "start": "01:54",
        "voice": 2,
 хронический "For those watching who have never heard of a brain computer interface, what is it?..."
      },
```

The Cyrillic word `хронический` ('chronic') replaced the `"text":`
key. Pro's training data leaked one word from another language at a
structural position in the JSON.

### Failure mode B: task-wrapper format

`The GPT Moment for Robotics Is Here` raw response, top of file:

```json
[
  {
    "task": "transcripts",
    "output": [
      {
        "start": "00:00",
        "voice": 1,
 минерал"text": "the equation I think for starting a robotic business has changed..."
      },
```

Two layered corruptions: (1) Pro decided to wrap each section in a
`{task, output}` object inside a top-level array instead of producing
the flat `{transcripts, screen_content, speakers}` envelope the prompt
asked for; (2) the same Cyrillic-word intrusion as failure mode A.

## Recovery asymmetry under the pre-fix salvage

`salvage_transcript_sections` (`scripts/video_intel.py:1599`)
recovered the two responses very differently. Empirical counts on the
real raw sidecars:

| Sidecar | Failure mode | Salvage today | Manual repair max |
|---|---|---|---|
| robotics raw | task-wrapper + Cyrillic | 0 / 0 / 0 | 93 / 14 / 5 |
| BCI raw | simple format + Cyrillic | 405 / 5 / 2 | 406 / 5 / 2 |

The simple-format case (BCI) salvaged near-perfectly. The
per-object recovery loop at `video_intel.py:1635` skipped one entry —
the one with the Cyrillic intrusion straddling the `"text":` key —
and absorbed the rest. Net loss: 1 entry out of 406.

The task-wrapper case (robotics) salvaged zero. The salvage regex
`"transcripts"\s*:\s*\[` literally cannot match `"task": "transcripts",`
because the comma comes after the string instead of the colon. The
entire array is invisible to the salvage scanner.

## Root cause

Two independent Gemini-side behaviors:

1. **Task-wrapper drift.** When the prompt includes phrases like
   "produce three tasks" or enumerates outputs, Pro sometimes
   restructures the response to mirror the request shape: a list of
   `{task: <name>, output: <data>}` objects. This is plausible
   over-fitting to instruction-tuning patterns ("explain your
   reasoning per task"). It does not happen on every response — once
   in 23 long videos in this batch, ~4%.

2. **Language leak at structural positions.** A single non-Latin
   token (Cyrillic in our two cases, but the same shape would apply
   to other scripts) gets emitted right before a JSON key or value.
   The injection point appears to be where the model is selecting
   the next token of a frequently-repeated structural literal —
   `"text":`, `"voice":` — and slips in a content token from a
   different language. The intrusion is always a single word and
   always at a position where it breaks JSON.

Both behaviors may quietly disappear in a Gemini model update. The
fix is intentionally narrow so it can be removed cleanly later.

## Resolution

A pure helper `_wrapper_to_envelope_dict(parsed)` operates on a
parsed Python object and rebuilds the flat envelope when the wrapper
shape is detected (top-level list, items are dicts with `task` in
`{transcripts, screen_content, speakers}`, `output` is a list).
Returns `None` when the input is not the wrapper shape, so callers
pass the original through.

Two callers use it at different layers:

1. `try_parse_transcript_json` — after a successful direct or
   isolated parse, normalizes a wrapper-shaped result into a flat
   dict. This is the **full-parse path** and was missed in the first
   draft of the fix; the correctness reviewer caught that a *clean*
   wrapper response (no Cyrillic intrusion) full-parses successfully,
   reaches `merge_transcript_json` with `raw_json[0]` =
   `{"task": ..., "output": [...]}`, and produces an empty transcript
   with `transcript_status: "complete"` — a silent regression.
2. `_normalize_task_wrapper` (text layer) — runs at the top of
   `salvage_transcript_sections` for the **salvage path** when the
   full parse fails. Tries the original text first, then a
   Cyrillic-stripped variant. Both attempts delegate to
   `_wrapper_to_envelope_dict` for the actual rebuild.

Cyrillic stripping (`_strip_cyrillic_for_structure`) is scoped to
the salvage-path helper only, never global. It exists to give
`json.loads` a chance at the wrapper itself when a Cyrillic intrusion
straddles a structural position. The strip is not applied to the
full-parse path because that path only fires when the JSON parses
clean, and it is not applied globally because verbatim foreign
content in legitimate speech is a real pattern (song titles, brand
names, multilingual interviews).

After the fix, on the same two real raw sidecars:

| Sidecar | Salvage post-fix | Acceptance criterion |
|---|---|---|
| robotics raw | 93 / 14 / 5 | ≥80 speech (PASS) |
| BCI raw | 405 / 5 / 2 | ≥400 speech (PASS, no regression) |

## What we did NOT do, and why

- **No global Cyrillic strip in salvage.** Issue #45 explicitly
  rejects this on false-positive grounds. The per-object salvage
  already absorbs single-token intrusions with ~1 entry lost per
  occurrence, which is acceptable.
- **No `response_schema` / Pydantic-typed Gemini output.** That is a
  larger change with broader unknowns (does it raise the failure
  rate? does it cost more tokens? does it work the same on 2.5 Pro?)
  and would need its own A/B against this fix.
- **No streaming JSON parser** like `ijson`. The wrapper case is
  syntactically valid JSON in the wrong shape — a tolerant parser
  would not have helped. The current regex-first approach is right
  for the actual failure modes seen.

## Reviewer guardrail

Added to `CLAUDE.md` under "Code Review Guardrails":

> **Salvage normalization is structural, not heuristic; Cyrillic
> stripping is scoped.** `_normalize_task_wrapper()` rebuilds a flat
> envelope from Pro's `[{"task": ..., "output": [...]}, ...]` shape
> (issue #45). It only fires when the JSON top-level is a list of
> `{task, output}` dicts; non-wrapper inputs pass through unchanged.
> The companion `_strip_cyrillic_for_structure()` is called only
> from inside the wrapper helper. Do not promote it to a global
> pre-strip in salvage.

## Tests

`tests/test_utils.py::TestSalvageTranscriptSections` adds:

- `test_recovers_from_pro_task_wrapper_format`
- `test_recovers_from_task_wrapper_with_cyrillic_intrusion`
- `test_task_wrapper_recovery_does_not_break_simple_format`
- `test_malformed_wrapper_falls_through_to_legacy_salvage`
- `test_robotics_raw_sidecar_recovers_at_least_80_speech_entries` (uses real fixture, skips when not on disk)
- `test_bci_raw_sidecar_still_recovers_at_least_400_speech_entries` (regression guard, real fixture)

## Reference

- Issue: [#45](https://github.com/dzivkovi/video-intel/issues/45)
- Requirements: [docs/brainstorms/2026-04-26-salvage-task-wrapper-requirements.md](../../brainstorms/2026-04-26-salvage-task-wrapper-requirements.md)
- Plan: [docs/plans/2026-04-26-001-fix-salvage-task-wrapper-plan.md](../../plans/2026-04-26-001-fix-salvage-task-wrapper-plan.md)
- Gate 1 evidence: [docs/plans/gate1-evidence/issue-45-salvage-smoke.txt](../../plans/gate1-evidence/issue-45-salvage-smoke.txt)
- Source videos:
  - https://www.youtube.com/watch?v=4EsUaur0nsQ (robotics)
  - https://www.youtube.com/watch?v=5gspRJVp9dI (BCI)
