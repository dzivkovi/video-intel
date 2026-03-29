# Video Intel

Multimodal video intelligence powered by Gemini. Scan YouTube channels,
generate thematic mind maps, and produce enriched transcripts that capture
what was said AND what was shown on screen.

## Why This Exists

You follow creators who publish faster than you can watch. You attend meetings
where critical information lives in screen shares that transcripts drop.
You need to know what matters without watching everything.

Video Intel solves this with a two-stage funnel:

1. **Scan** - Gemini watches each video (frames + audio + on-screen text)
   and produces a thematic mind map grouped by concept with timestamps.
   You read the mind map in 30 seconds instead of watching for 30 minutes.

2. **Transcript** - For videos worth deeper engagement, Gemini produces a
   fused document: diarized speech interleaved with SCREEN sections that
   describe slides, diagrams, code, and demos shown on screen. Speaker
   names are identified from visual cues (name cards, Zoom labels, badges)
   with evidence provided.

This is NOT transcript-based analysis. Gemini sees the video frames at
1 FPS, reads all on-screen text, and hears the audio simultaneously.
When a presenter says "as you can see here," the output tells you what
was actually shown.

## Quick Start

### 1. Get API Keys

- **Gemini API Key** (free): https://aistudio.google.com/apikey
- **YouTube Data API Key** (free): https://console.cloud.google.com/apis/credentials
  (enable "YouTube Data API v3")

### 2. Set Environment Variables

```bash
export GEMINI_API_KEY=your_gemini_key
export YOUTUBE_API_KEY=your_youtube_key
```

Add these to your `.zshrc` or `.bashrc` for persistence.

### 3. Install Dependencies

```bash
pip install google-genai google-api-python-client pyyaml
```

### 4. Install the Skill

**Claude Code:**
```bash
cp -r video-intel ~/.claude/skills/
```

**Gemini CLI:**
```bash
cp -r video-intel ~/.gemini/skills/
```

**Cross-platform (.agents convention):**
```bash
cp -r video-intel ~/.agents/skills/
```

### 5. Configure Your Channels

Edit `config.yaml` inside the skill folder:

```yaml
output_dir: ~/video-intel
default_since: 10d
default_prompt: mindmap-light

channels:
  - name: natebjones
    url: https://youtube.com/@natebjones
    prompt: mindmap-light
    auto_transcript: all
    since: 10d
```

### 6. Run Your First Scan

```bash
python ~/.claude/skills/video-intel/scripts/video_intel.py scan
```

Or in Claude Code, just say: **"scan my channels"**

## Usage

### Scanning Channels

```bash
# Scan all configured channels
python scripts/video_intel.py scan

# Scan one channel
python scripts/video_intel.py scan --channel natebjones

# Override lookback window
python scripts/video_intel.py scan --since 30d

# Preview what would be scanned (no API calls)
python scripts/video_intel.py scan --dry-run
```

### Transcribing a Specific Video

```bash
python scripts/video_intel.py transcript \
  --url "https://www.youtube.com/watch?v=XXXXX" \
  --channel natebjones
```

### Triage (No Script Needed)

After scanning, open Claude and say:

> "Read the mind maps in ~/video-intel/natebjones/ from this week
> and tell me which videos are worth watching for someone focused
> on agentic AI patterns and enterprise architecture."

Claude reads the markdown files and gives you a ranked briefing.
No Gemini cost for this step.

## Configuration Reference

### config.yaml

| Field | Default | Description |
|-------|---------|-------------|
| output_dir | ~/video-intel | Where output files are saved |
| default_since | 10d | Default lookback window |
| default_prompt | mindmap-light | Default prompt for mind maps |
| model | gemini-3-flash-preview | Gemini model to use |
| max_parallel | 5 | Concurrent Gemini requests |

### Channel Settings

| Field | Required | Description |
|-------|----------|-------------|
| name | Yes | Folder name and identifier |
| url | Yes | YouTube channel URL |
| prompt | No | Override default prompt |
| auto_transcript | No | "all" or "none" (default: none) |
| since | No | Override default lookback window |

### Since Formats

