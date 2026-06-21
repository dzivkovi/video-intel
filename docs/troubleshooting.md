# Troubleshooting: scan/transcript failure scenarios and recovery SOPs

Operator reference for the failure modes a scan hits in practice, each with its cause and a step-by-step recovery. These are documented procedures, not folklore - when a video does not process, find its symptom below.

This table doubles as the project's **failure-mode registry**: the **Status** column records where each recovery lives. `auto (#N)` means the tool self-recovers at runtime (the fix is in code - nothing for you to do); `manual (...)` means the recovery needs an operator action or a one-off command. New hard-won recoveries get a row here; when one graduates from `manual` to `auto` (a code fix ships), flip its Status and link the PR. See the durability-ladder note in `CLAUDE.md` for when a recovery should become code vs stay a doc row.

## Quick reference

| Symptom | Cause | Recovery | Status |
|---|---|---|---|
| Scan never finds a video that exists | **Unlisted** (not in the uploads feed) | manual `process --url`/`process --file`, or the SRT bridge below | manual (hard API limit) |
| `403 PERMISSION_DENIED` | **Members-only / gated** | download via membership, then `process --file` | manual (needs your access) |
| `400 INVALID_ARGUMENT`, fails fast | **Token cap** on a long video (single-shot transcript) | `transcript_source: auto` (captions fallback), or `process --url --chunk-minutes 50` | auto (#60) |
| Transcript call hangs for many minutes | **Gemini stall** on a long/dense video | per-transcript wall-clock timeout -> failover under `auto`; `mark-skip`/blocklist as backup | auto (#74) |
| Tiny transcript, `prompt=0`, looked "complete" | **Future/scheduled premiere** confabulated | confab guard discards it; pre-flight skips it before Gemini | auto (#60, #70) |
| Two mindmaps / metas for one video | **Title rotation** (A/B SEO) | `dedupe` (dry-run first) | manual (run `dedupe`) |
| Same video re-transcribed every scan; `meta.json` has no `video_id` | **Identity-less meta** (transcript writer was first and didn't stamp identity) | prevented going forward (writers stamp identity); `repair-metas --apply` heals existing ones | auto (#66) + `repair-metas` for old data |

## Scenarios

### Unlisted videos (scan cannot discover them)

`scan` discovers videos via the channel **uploads playlist** and `search()`. The YouTube Data API never returns unlisted videos to a non-owner key, so **no scan change can find them** - this is a hard API limit, not a bug. Captions and direct-URL playback still work, so once you have the URL you have options. (#70 adds pre-flight detection so an unlisted/future video that *does* appear via another path is skipped before Gemini, but discovery stays manual.)

Recovery options, cheapest first:
1. **Captions** (`transcript_source: yt-captions` or `--transcript-source yt-captions`) - speech-only, `$0` Gemini. Works because `youtube-transcript-api` hits the per-video caption endpoint, which serves unlisted videos.
2. **Full fidelity** - download via your access, save as `output_dir/<channel>/<videoId>.mp4`, then `process --file` (gets SCREEN/diarization).

### Members-only / gated (`403 PERMISSION_DENIED`)

Gemini cannot fetch members-only, paid, age-gated, or region-locked videos. `scan`/`mindmap --url`/`transcript --url` log the 403 and **exit 0** with a stub meta (`modes_completed: []`, `last_error: ...PERMISSION_DENIED...`). Always grep the run output for `PERMISSION_DENIED` before trusting exit 0.

Recovery: download via membership to `output_dir/<channel>/<videoId>.mp4`, then `process --file --video-id <id> --title "..." --date YYYY-MM-DD`.

### Token cap on long videos (`400 INVALID_ARGUMENT`, fails fast)

A long video's single-shot structured-JSON transcript exceeds Gemini's input or output cap and is rejected immediately. Scan's transcript loop pre-filters videos over `transcript_max_duration_seconds` (default 2h). For manual runs:
- **Chunk it:** `process --url URL --chunk-minutes 50` (each chunk is a separate Gemini call against the same upload, merged with offset-applied timestamps).
- **Or fail over to captions:** set `transcript_source: auto` on the channel - on a token-cap failure it falls back to the caption track (issue #60).

### Transcript hang (no output for many minutes)

A Gemini transcript call stalls and never returns. The httpx `read` timeout only bounds per-byte *silence* (1200s), not total time, so a slow-dribble or SDK-internal-retry hang can deadlock a scan indefinitely. Defenses, in order:
- **Per-transcript wall-clock timeout (issue #74, automatic).** Each transcript Gemini call (single-shot and per chunk) is capped at `transcript_timeout_seconds` (default 600s; per-channel/top-level config override). On expiry it raises `TranscriptTimeout`, which - exactly like a token-cap - **falls back to captions under `transcript_source: auto`** and is a clean error under `gemini`. A hang no longer deadlocks the batch and (under `auto`) self-rescues.
- `mark-skip --url URL --mode transcript --reason "hang on Nh video"` stops retrying just the transcript while keeping mindmap/concepts.
- Add the `video_id` to the channel's `skip_video_ids` to drop it before any Gemini call on future scans.

If you see a hang that *isn't* cleared by the timeout, it means the cap is set too high for the situation - lower `transcript_timeout_seconds` (top-level or per-channel). A run-wide external `timeout`/cap wrapper is no longer needed.

### Confabulation on future/scheduled premieres (`prompt=0`)

A channel publishes an announcement entry for a premiere that has not aired. It shows in the uploads feed, Gemini fetches a non-existent stream, ingests **zero** video tokens (`prompt=0`), confabulates a ~2 KB stub, and (before issue #60) the pipeline marked it `transcript_status: complete`. The **confabulation guard** (issue #60) now discards any `prompt=0` response so the garbage never lands.

The governing principle: **the corpus indexes things that have happened, not things that will happen.** If you find old confabulated stubs, delete the `.transcript.md` + `.meta.json` and rescan after the premiere airs. Issue #70 adds the pre-flight metadata check that skips `liveBroadcastContent: upcoming` videos before Gemini.

### Title rotation (duplicate metas)

A creator A/B-tests titles, rotating the slug so the same `video_id` lands under two prefixes. Run `dedupe` (dry-run first) to merge the loser titles into `alt_titles` on the canonical meta and remove the duplicate artifacts; then re-run `taxonomy-build` and `index --force`.

## SOP: SRT bridge for an unlisted/captioned video

When a wanted video is unlisted (or you just want a cheap speech-only index) and you do not need on-screen content, build a corpus transcript from its public caption track. As of issue #60 this is a one-command path - no manual file authoring:

```bash
# One-off: build a captions transcript and route it into the channel folder.
python scripts/video_intel.py transcript --url "https://www.youtube.com/watch?v=<id>" \
  --channel <channel> --transcript-source yt-captions

# Then generate the mindmap (text-only, reads the on-disk transcript) and concepts:
python scripts/video_intel.py mindmap --url "https://www.youtube.com/watch?v=<id>" --channel <channel>
python scripts/video_intel.py concepts --channel <channel>

# Finally re-index so it is searchable:
python scripts/video_intel.py index --force
```

The transcript is flagged `transcript_source: youtube_captions` with a banner noting it is speech-only (no SCREEN/diarization). Replace it later with `process --file` if the on-screen detail turns out to matter. To make captions the standing behavior for a discovery-only channel, set `transcript_source: yt-captions` (or `auto` for Gemini-with-captions-failover) on the channel in `config.yaml`.
