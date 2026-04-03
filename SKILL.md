---
name: video-intel
description: >
  Multimodal video intelligence via Gemini API. Use this skill whenever the
  user wants to: find videos about a topic across multiple channels; browse
  what concepts are covered in the video library; scan a YouTube channel for
  new videos and get mind maps of each; triage which videos are worth
  watching; get a full diarized transcript with on-screen content (slides,
  diagrams, code) captured; add or remove channels to monitor; change scan
  settings. Trigger phrases include "what videos cover [topic]", "find videos
  about [concept]", "which creators talk about [subject]", "scan channel",
  "what's new from [creator]", "watch this for me", "transcribe this video",
  "add [channel] to my watchlist", "what should I watch", "summarize this
  video", "is this worth watching", any YouTube URL followed by a question,
  "show my channels", "what concepts are in my library", or "what topics
  recur across channels". This skill calls the Gemini API as a multimodal
  proxy - it sees video frames, reads on-screen text, and hears audio
  simultaneously. It also uses the YouTube Data API to discover new videos
  from configured channels. A concept layer (taxonomy.json) enables
  cross-video topic lookup without reading every file.
---

# Video Intel

Multimodal video scanning and transcription powered by Gemini.

## What This Skill Does

Three layers, designed as a narrowing funnel:

1. **scan** - Fetch new videos from configured YouTube channels, generate
   thematic mind maps for each video in parallel via Gemini's multimodal API.
   Optionally auto-generate transcripts and concept extraction.

2. **transcript** - Generate a fused document for a single video: diarized
   speech interleaved with timestamped SCREEN sections describing what was
   shown (slides, diagrams, code, demos). Uses a three-task decoupled prompt
   for best quality.

3. **concepts** - Extract and normalize key concepts from mind maps into a
   canonical vocabulary (taxonomy.json). Different videos use different words
   for the same idea — the concept layer resolves synonyms so cross-video
   queries work without reading every file.

**Triage workflow:**
- **"Which videos cover X?"** → `search "X"` (concept match, no API calls)
- **"What did someone say about X?"** → `search "X" --vector` (semantic match over transcripts)
- **"What themes recur across channels?"** → `search` with broad terms, or read taxonomy.json directly
- Read only the files returned by search — don't scan the entire corpus.

## Prerequisites

Two API keys required as environment variables:

- **GEMINI_API_KEY** - Get free at https://aistudio.google.com/apikey
- **YOUTUBE_API_KEY** - Get free at https://console.cloud.google.com/apis/credentials
  (enable "YouTube Data API v3")

Python dependencies:

```bash
pip install google-genai google-api-python-client pyyaml

# Optional: for vector search
pip install lancedb voyageai
```

If prerequisites are missing, tell the user what's needed and where to get it.

## How to Use

### Find videos about a topic (start here)

```bash
# "Which videos cover X?" — concept match, no API calls
python "${SKILL_DIR}/scripts/video_intel.py" search "skills standard"

# "What did someone say about X?" — semantic search over transcripts
python "${SKILL_DIR}/scripts/video_intel.py" search "150-line skill limit" --vector

# Filter either mode to a channel
python "${SKILL_DIR}/scripts/video_intel.py" search "context window" --channel natebjones

# Check corpus status
python "${SKILL_DIR}/scripts/video_intel.py" status
```

**When to use which:**
- **`search "X"`** — topic lookup. Matches concept labels and aliases in
  taxonomy.json. Fast, no API calls. Use for "which videos cover X?" or
  "what themes recur?"
- **`search "X" --vector`** — evidence lookup. Finds relevant transcript
  passages by meaning. Returns up to 3000 chars of each matched chunk with
  preserved structure (speaker turns, SCREEN blocks). Use for "what did
  someone say about X?" or queries where the exact words aren't in any
  concept label. Requires `VOYAGE_API_KEY`. Add `--preview` for compact
  200-char output.
- Vector results include the matched evidence directly — follow-up transcript
  reads are usually unnecessary. Only read the full source file if you need
  broader context around the matched passage.

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
- `--force` - Regenerate even if output files exist

