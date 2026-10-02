---
project: video-intel
status: active
cadence: monthly
updated: 2026-10-02
backend: github-issues
trap: the repair trap - corpus cleanup counted as progress. The prize this corpus currently serves is a SALES ledger that lives in magma-rainmaker, and a clean index moves it by zero. Price any unrequested repair in one line before doing it.
---

## Next best action
delegate: an overnight `/dark-factory` run on #248 then #247, one PR each.

```text
Goal: close #248 (a chunk lost to the wall-clock timeout leaves a partial that captions failover never fills) and #247 (headline digest ranks on title plus description; standalone `headlines` command).
Current state: both filed 2026-10-02 with repro, gap analysis and done-when. No code yet. Proposal "captions failover cannot tell no-captions from 429" was dropped: #231 already closed it.
Relevant files: scripts/video_intel.py (_run_chunked_transcript_url, rank_headlines, render_headline_digest), skills/video-intel/SKILL.md, .claude/rules/transcript.md, .claude/rules/briefings.md.
Constraints: CLAUDE.md core guardrails ([core: real-caller], [core: probe-before-pay], skill parity in the same PR); never touch the live corpus in tests.
Done when: both PRs merged on green CI after ce-code-review plus the Codex peer pass, with each issue's done-when satisfied.
Checks to run: pytest tests/ --ignore=tests/evals; ruff; the falsification step each issue names (remove the new branch, confirm exactly the new test fails).
```

## Waiting on
- nothing

## Gates (dated)
- 2026-10-02: ledger check (see Watch). If still 0 of 3, the corpus work of 2026-09-18 was a detour and the next video-intel session should say so before touching anything.

## Watch
- Unread on Kindle: the 2026-10-01 Shankar/Husain five-steps evals briefing (EPUB in Downloads). re:cinq and peteryang are digest-only (enabled:false + headline_digest) since 2026-10-02: pull by hand when a headline earns it. New living thread `_briefings/adrian/` (Adrian Cockcroft, topic `adrian`).
- **Ledger: 0 of 3 problem-discovery conversations** (Module 0 of `G:\My Drive\video-intel\_briefings\sales-practice\curriculum.md`; owner is magma-rainmaker NEXT.md, debriefs go to magma-rainmaker `work/`). This number, not corpus health, is what this project is for right now.
- Optional, not urgent: `ashmaurya` channel could move to selective playlist mode (four methodology playlists) so scans stop pulling annual-restatement videos.
