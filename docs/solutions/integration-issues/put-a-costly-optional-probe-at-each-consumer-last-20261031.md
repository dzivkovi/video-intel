---
title: "Put a costly optional probe at each consumer, last in its decision, memoized, and inside the worker"
date: 2026-10-31
category: integration-issues
module: cmd_scan
problem_type: performance_issue
component: tooling
symptoms:
  - "An eager refinement pass probed videos the idempotency filter was about to drop, probed under --dry-run, and probed channels whose explicit config made the answer irrelevant"
  - "A refinement pass limited to new videos missed a consumer that walks a wider list with its own processed check"
  - "With the lazy ask written as an executor.submit(...) argument, every probe ran one after another on the main thread before any worker started"
  - Functional tests stayed green throughout because every verdict was correct; only the timing was wrong
root_cause: concurrency
resolution_type: code_fix
severity: medium
tags: [lazy-evaluation, memoization, thread-pool, executor-submit, yt-dlp, premiere-probe, scan]
related_components:
  - testing_framework
---

# Put a costly optional probe at each consumer, last in its decision, memoized, and inside the worker

## Problem
The YouTube Data API attaches `liveStreamingDetails` to an aired premiere exactly as to a livestream, so a cheap flag needed an expensive refinement: a `yt-dlp` subprocess that reads `live_status` (measured at about 5 seconds per video, capped by `PREMIERE_PROBE_TIMEOUT_SECONDS = 15`). Issue #245 (PR #246) had to decide where to ask that question so the scan pays for it only when the answer can change an outcome, and without serializing the scan's thread pool.

## Symptoms
- An upstream refinement pass paid for videos that `is_processed` dropped a moment later, for `--dry-run` previews, and for channels with an explicit `transcript_source: gemini`, whose routing the probe cannot change.
- Refining only the new videos was wrong in a different way: the transcript loop walks a wider list with its own per-mode processed check, so some of its videos were never refined.
- Placing the lazy ask in the `executor.submit(...)` argument list made every probe run serially on the submitting thread before the first job was queued, so the pool bought nothing.

## What Didn't Work
- An eager probe in the pre-flight loop. Both review layers flagged the wasted spend.
- A refinement pass over only the new videos. A validator proved it misses the transcript loop's wider list.
- `executor.submit(fn, ..., refine(video))`. Python evaluates every argument before `submit` runs, on the calling thread, so the "lazy" ask was eager and serial. A peer pass caught it.

## Solution
- **One definition.** `refine_was_livestream(video_id, api_flag)` is the only place the yt-dlp signal is read. `_is_completed_livestream` and `_lookup_was_livestream` keep producing the raw Data API flag.
- **Ask at each consumer, as the last term.** The transcript router in scan, `transcript --url` and `process --url`, and the mindmap suppression in scan and `process --url`, each end their decision with the refine call, so every cheaper term (the explicit-config check, the duration gate that already chose captions, the suppression predicate) can short-circuit first.
- **Memoize per process.** `_PREMIERE_PROBE_RESULTS` maps video id to verdict, so the router and the suppression asking about one video pay one probe. A plain dict is enough: a lost race between workers costs one duplicate probe, never a wrong verdict.
- **Ask inside the worker.** The scan wraps the call in a closure (`_transcribe_with_lazy_probe`) that the pool runs, and `submit` receives only the closure and the video:

```python
futures = {executor.submit(_transcribe_with_lazy_probe, v): v for v in transcript_videos}
```

The `and refine_was_livestream(v["video_id"], True)` term lives inside that closure, not in the argument list.

## Why This Works
Laziness has two parts that are easy to conflate: *where* the question is asked (at the consumer, after its cheaper terms) and *when* the expression is evaluated (on the worker thread, not while building the call). Short-circuit `and` handles the first, and only a function body the worker executes handles the second. An argument to `submit` is a value, not a deferred computation.

## Prevention
- Before adding an upstream enrichment pass, list every consumer of the value and the list each one walks. If they differ, ask at the consumer.
- Put a costly call last in its boolean decision so cheaper terms decide first.
- Never compute a slow value in a `submit(...)` or `map(...)` argument. Pass the inputs and compute inside the task.
- Test the call thread, not just the verdict. `tests/test_premiere_probe.py::TestCmdScanPremiereRefinement::test_the_probe_runs_in_the_worker_not_the_submit_loop` records `threading.current_thread().name` inside a fake probe and asserts it is never `MainThread`. Sibling tests assert the probe is never called for regular uploads, already-processed videos, `--dry-run`, or an explicit `gemini` channel.
- Pin the probe off for the rest of the suite. `tests/conftest.py::no_premiere_probe` pins the executable memo to `{"exe": None}` and clears the verdict memo so ordinary tests never spawn yt-dlp, and it saves and restores both directly rather than requesting `monkeypatch`.

## Related Issues
- Issue #245, PR #246 (premiere refinement)
- `.claude/rules/transcript.md`, the #245 entry, invariants 1, 2 and 6
- Issue #120 (captions-first routing for livestream VODs), issue #227 (duration gate to captions)
