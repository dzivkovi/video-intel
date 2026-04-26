# Per-mode skip + long-video transcript guard - Requirements

**GitHub issue:** [#42](https://github.com/dzivkovi/video-intel/issues/42)
**Status:** in-flight (overnight CE run)
**Author:** Daniel + Claude (CE pair)
**Date:** 2026-04-26

## What and why

Two related gaps in `cmd_scan` cost ~6.5 hours of wasted wall-clock on a 2h 24m video (`X5UN2LrRK48`, Sean Kochel "Build Your First SaaS App... Part 1"):

1. **Long-video transcript path silently fails.** `MAX_OUTPUT_TOKENS=65536` truncates the structured-JSON transcript response on videos over ~90 minutes. Salvage logic recovers small truncations; full mid-stream cutoff cannot be salvaged. Worse, occasionally the call hangs without surfacing an error - the httpx timeout never fires - and the `ThreadPoolExecutor.as_completed` loop blocks indefinitely. Auto-concepts never starts.

2. **Skip control is all-or-nothing per video.** Today `meta.skip = true` (read by `is_skipped()` at lines 2159, 2165, 2230 of `cmd_scan`, plus the auto-concepts loop at 2279, plus `cmd_mindmap`/`cmd_transcript`/`cmd_process` file-recovery paths at 2365, 2544, 2766) suppresses everything. There is no way to say "this video's mindmap is fine - just don't try transcript ever again." Manually marking `skip: true` is the workaround but is too coarse: it also blocks the concepts pass that depends on the existing mindmap.

The goal is to make scan robust to long-video outliers without losing their mindmap (which works fine, since mindmap output is small) and to give the user a per-mode skip primitive when transcript is genuinely impossible.

## Acceptance criteria (lifted from issue + tightened)

- [ ] `transcript_max_duration_seconds` config option (top-level; default `5400` = 90 min) respected during scan.
- [ ] Skipped long videos log a clear WARNING with the manual recovery recipe:
  ```
  [seankochel] Skipping transcript for "Build Your First SaaS..." (2h24m42s > 90m).
    To process manually with clipping: transcript --url URL --start 0 --end 5400
  ```
  Mindmap phase is unaffected. Concepts phase reads the existing mindmap and proceeds.
- [ ] `meta.skip_modes: ["transcript"]` works on a video that has a done mindmap; subsequent scans skip the transcript phase but not the mindmap or concepts loops. Concepts can still run (it reads from the existing mindmap, not from transcript).
- [ ] **Backward compat:** existing `skip: true` files behave as full-skip (all modes). Read order: if `skip_modes` exists, use it; else if `skip == True`, treat as `skip_modes = ["mindmap", "transcript", "concepts"]`; else nothing skipped.
- [ ] CLI `mark-skip --url URL --mode transcript [--reason TEXT]` writes the `skip_modes` array into the meta.json without hand-editing JSON. Multiple `--mode` flags allowed (`--mode transcript --mode concepts`).
- [ ] Tests covering: threshold filter, log message format, per-mode skip behavior on each of the three loops (mindmap, transcript, concepts), backward-compat `skip: true`, mark-skip CLI happy path.
- [ ] CLAUDE.md `## Code Review Guardrails` updated with the new per-mode skip contract and the long-video guard.
- [ ] SKILL.md (curate skill) updated in the same diff with the new `mark-skip` command and the `transcript_max_duration_seconds` config knob.

## Non-goals (deferred)

- **Stretch C (chunking-aware transcript):** Issue explicitly defers it pending evidence that A+B are insufficient. The 90-minute threshold + manual `--start`/`--end` recipe should cover the rare 2h+ outlier. If evidence accumulates that this is hit weekly, file a follow-up issue.
- **Hang detection / timeout fix:** The httpx-timeout-doesn't-fire issue is real but orthogonal. The threshold filter avoids the hang by never sending the long video in the first place. Detection of the underlying httpx misbehavior is its own investigation.
- **Per-mode skip on `cmd_dedupe`'s skip-flag check:** Dedupe doesn't currently inspect `skip` (it groups by `video_id` regardless), so no change needed.
- **Per-channel `transcript_max_duration_seconds` override:** Default is per-config; channel-level override is not in the issue. If the YouTube backlog has long-form-only creators (lengthy podcasts), that's a follow-up. Today, all configured channels are short-form-friendly.

## Design decisions / Why these choices

- **`skip_modes` as an array, not a bitfield or per-mode booleans.** Arrays are readable in JSON, extensible (if a new mode lands, no schema change), and roundtrip cleanly through `json.dumps`. The valid-mode validation in `mark-skip` keeps typos out.
- **Threshold check after `enrich_with_durations()`, before the transcript ThreadPoolExecutor.** Durations are already fetched in the existing skip-shorts path (line 2138). Reusing that data avoids a second YouTube API call. The filter is a list comprehension - single line.
- **Threshold default 5400s (90 min).** From issue. Empirically 2h+ videos hit truncation; 90m gives generous headroom for the typical 60-90m podcast that does succeed. Configurable so users with longer-form-friendly creators can raise it.
- **`is_skipped(..., mode="transcript")` over a brand-new `is_mode_skipped`.** Adding an optional kwarg preserves backward compat for the three current call sites (line 2159 mindmap-filter, line 2165 mindmap-filter-with-force, line 2230 transcript-filter). Existing callsites keep working unchanged; only the transcript filter passes the new kwarg.
- **`mark-skip` CLI** - one-shot ergonomic. Rule-of-three doesn't apply (only one place needs it), but the issue explicitly asks for it, and hand-editing JSON in 17 places per cleanup is friction the user will hit repeatedly. Cheaper to ship the helper than to expect text editing.

## Affected code map

- `scripts/video_intel.py`:
  - Lines 1213-1220: `is_skipped()` - add `mode` kwarg, evaluate `skip_modes` array with backward-compat fallback to boolean `skip`.
  - Lines 2138-2155: existing `enrich_with_durations` block - extend to capture parsed seconds for all videos.
  - **NEW** insertion after line 2155: long-video transcript filter (compute `transcript_max_duration_seconds`, drop > threshold from `transcript_videos` candidate set, log WARNING with recipe).
  - Line 2159, 2165, 2230: pass `mode="mindmap"` / `mode="transcript"` to `is_skipped()`.
  - Line 2279 (auto_concepts): replace `meta.get("skip")` direct read with `is_skipped_meta(meta, mode="concepts")` helper. Same backward-compat semantics.
  - Lines 2365, 2544, 2766: file-recovery paths - replace `existing.get("skip")` with `is_skipped_meta(existing, mode=...)`. (mindmap path → mode=mindmap, transcript path → mode=transcript, process path checks both per-mode.)
  - **NEW** `cmd_mark_skip(args, config)` and argparse subparser `mark-skip`.
- `tests/test_skip_long_videos.py` (new file): all new test classes.
- `CLAUDE.md`: append new bullet under `## Code Review Guardrails` documenting the `is_skipped(mode=...)` contract and the threshold filter ordering invariant.
- `skills/video-intel/SKILL.md`: add `mark-skip` row to Interpreting User Intent table; add config knob to Configuration section.

## Risk + Mitigation

- **Risk:** A user with `skip: true` set on many videos starts seeing different behavior on next scan. **Mitigation:** Backward compat is exact: `skip: true` continues to mean "skip all modes." Tests assert this.
- **Risk:** Threshold filter accidentally drops a 91-minute video that would have succeeded. **Mitigation:** WARNING log contains the recipe. User can either raise the threshold in config or run the manual `transcript --url --start 0 --end <N>` recipe.
- **Risk:** Concepts loop reads `meta.get("skip")` and a meta with `skip_modes: ["transcript"]` still gets concepts processed. That's the desired behavior, but if the loop was previously relying on "skip" alone, the new `is_skipped_meta(meta, mode="concepts")` call must return False for the per-mode case. **Mitigation:** test `test_concepts_runs_when_only_transcript_skipped` covers this.
- **Risk:** `mark-skip` accepts an unknown mode name. **Mitigation:** validate against `{"mindmap", "transcript", "concepts"}`; argparse choices=.

## Open questions

(None at requirements time. If implementation surfaces an ambiguity, append here and continue with the safer interpretation.)

## Test plan (RED targets)

1. `TestIsSkippedHelper`
   - `test_returns_false_when_no_meta`
   - `test_returns_true_when_legacy_skip_true_for_any_mode`
   - `test_returns_true_when_skip_modes_contains_mode`
   - `test_returns_false_when_skip_modes_lacks_mode`
   - `test_skip_modes_takes_precedence_over_boolean_skip`
2. `TestLongVideoTranscriptGuard`
   - `test_video_over_threshold_dropped_from_transcript_only`
   - `test_video_under_threshold_kept_for_transcript`
   - `test_warning_message_includes_url_and_recipe`
   - `test_threshold_default_is_5400_seconds`
   - `test_config_threshold_override_respected`
   - `test_mindmap_loop_unaffected_by_threshold`
3. `TestPerModeSkipCmdScan`
   - `test_skip_modes_transcript_only_blocks_transcript_keeps_mindmap`
   - `test_skip_modes_transcript_only_keeps_concepts`
   - `test_legacy_skip_true_still_blocks_all_modes`
4. `TestMarkSkipCli`
   - `test_writes_skip_modes_array_to_meta`
   - `test_appends_to_existing_skip_modes_array`
   - `test_records_reason_field_when_provided`
   - `test_validates_mode_choice`
   - `test_handles_missing_meta_with_helpful_error`

## Out-of-scope drift

If at any point I'm tempted to fix the httpx-hang root cause inline, stop. That's a separate failure mode; the threshold filter prevents it from being hit. File a follow-up if the hang shows up on under-threshold videos.

If at any point I'm tempted to add `enabled: false`-style channel-level threshold override, stop. The issue scopes it global; we'll learn whether per-channel is needed before adding the knob.
