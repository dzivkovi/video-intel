# Issue #50 implementation plan - process --url + chunked transcript

**Source issue:** [#50](https://github.com/dzivkovi/video-intel/issues/50)
**Branch:** `feat/issue-50-chunked-transcript`
**Date:** 2026-04-26

## What's already in place from PR #48

- `--start`/`--end` on `transcript --url` — single-segment clipping works.
- `transcript_max_duration_seconds` per-channel override - the safety net.
- Lex Fridman in notify-only mode (auto_mindmap=none, auto_transcript=none, min_duration_seconds=1800).
- `is_skipped_meta` per-mode contract for incremental re-runs.

## Two gaps to close

### Gap 1: Chunked transcript

Today the transcript path bombs on 3h+ videos because the structured-JSON response truncates beyond what salvage can recover. Manual `--start`/`--end` works for a single segment but each call overwrites the same `.transcript.md` (prefix derived from video_id), so multi-segment requires hand-renaming + hand-stitching.

`translate_video.py` already solved the equivalent problem for translation:
- `build_chunk_list(duration_seconds, chunk_minutes)` - segmentation primitive
- `stitch_parts(part_dir, ...)` - merge primitive with timestamp offset application
- `--chunk-minutes` flag (default 20) and `--stitch` flag

Port the pattern. The only categorical difference: transcript output is structured JSON (three sections: `transcripts`, `screen_content`, `speakers`), not plain text. The merge has to be JSON-aware.

### Gap 2: `process --url` orchestrator

`process --file` exists for local MP4s. No equivalent for YouTube URLs - users have to chain three commands. Single-shot URL command unblocks the user's "fully index this URL" use case.

## Design - chunked transcript merge

### Input

For each chunk i: `(chunk_start_seconds, chunk_json)` pair.
- `chunk_start_seconds`: the `--start` value passed to Gemini for this chunk.
- `chunk_json`: parsed Gemini response with three keys (`transcripts`, `screen_content`, `speakers`).

### Algorithm

```python
def merge_chunked_transcripts(
    chunks: list[tuple[int, dict]],  # [(start_seconds, json), ...]
) -> dict:
    """Merge per-chunk transcript JSONs into a single transcript JSON
    with offset-applied timestamps and globally-unique speaker voice ids."""
    merged = {"transcripts": [], "screen_content": [], "speakers": []}
    voice_remap: dict[tuple[int, str], int] = {}  # (chunk_idx, original_voice) -> new_voice
    name_to_global: dict[str, int] = {}  # name -> global voice id (dedup across chunks)
    next_global = 1

    for chunk_idx, (start_secs, chunk_json) in enumerate(chunks):
        # 1. Build this chunk's voice-to-name map; remap to global voice ids.
        for s in chunk_json.get("speakers", []):
            name = s.get("name", f"Speaker {s['voice']}")
            if name not in name_to_global:
                name_to_global[name] = next_global
                next_global += 1
                merged["speakers"].append({**s, "voice": name_to_global[name]})
            voice_remap[(chunk_idx, s["voice"])] = name_to_global[name]

        # 2. Offset transcripts and remap voice ids.
        for t in chunk_json.get("transcripts", []):
            offset_ts = _offset_timestamp(t["start"], start_secs)
            new_voice = voice_remap.get((chunk_idx, t.get("voice")), t.get("voice"))
            merged["transcripts"].append({**t, "start": offset_ts, "voice": new_voice})

        # 3. Offset screen_content.
        for sc in chunk_json.get("screen_content", []):
            new_sc = {**sc, "start": _offset_timestamp(sc["start"], start_secs)}
            if sc.get("end"):
                new_sc["end"] = _offset_timestamp(sc["end"], start_secs)
            merged["screen_content"].append(new_sc)

    return merged
```

### Coverage table at top of stitched output

```markdown
**Source:** chunked transcript, 4 segments (chunk_minutes=50)
**Total duration:** 3h15m52s

| Segment | Range | Status | Speakers |
|---|---|---|---|
| 1 | 0:00 - 50:00 | ok | Lex Fridman, Peter Steinberger |
| 2 | 50:00 - 1:40:00 | ok | Lex Fridman, Peter Steinberger |
| 3 | 1:40:00 - 2:30:00 | ok | Lex Fridman, Peter Steinberger |
| 4 | 2:30:00 - 3:15:52 | ok | Lex Fridman, Peter Steinberger |
```

### Failure modes

- Chunk N fails (parse error, MAX_TOKENS, network): persist chunks 0..N-1 with a visible H2 warning block (mirror PR #48's salvage pattern). Forensic sidecars `.transcript.raw.<chunkN>.txt` for failed chunks. Mark meta `transcript_status: "partial"`.
- Inter-chunk speaker drift: Gemini might rename "Speaker 1" → "Peter Steinberger" mid-stream. Voice dedup is by NAME, so v1 just produces both records and a WARNING. v2 could fuzzy-match.
- Total failure (all chunks fail): write `.transcript.raw.txt` with concatenated raw responses, no `.transcript.md`. Same disposition as PR #48 single-call truncation.

## Design - `process --url`

Mirror `process --file`'s flow. Differences:
- No upload step (Gemini fetches the URL directly from `https://www.youtube.com/watch?v=<id>`).
- `cmd_process` already handles partial-success and lazy-skip; reuse the same orchestration.
- For chunking decision: read duration from `enrich_with_durations()` once, pass through to transcript step.

```bash
python scripts/video_intel.py process --url "https://www.youtube.com/watch?v=YFjfBk8HI5o" --channel lexfridman
```

→ mindmap → (chunked) transcript → concepts. Exit 0 if mindmap succeeded.

## Implementation order

1. `_build_chunk_list` + `_offset_timestamp` helpers + tests.
2. `merge_chunked_transcripts` + tests.
3. Coverage-table renderer + tests.
4. Integrate into `cmd_transcript` URL path: when duration > chunk threshold, run chunks in sequence and merge.
5. `cmd_process_url` (or extend `cmd_process` to accept `--url` mutually exclusive with `--file`).
6. Update SKILL.md (`process --url` row + chunking note) + CLAUDE.md guardrails.
7. STOP before real Gemini smoke. User greenlights smoke against `YFjfBk8HI5o`.

## Out-of-scope reminders

- Coverage/quality gating (the "Lenny analysis" the user pasted earlier). Separate follow-up.
- Speaker fuzzy-matching across chunks. v1 dedupes by exact name match.
- Per-chunk safety filter overrides. v1 reuses the existing permissive safety settings.
- High-res media resolution (translate has it; transcript prompt is audio-focused, default low-res is fine).

## Open questions

(None at requirements time. Will append if implementation surfaces ambiguity.)
