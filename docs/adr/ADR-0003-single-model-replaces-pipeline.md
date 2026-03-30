# Single Model Replaces Four-Model Transcription Pipeline

**Status:** accepted

**Date:** 2026-03-28

**Decision Maker(s):** Daniel

## Context

The previous transcription pipeline used Whisper for speech-to-text, pyannote for diarization, Claude for segmentation, and Gemini for diarization correction. Four models, four failure points, no visual content captured.

User pushed back on keeping Whisper after finding Brown University's CCV benchmark showing Gemini matches Whisper on transcription accuracy and beats it on diarization. An earlier recommendation to keep Whisper was based on incorrectly cited benchmarks (Gemma vs Whisper, not Gemini vs Whisper).

## Decision

Use Gemini Flash 3.x as the single model for transcription, diarization, and visual content capture. One API call replaces the four-model chain.

Gemini sees faces (not just voices) for speaker identification, reads on-screen text, and captures slides, diagrams, and code alongside speech -- capabilities the previous pipeline could not provide at any complexity level.

## Consequences

### Positive Consequences

- One API call replaces four model invocations
- Visual content captured for the first time (slides, diagrams, code)
- Better diarization through multimodal speaker identification (face + voice)
- Dramatically simpler error surface and lower total cost

### Negative Consequences

- Single point of failure on Gemini API availability
- No fallback if Gemini transcription quality regresses in a future model update
- Vendor concentration risk on Google for the extraction layer

## Alternatives Considered

- **Option:** Keep Whisper for transcription accuracy, add Gemini only for the visual layer
- **Pros:** Proven transcription quality from a dedicated ASR model
- **Cons:** Brown/Voice Writer benchmarks show Gemini matches Whisper on accuracy; maintaining two pipelines adds complexity without a quality benefit
- **Status:** rejected after user-driven research correction (original advice incorrectly cited Gemma vs Whisper, not Gemini vs Whisper)

## Affects

- `scripts/video_intel.py` (entire processing pipeline)

## Related Debt

None spawned.

## Research References

- Voice Writer ASR benchmark (Jan 2025)
- Brown University CCV AI transcription comparison
- `consolidated_chat.md` lines ~450-480 (the correction moment)
