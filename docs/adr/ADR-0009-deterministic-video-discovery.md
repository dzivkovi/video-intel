# Deterministic Video Discovery via Uploads Playlist

**Status:** accepted

**Date:** 2026-04-01

**Decision Maker(s):** Daniel

## Context

`fetch_channel_videos()` used `youtube.search().list()` to discover new videos per channel. This endpoint is a search index, not a listing endpoint — it returns estimated results that fluctuate between calls. In practice, the same query returned 191 videos one run and 75 the next, with no code or config changes in between.

This non-determinism breaks the idempotency contract (ADR-0006). If the video list changes between runs, the "N new" count becomes unreliable, and users can't trust that a scan is complete.

The search endpoint also costs 100 quota units per API call, compared to 1 unit for most other YouTube Data API endpoints.

## Decision

Replace `youtube.search().list()` with `youtube.playlistItems().list()` on the channel's uploads playlist. Every YouTube channel has a hidden "uploads" playlist whose ID is the channel ID with the `UC` prefix replaced by `UU` (e.g., `UCxxxx` becomes `UUxxxx`).

Key implementation details:

- **Uploads playlist ID** derived via `"UU" + channel_id[2:]` — no extra API call, no signature changes to `get_channel_id()`.
- **Client-side date filtering** — `playlistItems` does not support `publishedAfter`, so we compare `contentDetails.videoPublishedAt` against `since_dt` in code.
- **Early termination** — uploads are ordered newest-first. The moment a video older than `since_dt` appears, we `return` immediately without paginating further.
- **No deduplication set** — `playlistItems` returns deterministic, non-duplicating results. The `seen_ids` set (needed for `search` which could return duplicates across pages) was removed.
- **Return format unchanged** — same `list[dict]` with keys `video_id`, `title`, `published`, `url`. All downstream code (filtering, file naming, processing) is unaffected.

## Consequences

### Positive Consequences

- Deterministic video counts — same query always returns the same results
- 100x cheaper API quota (1 unit vs 100 units per page)
- Early termination on date boundary avoids unnecessary pagination
- Simpler code — no deduplication set, no `type="video"` filter (uploads playlist only contains videos)
- Strengthens the idempotency contract (ADR-0006) by making the video list itself deterministic

### Negative Consequences

- Client-side date filtering instead of server-side `publishedAfter` — mitigated by early termination on the newest-first ordered list
- Relies on `UC` to `UU` prefix convention — undocumented by Google but stable for 10+ years, used universally by yt-dlp, Invidious, and every major YouTube tooling library

## Alternatives Considered

- **Option:** Modify `get_channel_id()` to return `uploads_playlist_id` via `channels().list(part="contentDetails")`
- **Pros:** Uses the official API to get the uploads playlist ID
- **Cons:** Changes the return signature of `get_channel_id()`, requiring updates to all callers including `cmd_transcript()` which doesn't need it. Extra complexity for no practical gain.
- **Status:** rejected (minimalism — the UC/UU convention is sufficient)

- **Option:** Extra helper `get_uploads_playlist_id()` calling `channels().list(part="contentDetails")`
- **Pros:** Official API, no convention reliance
- **Cons:** Extra API call per channel per scan (wastes quota), another function to maintain
- **Status:** rejected

## Affects

- `scripts/video_intel.py` (`fetch_channel_videos()`)
- `tests/test_utils.py` (`TestFetchChannelVideos` — 3 new mock-based tests)

## Related Debt

None spawned.

## Research References

- YouTube Data API v3: [PlaylistItems.list](https://developers.google.com/youtube/v3/docs/playlistItems/list)
- YouTube Data API v3: [Search.list](https://developers.google.com/youtube/v3/docs/search/list) — documents 100 quota unit cost
- UC/UU convention: universally used by [yt-dlp](https://github.com/yt-dlp/yt-dlp), [Invidious](https://github.com/iv-org/invidious), and other YouTube tools
