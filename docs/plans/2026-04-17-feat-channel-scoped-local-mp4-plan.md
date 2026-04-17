---
title: "feat: In-place local video recovery for gated channel videos"
type: feat
status: active
date: 2026-04-17
revision: 4
---

# feat: In-place local video recovery for gated channel videos

> Revision 4 keeps revision 3's direction (in-place corpus recovery under
> `output_dir/<channel>/`, stem-based artifact naming, parent-folder channel
> inference, mtime publish-date fallback, `.mkv` first-class) and closes three
> blocking gaps found in review:
>
> - **G1**: concepts/index pipelines enumerate via `*.meta.json` glob so they
>   pick up stem-named local artifacts alongside scan-generated canonical ones.
> - **G2**: meta.json uniqueness per `{channel, video_id}` — if scan already
>   wrote a canonical meta.json for a `video_id` and the user drops a file whose
>   stem maps to that same `video_id`, the local-recovery path **updates the
>   existing canonical meta and writes artifacts under that canonical prefix**
>   instead of creating a duplicate stem-named meta.
> - **G3**: `published_source` provenance field in meta.json (`"youtube_api"`,
>   `"cli_flag"`, `"mtime"`, `"sibling_meta"`) so downstream KB queries can
>   distinguish high-confidence dates from fallbacks.
>
> Revisions 1 and 2 (canonical-path reconstruction, strict refusal on missing
> metadata) remain superseded.

## Overview

Support `mindmap --file` and `transcript --file` as an in-place recovery path
for members-only or gated videos that Gemini cannot fetch from YouTube directly.

Preferred workflow:

- user saves the local video directly under `output_dir/<channel>/`
- the tool writes `.mindmap.md`, `.transcript.md`, and `.meta.json` beside the
  local video
- the channel is inferred from the parent folder when possible
- title and date are inferred from the local file when YouTube metadata is
  unavailable

This is simpler than routing arbitrary downloads into a separate canonical path.
The user already organizes files manually; the code should take advantage of
that instead of asking for more ceremony.

## User Workflow Assumptions

These are now design inputs, not edge cases:

- local video files already live in the correct channel folder
- examples:
  - `./video-intel/everyinc/Compound Engineering Camp.mkv`
  - `./video-intel/everyinc/lfML5OJc-CM.mp4`
- Firefox/manual saves often preserve the title in the filename stem
- some manually renamed files preserve the YouTube video ID in the filename stem
- the user already knows which channel folder to place the video in
- `LastWriteTime` is an acceptable fallback for `published` in this manual flow
- `.mkv`, `.mp4`, `.mov`, `.webm`, and `.avi` are valid local inputs

## Problem Statement

Current local-file behavior in `cmd_transcript` already uploads the local video
and writes artifacts beside the source file, but it is still ad hoc:

- title = filename stem
- published = `input_path.stat().st_mtime`
- no channel inference from the parent folder
- no clean way to produce channel-aware recovery artifacts for concepts/KB flow
- no local-file parity for `mindmap`

The previous revision overcorrected by treating "arbitrary file anywhere on
disk" as the default case. That is not the user's actual workflow.

## Recommended Design

### Primary path: in-place corpus recovery

When `--file` is used:

1. Resolve `input_path`.
2. If the file lives under `output_dir/<channel>/` and `<channel>` is configured,
   infer `channel` from the parent folder.
3. Write `.mindmap.md`, `.transcript.md`, and `.meta.json` beside the local
   video in that same folder.
4. Use the existing local upload flow to Gemini Files API.
5. Keep persisted artifacts free of Gemini `file_uri`; only canonical YouTube
   URLs, when known, should be written to disk.

`--channel` remains available as an override for local files stored elsewhere,
but it is no longer the default or preferred path.

### Identity resolution rules

Resolve `{channel, video_id, title, published, published_source, video_url, prefix}` in this order. Each step also sets `published_source` to track provenance (G3).

