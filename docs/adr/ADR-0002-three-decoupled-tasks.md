# Three Decoupled Tasks in Transcript Prompt

**Status:** accepted

**Date:** 2026-03-28

**Decision Maker(s):** Daniel

## Context

Asking Gemini to transcribe audio, describe screen content, and identify speakers in a single undifferentiated prompt degrades all three outputs. Laurent Picard's research found that within the Transformer architecture, tokens compete for attention -- decoupling tasks into explicit sections improves focus and output quality.

## Decision

The transcript prompt contains three explicitly separated tasks in a single API call, returning structured JSON:

1. **Task 1 (transcripts):** Focuses on audio -- verbatim diarized speech with timestamps.
2. **Task 2 (screen_content):** Focuses on vision -- slides, diagrams, code, on-screen text.
3. **Task 3 (speakers):** Uses both modalities for identity matching with visual/audio evidence.

Python's `merge_transcript_json()` fuses the three task outputs by timestamp sort into a single readable markdown document.

## Consequences

### Positive Consequences

- Each task gets dedicated attention within the model's context window
- Structured JSON enables programmatic merging and downstream tooling
- Speaker evidence (visual cues like name cards, titles) is captured separately from speech

### Negative Consequences

- More complex prompt engineering and maintenance burden
- JSON parse errors when Gemini truncates long output (4 failures observed in 260 videos)
- Requires merge logic in Python to reassemble a coherent document

## Alternatives Considered

- **Option:** Single monolithic "transcribe everything" prompt
- **Pros:** Simpler prompt, no merge logic needed
- **Cons:** Picard's research demonstrated measurable quality degradation with undifferentiated prompts
- **Status:** rejected

- **Option:** Two separate API calls (one for audio, one for vision)
- **Pros:** Complete task isolation, no attention competition
- **Cons:** Double the API cost, requires aligning two separate timelines with no shared context
- **Status:** rejected

## Affects

- `prompts/transcript.md`
- `scripts/video_intel.py` (`merge_transcript_json()`, `process_transcript()`)

## Related Debt

None spawned.

## Research References

- Laurent Picard, TDS series on Gemini multimodal prompting
- Philipp Schmid, `gemini-samples` notebook
- Google Cloud partner engineering blog
