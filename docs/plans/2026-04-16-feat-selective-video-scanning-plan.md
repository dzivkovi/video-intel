---
title: "feat: Selective video scanning via playlists and keywords"
type: feat
status: completed
date: 2026-04-16
---

# feat: Selective video scanning via playlists and keywords

## Overview

Add `playlists` and `keywords` fields to channel config so users can target
specific content instead of scanning everything since a date. When either field
is present, the channel switches to selective mode - only playlist and keyword
matches are processed. Channels without these fields retain current behavior.

## Problem Statement / Motivation

The date-based scan (`since: 90d`) is too blunt for prolific creators. Sean
Kochel publishes 159 videos/year across 7 playlists. The user only wants his
"Agent Skills" playlist (7 videos) and UX-related content, not all 159. Today
the only option is scanning everything and ignoring what you don't need, which
wastes Gemini API tokens on irrelevant content.

## Proposed Solution

Two new optional fields in channel config:

```yaml
channels:
  # Selective: only playlists + keyword matches
  - name: seankochel
    url: https://youtube.com/@iamseankochel
    playlists:
      - Agent Skills
      - Vibe Coding Tips
    keywords:
      - ux design
    auto_transcript: all

  # Current behavior: date-based scan (unchanged)
  - name: natebjones
    url: https://youtube.com/@natebjones
    auto_transcript: all
    since: 90d
```

### How it works

1. If channel has `playlists` or `keywords` -> **selective mode**
2. Resolve playlist names to IDs via YouTube API (case-insensitive contains)
3. Fetch all videos from matched playlists
4. Search channel for each keyword (capped at 200 results per keyword)
5. Union + deduplicate by video_id
6. Feed into existing pipeline (filter processed/skipped, process mindmaps, etc.)
7. `since` field and `--since` CLI flag are ignored with an info-level notice

## Technical Approach

### New functions in `scripts/video_intel.py`

**`resolve_playlist_ids`** (~line 172, near `fetch_channel_videos`)

```python
def resolve_playlist_ids(
    youtube, channel_id: str, playlist_names: list[str]
) -> list[tuple[str, str]]:
    """Resolve playlist names to (playlist_id, playlist_title) pairs.

    Uses case-insensitive contains matching. Logs warning for unresolved names
    with available playlist titles.
    """
```

- Calls `youtube.playlists().list(channelId=..., maxResults=50)`
- Paginates if needed (most channels have <50 playlists)
- Returns list of (id, title) tuples for matched playlists
- Logs WARNING for each unresolved name with available names

**`fetch_playlist_videos`**

```python
KEYWORD_MAX_PAGES = 4  # 200 results, 400 quota units

def fetch_playlist_videos(
    youtube, playlist_id: str
) -> list[dict]:
    """Fetch all videos from a specific playlist."""
```

- Same return format as `fetch_channel_videos`: `[{video_id, title, published, url}]`
- Same API call (`playlistItems().list`) - just different playlist ID
- No date filtering (playlists are curated collections)
- Paginates fully (playlists are typically <100 videos)

**`fetch_keyword_videos`**

```python
def fetch_keyword_videos(
    youtube, channel_id: str, keyword: str, *, max_pages: int = KEYWORD_MAX_PAGES
) -> list[dict]:
    """Search a channel for videos matching a keyword."""
```

- Calls `youtube.search().list(channelId=..., q=keyword, type="video", order="date")`
- Normalizes search response to same dict format (video_id from `id.videoId`)
- Capped at `max_pages` pages (default 4 = 200 results, 400 quota units)
- Logs quota warning: "keyword search: ~{cost} quota units for '{keyword}'"
- No date filtering (keywords search whole history per spec)

**`fetch_selective_videos`** (dispatcher)

```python
def fetch_selective_videos(
    youtube, channel_id: str, channel_config: dict
) -> list[dict]:
    """Fetch videos from playlists and/or keywords, deduplicated."""
```

- Called by `cmd_scan` when channel has `playlists` or `keywords`
- Calls `resolve_playlist_ids` + `fetch_playlist_videos` for each match
- Calls `fetch_keyword_videos` for each keyword
- Deduplicates by `video_id` (first-seen wins)
- Returns merged list

### Changes to `cmd_scan` (~line 919)

Replace the single `fetch_channel_videos` call with a dispatch:

