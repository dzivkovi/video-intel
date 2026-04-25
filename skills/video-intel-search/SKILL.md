---
name: video-intel-search
description: >
  Query a pre-built video corpus (mindmaps, transcripts, concept taxonomy,
  hybrid search index) produced by the video-intel skill. Use whenever the
  user wants to: find videos about a topic; look up what a creator said
  about something; retrieve evidence or quotes from transcripts; browse
  concepts in the library; synthesize a cross-creator brief ("nugget")
  grounded in indexed evidence; ask about corpus status (last scan,
  video counts); summarize a specific video that is already in the corpus;
  decide if a video is worth watching based on its indexed content. This
  skill is read-only against an existing corpus - it does not scan, index,
  or transcribe. Safe to install globally and invoke from any project.
  Trigger phrases: "find videos about [X]", "search my videos for [X]",
  "what videos cover [X]", "what did [creator] say about [Y]", "evidence
  for [claim]", "when did [creator] mention [Z]", "nugget brief on [X]",
  "consultant brief on [X]", "what do creators say about [X]", "agreements
  and disagreements on [X]", "synthesize insights across creators",
  "mental models across creators", "find the nuggets about [X]", "show
  corpus status", "when was this last scanned", "what concepts are in my
  library", "what topics recur across channels", "summarize this video",
  "is this worth watching", "what should I watch", any YouTube URL
  followed by a question about its content. For scanning new videos,
  transcribing, generating mindmaps, rebuilding the index, or any write
  operation on the corpus, use the video-intel skill from the plugin
  repo instead - those operations require channels configured and API
  keys the search skill does not need.
---

# Video Intel Search

Read-only query access to the video corpus. Pairs with the `video-intel` curate
skill, which builds and maintains the corpus from the plugin repo.

## What This Skill Does

Three commands against the pre-built corpus:

1. **`search`** - find videos and transcript passages by concept, keyword, or
   semantic similarity. Two modes: concept (fast, no API calls, returns video
   matches) and `--vector` (hybrid BM25 + vector + RRF, returns transcript
   passages with timestamps).

2. **`nugget`** - synthesize a consultant-grade cross-creator brief on a topic.
   Retrieves top-K evidence via hybrid search, feeds it through a Gemini-backed
   synthesis prompt, returns attributed insights with timestamps.

3. **`status`** - report on corpus freshness (last scan per channel, video
   counts, taxonomy size). No API calls.

## Portability

This skill is safe to install globally via a user-level
`~/.claude/settings.json` entry. From any CWD, it resolves the corpus via:

1. Plugin-repo `config.yaml` (if running from the plugin checkout)
2. `VIDEO_INTEL_OUTPUT_DIR` env var (absolute path to your corpus)
3. `~/.video-intel/config.yaml` with `output_dir:` and optional `vector_db_dir:`

See the plugin repo's `CLAUDE.md` for the full install procedure.

## Prerequisites

- **Corpus must exist.** This skill does not build one - run `video-intel`'s
  `scan` / `process` / `index` commands from the plugin repo first.
- **`VOYAGE_API_KEY`** - required for `--vector` search and `nugget`. Concept
  search (default) works without it. Get a free key at
  https://dash.voyageai.com/.
- Python dependencies: `pip install video-intel[vector]` (installs lancedb,
  voyageai) for hybrid search; the base install covers concept search.

## Important: Some Commands Call Gemini

- `search` concept mode: local lookup against `taxonomy.json`, instant.
- `search --vector`: Voyage embedding call + LanceDB hybrid query, ~1-3 seconds.
- `nugget`: hybrid search + Gemini synthesis call, 30-90 seconds. Use a
  long bash timeout (600000ms / 10 min) and `--log-level info` for progress.
- **`--log-level` goes BEFORE the subcommand.** `python video_intel.py --log-level info nugget "query"` works; `python video_intel.py nugget "query" --log-level info` errors with argparse. Applies to every subcommand.

## How to Use

### Find videos about a topic (start here)

```bash
# Concept match - fast, no API calls, returns videos + artifact paths
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "skills standard"

# Hybrid search - returns full transcript passages with timestamps
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "150-line skill limit" --vector

# Filter to a specific channel
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "context window" --vector --channel natebjones

# Date-window filter for "last N days" queries
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "recent takeaways" --vector --channel natebjones --since 30d
```

**Mode reference:**

| User intent | Command | Notes |
|-------------|---------|-------|
| "which videos cover X?" | `search "X"` | Concept match. Fast, no API calls. Video list + paths. |
| "what did they say about Y?" | `search "Y" --vector` | Hybrid search. Returns transcript passages (up to 3000 chars) with timestamps and speaker turns. |
| "recent X from [creator]" | `search "X" --vector --channel C --since Nd` | Pre-filtered date window, no recency bias. |
| "is this [URL] worth watching" | `search "<title or topic>" --vector` | If indexed, returns evidence; if not, tell the user to run the curate skill to process it. |
| "summarize this video" | `search "<video title>"` | If indexed, open the mindmap path from the result. If not, route to curate. |

Hybrid results include evidence directly - follow-up transcript reads are
usually unnecessary. Timestamps in result URLs (`&t=<seconds>`) jump to the
exact moment.

### Synthesize a cross-creator brief

```bash
# "What do creators say about X, together?" with attribution and emergent insights
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "LightRAG vs OpenBrain architectural tension"

# Restrict to recent coverage
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "context engineering" --since 90d

# Restrict to specific creators
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "graph RAG" --channel engineerprompt

# Save the briefing to a file
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "second brain patterns" --output brief.md
```

Output structure: Query in Focus -> Creators Surveyed -> Consensus -> Divergence
-> Noteworthy Nuggets (mental models, metaphors, warnings, workarounds) ->
Emergent Synthesis (1+1=3) -> Follow-Up Questions. Every claim cites the creator
and timestamp.

Options:
- `--limit N` - max excerpts feeding synthesis (default 15)
- `--channel X` - restrict to one creator
- `--since Nd` - time-window filter (`Nd` or `YYYY-MM-DD`)
- `--min-relevance F` - minimum RRF relevance score
- `--no-expand` - disable Stage-1 taxonomy query expansion
- `--output PATH` - write briefing to file instead of stdout

### Check corpus status

```bash
# Report freshness per channel, video counts, taxonomy size
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" status
```

## When to Use This Skill vs video-intel (curate)

| User intent | Skill |
|-------------|-------|
| Query existing corpus | **video-intel-search** (this) |
| Summarize a video already scanned | **video-intel-search** (look up in corpus) |
| Scan YouTube channels for new videos | **video-intel** (curate) |
| Transcribe a video or local MP4 | **video-intel** (curate) |
| Rebuild the index, taxonomy, or run dedupe | **video-intel** (curate) |
| Process a local MP4 through the full pipeline | **video-intel** (curate) |

If the user asks "summarize this [URL]" and the video is not in the corpus,
this skill's output tells them to switch to the curate skill to process the
video first. Do not attempt to scan or transcribe from this skill.

## Evaluate Search Quality

The repo ships a 25-query grounded golden dataset at
`tests/evals/golden_dataset.yaml`. Run before/after changes that touch retrieval:

```bash
pytest tests/evals/ -v -s
```

See `docs/search-internals.md`, `docs/adr/ADR-0013-hybrid-search-rrf.md`, and
`docs/adr/ADR-0017-kb-layer-strategy.md` for the retrieval design.
