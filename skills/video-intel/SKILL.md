---
name: video-intel
description: >
  Multimodal video intelligence via Gemini API. Use whenever the user wants
  to: find videos about a topic across channels; browse concepts in the
  library; scan a YouTube channel for new videos and get mind maps; triage
  which videos are worth watching; get a diarized transcript with on-screen
  content (slides, diagrams, code) captured; add/remove monitored channels;
  change scan settings. Trigger phrases: "what videos cover [topic]", "find
  videos about [concept]", "which creators talk about [subject]", "scan
  channel", "what's new from [creator]", "watch this for me", "transcribe
  this video", "add [channel] to my watchlist", "what should I watch",
  "summarize this video", "is this worth watching", any YouTube URL +
  question, "show my channels", "what concepts are in my library", "what
  topics recur across channels". Calls Gemini as multimodal proxy (frames +
  on-screen text + audio). A taxonomy layer enables cross-video topic lookup
  without reading every file.
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

**Triage workflow — pick the right mode first:**

| Query type | Examples | Command |
|------------|----------|---------|
| **Evidence** (who/what/when/how) | "which companies adopted X?", "what did they say about Y?" | `search "X" --vector` |
| **Discovery** (which videos / themes) | "which videos cover X?", "what themes recur?" | `search "X"` (no flag) |

- `--vector` uses **hybrid search** (BM25 keyword + vector semantic + RRF fusion).
  Results include full transcript passages — follow-up reads usually unnecessary.
- Concept search (no flag) matches taxonomy labels/aliases. Fast, no API calls.
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

## Important: These Commands Are Slow

Gemini API calls read video frames and audio — they take **1-5 minutes per video**. A scan of 10 videos can take 10-30 minutes. This is normal.

- **Default log level is `info`** - progress is visible without extra flags.
- **`--dry-run` is preview only** - shows what would be processed but creates no files and makes no Gemini calls. Use it to verify config before committing to a real scan.
- **Use a long bash timeout** (at least 600000ms / 10 minutes) for scan and transcript commands. The default 2-minute timeout WILL kill multi-video scans prematurely.
- **Silence between log lines is normal.** Gemini is processing video - don't diagnose or interrupt.
- **For large scans (10+ videos):** run in the background so the user isn't blocked. Check the output directory afterward for results.
- **For single transcripts:** 1-3 minutes is typical. Wait for the "Saved:" line before proceeding.
- **Transcripts are resilient to malformed JSON.** If Gemini returns broken JSON, the script tries to salvage partial content (speech entries, screen content) and writes a partial transcript with a visible warning. A partial transcript is useful for curiosity/search. For strategically important videos, rerun with `--model gemini-2.5-pro` or retry later.
- **Raw Gemini responses are saved on failure** as `.transcript.raw.txt` sidecars for debugging.

## Model Selection

The default model (`gemini-3-flash-preview` from config.yaml) works for most
operations. Override with `--model` / `-m` when needed:

| Scenario | Model | Why |
|----------|-------|-----|
| Default (mindmaps, concepts, scan) | `gemini-3-flash-preview` | Best deep video understanding |
| Transcripts failing with JSON errors | `gemini-2.5-pro` | More reliable structured JSON, higher output token limit |
| Gemini 3.x backend unreliable / 503s | `gemini-2.5-pro` | Stable fallback |
| Long videos (>60 min transcripts) | `gemini-2.5-pro` | Less likely to truncate mid-output |

```bash
# Override model for a single command
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --model gemini-2.5-pro transcript --url "URL"

# Model for scan (all videos in batch use this model)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --model gemini-2.5-pro scan --channel natebjones
```

Precedence: `--model` flag > `config.yaml` `model` field > `gemini-3-flash-preview`.

## How to Use

### Find videos about a topic (start here)

