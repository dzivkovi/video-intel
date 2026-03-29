# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Claude Code skill (open Agent Skills format) that uses Gemini's multimodal API as a proxy to analyze YouTube videos. Gemini sees video frames at 1 FPS, reads on-screen text, and hears audio simultaneously. Claude's role is triage and conversation over the resulting markdown artifacts — it never calls Gemini directly during triage.

## Commands

```bash
# Scan all configured channels (generates mind maps, optionally transcripts)
python scripts/video_intel.py scan

# Scan one channel
python scripts/video_intel.py scan --channel natebjones

# Preview what would be scanned (no API calls)
python scripts/video_intel.py scan --dry-run

# Override lookback window
python scripts/video_intel.py scan --since 30d

# Transcribe a specific video
python scripts/video_intel.py transcript --url "https://www.youtube.com/watch?v=XXXXX" --channel natebjones

# Install dependencies
pip install google-genai google-api-python-client pyyaml
```

Required env vars: `GEMINI_API_KEY`, `YOUTUBE_API_KEY`.

## Architecture

**Skill entry point:** `SKILL.md` — the YAML frontmatter `description` field controls when Claude Code triggers this skill. The body tells Claude how to invoke the scripts and manage config.

**Single script:** `scripts/video_intel.py` — all logic in one file, two subcommands:
- `scan` — uses YouTube Data API to discover new videos per channel, then calls Gemini in parallel (`ThreadPoolExecutor`) to generate mind maps. Optionally chains transcript generation for channels with `auto_transcript: all`.
- `transcript` — calls Gemini with `response_json=True`, parses the three-task JSON response (speech + screen_content + speakers), and merges them into a fused markdown document via `merge_transcript_json()`.

**Prompt templates:** `prompts/*.md` — self-contained, referenced by name (without extension) in `config.yaml`. Three shipped:
- `mindmap-light` — fast scan, 4-6 branches
- `mindmap-heavy` — comprehensive, 6-10 branches with resources/perspectives
- `transcript` — three-task decoupled prompt returning structured JSON

**Config:** `config.yaml` — channels, output directory, model, parallelism, per-channel prompt/since overrides.

**Idempotency:** `is_processed()` checks for existing output files by `{date}-{slug}.{mode}.md` naming. Re-running scan safely skips already-processed videos.

**Output goes to** `~/video-intel/{channel_name}/` (configurable via `output_dir`), not into this repo.

## Key Design Decisions

- Gemini is a multimodal proxy, not a competing assistant. Video understanding requires vision+audio that Claude doesn't have via API.
- The transcript prompt requests structured JSON with three parallel tasks (diarization, screen content, speaker ID). `merge_transcript_json()` fuses them by timestamp sort.
- `SKILL_DIR` is resolved from the script's own path (`Path(__file__).resolve().parent.parent`), making the skill relocatable across `~/.claude/skills/`, `~/.gemini/skills/`, or `~/.agents/skills/`.
- Lazy imports (`require_gemini()`, `require_youtube()`) give clear error messages when dependencies are missing instead of cryptic ImportErrors.

## Packaging

The skill packager has no `.skillignore` or `.dockerignore` equivalent. It hardcodes exclusions for `__pycache__/`, `node_modules/`, `*.pyc`, `.DS_Store`, and `evals/` only. **Everything else in the skill folder gets packaged.**

To package: "Package my video-intel skill" — this validates SKILL.md and produces `video-intel.skill`.

Before packaging, ensure the skill folder contains only shippable files:

- `SKILL.md`, `config.yaml`, `scripts/`, `prompts/`

Dev artifacts that must NOT be present when packaging:

- `.env*`, `.gitignore`, `CLAUDE.md`, `README.md`, output directories (`video-intel/`), `__pycache__/`

If developing in-place (skill folder is also the repo), remove or move dev files before packaging. The `output_dir` in config should point outside the skill folder (e.g. `~/video-intel`) for production use.
