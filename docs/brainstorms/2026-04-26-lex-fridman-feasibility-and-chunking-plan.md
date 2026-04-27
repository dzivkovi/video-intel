# Lex Fridman channel feasibility + chunking-aware transcript - Phase 1 report

**Date:** 2026-04-26
**Author:** Daniel + Claude (CE pair)
**Trigger:** user wants Lex Fridman content available for cherry-picked one-off processing, not auto-pipeline cost.
**Evidence:** [`docs/plans/gate1-evidence/lex-fridman-feasibility-report.txt`](../plans/gate1-evidence/lex-fridman-feasibility-report.txt)

## What the dry-run revealed

Last 20 Lex uploads (covers ~9 months of content):

| Metric | Count |
|---|---|
| Standard YouTube Shorts (under 60s) | 0 |
| Lex-shorts (under 30 min, user's mental model) | 2 (the Khabib training clips: 2m57s, 22m9s) |
| Over 2h (transcript would be auto-skipped per PR #48) | **17** |
| Total runtime | 65h39m |
| Average length | **3h17m** |

The PR #48 transcript threshold (now 7200s = 2h after the bump) catches **17 of 20** Lex uploads. The standard Shorts filter (under 60s) catches **zero**, because Lex doesn't post shorts in the YouTube sense.

Translation for the user's mental model:

- **"Are shorts skipped?"** Yes - the standard filter works. But Lex doesn't post any.
- **"For Lex, shorts are anything under 30 min."** The 2 short Khabib clips (2m57s, 22m9s) currently slip through. To filter those automatically would need a new per-channel `min_duration_seconds` knob (not built).
- **"I just want notification, no auto-processing."** PR #48 doesn't have a config knob for "discover but don't mindmap." Closest existing options are `enabled: false` (suppresses scan entirely, can't notify) or `auto_transcript: none` (still auto-mindmaps - that's 17 mindmap calls per scan once Lex's backlog is in scope). Neither fits.

## What's missing for the user's intent

Three small follow-up additions, in priority order:

### A. `auto_mindmap: none` per-channel flag (Phase 2A)

Mirrors `auto_transcript: none`. When set, scan still discovers and logs new videos (notification effect), but the mindmap loop skips them. Combined with `auto_transcript: none`, the channel becomes notify-only.

```yaml
- name: lexfridman
  url: https://youtube.com/@lexfridman
  auto_mindmap: none      # NEW - notify only, no Gemini cost on scan
  auto_transcript: none
```

Estimated work: ~10 lines in `cmd_scan` + 3-4 tests.

### B. `min_duration_seconds` per-channel filter (Phase 2B)

Symmetric to `transcript_max_duration_seconds`. Per-channel only (not global default), applied alongside `skip_shorts` after `enrich_with_durations()`. For Lex: `min_duration_seconds: 1800` (30 min) drops the Khabib clips automatically.

```yaml
- name: lexfridman
  min_duration_seconds: 1800  # filter anything under 30min
```

Estimated work: ~5 lines + 2-3 tests. Tiny.

### C. Chunking-aware transcript (Phase 3 - bigger)

Stretch goal C from issue #42, deferred in PR #48. The user's specific target is `YFjfBk8HI5o` (Peter Steinberger #491, 3h15m52s = 11752s). At 50 min per chunk = 4 calls; at 100 min per chunk = 2 calls. Per-chunk Gemini call returns the same JSON shape (speech, screen_content, speakers); merger needs to:

1. Offset each chunk's timestamps by its `--start` value before emit.
2. Concatenate speech sections in chronological order.
3. Deduplicate `speakers` records across chunks (same name -> single record, merge timestamp ranges).
4. Concatenate `screen_content` sections in order.
5. Write one stitched `.transcript.md` plus a per-chunk coverage table at the top.
6. If any chunk fails, persist the partial result with a visible warning block (mirror the existing salvage pattern).

Estimated work: ~150-200 lines + ~10 tests. Real Gemini calls for validation. translate_video.py has the chunking + stitch pattern but for text concatenation; the JSON merge is harder.

This is a **separate PR**, not part of #48. Building on #48's branch would balloon scope past the issue text.

## Suggested next moves (when user is back)

**Tonight (autonomously possible if user gives go):**
- Land `auto_mindmap: none` (Phase 2A) on PR #48 OR a new branch.
- Land `min_duration_seconds` (Phase 2B) similarly.
- Update Lex's config to use both: notify-only mode with sub-30-min filter.

**Later (user-paced):**
- Phase 3 chunking PR. Validate against `YFjfBk8HI5o` end to end.
- Once chunking lands and produces a usable `transcript.md` for the Steinberger episode, that becomes the Phase 3 Gate 1 evidence.

**Today's specific Lex video request:**
- The user wants `YFjfBk8HI5o` (Steinberger #491) fully indexed.
- With current code: `mindmap --url ... --channel lexfridman` works (one Gemini call, ~3 min, small JSON output, no truncation risk on 3h video). Concepts derive from mindmap. Search/nugget will find it.
- Transcript: blocked by PR #48 threshold. Workarounds: (a) `transcript --url ... --start 0 --end 7200` for first 2h, (b) wait for chunking PR.
- **Recommendation:** mindmap + concepts now (partial-but-useful indexing), transcript via chunking after Phase 3 lands.

## Open questions (pending user)

1. Build Phase 2 (auto_mindmap, min_duration_seconds) tonight or wait?
2. Build Phase 3 (chunking) tonight or as a separate session?
3. Process `YFjfBk8HI5o` mindmap+concepts tonight (real Gemini cost ~$0.05) or wait?

## Design notes for Phase 3 (so future-me has the head start)

### Chunking strategy
- Chunk size 50min default (matches translate_video.py's `--high-res` cap). User can override via `--chunk-minutes`.
- Boundaries are `--start`/`--end` offsets passed to Gemini's `VideoMetadata`. Same primitive used by manual segment recovery today.
- Token budget per chunk: ~20-30K tokens. Even at low-quality settings, 50min of audio is well within `MAX_OUTPUT_TOKENS=65536`.

### JSON merge invariants
- Each chunk's response has its own clock starting at 0:00. The merger must offset every timestamp by the chunk's `--start` before emit.
- Speakers: dedupe by `name` field. If the same speaker appears in multiple chunks, take union of `evidence_timestamps`.
- Screen content: each `[mm:ss-mm:ss]` block gets offset; concatenate chronologically.
- Speech: segments from each chunk concatenated in order. Speaker-label resolution stays per-chunk (no need to re-resolve across chunks).

### Failure modes to plan for
- Chunk N succeeds, chunk N+1 fails: persist N as `transcript.md`, write `transcript.partial.md` for N+1's available output, mark meta as `transcript_status: "partial"`.
- All chunks fail: same forensic sidecar pattern as PR #48's salvage (`.transcript.raw.<chunkN>.txt`).
- Inter-chunk drift: if chunk 1's speakers differ in label from chunk 2 (Gemini gets cute and renames "Speaker 1" -> "Peter Steinberger" in chunk 2), the merger should accept both labels but warn.

### Coverage table (top of stitched transcript)

```markdown
**Source:** chunked transcript, 4 segments
**Total duration:** 3h15m52s
**Coverage:**

| Segment | Range | Status | Tokens |
|---|---|---|---|
| 1 | 0:00 - 50:00 | ok | 12345 |
| 2 | 50:00 - 1:40:00 | ok | 11890 |
| 3 | 1:40:00 - 2:30:00 | ok | 12102 |
| 4 | 2:30:00 - 3:15:52 | ok | 10543 |
```

### Test plan sketch
- `test_chunk_offsets_applied_correctly` - verify each chunk's timestamps are offset by `--start`.
- `test_speaker_dedup_across_chunks` - same speaker in 2 chunks merges to one record.
- `test_partial_chunk_failure_persists_completed_chunks` - chunks 1-3 ok, chunk 4 fails -> stitched output has chunks 1-3 + warning.
- `test_chunk_size_capped_by_max_output_tokens` - verify chunks don't exceed budget (no math overflow).
- `test_coverage_table_emitted_in_stitched_output` - regression for the user-facing artifact.
- Integration test: real Gemini call against `YFjfBk8HI5o` (or a known shorter test video) - validates end-to-end against actual API behavior.