1. **Existing sibling `.meta.json` for the same filename stem.** Adopt all fields verbatim. `published_source = "sibling_meta"`. `prefix = input_path.stem`.
2. **Channel-wide `video_id` match in canonical scan-generated meta (G2 dedup).** Derive candidate `video_id` from `--video-id` flag or stem-as-id (if matches `^[A-Za-z0-9_-]{11}$`). Scan `channel_dir.glob("*.meta.json")` for a meta whose `video_id` matches. If found: **adopt the scan-generated prefix from that meta filename** (e.g. `2026-04-16-once-you-vibe-code`), **update that meta file in place**, and write artifacts under that canonical prefix. Uniqueness invariant: at most one `.meta.json` per `video_id` per channel. `published_source` inherited from the scan meta (usually `"youtube_api"`).
3. **Explicit CLI flags.** `--channel`, `--video-id`, `--title`, `--date` override anything inferred below. `published_source = "cli_flag"` when `--date` is given.
4. **Parent folder inference.** `output_dir/<channel>/<file>` → `channel = <channel>` if the folder name matches a configured channel. Only fills `channel`, not date/title.
5. **Filename stem as `title`.** Default title source for manual downloads. Only applies when steps 1–2 didn't already produce a title.
6. **Filename stem as `video_id`** only if it matches `^[A-Za-z0-9_-]{11}$`.
7. **`published` fallback to `input_path.stat().st_mtime`** (date-only, formatted `YYYY-MM-DD`). `published_source = "mtime"`. Only applies when steps 1–3 didn't produce a date.
8. **`video_url = https://www.youtube.com/watch?v=<video_id>`** if `video_id` is known. Otherwise left empty (local-only content without a canonical URL).

**Flag-override precedence within a G2 match.** When step 2 (G2 dedup) fires
and explicit flags are also present, the flags update the matched canonical
meta's **content fields** in place (`title`, `published`, `video_url` if
derivable). `published_source` becomes `"cli_flag"` in that case, overwriting
whatever the canonical meta carried before. Flags do **not** change
`channel_dir` or `prefix` — those stay canonical to honor uniqueness
invariant F11; otherwise the same video would split across two meta files.
`--video-id` cannot override a G2 match since G2 uses `video_id` as the
match key in the first place (passing a different `--video-id` sends the
resolver to step 3 instead of step 2).

Important: unlike revision 2, this design does not refuse when only local
metadata exists. The user's real workflow is manual and channel-aware, so a
pragmatic fallback is better than a hard stop. The `published_source` field
lets downstream KB queries filter or down-weight mtime-derived dates when
temporal accuracy matters.

**G2 concrete example.** Scan ran earlier and produced
`./video-intel/everyinc/2026-04-16-once-you-vibe-code-something-great.meta.json`
with `video_id: "once-you-vibeX"` and `published: "2026-04-16"`. User later
downloads the MP4 as `./video-intel/everyinc/once-you-vibeX.mp4` (stem = videoId).
Step 2 fires: prefix becomes `2026-04-16-once-you-vibe-code-something-great`
(from the scan meta filename), artifacts are written with that prefix, the
scan meta is updated in place. No second `once-you-vibeX.meta.json` is created.

### Artifact naming

For in-place local recovery, keep artifact names aligned to the source file stem:

- `Compound Engineering Camp.mkv`
- `Compound Engineering Camp.mindmap.md`
- `Compound Engineering Camp.transcript.md`
- `Compound Engineering Camp.meta.json`

For files named by video ID:

- `lfML5OJc-CM.mp4`
- `lfML5OJc-CM.mindmap.md`
- `lfML5OJc-CM.transcript.md`
- `lfML5OJc-CM.meta.json`

This matches the user's manual organization and avoids slug/date reconstruction.

## Why This Is Simpler

- No copy/move/register step.
- No need to re-route files downloaded into the correct channel folder already.
- Existing "output next to source" behavior is preserved and extended.
- Parent folder gives us channel identity for free.
- Filename stem provides a practical title or video ID in many cases.
- `LastWriteTime` provides a useful publish-date fallback in the manual workflow.

