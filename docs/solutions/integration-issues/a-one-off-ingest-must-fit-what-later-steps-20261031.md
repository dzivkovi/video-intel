---
title: "A one-off ingest must fit what later steps assume: name the channel the way config will, write the transcript contract exactly, and prove the clock against an independent source"
date: 2026-10-31
category: integration-issues
module: video_intel.py
problem_type: best_practice
component: tooling
severity: medium
applies_when:
  - A better transcript already exists for a video and you want it in the corpus without a new Gemini transcript call
  - A recording was trimmed or re-cut before upload, so its transcript may run on a different clock than the published video
  - A video from a creator who is not yet in config.yaml is processed with --url
tags: [manual-ingest, transcript-contract, timestamps, deep-links, channel-naming, identity, one-off-creators]
related_components:
  - documentation
---

# A one-off ingest must fit what later steps assume

## Context

Hand-ingesting one video (a transcript produced elsewhere, a talk recording, a creator not yet in config) is tempting because it skips the paid path. Every later step still reads the result as if `scan` had written it. Two things went wrong first: a hand-picked short prefix that a later step re-derived differently, and a one-off ingested before its creator had a config entry.

## Guidance

**No new subcommand is needed.** Mindmap-from-transcript, `concepts`, `taxonomy-build` and `index` all read files. Put two files in the channel folder, both named by `video_file_prefix` (`{published}-{slugify(title)}`), never a prefix you chose by hand, with these three properties:

- A `.transcript.md` whose header matches what `_write_transcript_md` emits and `_identity_from_transcript_header` parses: `# Transcript: <title>`, a `**Source:**` URL carrying a parseable 11-character id (a `v=` URL or a youtu.be short link), `**Published:** YYYY-MM-DD`, and a `---` line closing the header. `dialogue_entries_from_transcript_markdown` treats a file whose title is never followed by `---` as all header, so `assess-transcripts` skips it.
- Dialogue lines that open at column zero with `[MM:SS]` or `[H:MM:SS]`, the shape `chunk_transcript` and `ENTRY_TIMESTAMP_PATTERN` treat as an entry boundary. Anything else folds into the previous chunk and inherits its timestamp.
- A `.meta.json` with full identity (`video_url`, `video_id`, `channel`, `title`, `published`) and a `transcript_status` of `complete` or `ok` (`_HEALTHY_TRANSCRIPT_STATUSES`). With the transcript on disk and no severe quality flag, `resolve_mindmap_source` under `auto` picks the text-only path; a non-healthy status still works but stamps the mindmap with a partial-source provenance marker.

`assess-transcripts` branches on `transcript_source`: only the captions value gets captions thresholds, so a custom source value is judged by the Gemini thresholds.

**Prove the clock.** A transcript of a recording that was trimmed before upload is offset from the published video by a constant. Every `&t=` link is then wrong by that amount, and nothing notices: the stamps stay well-formed and monotonic, so usually no quality flag fires (an offset large enough to push stamps past the video length raises only a mild overrun flag). Derive the offset, then check it against times produced independently of the transcript (a hand-checked chapter list) at the start, middle and end. Drop content from before the cut, and fix the stamps in the ingest itself rather than with a standing prompt rule that every future run pays for.

The same independent-clock idea gives a free repair for a Gemini clock slip: the speaker-evidence footer (the `## Speaker Identification Evidence` block `merge_transcript_json` renders) comes from a separate task in the three-task prompt and often carries its own times, so it can anchor a correction without a re-run.

**Name the channel the way config will.** On a `--url` run, `--channel` accepts any name, and an unconfigured video falls back to `slugify(channelTitle)`. If a later config entry uses a different (for example handle-derived) name, one creator splits into two folders and two per-channel keys, silently skewing every per-channel count and rollup. Use the name config will use, or add an `enabled: false` stub before the first one-off (see the non-scannable sources doc below).

## Why This Matters

These failures are quiet. A re-derived prefix makes a later step miss the artifact and redo paid work. An offset clock breaks the one thing the corpus promises, which is that a citation jumps to the moment it names (CLAUDE.md, "Timestamps are data, not decoration"). A split channel corrupts counts without any error.

## When to Apply

- Before writing any artifact into a channel folder by hand.
- Whenever a recording was edited, trimmed or re-uploaded after the transcript was produced.
- Before the first `--url` run for a creator that is not in config.yaml.

## Examples

Offset check: a chapter list says a topic starts at 12:40 on the published video, and the transcript has it at 15:10. Confirm the same 2:30 difference near the middle and near the end before shifting every stamp by it.

## Related

- `docs/solutions/integration-issues/non-scannable-sources-enabled-flag-20260424.md` (`enabled: false` stubs)
- `.claude/rules/scan-config.md`, manual `--url` channel resolution (#205)
- Issue #66 (every transcript meta stamps full identity; `repair-metas` backfills from the header)
- Issue #195 (`ENTRY_TIMESTAMP_PATTERN`, unbounded minutes)
- `prompts/cliffnotes-distiller.md` (every row carries a clickable `&t=` timestamp)
