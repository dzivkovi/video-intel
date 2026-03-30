# Architecture

## System Overview

Video Intel is a two-API, single-script skill that uses Gemini as a multimodal
proxy for video understanding and produces markdown artifacts for Claude to
reason about.

```text
                    ┌─────────────────────────────────────┐
                    │           config.yaml                │
                    │  channels, prompts, output_dir       │
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────v──────────────────────┐
                    │       video_intel.py                  │
                    │                                      │
  YouTube Data API  │  fetch_channel_videos()              │
  (discover)  ──────>  get_channel_id()                    │
                    │         │                            │
                    │         v                            │
  Gemini API        │  process_mindmap()    ──> mindmap.md │
  (understand) ─────>  process_transcript() ──> transcript.md
                    │  call_gemini()        ──> meta.json  │
                    │                                      │
                    └──────────────────────────────────────┘
                                   │
                    ┌──────────────v──────────────────────┐
                    │      ~/video-intel/{channel}/         │
                    │  {date}-{slug}.mindmap.md             │
                    │  {date}-{slug}.transcript.md          │
                    │  {date}-{slug}.meta.json              │
                    └──────────────────────────────────────┘
                                   │
                    ┌──────────────v──────────────────────┐
                    │  Claude (triage, reasoning)           │
                    │  No API calls - reads markdown files  │
                    └──────────────────────────────────────┘
```

**Current state:** Everything runs locally. One Python script, two external
APIs (YouTube Data + Gemini), flat-file output. The skill entry point
(SKILL.md) triggers Claude Code to invoke the script.

## Project Vision

The system is evolving toward a two-tier architecture:

**Tier 1 - Indexing Backend** (runs independently, no skill dependency):

- Scheduled video scanning and transcription (cron or cloud function)
- Output to cloud object storage (S3, GCS, or similar) instead of local filesystem
- No dependency on Claude Code or any AI assistant for indexing

**Tier 2 - Skill Frontend** (Claude Code, Gemini CLI, etc.):

- Reads from the storage layer (local files or cloud objects)
- Triage, reasoning, Q&A over indexed content
- No dependency on Gemini/YouTube API keys (backend handles ingestion)

**Future layers** (not yet designed):

- Semantic search over indexed content (embeddings or knowledge graph)
- Cross-channel concept linking
- Automated scheduling and notification

The current single-script design serves both tiers. The separation will happen
when the storage layer moves to cloud.

## Architectural Decisions

Decisions are recorded as ADRs in [docs/adr/](docs/adr/). Summary:

| ADR | Decision | Core trade-off |
| --- | -------- | -------------- |
| [0001](docs/adr/ADR-0001-gemini-as-multimodal-proxy.md) | Gemini watches, Claude thinks | Two providers vs one, each doing what it's best at |
| [0002](docs/adr/ADR-0002-three-decoupled-tasks.md) | Three decoupled transcript tasks | Prompt complexity vs attention quality |
| [0003](docs/adr/ADR-0003-single-model-replaces-pipeline.md) | Single model replaces four | Vendor concentration vs pipeline simplicity |
| [0004](docs/adr/ADR-0004-external-prompt-files.md) | External prompt files | Duplication vs readability |
| [0005](docs/adr/ADR-0005-error-tracking-via-meta-json.md) | Error tracking in meta.json | Minimal fields vs full error database |
| [0006](docs/adr/ADR-0006-idempotency-via-filename.md) | Idempotency via filename | Filesystem simplicity vs slug change fragility |
| [0007](docs/adr/ADR-0007-per-channel-config.md) | Per-channel configuration | Config surface area vs creator-specific tuning |