## Technical Approach

### 1. Preserve the URL/media split

Keep revision 2's fix:

- persisted `video["url"]` should represent the canonical YouTube URL when known
- Gemini should receive a separate `media_uri` parameter carrying the uploaded
  local `file_uri`

This avoids reintroducing the 403/expired-URI bug.

### 2. Add local path inference helper

Add a helper near `resolve_output_dir()` / `video_file_prefix()`:

```python
def infer_channel_from_file_path(input_path: Path, output_dir: Path, config: dict[str, Any]) -> str | None:
    """Return channel name if input_path lives under output_dir/<channel>/..."""
```

Behavior:

- resolve `output_dir`
- check whether `input_path.parent` is exactly one configured channel folder
- return that channel name if matched, else `None`

### 3. Add local identity resolver

Add a helper:

```python
def resolve_local_file_identity(
    input_path: Path,
    *,
    channel_name: str | None,
    channel_dir: Path | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Resolve local file identity using sibling meta, canonical meta by video_id (G2),
    flags, parent folder, filename, and mtime. Returns prefix + meta_path aligned to the
    winning source — stem-based for local recovery, or canonical scan-generated prefix
    when G2 dedup fires."""
```

Expected return:

```python
{
    "channel": "everyinc" or None,
    "video_id": "lfML5OJc-CM" or "",
    "url": "https://www.youtube.com/watch?v=lfML5OJc-CM" or "",
    "title": "Compound Engineering Camp",
    "published": "2026-04-17",
    "published_source": "sibling_meta" | "cli_flag" | "mtime" | "youtube_api",
    "channel_dir": Path("./video-intel/everyinc"),  # parent folder or configured override
    "prefix": input_path.stem OR "2026-04-16-once-you-vibe-code-something-great",
    "meta_path": channel_dir / f"{prefix}.meta.json",
}
```

**`prefix` and `meta_path` depend on which identity-resolution step won:**

- Steps 1 (sibling meta) and 3–7 (flags / parent folder / stem / mtime): `prefix = input_path.stem`, `meta_path = input_path.with_suffix(".meta.json")`.
- Step 2 (G2 dedup — canonical meta matched by `video_id`): `prefix` is the stem of the canonical meta filename (e.g. `2026-04-16-once-you-vibe-code-something-great`), `meta_path` is the existing canonical meta file. The local-recovery path writes artifacts under that canonical prefix and **updates the existing meta in place** — no stem-named duplicate is created. This is how the uniqueness invariant (F11) is enforced in the resolver rather than the caller.

### 4. Extend `cmd_transcript`

In `cmd_transcript`:

- keep existing local-file upload behavior
- infer `channel` from the parent folder if possible
- resolve local identity (including G2 dedup) from sibling meta / canonical meta by `video_id` / flags / filename / mtime
- write identity metadata before transcription using `identity["meta_path"]`
- write transcript/mindmap artifacts under `identity["channel_dir"] / f"{identity['prefix']}.*"` — may be stem-based **or** the canonical scan-generated prefix when G2 dedup fires
- pass `media_uri=file_uri` into `process_transcript`, keeping canonical `video_url` separate

Pseudo-flow:

```python
if args.file:
    input_path = Path(args.file).resolve()
    output_dir = resolve_output_dir(config)
    inferred_channel = infer_channel_from_file_path(input_path, output_dir, config)
    channel_name = args.channel or inferred_channel
    channel_dir_hint = (output_dir / channel_name) if channel_name else input_path.parent

    identity = resolve_local_file_identity(
        input_path,
        channel_name=channel_name,
        channel_dir=channel_dir_hint,
        args=args,
    )

    # IMPORTANT (F11): channel_dir and prefix come from the resolver, NOT from
    # input_path.parent / input_path.stem. When G2 fires, these point at the
    # existing canonical scan-generated artifacts instead of stem-based ones.
    channel_dir = identity["channel_dir"]
    prefix = identity["prefix"]

    update_meta(
        identity["meta_path"],
        {
            "video_url": identity["url"],
            "video_id": identity["video_id"],
            "channel": identity["channel"],
            "title": identity["title"],
            "published": identity["published"],
            "published_source": identity["published_source"],
            "model": model,
            "transcript_source": "local_file",
        },
        mode="identity",
    )

    file_uri = upload_local_video(client, input_path)
    video = {
        "video_id": identity["video_id"],
        "url": identity["url"],   # canonical YouTube URL (empty if unknown); never file_uri
        "title": identity["title"],
        "published": identity["published"],
    }
    process_transcript(
        client, types, video, prompt_text, model,
        channel_dir, prefix,             # from identity, not input_path
        force=args.force,
        start_offset=start_offset, end_offset=end_offset,
        media_uri=file_uri,              # revision-2 split preserved
    )
```

Same pattern applies to `cmd_mindmap`'s `--file` branch (§ 5). Both commands consume `identity["channel_dir"]` and `identity["prefix"]` so the uniqueness invariant F11 is enforced by the resolver, not re-implemented per caller.

### 5. Add `mindmap --file` parity

`cmd_mindmap` should support the same local in-place recovery path:

- add `--file` as a local input
- infer `channel` from the parent folder when possible
- resolve identity using the same helper
- write `.mindmap.md` and `.meta.json` beside the local file
- pass `media_uri=file_uri` to `process_mindmap`

Mindmap parity remains necessary because concept extraction still depends on
mindmap presence.

### 6. Keep `update_meta(..., mode="identity")`

Retain revision 2's sentinel mode:

- `mode="identity"` writes/merges metadata
- does not append to `modes_completed`

That part of revision 2 is still right.

### 7. Pivot concepts/index enumeration to `*.meta.json` glob (G1)

Today, `cmd_scan`'s concept-extraction loop ([scripts/video_intel.py:1333-1341](scripts/video_intel.py#L1333-L1341)) iterates the YouTube-API-derived `videos` list and computes `prefix = video_file_prefix(v)`. Local-recovery videos never flow through that list, so concepts are never extracted for them.

**Change:** concept enumeration must iterate `channel_dir.glob("*.meta.json")` and derive `prefix` by stripping the `.meta.json` suffix from each filename. For each discovered meta:

1. Read the meta.json.
2. If `concepts.json` already exists under that prefix, skip.
3. Call `find_mindmap_source(channel_dir, prefix)` to locate the mindmap.
4. If no mindmap exists (e.g., scan 403'd on mindmap and user hasn't run `mindmap --file` yet), skip silently.
5. Otherwise, build a synthetic `video` dict from the meta fields and feed it to `process_concepts`.

This unifies scan-generated (`{YYYY-MM-DD}-{slug}.*`) and local-recovery (`{stem}.*`) artifacts under one enumeration path with zero special-casing. Same change pattern applies to `cmd_index` (the search index builder) — verify it walks by glob, not by video list.

Two filename conventions coexist in `output_dir/<channel>/`: scan-generated `{YYYY-MM-DD}-{slug}.*` and local-recovery `{stem}.*` (for local files whose `video_id` did not match any existing canonical meta). Downstream pipelines must enumerate via glob patterns to cover both.

## Acceptance Rules