### Transcribe a specific video

```bash
python "${SKILL_DIR}/scripts/video_intel.py" transcript \
  --url "https://www.youtube.com/watch?v=XXXXX"
```

Options:
- `--channel natebjones` - Save output under this channel's folder
- `--url` - YouTube URL to transcribe
- `--force` - Regenerate even if transcript exists

### Vector search (semantic / evidence queries)

```bash
# Build the vector search index from all transcripts (requires VOYAGE_API_KEY)
python "${SKILL_DIR}/scripts/video_intel.py" index

# Semantic search — finds relevant transcript passages by meaning
python "${SKILL_DIR}/scripts/video_intel.py" search "permission problems" --vector

# Filter vector search to a channel
python "${SKILL_DIR}/scripts/video_intel.py" search "150-line skill limit" --vector --channel natebjones
```

Vector search requires: `pip install 'video-intel[vector]'` and `VOYAGE_API_KEY`
(free at https://dash.voyageai.com/).

Use `--vector` for evidence queries ("what did they say about X?") that keyword
matching can't handle. Vector results show full chunk text (up to 3000 chars)
with preserved newlines — the evidence is in the output, no need to read the
source file unless you need more surrounding context. Add `--preview` for
compact 200-char single-line output. Use plain `search` (without `--vector`)
for concept lookups ("which videos cover agent skills?").

### Extract and normalize concepts

```bash
# Extract concepts from all mindmaps that don't have concepts yet
python "${SKILL_DIR}/scripts/video_intel.py" concepts

# Re-extract for a specific channel
python "${SKILL_DIR}/scripts/video_intel.py" concepts --channel natebjones --force

# Rebuild master taxonomy from all concept files
python "${SKILL_DIR}/scripts/video_intel.py" taxonomy-build
```

### Manage channels

Edit config.yaml directly or ask Claude Code to add/remove channels.
Claude Code has write access to the config file.

### Configuration

Configuration lives in `${SKILL_DIR}/config.yaml`. Key settings:

```yaml
output_dir: ~/video-intel          # Where output files are saved
default_since: 10d                 # Default lookback window
default_prompt: mindmap-knowledge  # Which prompt to use by default
auto_concepts: true                # Extract concepts after mindmap generation

channels:
  - name: natebjones               # Folder name for output
    url: https://youtube.com/@natebjones
    auto_transcript: all            # all | none
    since: 10d                      # Override default lookback
```

### Prompt files

Prompt templates live in `${SKILL_DIR}/prompts/`:
- `mindmap-knowledge.md` - Thematic mind map with domain terminology + timestamps (default)
- `mindmap-light.md` - Fast thematic scan (4-6 branches)
- `mindmap-heavy.md` - Comprehensive conceptual extraction
- `transcript.md` - Full diarized transcript with screen content
- `concepts.md` - Concept extraction + normalization against taxonomy

Each prompt is self-contained. Users can modify or add their own.

## Output Structure

```
~/video-intel/
├── taxonomy.json                                    # Master vocabulary (derived)
├── .lancedb/                                        # Vector search index (derived)
│   └── transcript_chunks.lance
├── natebjones/
│   ├── 2026-03-20-building-mcp-agents.mindmap.md
│   ├── 2026-03-20-building-mcp-agents.transcript.md
│   ├── 2026-03-20-building-mcp-agents.concepts.json
│   ├── 2026-03-20-building-mcp-agents.meta.json
│   └── ...
└── ramjad/
    └── ...
```

- **taxonomy.json** - Master vocabulary at the output root. Read this first for any topic query.
- **concepts.json** - Per-video normalized concepts with canonical IDs and aliases.
- **mindmap.md** - Thematic mind map with timestamps. Read for detail after finding via concepts.
- **transcript.md** - Full diarized transcript. Read for evidence/quotes after finding via concepts.

Files are idempotent. Re-running a scan skips already-processed videos.
Use `--force` on any command to regenerate.