```python
# Determine fetch strategy
is_selective = bool(ch.get("playlists") or ch.get("keywords"))

if is_selective:
    if args.since:
        log.info("  Note: --since ignored for %s (using playlists/keywords)", ch_name)
    videos = fetch_selective_videos(youtube, channel_id, ch)
else:
    # Current behavior unchanged
    since_str = args.since or ch.get("since") or config.get("default_since", "10d")
    since_dt = parse_since(since_str)
    videos = fetch_channel_videos(youtube, channel_id, since_dt)
```

Everything downstream (is_processed filter, dry-run, process_mindmap, auto-transcript,
auto-concepts) works unchanged - they all consume `list[dict]` with the same schema.

### Config validation

Add lightweight validation in `cmd_scan` before the channel loop:

- `playlists` must be a list of strings (if present)
- `keywords` must be a list of strings (if present)
- Empty lists treated as absent (no selective mode)
- Log error and skip channel on validation failure

### `--dry-run` enhancements

For selective channels, dry-run should show:

```
[seankochel] Sean Kochel (selective mode)
  Resolved playlists:
    "Agent Skills" -> PLqq3-KsIOgJod... (7 videos)
    "Vibe Coding Tips" -> PLqq3-KsIOgJo7ly... (20 videos)
  Keyword search: "ux design" (49 results, ~100 quota units)
  After dedup: 62 unique videos, 14 new.
    2026-04-08 - Google's 7-Step Vibe Engineering Skill
    2026-04-03 - How I Design Pro App UIs
    ...
```

## Acceptance Criteria

- [x] Channels with `playlists` field scan only those playlists (not uploads)
- [x] Channels with `keywords` field search only for those keywords
- [x] Channels with both use union of playlists + keywords, deduplicated
- [x] Channels without either retain current date-based behavior
- [x] Playlist names resolved via case-insensitive contains matching
- [x] Unresolved playlist names logged as WARNING with available names
- [x] Keyword search capped at 200 results (4 pages) per keyword
- [x] Keyword search logs quota warning at info level
- [x] `since` is additive for selective channels (fetches recent uploads too)
- [x] `--dry-run` shows playlist resolution, keyword results, dedup count
- [x] `--force` works with selective channels
- [x] `--channel` filter works with selective channels
- [x] `auto_transcript` and `auto_concepts` work with selective channels
- [x] Config validated: playlists/keywords must be list of strings
- [x] All new functions have type hints and tests
- [x] Existing tests still pass (backward compatibility)
- [x] SKILL.md, CLAUDE.md, README.md updated
- [x] `config.yaml` updated with seankochel example

## Success Metrics

- Sean Kochel's "Agent Skills" playlist (7 videos) scans and transcribes
  correctly with `--dry-run` showing resolution
- Existing channels (natebjones, ramjad, etc.) behavior unchanged
- `pytest -m "not integration" -q` passes with new tests

## Dependencies and Risks

| Risk | Mitigation |
|------|------------|
| YouTube API quota exhaustion from keywords | Cap at 200 results/keyword, log quota cost |
| Playlist name resolution ambiguity | Case-insensitive contains, log all matches |
| Search API non-determinism (ADR-0009) | Acceptable for keywords (unlike uploads scan) |
| `search().list` returns Shorts/streams | Same as current behavior - no new filtering needed |

## References and Research

### Internal References

- Brainstorm: `docs/brainstorms/2026-04-16-selective-video-scanning-brainstorm.md`
- ADR-0009 (deterministic video discovery): `docs/adr/ADR-0009-deterministic-video-discovery.md`
- `fetch_channel_videos`: `scripts/video_intel.py:172-211`
- `cmd_scan` channel loop: `scripts/video_intel.py:919-1065`
- Channel config consumed at: `scripts/video_intel.py:920-932`
- Test patterns for cmd_scan: `tests/test_utils.py:1028-1075`
- Test patterns for fetch_channel_videos: `tests/test_utils.py:360-422`

### API References

- YouTube `playlistItems.list`: 1 quota unit/call, deterministic order
- YouTube `playlists.list`: 1 quota unit/call, lists channel's playlists
- YouTube `search.list`: 100 quota units/call, non-deterministic relevance order
- Daily quota default: 10,000 units (free tier)

### Files to Modify

1. `scripts/video_intel.py` - new fetch functions, cmd_scan dispatch, config validation
2. `tests/test_utils.py` - tests for resolution, fetch, dedup, cmd_scan integration
3. `config.yaml` - add seankochel with playlists/keywords
4. `skills/video-intel/SKILL.md` - document selective scanning
5. `CLAUDE.md` - architecture notes for selective mode
6. `README.md` - config examples, usage
