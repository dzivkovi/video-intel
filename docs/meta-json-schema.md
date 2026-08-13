# meta.json schema

Every processed video has a `{date}-{slug}.meta.json` sidecar in its channel folder. This is the canonical reference for its fields. The shape has accreted over many issues; when you add a field, update this doc (it is the companion deliverable of any change that touches meta.json).

`meta.json` is derived state, not a source of truth for content (the `.transcript.md` / `.mindmap.md` / `.concepts.json` are). It records identity, processing state, and error/skip bookkeeping so re-runs are idempotent and failures are diagnosable.

## Field reference

### Identity (who this video is)

| Field | Type | Written by | Notes |
|---|---|---|---|
| `video_id` | str | scan, transcript, process | 11-char YouTube ID. **The identity key** - dedup and idempotency-checks key on this, not the slug ("video_id is identity, slug is decoration"). |
| `video_url` | str | scan, process | Canonical `https://www.youtube.com/watch?v=<id>`. Kept separate from any Gemini Files API `media_uri`, which never persists to disk. |
| `channel` | str | scan, process | Channel `name` from config.yaml (the output folder name). |
| `title` | str | scan, process | Video title. |
| `published` | str | scan, process | Publish date, `YYYY-MM-DD`. |
| `published_source` | str | process (local files) | How the date was derived when not from the YouTube API (e.g. file mtime). |
| `duration_seconds` | int | scan (`enrich_with_durations`) | Used by the Shorts filter and the long-video transcript guard. |
| `alt_titles` | list[str] | dedupe | Titles a creator rotated through (A/B SEO); merged onto the canonical meta during `dedupe`. |

### Processing state

| Field | Type | Written by | Allowed values / notes |
|---|---|---|---|
| `processed` | str (ISO-8601) | every writer | Last-processed timestamp. |
| `model` | str | mindmap, transcript | Gemini model used. |
| `modes_completed` | list[str] | every writer | Subset of `scan`, `transcript`, `mindmap`, `concepts`. Append-only via `update_meta`. |
| `mindmap_source` | str | mindmap | `transcript` or `video` - what the mindmap was built from (issue #54). |
| `mindmap_source_status` | str | mindmap | `partial` when the source transcript was non-healthy; absent otherwise. |
| `prompt` / `source_prompt` | str | mindmap | Mindmap prompt name used. |

### Transcript

| Field | Type | Written by | Allowed values / notes |
|---|---|---|---|
| `transcript_status` | str | transcript writers | `ok` (chunked) / `complete` (single-shot, captions) = **healthy** (`_HEALTHY_TRANSCRIPT_STATUSES`); `partial` (salvaged / thin chunks) and `truncated_output` (salvage caused by the 65536 output cap, issue #128 - the sweepable "a chunked re-run would fix this" marker) = degraded. Keep this set in sync with the writers. |
| `transcript_source` | str | transcript writers | `gemini` (multimodal, default) / `youtube_captions` (caption-derived, speech-only) / `local_file` (uploaded MP4). Issue #60. Provenance flag - fidelity is signalled here, not by degrading `transcript_status`. |
| `captions_is_generated` | bool | captions path | `true` for auto-generated ASR captions, `false` for a manual track. Issue #60. |
| `transcript_failover_reason` | str | captions failover | Why `auto` fell back to captions (the Gemini error / `prompt=0` / parse failure). Issue #60. |
| `transcript_recovery` | str | salvage path | `salvaged_sections` when a partial transcript was recovered from malformed JSON. |
| `transcript_parse_error` | str | salvage / error | The JSON parse error message. |
| `transcript_warning` | str | salvage path | Human-readable salvage warning. |
| `transcript_chunks` / `transcript_chunk_minutes` / `transcript_thin_chunks` | int | chunked path | Chunked-transcript bookkeeping (count, window size, count of sub-50%-coverage chunks). |
| `transcript_output_tokens` / `transcript_finish_reason` | int / str | single-shot salvage (issue #128) | Present only when the salvage hit the output cap: the reported candidates count and the response finish_reason, so an operator can judge whether a chunked re-run is worth paying for. |

### Skip and error bookkeeping

| Field | Type | Written by | Notes |
|---|---|---|---|
| `skip_modes` | list[str] | `mark-skip` | Per-mode skip (issue #42): any subset of `mindmap` / `transcript` / `concepts`. **Wins over `skip`** when both exist. |
| `skip` | bool | manual / legacy | Legacy full-skip. Honored only when `skip_modes` is absent. |
| `skip_reason` | str | `mark-skip` | Free-text bookkeeping for why a mode was skipped. |
| `last_error` | str \| null | every error path | The last error message, or `null`. |

## Minimal vs full meta

The transcript-first writer creates a **minimal** meta (only `processed`, `transcript_status`, `modes_completed`, `last_error`) before the scan loop fills in identity. A confabulation stub or a transcript-only run can therefore leave an **identity-less** meta with no `video_id`/`title`/`video_url`. This quietly conflicts with the "video_id is identity" rule and makes cleanup harder.

**Recommendation:** writers should populate `video_id`, `video_url`, `title`, and `published` on first write. Code that looks a video up by identity must tolerate a legacy minimal meta (fall back to the slug) but should not produce new ones.
