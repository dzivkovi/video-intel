# Video Intel

> **30 seconds to read a mind map vs. 30 minutes to watch the video.**
> Scanned 15 videos from a single channel in under 2 minutes, ~$0.15-0.25 each.
> Free tier covers 8 hours of YouTube video per day.

Multimodal video intelligence powered by Gemini. Scan YouTube channels,
generate thematic mind maps, and produce enriched transcripts that capture
what was said AND what was shown on screen.

## Key Principles

- **Multimodal, not transcript-based.** Gemini sees video frames at 1 FPS,
  reads all on-screen text, and hears audio simultaneously. When a presenter
  says "as you can see here," the output tells you what was actually shown.
- **Decoupled task prompting.** Transcription (audio) and speaker identification
  (vision) run as separate tasks within a single prompt to preserve attention
  quality, borrowed from Laurent Picard's research.
- **Scan-then-triage funnel.** Mind maps are cheap and fast. Read 30-second
  summaries, then spend transcript budget only on videos worth deep engagement.
- **Idempotent processing.** Re-running a scan skips already-processed videos.
  Safe to interrupt, safe to re-run.

## How It Works

```text
YouTube Data API          Gemini Multimodal API         Your filesystem
───────────────          ──────────────────────         ───────────────
Discover new videos  →   Watch frames + audio      →   mindmap.md
(per-channel config)     Generate mind maps (parallel)  meta.json

                         [optional, per-channel]
                         Fuse speech + screen content → transcript.md
                         (three-task decoupled prompt)
```

**scan** - Fetch new videos from configured channels, generate thematic mind
maps in parallel via Gemini. Optionally auto-generate transcripts for channels
where you want everything.

**transcript** - Fused document for a single video: diarized speech interleaved
with timestamped SCREEN sections describing slides, diagrams, code, and demos.
Speaker names identified from visual cues with evidence.

**triage** - After scanning, ask Claude (no Gemini cost):

> "Read the mind maps in ~/video-intel/natebjones/ from this week and tell me
> which videos are worth watching for agentic AI patterns."

## Quick Start

```bash
# 1. API keys (both free)
export GEMINI_API_KEY=your_key    # https://aistudio.google.com/apikey
export YOUTUBE_API_KEY=your_key   # https://console.cloud.google.com/apis/credentials

# 2. Dependencies
pip install google-genai google-api-python-client pyyaml

# 3. Install (pick your platform)
cp -r video-intel ~/.claude/skills/    # Claude Code
cp -r video-intel ~/.gemini/skills/    # Gemini CLI
cp -r video-intel ~/.agents/skills/    # Cross-platform

# 4. Configure channels in config.yaml, then:
python scripts/video_intel.py scan
```

Or in Claude Code, just say: **"scan my channels"**

## Configuration

### config.yaml

```yaml
output_dir: ~/video-intel
default_since: 10d
default_prompt: mindmap-light
model: gemini-3-flash-preview
max_parallel: 5

channels:
  - name: natebjones
    url: https://youtube.com/@natebjones
    prompt: mindmap-light
    auto_transcript: all
    since: 10d
```

| Field | Default | Description |
| ----- | ------- | ----------- |
| output_dir | ~/video-intel | Where output files are saved |
| default_since | 10d | Default lookback window |
| default_prompt | mindmap-light | Default prompt for mind maps |
| model | gemini-3-flash-preview | Gemini model to use |
| max_parallel | 5 | Concurrent Gemini requests |

### Channel Settings

| Field | Required | Description |
| ----- | -------- | ----------- |
| name | Yes | Folder name and identifier |
| url | Yes | YouTube channel URL |
| prompt | No | Override default prompt |
| auto_transcript | No | "all" or "none" (default: none) |
| since | No | Override default lookback window |

### Since Formats

- Relative: `7d`, `10d`, `30d`, `120d`
- Absolute: `2026-01-15`
- Command-line `--since` overrides per-channel and default settings

## Usage