```bash
# "Which videos cover X?" — concept match, no API calls
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "skills standard"

# "What did someone say about X?" — semantic search over transcripts
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "150-line skill limit" --vector

# Filter either mode to a channel
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "context window" --channel natebjones

# Check corpus status
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" status
```

**Mode reference:**
- **`search "X"`** — concept match. Matches labels/aliases in taxonomy.json.
  Fast, no API calls. Returns video list with artifact paths.
- **`search "X" --vector`** — hybrid search (BM25 + vector + RRF fusion).
  Returns full transcript passages (up to 3000 chars) with speaker turns and
  SCREEN blocks preserved. Searches both video titles and transcript text.
  Requires `VOYAGE_API_KEY`. Add `--preview` for compact 200-char output.
- Hybrid results include evidence directly — follow-up transcript reads are
  usually unnecessary. Only read the source file if you need surrounding context.

### Scan channels for new videos

```bash
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info scan
```

Scans all channels in config.yaml, processes new videos since each channel's
`since` window, saves mind maps and (optionally) transcripts to the output
directory. **This command is slow** — multiple Gemini API calls, 1-5 min each.
Use a 600000ms bash timeout. `--log-level info` is mandatory so progress is
visible; without it the command appears to produce no output.

Options:
- `--since 14d` - Override the time window for this run
- `--channel natebjones` - Scan only this channel
- `--dry-run` - Show what would be processed without calling Gemini
- `--force` - Regenerate even if output files exist

### Transcribe a specific video

```bash
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info transcript \
  --url "https://www.youtube.com/watch?v=XXXXX"
```

Options:
- `--channel natebjones` - Save output under this channel's folder
- `--url` - YouTube URL to transcribe
- `--force` - Regenerate even if transcript exists

### Hybrid search (evidence queries)

```bash
# Build the search index from all transcripts (requires VOYAGE_API_KEY)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" index

# Hybrid search — BM25 keyword + vector semantic, merged by RRF
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "permission problems" --vector

# Filter to a channel
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "150-line skill limit" --vector --channel natebjones
```

Hybrid search requires: `pip install 'video-intel[vector]'` and `VOYAGE_API_KEY`
(free at https://dash.voyageai.com/). See the triage workflow table above for
when to use `--vector` vs plain concept search.

### Extract and normalize concepts

```bash
# Extract concepts from all mindmaps that don't have concepts yet
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info concepts

# Re-extract for a specific channel
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info concepts --channel natebjones --force

# Rebuild master taxonomy from all concept files (fast, no Gemini call)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" taxonomy-build
```

### Manage channels

Edit config.yaml directly or ask Claude Code to add/remove channels.
Claude Code has write access to the config file.

### Configuration

Configuration lives at the plugin root, `${CLAUDE_SKILL_DIR}/../../config.yaml`. Key settings:

```yaml
output_dir: ~/video-intel          # Where output files are saved
default_since: 10d                 # Default lookback window
default_prompt: mindmap-knowledge  # Which prompt to use by default
auto_concepts: true                # Extract concepts after mindmap generation
model: gemini-3-flash-preview     # Gemini model (overridable via --model)

channels:
  - name: natebjones               # Folder name for output
    url: https://youtube.com/@natebjones
    auto_transcript: all            # all | none
    since: 10d                      # Override default lookback

  - name: seankochel               # Selective mode: playlists + keywords
    url: https://youtube.com/@iamseankochel
    playlists:                      # Playlist names (resolved via YouTube API)
      - Agent Skills
    keywords:                       # Channel-scoped search terms
      - ux design
    auto_transcript: all
```

**Selective scanning:** Channels with `playlists` or `keywords` skip the date-based
scan and only process matching videos. Playlist names are resolved via YouTube API
(case-insensitive contains matching). Keywords search the entire channel history
(capped at 200 results per keyword). The `since` field is ignored for selective
channels.

### Prompt files

Prompt templates live at the plugin root, `${CLAUDE_SKILL_DIR}/../../prompts/`:
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
