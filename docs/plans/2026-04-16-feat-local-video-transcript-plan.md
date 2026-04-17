---
title: "feat: Local MP4 transcription with segment clipping"
type: feat
status: completed
date: 2026-04-16
---

# feat: Local MP4 transcription with segment clipping

## Overview

Extend `video_intel.py transcript` to accept a **local MP4 file path** via
`--file` with optional segment clipping via `--start` / `--end`. Reuses all
existing prompt + JSON resilience infrastructure; adds Gemini Files API upload
and segment-clipped multimodal calls. Output lands next to the source file as
`{name}.transcript.md`.

Brainstorm reference:
[docs/brainstorms/2026-04-16-local-video-transcript-brainstorm.md](../brainstorms/2026-04-16-local-video-transcript-brainstorm.md)

## Problem Statement / Motivation

Today the `transcript` command only works with YouTube URLs. Users with their
own recordings (screen-sharing demos, meeting replays) have to upload to
YouTube first just to transcribe them - not practical for private or sensitive
content. Gemini's Files API supports direct local uploads up to 2GB with
segment clipping, so the existing three-task diarized transcript prompt can
work on local files with minimal new code.

## Proposed Solution

```bash
# Local file, full content
python scripts/video_intel.py transcript --file meeting.mp4

# Local file with segment (required for files > 500MB)
python scripts/video_intel.py transcript \
  --file meeting.mp4 --start 05:30 --end 18:45

# Works with Dropbox / Google Drive sync folders (they're local paths)
python scripts/video_intel.py transcript \
  --file ~/Dropbox/recordings/demo.mp4 --start 02:00 --end 15:00
```

Output: `meeting.mp4 → meeting.transcript.md` (and `meeting.meta.json`) in the
same directory.

## Technical Approach

### Four new functions

**1. `parse_time_to_seconds(value: str) -> int`** (scripts/video_intel.py, new)

```python
def parse_time_to_seconds(value: str) -> int:
    """Parse time string to seconds. Accepts 'MM:SS', 'HH:MM:SS', or raw seconds."""
```

- `"05:30"` → 330
- `"01:15:45"` → 4545
- `"330"` → 330
- Invalid input: raises `ValueError` with clear message
- Placed near existing `timestamp_to_seconds` (line 656) for discoverability
- `timestamp_to_seconds` stays as-is (used internally by `merge_transcript_json`)

**2. `upload_local_video(client, path: Path) -> str`** (scripts/video_intel.py, new)

```python
LARGE_FILE_THRESHOLD_BYTES = 500 * 1024 * 1024  # 500MB

def upload_local_video(client, path: Path) -> str:
    """Upload a local video to Gemini Files API, return the file URI."""
```

- Validates file exists, is readable, is an MP4 (warn on other extensions)
- Uses `client.files.upload(file=path)` → returns `file.uri`
- Logs progress ("Uploading video... done. URI: files/xyz")
- No lifecycle management (48h auto-expire per brainstorm)
- Raises `FileNotFoundError` / `ValueError` with clear messages

**3. `call_gemini` - add optional offsets** (scripts/video_intel.py:465)

Extend signature:

```python
def call_gemini(
    client, types, video_url, prompt_text, model,
    response_json=False,
    *,
    start_offset: int | None = None,
    end_offset: int | None = None,
):
```

Mirror `translate_video.py:1206-1214` pattern:

```python
part_kwargs = {"file_data": types.FileData(file_uri=video_url)}
if start_offset is not None or end_offset is not None:
    meta_kwargs = {}
    if start_offset is not None:
        meta_kwargs["start_offset"] = f"{start_offset}s"
    if end_offset is not None:
        meta_kwargs["end_offset"] = f"{end_offset}s"
    part_kwargs["video_metadata"] = types.VideoMetadata(**meta_kwargs)

contents = types.Content(
    parts=[types.Part(**part_kwargs), types.Part(text=prompt_text)]
)
```

**4. `process_transcript` - refactor output path contract** (scripts/video_intel.py:805)

Change signature from `(output_dir, channel_name)` to explicit path components:

