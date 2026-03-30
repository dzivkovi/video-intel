# Error Tracking via meta.json

**Status:** accepted

**Date:** 2026-03-28

**Decision Maker(s):** Daniel

## Context

Running scan on 260 videos produced 4 JSON parse errors and 1 permanent 403 (a YouTube Short restricted from Gemini API). Errors were printed to stdout but not recorded anywhere. On re-run, failed items retry automatically (file-based idempotency), but there is no way to distinguish "never attempted" from "attempted and failed", no way to see what failed without re-running, and no way to permanently skip broken videos.

## Decision

Add two optional fields to the existing `meta.json` per video:

- `last_error` (string or null) — set on failure, cleared on success. This is the dead letter.
- `skip` (boolean, absent by default) — set manually by user to permanently exclude broken videos.

A failure summary prints at the end of each scan with actionable guidance. No new files, no new CLI flags, no new commands. ~30 lines of code change.

## Consequences

### Positive Consequences

- Three-way state distinction: never attempted / failed / succeeded
- Errors persist across runs for diagnostics without re-running
- Skip mechanism for permanent failures (e.g., restricted Shorts)
- Automatic retry remains the default behavior

### Negative Consequences

- `meta.json` grows by two fields per video entry
- Manual skip requires user to edit JSON — no auto-detection of permanent failures

## Alternatives Considered

- **Option:** Separate error log file (`errors.json` or `errors.log`)
- **Pros:** Clean separation of concerns
- **Cons:** Another file to manage, divorced from the video it describes, requires correlation logic
- **Status:** rejected

- **Option:** Auto-skip on 403 errors
- **Pros:** No manual intervention needed
- **Cons:** Scope of "permanent" errors may expand unpredictably; user should make that judgment call
- **Status:** rejected

## Affects

- `scripts/video_intel.py` (`process_mindmap()`, `process_transcript()`, `cmd_scan()`, new `is_skipped()`)

## Related Debt

None spawned.

## Research References

None.
