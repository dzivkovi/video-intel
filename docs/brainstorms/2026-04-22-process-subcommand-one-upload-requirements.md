# Process Subcommand - One Upload for Full Local-File Pipeline - Requirements

**Date:** 2026-04-22

**Status:** drafted autonomously on the user's instruction ("trust your
judgment, run the CE loop with less of my attention"). Decisions below
reflect defensible defaults grounded in conversation evidence; user can
override before or after implementation.

## Problem

For local-file ingestion, running mindmap and transcript on the same MP4
uploads the bytes to Gemini Files API twice. Each subcommand
(`cmd_mindmap` at `scripts/video_intel.py:1995`, with upload calls at
`:2049` and `:2071`; `cmd_transcript` at `:2150`, with upload calls at
`:2238` and `:2243`) calls `upload_local_video()` (defined at
`scripts/video_intel.py:1265`) independently and receives a distinct
`file_uri`. The `file_uri` is deliberately not persisted to disk (it
expires in 48h; a persisted URI would rot), so no natural mechanism
exists for the second subcommand to reuse the first's handle.

Consequences today:

1. Wasted bandwidth: 43 MB example uploaded twice within 2 seconds
   (confirmed 2026-04-22 at 23:00:24 / 23:00:26, file IDs
   `hvnbfnr5yht1` and `n1bp10f9igvo`).
2. Missed implicit-cache opportunity: Gemini's implicit cache keys on
   prefix similarity within a short time window. Two different
   `file_uri` values for the same bytes likely miss the cache even when
   the underlying video content is identical.
3. Wasted wall-clock: ~10-25 seconds per video on the second upload
   plus Gemini's PROCESSING state wait.
4. No visibility into whether any caching is working: we never read
   `response.usage_metadata` fields, so implicit-cache hit rate is
   opaque.

## Evidence

From this conversation:

- Single 9:46 video run produced two file IDs for same 43.1 MB bytes.
- Gemini docs (retrieved via context7, `/websites/ai_google_dev_gemini-api`):
  *"Implicit cache storage is enabled by default for all Gemini 2.5 and
  newer models"*, *"place large and common content at the beginning of
  your prompt and send requests with similar prefixes within a short
  time window"*, *"number of tokens that were cache hits can be found
  in the `usage_metadata` field of the response object"*.
- Code at `scripts/video_intel.py:1030-1035` already orders Parts
  correctly (video Part first, prompt second). The prefix is
  cache-friendly; we just break it by re-uploading.
- Meta.json produced today shows `modes_completed: ["scan", "transcript"]`
  where "scan" is the mindmap pass. Existing convention; preserve it.

## Goal

Primary (deterministic, delivered regardless of Gemini behavior):

1. Eliminate the redundant second upload for local files when both
   mindmap and transcript are desired. Saves bandwidth (43 MB per
   example) and ~10-25s wall-clock per video. Unit-testable.
2. Instrument `usage_metadata` on every `generate_content` call inside
   the new subcommand so implicit-cache effectiveness becomes a
   measurable number instead of a guess. Unit-testable via log
   assertion.

