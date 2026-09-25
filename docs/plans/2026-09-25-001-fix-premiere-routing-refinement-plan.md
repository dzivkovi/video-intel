# Plan: tell an aired premiere apart from a livestream VOD (issue #245)

**Date:** 2026-09-25. **Issue:** [#245](https://github.com/dzivkovi/video-intel/issues/245). **Unattended run** (Codex as standing proxy; verdict recorded below).

## Problem

`_is_completed_livestream` flags any video carrying `liveStreamingDetails`, and YouTube attaches that resource to an aired PREMIERE of an ordinary upload exactly as to a genuine livestream. Premieres therefore route captions-first (speech only, no SCREEN blocks, no diarization) with `transcript_status: complete` and exit 0. Measured 2026-09-23/24: 20 slide-less transcripts had accumulated silently on one channel over two months, and a 288-video backfill hit it on video one. The corpus holds 49 more captions-first metas on other channels that mix real livestreams with misrouted premieres.

## What was measured before choosing

| Candidate discriminator | Result | Verdict |
|---|---|---|
| Duration threshold (the first #245 comment) | premieres up to 81 min; real livestreams that hard-400'd start at 1h44m; a 23-minute band | rejected (the rule's warning against duration heuristics stands) |
| Live window minus duration | a real stream showed +133 s, inside the premiere countdown band (+60..+120 s) | rejected |
| yt-dlp `live_status` (InnerTube `videoDetails.isLiveContent`) | 8/8 premieres `not_live`, 9/9 real livestreams `was_live`, including both #120 hard-400 cases | adopted |
| Gemini on premieres | 282/288 clean on the first pass (vs #120's 10/22 on real livestreams) | the premise the fix rests on |

yt-dlp derives `not_live` only from an explicit `False` in `isLive`/`isLiveContent` (`'not_live' if False in (is_live, live_content) else None`, `yt_dlp/extractor/youtube/_video.py` at 2026.06.09), so a missing field yields `None`, never `not_live`. The signal is positive evidence, not an absence.

## Codex verdict (design consult, read-only)

"Choose B, with A's visibility measures." Refine the Data API flag with the yt-dlp signal for FLAGGED videos only; keep `_is_completed_livestream` pure; feed the refined flag to BOTH transcript routing and mindmap suppression; subprocess with a short timeout, no retry, argument list, ignored user config; only a validated `not_live` clears the flag; missing executable, timeout, failed extraction, malformed output or unknown status keeps the flag. Two catches: verify `not_live` cannot be synthesized from a missing field (done, above), and remember that optional installation leaves the silent failure alive where yt-dlp is absent, so keep the per-video log line, report unresolved classifications, and document the opt-out.

## Design

- `probe_live_status(video_id) -> str | None`: one `yt-dlp --skip-download --no-playlist --no-warnings --ignore-config --print %(live_status)s` subprocess, 15 s timeout (measured 5 s on this machine), `None` on absent executable, non-zero exit, timeout, or an unknown value.
- `refine_was_livestream(video_id, api_flag) -> bool`: `False` without a probe when the API did not flag the video (regular uploads pay no network); `False` when the probe says `not_live` (one INFO line naming the video as an aired premiere); `True` otherwise, with a WARNING when the probe could not classify it and one INFO per process when yt-dlp is not on PATH.
- Asked LAZILY by each consumer as the last term of its decision (the transcript router after `livestream_captions_first_applies`, the mindmap suppression after `should_skip_video_mindmap_for_livestream`, in the scan and in both manual `--url` commands), memoized per video. The first cut refined eagerly at the two producers (the scan pre-flight loop and `_lookup_was_livestream`); the review pass caught that this probed already-processed videos on every scan, probed under `--dry-run`, and probed explicit `transcript_source: gemini` channels for nothing, and the validator proved a `new_videos`-scoped pass would miss the transcript loop's wider `videos` walk. A premiere still gets Gemini-first AND keeps its mindmap-from-video fallback.
- Nothing else changes: the Data API request stays one call, `_is_completed_livestream` keeps its upcoming/live exclusion, an explicit `transcript_source: gemini` still wins, and regular uploads are byte-identical.

## Tests (contract)

`tests/test_premiere_probe.py`: probe guards (absent executable spawns nothing; non-zero exit, timeout, garbage output all keep the flag); regular uploads never probe; scan caller-level through the real `cmd_scan` (a premiere routes Gemini-first and keeps its video mindmap fallback; a real livestream is unchanged; an unclassifiable video keeps captions-first); manual `cmd_transcript --url` and `cmd_process --url` through their real commands. `tests/conftest.py` pins the probe executable to absent for every test so no suite spawns yt-dlp and pre-#245 behavior is preserved wherever the probe is not under test.

## Out of scope

Regenerating the already-misrouted transcripts (operator-run, per the remediate-on-demand convention; the debrief lists them with commands). A page-fetch fallback when yt-dlp is absent (Codex: parser maintenance and block exposure without demonstrated benefit).
