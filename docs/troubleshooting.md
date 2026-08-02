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
| Mindmap describes a completely different video (plausible content, correct frontmatter) | **`prompt=0` on the mindmap-from-video call** - Gemini ingested no video and wrote from priors | confab guard discards it and records `last_error`; to clean up one already on disk, land a transcript FIRST, then delete the stale `.mindmap.md`, then regenerate (see the section below - a bare `--force` re-resolves to `video` and loops) | auto (#119) |
| Two mindmaps / metas for one video | **Title rotation** (A/B SEO) | `dedupe` (dry-run first) | manual (run `dedupe`) |
| Same video re-transcribed every scan; `meta.json` has no `video_id` | **Identity-less meta** (transcript writer was first and didn't stamp identity) | prevented going forward (writers stamp identity); `repair-metas --apply` heals existing ones | auto (#66) + `repair-metas` for old data |
| An already-done video is re-queued mid-scan though its artifacts are on disk (corpus on Google Drive File Stream) | **Cloud-mount read-after-write staleness** - the mount serves a cached pre-write `meta.json` to the scanning process | accepted environmental constraint; impact is bounded to wasted re-transcription (no hang/corruption) since #66/#74. See the scenario below | documented (#67) - blunted by #66/#74 |
| `duckdb: command not found` after `pip install -e ".[intelligence]"` | **The CLI is a separate binary** - the pip package is only the Python driver | `winget install DuckDB.cli` (Windows) / `brew install duckdb` (macOS), then `duckdb -readonly -ui ~/.cache/video-intel/intel.duckdb`; see README "Exploring the Intelligence Store" | manual (#102) |

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

The same `prompt=0` response can come back on a **mindmap-from-video** call (observed on livestream entries whose stream Gemini could not fetch), and there the output is far harder to spot by eye: a well-formed mind map with plausible `(MM:SS)` timestamps, describing an entirely different video. The frontmatter is no defense - `process_mindmap()` builds the `video` / `title` / `published` comments locally after the call returns. Issue #119 applies the same guard to that path: nothing is written, `last_error` records the reason, and the video stays eligible for a retry.

**Cleaning up a confabulated mindmap already on disk takes three steps, in this order.** A bare `mindmap --url --channel <name> --force` does NOT work, for two compounding reasons: under the default `mindmap_source: auto` the resolver only checks whether a transcript file exists, so with no transcript on disk (the usual state when the guard fired) it re-resolves to `source="video"`, re-confabulates, and trips the guard again; and because the guard raises BEFORE the write, `--force` cannot overwrite the poisoned artifact either. The command exits 0 in both cases, so an operator can believe the corpus was cleaned when the fabrication is still in it, still feeding `concepts.json`, `taxonomy.json`, and the index. Do this instead:

1. Land a transcript first (`transcript --url --channel <name>`, or a local-file / captions recovery if the stream itself is unfetchable). Without one, step 3 has nothing to read.
2. Delete the stale `.mindmap.md` (and its `.concepts.json`, so the fabricated concepts leave the taxonomy on the next `taxonomy-build`).
3. Regenerate with `mindmap --url --channel <name> --force`, which now resolves to `source="transcript"` (issue #54) because a transcript is present, and reads that instead of the stream.

The logged `error:` / `confabulation guard tripped` line is the real signal on every one of these commands, not the exit code.

### Title rotation (duplicate metas)

A creator A/B-tests titles, rotating the slug so the same `video_id` lands under two prefixes. Run `dedupe` (dry-run first) to merge the loser titles into `alt_titles` on the canonical meta and remove the duplicate artifacts; then re-run `taxonomy-build` and `index --force`.

### Cloud-mount stale meta reads (Google Drive File Stream) - accepted constraint

When `output_dir` lives on Google Drive File Stream, a `scan` process can read a **stale `.meta.json`** - the mount serves a cached pre-write version even after the write flushed locally. `_load_video_id_index` then misses a `video_id` that is actually on disk, `is_processed` falls back to the slug path, misses on a rotated title, and re-queues an already-done video. The tell is that the *same on-disk bytes* return different answers depending on when the reading process opened the file. This is the same class of cloud-mount limitation as [ADR-0016](adr/ADR-0016-vector-db-path-config.md) (which relocated the LanceDB index off the mount): a cloud-sync drive does not guarantee the read-after-write consistency the idempotency check assumes.

**Why this is documented, not code-fixed.** A stale read is not reliably distinguishable from a genuine "not done", and pathlib cannot force the mount's cache to refresh - a defensive re-read milliseconds later has no strong reason to bypass the same cached view (so it would be false confidence). Relocating `.meta.json` off the mount like `vector_db_dir` is the wrong trade: the vector index is a rebuildable local cache, but **`meta.json` is part of the portable corpus that Google Drive is meant to preserve and sync across machines.** So the meta layer intentionally stays with the corpus, and this read-after-write hazard is accepted.

**Why the impact is now small.** Two former amplifiers shipped: identity is always stamped into the meta ([#66], so a *fresh* read builds a complete index) and a re-queued transcript that hangs no longer freezes the scan ([#74], it times out and fails over). What remains is at worst **one wasted re-transcription** of an already-done video - no hang, no corruption, no garbage.

**If it bites you:** re-running the scan a few minutes later (a fresh process, after the mount's cache has caught up) processes it correctly. For a video that keeps re-queuing, `mark-skip --mode transcript` or `skip_video_ids` stops it. There is nothing to repair on disk - the artifacts are already correct.

**Same hazard, reader-side: a regenerated briefing PDF still shows its old content.** Observed 2026-08-02. Four briefing PDFs on the mount were rebuilt to fix table rendering; verification against the on-disk bytes showed the fix landed (zero raw pipe rows, correct byte size and mtime), yet the file opened from `G:\` still rendered the pre-fix layout. Same read-after-write cache, one layer up - the viewer or the mount served the previous revision. **Do not re-run the generator; it will produce identical bytes and the stale view will persist.** The tell is the same one as above: the same on-disk bytes give different answers depending on who opened them and when. Recovery is operator-side, which is why this is a row and not code: fully close the document in its viewer and reopen it, or copy the file off the mount first (`cp <file> ~/Downloads/`) and read the local copy. To confirm which side is wrong before touching anything, extract the text and count the damage - `python -c "from pypdf import PdfReader; t=''.join(p.extract_text() for p in PdfReader('f.pdf').pages); print(sum(1 for l in t.split(chr(10)) if l.count('|')>=2))"` returning `0` means the file is correct and the view is stale.

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
