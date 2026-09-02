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
  skill queries an existing corpus - it does not scan, index, or transcribe.
  `nugget` is the one command that also writes: by default it persists its
  synthesized brief back into the corpus under `_briefings/nuggets/` (pass
  `--no-save` to skip that). Safe to install globally and invoke from any project.
  Trigger phrases: "find videos about [X]", "search my videos for [X]",
  "what videos cover [X]", "what did [creator] say about [Y]", "evidence
  for [claim]", "when did [creator] mention [Z]", "nugget brief on [X]",
  "consultant brief on [X]", "what do creators say about [X]", "agreements
  and disagreements on [X]", "synthesize insights across creators",
  "mental models across creators", "find the nuggets about [X]",
  "which topics do I have", "list my topics", "show me everything tagged
  [topic]", "what videos are in my [topic] thread", "everything I've
  curated under [topic]", "show
  corpus status", "when was this last scanned", "what concepts are in my
  library", "what topics recur across channels", "summarize this video",
  "is this worth watching", "what should I watch",
  "verify whether [creator] said [paraphrase]", "fact-check this
  quote against [creator]'s videos", "did [creator] really say [X]",
  "is this [creator] quote real", "find the source for this [creator]
  claim", "check the corpus for the quote [paraphrase]", any YouTube
  URL followed by a question about its content. Also answers questions
  about the personalization lens itself: "why am I seeing this", "what
  is ranking my briefings", "show my interest profile", "where is my
  profile", "what does the digest think I care about" - `profile show`
  prints the resolved interest model and the paths of the two files
  that produce it, and writes nothing. For scanning new
  videos, transcribing, generating mindmaps, rebuilding the index,
  persisting or initializing the profile, or any other write operation on the
  corpus - including generating a catch-up briefing (`briefings --unseen`)
  or a curated topic briefing - use the video-intel skill
  from the plugin repo instead - those operations require channels
  configured and API keys the search skill does not need.
---

# Video Intel Search

Query access to the video corpus. Pairs with the `video-intel` curate
skill, which builds and maintains the corpus from the plugin repo. Three of
the four commands here are read-only; `nugget` additionally persists its
synthesis back into the corpus (see below) - it never scans, indexes, or
transcribes.

## What This Skill Does

Five commands against the pre-built corpus:

1. **`search`** - find videos and transcript passages by concept, keyword, or
   semantic similarity. Two modes: concept (fast, no API calls, returns video
   matches) and `--vector` (hybrid BM25 + vector + RRF, returns transcript
   passages with timestamps). Read-only.

2. **`nugget`** - synthesize a consultant-grade cross-creator brief on a topic.
   Retrieves top-K evidence via hybrid search, feeds it through a Gemini-backed
   synthesis prompt, returns attributed insights with timestamps. By default
   also persists the brief as a corpus artifact under
   `_briefings/nuggets/<date>-<query-slug>.md` so the same synthesis compounds
   instead of being re-paid every time it's asked; pass `--no-save` to skip
   the write and keep the old stdout-only behavior. `--no-save` runs are
   fully write-free (no config snapshot either).

3. **`search --topic <slug>`** - narrow either search mode to one curation
   topic's video set (see `topics-build` in the curate skill). Composable
   with `--channel`, `--since` and `--vector`. Answers "which videos belong
   to my FDE thread". Reads only the derived `topics.json`; when that file
   is absent or unreadable it says so and names `topics-build`, rather than
   returning a misleading empty result. With a query, the scope is applied
   to retrieval itself in both modes - at the search index for `--vector`,
   before the result cap for concept search - so a member competes only
   against the topic's other members, never against the whole corpus, and
   its corpus-wide rank is irrelevant. `--limit` caps how many results you
   see, exactly as it does for an unscoped search. The query is OPTIONAL
   with `--topic`:
   `search --topic fde` alone lists the topic's members (newest first, with
   channel, date and video link) straight from `topics.json` - no retrieval,
   no index needed. That is the command for "show me everything I tagged
   [topic]" / "everything I've curated under [topic]". `--vector` still
   requires a query (there is nothing to embed without one).

   `nugget` accepts the same `--topic <slug>` to scope its retrieval to a
   curated thread - "nugget brief on X from my [topic] videos" - so curation
   feeds synthesis instead of competing with corpus-wide ranking. The scope
   applies at the search index itself, so every topic member competes fairly
   for the excerpt slots; pass `--limit` at or above the topic's video count
   (shown by the query-less listing) to give every member a slot. Same
   narrowing contract (filters, never reorders), and a scoped brief records
   `topic: <slug>` in its front matter so the narrowed evidence base is
   visible provenance.
