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
| `transcript_quality_flags` | list[str] | quality guard (issue #157; extended by #158) | Sorted union of every severe/mild flag `assess_transcript_artifact` (and, for chunked runs, the per-chunk window-mismatch check) found. Severe (demotes `transcript_status` to `partial` / `partial (quality guard)`, counts as a pipeline gap - see `transcript_quality_flags_are_severe`): `monolithic_severe`, `blind_gap_severe`, `backward_jump_severe`, `chunk_window_mismatch_severe`. Mild (label-only, never changes status or exit code): `density_mild`, `backward_jump_mild`, `trailing_gap_mild`, `chunk_window_mismatch_mild`. |
| `transcript_max_blind_gap_seconds` / `transcript_blind_gap_at_seconds` | int | quality guard (issue #157) | The largest leading/internal/trailing dialogue gap `assess_transcript_artifact` found, and the second where it starts. Primary severity trigger for the blind-gap checks (see CLAUDE.md's issue #157 guardrail entry). |
| `transcript_last_dialogue_fraction` | float | quality guard (issue #157) | Last dialogue timestamp divided by known duration. TELEMETRY ONLY (issue #157 demoted this from a trigger to a metric - the 12 worst real blind-gap corpus cases all showed coverage `>= 0.99`, so a coverage-ratio trigger is precisely what missed that failure class the first time). |
| `transcript_dialogue_entries` | int | quality guard (issue #157) | Count of dialogue entries `assess_transcript_artifact` assessed. |
| `transcript_chunk_window_violations` | int | chunked path (issue #158) | Raw count of classified dialogue entries, summed across every chunk, that landed outside their own emitting chunk's real window - independent of which severity bucket (if any) fired. See the CLAUDE.md issue #158 guardrail entry for the window rule, the severity split, and this detector's scope limits. |

### Concepts

| Field | Type | Written by | Allowed values / notes |
|---|---|---|---|
| `concepts_status` | str | concepts success writer + `_record_concepts_error` | `ok` on success; `error: <message>` when the step failed (issue #129). Before #129 a concepts failure wrote **nothing at all** - no artifact and no field - so a video could be missing from `taxonomy.json` and the search index with no trace anywhere. The success path writes `ok` explicitly so a recovered video does not keep an old error forever. The failure writer never CREATES a meta that did not exist, because a caller that did not pass an explicit `prefix` could otherwise write a second meta claiming the same `video_id` and manufacture a dedupe group. |

### Skip and error bookkeeping

| Field | Type | Written by | Notes |
|---|---|---|---|
| `skip_modes` | list[str] | `mark-skip` | Per-mode skip (issue #42): any subset of `mindmap` / `transcript` / `concepts`. **Wins over `skip`** when both exist. |
| `skip` | bool | manual / legacy | Legacy full-skip. Honored only when `skip_modes` is absent. |
| `skip_reason` | str | `mark-skip` | Free-text bookkeeping for why a mode was skipped. |
| `last_error` | str \| null | every error path | The last error message, or `null`. |

## Minimal vs full meta

This section used to describe a live defect and end in a recommendation. **That recommendation shipped as an enforced contract in issue #66** - it is history now, kept because legacy files on disk still show the old shape.

**What used to happen.** The transcript-first writer (inverted ordering, issue #54) created a **minimal** meta carrying only `processed`, `transcript_status`, `modes_completed`, and `last_error`, before the scan loop filled in identity. A confabulation stub or a transcript-only run therefore left an **identity-less** meta with no `video_id`/`title`/`video_url`. `_load_video_id_index` skips such a file, so the video was re-transcribed on every scan.

**What happens now.** Every meta writer stamps identity on first write by merging `_transcript_identity_fields(video, channel_dir)` - `video_url`, `video_id`, `channel`, `title`, `published`. That covers the three transcript writers (single-shot success, salvage, captions failover), the concepts writers on both the success and failure paths (issue #129), and `cmd_mark_skip`. Falsy values are dropped so a re-stamp can only ADD identity, never downgrade a good field to empty.

**Reading old files.** Code that looks a video up by identity must still tolerate a legacy minimal meta and fall back to the slug, but must not produce new ones. `repair-metas --apply` backfills pre-#66 files from the `.transcript.md` header (Source URL to `video_id`); it never overwrites an existing field and never fabricates identity for a non-YouTube source.