| # | Rule | Rationale |
|---|------|-----------|
| F1 | If the local file lives under `output_dir/<channel>/`, infer `channel` from the parent folder. | Matches the user's real workflow. |
| F2 | If `--channel` is passed, it overrides parent-folder inference. | Keeps flexibility for files stored elsewhere. |
| F3 | Local artifacts are written beside the local video by default (stem-based naming). | Extends current ad hoc behavior. |
| F4 | Filename stem is the default `title`. | Firefox/manual saves often preserve the real title. |
| F5 | Filename stem is treated as `video_id` only if it looks like an 11-char YouTube ID. | Supports deliberate ID-based naming without overfitting every filename. |
| F6 | `published` may fall back to `input_path.stat().st_mtime` for manual local recovery. | User-approved pragmatic fallback. |
| F7 | `video_url` in persisted artifacts must never be a Gemini `file_uri`. | Keep artifacts stable and canonical. |
| F8 | `media_uri` remains separate from `video["url"]`. | Preserves revision 2's critical fix. |
| F9 | `mindmap --file` and `transcript --file` both support the in-place local workflow. | Prevents the concepts pipeline from stalling. |
| F10 | `.mkv` is a supported and documented local input. | Real user case, not an edge case. |
| F11 | **Uniqueness invariant (G2):** at most one `.meta.json` per `{channel, video_id}`. If a scan-generated canonical meta with a matching `video_id` exists, the local-recovery path updates it in place and writes artifacts under the canonical prefix instead of creating a second stem-named meta. | Prevents the KB from double-counting the same video. |
| F12 | **Concepts/index enumeration (G1):** `cmd_scan`'s concept-extraction loop and `cmd_index`'s indexing loop iterate `channel_dir.glob("*.meta.json")` and derive prefix from the filename, not from `video_file_prefix(v)` applied to a YouTube-API video list. | Unifies scan-generated and local-recovery artifacts under one enumeration path. |
| F13 | **Provenance field (G3):** `.meta.json` includes `published_source` ∈ {`"youtube_api"`, `"sibling_meta"`, `"cli_flag"`, `"mtime"`}. | Lets downstream KB queries distinguish high-confidence dates from mtime fallbacks. |

## Acceptance Criteria

### Functional

- [ ] `transcript --file ./video-intel/everyinc/Compound Engineering Camp.mkv`
      writes `.transcript.md` and `.meta.json` beside the file.
- [ ] `mindmap --file ./video-intel/everyinc/Compound Engineering Camp.mkv`
      writes `.mindmap.md` and `.meta.json` beside the file.
- [ ] `channel = "everyinc"` is inferred from the parent folder for files under
      `output_dir/everyinc/`.
- [ ] `title = "Compound Engineering Camp"` is inferred from the filename stem.
- [ ] `published = YYYY-MM-DD` falls back to `LastWriteTime` when no better
      metadata is available.
- [ ] `lfML5OJc-CM.mp4` infers `video_id = "lfML5OJc-CM"` from the stem.
- [ ] `video_url` is canonical YouTube URL when `video_id` is known and never a
      Gemini `file_uri`.
- [ ] `--start/--end` still works for large local files and writes output beside
      the video.
- [ ] `--file` without `--channel` works cleanly when the file already lives
      under a configured channel folder.
- [ ] `--channel` still works as an override for files stored outside the corpus.

### Concepts / KB flow

- [ ] After `mindmap --file` and `transcript --file`, the next
      `scan --channel everyinc` extracts concepts from the local mindmap without
      special-casing.
- [ ] Concept extraction enumerates via `channel_dir.glob("*.meta.json")` (G1)
      and picks up both scan-generated and stem-named local artifacts.
- [ ] `index` picks up the local transcript for search (same glob-based
      enumeration).
- [ ] Meta.json uniqueness per `{channel, video_id}` (G2) is verified: dropping
      an MP4 whose stem matches an existing canonical meta's `video_id` updates
      that canonical meta in place; no second `.meta.json` is created.
- [ ] `published_source` field (G3) appears in every meta.json and accurately
      reflects the source of the `published` value.

### Non-Functional

- [ ] Unit tests cover parent-folder channel inference.
- [ ] Unit tests cover title inference from filename stem.
- [ ] Unit tests cover video ID inference from 11-char filename stems.
- [ ] Unit tests cover `mtime` fallback for `published`.
- [ ] Unit tests cover `update_meta(..., mode="identity")`.
- [ ] Integration tests cover `.mkv` local-file happy path for both
      `mindmap --file` and `transcript --file`.
- [ ] Existing tests pass unchanged except where intentionally updated for new
      local-file inference behavior.

## Revision History & Joint Agreement