4. **`status`** - report on corpus freshness (last scan per channel, video
   counts, taxonomy size). No API calls.

5. **`profile show`** - print the personalization lens that ranks briefings and
   the headline digest (the resolved interest model and the paths of the two
   files behind it). Writes nothing, needs no `channels:`.

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

> **Do not `grep` / `Grep` / `rg` the `output_dir` directory directly when
> verifying a paraphrase.** The speaker's vocabulary almost never matches a
> paraphrase verbatim, so keyword search returns false negatives. Always
> start with `search --vector`, which uses semantic similarity to overcome
> that vocabulary mismatch. Direct file search is only appropriate when the
> user has already given you an exact phrase known to appear in transcripts.

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
| "which videos cover X?" (topic only, no creator) | `search "X"` | Concept match. Fast, no API calls. Video list + paths. |
| "what did they say about Y?" | `search "Y" --vector` | Hybrid search. Returns transcript passages (up to 3000 chars) with timestamps and speaker turns. |
| **"find videos about [creator] [topic]"** / **"[creator] on [topic]"** | **`search "topic" --vector --channel C`** | **Creator + topic = evidence query. Go to `--vector` from the start.** Concept search returns topic matches ranked by relevance across all creators and usually crowds out the specific creator's videos. |
| **"verify [creator] said [paraphrase]"** / **"fact-check this quote"** / **"did [creator] really say [X]"** | **`search "<key noun phrase>" --vector --channel C`** then **`nugget`** if multiple chunks help | **Paraphrase verification is a semantic question, not a keyword question. The speaker's vocabulary likely differs from the paraphrase - vector match catches it where keyword grep misses. Try 2-3 noun-phrase variants if the first returns nothing.** |
| "recent X from [creator]" | `search "X" --vector --channel C --since Nd` | Pre-filtered date window, no recency bias. |
| "is this [URL] worth watching" | `search "<title or topic>" --vector` | If indexed, returns evidence; if not, tell the user to run the curate skill to process it. |
| "why am I seeing this", "what is ranking my briefings", "show my interest profile", "where is my profile", "how do I retune my recommendations" | `profile show` | Read-only. Prints the resolved interest model (source `persisted` vs `inferred`, top weighted concepts/domains) and the on-disk paths of `_briefings/profile.yaml` (ranking weights) and `_briefings/audience.md` (reader-context prose). Writes nothing, needs no `channels:`. To *change* the ranking, point the user at those file paths - editing them is the retune path. |
| "summarize this video" | `search "<video title>"` | If indexed, open the mindmap path from the result. If not, route to curate. |

Hybrid results include evidence directly - follow-up transcript reads are
usually unnecessary. Timestamps in result URLs (`&t=<seconds>`) jump to the
exact moment.

> **Routing tip:** when a query combines a creator name and a topic (e.g.
> "Simon Scrapes on memory systems"), prefer `--vector --channel <name>`
> from the start. Concept search is fast but returns topic-dominant
> results that can drown out a specific creator's contribution.

### Synthesize a cross-creator brief

```bash
# "What do creators say about X, together?" with attribution and emergent insights
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "LightRAG vs OpenBrain architectural tension"

# Restrict to recent coverage
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "context engineering" --since 90d

# Restrict to specific creators
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "graph RAG" --channel engineerprompt

# Save the printed brief to a file of your own choosing, in addition to the
# corpus artifact below
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "second brain patterns" --output brief.md

# Skip persisting a corpus artifact - stdout only, old behavior
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" nugget "second brain patterns" --no-save
```

