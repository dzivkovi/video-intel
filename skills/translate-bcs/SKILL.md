---
name: translate-bcs
description: >
  Translate YouTube videos and rich English transcripts into
  Bosnian/Croatian/Serbian (BCS) subtitles via Gemini. Use this skill
  whenever the user wants to: translate a YouTube video to Bosnian,
  Croatian, Serbian, or Serbo-Croatian; produce BCS captions or
  subtitles for a video; download just the English SRT from a YouTube
  video with no translation; or translate a context-rich transcript
  (with on-screen content and speaker labels) into BCS while preserving
  that context. Trigger phrases include "translate to Bosnian",
  "translate to Croatian", "translate to Serbian", "Serbo-Croatian
  subtitles", "BCS subtitles", "BCS captions", "titl na bosanski",
  "titl na srpski", "titl na hrvatski", "prevedi ovaj video", "prevedi
  titlove", "download the English SRT", "just give me the SRT", any
  YouTube URL followed by a request for Bosnian/Croatian/Serbian
  output, and any request to caption or subtitle a YouTube video in
  one of those languages. This skill is for subtitle translation only —
  for English transcription with on-screen content, use the video-intel
  skill instead. Built for diaspora audiences in Bosnia, Serbia,
  Croatia, Montenegro, Kosovo, and neighboring regions who want
  long-form YouTube content in a language they read fluently.
---

# Translate BCS

Bosnian/Croatian/Serbian subtitle translation for YouTube videos, powered by Gemini.

## What This Skill Does

Three modes, picked based on what the video contains and what the user needs:

1. **URL → BCS (captions-first, for short videos)** — the script fetches YouTube's English caption track and sends it to Gemini for translation. Fast (minutes), cheap (~10-20K input tokens). Falls back to direct video-audio translation if no captions exist.

2. **URL → English SRT** (`--srt-only`) — download only, no translation. Writes a `.en.srt` sibling file byte-identical to what downsubs.com or `yt-dlp --write-auto-subs` would produce. No Gemini call. Useful when the user wants the English source for their own summarization workflow or to compare against a translation.

3. **Rich transcript → BCS** (`--from-transcript`) — translates a transcript produced by the `video-intel` skill's `transcript` subcommand. Preserves every structural marker (timestamps, SCREEN sections, On-screen text lines, speaker labels, code blocks) and translates every content field. Use this for context-heavy videos where the English captions would miss too much meaning.

## Prerequisites

