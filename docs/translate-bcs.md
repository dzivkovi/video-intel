# Bosnian/Croatian/Serbian (BCS) Translation Utility

The full guide. [README](../README.md#bosniancroatianserbian-bcs-translation-utility) carries the two-command summary; [`skills/translate-bcs/SKILL.md`](../skills/translate-bcs/SKILL.md) carries the agent-routing side (which phrasing triggers what).

A separate utility script for translating YouTube video audio into
BCS subtitles. Not part of the video-intel
pipeline, but shares the same Gemini API patterns and lives in this repo.

**About BCS.** Bosnian, Croatian, and Serbian — collectively "BCS" — are
mutually-intelligible South Slavic languages spoken across the former
Yugoslavia (Bosnia, Serbia, Croatia, Montenegro, plus Serbian-speaking
communities in Kosovo) and the diaspora. Same grammar, same core
vocabulary; differences are dialect, script, and a few hundred preferred
words. This script outputs Bosnian-neutral Latin-script ijekavica —
natural across all four countries and readable by Serbian Cyrillic users
without conversion.

**Why this matters.** Many immigrants around the world don't speak English
at all. Most long-form journalism, podcasts, and political interviews
that shape global discourse are in English — especially in North America.
For BCS speakers in diaspora communities, that means missing out.
This script gives them a path to read those videos in their own language —
about $0.50 for short videos via the captions-first path, or ~$0.90 per
2-hour video via the rich-transcript path. Built for family, elders, and
friends who live in North America but cannot benefit from English-language
YouTube.

```bash
# Translate a video to BCS (auto-detects title, saves to file)
# Default behavior: tries YouTube English captions first, falls back to video
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Save to a specific directory (e.g., the examples folder in this repo)
python scripts/translate_video.py "https://www.youtube.com/watch?v=Sm7568B0BC8" \
  --output-dir ./examples

# Print to stdout instead of file
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --stdout

# Use a different model (default: gemini-2.5-pro)
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --model gemini-2.5-flash

# Force the video-understanding path even when captions are available
# (useful for testing the fallback or when caption quality is known to be bad)
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --force-video
```

Output follows the same `{date}-{slug}` naming convention as video-intel
artifacts. See [examples/2026-04-05-the-tide-has-turned-rejoice-in-this.translate-bcs.txt](../examples/2026-04-05-the-tide-has-turned-rejoice-in-this.translate-bcs.txt)
for a real translation output.

**Translation strategy: SRT-first.** The script first checks YouTube for an
English caption track via `youtube-transcript-api`, preferring manually
authored captions over auto-generated. When a caption track exists, the
text goes to Gemini as a single non-streaming request — completes in
seconds, costs ~10-20K input tokens, and avoids the long-video safety-filter
soft-stops we documented in
[ADR-0015](../docs/adr/ADR-0015-permissive-safety-filters-for-faithful-reporting.md).
The output file's `**Source mode:**` field tells you exactly where the
BCS came from: manual captions, auto-generated captions (with silent ASR
cleanup), or direct video audio. When the captions track is auto-generated,
the SRT prompt instructs Gemini to repair punctuation and capitalization
as part of the translation pass.

**Long videos: rich-transcript path.** For videos over ~90 minutes, or
content where on-screen text and speaker changes carry meaning (lectures
with slides, multi-speaker interviews, news-style overlays, OCR-heavy
material), YouTube's SRT alone loses too much. Run two commands instead
of one:

```bash
# Step 1 — produce a rich transcript via video-intel
python scripts/video_intel.py transcript --url URL --channel <name>

# Step 2 — translate the transcript to BCS
python scripts/translate_video.py --from-transcript <path>
```

This path keeps speaker labels and on-screen content in the BCS output.
Cost is roughly $0.50 (transcript) + $0.40 (translation) = ~$0.90 per
2-hour video. The translate-bcs skill auto-routes long videos here. For
the engineering rationale, see
[docs/solutions/integration-issues/gemini-flash3-vs-pro25-chunked-transcription-20260427.md](../docs/solutions/integration-issues/gemini-flash3-vs-pro25-chunked-transcription-20260427.md).

**Video fallback (used when no captions exist):** Gemini's input limit is
1M tokens. Translation reads audio only — the `translate-bcs` prompt never
references on-screen text — so the script **defaults to low media resolution**
(~100 tokens/sec, fits videos up to ~170 min in a single request). Audio
quality is unaffected: `media_resolution` only controls video frame tokens,
and audio is tokenized at a fixed 32 tokens/sec regardless. Pass
`--high-res` (~300 tokens/sec, ~55 min per request) only when the prompt
needs to read on-screen text such as slides or burned-in captions.

**Long videos (resolution-aware threshold):** The chunking cutoff depends on
which media resolution you're using. At the default low resolution, videos
up to **150 minutes** run as a single request. With `--high-res`, the
threshold drops to **50 minutes**. Above the threshold, the script
auto-chunks into uniform `--chunk-minutes` (default 20) segments from the
start, and each chunk produces a separate part file — these are the
primary artifacts. Both single-request and chunked paths carry coverage
diagnostics in the output header: single-request gets a TRUNCATED
annotation if Gemini stops early, and stitched files include a
per-segment coverage table plus `<!-- segment ... -->` dividers around
non-ok chunks.

Long-video workflow is two steps: **translate** (produces part files), then
**stitch** (merges them). Part files use filenames for stable slug-based naming;
the video title is translated to BCS during stitch via a single lightweight
Gemini call. Timestamps within chunks are relative to the clip start — the
stitcher applies absolute offsets from the filename and normalizes to `[HH:MM:SS]`.

```bash
# Any talking-head video up to ~2.5 hours — single pass, low-res default
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Partial translation — e.g. first hour only, skip the interview segment
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --end 63

# Stitch auto-chunked parts (for videos past the resolution-aware threshold)
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --stitch

# Backfill a failed chunk
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --start 40 --end 60

# Slide-driven talk where on-screen terminology matters (rare)
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --high-res

# Override the auto-translated title
python scripts/translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" \
  --stitch --title "Moj Naslov"
```

**Partial translations:** When stitching a subset of a video (e.g., only
the first hour of a 2h18m video), the output includes a `**Coverage:**`
line in the header and a BCS reader note indicating what portion was
translated. Full translations omit these — no clutter in the normal case.

## Translating from a rich transcript (`--from-transcript`)

Some videos carry meaning that YouTube's English captions cannot preserve:
a journalist cutting between their own commentary and clips of other
speakers, on-screen overlays labeling who is speaking, quoted text from
documents or news tickers, footage with burned-in captions from another
news outlet. The captions-first path sees none of that. The
video-understanding fallback reads audio only. Both paths will translate
what is said but lose *why it was shown*.

The fix is to generate our own rich transcript first (speech + on-screen
content + speaker identification), then translate that file. Two
commands, run manually in sequence:

```bash
# Step 1 — rich transcript. One Gemini call, reads the video with vision
# enabled, produces speakers, SCREEN sections, and On-screen text: lines
# in English. Typical 10-minute video: 60-90 seconds, a few cents.
python scripts/video_intel.py --log-level info transcript \
  --url "https://www.youtube.com/watch?v=VIDEO_ID"
# Output: ~/video-intel/{channel-or-_standalone}/{date}-{slug}.transcript.md

# Step 2 — translate the transcript into BCS. Text-in / text-out. Preserves
# timestamps, SCREEN markers, On-screen text labels, speaker names; translates
# speech content, speaker role parentheticals, SCREEN descriptions, OCR text,
# and the Speaker Identification Evidence footer.
python scripts/translate_video.py --log-level info \
  --from-transcript "path/to/{date}-{slug}.transcript.md"
# Output: sibling file — same directory as the transcript, same base name,
# `.translate-bcs.txt` extension.
```

**When to use this instead of the default path:**

| Symptom | Use |
| ------- | --- |
| Plain talking head, long interview, single speaker, no overlays matter | **Default** (`translate_video.py URL`) — captions-first, fastest |
| No English captions available but audio is enough | **Default** falls through to video understanding automatically |
| Journalist cutting to clips of other speakers; overlays label who is speaking; OCR text matters; news tickers; multi-source edits | **`--from-transcript`** (run the two-step pipeline above) |

**Real example.** Abby Martin / Double Down News, 10 minutes, heavy
editorial cutting with labeled clips:

```bash
python scripts/video_intel.py --log-level info transcript \
  --url "https://www.youtube.com/watch?v=hLQbPCvV8W8"
# → video-intel/double-down-news/2026-04-07-abby-martin-went-to-israel-its-worse-than-you-think.transcript.md
# (67s, 288 lines with SCREEN / On-screen text / speaker labels)

python scripts/translate_video.py --log-level info \
  --from-transcript "video-intel/double-down-news/2026-04-07-abby-martin-went-to-israel-its-worse-than-you-think.transcript.md"
# → video-intel/double-down-news/2026-04-07-abby-martin-went-to-israel-its-worse-than-you-think.translate-bcs.txt
# (1m 54s, 289 lines, 12K tokens, thinking_budget=128 auto-applied,
#  timestamps / SCREEN / On-screen text counts preserved 1:1)
```

That writes the full BCS output next to the transcript, as
`<output_dir>/double-down-news/2026-04-07-abby-martin-went-to-israel-its-worse-than-you-think.translate-bcs.txt`.
It is a corpus artifact, not a file in this repo, so there is nothing to link to here - look in your own
`output_dir` after the run.

**Windowing (issue #206).** A long transcript is split into `--chunk-minutes` windows (default 20) and translated one window per Gemini call, then stitched. A two-hour transcript is roughly 190 KB of text: sent in a single call it either truncates at the output-token cap or fails outright, and a mid-run failure used to lose everything. Windows are cut on cue boundaries, so an entry and its SCREEN block are never split, and the translated title header is sent with the first window only.

`--start` and `--end` narrow the work **before** any Gemini call, so a range that selects nothing costs nothing. The end is **exclusive**: `--end 45` stops before the 45:00 cue and a following `--start 45` picks it up, so consecutive ranges tile without duplicating a boundary cue.

If a window returns nothing, the run continues (the other windows are real, billed translation) but the gap is never silent: an `<!-- MISSING: ... -->` comment marks it in place, an "Incomplete translation" notice at the top of the header names each missing range, and the command exits **3** rather than 0. Recover with `--start` / `--end` over the named range.

**Design notes.** This is a *manual* two-step handoff, not an auto-chained
pipeline — the intermediate transcript is a reviewable artifact, and the
two scripts stay operationally independent per [CLAUDE.md](../CLAUDE.md).
The `--from-transcript` flag accepts any transcript-shaped markdown file;
validation is permissive (file must exist, be under 500KB, and contain at
least one `[MM:SS]` timestamp line — no required footer, no strict header
format). The path inherits `SRT_DEFAULT_THINKING_BUDGET=128` on 2.5 Pro,
the same hallucination mitigation used on the captions path. `--stdout`
and `--force` work the same way as elsewhere.

**Error handling:** The script retries automatically on Gemini server
errors (408, 500, 502, 503, 504) with exponential backoff — up to 8
retries over ~30 minutes. Rate limits (429) retry 3 times with shorter
waits. A 20-minute read timeout prevents infinite hangs when Gemini
accepts a request but never responds — the connection is aborted and
your terminal is returned. All retries log progress with
`(Ctrl+C to abort)`.

**Note:** The Gemini Python SDK has a
[known bug](https://github.com/googleapis/python-genai/issues/1893) where
requests can stall at the socket level. If this happens, try `--ipv4` to
force IPv4 connections as a workaround.