Hypothesis under test (confirmed or refuted by the instrumentation
shipped in this PR, not by the PR's own success criteria):

1. Gemini's implicit cache will fire on the transcript call when it
   shares a `file_uri` with an immediately-preceding mindmap call,
   yielding reduced billed input tokens on the transcript call. If
   logs show this, a follow-up PR adopting explicit caching becomes
   unnecessary. If logs show cache misses, this PR still delivers
   goals 1 and 2; explicit caching becomes a motivated follow-up.

Single-mode workflows (user asks only for a mindmap; user asks only for
a transcript) stay unchanged. Existing `mindmap` and `transcript`
subcommands retain their current behavior and defaults.

## Scope decisions

### 1. New `process` subcommand

Add `process --file <PATH>` that chains mindmap -> transcript -> concepts
on a single `upload_local_video()` call. Existing `mindmap` and
`transcript` subcommands are not modified beyond any shared helpers
extracted during implementation.

Rationale for a new subcommand rather than a flag on existing commands:
the verb `process` matches how `scan` already chains these modes for
YouTube. Flag-on-existing-command options (`mindmap --also-transcript`
or `transcript --with-mindmap`) attach composed behavior to a name that
describes only one step, which hurts the cognitive-load principle from
`specs/agent-rules.md` §1.

### 2. CLI surface mirrors `mindmap --file` / `transcript --file`

- `--file <PATH>` required.
- `--channel <NAME>` optional (overrides parent-folder inference).
- `--video-id`, `--title`, `--date` optional identity flags (honored by
  `resolve_local_file_identity()` in the same priority order as existing
  `--file` paths).
- `--start`, `--end` optional segment clipping (passes through to both
  the mindmap and transcript Gemini calls so clipping is consistent
  across artifacts).
- `--force` regenerates existing outputs.
- Top-level `--model` / `-m` override still applies.

No new flags beyond what the two subcommands already accept. Users who
know one command already know this one.

### 3. Upload-once invariant

`process` calls `upload_local_video()` exactly once per invocation and
threads the returned `file_uri` into both the mindmap and transcript
generation paths. This is the single correctness bar for the feature.

Both `generate_content` calls inside `process` (mindmap and transcript)
continue to pass through the existing `get_retry_delay()` posture in
`scripts/gemini_common.py:96-121` for transient 429 / 5xx. That path is
unchanged.

File-expiry fallback applies to **either** call, not just transcript.
If `generate_content` surfaces a file-not-found / file-expired error,
log the event, re-upload once, retry once, then fail. In practice this
should almost never fire on a run that completes inside a few minutes
(Files API TTL is 48h), but the fallback guards the mindmap artifact
already on disk when transcript trips it, and guards the wasted-upload
case when mindmap trips it. Bounded retries only: one re-upload, one
retry, then fail cleanly. The concrete Gemini exception type / HTTP
code / message substring that triggers the re-upload branch is left
to the plan phase (coherence + feasibility review flagged it — see
Open Questions).

### 4. Partial-success semantics

`process` is an orchestrator, not a transaction. It follows the
existing two-step meta.json pattern that `cmd_transcript` already uses
(identity block written before the first Gemini call, completion fields
written after each step returns):

1. **Before any Gemini call:** meta.json written with the identity
   block (video_id, channel, title, published, model, prompt). Acts as
   a "work started" marker.
2. **After mindmap call returns:** `*.mindmap.md` written; meta.json
   appends `"scan"` to `modes_completed`.
3. **After transcript call returns:** `*.transcript.md` written;
   meta.json appends `"transcript"` to `modes_completed`.
4. **After concepts call returns** (if channel is configured per §5):
   `*.concepts.json` written; meta.json appends `"concepts"` to
   `modes_completed`. If concepts is skipped because the channel is
   not configured, the meta.json remains with only
   `["scan", "transcript"]`.

If any step after the identity write fails, prior artifacts stay.
Mindmap persists even if transcript fails. Mindmap + transcript persist
even if concepts fails. Exit code 0 only if at least mindmap succeeded;
the command logs which modes completed and which did not.

The existing transcript JSON salvage + one bounded retry
(inside `call_gemini_video` around `scripts/video_intel.py:1573`)
continues to apply unchanged inside the orchestrator.

**Re-run semantics (no `--force`):** `process --file X.mp4` on a file
with existing artifacts follows the same idempotency rule as the
single-mode commands. If meta.json shows `modes_completed=["scan"]`
and no transcript file exists, `process` re-uploads (cannot reuse the
prior run's file_uri) and runs only the missing steps (transcript +
concepts), preserving the mindmap. This gives users a clean recovery
path after a crash or Ctrl-C without forcing a full regeneration.

**Re-run semantics (`--force`):** regenerates all three artifacts from
scratch in one shot, regardless of prior partial state. Meta.json is
rewritten with a fresh identity block. This is the intentional reset
button.

### 5. Concepts runs inline

Concepts extraction reads the mindmap text file, not the video, so it
does not need the `file_uri`. Including it inside `process` keeps the
"full pipeline" UX intact and costs nothing beyond the one text-only
Gemini call the user was going to make anyway.

Reuse target: the per-video helper `process_concepts()` at
`scripts/video_intel.py:1593`, **not** the batch command
`cmd_concepts` at `:2323`. `cmd_concepts` iterates configured channels
and walks every meta.json in each; `process` already has a single
video in hand and only needs the helper.

Configured-channel gating (parity with how `cmd_mindmap` and
`cmd_transcript` treat channel-less files today, lines 2021-2025 and
2189-2193 respectively): if `resolve_local_file_identity()` returns a
channel name that is present in `config.yaml`, concepts runs;
otherwise concepts is skipped with a `warning` log line. The run still
exits 0 because mindmap + transcript succeeded — the channel
configuration is a known limitation of the concept-extraction pipeline,
not a failure mode. Users can re-run `concepts --channel <name>` after
adding the channel to config. Exit-code rationale is spelled out in
Success criterion 2.

### 6. Observability bundle

A small helper `log_usage_metadata(response, label)` logs, at info
level, the fields Gemini exposes on every response:

- `prompt_token_count` - what we sent
- `cached_content_token_count` - what hit cache (implicit or explicit)
- `candidates_token_count` - what we got back
- `total_token_count` - sum

**Scoped to the two video-bearing `generate_content` calls that
`cmd_process` drives**: the mindmap call path (`call_gemini_video` at
`scripts/video_intel.py:1041`) and the transcript call path (same
function, same call site, different prompt). The text-only concepts
call (`call_gemini_text` around `scripts/video_intel.py:1573`) gets the
same helper since it runs inside `cmd_process` too; cache is unlikely
to fire there given the short prompt, but logging the fields keeps the
helper uniform and the log format consistent. The `cmd_nugget` call at
`scripts/video_intel.py:3356` is out of scope for this PR; do not
instrument it.

This observability is in-scope because it is the evidence the hypothesis
in Goal #3 rests on. Without it, shipping `process` and claiming it
saved tokens would be a speculative claim contradicting
`specs/agent-rules.md` §6 ("Verify, don't assume").

Log format keeps one line per call, machine-parseable enough for a
future grep-based cost report:

```text
usage mindmap prompt=77312 cached=0 candidates=1204 total=78516
usage transcript prompt=77312 cached=77000 candidates=14807 total=169119
```

### 7. Skill-parity

CLAUDE.md Code Review Guardrails: when a PR adds a new CLI subcommand,
the matching `SKILL.md` entry lands in the same PR. This PR updates
`skills/video-intel/SKILL.md` with natural-language routing for the new
command so the skill surface stays aligned with the CLI surface.

## Success criteria

1. Running `process --file X.mp4` uploads the MP4 exactly once; a unit
   test mocks `upload_local_video` and asserts call count == 1.
2. Mindmap + transcript artifacts produced on a successful run.
   Concepts artifact produced **when the channel is configured**; when
   the channel is not configured, concepts is skipped with a warning
   log line and exit code is still 0 (the behavior §5 specifies).
3. If the transcript call fails, the mindmap file and its corresponding
   meta.json entries remain intact on disk. Separate test covers:
   transcript succeeds, concepts fails - mindmap and transcript persist.
4. `usage_metadata` log line emitted for each `generate_content` call
   the orchestrator drives (mindmap, transcript, concepts when it
   runs). Log line contains all four token-count fields. Unit test
   asserts the log line is present and well-formed, **not** that
   `cached_content_token_count` has any particular value - that value
   is the observation the logs exist to capture, not a pass/fail
   threshold for this PR.
5. Re-running `process --file X.mp4` without `--force` after a partial
   failure (e.g., mindmap written but transcript missing) correctly
   skips the completed step and runs only the missing steps. Unit
   test covers this recovery path.
6. `ruff format . && ruff check . --fix && pytest -m "not integration" -q`
   passes.
7. `skills/video-intel/SKILL.md` updated with a natural-language entry
   for the new subcommand in the same commit.
8. CLAUDE.md Commands section updated with the new subcommand's usage
   example in the same commit.

## Non-goals

- **Explicit context caching** via `client.caches.create()`. Deferred
  until the usage_metadata logs reveal whether implicit caching is
  already sufficient. Building it without data violates the
  grounded-claims rule.
- **`--url` variant of `process`** for YouTube videos. `scan` already
  chains modes for YouTube and has no upload cost to eliminate.
  Different surface, different optimization; separate PR if later
  deemed worth it.
- **Parallelizing mindmap and transcript calls.** Sequential calls
  maximize the chance of an implicit-cache hit per the Gemini docs;
  parallel calls defeat that. Revisit only if logs show implicit cache
  fails anyway.
- **Refactoring `scan`'s three-call pattern** to use explicit caching.
  Worth doing later; out of scope for this PR.
- **Persisting `file_uri` to meta.json** as a reuse mechanism. The 48h
  TTL makes it a zombie value on disk; the upload-once invariant lives
  inside a single process, not across runs.
- **Auto-invoking `taxonomy-build` or `index` rebuild** after `process`.
  Same reasoning as the dedupe brainstorm: keep blast radius
  predictable.

## Risks worth naming in the PR description

1. **If implicit cache misses, the token-savings story is zero.** The
   PR still saves one upload's bandwidth and ~10-25s wall-clock per
   video, but the main cost argument (Gemini input tokens) depends on
   implicit caching firing. The observability logs will tell us
   whether this risk materialized; the PR ships the thermometer, not
   the fix.
2. **Orchestrator-level coupling** between mindmap and transcript is
   net new. Today the two subcommands are fully independent. The
   orchestrator introduces a shared failure surface (the single upload).
   Mitigation: partial-success semantics (§4) plus bounded fallback
   re-upload (§3).
3. **Meta.json write ordering** becomes a correctness concern. The
   existing subcommands each write meta.json once at the end of their
   own invocation. The orchestrator must write incrementally so a crash
   mid-run leaves a truthful state on disk. This is the subtle part of
   §4 and deserves explicit test coverage (unit test: kill after
   mindmap-write, re-run, observe idempotent recovery).

## References

- `scripts/video_intel.py:1995` - `cmd_mindmap` definition
- `scripts/video_intel.py:2049, :2071` - `cmd_mindmap` call sites for
  `upload_local_video`
- `scripts/video_intel.py:2150` - `cmd_transcript` definition
- `scripts/video_intel.py:2238, :2243` - `cmd_transcript` call sites
  for `upload_local_video`
- `scripts/video_intel.py:2323` - `cmd_concepts` definition (batch
  command; `process` reuses the per-video `process_concepts` helper
  at `:1593` instead)
- `scripts/video_intel.py:1265` - `upload_local_video` definition
- `scripts/video_intel.py:1066` - `process_mindmap` helper
- `scripts/video_intel.py:1435` - `process_transcript` helper
- `scripts/video_intel.py:1593` - `process_concepts` helper
- `scripts/video_intel.py:1030-1035` - current Parts ordering (video
  Part first, prompt second) - cache-friendly prefix
- `scripts/video_intel.py:1041, :1573` - the two `generate_content`
  call sites `cmd_process` drives (`call_gemini_video` and
  `call_gemini_text`). `:3356` is `cmd_nugget`'s call site, out of
  scope for this PR.
- `scripts/video_intel.py:664` - `resolve_local_file_identity`
  (identity resolution shared by the new subcommand)
- `scripts/gemini_common.py:96-121` - `get_retry_delay()` - existing
  transient-error retry classifier that `cmd_process` continues to
  delegate to for 429 / 5xx. File-expiry is a new branch, not a change
  to this function.
- `specs/agent-rules.md` §1, §6, §7 - cognitive load,
  verify-don't-assume, proceed-without-asking scope
- `CLAUDE.md` Code Review Guardrails - skill-parity rule, bounded
  retries, probe-before-you-pay (not directly in scope but the same
  posture informs §3)
- Memory: `project_model_selection.md` - current model is
  `gemini-3-flash-preview`, minimum implicit-cache threshold is 1024
  tokens for this model
- [Gemini caching docs](https://ai.google.dev/gemini-api/docs/caching) -
  authoritative source for implicit caching behavior

## Deferred / Open Questions

### From 2026-04-22 review

1. **File-expiry exception taxonomy (feasibility F5, coherence F004).**
   The re-upload-on-expiry branch in §3 requires naming the concrete
   Gemini SDK exception type / HTTP code / message substring that
   identifies "file not found / expired." Not specified here. Plan
   phase should verify against `google-genai` SDK (likely
   `FailedPrecondition` with message containing `File` / `not found`),
   land a unit test fixture for the error shape, and guard against the
   branch firing on unrelated transient errors that the existing
   `get_retry_delay()` already handles.
2. **Trajectory question: does `process` survive an explicit-caching
   follow-up? (product-lens trajectory).** If the observability logs
   shipped in this PR reveal implicit-cache misses, the natural
   follow-up is explicit context caching via `client.caches.create()`.
   That feature could be implemented inside the existing `mindmap` and
   `transcript` subcommands, potentially making `cmd_process` redundant
   for its original upload-dedup motivation. Decide at that time
   whether `cmd_process` remains as a pure UX affordance (one-command
   full pipeline, partial-success semantics) or is removed. This PR
   does not pre-commit either way; the question is left for the next
   data-driven decision.