This plan is the product of a three-party review loop. Revisions are preserved
here as decision archaeology for anyone reading the committed plan later.

### Revision 1 (Claude Code, 2026-04-17, superseded)

Initial design. Routed all `--file` output into canonical
`{YYYY-MM-DD}-{slug}.*` names under `output_dir/<channel>/`, regardless of
where the MP4 lived. Refused to proceed when identity couldn't be recovered
from a prior scan-generated `.meta.json` or explicit `--date` flag. Over-
optimized for download-anywhere-then-route workflows that Daniel does not use.

### Revision 2 (Claude Code, 2026-04-17, superseded)

Folded peer-review findings from Codex:

- Split Gemini `media_uri` from canonical `video_url` (critical bug fix:
  revision 1 would have re-sent the YouTube URL to Gemini, reproducing the
  403 it was meant to avoid).
- Allowed `--start/--end` with channel-scoped output (revision 1 refused
  this, breaking the primary trigger case of long member interviews >500MB).
- Added explicit identity-meta write step before the Gemini upload so
  failures/partials still leave a complete `.meta.json`.
- Brought `mindmap --file --channel` into scope so concept extraction
  unblocks end-to-end.
- Kept the canonical-path reconstruction philosophy from revision 1.

### Revision 3 (Codex, 2026-04-17, superseded by revision 4)

Pushed back on the canonical-path philosophy once Daniel shared the real
corpus layout:

- Gated videos are manually downloaded directly into
  `output_dir/<channel>/` (example: `G:\My Drive\video-intel\everyinc\Compound Engineering Camp.mkv`).
- The parent folder already identifies the channel; no need to route.
- Firefox/manual saves preserve useful metadata in the filename stem
  (sometimes the real title, sometimes the YouTube video ID).
- `.mkv` is a normal input, not an edge case.
- `LastWriteTime` is an acceptable fallback publish date for this manual flow.
- Preferred path shifted to "process the file where it already lives"
  rather than "download anywhere, then route it."

Revision 3 retained revision 2's `media_uri` split and mindmap parity but
replaced canonical-name reconstruction with stem-based in-place artifacts
and pragmatic fallback inference.

### Revision 4 (joint, 2026-04-17, this document)

Claude Code's review flagged three blocking gaps in revision 3. Codex
concurred. Revision 4 closes all three:

