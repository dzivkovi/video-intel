# External Prompt Files

**Status:** accepted

**Date:** 2026-03-28

**Decision Maker(s):** Daniel

## Context

The system has light and heavy variants of the mind map prompt, and prompt text is iterated on frequently. Prompts need to be versionable, swappable per channel, and shareable with other users. An early design included a `multimodal-prefix.md` that would be auto-prepended to all prompts at runtime, keeping the multimodal preamble DRY.

## Decision

Each prompt is a self-contained `.md` file in `prompts/`. No hidden prefix assembly — each file includes its own multimodal preamble. `config.yaml` references prompts by filename without extension. Different channels can specify different prompts via per-channel config.

The `multimodal-prefix.md` idea was rejected. Opening `mindmap-light.md` alone wouldn't show the full prompt Gemini actually receives — a leaky abstraction that makes debugging and sharing harder.

## Consequences

### Positive Consequences

- Each prompt is complete and readable standalone — what you read is what Gemini receives
- Users can swap, A/B test, and share prompt files by copying a single `.md`
- Per-channel prompt selection via `config.yaml` requires no code changes

### Negative Consequences

- Multimodal preamble is duplicated across prompt files
- If the preamble needs updating, multiple files must change manually

## Alternatives Considered

- **Option:** `multimodal-prefix.md` auto-prepended at runtime
- **Pros:** DRY — preamble lives in one place
- **Cons:** Leaky abstraction — opening a prompt file doesn't show the full prompt; debugging requires reading two files and understanding assembly order
- **Status:** rejected

## Affects

- `prompts/mindmap-light.md`
- `prompts/mindmap-heavy.md`
- `prompts/transcript.md`
- `config.yaml` (`prompt` field)
- `scripts/video_intel.py` (`load_prompt()`)

## Related Debt

None spawned.

## Research References

None.
