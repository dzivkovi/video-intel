---
date: 2026-04-16
topic: local-video-transcript
---

# Local Video Transcript: Rich Transcripts for Your Own Recordings

## What We're Building

Extend `video_intel.py transcript` to accept a **local MP4 file path** instead of
a YouTube URL, with **segment clipping** via `--start` and `--end`. Output
lands next to the source file as `{name}.transcript.md`.

Today: `transcript --url "https://youtube.com/..."` works only for YouTube.
After: `transcript --file meeting.mp4 --start 05:30 --end 18:45` works on your
own screen-sharing recordings (from local disk, Dropbox/GDrive sync folders).

## Why This Approach

Three options were considered:

1. **Local disk only (chosen)** - Path to MP4 on filesystem. Uses Gemini Files
   API to upload. Dropbox/GDrive sync folders are already local paths, so this
   covers 90% of cases.
2. **Accept any URL** - local paths, http/https, Dropbox shared links. More
   complex URL scheme handling.
3. **Full cloud API integration** - Dropbox/GDrive SDKs with OAuth. Overkill.

The chosen scope reuses the biggest existing piece: `translate_video.py` already
implements segment clipping via `video_metadata.start_offset/end_offset`
(lines 1206-1217). The pattern is proven.

## Key Decisions

- **Extend `transcript` command, not a new command.** `--url` and `--file` are
  mutually exclusive. Reuses existing prompt loading, JSON resilience (salvage,
  retry, raw sidecar), and `.transcript.md` output naming. One command for both
  inputs.

- **Segments are required for large files.** If the file is over a threshold
  (e.g., 500MB), `--start` and `--end` become required. Keeps things simple -
  you always know what segment you want anyway, and it dodges Gemini's 2GB
  per-file upload limit.

- **Output lands next to the input file.** `meeting.mp4` produces
  `meeting.transcript.md` in the same directory. Findable without config,
  matches user's mental model. Also produces a sibling `.meta.json`.

- **Segment specification uses time offsets only (MM:SS or HH:MM:SS).**
  No named segments, no config file, no state. Matches the simplicity of
  `translate_video.py`'s existing flags.

## How It Works

1. Validate `--file` path exists, check file size
2. If size > 500MB and no `--start`/`--end`, error with clear message
3. Upload to Gemini via `client.files.upload(file=path)` → get `file_uri`
4. Build video Part with `file_data=FileData(file_uri=...)` + optional
   `video_metadata=VideoMetadata(start_offset="330s", end_offset="1125s")`
5. Same three-task JSON prompt as YouTube transcripts
6. Same JSON resilience layer (direct → isolate → salvage → retry)
7. Write `meeting.transcript.md` next to `meeting.mp4`

## Resolved Questions

- **Time format:** Accept both `MM:SS` / `HH:MM:SS` and raw seconds. Parse and
  convert to seconds internally. Best UX, works for any video length.

- **File upload cleanup:** Leave alone. Gemini's Files API auto-deletes uploads
  after 48 hours. No explicit cleanup in the script. Preserves re-run
  optimization potential (could skip re-upload if same file hash within 48h).

- **Concepts/search integration:** Exclude local transcripts from both for v1.
  Local files are personal/one-off; the concepts taxonomy is designed for
  channel libraries. No synthetic `_local` channel. Revisit if use case grows.

## Next Steps

Run `/workflows:plan` for implementation details.
