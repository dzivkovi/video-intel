---
name: video-intel
description: >
  Ingest/curate side of the video intelligence plugin. Use whenever the
  user wants to modify or build the corpus: scan YouTube channels for new
  videos and generate mind maps via Gemini; transcribe a video (URL or
  local MP4); run the full mindmap+transcript+concepts pipeline on a local
  video; extract and normalize concepts into the taxonomy; rebuild the
  LanceDB hybrid-search index from existing transcripts; clean up
  title-rotation duplicates; prune YouTube Shorts that polluted the corpus;
  manage channel configuration. Trigger phrases:
  "scan channel", "scan all channels", "what's new from [creator]" (with
  scan intent), "last N days of [creator]", "transcribe this video",
  "transcribe [creator]'s backlog", "videos I am missing from [creator]",
  "catch up on [creator]", "fully scan [creator]", "backfill [creator]",
  "add [channel] to my watchlist", "find duplicate videos", "clean up
  duplicates", "dedupe my corpus", "the same video got scanned twice",
  "why do I have two mindmaps for [video]", "creator rotated the title",
  "prune shorts", "remove shorts from corpus", "delete YouTube Shorts",
  "clean up shorts", "too many shorts in my corpus",
  "process this local video", "run the full pipeline on [file]", "mindmap
  plus transcript plus concepts on one upload", "do everything on this
  MP4", "extract concepts", "rebuild the taxonomy", "rebuild the index",
  "build the search index". Requires GEMINI_API_KEY, YOUTUBE_API_KEY, and
  `channels:` configured in config.yaml - this skill must run from the
  plugin repo checkout, not a globally-installed cache. Calls Gemini as
  multimodal proxy (frames + on-screen text + audio). For read-only
  queries against an already-built corpus (library search, cross-creator
  synthesis, corpus-freshness reports, summarizing a video that is already
  indexed), use the video-intel-search skill instead - that one is safe
  to install globally and invoke from any project.
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
- **`--log-level` goes BEFORE the subcommand.** `python video_intel.py --log-level info scan` works; `python video_intel.py scan --log-level info` errors with argparse. Applies to every subcommand.
- **`--dry-run` is preview only** - shows what would be processed but creates no files and makes no Gemini calls. Use it to verify config before committing to a real scan.
- **Use a long bash timeout** (at least 600000ms / 10 minutes) for scan and transcript commands. The default 2-minute timeout WILL kill multi-video scans prematurely.
- **Silence between log lines is normal.** Gemini is processing video - don't diagnose or interrupt.
- **For large scans (10+ videos):** run in the background so the user isn't blocked. Check the output directory afterward for results.
- **For single transcripts:** 1-3 minutes is typical. Wait for the "Saved:" line before proceeding.
- **Transcripts are resilient to malformed JSON.** If Gemini returns broken JSON, the script tries to salvage partial content (speech entries, screen content) and writes a partial transcript with a visible warning. A partial transcript is useful for curiosity/search. For strategically important videos, rerun with `--model gemini-2.5-pro` or retry later.
- **Raw Gemini responses are saved on failure** as `.transcript.raw.txt` sidecars for debugging.
- **Exit 0 ≠ success on `--url` paths. Always verify.** `mindmap --url` and `transcript --url` exit 0 even when Gemini returns `403 PERMISSION_DENIED` (members-only, age-gated, region-locked, or otherwise restricted videos). The error is logged inline but not re-raised, so the script can keep going inside a `scan` batch. After any URL run, grep the captured output for `PERMISSION_DENIED` (or check that the expected `<prefix>.mindmap.md` / `<prefix>.transcript.md` actually landed on disk) before reporting success to the user. If 403 is found, jump to **"When a YouTube URL returns 403"** below.

## Interpreting User Intent

The verb a user reaches for doesn't always match a CLI command name. This
table is the canonical mapping — read it before picking a command.

