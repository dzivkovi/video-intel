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

# Full pipeline on a local MP4 with one Gemini upload
# (mindmap + transcript + concepts; lazy-skips already-done steps without re-uploading)
python scripts/video_intel.py process --file "./video-intel/earlyaidopters/some-talk.mp4"

# Override Gemini model (e.g., Pro for transcripts when Flash truncates)
python scripts/video_intel.py --model gemini-2.5-pro transcript --url "URL"

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
Optional: `VIDEO_INTEL_OUTPUT_DIR` (absolute path to the corpus; reached only when the plugin's `config.yaml` is absent — see Corpus Discovery below).

## Architecture

**Plugin, not single skill.** This repo ships as a **plugin** — a container that holds multiple independent skills — per the current Anthropic plugin format. Layout:

```text
video-intel/                          ← plugin root (git repo root)
├── .claude-plugin/plugin.json        ← plugin manifest (name, version, skill list)
├── skills/
│   ├── video-intel/SKILL.md          ← curate: scan / transcript / mindmap / process / index / dedupe / concepts / taxonomy-build
│   ├── video-intel-search/SKILL.md   ← read-only query: search / nugget / status (globally installable)
│   └── translate-bcs/SKILL.md        ← BCS subtitle translation
├── scripts/                          ← shared by all skills
├── prompts/                          ← shared by all skills
├── config.yaml                       ← gitignored; per-user. Copy config.yaml.example to create.
├── config.yaml.example               ← committed template
└── tests/                            ← covers shared scripts
```

Each `SKILL.md` has its own frontmatter description and is independently triggered by Claude Code. Scripts and prompts are shared at the plugin root so the skills can reuse `translate_video.py`, `gemini_common.py`, etc., without duplication. The operational-separation rule (translate_video.py does not read video-intel's config/taxonomy/meta.json) is unchanged — scripts stay independent even though they ship in the same plugin.

**When the plugin is installed**, all three skills become available to Claude. A user asking "scan my channels" triggers `video-intel` (curate); a user asking "find videos about MCP" triggers `video-intel-search` (read-only); a user asking "translate this video to Bosnian" triggers `translate-bcs`. No cross-skill invocation is required — each skill's body tells Claude which CLI commands to run, and Claude executes them from the plugin's shared scripts directory.

### Corpus Discovery

`load_config()` in `scripts/video_intel.py` resolves `output_dir` via a four-step precedence chain (see KD1 of `docs/plans/2026-04-23-001-feat-search-skill-portability-plan.md`):

1. **`SKILL_DIR/config.yaml`** — the plugin-local config, gitignored. Authored by the developer running curate workflows from a source checkout. Wins when present so a stale env var cannot silently redirect `scan` away from the canonical corpus.
2. **`$VIDEO_INTEL_OUTPUT_DIR`** — env var override for users who point a cached plugin at a different corpus. Must be an absolute path.
3. **`~/.video-intel/config.yaml`** — user-level minimal config accepting `output_dir` (required) and `vector_db_dir` (optional). Extra keys are ignored with one INFO log.
4. **Hard error** naming both the env var and the user-config path.

One INFO log line per invocation names the winning source (e.g. `"Config resolved from VIDEO_INTEL_OUTPUT_DIR=/foo"`).

**Curate guard:** curate commands (`scan`, `concepts`, `dedupe`, and the `--channel` branch of `mindmap` / `transcript` / `process`) require `channels:` in the resolved config. Running them with the user-level minimal config (no channels) fails fast with an actionable message. Read-only commands (`search`, `nugget`, `status`, `index`, `taxonomy-build`) do not require `channels:`.

### User-level install (`video-intel-search` skill anywhere)

The read-only search skill can be made available from any project via a
user-level marketplace entry in `~/.claude/settings.json`. See
[INSTALLATION.md](INSTALLATION.md#claude-code-user-level-access-the-search-skill-from-any-project)
for the step-by-step procedure (JSON to paste, OS-specific absolute paths,
env-var vs user-config choice).

> **Critical:** the marketplace key under `extraKnownMarketplaces` and the
> suffix after `@` in `enabledPlugins` MUST both be exactly `video-intel`
> (matching `.claude-plugin/plugin.json`'s `name` field). Claude Code
> normalizes the key silently - any suffix like `-local` is stripped, the
> `enabledPlugins` entry points at a marketplace name that does not exist,
> and skills never appear in other projects. If you hit "skills not
> appearing after Claude Code restart", this is the first thing to check.

Curate operations (`scan`, `process`, `concepts`, `dedupe`, and the
`--channel` branch of `mindmap` / `transcript` / `process`) still require
the plugin repo as CWD - they read `channels:` from the plugin-local
`config.yaml` which the user-level fallback does not provide.

**Shared utilities:** `scripts/gemini_common.py` — Gemini retry logic (`get_retry_delay`), client factory with httpx timeouts (`create_client`), lazy imports (`require_gemini`, `require_youtube`). Used by both scripts; kept minimal.

**Single script:** `scripts/video_intel.py` — all logic in one file, subcommands:
- `scan` — uses YouTube Data API to discover new videos per channel, then calls Gemini in parallel (`ThreadPoolExecutor`) to generate mind maps. Optionally chains transcript and concept generation. Supports **selective mode**: channels with `playlists` or `keywords` in config skip the date-based uploads scan and only process matching videos. `resolve_playlist_ids()` resolves human-readable names to IDs via case-insensitive contains matching. `fetch_keyword_videos()` uses `search().list()` (100 quota units/call, capped at `KEYWORD_MAX_PAGES` pages). `fetch_selective_videos()` dispatches and deduplicates.
- `transcript` — calls Gemini with `response_json=True`, parses the three-task JSON response (speech + screen_content + speakers), and merges them into a fused markdown document via `merge_transcript_json()`. Resilient to malformed JSON: tries direct parse, then `isolate_json()` cleanup, then `salvage_transcript_sections()` to recover partial content. Saves raw Gemini response as `.transcript.raw.txt` sidecar on failure for forensics. One bounded retry if salvage fails. Partial transcripts are written with a visible warning block and `transcript_status: "partial"` in meta.json. Accepts either `--url` (YouTube) or `--file` (local MP4) as input. For local files, uses `upload_local_video()` via Gemini Files API (48h auto-expire); by default output lands next to the source file with the filename stem as prefix. **Channel-scoped local recovery (plan rev 4):** pass `--channel <NAME>` alongside `--file` (or drop the file under `output_dir/<channel>/` and let the parent-folder inference pick it up) to route artifacts into the canonical channel folder with the same meta.json shape as scan-generated artifacts. `resolve_local_file_identity()` picks the prefix: (1) sibling `.meta.json` wins, (2) G2 dedup against canonical scan metas by `video_id`, (3) explicit `--video-id`/`--title`/`--date` flags, (4) filename stem plus `LastWriteTime`. Canonical `video_url` and Gemini `media_uri` stay separate fields so `file_uri` never persists to disk. Both paths support `--start`/`--end` for segment clipping (parsed by `parse_time_to_seconds()`, accepts `MM:SS`, `HH:MM:SS`, or raw seconds).
- `mindmap` — generate a mind map for a single video. Accepts `--url` (YouTube) or `--file` (local video) as input. The `--file` path mirrors `transcript --file`: drop a gated-video MP4 under `output_dir/<channel>/` (or pass `--channel` explicitly) to route the `.mindmap.md` + `.meta.json` into the canonical channel folder using the same identity resolver as transcript. Used by the scan 403 recovery flow: `scan` on a members-only video returns 403 and the log line prints a two-command recipe (`mindmap --file ... --channel ...`, then `transcript --file ... --channel ...`).
- `concepts` — extract and normalize concepts from existing mindmaps against a growing canonical vocabulary (thesaurus). Text-only Gemini calls reading mindmap markdown, not video.
- `process` — one-upload orchestrator for local MP4s. Calls `upload_local_video()` once (lazy: skipped when meta.json already records all modes completed and artifacts exist on disk), threads the `file_uri` to `process_mindmap(..., media_uri=...)` and `process_transcript(..., media_uri=...)`, then runs `process_concepts(...)` inline on the now-on-disk mindmap text. Partial-success semantics: mindmap persists even if transcript fails; exit 0 whenever mindmap succeeded. File-expiry fallback: if either helper returns a status matching `_is_file_expiry_error_status()` (references `files/` + expired/not-found/FAILED-state, and no negative marker like quota/safety), re-upload once and retry once. The observability helper `log_usage_metadata()` logs token usage on every Gemini call through `process_mindmap`, `process_transcript`, `process_concepts` (via the `on_response` callback in `call_gemini`/`call_gemini_text`); `cmd_nugget`'s direct `generate_content` call at `:3356` is intentionally not instrumented. Accepts `--file` (required), `--channel`, `--video-id`, `--title`, `--date`, `--start`/`--end`, `--force`, and `--prompt`.
- `taxonomy-build` — rebuild `taxonomy.json` by aggregating all per-video `concepts.json` files. This is a derived artifact, always rebuildable.
- `search` — search corpus by concept label/alias (default) or hybrid BM25+vector (`--vector`). Concept search returns matching videos with artifact paths. Hybrid search returns ranked transcript chunks by combined keyword and semantic relevance (RRF fusion). Use this FIRST when the user asks about topics — avoids reading the entire corpus. **Stage-1 query expansion (2026-04-20, [ADR-0017](docs/adr/ADR-0017-kb-layer-strategy.md)):** hybrid mode preprocesses the query through `expand_query_via_taxonomy()`, appending creator-vocabulary siblings for any canonical label or alias in `taxonomy.json` that matches the query. The expander uses a punctuation-aware boundary (handles `C++`, `.NET`, `(MCP)`, `k3s` where stdlib `\b` fails), caps sibling additions at 12 per query to limit embedding dilution, and writes its expanded string to both the BM25 FTS call and the Voyage query embed. Pass `--no-expand` to disable and run the pre-Stage-1 baseline behavior for A/B comparison. `hybrid_search()` also accepts `return_diagnostics=True` to return `(hits, expansion_record)` — the eval harness uses this to write per-query records to `tests/evals/results/<run_tag>-expansion.jsonl`. Concept-search mode (`search_corpus()`) is intentionally untouched by Stage 1.
- `index` — build search index (vector embeddings + FTS on title/text) from all transcripts using LanceDB + Voyage AI. Required before `search --vector`. Rebuildable at any time.
- `dedupe` — find and clean up title-rotation duplicates (same `video_id`, different slug). Groups meta.json files by `video_id`; for any group with >1 meta, picks canonical by latest `processed` timestamp (tie-break on `modes_completed` size, then alphabetical prefix), merges loser titles into canonical's `alt_titles` list, moves artifacts for any mode only a loser has, deletes loser siblings. Dry-run by default; pass `--apply` to mutate. After `--apply`, re-run `taxonomy-build` and `index --force` (derived artifacts are not auto-rebuilt: blast radius stays predictable). Prevention is automatic: `is_processed()` consults a per-channel `{video_id: prefix}` index before falling back to slug-based existence checks, so the same `video_id` under a rotated title is recognized as already-processed. A pre-scan pass inside `cmd_scan` calls `record_alt_title_if_rotated()` to capture ongoing rotations into existing metas' `alt_titles`.

All commands support `--force` to regenerate existing output files. Gemini-calling commands (scan, mindmap, transcript, concepts, process) accept `--model` / `-m` at the top level to override the config.yaml model. Precedence: CLI flag > config.yaml > `DEFAULT_MODEL` constant. `MAX_OUTPUT_TOKENS = 65536` caps Gemini output (matches `translate_video.py`). Default log level is `info` (visible progress without extra flags).

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

- `vector_db_dir` (optional): path for the LanceDB vector index. Defaults to `output_dir / .lancedb`. Must be on a real local filesystem — cloud-synced mounts (Google Drive File Stream, OneDrive, Dropbox) do not support the atomic file operations LanceDB needs to commit its MVCC manifests. The `index` command runs a pre-flight probe (`probe_atomic_writes`) that does a throwaway LanceDB connect + create + drop round-trip against the target path; if that round-trip fails, the command aborts with an actionable diagnostic *before* any Voyage embedding call, saving the user from paying for embeddings on a write that cannot succeed. The vector index is a derived artifact (rebuildable from transcripts via `index`), so it is safe to live in a local cache directory (e.g., `~/.cache/video-intel/lancedb`) outside a cloud-synced `output_dir`. See [ADR-0016](docs/adr/ADR-0016-vector-db-path-config.md).

- Per-channel `enabled: false` (added 2026-04-24) skips the channel from `scan` entirely — including explicit `scan --channel <name>` invocations — but keeps the channel addressable for `mindmap --url --channel <name>`, `transcript --url --channel <name>`, `mindmap --file` / `transcript --file` / `process --file`, and `concepts --channel <name>`. Use this for non-scannable sources: Skool communities (no YouTube API metadata), Vimeo and other platforms the URL parser does not understand, members-only YouTube that 403s from Gemini, and one-off creators whose feed is mostly off-topic. The flag is opt-in (default true) and strict — overriding it on the command line is deliberately unsupported. See [`docs/solutions/integration-issues/non-scannable-sources-enabled-flag-20260424.md`](docs/solutions/integration-issues/non-scannable-sources-enabled-flag-20260424.md) for the full pattern and `tests/test_channel_enabled_flag.py` for the contract.

**Idempotency:** `is_processed()` checks for existing output files by `{date}-{slug}.{mode}.md` naming. Re-running scan safely skips already-processed videos. All commands support `--force` to regenerate.

**Output goes to** `~/video-intel/{channel_name}/` (configurable via `output_dir`), not into this repo. Master `taxonomy.json` lives at the output root.

**Concept layer:** Per-video `concepts.json` is the source of truth. `taxonomy.json` is derived (rebuilt by `taxonomy-build`). During batch extraction, new concepts accumulate in memory so each video normalizes against concepts discovered in earlier videos. See ADR-0010.

**Search internals:** Score math, pipeline mechanics, tuning levers, and empirical observations are documented in [`docs/search-internals.md`](docs/search-internals.md). Read this before modifying search behavior.

**Testing and eval framework:** [`docs/testing.md`](docs/testing.md) is the operational reference for both test suites — unit/integration in `tests/` and the grounded-golden-dataset retrieval eval in `tests/evals/`. As of 2026-04-19 the hybrid-search eval baseline is 1/25; any PR that touches retrieval logic must re-run `pytest tests/evals/` and record the new N/25 in the description. The golden dataset at `tests/evals/golden_dataset.yaml` is a frozen contract per [ADR-0017](docs/adr/ADR-0017-kb-layer-strategy.md) — edits need ADR-grade justification.

**KB-layer direction:** [`ADR-0017`](docs/adr/ADR-0017-kb-layer-strategy.md) establishes a staged approach (query expansion → LightRAG → LLM Wiki), each stage gated on eval uplift. Cognee is rejected. The April 2026 brainstorm lives in `work/2026-04-16/04-knowledge-layer-options-brainstorm.md` and `work/2026-04-16/03-architecture-futures-cognee-lightrag-llm-wiki.md` — ADR-0017 is the durable decision record on top of them.

## Key Design Decisions

- Gemini is a multimodal proxy, not a competing assistant. Video understanding requires vision+audio that Claude doesn't have via API.
- The transcript prompt requests structured JSON with three parallel tasks (diarization, screen content, speaker ID). `merge_transcript_json()` fuses them by timestamp sort.
- `SKILL_DIR` is resolved from the script's own path (`Path(__file__).resolve().parent.parent`), making the skill relocatable across `~/.claude/skills/`, `~/.gemini/skills/`, or `~/.agents/skills/`.
- Lazy imports (`require_gemini()`, `require_youtube()`) in `gemini_common.py` give clear error messages when dependencies are missing instead of cryptic ImportErrors.

## Packaging and Distribution

**Plugins are distributed as git repositories, not as packaged archive files.** The current Claude Code plugin docs describe two real consumption paths, and neither involves uploading a `.zip` or `.skill` artifact to a GitHub release:

1. **Self-registering local install** (primary): the repo ships with `.claude/settings.json` containing `extraKnownMarketplaces` pointing at itself and `enabledPlugins` pre-activating the plugin. When a user clones the repo and opens Claude Code inside it, the plugin is auto-discovered. Claude shows a one-time trust prompt; the user clicks "Install for this project" and both skills become available. No manual path editing.
2. **Marketplace install** (future, for broader distribution): the plugin author publishes the repo, then a Claude Code marketplace references it. End users install via `/plugin install video-intel@<marketplace-name>` from inside Claude Code.

What matters for shipping a release of this repo:

- Tag the commit on `main` with `vX.Y.Z`. Push the tag.
- Make sure `.claude-plugin/plugin.json` `version` matches the tag.
- The `.claude/settings.json` that ships with the repo handles local auto-discovery for anyone who clones.

The `output_dir` in `config.yaml` should point outside the plugin folder (e.g. `~/video-intel`) for production use, so user data does not live inside the cached plugin directory that Claude Code manages.

## Release Process

1. Bump `.claude-plugin/plugin.json` `version` to match the upcoming tag.
2. Commit the bump.
3. Tag the commit:

   ```bash
   git tag -a vX.Y.Z -m "short description"
   ```

4. Push commits and tag: `git push origin main --tags`
5. (Optional) Create a GitHub Release tied to the tag with release notes — useful for humans browsing changes, even though Claude Code itself does not consume a release asset under the documented install paths.

**Users installing from a previous release:** The repo went from "single skill at repo root" to "plugin with two skills under `skills/`." Anyone who previously installed by copying the old layout to `~/.claude/skills/video-intel/` should remove that directory and re-install via one of the documented plugin paths. Claude Code manages the actual plugin cache itself (`~/.claude/plugins/cache/...`); end users do not copy files there directly.

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run unit/integration tests
pytest tests/ -v --ignore=tests/evals

# Run tests with coverage
pytest --cov=scripts --cov-report=term-missing -v --ignore=tests/evals

# Run retrieval eval (requires pip install deepeval, VOYAGE_API_KEY, built LanceDB index)
pytest tests/evals/ -v -s

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

## Parallel Sessions

- **Suspect parallel workers when the working tree is unfamiliar.** If you find untracked files you did not create, your branch differs from session-start, or `git status` shows changes you did not make, another Claude session is likely operating on the same checkout. Do **not** stash, checkout, or reset to "clean up" — that overwrites the other worker's in-progress changes. Instead, isolate via `git worktree add ../<repo>-<feature> <your-branch>` and continue from the worktree. The shared `.git` dir is safe; only the working tree needs isolation. *Why:* parallel Claude sessions are not coordinated by Compound Engineering or any hook, so without a worktree they fight over a single index/HEAD and one will silently lose work. Burned once on 2026-04-25 when issue #36 work and `feat/skip-shorts-and-prune` raced and the other worker had to WIP-stash the in-flight changes onto a branch.
- **CE has `ce-worktree` but does not auto-invoke it.** Phrasings like "create a worktree" trigger the skill explicitly; otherwise sessions share the working tree. If you anticipate parallel work (multiple open issues, side-by-side feature branches), kick off with `ce-worktree` rather than waiting to discover the conflict.

## Code Review Guardrails

Rules for review agents (auto-selected by `/ce-code-review`) and for anyone cutting a PR. These are the non-obvious checks — the general "does it work, does it have tests" bar is assumed.

- **Bounded retries only.** The `transcript` path tries one JSON parse, then `isolate_json()`, then `salvage_transcript_sections()`, then one bounded retry if salvage fails. Do not promote this to an unbounded loop. Partial writes plus a `.transcript.raw.txt` sidecar are the designed failure mode, not something to "fix."
- **Probe before you pay.** `probe_atomic_writes()` runs *before* any Voyage embedding call in `build_search_index`. Reordering that sequence (probe after embedding, probe conditional on a flag, probe only in verbose mode) costs ~$0.30 per failed run. See [ADR-0016](docs/adr/ADR-0016-vector-db-path-config.md). Reviewers: grep for `probe_atomic_writes` in any diff touching `build_search_index` or `index` CLI.
- **Timestamps are data, not decoration.** Every retrieved chunk carries `timestamp_seconds`, surfaced as `&t=<seconds>` in result URLs. Changes to chunking, dedup, or rendering that drop or corrupt that field break user-visible behavior. Reviewers: grep for `timestamp_seconds` in diffs touching `hybrid_search`, `_dedup_by_video`, or chunk rendering.
- **Skill-parity: same diff, not follow-up.** When a PR adds a new CLI subcommand or flag to `video_intel.py` or `translate_video.py`, the matching `SKILL.md` entry (natural-language routing) lives in the same PR. "I'll update the skill separately" is a regression — the plugin's skill surface drifts from its CLI surface and users can't reach the new capability through the skill.
- **Video id is the identity, slug is decoration.** Any code that dedups, idempotency-checks, or looks up per-video artifacts must key on `video_id` (with slug as fallback for legacy files missing meta.json). The 2026-04-22 title-rotation dedup shipped `_load_video_id_index()` and a `dedupe` subcommand precisely because slug-only checks missed A/B-tested titles. Reviewers: grep for `video_file_prefix` or `is_processed` in diffs touching scan/transcript/concepts — any new path that treats slug as identity needs pushback.
- **Out of scope for cleanup flags.** `docs/plans/*.md`, `docs/solutions/*.md`, `work/**/*`, and the root `plans/` directory are living or historical session artifacts. Review agents must not flag them for deletion, rewriting, or consolidation — that's the three-bucket rule at the end of the Workflows section.
