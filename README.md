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

The architecture is a narrowing funnel - like fishing, where you look for
birds before you cast a line and read the water before you commit to a spot.

```text
┌─────────────────────────────────────────────────────────────────┐
│  SCAN (the birds)                          Cost: ~$0.20/video   │
│  ┌───────────────┐    ┌───────────────────┐    ┌─────────────┐  │
│  │ YouTube Data  │───>│ Gemini Multimodal │───>│ mindmap.md  │  │
│  │ API: discover │    │ API: watch frames │    │ meta.json   │  │
│  │ new videos    │    │ + audio (parallel)│    │ per video   │  │
│  └───────────────┘    └───────────────────┘    └─────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│  TRIAGE (the drop-off)                     Cost: $0 (no API)    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ You + Claude read mind maps. No Gemini needed.           │   │
│  │ "Which of these 15 videos matter for agentic patterns?"  │   │
│  └──────────────────────────────────────────────────────────┘   │
├─────────────────────────────────────────────────────────────────┤
│  TRANSCRIPT (the catch)                    Cost: ~$0.50/video   │
│  ┌────────────────────┐    ┌─────────────────────────────────┐  │
│  │ Gemini: 3-task     │───>│ transcript.md                   │  │
│  │ decoupled prompt   │    │ Diarized speech interleaved     │  │
│  │ (audio + vision +  │    │ with SCREEN sections describing │  │
│  │  speaker ID)       │    │ slides, diagrams, code, demos   │  │
│  └────────────────────┘    └─────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
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

## What a Fused Transcript Looks Like

Traditional transcripts lose everything visual. When a presenter says "as you
can see here," you see nothing. The fused transcript captures both channels:

```text
[01:09] Ray (Developer and Instructor): "But then this introduced
a brand new problem whereby in session one you would have a pretty
fresh, clean, and relevant memory. And then as you go on, you would
notice that Claude Code decides to add more and more stuff to its
memory and you get noise and contradictions and stuff like that."

  SCREEN [01:09-01:31] [diagram]: Excalidraw diagram titled
  'THE PROBLEM WITH AI MEMORY', illustrating how memory accumulates
  noise and contradictions over multiple sessions (Session 1 to
  Session 20).

[01:32] Ray (Developer and Instructor): "And Claude did have some
instructions in the system prompt telling it to verify that the
memory is still correct and up-to-date, but that didn't really
do a good job."
```

This is real output from scanning [Ray Amjad's](https://youtube.com/@ramjad)
channel. Speaker names are identified from visual cues (Zoom labels, name
cards, badges, slide footers) with evidence provided for each identification.

## Why This Architecture

- **Gemini watches, Claude thinks.** Best tool for each job, not competing models.
- **Mind maps first, transcripts second.** 30-second scan before 15-minute commitment.
- **Three decoupled tasks, not one prompt.** Tokens compete for attention - split audio, vision, and speaker ID for better quality.
- **One model replaces four.** Gemini Flash 3.x does what Whisper + pyannote + Claude + Gemini did separately - and captures visual content they never could.
- **Prompts are fuel, the skill is the engine.** External, self-contained, swappable. No hidden prefix assembly.
- **Per-channel config.** Daily creators get `since: 10d`. Monthly creators get `since: 120d`. Each channel captures your relationship with that creator.
- **The skill only does Gemini work.** Triage and deep-dive are conversations with Claude, not API calls. They were in the design, then deliberately cut.

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

For detailed installation across all platforms (Gemini CLI, Cursor, Copilot,
Codex, Databricks, NPX, symlinks), see
[INSTALLATION.md](INSTALLATION.md). Tested locally with Claude Code -
other platforms follow the same Agent Skills spec but are not yet validated.
Community feedback welcome.

## Configuration

### config.yaml

```yaml
output_dir: ~/video-intel
default_since: 10d
default_prompt: mindmap-light
model: gemini-3-flash-preview
max_parallel: 10

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
| max_parallel | 10 | Concurrent Gemini requests (paid tier can go 50+) |

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