```python
def process_transcript(
    client, types, video, prompt_text, model,
    channel_dir: Path,    # directory for transcript/meta/raw files
    prefix: str,           # filename prefix (without extension)
    *, force=False,
    start_offset: int | None = None,
    end_offset: int | None = None,
):
```

Caller computes paths based on source:
- **YouTube:** `channel_dir = output_dir / channel_name`, `prefix = video_file_prefix(video)`
- **Local file:** `channel_dir = input_path.parent`, `prefix = input_path.stem`

This breaks one internal contract but touches only 2 callers (`cmd_transcript`,
`cmd_scan` auto-transcript path). Pass `start_offset` / `end_offset` through to
`call_gemini` on both attempts (initial + retry).

### `cmd_transcript` branching

Mutually exclusive argparse group (pattern from `translate_video.py:2526-2550`):

```python
source = tx_parser.add_mutually_exclusive_group(required=True)
source.add_argument("--url", help="YouTube video URL")
source.add_argument("--file", type=Path, help="Path to local MP4 file")
tx_parser.add_argument("--start", help="Segment start time (MM:SS, HH:MM:SS, or seconds)")
tx_parser.add_argument("--end", help="Segment end time (MM:SS, HH:MM:SS, or seconds)")
```

In `cmd_transcript`:

```python
if args.url:
    # Existing YouTube flow, unchanged
    # Build video dict, channel_dir = output_dir / channel_name, prefix = video_file_prefix(video)
elif args.file:
    input_path: Path = args.file.resolve()
    if not input_path.exists():
        log.error("File not found: %s", input_path)
        sys.exit(1)

    size = input_path.stat().st_size
    has_segment = args.start is not None or args.end is not None
    if size > LARGE_FILE_THRESHOLD_BYTES and not has_segment:
        log.error(
            "File is %.1fGB. Specify --start and --end to transcribe a segment "
            "(Gemini's 2GB upload limit applies).",
            size / 1024 / 1024 / 1024,
        )
        sys.exit(1)

    start_seconds = parse_time_to_seconds(args.start) if args.start else None
    end_seconds = parse_time_to_seconds(args.end) if args.end else None

    file_uri = upload_local_video(client, input_path)

    video = {
        "video_id": input_path.stem,
        "url": file_uri,
        "title": input_path.stem,
        "published": datetime.fromtimestamp(input_path.stat().st_mtime).strftime("%Y-%m-%d"),
    }
    channel_dir = input_path.parent
    prefix = input_path.stem

process_transcript(
    client, types, video, prompt_text, model, channel_dir, prefix,
    force=args.force,
    start_offset=start_seconds if args.file else None,
    end_offset=end_seconds if args.file else None,
)
```

### Concepts / search exclusion

No code changes needed. `cmd_concepts` iterates `config["channels"]`; local
transcripts aren't tied to a channel, so they're naturally excluded. Same for
the `index` / `search` commands which walk configured channel directories.

## Acceptance Criteria

### Functional Requirements

- [ ] `transcript --file meeting.mp4` uploads and transcribes a small file
- [ ] `transcript --file meeting.mp4 --start 05:30 --end 18:45` clips segment
- [ ] `transcript --file big.mp4` (>500MB without segment) errors with clear message
- [ ] Output lands at `meeting.transcript.md` in same dir as `meeting.mp4`
- [ ] `meeting.meta.json` sibling file created with processing info
- [ ] `--url` and `--file` are mutually exclusive; one is required
- [ ] Time args accept `MM:SS`, `HH:MM:SS`, and raw seconds (e.g., `"330"`)
- [ ] Invalid time strings fail with clear error message
- [ ] YouTube `transcript --url` path unchanged (backward compat)
- [ ] JSON resilience (salvage, retry, raw sidecar) works on local-file path
- [ ] `--model` / `-m` CLI flag works for both YouTube and local

### Non-Functional Requirements

- [ ] All new functions have type hints (project convention)
- [ ] Unit tests for `parse_time_to_seconds` (valid + invalid inputs)
- [ ] Unit tests for `upload_local_video` (mock client, missing file, too large)
- [ ] Extend `TestProcessTranscriptResilience` with new signature
- [ ] Integration test for `cmd_transcript` with `--file` (mocked upload + gemini)
- [ ] Existing 370 tests still pass (no regressions)
- [ ] Ruff format + lint clean

