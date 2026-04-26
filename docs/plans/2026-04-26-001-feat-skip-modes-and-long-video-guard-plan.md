# Implementation plan - per-mode skip + long-video transcript guard

**Source requirements:** `docs/brainstorms/2026-04-26-skip-modes-and-long-video-guard-requirements.md`
**Issue:** [#42](https://github.com/dzivkovi/video-intel/issues/42)
**Branch:** `feat/issue-42-skip-controls`

## Strategy

Three small, independent units. Each unit has its own RED-GREEN cycle. Compose at the end. Touchpoints are concentrated in `cmd_scan` and one new helper - the file-recovery paths and concepts loop are shallow integrations.

## Unit 1 - `is_skipped()` accepts `mode` kwarg + `is_skipped_meta()` helper for in-memory dicts

### Approach

Replace the simple boolean check at line 1213-1220 with a mode-aware version. Backward compat: `skip: true` (legacy boolean) is treated as `skip_modes = ["mindmap", "transcript", "concepts"]`. Add a sibling `is_skipped_meta(meta_dict, mode)` for callers that already have the parsed meta in memory (auto-concepts loop, file-recovery paths).

### Contract

```python
SKIP_MODES_VALID = ("mindmap", "transcript", "concepts")

def is_skipped_meta(meta: dict, mode: str | None = None) -> bool:
    """True when the video should be skipped for the given mode.

    Resolution order:
      1. If meta has skip_modes (a list), check membership.
      2. Else if meta has skip == True (legacy), treat as full-skip.
      3. Else False.

    mode=None preserves "any skip" semantics: returns True if either
    the legacy boolean is set OR skip_modes is non-empty.
    """

def is_skipped(output_dir, channel_name, video, mode: str | None = None) -> bool:
    """Disk-backed wrapper over is_skipped_meta. Backward compatible."""
```

### RED tests (`tests/test_skip_long_videos.py`)

`TestIsSkippedHelper`:
- `test_returns_false_when_no_meta`
- `test_legacy_skip_true_returns_true_for_any_mode`
- `test_skip_modes_array_returns_true_for_listed_mode`
- `test_skip_modes_array_returns_false_for_unlisted_mode`
- `test_skip_modes_takes_precedence_over_legacy_boolean`
- `test_no_mode_arg_returns_true_when_any_skip_present`

### GREEN

- Add module constant `SKIP_MODES_VALID`.
- New `is_skipped_meta(meta: dict, mode: str | None) -> bool`.
- Refactor `is_skipped(...)` to read meta.json then call `is_skipped_meta`.
- Existing callers without `mode=` keep working (they get the "any skip" semantics, which is what they had).

## Unit 2 - Long-video transcript guard in `cmd_scan`

### Approach

After `enrich_with_durations()` (existing line 2138-2155), and after the shorts filter, derive a per-video parsed-seconds dict. Inside the auto_transcript=="all" block (line 2225), filter `transcript_videos` against the threshold. Log the warning for each dropped video. Mindmap loop is upstream and untouched.

### Contract

```python
TRANSCRIPT_MAX_DURATION_DEFAULT = 5400  # 90 minutes

# Inside cmd_scan, after the shorts filter and before auto_transcript block:
threshold = config.get("transcript_max_duration_seconds", TRANSCRIPT_MAX_DURATION_DEFAULT)

# Inside the auto_transcript=="all" block, replace:
#   transcript_videos = [v for v in videos if not is_processed(...) and not is_skipped(...)]
# with:
candidates = [v for v in videos if not is_processed(output_dir, ch_name, v, "transcript")
                                 and not is_skipped(output_dir, ch_name, v, mode="transcript")]
transcript_videos = []
for v in candidates:
    duration_s = _parse_iso8601_duration(v.get("duration_iso"))
    if duration_s is not None and duration_s > threshold:
        log.warning(
            '[%s] Skipping transcript for "%s" (%s > %dm).',
            ch_name, v["title"], _fmt_hms(duration_s), threshold // 60,
        )
        log.warning(
            "  To process manually with clipping: transcript --url %s --start 0 --end %d",
            v["url"], threshold,
        )
        continue
    transcript_videos.append(v)
```

`_fmt_hms(seconds: int) -> str` formats `8682 -> "2h24m42s"`. New small helper.

### RED tests

`TestLongVideoTranscriptGuard`:
- `test_video_over_threshold_dropped_from_transcript_only` - mindmap still hits process_mindmap, transcript skipped.
- `test_video_under_threshold_kept_for_transcript` - both loops process the video.
- `test_warning_message_includes_url_and_recipe` - capture log via `caplog`.
- `test_default_threshold_5400_seconds`
- `test_custom_threshold_in_config_respected`
- `test_video_with_unparseable_duration_kept_fail_safe` - if `_parse_iso8601_duration` returns None, do not drop (better to attempt and fail visibly than silently skip).

### GREEN

- Add module constant `TRANSCRIPT_MAX_DURATION_DEFAULT = 5400`.
- Add `_fmt_hms()` near `_parse_iso8601_duration()`.
- Modify `cmd_scan` `auto == "all"` block: change the comprehension to a loop with the threshold check.
- Mindmap loop remains untouched.

## Unit 3 - `mark-skip` CLI subcommand

### Approach

New `cmd_mark_skip(args, config)` that locates the meta.json by URL, validates the mode, appends to `skip_modes` (creating it if absent), records `skip_reason` if `--reason` given, writes via `update_meta(..., mode="identity")` so `modes_completed` is untouched.

### Contract

```python
def cmd_mark_skip(args, config) -> None:
    """Mark a video to skip for one or more modes without hand-editing meta.json.

    Resolves video_id from --url. Walks all configured channels' folders to find
    the meta.json. Errors clearly if not found. Validates each --mode value.
    """
```

CLI:
```
python video_intel.py mark-skip --url URL --mode transcript [--mode concepts] [--reason "OOM truncation"]
```

argparse:
- `--url` (required)
- `--mode` (required, `action="append"`, `choices=("mindmap", "transcript", "concepts")`)
- `--reason` (optional string)

### RED tests

`TestMarkSkipCli`:
- `test_writes_skip_modes_array_to_meta`
- `test_appends_to_existing_skip_modes_without_duplicates`
- `test_records_reason_field_when_provided`
- `test_handles_missing_meta_with_helpful_error`
- `test_argparse_rejects_unknown_mode`

### GREEN

- New `cmd_mark_skip()`.
- argparse subparser entry between `prune-shorts` and the parse_args call.
- Add to dispatcher elif chain.
- Add SKILL.md row for the new command.

## Unit 4 - Wire `mode=` through three call sites + concepts loop + file-recovery paths

### Sites

1. Line 2159 (mindmap force branch): `is_skipped(output_dir, ch_name, v, mode="mindmap")`
2. Line 2165 (mindmap normal branch): `is_skipped(..., mode="mindmap")`
3. Line 2230 (transcript filter): `is_skipped(..., mode="transcript")` (this becomes the per-iteration check inside the new loop from Unit 2; keep parity)
4. Line 2279 (auto-concepts loop reads meta in-memory): `is_skipped_meta(meta, mode="concepts")`
5. Lines 2365 (`cmd_mindmap` file-recovery): `is_skipped_meta(existing, mode="mindmap")`
6. Lines 2544 (`cmd_transcript` file-recovery): `is_skipped_meta(existing, mode="transcript")`
7. Lines 2766 (`cmd_process` file-recovery): currently checks `existing_meta.get("skip")` once; replace with `is_skipped_meta(existing_meta)` (no mode - process orchestrates both modes; full-skip semantics preserve current behavior).

`cmd_process` deserves a thought: it bundles mindmap+transcript+concepts. If `skip_modes: ["transcript"]` is set, should `cmd_process --file` still run mindmap and concepts? Decision: yes. `process` is the orchestrator; it should respect per-mode skip just like `cmd_scan` does. Implementation: at line 2796 (`needs_mindmap`) and 2797 (`needs_transcript`), AND in `is_skipped_meta(existing_meta, mode=<per-mode>)`. If neither mindmap nor transcript is needed (after per-mode skip + lazy-skip), exit cleanly.

### RED tests

`TestPerModeSkipCmdScan`:
- `test_skip_modes_transcript_only_keeps_mindmap` - mindmap loop still calls process_mindmap; transcript loop does not.
- `test_skip_modes_transcript_only_keeps_concepts` - auto-concepts loop processes the video (mindmap exists on disk).
- `test_legacy_skip_true_blocks_all_loops` - mindmap, transcript, concepts all skip.

`TestPerModeSkipCmdProcess` (lighter; mark TODO if scope creeps):
- `test_process_with_skip_modes_transcript_runs_mindmap_only`

### GREEN

Five line changes (one per call site) plus the `cmd_process` per-mode plumbing.

## Order of operations

1. **Unit 1** (`is_skipped_meta` helper) - foundation; nothing else lands until tests pass.
2. **Unit 4** (rewire existing call sites with `mode=`) - small, mostly mechanical, validates Unit 1 against real callers.
3. **Unit 2** (long-video guard) - independent of skip plumbing; just adds a filter. Low blast radius.
4. **Unit 3** (`mark-skip` CLI) - last because it's the user-facing surface.
5. **Skill-parity update** - same diff as Unit 3.
6. **CLAUDE.md guardrail** - same diff as everything else.

## Validation gates

- `ruff format . && ruff check . --fix` - clean.
- `pytest -m "not integration" -q` - all green, including the new test file.
- **Gate 1 smoke test:** simulate by manually constructing a meta.json with `skip_modes: ["transcript"]`, dropping a fake mindmap+concepts.json beside it, then running `python scripts/video_intel.py scan --channel <test_channel> --dry-run` to confirm the mindmap+concepts paths see the video while transcript filtering reports it skipped. Augmented by a real `mark-skip` invocation that writes the meta and a follow-up `cat` to show the JSON.

## Out-of-scope reminders

- Do not touch `cmd_dedupe` skip semantics (it doesn't read skip).
- Do not add chunking-aware transcript path - issue defers it.
- Do not add per-channel `transcript_max_duration_seconds` override.
- Do not investigate the httpx-hang root cause; the threshold filter prevents the trigger.
