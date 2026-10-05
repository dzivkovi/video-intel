---
project: video-intel
status: active
cadence: monthly
updated: 2026-10-06
backend: github-issues
trap: the repair trap - corpus cleanup counted as progress. The prize this corpus currently serves is a SALES ledger that lives in magma-rainmaker, and a clean index moves it by zero. Price any unrequested repair in one line before doing it.
---

## Next best action
Daniel: read `work/2026-10-05/06-night-summary.md` (or say `/read-along last night`), answer its six Decisions, then run `python scripts/video_intel.py headlines --dry-run`, pick the one or two uploads worth `process --url`, and go back to the magma-rainmaker ledger.

```text
Done overnight 2026-10-05: #248 (PR #251), #139 (#252), #247 (#253), #249 (#254) all merged on green after ce-code-review plus the Codex peer pass; follow-up #250 filed (chunk-size A/B). Five worktrees under .claude/worktrees/ are still on disk because the permission layer denied removal: the commands are in the night summary, Decisions item 6.
```

## Waiting on
- nothing

## Gates (dated)
- 2026-10-02: ledger check (see Watch). If still 0 of 3, the corpus work of 2026-09-18 was a detour and the next video-intel session should say so before touching anything.

## Watch
- New since 2026-10-05: `headlines` (and `headlines --dry-run`) renders the digest of the 16 digest-only channels without a scan; a lost chunk under `transcript_source: auto` is filled from captions; timestamps past a video's end are recorded as mild flags (`transcript_timestamp_overrun_entries`); the mindmap-from-video call is capped by `transcript_timeout_seconds`.
- Unread on Kindle: the 2026-10-01 Shankar/Husain five-steps evals briefing (EPUB in Downloads). re:cinq and peteryang are digest-only (enabled:false + headline_digest) since 2026-10-02: pull by hand when a headline earns it. New living thread `_briefings/adrian/` (Adrian Cockcroft, topic `adrian`).
- **Ledger: 0 of 3 problem-discovery conversations** (Module 0 of `G:\My Drive\video-intel\_briefings\sales-practice\curriculum.md`; owner is magma-rainmaker NEXT.md, debriefs go to magma-rainmaker `work/`). This number, not corpus health, is what this project is for right now.
- Optional, not urgent: `ashmaurya` channel could move to selective playlist mode (four methodology playlists) so scans stop pulling annual-restatement videos.
