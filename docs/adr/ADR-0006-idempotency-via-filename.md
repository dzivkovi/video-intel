# Idempotency via Filename Convention

**Status:** accepted

**Date:** 2026-03-28

**Decision Maker(s):** Daniel

## Context

Users run scan repeatedly (daily or weekly). Re-processing already-scanned videos wastes Gemini API budget. The system needs to know what has been processed without a database or external state store.

## Decision

Each video produces files named `{YYYY-MM-DD}-{slugified-title}.{mode}.md`. The `is_processed()` function checks if the output file exists and has size > 0. If yes, skip. No database, no state file, no lock mechanism.

The zero-byte check was added after a Ctrl+C incident where an interrupted write left an empty file that permanently blocked reprocessing. Checking `size > 0` ensures partial writes are retried on the next run.

## Consequences

### Positive Consequences

- Zero infrastructure — the filesystem IS the state store
- Survives process crashes (atomic `write_text`)
- Easy to force reprocessing by deleting the output file
- Works offline and is directly inspectable (`ls` shows what's done)

### Negative Consequences

- Filename changes (e.g., slug algorithm update from 50 to 80 chars) break matching — requires manual rename of existing files
- No tracking of processing attempts or timing metadata

## Alternatives Considered

- **Option:** SQLite database for processing state
- **Pros:** Richer querying, proper state machine, attempt tracking
- **Cons:** Another dependency, another file to manage, overkill for a filesystem-based tool
- **Status:** deferred (may revisit when cloud storage layer is added)

## Affects

- `scripts/video_intel.py` (`is_processed()`, `slugify()`, `video_file_prefix()`)

## Related Debt

None spawned.

## Research References

None.