Output structure: Query in Focus -> Creators Surveyed -> Consensus -> Divergence
-> Noteworthy Nuggets (mental models, metaphors, warnings, workarounds) ->
Emergent Synthesis (1+1=3) -> Follow-Up Questions. Every claim cites the creator
and timestamp.

**Persistence:** by default, the brief is also written to
`_briefings/nuggets/<date>-<query-slug>.md` with front matter (`title`, `date`,
`query`, `generator`, `cited_video_ids`). A same-day re-run of the same query
gets a `-2`, `-3`, ... suffix rather than overwriting the earlier file. The
persisted file's `cited_video_ids` field is deliberately distinct from a
briefing's `video_ids` field: a video a nugget cites as evidence is not marked
"seen" and stays eligible to surface in a future catch-up briefing - citation
is weaker than curation.

Options:
- `--limit N` - max excerpts feeding synthesis (default 15)
- `--channel X` - restrict to one creator
- `--since Nd` - time-window filter (`Nd` or `YYYY-MM-DD`)
- `--no-save` - skip persisting the corpus artifact (stdout only)
- `--min-relevance F` - minimum RRF relevance score
- `--no-expand` - disable Stage-1 taxonomy query expansion
- `--output PATH` - write briefing to file instead of stdout

### See what is ranking the results

```bash
# Resolved interest model + the paths of the two files behind it (writes nothing)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" profile show
```

One compiled interest model ranks both the catch-up briefings and the scan's
headline digest, built from two files in the corpus:

- `<output_dir>/_briefings/profile.yaml` - machine ranking weights
  (`interest_concepts: {concept_id: weight}` plus `interest_domains`).
- `<output_dir>/_briefings/audience.md` - hand-written reader context (persona,
  pillars, goals, signal/noise) used when authoring a curated topic briefing.

Both are hand-edited to retune, and nothing overwrites them. When a user asks
why something ranked where it did, run `profile show` and read the answer off
the model - do not re-derive or invent ranking weights in-session, and do not
edit either file on the user's behalf unless they ask.

Two states worth reading out loud when they appear:

- `[inferred (ephemeral - not on disk)]` - no profile is persisted yet, so every
  run infers a throwaway one. Tell the user to run `profile init` from the
  **video-intel (curate)** skill in the plugin repo; this skill does not write.
- `[IGNORED - file exists but is empty or unparseable]` - a hand-edit broke the
  file, so ranking silently fell back to inference. Point at the path to fix.

### Check corpus status

```bash
# Report freshness per channel, video counts, taxonomy size
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" status
```

The output includes a per-channel topics rollup (the answer to "why is this
channel in my corpus"), when `topics.json` has been built.

### List which topics exist ("which topics do I have", "list my topics")

Topic slugs are not free text - they come from the first-level folder names
under `_briefings/` plus `--topic` stamps in per-video metas. To enumerate
them, run `status`: its per-channel rollup lists every topic with its video
count. If `status` reports no topics, `topics.json` has not been built yet -
that is a curate operation (`topics-build`, video-intel skill).

### Filter a search to one topic

```bash
# Concept mode
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "positioning" --topic fde

# Hybrid mode, composed with the other filters
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" search "discovery calls" \
  --vector --topic sales --since 180d
```

## When to Use This Skill vs video-intel (curate)

| User intent | Skill |
|-------------|-------|
| Query existing corpus | **video-intel-search** (this) |
| Summarize a video already scanned | **video-intel-search** (look up in corpus) |
| See what is ranking briefings / the headline digest (`profile show`) | **video-intel-search** (this - read-only) |
| Persist or scaffold the profile (`profile init`) | **video-intel** (curate - it writes files) |
| Generate a catch-up briefing across the whole corpus (`briefings --unseen`) | **video-intel** (curate) |
| Build a curated topic briefing (editorial synthesis) | **video-intel** (curate) |
| Scan YouTube channels for new videos | **video-intel** (curate) |
| Transcribe a video or local MP4 | **video-intel** (curate) |
| Rebuild the index, taxonomy, or run dedupe | **video-intel** (curate) |
| Prune shorts, remove shorts, delete YouTube Shorts from corpus | **video-intel** (curate) |
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
