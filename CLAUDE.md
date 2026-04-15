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

# Optional: vector search
pip install lancedb voyageai

# Build vector search index (requires VOYAGE_API_KEY)
python scripts/video_intel.py index

# Semantic search over transcript chunks
python scripts/video_intel.py search "permission problems" --vector
```

Required env vars: `GEMINI_API_KEY`, `YOUTUBE_API_KEY`.
Optional: `VOYAGE_API_KEY` (for vector search, free at https://dash.voyageai.com/).

## Architecture

**Skill entry point:** `SKILL.md` — the YAML frontmatter `description` field controls when Claude Code triggers this skill. The body tells Claude how to invoke the scripts and manage config.

**Shared utilities:** `scripts/gemini_common.py` — Gemini retry logic (`get_retry_delay`), client factory with httpx timeouts (`create_client`), lazy imports (`require_gemini`, `require_youtube`). Used by both scripts; kept minimal.

**Single script:** `scripts/video_intel.py` — all logic in one file, subcommands:
- `scan` — uses YouTube Data API to discover new videos per channel, then calls Gemini in parallel (`ThreadPoolExecutor`) to generate mind maps. Optionally chains transcript and concept generation.
- `transcript` — calls Gemini with `response_json=True`, parses the three-task JSON response (speech + screen_content + speakers), and merges them into a fused markdown document via `merge_transcript_json()`.
- `mindmap` — generate a mind map for a single video URL with a specific prompt.
- `concepts` — extract and normalize concepts from existing mindmaps against a growing canonical vocabulary (thesaurus). Text-only Gemini calls reading mindmap markdown, not video.
- `taxonomy-build` — rebuild `taxonomy.json` by aggregating all per-video `concepts.json` files. This is a derived artifact, always rebuildable.
- `search` — search corpus by concept label/alias (default) or hybrid BM25+vector (`--vector`). Concept search returns matching videos with artifact paths. Hybrid search returns ranked transcript chunks by combined keyword and semantic relevance (RRF fusion). Use this FIRST when the user asks about topics — avoids reading the entire corpus.
- `index` — build search index (vector embeddings + FTS on title/text) from all transcripts using LanceDB + Voyage AI. Required before `search --vector`. Rebuildable at any time.

All commands support `--force` to regenerate existing output files.

**Standalone utility:** `scripts/translate_video.py` — BCS subtitle translation. **This is NOT part of the video-intel pipeline.** It shares the same Gemini API patterns (lazy imports, retry logic, FileData for YouTube URLs) and lives in this repo for convenience, but has no dependency on `video_intel.py`, `config.yaml`, or the scan/transcript/concepts workflow. It has its own CLI args, its own output directory (`./examples` by default), and its own tests. Do not integrate it into the main script or add pipeline features (meta.json, concept extraction, etc.) to it. Default model is `gemini-2.5-pro` (GA, stable, best translation quality and timestamp compliance).

**Translation strategy: SRT-first with video fallback.** The script first tries to fetch the YouTube English caption track via `youtube-transcript-api`, preferring manually authored captions over auto-generated. If captions exist, it sends them to Gemini as a single streaming text-only request — fast (minutes, not tens of minutes), cheap (~10-20K input tokens vs ~400K for video), and immune to the long-video safety-filter soft-stops documented in [ADR-0015](docs/adr/ADR-0015-permissive-safety-filters-for-faithful-reporting.md) and [the journalist-video solution doc](docs/solutions/integration-issues/gemini-soft-stop-political-content-20260411.md). The SRT-first path also writes a raw `.en.srt` sibling file alongside the BCS translation, byte-identical to what you'd get from downsubs.com or `yt-dlp --write-auto-subs`, so you always have an independently reviewable English source even when Gemini fails. The output file's `**Source mode:**` field tells the reader whether the BCS came from a manual caption track, an auto-generated caption track (with silent ASR cleanup), or direct video audio. When the captions track is auto-generated, the SRT prompt instructs Gemini to silently fix punctuation and capitalization as part of the translation pass. Pass `--force-video` to skip the captions check and go straight to video understanding (for testing the fallback or when caption quality is known to be bad). Pass `--srt-only` to write just the `.en.srt` sibling and exit without calling Gemini — useful as a free replacement for third-party subtitle-download sites or when summarizing videos in English only.

**SRT prompt discipline: positive 1:1 count invariant.** The `translate-bcs-from-srt` prompt instructs Gemini to produce exactly N output lines (where N is substituted via `{{INPUT_LINE_COUNT}}`), one per input line, in the same order, with a 1-to-1 correspondence by position. This is **positive framing only** — no "do not invent"/"do not extrapolate" negative rules, because empirical evidence (Phase 4 revert) showed those INCREASED hallucination rather than reducing it. The `test_prompt_has_no_hard_stopping_rule` and `test_prompt_has_no_negative_format_examples` tests in `tests/test_translate_video.py` guard against accidentally reintroducing the bad phrasings.

**Thinking budget caveat on Gemini 2.5 Pro.** The `--thinking-budget N` flag caps the model's internal reasoning tokens via ThinkingConfig. Critical fact: on 2.5 Pro, thinking tokens and output tokens **share** `max_output_tokens` — if thinking burns 28k of the 65k cap, you effectively have only 37k for visible output, which can trigger `MAX_TOKENS` mid-stream on long SRT jobs. 2.5 Pro cannot disable thinking (valid range **128–32768**, confirmed at [ai.google.dev/gemini-api/docs/thinking](https://ai.google.dev/gemini-api/docs/thinking)); 2.5 Flash can (range 0–24576); Gemini 3.x uses `thinking_level` and rejects `thinking_budget` entirely. The SRT path **defaults to `thinking_budget=128`** on 2.5 Pro (constant `SRT_DEFAULT_THINKING_BUDGET`). Empirical testing on a 1h 4min politically sensitive interview showed this eliminated hallucinated content, restored `finish_reason=STOP`, held timestamp drift to <2 minutes local, and cut total tokens by 26%. Pass `--thinking-budget N` to override. The video-understanding fallback path does NOT inherit this default. See `validate_thinking_budget()` for the model-aware range validator.

**Rich-transcript input (`--from-transcript PATH`):** For videos where on-screen content and speaker changes matter (clipped interviews, slide-heavy talks, content with OCR overlays), YouTube SRT alone loses too much context. The two-step manual chain is: (1) `python scripts/video_intel.py transcript --url "URL"` produces a rich markdown transcript with `[MM:SS]` timestamps, speaker labels with roles, `SCREEN [start-end] [type]: description` sections, optional `On-screen text: "..."` OCR lines, and an optional `## Speaker Identification Evidence` footer; (2) `python scripts/translate_video.py --from-transcript PATH/to/file.transcript.md` reads that file and translates it to BCS using the new `translate-bcs-from-transcript` prompt, which preserves every structural marker (timestamps, SCREEN markers, code blocks, markdown) and translates every content field (speech, SCREEN descriptions, OCR, role parentheticals, evidence bullets). The output lands as a `.translate-bcs.txt` sibling next to the input transcript. The path inherits `SRT_DEFAULT_THINKING_BUDGET=128` on 2.5 Pro. The two scripts stay operationally separate — this is a file handoff, not a pipeline merge. Use when captions are poor, missing, or when preserving on-screen context is essential to the translation's meaning. Validation is permissive: file exists, <500KB, contains at least one `[MM:SS]` line. No structural count canaries or monotonicity checks in v1 — we rely on the same thinking_budget mitigation that fixed the SRT path.

