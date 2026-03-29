---
name: video-intel
description: >
  Multimodal video intelligence via Gemini API. Use this skill whenever the
  user wants to: scan a YouTube channel for new videos and get mind maps of
  each; triage which videos are worth watching; get a full diarized transcript
  with on-screen content (slides, diagrams, code) captured; add or remove
  channels to monitor; change scan settings. Trigger phrases include "scan
  channel", "what's new from [creator]", "watch this for me", "transcribe
  this video", "add [channel] to my watchlist", "what should I watch",
  "summarize this video", "is this worth watching", any YouTube URL followed
  by a question, or "show my channels". This skill calls the Gemini API as a
  multimodal proxy - it sees video frames, reads on-screen text, and hears
  audio simultaneously. It also uses the YouTube Data API to discover new
  videos from configured channels.
---

# Video Intel

Multimodal video scanning and transcription powered by Gemini.

## What This Skill Does

Two operations, designed as a funnel:

1. **scan** - Fetch new videos from configured YouTube channels, generate
   thematic mind maps for each video in parallel via Gemini's multimodal API.
   Optionally auto-generate full transcripts for channels where the user
   wants everything.

2. **transcript** - Generate a fused document for a single video: diarized
   speech interleaved with timestamped SCREEN sections describing what was
   shown (slides, diagrams, code, demos). Uses a three-task decoupled prompt
   for best quality.

After scanning, the user typically triages results in conversation with
Claude (no Gemini needed for that step).

## Prerequisites

Two API keys required as environment variables:

- **GEMINI_API_KEY** - Get free at https://aistudio.google.com/apikey
- **YOUTUBE_API_KEY** - Get free at https://console.cloud.google.com/apis/credentials
  (enable "YouTube Data API v3")

Python dependencies:

```bash
pip install google-genai google-api-python-client pyyaml
```

If prerequisites are missing, tell the user what's needed and where to get it.

## How to Use

### Scan channels for new videos

```bash
python "${SKILL_DIR}/scripts/video_intel.py" scan
```

Scans all channels in config.yaml, processes new videos since each channel's
`since` window, saves mind maps and (optionally) transcripts to the output
directory.

Options:
- `--since 14d` - Override the time window for this run
- `--channel natebjones` - Scan only this channel
- `--dry-run` - Show what would be processed without calling Gemini

### Transcribe a specific video

```bash
python "${SKILL_DIR}/scripts/video_intel.py" transcript \
  --url "https://www.youtube.com/watch?v=XXXXX"
```

Options:
- `--channel natebjones` - Save output under this channel's folder
- `--url` - YouTube URL to transcribe

### Manage channels

Edit config.yaml directly or ask Claude Code to add/remove channels.
Claude Code has write access to the config file.

### Configuration

Configuration lives in `${SKILL_DIR}/config.yaml`. Key settings:

```yaml
output_dir: ~/video-intel          # Where output files are saved
default_since: 10d                 # Default lookback window
default_prompt: mindmap-light      # Which prompt to use by default

channels:
  - name: natebjones               # Folder name for output
    url: https://youtube.com/@natebjones
    prompt: mindmap-light           # Override default prompt
    auto_transcript: all            # all | none
    since: 10d                      # Override default lookback
```

### Prompt files

Prompt templates live in `${SKILL_DIR}/prompts/`:
- `mindmap-light.md` - Fast thematic scan (default)
- `mindmap-heavy.md` - Comprehensive conceptual extraction
- `transcript.md` - Full diarized transcript with screen content

Each prompt is self-contained. Users can modify or add their own.

## Output Structure

```
~/video-intel/
├── natebjones/
│   ├── 2026-03-20-building-mcp-agents.mindmap.md
│   ├── 2026-03-20-building-mcp-agents.transcript.md
│   ├── 2026-03-20-building-mcp-agents.meta.json
│   └── ...
└── ramjad/
    └── ...
```

Files are idempotent. Re-running a scan skips already-processed videos.