| User says (intent) | Run | Notes |
|---|---|---|
| "transcribe this video" + URL | `transcript --url URL --channel <NAME>` | Single video. Grep log for `PERMISSION_DENIED` before reporting success. |
| "scan this single video" + URL, "process this URL", "full pipeline on this URL", "do everything on [URL]" | `mindmap --url URL --channel <NAME>` then `transcript --url URL --channel <NAME>` then `concepts --channel <NAME>` | No single-shot URL command exists (`process --file` is local-only). After each URL step, **grep log for `PERMISSION_DENIED`** — exit 0 lies on gated content. Run mindmap and transcript in parallel for wall-time savings; concepts is text-only and runs last. |
| "run full pipeline on [local file]", "mindmap + transcript + concepts on one upload", "process this MP4" | `process --file PATH` | Local video only; single upload, lazy-skipped when artifacts already exist |
| "scan", "what's new", "check for new videos" | `scan` | All channels, configured `since` |
| "what's new from [creator]" | `scan --channel X` | Single channel, configured `since` |
| "transcribe [creator]'s backlog", "videos I'm missing from [creator]", "catch up on [creator]" | `scan --channel X --since 2y` (or wider) | **Always `--dry-run` first** to surface scope |
| "fully scan [creator]", "everything from [creator]" | `scan --channel X --since 2005-01-01` | **Always `--dry-run` first** — implies entire channel history |
| Backlog of N videos to transcribe | `scan` with `auto_transcript: all` configured | NOT N separate `transcript --url` calls |
| "rebuild the index", "reindex after dedupe" | `index --force` | Write-path rebuild of LanceDB; query uses `video-intel-search` |
| "prune shorts", "remove shorts", "too many shorts in my corpus" | `prune-shorts [--apply]` | **Always `--dry-run` first** — destructive on `--apply`; deletes mindmap/transcript/concepts/meta per Short |
| "rebuild taxonomy", "update master vocabulary" | `taxonomy-build` | Derived artifact; rebuildable anytime |
| "skip transcript on this video", "stop trying to transcribe [URL]", "this video keeps failing transcript", "block transcript only" | `mark-skip --url URL --mode transcript [--reason TEXT]` | Per-mode skip (issue #42). Mindmap and concepts continue to run. Repeatable: `--mode transcript --mode concepts`. |
| "permanently ignore this video", "never re-process [video_id]", "skip these IDs on backfill", "stop touching this URL on every scan" | edit `skip_video_ids` under the channel in `config.yaml` | Declarative pre-fetch blocklist. Listed IDs never reach Gemini, never get a meta.json, no cost. Override = remove the ID from config. Cheaper than `mark-skip` since no meta.json roundtrip needed. |
| "find videos about X", "search for Y", "nugget brief on Z", "corpus status", "verify quote", "fact-check claim against [creator]" | — | **Wrong skill.** These are read-only queries; use the **video-intel-search** skill. |

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

## Query Workflow (Different Skill)

For searching the corpus, nugget briefs, corpus status, or summarizing a video
that is already indexed, use the **video-intel-search** skill. It is read-only,
globally installable, and reads the same `output_dir` this skill writes to.
This skill covers the write path only: scan, transcribe, process, index,
concepts, dedupe, taxonomy-build.

## How to Use

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

**When a YouTube URL returns 403 (members-only / gated content)**

**Detection.** Gemini cannot fetch members-only, paid, age-gated, or
region-locked videos and returns `403 PERMISSION_DENIED`. The script logs
the error and exits 0 — there will be a stub `<prefix>.meta.json` on disk
with `modes_completed: []` and `last_error: "...PERMISSION_DENIED..."`,
but no `.mindmap.md` or `.transcript.md`. This applies to `scan`,
`mindmap --url`, and `transcript --url` alike. The stub meta is **not
garbage** — it carries the canonical identity (`video_id`, `title`,
`published`, `channel`) and the recovery flow below will reuse it to
write artifacts under the canonical `{YYYY-MM-DD}-{slug}` prefix.

**Hint.** If `output_dir/<channel>/` already contains an MKV/MP4 with a
companion `.transcript.md`, the user has done this recovery before for
the same creator — follow the same pattern.

**Recovery (preferred: `process --file` — one upload, both modes):**

1. Download the video locally via the user's member access. The script
   does not download for you.
2. Save under `output_dir/<channel>/` named `<videoId>.mp4` (the 11-char
   YouTube ID). This makes the tool G2-dedup against any existing stub
   meta and route artifacts to the canonical `{YYYY-MM-DD}-{slug}`
   prefix automatically. `.mkv`, `.mp4`, `.mov`, `.webm`, `.avi` are all
   accepted.
3. Run `process --file`:

```bash
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info process \
  --file "${OUTPUT_DIR}/everyinc/jPrwIL2B56Q.mp4" \
  --video-id jPrwIL2B56Q \
  --title "Camp: Codex for Knowledge Work" \
  --date 2026-04-24
```

`--video-id` / `--title` / `--date` are redundant when a stub
`.meta.json` already exists with those fields, but passing them is
harmless and explicit. With a `<videoId>.mp4` filename and no stub,
they let the tool stamp identity into a fresh canonical meta.

**Why `process --file` over separate `mindmap --file` + `transcript --file`:**
single Gemini upload (the legacy form uploaded twice), lazy-skip when
artifacts already exist, automatic file-expiry recovery if Gemini's 48h
TTL expires mid-run, and inline concepts when the channel is configured.

**Legacy two-call form** (still works, kept for reference; prefer
`process --file` for new recoveries):

```bash
# Drop the MP4 under output_dir/everyinc/ first, then:
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" mindmap \
  --file "${OUTPUT_DIR}/everyinc/Compound Engineering Camp.mkv"
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" transcript \
  --file "${OUTPUT_DIR}/everyinc/Compound Engineering Camp.mkv"

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

### Process a local video (full pipeline on one upload)

Use `process --file` when the user has a local MP4 and wants the complete
artifact set — mindmap + transcript + concepts — from a single Gemini upload.
The existing `mindmap --file` and `transcript --file` commands still work for
single-mode runs; `process` is the opt-in efficient path when both are wanted.

```bash
# Channel inferred from parent folder (drop the MP4 under output_dir/<channel>/)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info process \
  --file "./video-intel/earlyaidopters/some-talk.mp4"

# Or keep the MP4 anywhere and pass --channel explicitly
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info process \
  --file "~/Downloads/some-talk.mp4" --channel earlyaidopters

# Regenerate everything from scratch (bypasses lazy-upload skip)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" process \
  --file "./video-intel/earlyaidopters/some-talk.mp4" --force
```

How `process` differs from calling `mindmap --file` then `transcript --file`:

- **One upload** per invocation. The 40+MB MP4 is uploaded to Gemini Files
  API exactly once and threaded to both the mindmap and transcript calls.
  Old flow uploaded twice, costing bandwidth and wall-clock.
- **Lazy upload skip.** When meta.json already shows all modes completed and
  artifacts exist, `process` uploads nothing and exits quickly. A partial
  prior run (e.g., mindmap succeeded but transcript did not) re-uploads
  once and regenerates only the missing steps.
- **File-expiry recovery.** If Gemini's 48h Files API TTL expires mid-run
  (rare), `process` detects the expiry error, re-uploads once, retries
  once, then fails cleanly.
- **Observability.** Each Gemini call emits a
  `usage <label> prompt=N cached=N candidates=N total=N` log line at info
  level. `cached>0` on the transcript line means implicit caching fired and
  you got a token discount; `cached=0` means it did not.
- **Exit-code contract.** Exit 0 if mindmap succeeded, regardless of
  transcript / concepts outcome. Automation callers that need partial-success
  detail inspect `modes_completed` in the resulting meta.json.

Concepts extraction runs inline when the channel is configured in
`config.yaml`. For loose files (no channel match), concepts is skipped with
a warning log and the run still exits 0.

Options:

- `--file PATH` - Path to local video file (required).
- `--channel NAME` - Channel name (must exist in config.yaml). Overrides
  parent-folder inference.
- `--video-id ID` - 11-char YouTube video ID for G2 dedup against a canonical
  scan meta.json.
- `--title T` / `--date YYYY-MM-DD` - Override filename-inferred defaults.
- `--start`/`--end` - Segment time offsets (shared across both video calls).
- `--force` - Regenerate all artifacts from scratch.
- `--prompt NAME` - Mindmap prompt override (default from config.yaml).

### Build the search index

```bash
# Build or rebuild the LanceDB index from all transcripts (requires VOYAGE_API_KEY)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" index

# Force a rebuild from scratch after dedupe or large corpus changes
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" index --force
```

The `index` command is a write-path operation (rebuilds the LanceDB hybrid
index). Querying the index belongs to the `video-intel-search` skill.

### Extract and normalize concepts

```bash
# Extract concepts from all mindmaps that don't have concepts yet
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info concepts

# Re-extract for a specific channel
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" --log-level info concepts --channel natebjones --force

# Rebuild master taxonomy from all concept files (fast, no Gemini call)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" taxonomy-build
```

### Clean up title-rotation duplicates

YouTube creators A/B-test video titles for SEO, which rotates the slug
and can fool `is_processed()` into re-scanning the same `video_id` under
a second prefix. Prevention is automatic (the video_id index inside
`is_processed()` catches repeats across any slug change), but historical
duplicates from earlier scans need a one-shot cleanup.

```bash
# Dry-run: report all video_id groups with >1 meta.json (no mutation)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" dedupe

# Restrict to one channel
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" dedupe --channel natebjones

# Apply: merges discarded titles into canonical meta's alt_titles list,
# moves any artifact only a loser has, deletes all loser siblings.
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" dedupe --apply
```

Canonical selection: latest `processed` timestamp wins. Tie-breaks:
larger `modes_completed` set, then alphabetical prefix. Discarded titles
are preserved as `alt_titles: [...]` on the surviving meta.

After `dedupe --apply`, derived artifacts may be stale. Re-run:

```bash
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" taxonomy-build
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" index --force
```

Dedupe is dry-run by default because it mutates shared state (disk).
Only pass `--apply` after reviewing the dry-run report.

### Prune YouTube Shorts

Shorts polluted the corpus before the scan-time `skip_shorts` filter
existed (default-on as of plugin v1.11.0). The `prune-shorts` subcommand
removes them retroactively. Detection: `duration < 60s OR /shorts/<id>
HEAD redirect returns 200`. Sidecars from `translate_video.py` (`.en.srt`,
`.translate-bcs.txt`) are preserved.

```bash
# Dry-run: report all Shorts with title, duration, URL, artifact count
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" prune-shorts

# Restrict to one channel
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" prune-shorts --channel chase_h_ai

# Apply: deletes mindmap.md, transcript.md (+ raw forensics), concepts.json,
# meta.json, and any mindmap.<variant>.md files for each detected Short.
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" prune-shorts --channel chase_h_ai --apply
```

After `prune-shorts --apply`, derived artifacts may be stale. Re-run:

```bash
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" taxonomy-build
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" index --force
```

Like dedupe, prune-shorts is dry-run by default. Always review the dry-run
output before passing `--apply` — eyeball the 60-90s edge-case rows to make
sure nothing substantive is in the deletion list.

### Skip a single mode on one video (per-mode skip)

Issue #42: a 2h 24m video kept truncating its transcript and hung scan for
hours. Marking the whole video `skip: true` worked but also blocked the
concepts pass that the existing mindmap could have fed. Use `mark-skip`
when you want to silence one mode (commonly `transcript`) and let the
others keep running.

```bash
# Stop trying to transcribe a single video. Mindmap and concepts keep going.
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" mark-skip \
  --url "https://www.youtube.com/watch?v=X5UN2LrRK48" \
  --mode transcript \
  --reason "JSON truncation on 2h24m video, structured output exceeds MAX_OUTPUT_TOKENS"

# Block both transcript AND concepts but keep mindmap
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" mark-skip \
  --url "https://www.youtube.com/watch?v=X5UN2LrRK48" \
  --mode transcript \
  --mode concepts
```

Writes `skip_modes: ["transcript"]` (or whatever modes you passed) into
the video's `.meta.json`. On subsequent scans, `is_skipped(..., mode="transcript")`
sees the array and skips just that loop. Repeating the same `--mode` is
idempotent. The optional `--reason` lands as a `skip_reason` field for
your own bookkeeping.

The default 90-minute filter (`transcript_max_duration_seconds` in
config.yaml) handles the long-video case automatically — `mark-skip` is
for cases where the duration is under threshold but transcript fails for
other reasons (poor audio, region-locked transcript fetch, etc.).

Backward compat: existing meta.json files with the old `skip: true`
keep behaving as full-skip on every mode. To migrate to per-mode, just
re-run `mark-skip` with the new flags — `skip_modes` wins outright when
both keys exist.

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
transcript_max_duration_seconds: 5400   # Skip transcripts on videos longer than this
                                        # (issue #42). Default 90 minutes. Mindmap
                                        # phase is unaffected. Override per workload.

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

  - name: lennyspodcast            # Manual one-offs only: skipped by `scan`.
    url: https://youtube.com/@lennyspodcast
    auto_transcript: all
    enabled: false                   # see "One-off creators" below

  - name: seankochel
    url: https://youtube.com/@iamseankochel
    auto_transcript: all
    skip_video_ids:                  # Issue #42: declarative pre-fetch blocklist.
      - X5UN2LrRK48                  # 2h24m SaaS workshop - transcript truncates,
                                     # mindmap + concepts already done by hand.
      - SOMEOTHERID                  # add IDs here as you see them fail. The scan
                                     # never touches these on subsequent runs.
```

**Selective scanning:** Channels with `playlists` or `keywords` target specific
content instead of scanning all uploads. Playlist names are resolved via YouTube API
(case-insensitive contains matching). Keywords search the entire channel history
(capped at 200 results per keyword). If `since` is also set, recent uploads are
fetched as an additional source alongside playlists/keywords.

**One-off creators (`enabled: false`):** When a creator posts content you
occasionally want a transcript or mindmap of, but you do NOT want them in
the regular `scan` rotation, add them with `enabled: false`. The channel
stays in config (so `mindmap --url --channel <name>`, `transcript --url
--channel <name>`, and `concepts --channel <name>` all work) but `scan`
skips them entirely — including when targeted explicitly via `--channel`.
To temporarily bulk-scan such a creator, remove the flag rather than
overriding it on the command line. The flag's purpose is durable
manual-only routing, not advisory exclusion.

### Prompt files

Prompt templates live at the plugin root, `${CLAUDE_SKILL_DIR}/../../prompts/`:
- `mindmap-knowledge.md` - Thematic mind map with domain terminology + timestamps (default)
- `mindmap-light.md` - Fast thematic scan (4-6 branches)
- `mindmap-heavy.md` - Comprehensive conceptual extraction
- `transcript.md` - Full diarized transcript with screen content
- `concepts.md` - Concept extraction + normalization against taxonomy
- `nugget-brief.md` - Consultant-grade cross-creator synthesis with attributed nuggets

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
