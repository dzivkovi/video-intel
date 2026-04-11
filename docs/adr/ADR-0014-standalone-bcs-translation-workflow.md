# Standalone BCS Translation Workflow for Audio-First Long-Form Video

**Status:** accepted

**Date:** 2026-04-11

**Decision Maker(s):** Daniel Zivkovic

## Context

A BCS (Bosnian/Croatian/Serbian) subtitle translation capability was added
initially as a small convenience utility. In practice, it exposed a different
set of constraints than the main `video_intel.py` pipeline.

The transcript pipeline is a multimodal understanding workflow: it cares about
who spoke, what was shown on screen, and how visual evidence relates to the
audio. Translation is different. Its primary goal is to convert spoken English
audio into readable BCS subtitles with timestamps.

That difference changed the architecture in several ways:

- Translation is **audio-first**, not screen-first
- Long videos exposed Gemini operational issues not seen in short runs:
  stalls, malformed timestamps, and silent early termination
- The cost / capacity trade-off is different from transcript generation because
  translation usually does not need dense OCR or fine visual detail
- Recoverability matters more: a failed long run should not force the entire
  video to be reprocessed from scratch

Implementation and debugging showed that translation is not just "another
prompt." It is a separate workflow with its own token-budget strategy, retry
behavior, artifact model, and long-video failure modes.

This ADR is retrospective. The utility began as a small add-on, but the
implementation surfaced enough architectural consequences that the workflow now
needs an explicit record.

## Decision

Treat BCS subtitle translation as a **standalone workflow**, implemented in
`scripts/translate_video.py`, separate from the main
`video_intel.py` transcript / mindmap / concept pipeline.

### Translation is optimized for audio-first processing

- Translation defaults to **low media resolution**
- This is an explicit cost / capacity choice: audio understanding is preserved
  while video-frame token usage drops significantly
- Higher visual resolution is reserved for future translation prompts that
  genuinely need to read slides, captions, or other on-screen text

### Translation remains operationally separate from the main pipeline

- Translation will not be folded into `video_intel.py`
- It keeps its own CLI, retry behavior, output directory conventions, and tests
- Low-level Gemini helpers may be shared, but pipeline behavior and artifacts
  remain independent

### Long videos use chunking with stitch-time recovery

- Single-request translation is preferred when the video safely fits the chosen
  resolution budget
- When a video exceeds that safe window, translation is chunked
- Chunk outputs are written as **part files**, which are treated as the
  canonical recoverable artifacts
- Stitching is a separate step that produces a convenience merged file from the
  canonical parts

### Robustness is favored over pretending outputs are perfect

- Long-video translation must tolerate Gemini stalls, malformed timestamps, and
  partial chunk failures
- Failures should be surfaced and preserved, not silently hidden
- Stitching is responsible for deterministic normalization and diagnostics at
  chunk boundaries
- Coverage visibility is preferred over speculative repair for cases where the
  model simply did not produce enough output

## Consequences

### Positive Consequences

- Translation is cheap enough to use on long-form videos by default because it
  does not spend transcript-style budget on unnecessary visual detail
- Long videos can be processed incrementally rather than as one fragile request
- Failures are easier to recover because part files survive as first-class
  artifacts
- Translation-specific reliability logic can evolve without complicating the
  main transcript pipeline
- Lessons learned here can later inform transcript hardening without coupling
  the two workflows prematurely

### Negative Consequences

- The repository now contains two multimodal video workflows with different
  operational rules
- Chunking and stitching add complexity that single-request paths do not have
- Timestamp normalization, coverage checks, and long-video recovery need
  dedicated tests and maintenance
- Some reliability concerns are now solved in translation first and may be
  duplicated in transcription until shared abstractions are justified

## Alternatives Considered

- **Option:** Integrate BCS translation into `video_intel.py`
  - **Pros:** Single entry point, fewer scripts, one artifact pipeline
  - **Cons:** Mixes audio-first translation concerns with transcript / concept
    pipeline concerns; different operational profile and failure modes
  - **Status:** rejected

- **Option:** Use default or high media resolution for all translation runs
  - **Pros:** More visual detail if future prompts need it
  - **Cons:** Higher cost, lower safe single-request duration, unnecessary for
    the dominant audio-first use case
  - **Status:** rejected

- **Option:** Force long videos through one request whenever possible
  - **Pros:** Simpler output model, no stitch step
  - **Cons:** More fragile on long inputs; harder recovery when Gemini stalls
    or truncates
  - **Status:** rejected

- **Option:** Treat stitched output as the canonical artifact
  - **Pros:** Simpler user-facing mental model
  - **Cons:** Loses recoverability; makes partial reruns and backfills harder
  - **Status:** rejected

## Affects

Source files changed by this decision:

- `scripts/translate_video.py`
- `prompts/translate-bcs.md`
- `tests/test_translate_video.py`
- `README.md`
- `CLAUDE.md`

## Related Debt

- Improve long-video coverage diagnostics for both chunked and single-request
  translation paths
- Revisit chunk threshold policy as Gemini model limits and default resolution
  behavior evolve
- Evaluate which translation reliability helpers should later be shared with
  transcript workflows
- Keep translation and transcript media-resolution policies distinct unless
  evidence supports unifying them

## Research References

- `work/2026-04-11/01-stitch-retry-budget-p2.md`
- `work/2026-04-11/03-transcript-media-resolution-findings.md`
- `plans/eventual-prancing-dijkstra.md`
- [Gemini API media resolution docs](https://ai.google.dev/gemini-api/docs/media-resolution)
- [Gemini API video understanding docs](https://ai.google.dev/gemini-api/docs/video-understanding)

## Notes

The important architectural lesson is not merely that "BCS translation exists."
The durable decision is that long-form subtitle translation has a different
operational profile from multimodal transcript generation and should be treated
as its own workflow.

This ADR records the stable shape of that workflow after implementation
revealed the real constraints.
