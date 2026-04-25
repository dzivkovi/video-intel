---
title: "feat(scan): skip Shorts by default + add prune-shorts subcommand"
type: feat
status: active
date: 2026-04-24
deepened: 2026-04-24
origin: https://github.com/dzivkovi/video-intel/issues/37
---

# feat(scan): skip Shorts by default + add prune-shorts subcommand

## Overview

Stop polluting the corpus with YouTube Shorts. Two prongs in one PR:

1. **Scan-time filter.** Default-on per-channel `skip_shorts` flag wired into `cmd_scan`. New scans drop Shorts before any Gemini call. Per-channel `skip_shorts: false` opts back in.
2. **`prune-shorts` subcommand.** Mirrors the `dedupe` contract — dry-run by default, `--apply` to mutate. Walks meta.json files per channel, classifies each video, deletes all four artifact types per Short on `--apply`. Manual `taxonomy-build` + `index --force` follow-up (not automatic).

The corpus today contains ~187 Shorts in `chase_h_ai` alone (57% of that channel). Other channels are smaller-scale contaminated. The feature ships future-proofing (filter) + retroactive cleanup (prune) so the user can land at a clean baseline post-merge.

## Problem Frame

Empirical evidence (see [issue #37](https://github.com/dzivkovi/video-intel/issues/37)): chase_h_ai has 331 metas vs natebjones's 309 despite a narrower lookback window. Sampling all chase_h_ai mindmaps and using "max timestamp < 90s" as a Shorts proxy yielded 187/142 split. Smoking gun: `2025-11-10-ai-speed-ramping.mindmap.md` carries `(0:00) (0:01) (0:02)` timestamps — those are seconds, not minutes. Vertical Short.

The scanner has zero filtering logic. `grep -i "short|duration"` against [scripts/video_intel.py](../../scripts/video_intel.py) returns three hits, all unrelated string-handling. YouTube's `uploads` playlist mixes Shorts and long-form with no native toggle, so every Short on a configured channel costs three Gemini calls (mindmap + transcript + concepts) and accumulates four useless artifacts.

The corpus use case is "what did creators say about [topic]" search. 30-second hooks rarely answer that well. Default-skip aligns the tool with stated user intent.

## Requirements Trace

Source: issue #37 acceptance criteria (read via `gh issue view 37`).

- **R1.** `is_short(video)` predicate combines `duration < 60s OR /shorts/<id> redirect 200`.
- **R2.** Per-channel `skip_shorts` flag, default `true`. Per-channel `skip_shorts: false` opts back in.
- **R3.** `cmd_scan` consults `is_short()` BEFORE any Gemini call (probe-before-pay rule).
- **R4.** `prune-shorts` subcommand: dry-run by default, `--apply` to mutate, `--channel` to scope.
- **R5.** Dry-run output names `title | duration | url | artifact_count` per match plus per-channel summary.
- **R6.** `--apply` deletes all four artifact types per video (mindmap.md, transcript.md, concepts.json, meta.json).
- **R7.** Skill-parity: `skills/video-intel/SKILL.md` updates ship in same PR.
- **R8.** `prune-shorts` deletion path keys on `video_id` from meta.json, NOT slug.
- **R9.** Manual `taxonomy-build` + `index --force` follow-up (not automatic). Mirrors `dedupe` contract.
- **R10.** Tests cover: duration boundary, `/shorts/` redirect both directions, scan-with-skip-on, scan-with-skip-off, dry-run no-mutation, `--apply` deletes all four artifact types, legacy meta without duration falls back gracefully.
- **R11.** Gate 1 smoke test artifact attached to the PR — **two-sided**: (a) `prune-shorts --channel chase_h_ai` dry-run shows ≥150 detected Shorts (matches the 187 pre-existing estimate within tolerance) AND no row with duration ≥3 minutes; (b) `prune-shorts --channel natebjones` dry-run shows ≤2 detected Shorts among the first 20 sampled (long-form-only channel; high false-positive rate would indicate the redirect signal broke or duration parsing is wrong).

## Scope Boundaries

- No changes to `scripts/translate_video.py`. Operationally separate per [CLAUDE.md](../../CLAUDE.md).
- No search-time filtering of Shorts. Pruning at source is the cleaner fix.
- No yt-dlp or downloaded-video metadata path. Defeats the no-quota goal.
- No automatic `taxonomy-build` or `index --force` after `--apply`. Mirrors `dedupe` blast-radius discipline.
- No aspect-ratio detection. Rejected in issue #37 — YouTube Data API does not reliably expose aspect ratio.
- No hashtag/description sniffing. Rejected in issue #37 — user-editable, unreliable.

### Deferred to Separate Tasks

- Running `prune-shorts --apply` against the user's actual G:/ corpus. **User-driven post-merge** per the SDLC chain doc's destructive-action rule. The chain attaches dry-run output as Gate 1 artifact; the apply happens when the user is ready.
- Backfilling `duration_seconds` into all existing meta.json files. The `prune-shorts` path handles legacy metas via on-the-fly fetch; a bulk backfill is future work if it becomes useful.
- A `docs/solutions/` entry capturing the cleanup-command pattern + fail-safe-predicate pattern for reuse. Will be drafted post-merge if the lessons compound.

## Context & Research

### Relevant code and patterns

- **`cmd_dedupe` is the contract template** for `prune-shorts`. Files: [scripts/video_intel.py:3662-3725](../../scripts/video_intel.py#L3662). Gives us: `require_channels_config()` + `resolve_output_dir()` boilerplate, `getattr(args, "channel", None)` + `getattr(args, "apply", False)`, per-channel iteration, dry-run logging, `if apply: _apply_*()` gate, summary footer with "Re-run with --apply" vs "Next steps: run `taxonomy-build` and `index --force`".
- **Argparse registration template** at [scripts/video_intel.py:4097-4106](../../scripts/video_intel.py#L4097). `subparsers.add_parser("dedupe", ...)`, `--channel`, `--apply` (action=store_true, dry-run-by-default messaging).
- **Dispatcher elif** at [scripts/video_intel.py:4143](../../scripts/video_intel.py#L4143).
- **Mode-to-artifact mapping** at [scripts/video_intel.py:3533-3537](../../scripts/video_intel.py#L3533) (`_MODE_ARTIFACT_PATTERNS`). `prune-shorts` doesn't need this — it deletes ALL siblings sharing a prefix via `channel_dir.glob(f"{prefix}.*")`, identical to [_apply_dedupe_group:3640](../../scripts/video_intel.py#L3640).
- **Per-channel video_id cache** must be invalidated after deletion: [_invalidate_video_id_cache:3643](../../scripts/video_intel.py#L3643).
- **Per-channel config flag pattern** (channel + global fallback): mirror `auto_concepts` at [scripts/video_intel.py:1971](../../scripts/video_intel.py#L1971): `ch.get("skip_shorts", config.get("skip_shorts", True))`.
- **`cmd_scan` filter insertion point** is between line 1965 (post-fetch) and line 1989 (pre-`is_processed`). The new `skip_shorts` filter runs there — after `record_alt_title_if_rotated()` (1986), before the `is_processed` filter (1989). Order matters because alt-title recording must run for ALL videos (including Shorts) so we don't lose SEO A/B-test signal if the user later flips `skip_shorts` to false.
- **YouTube client construction** is lazy via `require_youtube()`. Existing call sites: [scripts/video_intel.py:1917, 2271, 2429](../../scripts/video_intel.py#L1917). Existing single-video `videos().list(part='snippet')` at 2272 and 2430 — adding `part='snippet,contentDetails'` (or a separate `part='contentDetails'` call) follows the same shape.
- **Test mocking convention** (per `tests/test_video_id_dedup.py:394-403`): `monkeypatch.setattr(vi, "<helper>", lambda ...)`. The codebase patches functions, not the network. Wrap the redirect check in a top-level `_is_youtube_short_url(video_id) -> bool` helper so tests can swap it cleanly.
- **No HTTP mocking library is in use** (`requests_mock`, `responses` not present). We do not need one — `_is_youtube_short_url` becomes the patch boundary. One thin integration test against an `httpx.MockTransport` is acceptable for smoke coverage of the actual HTTP shape (HEAD + `follow_redirects=False`).
- **httpx is the existing HTTP client** ([scripts/gemini_common.py:103](../../scripts/gemini_common.py#L103)). Use `httpx.head(...)` for the redirect check. No new dependency.

### Institutional learnings

- [docs/solutions/workflow-issues/full-sdlc-chain-via-context-packets-20260423.md](../../docs/solutions/workflow-issues/full-sdlc-chain-via-context-packets-20260423.md) §4: Gate 1 smoke test (run on real input, capture observable signal) is required before merge. Gate 2 (destructive preview) is required when shared state mutates. Both apply here. The chain ends at merge-ready PR with Gate 1 artifact attached.
- [docs/brainstorms/2026-04-22-video-id-dedup-requirements.md](../../docs/brainstorms/2026-04-22-video-id-dedup-requirements.md) (referenced in CLAUDE.md): the dedupe contract — dry-run default, `--apply` to mutate, manual derived-artifact rebuild. Direct precedent for `prune-shorts`.
- [CLAUDE.md](../../CLAUDE.md) Code Review Guardrails: §1 bounded retries (no infinite retry on `is_short()` failure), §3 timestamps are data (irrelevant here — no chunk rendering), §4 skill-parity (SKILL.md ships in same PR), §5 video_id is identity (deletion keys on video_id), §6 out-of-scope cleanup (no edits to `docs/plans/`, `docs/solutions/`, `work/**`).
- [specs/agent-rules.md](../../specs/agent-rules.md) §1 surgical changes, §3 TDD + test naming `test_<what>_<when>_<expected>`.

### External references

None needed. Codebase has 6 existing argparse subcommands as reference, the dedupe pattern is exact precedent, and YouTube `videos.list(part='contentDetails')` semantics are stable. Skipped external research per Phase 1.2 gate.

## Key Technical Decisions

### D1. `is_short()` lives as a top-level helper in `scripts/video_intel.py`

Not a new module. `gemini_common.py` is for Gemini-specific concerns; YouTube classification belongs alongside `is_processed()`, `_load_video_id_index()`, `video_file_prefix()`. **Rationale**: existing helper-function locality. Adding a new module for one predicate is over-architecture.

### D2. Two-signal predicate: duration < 60s OR `/shorts/<id>` HEAD returns 200

Per issue #37. Both signals required because YouTube raised the Shorts cap to 3 minutes in late 2024; duration-only misses 60-180s Shorts. Aspect ratio is not reliably exposed by YouTube Data API. Hashtag sniffing is user-editable. **Rationale**: yt-dlp uses the same redirect signal. No quota cost on the redirect check.

### D3. Both `cmd_scan` and `cmd_prune_shorts` use `enrich_with_durations` (batched 50 per call)

Unit 2's `enrich_with_durations` is the single bulk-fetch helper used by both paths. **Rationale**: the helper is already in scope for prune-shorts; reusing it in scan avoids two divergent code paths and drops scan's per-channel quota cost from 200 calls to ~4 (200 candidates / 50). Quota math: 12 channels × 4 calls = 48 units per scan. The earlier "match existing per-video pattern" was a stylistic preference, not a constraint — the existing per-video `videos.list(part='snippet')` calls at lines 2272 and 2430 are unrelated single-video lookups, not list enrichment.

**Quota-exhaustion contract**: if `videos.list` returns HTTP 403 quota-exceeded mid-scan, the scan **aborts with an actionable message** rather than silently fail-safing to long-form (which would silently admit Shorts the filter was supposed to drop). This is distinct from D8's per-video classification ambiguity — quota exhaustion is a global API-state failure that warrants stopping rather than continuing in degraded mode.

### D4. Cache the redirect check with `functools.lru_cache(maxsize=None)` per process

`fetch_selective_videos` can re-encounter the same video_id across selective-mode dedup. The redirect status of a video doesn't change mid-process. **Rationale**: trivial correctness win, zero risk.

### D5. `cmd_scan` filter inserts at line 1986 boundary (after alt-title recording, before `is_processed` filter)

Alt-title recording must run for ALL videos so SEO A/B-test signal survives even if `skip_shorts` is flipped from true to false later. Filter inserts BEFORE the `is_processed` filter so `is_processed` doesn't see Shorts at all. **Rationale**: preserves a continuous corpus signal while honoring probe-before-pay.

### D6. New scans persist `duration_seconds` into meta.json

Forward-fix. When `cmd_scan` calls `videos.list(part='contentDetails')` for the filter, it already has the duration. Persisting it means future `prune-shorts` runs need only fetch durations for legacy metas (those produced before this PR), not the entire corpus. **Rationale**: amortize the API cost over time. Field name `duration_seconds: int` lives alongside existing meta fields like `published`, `processed`, `transcript_status`.

### D7. `prune-shorts` walk strategy: disk-first with on-demand YouTube fallback

Read all meta.json files per channel. For metas with `duration_seconds` already present (post-PR scans), use that. For legacy metas missing the field, batch them into `videos.list(id=v1,...,v50, part='contentDetails')` calls. **Rationale**: avoids a corpus-wide re-fetch when we already know durations from recent scans. Fail-safe: if YouTube returns nothing for a video_id (e.g. video deleted), skip it from the prune candidate list — `prune-shorts` does not delete videos it cannot classify.

### D8. Fail-safe to long-form on classification ambiguity

If `videos.list` returns no `contentDetails`, or the redirect check throws, treat the video as long-form (do NOT include in Shorts list, do NOT skip in scan). **Rationale**: false negatives (a Short slipping through as long-form) are recoverable; false positives (deleting a real video) are not. CLAUDE.md "bounded retries only" — one API attempt, one redirect attempt, then long-form classification. No retry loop.

### D9. Test boundaries patch the helper, not the network

Wrap the redirect check in `_is_youtube_short_url(video_id) -> bool`. Tests use `monkeypatch.setattr(vi, "_is_youtube_short_url", ...)`. Mirrors the existing `tests/test_video_id_dedup.py` convention. One thin integration test against `httpx.MockTransport` validates HEAD + `follow_redirects=False` shape — kept separate from unit tests so most of the suite runs offline.

## Open Questions

### Resolved during planning

- **Where does `is_short()` live?** Top-level helper in `video_intel.py` (D1).
- **Cache the redirect check?** Yes, `functools.lru_cache(maxsize=None)` (D4).
- **Batch `videos.list`?** Yes — `enrich_with_durations` (Unit 2) is the single bulk-fetch helper used by both `cmd_scan` and `cmd_prune_shorts`. Batched 50-per-call. Resolves earlier D3 contradiction (D3 revised).
- **Where to wire in `cmd_scan`?** Between line 1986 and 1989 (D5).
- **Walk strategy for prune?** Disk-first with on-demand YouTube fallback for legacy metas (D7).
- **HEAD library?** `httpx` — declared explicitly in `pyproject.toml` (Unit 1) so the project doesn't lean on a transitive that could disappear in a future `google-genai` release.
- **Retry on transient `_is_youtube_short_url` failures?** One bounded retry on 5xx/timeout, then fail-safe to long-form (Unit 1). Per CLAUDE.md "bounded retries only" — one retry is bounded.
- **Test scaffolding?** Patch the function, not the network. One `MockTransport` test for HTTP shape (D9). Single test file `tests/test_skip_shorts.py` per codebase precedent.

### Deferred to implementation

- **Exact log message format for skipped Shorts in scan.** Implementer picks a format consistent with existing INFO logs. Suggestion: `"  Skipping Short: %s - %s (%ds)"` mirroring the alt-title log style.
- **Exact column widths in dry-run output.** Implementer picks values that don't truncate typical YouTube titles (~80 chars).
- **Whether to surface a per-channel kept count.** Probably yes for transparency, but the precise log line shape is implementer's call.

## Implementation Units

- [ ] **Unit 1: `is_short()` predicate, helpers, and unit tests**

**Goal:** Land the foundational classifier with full coverage. Nothing else depends on the predicate yet, so this unit is testable in isolation and lands first.

**Requirements:** R1, R8 (fail-safe), R10 (test coverage).

**Dependencies:** None.

**Files:**
- Modify: `scripts/video_intel.py`
- Modify: `pyproject.toml` (declare `httpx` as a direct dependency — currently transitive via `google-genai`)
- Create: `tests/test_skip_shorts.py` (consolidated test file for the entire feature — see Unit-organization note at the end of this section)

**Approach:**
- Add three top-level helpers in `scripts/video_intel.py`:
  - `_parse_iso8601_duration(iso: str) -> int | None` — handles `PT47S`, `PT1M30S`, `PT12M`, `PT1H4M3S`. Returns seconds, or `None` if unparseable.
  - `_is_youtube_short_url(video_id: str) -> bool` — `httpx.head(f"https://www.youtube.com/shorts/{video_id}", follow_redirects=False, timeout=5.0)`; returns `True` on HTTP 200, `False` on **any other status** (YouTube empirically returns 303, not 302, for non-Shorts — generalizing to "non-200 → False" avoids locking in a specific redirect code that could change). On `httpx.HTTPError`, transient 5xx (500-599), or timeout: **one bounded retry** with 0.5s sleep, then return `False` (fail-safe to long-form). Per CLAUDE.md "bounded retries only" — one retry, not a loop. `@functools.lru_cache(maxsize=None)`. **Test discipline**: test files always patch and call via `vi._is_youtube_short_url(...)`, never `from scripts.video_intel import _is_youtube_short_url` directly — module-level patching defeats the cache cleanly.
  - `is_short(video_id: str, duration_iso: str | None) -> bool` — top-level. Returns `True` if duration parses to <60s; otherwise calls `_is_youtube_short_url(video_id)`. Returns `False` (long-form) on classification failure (D8 fail-safe).
- Place helpers near `_load_video_id_index` (~line 951) so they sit with other top-level YouTube-related helpers.

**Unit-organization note**: per codebase precedent ([tests/test_video_id_dedup.py](../../tests/test_video_id_dedup.py) covers helper + cmd integration in one file), all Shorts-feature tests land in **one file** `tests/test_skip_shorts.py`. Units 2-4 reference the same file; do not create separate files per unit.

**Execution note:** Test-first. Write the test file with all scenarios in RED state, then land helpers minimally to GREEN.

**Patterns to follow:**
- `_load_video_id_index` at [scripts/video_intel.py:951](../../scripts/video_intel.py#L951) — top-level helper with module-level docstring style.
- Lazy-import discipline: `httpx` is fine to import at module top since `gemini_common` already imports it lazily and the import is cheap.

**Test scenarios:**
- Happy path: `is_short("abc", "PT47S")` returns `True`. (duration < 60s)
- Happy path: `is_short("abc", "PT12M30S")` with `_is_youtube_short_url` mocked to return `False` returns `False`.
- Edge case: `is_short("abc", "PT1M30S")` (90s, raised-cap Short) with `_is_youtube_short_url` mocked to return `True` returns `True`.
- Edge case: `is_short("abc", "PT1M30S")` with `_is_youtube_short_url` mocked to return `False` returns `False`.
- Edge case: `is_short("abc", None)` — no duration data — falls back to redirect check.
- Edge case: `is_short("abc", "BOGUS")` — unparseable — falls back to redirect check.
- Error path: `_is_youtube_short_url` raises `httpx.HTTPError` — outer `is_short` returns `False` (fail-safe to long-form, per D8).
- Error path: `_is_youtube_short_url` against transport that returns HTTP 503 then 200 → `True` (one bounded retry succeeded).
- Error path: `_is_youtube_short_url` against transport that returns 503 twice → `False` (retry exhausted, fail-safe).
- Edge case: `_parse_iso8601_duration("PT1H")` returns 3600.
- Edge case: `_parse_iso8601_duration("garbage")` returns `None`.
- Integration (one test): `_is_youtube_short_url` against `httpx.MockTransport` returning 200 → `True`; same against 303 with `Location: /watch?v=<id>` → `False`. (Empirically YouTube returns 303 for non-Shorts; the test should exercise the actual contract, not a guessed 302.)

**Verification:**
- All test scenarios pass.
- `ruff format scripts/video_intel.py tests/test_is_short.py && ruff check scripts/video_intel.py tests/test_is_short.py` clean.
- Helper functions visible at module top level (importable as `video_intel.is_short`).

---

- [ ] **Unit 2: `enrich_with_durations()` for batched `videos.list` lookups**

**Goal:** A bulk-fetch helper used by `cmd_prune_shorts` (and reusable elsewhere). Batches up to 50 video_ids per `videos.list` call, returns a `dict[video_id, duration_iso | None]`.

**Requirements:** R8 (fail-safe to None on missing data), supports R10 (prune-shorts batching).

**Dependencies:** None (pure YouTube API helper). Lands before Unit 4 needs it.

**Files:**
- Modify: `scripts/video_intel.py`
- Create: `tests/test_enrich_durations.py`

**Approach:**
- Add `enrich_with_durations(youtube, video_ids: list[str]) -> dict[str, str | None]` near `fetch_channel_videos` (~line 474).
- Batch into chunks of 50 (YouTube API limit). For each batch: `youtube.videos().list(id=','.join(batch), part='contentDetails').execute()`.
- For each item in the response, extract `contentDetails.duration` (ISO 8601 string).
- For any video_id that did not appear in the response (deleted, members-only, etc.), map to `None`.
- No retry. One API attempt per batch. Per CLAUDE.md "bounded retries only".

**Execution note:** Test-first. Mock the `youtube` client to return canned responses including the missing-id case.

**Patterns to follow:**
- `fetch_channel_videos` at [scripts/video_intel.py:474](../../scripts/video_intel.py#L474) — same `youtube.X().list(...).execute()` shape.
- `fetch_keyword_videos` at [scripts/video_intel.py:603](../../scripts/video_intel.py#L603) — pagination pattern (not needed here since we slice in 50-batches ourselves).

**Test scenarios:**
- Happy path: 3 video_ids → `videos.list` called once with all three → returns dict with three entries.
- Edge case: 51 video_ids → two `videos.list` calls (50 + 1) → returns dict with 51 entries.
- Edge case: empty input list → no API call → returns empty dict.
- Error path: video_id not present in response → entry is `None` (not missing from dict).
- Edge case: response item missing `contentDetails` → entry is `None`.

**Verification:**
- Tests pass.
- `ruff` clean.

---

- [ ] **Unit 3: Wire `skip_shorts` filter into `cmd_scan`**

**Goal:** Default-on per-channel filter that drops Shorts before any Gemini call. Persists `duration_seconds` into meta.json on first successful scan.

**Requirements:** R2, R3, R6 (forward-fix duration_seconds), R10 (test coverage for scan-with-skip-on, scan-with-skip-off, legacy fallback).

**Dependencies:** Unit 1 (`is_short`), Unit 2 (`enrich_with_durations`).

**Files:**
- Modify: `scripts/video_intel.py` — add filter to `cmd_scan`; thread `duration_seconds` through the video dict into `process_mindmap`'s meta-write.
- Modify: `tests/test_skip_shorts.py` — append scan-integration test classes alongside Unit 1's helper tests (single-file convention per Unit 1's organization note).

**Approach:**

*Filter wiring in `cmd_scan`* (insertion point: between [scripts/video_intel.py:1986](../../scripts/video_intel.py#L1986) and [scripts/video_intel.py:1989](../../scripts/video_intel.py#L1989) — after alt-title recording, before `is_processed` filter):

- Read `skip_shorts = ch.get("skip_shorts", config.get("skip_shorts", True))`. Mirrors `auto_concepts` pattern at [scripts/video_intel.py:1971](../../scripts/video_intel.py#L1971).
- Always call `durations = enrich_with_durations(youtube, [v["video_id"] for v in videos])` (Unit 2). The fetch happens regardless of `skip_shorts` so we can persist `duration_seconds` to meta.json on scan success even when Shorts are NOT being filtered. (D6 forward-fix: future `prune-shorts` runs avoid re-fetching.)
- For each video in `videos`, set `v["duration_iso"] = durations.get(v["video_id"])`. Mutates the candidate-list dicts in place so the field travels downstream.
- If `skip_shorts` is true: filter `videos = [v for v in videos if not is_short(v["video_id"], v["duration_iso"])]`. Track two counters: `n_skipped_short` (filter matched) and `n_classification_failed` (`is_short` fail-safed to long-form due to API/network error — these videos are admitted but the count surfaces silent contamination risk).
- INFO log: `"  Skipped %d Shorts; %d classification failures (admitted as long-form — re-run prune-shorts later if needed)"`.

*Persisting `duration_seconds` into meta.json*: the threading mechanism is `v["duration_iso"]` populated in `cmd_scan` flows through to `process_mindmap`. In `process_mindmap` (the function that calls `update_meta` at [scripts/video_intel.py:1247-1257](../../scripts/video_intel.py#L1247)), before the `update_meta` call, parse the iso string with `_parse_iso8601_duration(video.get("duration_iso"))`. If non-None, add `duration_seconds: <int>` to the `meta_fields` dict. Backwards compatible — old metas without the field still work, and reads in `_collect_short_candidates` (Unit 4) tolerate missing fields.

**Quota-exhaustion abort path**: if `enrich_with_durations` raises an HTTP 403 quota-exceeded error (distinguishable from per-video missing entries via the exception type/status), abort `cmd_scan` for that channel with an actionable log message and continue to the next channel. Per D3 quota-exhaustion contract — silent fail-safe is wrong at this scale.

**Execution note:** Test-first. Pattern after `tests/test_video_id_dedup.py:test_scan_dry_run_does_not_call_alt_title_recorder`.

**Patterns to follow:**
- `auto_concepts` flag read at [scripts/video_intel.py:1971](../../scripts/video_intel.py#L1971).
- Test scaffolding at [tests/test_video_id_dedup.py:394-437](../../tests/test_video_id_dedup.py#L394).

**Test scenarios:**
- Happy path: channel without `skip_shorts` defaults to true, Shorts videos do not reach Gemini call site.
- Happy path: channel with `skip_shorts: false` — Shorts ARE included, scan proceeds normally.
- Edge case: empty video list (no candidates after fetch) — no `enrich_with_durations` call, no error.
- Edge case: all candidates are Shorts — log shows "0 to process" without crash.
- Integration: meta.json written after scan contains `duration_seconds: <int>` field.
- Edge case: `enrich_with_durations` returns `None` for one video_id — that video is treated as long-form (fail-safe), gets scanned, meta.json written without `duration_seconds`.
- Edge case: alt-title recording still fires for Shorts (don't lose SEO signal even when filtering).
- Error path: `enrich_with_durations` raises HTTP 403 quota-exceeded — `cmd_scan` aborts that channel with actionable error message; subsequent channels still process. (D3 quota contract.)
- Integration: classification-failure counter increments when `_is_youtube_short_url` raises — visible in log.

**Verification:**
- Tests pass.
- `ruff` clean.
- Manual sanity check: dry-run on chase_h_ai with `skip_shorts: true` (default) shows substantially fewer "to process" videos than without the flag.

---

- [ ] **Unit 4: `cmd_prune_shorts` subcommand + argparse + dispatcher**

**Goal:** Cleanup command for existing data. Dry-run-by-default. `--apply` deletes the named artifact types per Short — never sweeps the whole prefix glob. Mirrors `dedupe`'s contract but uses a stricter deletion allowlist.

**Requirements:** R4, R5, R6, R8, R9, R10 (test coverage for dry-run no-mutation, --apply deletes all four, legacy fallback, sidecar survival).

**Dependencies:** Unit 1, Unit 2.

**Files:**
- Modify: `scripts/video_intel.py` (new `cmd_prune_shorts`, `_collect_short_candidates`, `_apply_prune_shorts`, argparse registration, dispatcher elif)
- Modify: `tests/test_skip_shorts.py` (append prune-shorts test classes — single-file convention)

**Approach:**

*Helper: `_collect_short_candidates(channel_dir, youtube)` returns `list[tuple[Path, dict, int]]` — meta_path, meta_data, duration_seconds.*

- Walk `channel_dir.glob("*.meta.json")`, parse each.
- Partition metas: those with `duration_seconds` already present vs those needing fetch.
- Batch the missing-duration set via `enrich_with_durations(youtube, video_ids)` (Unit 2). Parse iso → seconds.
- For each meta, call `is_short(video_id, duration_iso)`. Keep matches; drop non-Shorts.
- Failure-mode: any meta missing video_id is skipped (cannot classify reliably).

*`_apply_prune_shorts(channel_dir, candidates)`:* for each candidate's meta_path, derive `prefix = meta_path.name.removesuffix(".meta.json")`. **Deletion uses an explicit suffix allowlist, NOT the whole-prefix glob `_apply_dedupe_group` uses** (CRITICAL — see Risks table).

The allowlist reuses `_MODE_ARTIFACT_PATTERNS` at [scripts/video_intel.py:3533-3537](../../scripts/video_intel.py#L3533) plus the meta and forensic-sidecar patterns:

```
PRUNE_SHORTS_DELETION_PATTERNS = (
    "{prefix}.mindmap.md",
    "{prefix}.mindmap.*.md",        # knowledge / light / heavy variants
    "{prefix}.transcript.md",
    "{prefix}.transcript.raw.txt",   # forensic sidecar from salvage path
    "{prefix}.transcript.raw.*.txt", # bounded-retry forensic sidecar
    "{prefix}.concepts.json",
    "{prefix}.meta.json",
)
```

For each pattern, `for path in channel_dir.glob(pattern.format(prefix=prefix)): path.unlink()`.

This is the **bug fix** for the dedupe pattern's wildcard-glob behavior. translate_video.py produces `{prefix}.en.srt` and `{prefix}.translate-bcs.txt` siblings (per CLAUDE.md). Those MUST survive `prune-shorts --apply` because translate-bcs is a separate workflow with its own intent. A user who has translated a Short to BCS still owns that translation; the Shorts-prune cleans the curate-side artifacts, not the translate-side ones.

After the deletion loop: call `_invalidate_video_id_cache(channel_dir)` (mirrors [scripts/video_intel.py:3643](../../scripts/video_intel.py#L3643)).

*`cmd_prune_shorts(args, config)`:* mirrors `cmd_dedupe`'s skeleton:
- `require_channels_config(config)`, `resolve_output_dir(config)`.
- Iterate channels (filtered by `--channel` if set).
- For each channel: build `youtube` client lazily (only if at least one meta needs duration fetch — saves API calls on already-enriched channels).
- Print dry-run table: `title (truncated to 60ch) | duration (M:SS) | url | artifacts (always 4)`.
- Per-channel summary: `"  channel_name: N shorts, N*4 artifacts"`.
- If `apply`: call `_apply_prune_shorts(channel_dir, candidates)` per channel.
- Footer mirrors dedupe's: "Re-run with --apply to execute" or "Next steps: run `taxonomy-build` and `index --force`".

*Argparse registration* (mirror dedupe at [scripts/video_intel.py:4097-4106](../../scripts/video_intel.py#L4097)):
```
prune_parser = subparsers.add_parser("prune-shorts", help="Find and delete YouTube Shorts artifacts (mindmap, transcript, concepts, meta)")
prune_parser.add_argument("--channel", help="Restrict to this channel (default: all configured channels)")
prune_parser.add_argument("--apply", action="store_true", help="Actually mutate disk. Default is dry-run (report only).")
```

*Dispatcher elif* after [scripts/video_intel.py:4144](../../scripts/video_intel.py#L4144): `elif args.command == "prune-shorts": cmd_prune_shorts(args, config)`.

**Execution note:** Test-first. Use `tests/test_video_id_dedup.py` `_write_meta` and `_touch` helpers as templates (probably copy them into the new test file rather than refactoring shared scaffolding).

**Patterns to follow:**
- `cmd_dedupe` at [scripts/video_intel.py:3662](../../scripts/video_intel.py#L3662) — full pattern.
- `_apply_dedupe_group` at [scripts/video_intel.py:3607](../../scripts/video_intel.py#L3607) — deletion sweep pattern.

**Test scenarios:**
- Happy path: dry-run on a channel with one Short (47s) → output lists the Short, no files deleted.
- Happy path: `--apply` on the same channel → all 4 mindmap/transcript/concepts/meta files for that Short are removed; the long-form video's artifacts remain.
- **Critical: sidecar survival.** Set up `{prefix}.en.srt`, `{prefix}.translate-bcs.txt`, `{prefix}.unrelated.txt` alongside the four target artifacts. After `--apply`, assert all three sidecars remain on disk. Asserts the suffix-allowlist contract; would catch a regression to the wholesale prefix-glob deletion.
- Happy path: `{prefix}.mindmap.knowledge.md` and `{prefix}.mindmap.light.md` (variant mindmaps) ARE deleted by `--apply` (covered by `{prefix}.mindmap.*.md` pattern).
- Happy path: `{prefix}.transcript.raw.txt` (forensic sidecar) IS deleted (covered by allowlist).
- Edge case: meta.json without `video_id` field — skipped silently (cannot classify; `prune-shorts` is conservative).
- Edge case: meta.json with `duration_seconds` present and ≥60 — not in candidates list (no API call needed).
- Edge case: meta.json without `duration_seconds` — triggers `enrich_with_durations` call; result merges into candidate decision.
- Edge case: video_id not returned by YouTube (deleted) — `is_short` returns `False` (fail-safe), video stays.
- Edge case: empty channel directory — no candidates, no error.
- Edge case: `--channel nonexistent` — same behavior as `dedupe --channel nonexistent` (graceful empty result).
- Integration: after `--apply`, `_invalidate_video_id_cache` was called for each affected channel.

**Verification:**
- Tests pass.
- `ruff` clean.
- `python scripts/video_intel.py prune-shorts --help` shows reasonable help text.
- `python scripts/video_intel.py prune-shorts` (no flag) shows summary line ending in "Re-run with --apply to execute."

---

- [ ] **Unit 5: Skill parity, config example, CLAUDE.md, plugin version, mutex test**

**Goal:** All non-code surfaces updated in the same PR per the skill-parity guardrail. No follow-up commits required to make the new feature reachable through the skill or visible in repo conventions.

**Requirements:** R7 (skill-parity), R11 (Gate 1 documentation).

**Dependencies:** Unit 4 (final command shape must be settled).

**Files:**
- Modify: `skills/video-intel/SKILL.md` (frontmatter trigger phrases, intent table, new "Prune YouTube Shorts" section)
- Modify: `skills/video-intel-search/SKILL.md` (line 162 routing table: add a row mapping prune-shorts → curate)
- Modify: `tests/test_skill_descriptions.py` (extend `CURATE_TRIGGERS` list at line 54 with new prune-shorts trigger phrases — commits the mutex assertion, no hand-wave)
- Modify: `config.yaml.example` (add `skip_shorts` example with comment)
- Modify: `CLAUDE.md` (Architecture section: document `prune-shorts` and `skip_shorts`; Code Review Guardrails: add "Shorts identity is `duration < 60s OR /shorts/ redirect 200`" entry)
- Modify: `.claude-plugin/plugin.json` (bump `version` from 1.10.0 → 1.11.0 — this is a behavioral change to scan defaults; per CLAUDE.md Release Process the version field tracks user-facing behavior changes)

**Approach:**

*`skills/video-intel/SKILL.md` updates*:
- Frontmatter description (lines 1-28): append to trigger phrases — `"prune shorts"`, `"remove shorts from corpus"`, `"delete YouTube Shorts"`, `"clean up shorts"`.
- Intent table (~line 105-117): add a row mapping `"prune shorts" / "remove shorts" / "shorts polluting the corpus"` to `prune-shorts` (with note: `**Always --dry-run first** — destructive on --apply`).
- New section after `### Clean up title-rotation duplicates` (~line 379), titled `### Prune YouTube Shorts`. Bash examples:
```
# Dry-run
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" prune-shorts --channel chase_h_ai

# Apply (after reviewing dry-run output)
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" prune-shorts --channel chase_h_ai --apply

# Don't forget the post-apply rebuild:
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" taxonomy-build
python "${CLAUDE_SKILL_DIR}/../../scripts/video_intel.py" index --force
```

*`config.yaml.example` update*: add to the `example_creator` block:
```yaml
  - name: example_creator
    url: https://youtube.com/@example
    auto_transcript: all
    since: 90d
    # skip_shorts: true   # default; set to false if creator does substantive Shorts
```

*`CLAUDE.md` updates*:
- Architecture section: under the `scan` description (~line 84), add: "Filters Shorts via the `skip_shorts` per-channel flag (default `true`). Detection: duration < 60s OR `/shorts/<id>` HEAD returns 200."
- Architecture section: add a new subsection for `prune-shorts` after `dedupe` description, ~3 lines.
- Code Review Guardrails section: add a new bullet:
  > **Shorts identity is `duration < 60s OR /shorts/<id> redirect 200`.** Do not substitute aspect ratio without API support — YouTube Data API does not reliably expose it. The redirect check is YouTube's own classifier and is what yt-dlp uses. Reviewers: grep for `is_short` in any diff touching scan or prune-shorts logic — any new path that uses a different signal needs pushback.

**Test scenarios:**

- `tests/test_skill_descriptions.py`: extend the parametrized `CURATE_TRIGGERS` list (line 54) with the new trigger phrases: `"prune shorts"`, `"clean up shorts"`, `"remove shorts from corpus"`, `"delete YouTube Shorts"`. Existing parametrized tests at lines 94 and 100 will assert these phrases appear in `video-intel`'s description AND do NOT appear in any other skill's description. **This is a deliverable in this PR, not a maybe.**
- Add one new test: `test_prune_shorts_appears_in_curate_routing_table` — reads `skills/video-intel-search/SKILL.md`, asserts the routing table at line 162 contains a row mentioning `prune-shorts` (or `prune shorts`).

**Verification:**
- `pytest tests/test_skill_descriptions.py` passes (existing test of skill description mutex still passes; new parametrized assertions also pass).
- `python scripts/video_intel.py prune-shorts --help` output matches what `SKILL.md` claims it does.
- `cat .claude-plugin/plugin.json` shows `"version": "1.11.0"`.
- Manual read: `CLAUDE.md` Architecture and Code Review Guardrails sections render cleanly.
- `translate-bcs/SKILL.md` is **unchanged** — the BCS translation skill is operationally separate from curate per CLAUDE.md.

## System-Wide Impact

- **Interaction graph.** `cmd_scan` gains a pre-Gemini filter step. `cmd_prune_shorts` is a new top-level entry point; no other command calls into it. `enrich_with_durations` is a new helper; not called from any existing path beyond the two we add.
- **Error propagation.** Failures in `_is_youtube_short_url` or `enrich_with_durations` propagate as a long-form classification (fail-safe). No exception bubbles out of `is_short()`. Failure to delete a single artifact in `_apply_prune_shorts` lets `unlink()` raise — which mirrors `_apply_dedupe_group`'s implicit behavior. We do not catch and continue silently; partial deletion is loud.
- **State lifecycle risks.** `_apply_prune_shorts` mutates disk and the per-channel video_id index cache. The cache invalidation must happen after deletion or future `is_processed()` calls will return false-positives until the next process restart. Mirror `_apply_dedupe_group:3643`.
- **API surface parity.** `cmd_prune_shorts`'s dry-run/apply contract matches `cmd_dedupe` exactly (same flag names, same default). Reviewers should expect identical UX.
- **Integration coverage.** Most behavior is testable with mocks (the `monkeypatch.setattr` pattern is well-established in this codebase). The `httpx.MockTransport` integration test for the redirect check covers the one piece mocks alone don't prove (HTTP shape).
- **Unchanged invariants.** `cmd_scan`'s output for non-Shorts channels with `skip_shorts: false` is byte-identical to current behavior. `cmd_dedupe` is untouched. `is_processed()` is untouched. `_load_video_id_index` is untouched. `translate_video.py` is untouched. The vector index format does not change. Existing meta.json files without `duration_seconds` are still readable; the field is additive and optional.

## Risks & Dependencies

| Risk | Mitigation |
|---|---|
| Per-process LRU cache means a long-running scan that hits 10,000+ unique video IDs grows unbounded. | `maxsize=None` is documented choice; scan runs are short-lived (one process per channel-set). For the typical 3-12 channel scan with hundreds of candidates, memory cost is trivial. If this changes, switch to `maxsize=10000`. |
| `videos.list` quota cost. Per D3 (batched-50): scan = 4 calls/channel × 12 channels = 48 units/scan. Plus existing `playlistItems().list()` enumeration cost (~7 units/channel for chase_h_ai with 180d). YouTube default daily quota is 10,000 units. | Comfortable margin. The 403 quota-exceeded path aborts cleanly per D3 instead of silently fail-safing — surfaces the problem instead of hiding it. |
| `httpx.head` to `/shorts/<id>` could be rate-limited by YouTube. | LRU cache eliminates re-checks. One bounded retry on 5xx absorbs transient blips. Per-process scan checks ~hundreds of unique IDs, well below any reasonable rate limit. If we see 429s in practice, add `time.sleep(0.05)` between checks. Not pre-emptively. |
| `/shorts/<id>` redirect signal could break overnight (YouTube changes contract, A/B tests, anti-bot defenses). Default-on filter would silently skip everything. | **Two-sided Gate 1 smoke test** (R11): the PR-attached dry-run output must show ≥150 Shorts on chase_h_ai AND ≤2 false positives among the first 20 candidates from a known-long-form channel (natebjones). Asymmetric-only Gate 1 would miss this regression. The `n_classification_failed` counter on `cmd_scan` (Unit 3) surfaces silent fail-safe events to the user post-merge. |
| **`prune-shorts --apply` deletes unrelated sidecar files** (`.en.srt`, `.translate-bcs.txt`, etc.) that share the prefix. | **Mitigated by suffix allowlist (Unit 4 critical fix).** Deletion uses `PRUNE_SHORTS_DELETION_PATTERNS` (explicit list of `.mindmap*.md`, `.transcript.md`, `.transcript.raw*.txt`, `.concepts.json`, `.meta.json`), NOT the whole-prefix glob `_apply_dedupe_group` uses today. Test scenario in Unit 4 explicitly seeds `.en.srt` / `.translate-bcs.txt` / `.unrelated.txt` and asserts they survive `--apply`. |
| `prune-shorts --apply` deletes the wrong videos due to misclassification. | Dry-run by default. Gate 1 smoke test (real-corpus dry-run output attached to PR) catches systematic misclassification before merge. Manual eyeball of 60-180s edge-case rows. False positives in classification are recovered by RE-running scan to repopulate (it would be re-fetched from YouTube). |
| A non-Short slips through (false negative in `is_short()`). | Acceptable — false negatives are recoverable (run prune-shorts again later). False positives are not, and we fail-safe to long-form on any classification ambiguity (D8). |
| Default-on `skip_shorts` is invasive — existing users get a behavior change on `git pull`. | (a) Plugin version bump 1.10.0 → 1.11.0 (Unit 5) — minor-version semver = behavioral change. (b) One-time INFO log on first scan post-upgrade announcing the new default and pointing to per-channel `skip_shorts: false` opt-out. (c) PR description includes a "Behavior change" callout. |
| `unlink()` failures on Google Drive File Stream (Windows path quirk; sync collisions). | Mirrors `_apply_dedupe_group:3641` pattern that has shipped without issue. Errors surface (we don't catch silently); user re-runs after sync settles. Same defensible posture. |

## Documentation / Operational Notes

- After merge, the user runs:
  1. `git pull`
  2. `python scripts/video_intel.py prune-shorts --channel chase_h_ai` — read the dry-run output.
  3. Spot-check a few of the 60-90s edge-case videos (these are most likely to be ambiguous).
  4. `python scripts/video_intel.py prune-shorts --channel chase_h_ai --apply` — only after the dry-run passes eyeball.
  5. Repeat for other channels as desired.
  6. `python scripts/video_intel.py taxonomy-build`
  7. `python scripts/video_intel.py index --force`
- Future scans automatically respect `skip_shorts: true` (new default). No additional action.
- If the user wants to backfill `duration_seconds` for ALL legacy metas at once (instead of on-demand during prune), that's a separate small feature — out of scope here.

## Sources & References

- **Origin issue:** [GitHub #37](https://github.com/dzivkovi/video-intel/issues/37)
- **Triage note:** [work/2026-04-24/01-shorts-pollution-and-sdlc-chain-plan.md](../../work/2026-04-24/01-shorts-pollution-and-sdlc-chain-plan.md)
- **SDLC chain pattern:** [docs/solutions/workflow-issues/full-sdlc-chain-via-context-packets-20260423.md](../../docs/solutions/workflow-issues/full-sdlc-chain-via-context-packets-20260423.md)
- **Dedupe contract precedent:** [scripts/video_intel.py:3662](../../scripts/video_intel.py#L3662) (`cmd_dedupe`), [scripts/video_intel.py:3607](../../scripts/video_intel.py#L3607) (`_apply_dedupe_group`), [docs/brainstorms/2026-04-22-video-id-dedup-requirements.md](../../docs/brainstorms/2026-04-22-video-id-dedup-requirements.md)
- **YouTube Data API:** `videos.list` reference — `contentDetails.duration` ISO 8601 format. https://developers.google.com/youtube/v3/docs/videos/list
- **Test mocking convention:** [tests/test_video_id_dedup.py:394-437](../../tests/test_video_id_dedup.py#L394) — patch the function, not the network.
- **Project rules:** [CLAUDE.md](../../CLAUDE.md), [specs/agent-rules.md](../../specs/agent-rules.md).
