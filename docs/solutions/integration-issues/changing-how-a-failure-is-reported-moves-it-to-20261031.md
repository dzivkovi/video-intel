---
title: Changing how a failure is reported moves it to every downstream judge
date: 2026-10-31
category: integration-issues
module: video_intel.py
problem_type: logic_error
component: tooling
symptoms:
  - "A transcript with zero dialogue lines was stamped `transcript_status: complete` with a healthy entry count and no severe flag"
  - The run exited 0 and nothing re-queued the video
  - A value the chunked merge now let through reached a sort key that raised although its docstring said it could not
root_cause: logic_error
resolution_type: code_fix
severity: high
tags: [shared-predicate, writer-assessor-drift, status-literals, transcript-quality, unassessable, circuit-breaker, crash-to-skip]
---

# Changing how a failure is reported moves it to every downstream judge

## Problem

Turning a crash into a skip looked like a pure safety win. In the transcript pipeline it was worse than the crash it replaced: the writer started skipping malformed Gemini entries, the quality assessor still counted them, and an empty transcript was stamped `transcript_status: "complete"` with healthy-looking metrics (issue #161).

## Symptoms

- A `.transcript.md` with no dialogue lines, while meta.json reported a healthy `transcript_dialogue_entries` count and `severe: []`.
- Exit 0, so nothing downstream noticed.
- A non-string `start` that the chunked merge now let through reached the sort key `timestamp_to_seconds`, whose docstring promised a fall-through to 0; `split()` sat outside the `try` and raised `AttributeError`.

## What Didn't Work

- The first crash fix passed a smoke test that checked the merged markdown only. Reviewers who executed a repro found the silent `complete` empty transcript; reading the diff did not.
- For the follow-up quality guard (issue #157), consider a label-only design: write a quality field, leave the status alone. Listing every reader of `transcript_status` shows why that fails. The field carries both quality provenance and workflow completion, so a label alone would leave a detected-corrupt transcript looking healthy to the exit-code gate and to the mindmap-source resolver, which would then build the mindmap and concepts on top of it.

## Solution

- One predicate, `_usable_timestamp(entry, key)`, shared by the writer (`merge_transcript_json`), the assessor (`assess_transcript_artifact`) and the per-chunk relativizer (`_relativize_chunk_entries`). It checks shape only (a dict whose value is a non-blank string) and never synthesizes a replacement, because a fabricated `00:00` would sort the entry to the top and corrupt deep-links.
- `tests/test_merge_malformed_entries.py::TestWriterAndAssessorAgreeOnUsableEntries` derives the writer's rendered output and the assessor's verdict independently from the same raw payload and compares them, following the checker/writer pattern from PR #136.
- `timestamp_to_seconds` now keeps the whole parse, `split()` included, inside its `try`.
- For #157, a severe flag demotes the status to the existing `partial` literal, sets a step-level `quality_severe` key that `missing_pipeline_artifacts` turns into exit 3, and makes `resolve_mindmap_source` treat the transcript as unavailable under `auto`. Captions failover and `is_processed()` stay unchanged on purpose: each reader got its own decision.

## Why This Works

Two paths judging the same entry with private predicates drift, invisibly to any test that stubs one side. One shared predicate removes the second definition; the independent-derivation test catches a future copy. Enumerating readers turns "which literal" from a naming question into a checklist.

## Prevention

- Before changing how a failure is reported (crash to skip, error to partial, raise to log), list every reader of the affected status or value by file and line, and decide each one explicitly. A value that survives longer reaches consumers that never saw it before.
- A guard against silent incompleteness reads its control keys with required access. `missing_pipeline_artifacts` reads `step["requested"]`, so a misspelled key raises instead of quietly dropping a step from the check. `.get(default)` is right only where the default is the correct answer for every caller, as with its optional `quality_severe` key.
- Keep "could not determine" apart from "clean" or "absent". `scripts/remediation_sweep.py` puts a transcript with no known duration in an `unassessable` bucket, never `clean`, because without a duration the gap, density and monolithic checks are all gated off. Manual `--url` channel resolution (issue #205, `.claude/rules/scan-config.md` item 9) logs a line when a lookup raised for a request-wide reason such as quota or a bad key, instead of silently routing as "unconfigured", and a companion test proves a genuine no-match does not carry that line.
- Converting a per-item exception into log-and-continue changes the economics of every batch driver above it. `scripts/remediation_run.py` stops after `--max-consecutive-failures` (default 3) hard failures, and separately after consecutive re-runs that exit cleanly but do not improve the transcript.
- Open follow-up: `assess-transcripts` still tallies a transcript with unknown duration under `clean` in its summary counter. The same "unassessable is not clean" rule applies there.

## Related Issues

- Issues #161, #171 (malformed-entry hardening), #157 (quality assessor), #129 (exit 3), #172 (remediation sweep), #205 (channel resolution); PRs #136, #160, #166 and #174.
- `.claude/rules/transcript.md` (the #129, #157 and #161/#171 entries), `.claude/rules/scan-config.md` (#205 item 9).
- `docs/plans/2026-08-29-001-fix-transcript-quality-guards-plan.md` (design decisions and validation record).