```bash
# Scan all configured channels
python scripts/video_intel.py scan

# Scan one channel
python scripts/video_intel.py scan --channel natebjones

# Override lookback window
python scripts/video_intel.py scan --since 30d

# Preview what would be scanned (no API calls)
python scripts/video_intel.py scan --dry-run

# Transcribe a specific video
python scripts/video_intel.py transcript \
  --url "https://www.youtube.com/watch?v=XXXXX" \
  --channel natebjones
```

## Prompt Customization

Prompts live in `prompts/`. Each file is self-contained.

| File | Purpose |
| ---- | ------- |
| mindmap-light.md | Fast thematic scan (4-6 branches, tight bullets) |
| mindmap-heavy.md | Comprehensive extraction (6-10 branches, resources, perspectives) |
| transcript.md | Three-task diarized transcript with screen content |

Add a `.md` file to `prompts/` and reference it in config.yaml by filename
(without extension).

## Output

```text
~/video-intel/
├── natebjones/
│   ├── 2026-03-20-building-mcp-agents.mindmap.md
│   ├── 2026-03-20-building-mcp-agents.transcript.md
│   ├── 2026-03-20-building-mcp-agents.meta.json
│   └── ...
```

- **mindmap.md** - Thematic mind map with timestamps. Obsidian-compatible.
- **transcript.md** - Fused diarized transcript with SCREEN sections.
- **meta.json** - Video metadata, source URL, processing history.

## Cost

Using Gemini 3 Flash ($0.50/M input tokens, $1.00/M audio, $3.00/M output):

| Operation | Typical Cost |
| --------- | ----------- |
| Mind map for a 15-min video | ~$0.15-0.25 |
| Mind map for a 45-min video | ~$0.40-0.60 |
| Full transcript for a 30-min video | ~$0.50-0.80 |
| Weekly scan of 5 channels (30 videos) | ~$5-10 |
| Batch API (async, 50% discount) | Half the above |

Free tier: 8 hours of YouTube video per day at no cost.

## Cross-Platform Compatibility

This skill uses the open Agent Skills format (SKILL.md + scripts/).
It works with Claude Code, Gemini CLI, Cursor, GitHub Copilot, and others
supporting the skills spec. API keys are read from environment variables.

## Packaging

To package for distribution, tell Claude Code: "Package my video-intel skill."

## Design Influences & Sources

| What shaped it | Source | Key takeaway |
| -------------- | ------ | ------------ |
| Decoupled task prompting | Laurent Picard ([TDS](https://towardsdatascience.com/unlocking-multimodal-video-transcription-with-gemini/), [GCC](https://medium.com/@PicardParis/unlocking-multimodal-video-transcription-with-gemini-part4-3381b61aaaec)) | Split transcription from speaker ID to preserve attention quality |
| Speaker evidence | Philipp Schmid ([gemini-samples](https://github.com/philschmid/gemini-samples/blob/main/examples/gemini-analyze-transcribe-youtube.ipynb)) | Pre-seed names, require visual evidence for each ID |
| Diarization strategy | Google Cloud ([partner blog](https://cloud.google.com/blog/topics/partners/how-partners-unlock-scalable-audio-transcription-with-gemini/)) | Zero-shot for transcription, few-shot for diarization |
| API patterns | Google ([video](https://ai.google.dev/gemini-api/docs/video-understanding) & [audio](https://ai.google.dev/gemini-api/docs/audio) docs) | Token economics, context caching, multimodal config |
| Gemini vs Whisper | [Voice Writer](https://voicewriter.io/blog/best-speech-recognition-api-2025), [Brown CCV](https://docs.ccv.brown.edu/ai-tools/services/transcribe/comparing-speech-to-text-models) | Single-model Gemini beats multi-model Whisper + pyannote pipeline |
| Skills ecosystem | Mark Kashef, [Early AI-dopters](https://www.skool.com/earlyaidopters) community | Pointed to Google's [gemini-skills](https://github.com/google-gemini/gemini-skills) repo; built on the open cross-platform [Agent Skills format](https://code.claude.com/docs/en/skills) |

Co-designed iteratively with Claude (Anthropic) across a single session
spanning use case discovery, research synthesis, and build.

## License

MIT
