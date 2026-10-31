---
title: "A transcript's grade decides what to pay for and how it may be cited"
date: 2026-10-31
category: integration-issues
module: transcript sources (captions and Gemini) and citation from the corpus
problem_type: best_practice
component: tooling
severity: medium
applies_when:
  - Moving a channel to `transcript_source: yt-captions` to save the Gemini transcript cost
  - Deciding whether a captions-indexed talk is worth a full Gemini pass
  - Repairing one bad window of a transcript instead of the whole video
  - Quoting or deep-linking a transcript in a briefing or anything circulated
tags: [captions, transcript-source, mindmap-source, transcript-quality, citation, deep-links, cost]
---

# A transcript's grade decides what to pay for and how it may be cited

## Context
The corpus holds transcripts of very different grades: full Gemini multimodal (speech, on-screen text, speakers), speech-only caption tracks, `partial` or severe-flagged Gemini output, and local ingests. The grade is recorded in each video's `meta.json` (`transcript_source`, `transcript_status`, `transcript_quality_flags`), but it is easy to ignore twice: once when choosing a source to save money, and again when quoting. Two things went wrong this way: captions that mis-heard a talk's central term became the vocabulary later searches used, and a guest quote taken from captions text alone turned out to be the host speaking.

## Guidance
**Routing a channel to captions**
- Pair `transcript_source: yt-captions` with an explicit `mindmap_source: transcript`. Under the default `auto`, `resolve_mindmap_source` falls back to the mindmap-from-video path whenever no usable transcript is on disk, so a captions miss silently buys the expensive call (see `docs/cost-model.md`). With `transcript`, the same miss raises a `ValueError` you can see.
- Confirm the English track is manually authored with `yt-dlp --list-subs <url>`. The fetcher prefers a manual track but falls back to auto-generated captions and only records that after the fact (header status and `captions_is_generated`), so check `--list-subs` before routing the channel.
- Keep the throttles apart in your head. Caption text comes from the per-video caption endpoint (an IP refusal there is the #231 `captions request refused` row); scan discovery spends Data API quota. A refusal on one says nothing about the other. The official Data API caption download is meant for a video's owner, so it is not a route for a third-party corpus.

**Before paying for a Gemini upgrade**
- Transcribe one 10-minute window with `--start/--end` first. It shows whether slides carry meaning and whether the captions mis-hear the talk's key term, which matters because that term is what later searches will use.

**Repairing one window**
- A `--start/--end --force` run writes to the canonical transcript file, so back it up first and merge by absolute timestamp. Then rebuild the mindmap, concepts, `taxonomy-build` and `index --channel <name>`.
- Do not read chunk counts as a health metric across a source swap. The index groups five entries per chunk, and caption cues are typically much shorter than Gemini entries, so the count moves with no content lost.

**Citing**
- Read the meta before quoting. A `partial` or severe-flagged transcript is never deep-linked, because its stamps are not trustworthy. The status also does not mean the surviving words are accurate, so any quotation from it needs independent verification (the clip itself, or a second source) before it goes into something circulated.
- A captions transcript has no diarization. Attribute a quote to a guest only after a clip check.
- A local ingest with no real video id has an empty `video_url`, so it gets no link; a filename that happens to look like an 11-character id yields a URL that does not point at the talk, so do not link it either.
- Put the grade into the briefing's Sources entry, so the reader knows what each claim rests on.

## Why This Matters
Every saving and every citation rests on the same fields. Ignore them on the cost side and `auto` quietly spends the money captions were meant to save. Ignore them on the citation side and a briefing ships a link that lands on the wrong second, a quote with mis-heard words, or a line credited to the wrong speaker. None of these fails loudly: the scan exits 0 and the briefing renders.

## When to Apply
- Any time a channel's `transcript_source` changes, or a talk is considered for a Gemini re-run.
- Any time corpus text is quoted, linked or attributed outside the corpus.

## Examples
```yaml
channels:
  - name: example-talking-head
    url: https://youtube.com/@example
    transcript_source: yt-captions
    mindmap_source: transcript   # a captions miss errors instead of watching the video
```

A Sources entry that carries the grade: `Talk title (captions, speech-only; quote checked against the clip)` or `Talk title (Gemini, partial; quoted after verification, no timestamp link)`.

## Related
- `config.yaml.example` (the `transcript_source` cost tiers and `mindmap_source` values)
- `docs/cost-model.md`
- `docs/troubleshooting.md` (captions refusal row, #231; severe-flag row; per-window confabulation recipe, #123)
- `.claude/rules/transcript.md` (#157 quality assessor and containment rule, #231 captions classification)
- `skills/video-intel/SKILL.md` (captions are speech-only: no on-screen content, no diarization)