- Relative: `7d`, `10d`, `30d`, `120d`
- Absolute: `2026-01-15`
- Command-line `--since` overrides per-channel and default settings

## Prompt Customization

Prompts live in the `prompts/` folder. Each file is self-contained.

| File | Purpose |
|------|---------|
| mindmap-light.md | Fast thematic scan (4-6 branches, tight bullets) |
| mindmap-heavy.md | Comprehensive extraction (6-10 branches, resources, perspectives) |
| transcript.md | Three-task diarized transcript with screen content |

To create your own prompt, add a `.md` file to `prompts/` and reference it
in config.yaml by filename (without extension).

## Output Files

```
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

Re-running a scan skips already-processed videos (idempotent).

## Cost Estimates

Using Gemini 3 Flash ($0.50/M input tokens, $1.00/M audio, $3.00/M output):

| Operation | Typical Cost |
|-----------|-------------|
| Mind map for a 15-min video | ~$0.15-0.25 |
| Mind map for a 45-min video | ~$0.40-0.60 |
| Full transcript for a 30-min video | ~$0.50-0.80 |
| Weekly scan of 5 channels (30 videos) | ~$5-10 |
| Batch API (async, 50% discount) | Half the above |

Free tier: 8 hours of YouTube video per day at no cost.

## Cross-Platform Compatibility

This skill uses the open Agent Skills format (SKILL.md + scripts/).
It works with:

- Claude Code (`~/.claude/skills/`)
- Gemini CLI (`~/.gemini/skills/`)
- Cross-platform agents (`~/.agents/skills/`)
- Cursor, GitHub Copilot, and others supporting the skills spec

API keys are read from environment variables. No platform-specific
configuration needed.

## Packaging

To package for distribution, tell Claude Code: "Package my video-intel skill."

## Design Influences & Sources

This skill's architecture was shaped by research into Gemini's multimodal
video capabilities and community best practices for prompting.

**Multimodal transcription methodology:**
Laurent Picard's series on multimodal video transcription
([Towards Data Science](https://towardsdatascience.com/unlocking-multimodal-video-transcription-with-gemini/),
[Google Cloud Community](https://medium.com/@PicardParis/unlocking-multimodal-video-transcription-with-gemini-part4-3381b61aaaec))
introduced the decoupled task pattern: splitting transcription (audio-focused)
from speaker identification (vision-focused) into separate tasks within a
single prompt to preserve attention quality.

**Speaker identification with visual evidence:**
Philipp Schmid's [gemini-samples](https://github.com/philschmid/gemini-samples/blob/main/examples/gemini-analyze-transcribe-youtube.ipynb) notebook demonstrated pre-seeding speaker names and requesting evidence for each identification.

**Diarization prompting:**
Google Cloud's [partner engineering blog](https://cloud.google.com/blog/topics/partners/how-partners-unlock-scalable-audio-transcription-with-gemini/) recommended zero-shot prompts for transcription and few-shot examples for diarization.

**Video understanding capabilities:**
Google's official [video understanding](https://ai.google.dev/gemini-api/docs/video-understanding) and [audio understanding](https://ai.google.dev/gemini-api/docs/audio) documentation provided the API patterns, token economics, and context caching strategies.

**Gemini vs Whisper benchmarking:**
[Voice Writer's ASR benchmark](https://voicewriter.io/blog/best-speech-recognition-api-2025) (January 2025) and Brown University's [CCV AI transcription comparison](https://docs.ccv.brown.edu/ai-tools/services/transcribe/comparing-speech-to-text-models) informed the decision to use Gemini for both transcription and diarization rather than a multi-model Whisper + pyannote pipeline.

**Skills ecosystem:**
Inspired by Mark Kashef (Early AI-dopters community) sharing Google's [gemini-skills](https://github.com/google-gemini/gemini-skills) repo, and built using the open [Agent Skills format](https://code.claude.com/docs/en/skills) for cross-platform compatibility.

**Co-design process:**
Architecture, prompts, and implementation co-designed iteratively with Claude (Anthropic) across a single session spanning use case discovery, research synthesis, and build.

## License

MIT