- **G1 — concepts/index enumeration.** Revision 3 claimed "next scan
  extracts concepts without special-casing," but the existing concept
  loop iterates a YouTube-API-derived video list that local-recovery files
  never reach. Fix: both `cmd_scan`'s concept loop and `cmd_index` must
  enumerate via `channel_dir.glob("*.meta.json")` and derive prefix from
  filename, not from `video_file_prefix(v)`. Unifies both naming
  conventions under one path. (See "Pivot concepts/index enumeration to
  `*.meta.json` glob" above and acceptance rule F12.)
- **G2 — meta.json uniqueness per `{channel, video_id}`.** If scan
  already wrote a canonical meta for a videoId and the user drops a
  file whose stem maps to the same videoId, revision 3 would have
  created a duplicate stem-named meta, letting the KB double-count.
  Fix: identity-resolution step 2 scans the channel for an existing
  canonical meta by videoId; if found, the local-recovery path updates
  it in place and writes artifacts under the canonical prefix.
  (See "Identity resolution rules" step 2 and acceptance rule F11.)
- **G3 — `published_source` provenance field.** `mtime`-derived dates
  and `youtube_api`-derived dates are not equally trustworthy. Meta.json
  now records which source produced the `published` value so downstream
  KB queries can weight accordingly. (See "Identity resolution rules"
  and acceptance rule F13.)

### Ground truths shared by all three parties

These are settled design inputs, not open questions:

- Gated videos are manually downloaded directly into
  `output_dir/<channel>/`. The tool processes files where they already live.
- Parent folder identifies the channel.
- Filename stem is the title by default; stem is treated as `video_id`
  only when it matches `^[A-Za-z0-9_-]{11}$`.
- `.mkv` is a first-class input alongside `.mp4/.mov/.webm/.avi`.
- `LastWriteTime` is an acceptable fallback for `published` in the manual
  recovery flow, annotated via `published_source: "mtime"`.
- Canonical `video_url` and Gemini `media_uri` remain separate fields;
  `file_uri` never persists to disk.
- `mindmap --file` has parity with `transcript --file` to keep the
  concepts pipeline from stalling on gated content.
- Concepts/index pipelines enumerate via `*.meta.json` glob so both
  stem-named local artifacts and canonical scan-generated artifacts are
  picked up without special-casing.
- Uniqueness invariant: at most one `.meta.json` per `{channel, video_id}`.

## Verification

```bash
# --- Local in-place recovery (title-stemmed file, no prior scan meta) ---
python scripts/video_intel.py mindmap \
  --file "./video-intel/everyinc/Compound Engineering Camp.mkv"
python scripts/video_intel.py transcript \
  --file "./video-intel/everyinc/Compound Engineering Camp.mkv"

# Verify sibling artifacts with stem-based naming
ls -la "./video-intel/everyinc/Compound Engineering Camp."*

# Check inferred metadata (G3: published_source must be "mtime" here)
jq '.channel, .title, .published, .published_source, .video_id, .video_url, .modes_completed' \
  "./video-intel/everyinc/Compound Engineering Camp.meta.json"
# Expected:
#   "everyinc"                        (inferred from parent folder)
#   "Compound Engineering Camp"       (from filename stem)
#   "2026-04-17"                      (from mtime; will drift over time)
#   "mtime"                           (G3 provenance)
#   ""                                (stem isn't an 11-char YouTube ID)
#   ""                                (no video_id → no canonical URL)
#   ["scan", "transcript"]

# --- G2 verification: videoId-stemmed file merges into existing canonical meta ---
# Precondition: scan produced 2026-04-16-once-you-vibe-code...meta.json with
# video_id="onceYouVibeX" and published_source="youtube_api".
# User drops onceYouVibeX.mp4 next to it.
python scripts/video_intel.py mindmap \
  --file "./video-intel/everyinc/onceYouVibeX.mp4"

# Artifacts must land under the CANONICAL prefix, not the stem
ls "./video-intel/everyinc/2026-04-16-once-you-vibe-code-something-great.mindmap.md"
# And no duplicate stem-named meta should exist
[ ! -f "./video-intel/everyinc/onceYouVibeX.meta.json" ] && echo "OK — G2 invariant holds"

# Meta.json still has youtube_api-sourced published date
jq '.published_source' \
  "./video-intel/everyinc/2026-04-16-once-you-vibe-code-something-great.meta.json"
# Expected: "youtube_api"

# --- No Gemini file_uri leaked into persisted artifacts (F7/F8) ---
grep -rE "files/[a-z0-9]{10,}" "./video-intel/everyinc/" \
  && echo "LEAK — file_uri present" || echo "OK — no file_uri in artifacts"

# --- Large-file segment clipping under in-place recovery ---
python scripts/video_intel.py transcript \
  --file "./video-intel/everyinc/Compound Engineering Camp.mkv" \
  --start 00:30 --end 45:00 --force
jq '.segments' "./video-intel/everyinc/Compound Engineering Camp.meta.json"
# Expected: [{"start": 30, "end": 2700}]

# --- G1 verification: concepts pipeline enumerates via glob ---
python scripts/video_intel.py scan --channel everyinc
# Must produce concepts for BOTH:
#   - 2026-04-16-once-you-vibe-code-something-great.concepts.json  (canonical)
#   - Compound Engineering Camp.concepts.json                      (stem-based)
ls "./video-intel/everyinc/"*.concepts.json

# --- Re-run idempotency ---
python scripts/video_intel.py transcript \
  --file "./video-intel/everyinc/Compound Engineering Camp.mkv"  # → "skipped (exists)"
```
