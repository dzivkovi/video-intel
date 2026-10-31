---
title: "Gate 1 evidence when the real failure cannot be replayed: real content with a synthetic defect, live legs for plumbing, the stubbed real caller for stochastic branches"
date: 2026-10-31
category: workflow-issues
module: pr-validation-discipline
problem_type: workflow_issue
component: development_workflow
severity: medium
applies_when:
  - A fix targets a failure whose original payload was never captured on disk
  - A Gate 1 smoke needs a pre-fix baseline to compare against
  - A paid live API cannot be steered into the exact failure shape under test
  - A data remediation (re-index, re-chunk) must be shown to have worked
tags: [gate-1, real-input-smoke, forensic-sidecars, synthetic-defect, pre-fix-baseline, real-caller, remediation-validation, evidence-legs]
---

# Gate 1 evidence when the real failure cannot be replayed

## Context

Gate 1 wants real input, but the failing input is often gone, and the forensic sidecars that should hold it often do not. On the chunked transcript path, a chunk that hits the wall-clock cap raises `TranscriptTimeout`, and `_run_chunked_transcript_url` records it as `(start, end, "")` in `failed_chunks`. When every chunk fails, the per-chunk `.transcript.raw.chunk*.txt` sidecars are written from that empty string, so they are 0 bytes, while the all-chunks-failed return still says "raw sidecars saved". A crash after a successful parse is worse: no `.transcript.raw.txt` is written on the healthy-parse path at all (the `_entry_snippet` docstring says so), so a run that died on a missing `start` key (issue #161) left nothing to replay.

## Guidance

- **Build the input from real content, inject only the defect, and say so.** The issue #158 harness in `docs/plans/gate1-evidence/` takes real dialogue from a chunked transcript already on disk and mechanically double-offsets one chunk's stamps; its docstring states "the INPUT is real content, the CORRUPTION is synthetic". The same move covers a deleted `start` key. Link such a PR with `Refs #N`, not `Closes #N`, until a real captured failure drives it.
- **Get the pre-fix baseline without checking out old code.** Give the harness a `SCRIPTS_DIR` argument so the same rig runs against a copy of `main` (both issue #129 harnesses do this). When `git diff` shows the only change is a module constant that the call site reads at call time, rebinding that constant to its old value is an equivalent baseline: `video_intel` passes `max_retries_transport=MAX_RETRIES_TRANSPORT` from its module globals on each call.
- **Fake the network, not the SDK.** The issue #129 harnesses stand up a local TCP server that accepts a connection and closes it without a status line. That drove the real `google-genai` client and real httpx to a verbatim pre-fix reproduction, where a mock would only raise the exception class the author expected.
- **Split the legs by what each can prove.** A live paid API cannot be made to produce a precise partial-failure shape (in one attempt the same chunk took very different times on consecutive tries). Use live legs for the deterministic plumbing, and prove the exact branch at the real caller with only the paid boundary stubbed, per [core: real-caller] in `CLAUDE.md`. The issue #248 contract does this: `tests/test_chunk_loss_captions_failover.py` drives the real `cmd_transcript --url` with the paid and network boundaries stubbed (Gemini, captions, and the metadata lookups). State in the PR which leg proves which claim.
- **Validate a data remediation by snapshot comparison, never by status lines.** Per-channel exit lines piped through `| tail` show that commands ran, not that data changed correctly. Before and after: the defect metric goes to zero, every untouched partition matches row for row, the new total equals a number predicted beforehand, the instrument and the N/25 retrieval eval are re-run, and one user-visible query that could not succeed before now does. Check every consumer of the remediated field (issue #195 has two: `index --channel` for LanceDB and `intel_graph.py load --force` for the DuckDB segment store).

## Why This Matters

Each shortcut yields evidence that looks complete and proves less than it claims: an empty sidecar hides a payload that was never saved, paid retries chase a shape the service will not produce on demand, a mock confirms the author's model of the failure, and a tailed status line confirms exit codes, not data. Naming which leg proves what stops a reviewer crediting a live run with a branch it never exercised.

## When to Apply

- A Gate 1 for a transient, stochastic or already-lost failure
- A pre-fix baseline that would otherwise need an old checkout
- Any re-index, re-chunk or other corpus remediation

## Examples

- Issue #158: real dialogue plus a synthetic double offset, because every sidecar on disk was 0 bytes.
- Issue #129: the drop-server harness (accept-and-close), run against two `SCRIPTS_DIR` copies (pre-fix: one attempt per call).
- Issue #248: paid attempts to lose exactly one chunk failed, so the branch was proven at the real caller.

## Related

- `docs/solutions/workflow-issues/multi-gate-ai-pr-validation-20260427.md`
- `.claude/rules/transcript.md` (#129, #158, #161, #248 entries)
- `.claude/rules/search-index.md` (#195 remediation, two consumers)
- `CLAUDE.md` [core: real-caller]