# Transcribe a specific video (channel auto-detected from config)
python scripts/video_intel.py transcript \
  --url "https://www.youtube.com/watch?v=XXXXX"
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
| Full transcript for a 30-min video | ~$0.30-0.50 |
| Weekly scan of 5 channels (30 videos) | ~$5-10 |
| Batch API (async, 50% discount) | Half the above |

**Free tier** covers 8 hours of input video per day. When active, input tokens
cost nothing and output tokens ($3/M) become nearly the entire bill — about
$0.05 per video. Steady-state weekly scans of 30 videos fit comfortably within
the daily free quota.

**First-run backfill:** If you configure channels with long lookback windows
(e.g., `since: 90d`), the first scan processes every video in that window.
Start with `--dry-run` to preview volume, or use short `since` values and
widen them gradually.

**Rate limits:** Free tier has lower requests-per-minute limits. The script
retries automatically with backoff on 429 errors, but if you hit throttling,
reduce `max_parallel` in config.yaml (try 3-5). Paid tier users have
generous limits (20,000+ RPM) and can increase parallelism freely. Check
your limits at [Google AI Studio](https://aistudio.google.com/apikey) or
the [rate limits docs](https://ai.google.dev/gemini-api/docs/rate-limits).

## Cross-Platform Compatibility

This skill uses the open Agent Skills format (SKILL.md + scripts/).
It works with Claude Code, Gemini CLI, Cursor, GitHub Copilot, and others
supporting the skills spec. API keys are read from environment variables.

## Packaging

To package for distribution, tell Claude Code: "Package my video-intel skill."

## Design Influences & Sources

[Gemini API Development Skill](https://github.com/google-gemini/gemini-skills/blob/main/skills/gemini-api-dev/SKILL.md)
is a knowledge skill - it gives coding agents correct model names and
SDK patterns so they write working Gemini code. It doesn't watch videos. The
video-watching capability is built into Gemini itself. Video-intel is the
execution skill that wraps that capability: you say "scan my channels" and
it calls the API, produces mind maps, saves files. Google published the
cookbook. This is the kitchen.

| What shaped it | Source | Key takeaway |
| -------------- | ------ | ------------ |
| Decoupled task prompting | Laurent Picard ([TDS](https://towardsdatascience.com/unlocking-multimodal-video-transcription-with-gemini/), [GCC](https://medium.com/@PicardParis/unlocking-multimodal-video-transcription-with-gemini-part4-3381b61aaaec)) | Split transcription from speaker ID to preserve attention quality |
| Speaker evidence | Philipp Schmid ([gemini-samples](https://github.com/philschmid/gemini-samples/blob/main/examples/gemini-analyze-transcribe-youtube.ipynb)) | Pre-seed names, require visual evidence for each ID |
| Diarization strategy | Google Cloud ([partner blog](https://cloud.google.com/blog/topics/partners/how-partners-unlock-scalable-audio-transcription-with-gemini/)) | Zero-shot for transcription, few-shot for diarization |
| API patterns | Google ([video](https://ai.google.dev/gemini-api/docs/video-understanding) & [audio](https://ai.google.dev/gemini-api/docs/audio) docs) | Token economics, context caching, multimodal config |
| Gemini vs Whisper | [Voice Writer](https://voicewriter.io/blog/best-speech-recognition-api-2025), [Brown CCV](https://docs.ccv.brown.edu/ai-tools/services/transcribe/comparing-speech-to-text-models) | Single-model Gemini beats multi-model Whisper + pyannote pipeline |
| Skills ecosystem | Mark Kashef, [Early AI-dopters](https://www.skool.com/earlyaidopters) community | Pointed to Google's [gemini-skills](https://github.com/google-gemini/gemini-skills) repo; built on the open cross-platform [Agent Skills format](https://code.claude.com/docs/en/skills) |

Architected through iterative conversation with [Claude Desktop](https://claude.ai/) -
from use case discovery through research synthesis to working prototype.
Engineered and shipped in [Claude Code](https://claude.ai/code).

## License

MIT
