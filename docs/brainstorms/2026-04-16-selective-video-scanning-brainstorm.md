---
date: 2026-04-16
topic: selective-video-scanning
---

# Selective Video Scanning: Playlists + Keywords

## What We're Building

Add playlist and keyword filtering to channel config so users can be selective
about which videos to process instead of scanning everything since a date.

Today: "scan all natebjones uploads from the last 90 days."
After: "scan only Sean Kochel's 'Agent Skills' playlist" or "scan only his
videos about UX" - regardless of date.

Both mechanisms produce a list of video IDs. The processing pipeline (mindmap,
transcript, concepts) doesn't care where the list came from.

## Why This Approach

Three options were considered:

1. **Playlists only** - stable IDs, cheap API calls (1 unit), but not all
   creators organize content into playlists.
2. **Keywords only** - flexible, works for any channel, but costlier API calls
   (100 units per search) and fuzzier matching.
3. **Both (chosen)** - playlists for curated collections, keywords for ad-hoc
   topic filtering. Same config block, same processing pipeline.

Both use existing YouTube Data API methods that the codebase already calls
(`playlistItems().list()` for playlists, `search().list()` for keywords).

## Key Decisions

- **Selective replaces default scan:** If a channel has `playlists` or
  `keywords`, only those videos are processed. No date-based uploads scan. The
  `since` field becomes irrelevant for selective channels. Channels without
  playlists/keywords retain current behavior unchanged.

- **Keywords ignore the since window:** Keywords search the whole channel
  history. You're already being selective by specifying keywords - adding a
  date filter would complicate the mental model for no real benefit.

- **Playlist names resolved via API:** Config uses human-readable names (e.g.,
  "Agent Skills"), not raw playlist IDs. The script resolves names to IDs at
  runtime by listing the channel's playlists and matching. One extra API call
  per scan, but friendlier config that doesn't break when playlist IDs change.

- **Deduplication by video_id:** A video can appear in multiple playlists and
  match multiple keywords. Dedup before processing so each video is only
  processed once.

## Config Design

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

No `mode` flag needed. The presence of `playlists` or `keywords` implicitly
switches to selective mode.

## API Feasibility (Verified)

- `youtube.playlists().list(channelId=X)` returns all playlists with names and
  IDs. Tested: Sean Kochel has 7 playlists, "Agent Skills" = 7 videos.
- `youtube.search().list(channelId=X, q="ux")` returns 49 results scoped to
  the channel. Works like the web URL `@channel/search?query=ux`.
- `youtube.playlistItems().list(playlistId=X)` already used by the codebase
  for uploads - works identically for custom playlists.

## Resolved Questions

- **Quota budgeting:** Log a warning at info level when keyword search is used
  ("keyword search uses 100 quota units per call"). Users see the cost without
  us building quota tracking.

- **Playlist name matching:** Case-insensitive contains. "Agent Skills" matches
  "Agent Skills - Updated". More forgiving for real-world playlist naming. If
  zero playlists match, log a warning with available playlist names.

## Next Steps

Run `/workflows:plan` for implementation details.
