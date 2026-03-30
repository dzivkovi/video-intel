# Gemini as Multimodal Proxy for Video Understanding

**Status:** accepted

**Date:** 2026-03-28

**Decision Maker(s):** Daniel

## Context

Claude's API cannot process video -- there is no vision+audio streaming capability. Users need to analyze YouTube videos for content triage and transcription. Gemini's API accepts YouTube URLs directly via `file_data`, processes frames at 1 FPS alongside audio simultaneously, and returns structured text analysis.

## Decision

Use Gemini as a multimodal proxy for video understanding. Gemini watches videos and extracts structured artifacts. Claude reasons about those artifacts (mind maps, transcripts). Neither model does both jobs.

The skill only calls Gemini. Triage and deep-dive happen as conversations with Claude reading the resulting markdown files -- no additional API calls needed during those phases.

## Consequences

### Positive Consequences

- Each model does what it's best at: Gemini sees and hears, Claude reasons and converses
- No video download or ffmpeg pipeline required -- Gemini accepts YouTube URLs natively
- Free tier covers approximately 8 hours of video processing per day

### Negative Consequences

- Dependency on two separate API providers (Google for Gemini, Anthropic for Claude)
- Gemini's output quality gates Claude's reasoning quality -- garbage in, garbage out

## Alternatives Considered

- **Option:** Build a frame-extraction pipeline (ffmpeg + screenshots + Claude vision)
- **Pros:** Single provider, full control over frame selection
- **Cons:** Loses audio entirely, loses temporal coherence between frames, massive infrastructure complexity, no on-screen text reading at scale
- **Status:** rejected

## Affects

- `scripts/video_intel.py` (`call_gemini()`)
- `SKILL.md` (trigger descriptions)

## Related Debt

None spawned.

## Research References

- [Google video understanding docs](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Google audio understanding docs](https://ai.google.dev/gemini-api/docs/audio-understanding)
- `consolidated_chat.md` (the original design conversation with Claude Desktop)
