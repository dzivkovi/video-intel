# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## System Instructions

Before executing any task, you MUST read and strictly adhere to the constraints defined in `specs/agent-rules.md`.

## Backlog

Use GitHub Issues for feature requests and bugs. Do not create file-based todos.

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

# Transcribe a specific video (channel auto-detected from config)
python scripts/video_intel.py transcript --url "https://www.youtube.com/watch?v=XXXXX"

# Install dependencies
pip install google-genai google-api-python-client pyyaml
```

Required env vars: `GEMINI_API_KEY`, `YOUTUBE_API_KEY`.

## Architecture

**Skill entry point:** `SKILL.md` — the YAML frontmatter `description` field controls when Claude Code triggers this skill. The body tells Claude how to invoke the scripts and manage config.

**Single script:** `scripts/video_intel.py` — all logic in one file, subcommands:
- `scan` — uses YouTube Data API to discover new videos per channel, then calls Gemini in parallel (`ThreadPoolExecutor`) to generate mind maps. Optionally chains transcript and concept generation.
- `transcript` — calls Gemini with `response_json=True`, parses the three-task JSON response (speech + screen_content + speakers), and merges them into a fused markdown document via `merge_transcript_json()`.
- `mindmap` — generate a mind map for a single video URL with a specific prompt.
- `concepts` — extract and normalize concepts from existing mindmaps against a growing canonical vocabulary (thesaurus). Text-only Gemini calls reading mindmap markdown, not video.
- `taxonomy-build` — rebuild `taxonomy.json` by aggregating all per-video `concepts.json` files. This is a derived artifact, always rebuildable.

All commands support `--force` to regenerate existing output files.

**Prompt templates:** `prompts/*.md` — self-contained, referenced by name (without extension) in `config.yaml`:
- `mindmap-knowledge` — thematic mind map with domain terminology + timestamps (default)
- `mindmap-light` — fast scan, 4-6 branches
- `mindmap-heavy` — comprehensive, 6-10 branches with resources/perspectives
- `transcript` — three-task decoupled prompt returning structured JSON
- `concepts` — concept extraction + normalization against taxonomy, with `{{taxonomy}}` template slot

**Config:** `config.yaml` — channels, output directory, model, parallelism, per-channel prompt/since overrides.

**Idempotency:** `is_processed()` checks for existing output files by `{date}-{slug}.{mode}.md` naming. Re-running scan safely skips already-processed videos. All commands support `--force` to regenerate.

**Output goes to** `~/video-intel/{channel_name}/` (configurable via `output_dir`), not into this repo. Master `taxonomy.json` lives at the output root.

**Concept layer:** Per-video `concepts.json` is the source of truth. `taxonomy.json` is derived (rebuilt by `taxonomy-build`). During batch extraction, new concepts accumulate in memory so each video normalizes against concepts discovered in earlier videos. See ADR-0010.

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

If developing in-place (skill folder is also the repo), package from a clean temp copy:

```bash
# Create clean copy with only shippable files
mkdir -p /tmp/video-intel-clean
cp SKILL.md config.yaml README.md /tmp/video-intel-clean/
cp -r scripts prompts /tmp/video-intel-clean/

# Package from clean copy (run from skill-creator directory)
cd ~/.claude/skills/skill-creator
python -m scripts.package_skill /tmp/video-intel-clean
```

The `output_dir` in config should point outside the skill folder (e.g. `~/video-intel`) for production use.

## Release Process

1. Commit changes and tag: `git tag -a v1.x.0 -m "description"`
2. Package the skill (see Packaging above)
3. Copy `.skill` file to project: `cp ~/.claude/skills/skill-creator/video-intel-clean.skill ./video-intel.skill`
4. Push commits and tag: `git push origin main --tags`
5. Create GitHub release with asset: `gh release create v1.x.0 video-intel.skill --title "v1.x.0 - Title" --notes "description"`

The `.skill` file is a build artifact (like a Docker image) - it lives in GitHub Releases, not in git. It's in `.gitignore`.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run tests with coverage
pytest --cov=scripts --cov-report=term-missing -v

# Lint and format
ruff format .
ruff check . --fix
```

Config in `pyproject.toml`. Run ruff before declaring any task complete.

## Workflows

This project uses the [Compound Engineering plugin](https://github.com/EveryInc/compound-engineering-plugin/) for structured workflows:

- `/workflows:work` — Execute tasks with progress tracking
- `/workflows:review` — Code review with multi-agent analysis
- `/workflows:compound` — Document solved problems (produces `docs/solutions/` entries)

Session plans are stored in `plans/` (configured via `.claude/settings.json`). Plans are session artifacts — historical, not living docs.

Solved problems are recorded in `docs/solutions/` following the three-bucket rule (living / historical / decision records).