### Documentation

- [ ] SKILL.md: add `--file` / `--start` / `--end` example in "How to Use"
- [ ] CLAUDE.md: note local file path in architecture section
- [ ] README.md: add local file example in Usage
- [ ] Help text for new args is self-explanatory

## Success Metrics

- Real integration test: transcribe a segment of a local screen-recording MP4
  (user has these), verify output quality matches YouTube transcripts
- Zero regressions: 370 existing tests pass unchanged

## Dependencies & Risks

| Risk | Mitigation |
|------|------------|
| Files API 48h expiry breaks long-running workflows | Document in SKILL.md; `--force` re-uploads on re-run |
| Large file upload times out (httpx default 1200s per `gemini_common.py:46`) | Existing timeout is generous; log progress during upload |
| User confusion on time format | Error message shows accepted formats; help text explicit |
| Refactor of `process_transcript` breaks scan auto-transcript | Update both callers in same change; covered by existing tests |
| `cmd_scan` auto-transcript path change | Test covers it; signature change is small (compute paths from dir+name before call) |

## References & Research

### Internal References

- Brainstorm: `docs/brainstorms/2026-04-16-local-video-transcript-brainstorm.md`
- Segment clipping reference: `scripts/translate_video.py:1198-1231`
- Current `cmd_transcript`: `scripts/video_intel.py:1337-1403`
- Current `process_transcript`: `scripts/video_intel.py:805-887`
- Current `call_gemini`: `scripts/video_intel.py:465-490`
- Mutual-exclusion arg pattern: `scripts/translate_video.py:2526-2550`
- Test patterns: `tests/test_utils.py:1240-1329`
- Existing time helper (colon forms only): `scripts/video_intel.py:656-666`
- Safety settings (permissive, reused): `scripts/gemini_common.py:68-88`
- Client factory (1200s timeout): `scripts/gemini_common.py:46-60`

### Related ADRs

- ADR-0002: Three decoupled tasks in transcript prompt (reused as-is)
- ADR-0015: Permissive safety filters (applied to local-file calls too)

### Files to Modify

1. `scripts/video_intel.py`:
   - NEW `parse_time_to_seconds()` helper
   - NEW `upload_local_video()` helper + `LARGE_FILE_THRESHOLD_BYTES` constant
   - EXTEND `call_gemini()` with `start_offset` / `end_offset` kwargs
   - REFACTOR `process_transcript()` signature to `(channel_dir, prefix)` + offset kwargs
   - EXTEND `cmd_transcript()` with `--file` / `--start` / `--end` branching
   - UPDATE `cmd_scan()` auto-transcript call to match new `process_transcript` signature

2. `tests/test_utils.py`:
   - NEW `TestParseTimeToSeconds` class (6-8 tests)
   - NEW `TestUploadLocalVideo` class (3-4 tests)
   - UPDATE `TestProcessTranscriptResilience` for new signature
   - NEW `TestCmdTranscriptFile` class (integration for `--file` branch)

3. `skills/video-intel/SKILL.md`: local file example in "How to Use"

4. `CLAUDE.md`: mention local-file input in transcript subcommand description

5. `README.md`: local file usage example

## Verification

```bash
# Lint + unit tests
ruff format . && ruff check . --fix && pytest -m "not integration" -q

# CLI help shows new args
python scripts/video_intel.py transcript --help

# Dry-run-ish: show argparse errors
python scripts/video_intel.py transcript --file nonexistent.mp4
python scripts/video_intel.py transcript --file some.mp4 --url "https://youtube.com/..."
python scripts/video_intel.py transcript --file some.mp4 --start "bad-time"

# Real integration test with a small local MP4 + segment
python scripts/video_intel.py transcript \
  --file ~/Videos/test-5min.mp4 --start 00:30 --end 02:00

# Verify outputs land next to input
ls -la ~/Videos/test-5min.transcript.md
ls -la ~/Videos/test-5min.meta.json
```
