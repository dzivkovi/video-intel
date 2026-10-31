---
title: "Prove a check can fail before trusting its verdict: validate smoke harnesses and mutation runs, and let only the property under test decide the fixture"
date: 2026-10-31
category: workflow-issues
module: pr-validation-discipline
problem_type: workflow_issue
component: development_workflow
severity: high
applies_when:
  - "A smoke harness, A/B script or mutation run is used as evidence that a fix works"
  - "A guard is falsified before shipping, per the real-caller rule in CLAUDE.md"
  - "A test asserts on an exit code, a severity flag or a ranking position"
  - "A fixture could trip more than one detector, ranking key or bypass branch"
tags: [testing, falsification, mutation-testing, smoke-harness, fixture-isolation, exit-codes, real-caller, vacuous-tests]
---

# Prove a check can fail before trusting its verdict: validate smoke harnesses and mutation runs, and let only the property under test decide the fixture

## Context

The `[core: real-caller]` principle in `CLAUDE.md` says a guard test is falsified before it ships. That assumes the falsification is sound. Harnesses and mutation runs were trusted on their summary line or exit code: one falsification only changed a guard's exit code and still passed with the guard deleted, and tests named as regression locks were found that passed with the lines they locked removed, on a fully green suite.

A broken harness does not crash. It returns a clean, symmetric, exit-0 result that reads like a finding. Shapes seen: an injection anchor that never matched, a script whose own folder replaced the repo on `sys.path`, a first-match `str.replace` that patched the wrong one of three identical lines, new tests placed after a `return` and never collected, and a `--dry-run` smoke that returned before the changed line.

## Guidance

Validate the instrument before reading its verdict:

- Run the 2x2: pre-fix and post-fix code, each against both expectations.
- Confirm the mutation landed: diff the file, or assert inside the script that the target text was found and replaced exactly once. Read "zero reds" as "the mutation did not happen" until proven otherwise.
- Compare `pytest --collect-only` counts before and after adding tests. Tests that are not collected are always green.
- Read exit codes directly, never through a pipe (a pipe reports the last command's status). In PowerShell `$?` is a boolean; the number is in `$LASTEXITCODE`.

Falsify by deleting the guard, not by flipping its output. An earlier preamble or a downstream catch-all can emit the same signal:

- `tests/test_file_size_guard_exit.py` documents both: a missing `GEMINI_API_KEY` and a prompt load exit 1 before the size guard, and `_cmd_process_impl` wraps the upload in `except Exception: ... sys.exit(1)`. The fix: `_isolated` stubs every pre-guard check, each test asserts `upload_local_video` was never reached, and `TestTheseTestsReachTheGuardTheyClaimToTest` is a canary proving a normal-sized file does reach the upload, once per command.
- `pytest.raises(SystemExit)` is satisfied by `SystemExit(0)`; assert `exc.value.code` (`.claude/rules/transcript.md`, the #185 entry).

Let only the property under test decide the outcome:

- A co-firing sibling detector satisfies a "severe" or exit-code assertion on its own. Assert the exact flag list. The isolation tests in `tests/test_chunk_window_mismatch.py` densify the fixture so no #157 flag can fire, then assert the flag list equals `["chunk_window_mismatch_severe"]` (filtering only the documented #139 sibling).
- A ranking test whose pair also differs in a second key (for example severity) cannot show that the first key is sorted. Hold every other ranking key equal.
- A fixture with one untouched item cannot see a loop that lost its bound. Use two or more untouched items and count calls: `tests/test_process_force_propagation.py` (`TestScanAutoConceptsForcePropagation`) runs five historical videos plus two touched ones and asserts only the two get a concepts call.
- An OR-shaped bypass needs one case per disjunct. The `SEGMENTS` parametrization in `test_file_size_guard_exit.py` covers start-only, end-only and both, so `or` turning into `and` goes red.
- Pin a deliberately kept residual with a test asserting it still exists, so a warning is never later read as a fix. Examples: `test_does_not_claim_the_monolithic_early_stop_shape` in `tests/test_output_cap_truncation.py`, and `TestDroppedMalformedEntriesShrinkTheSeverityDenominatorOnPurpose`.

## Why This Matters

A vacuous test removes the reviewer's doubt while leaving the defect live. A falsification that cannot fail certifies the test, which then certifies the code, and both certificates are empty.

## When to Apply

- Before quoting a harness, smoke or mutation result as evidence in a PR or issue.
- When a test's claim is an exit code, a flag, an ordering, or "a paid call was not made".

## Examples

Before: flipping the size guard's `sys.exit(1)` to `sys.exit(0)` turns the exit-code test red, so the test is declared sound. With the guard deleted, the same test passes, because the upload handler's catch-all also exits 1. After: delete the guard, assert the exit code and zero uploads, and keep the canary. Now exactly the guard's tests go red.

## Related

- `CLAUDE.md`, Code Review Guardrails, `[core: real-caller]` and `[core: probe-before-pay]`
- `.claude/rules/transcript.md`, the #185 entry
- `docs/solutions/workflow-issues/multi-gate-ai-pr-validation-20260427.md`
