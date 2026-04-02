# Structured Logging via Python logging Module

**Status:** accepted

**Date:** 2026-04-02

**Decision Maker(s):** Daniel Zivkovic

## Context

All output in `video_intel.py` uses bare `print()` statements. This creates three problems:

1. **Invisible background jobs.** Python block-buffers stdout when output is redirected to a file (e.g., background tasks in Claude Code). A 15-minute concept backfill produces zero visible output until the buffer fills or the process exits. The user must check Google Cloud Console or count files manually to know if the job is running.

2. **No verbosity control.** Every run prints the same level of detail — progress lines, API key warnings, rate-limit retries, and error messages are all interleaved at the same level. There is no way to run silently in production or increase detail for debugging.

3. **No timestamps or structure.** When reviewing output after the fact (e.g., a failed background job), there are no timestamps to correlate with API billing or to measure per-video processing time.

The immediate trigger: a concept backfill ran for 6+ hours in the background with no visible output, while accumulating $1-2 CAD/hour in API costs. The user had no way to monitor progress without external tools.

## Decision

Replace `print()` calls with Python's standard `logging` module.

### Configuration

- **Default level: WARNING** — production-quiet. Only errors, rate limits, and skipped files appear.
- **`--log-level` argument** on all subcommands — `info` for monitoring, `debug` for troubleshooting.
- **Format:** `HH:MM:SS LEVEL   message` — compact, grep-friendly, includes timestamps.
- **Flushing:** `logging.StreamHandler` flushes after every record by default, eliminating the block-buffering problem.

### Level assignments

| Level | Content |
|-------|---------|
| `ERROR` | API failures after retries exhausted, JSON parse errors, missing files |
| `WARNING` | Rate limit retries, skipped videos (missing mindmap), API key ambiguity |
| `INFO` | Per-video progress with counter (`[12/69] [natebjones] slug: done`), batch start/end with totals and timing, taxonomy rebuild stats |
| `DEBUG` | Taxonomy size, prompt token estimates, match scores, full API config |

### Progress counter

Batch operations include a counter prefix: `[12/69]` so the user knows position in the queue without counting lines.

### Timing

Batch operations log elapsed time on completion: `"Completed 69 videos in 14m 32s"`.

## Consequences

### Positive Consequences

- Background jobs produce visible, timestamped output immediately (no buffering delay)
- Silent by default — `WARNING` level means normal production runs emit nothing unless something is wrong
- `--log-level info` gives full monitoring without checking external dashboards
- `--log-level debug` aids troubleshooting without adding `print()` statements
- Progress counter eliminates "is it stuck?" anxiety
- Timing data enables cost-per-video estimates
- Standard Python pattern — no new dependencies

### Negative Consequences

- Touches ~15-20 `print()` call sites across the file — moderate diff size
- Developers must use `log.info()` instead of `print()` going forward (minor habit change)
- `WARNING` default means users who previously saw progress output will see nothing until they add `--log-level info`

## Alternatives Considered

- **`flush=True` on print statements.** Fixes buffering only. No levels, no timestamps, no verbosity control. Status: rejected — solves the symptom, not the problem.
- **`--verbose` / `--quiet` boolean flags.** Two levels (on/off) instead of four. Simpler but less flexible — can't distinguish "show progress" from "show debug detail." Status: rejected — logging levels are the standard Python solution.
- **Third-party logging (structlog, loguru, rich).** Better formatting, but adds dependencies to a zero-dependency script. Status: rejected — stdlib `logging` is sufficient.
- **`PYTHONUNBUFFERED=1` environment variable.** Fixes buffering globally but is a runtime workaround, not a code fix. Doesn't add timestamps or levels. Status: rejected.

## Affects

- `scripts/video_intel.py` — all `print()` calls replaced with `log.info/warning/error/debug`, new `--log-level` argument, progress counters added to batch loops
- `SKILL.md` — document `--log-level` option
- `README.md` — document `--log-level` option

## Notes

The `logging` module's `StreamHandler` uses `stream.flush()` after every `emit()` call. This is the key property that solves the buffering problem — it's not just a logging best practice, it's the specific technical fix for the invisible-background-job issue.