- `GEMINI_API_KEY` environment variable (free at https://aistudio.google.com/apikey)
- For `--from-transcript`: generate the transcript first (see the decision guide below)
- Python dependencies: `google-genai`, `youtube-transcript-api`

```bash
pip install google-genai youtube-transcript-api
```

## Important: These Commands Are Slow

Gemini translation takes **1-15 minutes** depending on video length. The transcript-first workflow runs two Gemini calls (transcript + translation). This is normal.

- **Always use `--log-level info`** so progress is visible.
- **Use a long bash timeout** (at least 600000ms / 10 minutes). The default 2-minute timeout will kill translations of longer videos.
- **Silence between log lines is normal.** Gemini is streaming the translation — don't diagnose or interrupt.
- Wait for the "Written to" or "Already translated" line before reporting results to the user.

## Decision Guide — Which Mode to Use

| If the video is… | Use this command |
| --- | --- |
| **Long (over ~90 minutes)** — YouTube SRT chunking drifts on long videos ([issue #49](https://github.com/dzivkovi/video-intel/issues/49)); the rich-transcript path uses chunked transcription with normalized timestamps and is the supported route | **Transcript-first** (two-command workflow below) |
| Heavily edited with cut-ins to other speakers; on-screen text labels who is speaking or where footage came from; news-style overlays, tickers, OCR text; burned-in captions from other outlets | **Transcript-first** (two-command workflow below) |
| Short (under ~90 min) talking head, long interview, or single-speaker monologue — captions are enough, on-screen content does not carry meaning | `python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py URL` |
| No English captions available but audio-only translation is fine | Same captions-first command — it falls through to the video-understanding path automatically |
| The user only wants the English SRT (no BCS translation) | `python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py URL --srt-only` |
| The user wants mind maps, concept extraction, or cross-video search | This is not the right skill — use the **video-intel** skill instead |

## Two-Command Transcript-First Workflow

When the video is context-heavy and captions-alone would lose meaning, this workflow generates a rich transcript first, then translates it. Both scripts ship in the plugin's shared `scripts/` directory.

Two commands, run in order. The transcript command is idempotent — if it already exists from a previous scan, it skips instantly (no API cost) and logs the path.

```bash
# Step 1 — generate a rich English transcript (idempotent).
# Look for "Saved:" (new) or "Exists:" (already done) in the output — that's the transcript path.
python ${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py --log-level info transcript \
  --url "https://www.youtube.com/watch?v=VIDEO_ID"

# Step 2 — translate to BCS. Use the EXACT path from step 1's log output.
# Output goes to ./examples/ by default (same as URL-based translation).
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py --log-level info \
  --from-transcript "<path from step 1>"
```

A long transcript is translated in `--chunk-minutes` windows (default 20) and stitched, so a two-hour input does not go to Gemini in one call. `--start` / `--end` narrow it to a range first (the end is exclusive, so adjacent ranges tile). If a window returns nothing the run keeps the rest, marks the gap in place with an `<!-- MISSING: ... -->` comment plus a header notice, and exits 3 - re-run over that range to fill it.

Note: `--log-level` goes **before** the subcommand for `video_intel.py`, anywhere for `translate_video.py`.

## Default Commands

```bash
# Translate any talking-head video to BCS (captions-first, falls back to video audio)
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Save to a specific directory (default: ./examples)
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --output-dir ~/my-translations

# Print translation to stdout instead of writing a file
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --stdout

# Force overwriting an existing translation
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --force

# Download only the English SRT, no translation (free replacement for downsubs.com)
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --srt-only

# Force the video-understanding path even when captions are available
# (for testing the fallback, or when captions are known to be low quality)
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --force-video

# Translate only a time range (minutes)
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --start 0 --end 30

# Use a different model (default: gemini-2.5-pro — GA, stable, highest quality)
python ${CLAUDE_SKILL_DIR}/../../scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --model gemini-2.5-flash
```

## Output Format

Translation output is a plain-text file with a metadata header and timestamped BCS lines:

```text
# Translation (BCS): Video Title Here

**Source:** https://www.youtube.com/watch?v=VIDEO_ID
**Published:** 2026-04-07
**Translated:** 2026-04-15 21:24 UTC
**Model:** gemini-2.5-pro
**Source mode:** Local transcript file (from video_intel.py transcript)

---

[00:00] Speaker Name (uloga): "prevedeni sadržaj..."

  SCREEN [00:24-00:27] [other]: Opis snimka.

[00:28] Drugi govornik: "sljedeća rečenica..."
```

The `**Source mode:**` line tells the reader where the translation came from: YouTube manual captions, YouTube auto-generated captions (with ASR cleanup), direct video audio, or a local transcript file.

## Output Location

- Default URL mode: `./examples/{date}-{slug}.translate-bcs.txt` (or wherever `--output-dir` points)
- `--srt-only` mode: `./examples/{date}-{slug}.en.srt`
- `--from-transcript` mode: sibling to the input — same directory, same base name, `.translate-bcs.txt` extension

## When NOT to Use This Skill

Use the **video-intel** skill instead when the user wants:
- English transcripts with on-screen content preserved (not translated)
- Mind maps or thematic summaries of a video
- Concept extraction and cross-video taxonomy
- Searching a library of processed videos

This skill is a one-way translation utility: YouTube video → BCS subtitle file. It does not maintain a video library, does not extract concepts, and does not integrate with the video-intel pipeline's `meta.json` or `taxonomy.json` conventions.

## Language Note

The translation is Bosnian-neutral Latin-script ijekavica — natural across Bosnia, Serbia, Croatia, Montenegro, and Kosovo, and readable by most Serbian speakers who use Cyrillic. Gemini 2.5 Pro is the default because it follows the dialect and timestamp instructions faithfully; Flash is available via `--model` but produces lower-quality translations on politically sensitive or technical content.

## Troubleshooting

- **"GEMINI_API_KEY not set"** — export the key from https://aistudio.google.com/apikey
- **"No English captions available ... cannot produce SRT"** — the video has no captions at all (CC disabled by uploader). Remove `--srt-only` to fall back to video-audio translation.
- **Translation is missing the last few minutes** — `finish_reason: MAX_TOKENS` in the log. Try `--thinking-budget 128` if on 2.5 Pro (default) or `--thinking-budget 0` on 2.5 Flash.
- **IPv6 socket stalls** — pass `--ipv4` to force IPv4 (workaround for googleapis/python-genai#1893).
- **Translation output extends past the video's real duration** — the overshoot detector fires a warning and annotates the file. Content is preserved; review the tail manually and trim if needed.