**Video fallback:** When no English captions are available, the script falls through to the video-understanding path. **Token budget:** that path reads audio only — the `translate-bcs` prompt never references on-screen text — so it defaults to low media resolution (~100 tokens/sec, fits videos up to ~170 min in one request). Audio quality is unaffected: `media_resolution` only controls video frame tokens, and audio is tokenized at a fixed 32 tokens/sec regardless. Use `--high-res` (~300 tokens/sec, ~55 min per request) only when a future prompt needs to read slides or burned-in captions. Chunking is resolution-aware: at the default low resolution, videos up to **150 minutes** run as a single request; with `--high-res`, the threshold drops to **50 minutes**. Above the threshold, the script auto-chunks into uniform `--chunk-minutes` (default 20) segments from the start. Part files are the canonical artifacts; `--stitch` merges them into a single file with timestamp normalization, a per-segment coverage table, `<!-- segment ... -->` dividers around non-ok chunks, and partial-translation annotations. Single-request outputs also get a coverage sanity check and a TRUNCATED annotation in the header if Gemini silently stops early. A visible `## ⚠️ Incomplete translation` H2 notice block is emitted before the body whenever observed coverage falls below 95%, with the root cause tailored to the captured `finish_reason` (SAFETY, MAX_TOKENS, STOP, or unknown).

**Prompt templates:** `prompts/*.md` — self-contained, referenced by name (without extension) in `config.yaml`:
- `mindmap-knowledge` — thematic mind map with domain terminology + timestamps (default)
- `mindmap-light` — fast scan, 4-6 branches
- `mindmap-heavy` — comprehensive, 6-10 branches with resources/perspectives
- `transcript` — three-task decoupled prompt returning structured JSON
- `concepts` — concept extraction + normalization against taxonomy, with `{{taxonomy}}` template slot
- `translate-bcs` — BCS subtitle translation system prompt for the **video-understanding fallback** (used by `translate_video.py` when no captions are available)
- `translate-bcs-from-srt` — BCS translation prompt for the **captions-first path** (text-in / text-out, preserves `[HH:MM:SS]` prefixes, optional `{{AUTO_GEN_NOTE}}` cleanup slot for auto-generated tracks)

**Config:** `config.yaml` — channels, output directory, model, parallelism, per-channel prompt/since overrides.

**Idempotency:** `is_processed()` checks for existing output files by `{date}-{slug}.{mode}.md` naming. Re-running scan safely skips already-processed videos. All commands support `--force` to regenerate.

**Output goes to** `~/video-intel/{channel_name}/` (configurable via `output_dir`), not into this repo. Master `taxonomy.json` lives at the output root.

**Concept layer:** Per-video `concepts.json` is the source of truth. `taxonomy.json` is derived (rebuilt by `taxonomy-build`). During batch extraction, new concepts accumulate in memory so each video normalizes against concepts discovered in earlier videos. See ADR-0010.

**Search internals:** Score math, pipeline mechanics, tuning levers, and empirical observations are documented in [`docs/search-internals.md`](docs/search-internals.md). Read this before modifying search behavior.

## Key Design Decisions

- Gemini is a multimodal proxy, not a competing assistant. Video understanding requires vision+audio that Claude doesn't have via API.
- The transcript prompt requests structured JSON with three parallel tasks (diarization, screen content, speaker ID). `merge_transcript_json()` fuses them by timestamp sort.
- `SKILL_DIR` is resolved from the script's own path (`Path(__file__).resolve().parent.parent`), making the skill relocatable across `~/.claude/skills/`, `~/.gemini/skills/`, or `~/.agents/skills/`.
- Lazy imports (`require_gemini()`, `require_youtube()`) in `gemini_common.py` give clear error messages when dependencies are missing instead of cryptic ImportErrors.

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
