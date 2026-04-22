---
name: video-intel
description: >
  Multimodal video intelligence via Gemini API. Use whenever the user wants
  to: find videos about a topic across channels; browse concepts in the
  library; scan a YouTube channel for new videos and get mind maps; backfill
  a creator's backlog or catch up on missing videos; triage which videos are
  worth watching; get a diarized transcript with on-screen content (slides,
  diagrams, code) captured; add/remove monitored channels; change scan
  settings. Trigger phrases: "what videos cover [topic]", "find videos
  about [concept]", "which creators talk about [subject]", "scan channel",
  "what's new from [creator]", "last N days of [creator]", "recent
  takeaways from [creator]", "watch this for me", "transcribe this
  video", "transcribe [creator]'s backlog", "videos I'm missing from
  [creator]", "catch up on [creator]", "fully scan [creator]", "backfill
  [creator]", "add [channel] to my watchlist", "what should I watch",
  "summarize this video", "is this worth watching", any YouTube URL +
  question, "show my channels", "what concepts are in my library", "what
  topics recur across channels", "nugget brief on [topic]", "consultant
  brief on [topic]", "what do [creators] say about [topic]", "agreements
  and disagreements on [topic]", "synthesize insights across creators",
  "mental models across creators", "find the nuggets about [topic]".
  Calls Gemini as multimodal proxy (frames +
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
| **Synthesis** (what do creators say, together) | "nugget brief on X", "what do creators agree/disagree about Y?", "consultant brief" | `nugget "X"` |

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

## Interpreting User Intent

The verb a user reaches for doesn't always match a CLI command name. This
table is the canonical mapping — read it before picking a command.

| User says (intent) | Run | Notes |
|---|---|---|
| "find videos about X", "what covers Y" | `search "X"` | Discovery — fast, no API calls |
| "what did they say about X", "evidence for Y" | `search "X" --vector` | Hybrid search; returns transcript passages |
| "recent tips / takeaways from [creator]", "last N days of [creator]" | `search "X" --vector --channel Y --since Nd` | Query existing index over a date window; no Gemini calls |
| "nugget brief on X", "consultant brief on X", "what do creators say about X (together)", "agreements and disagreements on X", "find the nuggets" | `nugget "X"` | Cross-creator synthesis; retrieves top-K excerpts and feeds consultant-grade prompt for attributed briefing with 1+1=3 emergent insights |
| "transcribe this video" + URL | `transcript --url URL` | Single video |
| "scan", "what's new", "check for new videos" | `scan` | All channels, configured `since` |
| "what's new from [creator]" | `scan --channel X` | Single channel, configured `since` |
| "transcribe [creator]'s backlog", "videos I'm missing from [creator]", "catch up on [creator]" | `scan --channel X --since 2y` (or wider) | **Always `--dry-run` first** to surface scope |
| "fully scan [creator]", "everything from [creator]" | `scan --channel X --since 2005-01-01` | **Always `--dry-run` first** — implies entire channel history |
| Backlog of N videos to transcribe | `scan` with `auto_transcript: all` configured | NOT N separate `transcript --url` calls |

### Channel name resolution

When the user names a creator (e.g. "Grace Leung", "Nate Jones"):

1. Read `${CLAUDE_SKILL_DIR}/../../config.yaml` and match the name
   case-insensitively against both the `name` field and the handle in `url`.
2. If exactly one match — use that channel's `name`.
3. If multiple matches — list them and ask which one.
4. If zero matches — list available channels and ask whether to add the
   creator. **Do not invent a YouTube handle and proceed.**

### When to pause and confirm

Before running `scan` (which costs Gemini quota), run `--dry-run` first if
any of these are true:

- The user said "all", "fully", "missing", "backlog", or "catch up"
  without an explicit date window
- The implied scope is more than ~10 new videos
- The channel name was fuzzy and required config-file resolution
- `auto_transcript: all` is set on the target channel (each new video =
  2 Gemini calls — mindmap + transcript)

Report the count of new videos and the estimated Gemini call count
(videos × 2 if `auto_transcript: all`, otherwise videos × 1). Wait for
the user's go-ahead before running the real scan.

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

## Natural-Language Content Queries

When you ask Claude Code about topics in your video library, the skill routes
your question through grounded search automatically — you don't need to know
whether to use vector, hybrid, or concept search internally. Here's what happens:

**Default behavior:** Your question is routed to **hybrid search** (BM25 keyword +
vector semantic embedding via Voyage AI + rank-reciprocal fusion). This finds
passages in transcripts that are both keyword-relevant and semantically similar
to your query, ranked by combined score.

**Output shape:** Results come back as a **narrative summary** with:
- A thematic headline (the cross-cutting thread)
- One paragraph per video (what was said and in what context)
- **Jump links with timestamps** (e.g. `[2:45](https://youtube.com/watch?v=xxx&t=165)`)
  pointing to the exact moment the evidence was found — no scrubbing, no watching
  the full video

**Date-window queries:** For "last N days" questions, add `--since 30d` (or any
`Nd` / `YYYY-MM-DD` value). The filter is pushed into LanceDB *before* ranking,
so every recent video is considered — recency doesn't get crowded out by older,
higher-relevance hits. Use this by default for `last N days of [creator]` questions.

**Fallback:** If vector search is unavailable (missing `VOYAGE_API_KEY` or index
not built), the skill falls back to concept search (fast, no API calls). Results
are video matches only; open the transcript file afterward to read detail.

**See also:** [ADR-0013](../../../docs/adr/ADR-0013-hybrid-search-rrf.md) (hybrid
search design), [ADR-0016](../../../docs/adr/ADR-0016-vector-db-path-config.md)
(vector index configuration).

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

**YouTube URL:**

```bash
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info transcript \
  --url "https://www.youtube.com/watch?v=XXXXX"
```

**Local MP4 file** (works for screen recordings, meetings, Dropbox/GDrive sync folders):

```bash
# Full file (<500MB)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info transcript \
  --file ~/Videos/meeting.mp4

# Specific segment (required for files >500MB)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info transcript \
  --file ~/Videos/meeting.mp4 --start 05:30 --end 18:45
```

Local files produce `{name}.transcript.md` and `{name}.meta.json` in the same
directory as the source by default. Uploaded files auto-expire from Gemini
after 48 hours.

**Members-only / gated video recovery** (when Gemini cannot fetch a YouTube URL
because it is member-only, paid, or otherwise restricted and `scan` returned
HTTP 403 PERMISSION_DENIED):

1. Download the video locally. The simplest workflow is to save it directly
   under `output_dir/<channel>/` (e.g., `./video-intel/everyinc/Compound Engineering Camp.mkv`);
   the tool infers the channel from the parent folder. `.mkv`, `.mp4`, `.mov`,
   `.webm`, and `.avi` are all accepted.
2. Run `mindmap --file` then `transcript --file` on the local path. The
   artifacts land in the canonical channel folder using the same `.meta.json`
   shape as scan-generated ones so the concepts/search pipelines pick them
   up on the next `scan` without special-casing.

```bash
# Drop the MP4 under video-intel/everyinc/ first, then:
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" mindmap \
  --file "./video-intel/everyinc/Compound Engineering Camp.mkv"
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" transcript \
  --file "./video-intel/everyinc/Compound Engineering Camp.mkv"

# Or keep the MP4 elsewhere and pass --channel explicitly:
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" transcript \
  --file "~/Downloads/lfML5OJc-CM.mp4" --channel everyinc
```

When the local filename is `<videoId>.mp4` (11-char YouTube ID), the tool
matches it against an existing canonical scan-generated `.meta.json` in the
channel folder and writes artifacts under the canonical `{YYYY-MM-DD}-{slug}`
prefix, keeping a single meta.json per video. Otherwise the filename stem
is used as both the title and the artifact prefix.

Options:
- `--url` - YouTube URL to transcribe (mutually exclusive with `--file`)
- `--file` - Path to local MP4 / MKV / MOV / WebM / AVI (mutually exclusive with `--url`)
- `--start`/`--end` - Segment time offsets (accepts `MM:SS`, `HH:MM:SS`, or raw seconds)
- `--channel <NAME>` - Save output under this channel's folder; with `--file`, enables in-place recovery routing
- `--video-id <ID>` - 11-char YouTube video ID for explicit canonical-meta matching
- `--title <T>` / `--date YYYY-MM-DD` - Override filename-inferred defaults
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

### Synthesize a consultant-grade nugget brief (cross-creator)

```bash
# Ask "what do creators say about X, together?"
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "LightRAG vs OpenBrain architectural tension"

# Restrict to recent coverage
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "context engineering" --since 90d

# Restrict to specific creators
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "graph RAG" --channel engineerprompt

# Save the briefing to a file
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "second brain patterns" --output brief.md
```

The `nugget` command retrieves top-K excerpts via hybrid search, then feeds them through the `nugget-brief` prompt for a consultant-grade briefing. Output structure: Query in Focus → Creators Surveyed → Consensus → Divergence → Noteworthy Nuggets (mental models, metaphors, warnings, workarounds, business psychology) → Emergent Synthesis (1+1=3) → Follow-Up Questions. Every claim cites the creator and timestamp.

Options:
- `--limit N` - Max excerpts feeding synthesis (default: 15)
- `--channel X` - Restrict to one creator
- `--since Nd` - Time-window filter ('Nd' or 'YYYY-MM-DD')
- `--min-relevance F` - Minimum RRF relevance score
- `--no-expand` - Disable Stage-1 taxonomy query expansion
- `--output PATH` - Write briefing to file instead of stdout

Use when the user wants **grounded, multi-creator, citable analysis** — not a single-video summary and not raw evidence-search results. This is the "what do they say together, and what emerges from comparing them" mode.

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
    auto_transcript: none             # mindmaps for discovery, transcript manually
    since: 30d                        # also catch recent uploads (additive)
```

**Selective scanning:** Channels with `playlists` or `keywords` target specific
content instead of scanning all uploads. Playlist names are resolved via YouTube API
(case-insensitive contains matching). Keywords search the entire channel history
(capped at 200 results per keyword). If `since` is also set, recent uploads are
fetched as an additional source alongside playlists/keywords.

### Prompt files

Prompt templates live at the plugin root, `${CLAUDE_SKILL_DIR}/../../prompts/`:
- `mindmap-knowledge.md` - Thematic mind map with domain terminology + timestamps (default)
- `mindmap-light.md` - Fast thematic scan (4-6 branches)
- `mindmap-heavy.md` - Comprehensive conceptual extraction
- `transcript.md` - Full diarized transcript with screen content
- `concepts.md` - Concept extraction + normalization against taxonomy
- `nugget-brief.md` - Consultant-grade cross-creator synthesis with attributed nuggets

Each prompt is self-contained. Users can modify or add their own.

## Evaluate Search Quality

The repo ships a 25-query grounded golden dataset at `tests/evals/golden_dataset.yaml` that measures hybrid search against known-correct transcript passages. Run this before/after any change that touches retrieval (search ranking, chunking, KB layer, concept normalization) and record the before/after score in the PR description.

```bash
# Full run — ~1 min wall-clock, ~$0.01-0.05 in Voyage tokens
pytest tests/evals/ -v -s

# Smoke mode (Q01 only) — ~3 seconds, for iterating on the harness itself
VIDEO_INTEL_EVAL_SMOKE=1 pytest tests/evals/ -v -s
```

The `-s` flag matters — the per-metric diagnostics are what tell you *why* a query failed, not just that it did. See `tests/evals/README.md` for the quick-start and `docs/testing.md` / `docs/adr/ADR-0017-kb-layer-strategy.md` for the baseline (1/25 as of 2026-04-19) and the staged-KB plan this eval gates.

The golden dataset is a frozen contract per ADR-0017 — changing queries needs ADR-grade justification, not a silent edit.

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
