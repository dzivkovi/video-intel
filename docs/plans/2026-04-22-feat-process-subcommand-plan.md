---
title: "feat: process subcommand - one upload for full local-file pipeline"
type: feat
status: active
date: 2026-04-22
origin: docs/brainstorms/2026-04-22-process-subcommand-one-upload-requirements.md
---

# feat: process subcommand - one upload for full local-file pipeline

## Overview

Add a new `process --file <PATH>` CLI subcommand that uploads a local
MP4 to Gemini Files API once and threads the resulting `file_uri`
through the existing `process_mindmap`, `process_transcript`, and
`process_concepts` helpers. Eliminates the redundant second upload
we have today when a user runs `mindmap --file` followed by
`transcript --file` on the same video. Bundles observability logging
for `usage_metadata` so implicit-cache effectiveness becomes measurable
rather than speculative.

Existing `mindmap` and `transcript` subcommands are unchanged. Users
who only want a mindmap continue typing `mindmap --file`; users who
want the full pipeline on one upload type `process --file` instead.

## Problem Frame

Today, running mindmap + transcript on a local MP4 uploads the bytes
to Gemini twice (confirmed 2026-04-22 at 23:00:24 / 23:00:26, two
distinct file IDs for the same 43 MB of bytes). Wasted bandwidth,
wasted 10-25 seconds per video, and the two `file_uri` values
probably miss Gemini's implicit cache even though the underlying
bytes are identical. See origin document for the full evidence chain.

The `file_uri` is deliberately not persisted across process
invocations (48h TTL, would create zombie references in meta.json),
so the fix has to live inside a single orchestrator process. A new
`process` subcommand is the natural place.

## Requirements Trace

- R1. Upload the MP4 exactly once per `process` invocation (origin
  Goal 1, success criterion 1).
- R2. Produce `*.mindmap.md` + `*.transcript.md` artifacts; produce
  `*.concepts.json` when the channel is configured; skip concepts
  with a warning log when the channel is not configured and still
  exit 0 (origin §5, success criterion 2).
- R3. If transcript or concepts calls fail, prior artifacts remain
  on disk (origin §4, success criterion 3).
- R4. Emit a `usage_metadata` log line per `generate_content` call
  that `cmd_process` drives (mindmap, transcript, concepts-when-run).
  Log contains `prompt_token_count`, `cached_content_token_count`,
  `candidates_token_count`, `total_token_count` (origin §6, success
  criterion 4).
- R5. Re-run without `--force` after partial failure resumes from
  the partial state; `--force` regenerates everything (origin §4,
  success criterion 5).
- R6. `ruff format && ruff check --fix && pytest -m "not integration"`
  passes after every unit (specs/agent-rules.md §3, success criterion 6).
- R7. `skills/video-intel/SKILL.md` gets a natural-language entry for
  `process` in the same PR (success criterion 7, skill-parity
  guardrail in CLAUDE.md).
- R8. `CLAUDE.md` Commands section gets a usage example for `process`
  in the same PR (success criterion 8).

## Scope Boundaries

Non-goals explicitly carried from the origin document:

- Explicit context caching via `client.caches.create()`. Deferred
  until observability logs reveal whether implicit caching is already
  sufficient.
- `--url` variant of `process` for YouTube videos. `scan` already
  chains modes for YouTube and has no upload cost to eliminate.
- Parallelizing mindmap and transcript calls. Sequential maximizes
  implicit-cache hit chance per Gemini docs.
- Refactoring `scan`'s three-call pattern.
- Persisting `file_uri` across runs.
- Auto-invoking `taxonomy-build` or `index` rebuild after `process`.

### Deferred to Separate Tasks

- Trajectory decision about whether `cmd_process` remains once
  explicit caching lands. Revisit with observability data in hand.
  See origin Deferred / Open Questions #2.

## Context & Research

### Relevant Code and Patterns

Code already in place that the plan leans on:

- `process_mindmap(scripts/video_intel.py:1066)` already accepts
  `media_uri: str | None = None` (keyword). When set, used as the
  Gemini input; when unset, falls back to `video["url"]`. No
  signature change needed.
- `process_transcript(scripts/video_intel.py:1435)` already accepts
  the same `media_uri` kwarg with identical semantics. No signature
  change needed.
- `process_concepts(scripts/video_intel.py:1593)` is text-only -
  accepts `mindmap_text` as an argument (caller reads from disk)
  and calls `call_gemini_text`. Does not need `file_uri`.
- `upload_local_video(scripts/video_intel.py:1265)` is the single
  upload helper that `cmd_process` calls once.
- `resolve_local_file_identity(scripts/video_intel.py:664)` is the
  identity resolver shared with `cmd_mindmap` / `cmd_transcript`.
  `cmd_process` reuses it verbatim.
- `call_gemini(scripts/video_intel.py:1041)` and
  `call_gemini_text(scripts/video_intel.py:1573)` are the two
  `generate_content` wrappers on the `process` path. Observability
  logging attaches inside these two functions so all three calls the
  orchestrator drives (mindmap, transcript, concepts) are covered
  automatically. The `cmd_nugget` direct call at
  `scripts/video_intel.py:3356` does not route through these wrappers
  and is therefore not instrumented by this PR, matching the origin
  scope boundary.
- `update_meta(...)` helper already appends a mode name to
  `modes_completed` atomically (used by `process_mindmap` at
  `:1127` with mode `"scan"` and by `process_transcript` at
  `:1499-1503` with mode `"transcript"`). The `cmd_process` path
  gets partial-success for free by reusing these two call sites
  plus adding one more call with mode `"concepts"` from the concepts
  step.
- `get_retry_delay(scripts/gemini_common.py:96)` classifies 429 /
  5xx transient errors. File-expiry is **not** handled here. The
  `cmd_process` fallback sits in the orchestrator, above
  `process_mindmap` / `process_transcript`, so the existing retry
  layer is unchanged.

### Institutional Learnings

- `docs/plans/2026-04-17-feat-channel-scoped-local-mp4-plan.md` is
  the closest structural precedent - it added `--file` to
  `mindmap`/`transcript` with identity resolution and meta.json
  writes. Unit sizing and test-scenario shape from that plan carry
  over.
- CLAUDE.md Code Review Guardrails: skill-parity (SKILL.md in the
  same PR) and bounded-retries (one re-upload + one retry, no
  unbounded loops) apply directly.
- specs/agent-rules.md §3: TDD cycle RED -> GREEN -> REFACTOR,
  pytest + ruff gate, test naming `test_<what>_<when>_<expected>`.

### External References

- [Gemini caching docs](https://ai.google.dev/gemini-api/docs/caching)
  fetched via context7 during brainstorm. Confirms implicit caching
  is on by default for Gemini 2.5+ including `gemini-3-flash-preview`,
  with a 1024-token minimum. Our 10-min video far exceeds that.
- `google.genai.errors.APIError` is the base class used by
  `get_retry_delay`. File-expiry surface is likely a subclass with
  `code` in {400, 403, 404} and message containing `File` / `expired`
  / `not found`. Exact shape is an execution-time discovery.

## Key Technical Decisions

- **Thin orchestrator, no internal refactor.** `cmd_process`
  delegates to existing `process_mindmap` / `process_transcript` /
  `process_concepts` with `media_uri=file_uri` threaded through. The
  existing kwargs plumbing discovered during planning makes a new
  shared helper unnecessary. Simpler diff, lower risk.
- **Lazy upload.** Check whether mindmap and transcript artifacts
  already exist (both paths resolved from identity) before calling
  `upload_local_video`. If both exist and not `--force`, skip the
  upload; `cmd_process` jumps directly to the concepts step (which
  reads mindmap from disk, does not need `file_uri`). This preserves
  the upload-once invariant under the resume-after-partial-failure
  scenario and protects users from paying for an upload when nothing
  needs regenerating.
- **File-expiry fallback at the orchestrator level.** The detection
  + re-upload + retry logic lives in `cmd_process`, not in
  `process_mindmap` / `process_transcript` / `get_retry_delay`.
  Reasons: (a) keeps the failure envelope tight to `cmd_process`
  without mutating helpers used by other commands, (b) lets the
  re-upload cost the one additional `upload_local_video` call in one
  place and record it in one log line, (c) aligns with bounded-retry
  posture (one retry, no surprises for single-mode callers).
- **Observability wired inside `process_mindmap`, `process_transcript`,
  and `process_concepts`** (not inside the lower-level `call_gemini` /
  `call_gemini_text` wrappers). Reason: `call_gemini` has no `label`
  parameter, so wiring the logger there would collapse mindmap and
  transcript into a single label and defeat the per-call observability
  story. The helpers, on the other hand, know which call they are
  making (process_mindmap -> label "mindmap", process_transcript ->
  "transcript", process_concepts -> "concepts"). The helper is invoked
  with the appropriate label immediately after its `call_gemini(_text)`
  returns.

  **Scope expansion note:** because these three helpers are also used
  by `cmd_scan`, `cmd_mindmap`, `cmd_transcript`, and `cmd_concepts`,
  the log line fires on every Gemini call across the whole tool, not
  just `cmd_process`. This is a net-positive scope expansion - it gives
  the scan pipeline the same cache-hit visibility `cmd_process` gets,
  and grep-based cost reports work across the whole corpus. Release
  notes should mention the new log lines so users who pipe output to
  log files know what the new `usage <label> ...` lines are.

  The only `generate_content` call site that does **not** get
  instrumented is `cmd_nugget` at `scripts/video_intel.py:3356`, which
  makes a direct call rather than going through the helpers. That is
  consistent with the origin scope boundary and stays out of this PR.
- **Partial-success reuses the existing two-step meta pattern.**
  `process_mindmap` writes identity + appends `"scan"` to
  `modes_completed`; `process_transcript` writes the processed
  timestamp + appends `"transcript"`; `cmd_process` adds one more
  `update_meta(..., "concepts")` call after `process_concepts`
  returns successfully. No new pattern to invent.
- **Test-first for orchestration, integration-first for helpers
  already covered.** `cmd_process` is new code: full TDD with mocks
  for `upload_local_video` / `process_mindmap` / `process_transcript`
  / `process_concepts`. The helpers themselves are already covered
  by existing tests; the plan does not add redundant coverage.

## Open Questions

### Resolved During Planning

- **Internal helper shape** (origin Open Question #2): no new
  helper needed. `process_mindmap` / `process_transcript` already
  accept `media_uri` as a keyword, and `resolve_local_file_identity`
  already returns the identity tuple the orchestrator needs.
  `cmd_process` is a thin function that: resolves identity, decides
  whether to upload, uploads once, calls the three helpers in order,
  handles file-expiry at its own level. Confirmed by reading
  `scripts/video_intel.py:1066-1150`, `:1435-1505`, `:1593-1650`.
- **Observability wire points:** attach the helper inside
  `process_mindmap`, `process_transcript`, and `process_concepts`
  immediately after the underlying `call_gemini(_text)` returns.
  The helpers pass their own label ("mindmap" / "transcript" /
  "concepts"). Nugget path at `scripts/video_intel.py:3356` does
  not route through these helpers and stays untouched.

### Deferred to Implementation

- **Exact Gemini file-expiry exception shape** (origin Open
  Question #1). The plan specifies a test fixture that simulates
  the error; the implementer discovers the actual class / code /
  message pattern during GREEN by either (a) raising a stale
  `file_uri` against a live API in a scratch test, or (b) checking
  the `google-genai` SDK source under
  `~/.local/lib/python3.12/site-packages/google/genai/errors.py`
  for file-related error subclasses. Unit 3's test parametrizes
  across plausible shapes (`APIError(code=403, message="File ...
  not found")`, `APIError(code=400, ...)`, etc.) so the detection
  function can be written against the observed reality without
  rework.

## Implementation Units

- [ ] **Unit 1: Observability helper `log_usage_metadata`**

**Goal:** Ship the thermometer. Add a helper in
`scripts/gemini_common.py` that takes a Gemini response object and
a string label, logs one line with `prompt_token_count`,
`cached_content_token_count`, `candidates_token_count`,
`total_token_count`. Wire it into the three `process_*` helpers so
each call labels itself ("mindmap", "transcript", "concepts").

**Requirements:** R4

**Dependencies:** None. Independent of the orchestrator.

**Files:**

- Modify: `scripts/gemini_common.py` (add `log_usage_metadata` function)
- Modify: `scripts/video_intel.py` (three wire points:
  `process_mindmap` at `:1066`, `process_transcript` at `:1435`,
  `process_concepts` at `:1593`). The `call_gemini` and
  `call_gemini_text` wrappers themselves do **not** change - they
  already return `response.text`; the helpers keep the response
  object available until logging is done by adjusting their local
  variable pattern to `response = call_gemini(...); log_usage_metadata(response, "<label>"); ... use response.text`.

  Exception: `call_gemini` and `call_gemini_text` currently return
  `response.text` directly. To make the `response` object available
  to callers, either (a) change the wrappers to return the full
  response object and have all callers use `.text` (touches scan and
  nugget), or (b) keep returning `.text` but also invoke a per-helper
  logging lambda. Option (b) is the smaller diff: the wrappers gain
  a `on_response: Callable[[object], None] | None = None` kwarg that
  if provided is called with the response before `.text` is returned.
  Helpers pass `on_response=lambda r: log_usage_metadata(r, "mindmap")`.
  Decide at implementation time; both work.
- Test: `tests/test_usage_metadata_logger.py` (new file)

**Approach:**

- Function signature:
  `log_usage_metadata(response: object, label: str) -> None`.
- Reads `response.usage_metadata` defensively - treats missing
  fields as 0 (Gemini may omit `cached_content_token_count` on
  small prompts).
- **Type coercion:** coerce each field to `int` before formatting.
  Gemini SDK occasionally returns a list-of-submodels for newer
  models (e.g., `candidates_token_count` may be a `List[ModalityTokenCount]`
  on multimodal responses in gemini-3 preview). The helper converts
  non-int values to 0 with a debug log, never raises.
- Emits one info-level log line in the exact format the origin
  document specifies:
  `usage {label} prompt={N} cached={N} candidates={N} total={N}`.
- No return value, side effects only. On any internal exception
  (malformed response, missing attribute, weird SDK shape), catch
  broadly and log at warning level; never let observability break
  the caller.

**Execution note:** Test-first. Write the log-format assertion
before the implementation so the format contract is locked
down before call sites are touched.

**Patterns to follow:**

- Logger name convention: module-local `log = logging.getLogger(__name__)`
  already in `scripts/gemini_common.py:7`.
- Defensive attribute access: `getattr(usage, "field", 0)` pattern
  used elsewhere in the script.

**Test scenarios:**

- Happy path: mock `response.usage_metadata` with all four fields
  set; `log_usage_metadata(resp, "mindmap")` emits one info line
  matching `usage mindmap prompt=\d+ cached=\d+ candidates=\d+ total=\d+`.
- Edge case: `response.usage_metadata` missing the
  `cached_content_token_count` attribute - helper logs `cached=0`,
  does not raise.
- Edge case: `response.usage_metadata` is `None` - helper logs
  `cached=0 prompt=0 candidates=0 total=0` with a warning and does
  not raise.
- Edge case: `response.usage_metadata.candidates_token_count` is a
  list of submodels (gemini-3 multimodal shape) - helper coerces
  to `0` with a debug log, emits a well-formed log line, does not
  raise. This guards against the observability helper breaking on
  an SDK-version drift.
- Edge case: `response.usage_metadata` raises `AttributeError` on
  access (hypothetical SDK quirk) - helper catches broadly, logs
  a single warning line, does not propagate the exception to the
  caller.
- Integration: after wiring into `process_mindmap`, a unit test that
  mocks `call_gemini` to return a pair (response object, text) and
  asserts the log line is emitted at info level with label `"mindmap"`.
  Repeat for `process_transcript` (label `"transcript"`) and
  `process_concepts` (label `"concepts"`).

**Verification:**

- Running `mindmap --file small.mp4` after this unit lands emits
  one `usage mindmap ...` line at info level in stderr.
- Ruff + pytest green.

- [ ] **Unit 2: `cmd_process` orchestrator happy path**

**Goal:** Implement the new subcommand end-to-end for the
all-artifacts-missing case. Upload once, drive mindmap then
transcript then concepts with `media_uri` threaded through, write
artifacts incrementally, exit 0 on success.

**Requirements:** R1, R2, R3 (partial), R5, R6

**Dependencies:** Unit 1 (so log output is already present when
the orchestrator runs its Gemini calls).

**Files:**

- Modify: `scripts/video_intel.py` (new `cmd_process` function
  placed next to `cmd_transcript` at `:2150`, new argparse
  subparser in the main parser at the same section the other
  `--file`-accepting commands are registered)
- Test: `tests/test_cmd_process.py` (new file)

**Approach:**

- Argparse surface mirrors `mindmap --file` / `transcript --file`:
  `--file` required; `--channel`, `--video-id`, `--title`,
  `--date`, `--start`, `--end`, `--force` optional; top-level
  `--model` / `-m` override respected.
- Identity resolution via `resolve_local_file_identity(...)`,
  identical to `cmd_mindmap` and `cmd_transcript`.
- **Lazy-upload decision gated on meta.json, not filesystem existence.**
  Load the resolved identity's meta.json (if it exists). Compute
  `needs_mindmap = force or "scan" not in modes_completed or not
  mindmap_path.exists()`. Compute `needs_transcript = force or
  "transcript" not in modes_completed or not transcript_path.exists()`.
  Also mark transcript as needing regeneration when a
  `{prefix}.transcript.raw.txt` sidecar is present - that sidecar
  signals a prior parse failure that should not be treated as a
  completed mode. If either `needs_mindmap` or `needs_transcript`,
  call `upload_local_video(client, input_path)` once. Otherwise
  set `file_uri = None` and skip the upload (still run concepts
  below if `needs_concepts`).

  This gating matters: a bare file on disk is not proof of a
  successful Gemini call. A prior crash can leave a truncated
  mindmap.md without `"scan"` in `modes_completed`; trusting
  file existence alone would propagate silent corruption. The
  helpers already check `file.exists() and not force` for their
  own skip logic; gating the upload on meta.json content sits
  cleanly above that.
- Step sequence: call `process_mindmap(..., media_uri=file_uri)`;
  if `status == "done"` or `status == "skipped (exists)"`, continue;
  if `status` starts with `"error:"`, delegate to the file-expiry
  fallback (Unit 3) or exit non-zero. Call
  `process_transcript(..., media_uri=file_uri)` with same status
  handling. Then if channel is configured, read the mindmap text
  back from disk and call `process_concepts(...)`; append
  `"concepts"` to `modes_completed` via `update_meta` on success.
  If the channel is **not** configured, log a warning and skip
  concepts without failing the run.

**Exit-code contract:**

- `0` if mindmap succeeded, regardless of transcript / concepts
  outcome. This matches the origin requirement ("at least mindmap
  succeeded"). Automation callers that need to distinguish full-
  success from partial-success inspect `modes_completed` in the
  resulting meta.json rather than the exit code.
- Non-zero (specifically `1`) if mindmap failed and the file-expiry
  fallback did not recover it.
- Rationale for exit 0 on transcript/concepts failure: the shell
  one-liner equivalent (`mindmap --file X && transcript --file X`)
  would already stop on mindmap failure and continue on transcript
  failure, since each subcommand today exits 0 even when it writes
  a `transcript_status: "partial"` marker. `cmd_process` preserves
  that behavioral parity. Callers that want hard-fail on any
  partial outcome can grep `modes_completed` after the run.

**Execution note:** Test-first. Write the "upload called exactly
once" assertion and the "mindmap -> transcript -> concepts order"
assertion before writing `cmd_process`.

**Patterns to follow:**

- `cmd_mindmap` (`scripts/video_intel.py:1995`) and `cmd_transcript`
  (`:2150`) for argparse registration, identity resolution, skip-on-exists.
- `cmd_concepts` (`:2323`) for how concept extraction reads a
  mindmap from disk and calls `process_concepts`. The orchestrator
  does the same single-video version inline.

**Test scenarios:**

- Happy path: `process --file X.mp4` with all helpers mocked;
  assert `upload_local_video` called exactly once, `process_mindmap`
  called with the returned `file_uri` as `media_uri`,
  `process_transcript` called with the same `media_uri`,
  `process_concepts` called with the mindmap text read from the
  mocked disk output.
- Happy path - channel configured: the full three-artifact path
  runs; meta.json's final state has
  `modes_completed=["scan", "transcript", "concepts"]`.
- Happy path - channel not configured: same flow but
  `process_concepts` is not called; a warning log line is emitted;
  exit code is 0; meta.json has
  `modes_completed=["scan", "transcript"]`.
- Edge case: `--force` passes `force=True` through to all three
  helpers; upload happens even when artifacts exist.
- Edge case: `--start 60 --end 120` passes `start_offset=60`,
  `end_offset=120` to `process_mindmap` and `process_transcript`.
- Error path - mindmap fails: `process_mindmap` returns
  `"error: ..."`; `cmd_process` exits non-zero and does not call
  `process_transcript` or `process_concepts`.
- Error path - transcript fails after mindmap: `process_transcript`
  returns `"error: ..."`; **exit code is 0** (mindmap succeeded);
  a log line names the transcript failure; mindmap file still on
  disk; `modes_completed=["scan"]` in meta.json after the run.
- Error path - concepts fails after transcript: `process_concepts`
  raises or returns an error; exit code is 0; mindmap and
  transcript files remain on disk; `modes_completed=["scan",
  "transcript"]` in meta.json.
- Integration - file_uri threading: running the full command with
  minimal mocks (mock `client.models.generate_content` at the
  wrapper boundary, exercise real helper internals), assert that
  the `contents` argument to `generate_content` contains the exact
  `file_uri` string returned by the mocked `upload_local_video`
  and does **not** contain `video["url"]`. Catches the falsy-check
  regression where `effective_media_uri = media_uri if media_uri
  else video['url']` would collapse empty-string to the fallback.
- Integration - log-line ordering: the three `usage_metadata` log
  lines appear in order (mindmap, transcript, concepts) with
  distinct labels and reasonable token-count values. Use
  `caplog` or equivalent.

**Resume-and-idempotency scenarios (folded in from the original
separate fourth unit; these belong here as the tests that drive the
lazy-upload-gate implementation):**

- Resume: mindmap.md and meta.json (with `modes_completed=["scan"]`)
  exist on disk, no transcript file. Run `process --file X.mp4`
  without `--force`. Assert `upload_local_video` called exactly
  once (transcript needs regenerating), `process_mindmap` called
  (returns "skipped (exists)" from its own check), `process_transcript`
  called and succeeds, `process_concepts` called and succeeds. Exit 0.
- Resume - trust meta.json over file existence: mindmap.md exists
  but meta.json has `modes_completed=[]` (or meta.json is missing
  entirely). The gate forces regeneration: `upload_local_video` is
  called, `process_mindmap` is called with `force=False`, which
  skips because the .md file exists. This is a designed behavior:
  the helper's own file-existence check is the final word on
  regeneration, but the upload decision is gated on meta.json.
  Test asserts: upload called once; mindmap helper returns
  `"skipped (exists)"`; the log line about the meta/file mismatch
  is emitted at warning level.
- Resume - .transcript.raw.txt sidecar present: mindmap.md exists
  with `modes_completed=["scan"]`, transcript.md exists, but a
  `{prefix}.transcript.raw.txt` sidecar also exists (prior run's
  salvage forensics). The gate treats transcript as needing
  regeneration. Upload happens; transcript regenerates.
- All-artifacts-exist fast path: mindmap.md, transcript.md,
  concepts.json all exist and meta.json has
  `modes_completed=["scan", "transcript", "concepts"]`. Run
  without `--force`. Assert `upload_local_video` is **never**
  called (no upload cost, no wall-clock). All three helpers
  return `"skipped (exists)"`. Exit 0.
- `--force` fast-path bypass: same as above but with `--force`.
  Upload happens, all three helpers regenerate.

**Verification:**

- `python scripts/video_intel.py process --file <small.mp4>`
  produces all three artifacts plus meta.json when the channel is
  configured.
- Re-running without `--force` reports all modes skipped-exists;
  no upload happens (unit test asserts this).
- Ruff + pytest green.

- [ ] **Unit 3: File-expiry fallback (re-upload once, retry once)**

**Goal:** If any `generate_content` call inside `cmd_process`
raises the Gemini-specific file-not-found / file-expired error,
re-upload the input file once, retry the failing call once, then
fail cleanly if that also errors.

**Requirements:** R1 (protects the upload-once invariant on the
rare-but-real case), R3.

**Dependencies:** Unit 2.

**Files:**

- Modify: `scripts/video_intel.py` (add `_is_file_expiry_error`
  detector function near `cmd_process`; wrap the helper calls in
  `cmd_process` with the detection + retry logic)
- Test: `tests/test_cmd_process.py` (add scenarios for this unit)

**Approach:**

- **Contract clarification:** `process_mindmap` and
  `process_transcript` catch their internal exceptions at
  `scripts/video_intel.py:1131-1150` and `:1485-1490` / `:1547-1552`
  and return `(prefix, "error: {stringified_exception}")` rather
  than re-raising. The file-expiry detection therefore operates on
  the returned error-status **string**, not on a live exception
  object. This keeps the shared helpers unchanged and matches how
  `cmd_scan` already consumes helper statuses.
- Add `_is_file_expiry_error_status(status: str) -> bool` that
  parses the "error: ..." string and returns True when it matches
  the file-expiry signature. Initial implementation: case-insensitive
  substring match on specific phrases that are high-signal for
  file-expiry and low-signal for unrelated failures:
  - `"file ... is in the failed state"` (stale Files API handle)
  - `"file ... not found"` combined with a `files/` path fragment
  - `"resource ... files/"` combined with `"expired"` or `"not found"`

  Negative markers that explicitly disqualify (even if a substring
  coincidentally matches):
  - `"quota"`, `"rate"` (quota-exceeded / rate-limited)
  - `"safety"`, `"blocked"` (safety-filter PERMISSION_DENIED)
  - `"members only"`, `"permission_denied"` without a files/ path
    (YouTube gated content per memory
    reference_youtube_visibility.md)

  The combination is the point: a file-expiry error references
  the `files/HASH` resource by its Files API path. An unrelated
  403 typically does not. Pattern-match on the conjunction of
  `files/TOKEN` presence plus one of the positive markers.
- During GREEN, discover the exact error shape by either
  examining the `google-genai` SDK source or by provoking the
  error with a stale `file_uri` in a scratch test. Tighten the
  detector if the observed reality differs. Keep test
  parametrization covering multiple plausible shapes so future
  SDK changes surface a test failure instead of a silent miss.
- In `cmd_process`, after each call to `process_mindmap` and
  `process_transcript`, inspect the returned status. If it starts
  with `"error: "` and `_is_file_expiry_error_status(status)` is
  True, log the event, re-upload via `upload_local_video(...)`
  once, retry the same helper once with the new `file_uri`. Cap
  at one re-upload total per `cmd_process` invocation (shared
  counter across both helpers).

**Execution note:** Test-first. Write the file-expiry fixture +
detector test before modifying `cmd_process`. Parametrize over
several plausible error shapes.

**Patterns to follow:**

- `get_retry_delay` (`scripts/gemini_common.py:96`) as the model
  for "inspect exception, decide retryability" - but the new
  detector is separate; it does not modify `get_retry_delay`.
- The one-bounded-retry pattern already used by
  `process_transcript` for JSON salvage
  (`scripts/video_intel.py:1573` region).

**Test scenarios:**

- Happy path: mock `process_mindmap` to return
  `("prefix", "error: APIError: 403 File files/abc123 is in the FAILED state")`
  once, then return `("prefix", "done")` on retry; `cmd_process`
  re-uploads, retries, completes normally; `upload_local_video`
  called exactly twice; exit 0.
- Happy path: same for `process_transcript` returning the error
  string on first call (file-expiry during second Gemini call);
  mindmap succeeded on first call; upload called twice; exit 0.
- Positive detector cases: `_is_file_expiry_error_status` returns
  `True` for:
  - `"error: APIError: 403 File files/abc is in the FAILED state"`
  - `"error: APIError: 404 File files/xyz not found"`
  - `"error: APIError: Resource files/def expired"`
- Negative detector cases (critical - avoids false-positive
  re-upload on unrelated 403s): `_is_file_expiry_error_status`
  returns `False` for:
  - `"error: APIError: 403 quota exceeded for project X"` (quota)
  - `"error: APIError: 429 rate limit"` (rate limit)
  - `"error: APIError: 403 safety filter blocked output"` (safety)
  - `"error: APIError: 403 permission_denied: members only content"`
    (YouTube members-only per reference_youtube_visibility.md)
  - `"error: APIError: 500 internal server error"` (transient)
  - `"error: ValueError: malformed response"` (unrelated parse error)
- Error path: helper returns a file-expiry error, re-upload itself
  fails (e.g., `upload_local_video` raises `ConnectionError`);
  `cmd_process` does not enter a second retry loop, exits non-zero
  with a clear log line naming the cause as re-upload failure
  (not file-expiry).
- Error path: helper returns a file-expiry error on first attempt,
  re-uploads successfully, retry returns another error-status
  string (same file-expiry OR a different error). One bounded
  retry only; no third attempt. Exit non-zero if mindmap was the
  failing step; exit 0 if transcript was the failing step (per
  Unit 2's exit-code contract).
- Fallback interaction: a helper returns an error that is NOT
  file-expiry (e.g., rate-limit already exhausted by
  `get_retry_delay`). `cmd_process` does **not** treat it as
  file-expiry, does not re-upload, proceeds to exit per Unit 2's
  contract. Verifies the detector's negative cases actually
  prevent the false-positive re-upload.

**Verification:**

- Unit tests parametrize across plausible error shapes; all pass.
- `_is_file_expiry_error` behavior is explicit in test fixtures.
- Ruff + pytest green.

- [ ] **Unit 4: Skill-parity documentation**

**Goal:** Add `process` to the user-facing CLI documentation so
the skill surface stays aligned with the new command (CLAUDE.md
Code Review Guardrails: "skill-parity: same diff, not follow-up").

**Requirements:** R7, R8.

**Dependencies:** Unit 2 (command exists and behaves as documented).

**Files:**

- Modify: `skills/video-intel/SKILL.md` (natural-language routing
  entry)
- Modify: `CLAUDE.md` Commands section (usage example alongside
  the existing `mindmap` / `transcript` examples)

**Approach:**

- SKILL.md entry: a short paragraph explaining that when a user
  asks "run the full pipeline on a local MP4" or "do mindmap plus
  transcript plus concepts on a file," the skill should invoke
  `python scripts/video_intel.py process --file <PATH>`. Mirror
  the language style of the existing entries for `mindmap --file`
  and `transcript --file`.
- CLAUDE.md Commands section addition: one shell example
  demonstrating `process --file "G:/My Drive/video-intel/<channel>/X.mp4"`
  alongside the existing `mindmap` / `transcript` examples. Note
  that `process` is the preferred form when the user wants both
  mindmap and transcript on one upload.

**Execution note:** None (documentation).

**Patterns to follow:**

- Existing `mindmap --file` and `transcript --file` entries in
  SKILL.md. Keep the same voice, same level of detail.
- CLAUDE.md's existing Commands section format (one bash block,
  inline comments for flags).

**Test scenarios:**

- Test expectation: none - documentation changes do not require
  behavioral test coverage. Ruff / markdownlint run via the
  existing IDE integration catches formatting issues.

**Verification:**

- `skills/video-intel/SKILL.md` contains an entry for `process`.
- `CLAUDE.md` Commands section shows a `process --file` usage
  example.
- Ruff passes (no code changes here, but the final pytest run
  across the full PR should stay green).

## System-Wide Impact

- **Interaction graph:** `cmd_process` composes three existing
  helpers (`process_mindmap`, `process_transcript`,
  `process_concepts`). The helpers themselves are unchanged. No
  new callbacks, middleware, or event surfaces introduced.
- **Error propagation:** Errors from the helpers surface to
  `cmd_process`, which distinguishes file-expiry (re-upload +
  retry) from other failures (non-zero exit, prior artifacts
  preserved). Errors in `log_usage_metadata` itself must never
  propagate - the helper is observability and should fail silent
  to a warning log if `response.usage_metadata` is malformed.
- **State lifecycle risks:** Partial-write handling is covered by
  the existing two-step meta.json pattern. The new concern is a
  meta.json with `modes_completed=["scan"]` and no transcript
  file, which Unit 2's resume-semantics scenarios explicitly cover.
- **API surface parity:** New `process` subcommand is an addition.
  Existing `mindmap` / `transcript` / `concepts` / `scan` are not
  modified in their CLI surface or return behavior.
- **Integration coverage:** The cross-layer "upload_local_video ->
  process_mindmap (with media_uri) -> process_transcript (with same
  media_uri) -> process_concepts -> meta.json final state" flow is
  tested by Unit 2's integration scenario. Mocking all the way
  down would miss the media_uri threading, so the integration
  test stubs at the `client.models.generate_content` boundary and
  exercises the real helper internals above that line.
- **Unchanged invariants:**
  - `file_uri` still never persists to disk. The orchestrator's
    scope is one Python process.
  - `cmd_mindmap` and `cmd_transcript` CLI behaviors unchanged;
    the existing "mindmap only" and "transcript only" workflows
    keep working.
  - `scan`'s three-call pattern unchanged; this PR does not touch
    YouTube ingestion.
  - `get_retry_delay` unchanged; file-expiry detection is a new
    orchestrator-level branch, not a modification of the existing
    retry classifier.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Implicit cache misses on the transcript call and token savings are zero | Unit 1 ships the observability logging; post-merge logs will tell us whether implicit cache fires. Non-blocking: the PR still delivers upload bandwidth + wall-clock savings (Goals 1-2) regardless. |
| File-expiry exception shape doesn't match our detector | Unit 3's parametrized tests cover multiple plausible shapes; detector is inspected first, not blindly trusted. A missed shape surfaces as "file-expiry branch didn't fire when it should have" - test-visible, not silent. |
| Orchestrator introduces shared failure surface between mindmap and transcript (brainstorm Risk 2) | Partial-success semantics (Unit 2) + file-expiry fallback (Unit 3). Mindmap artifact persists even if transcript fails; transcript artifact persists even if concepts fails. |
| Meta.json incremental-write ordering divergence (brainstorm Risk 3) | Reuse the existing two-step pattern already in `process_mindmap` and `process_transcript`. Unit 2's "mindmap exists, transcript missing" scenario is the direct test for this correctness concern. |
| Documentation drift between CLI and SKILL.md (skill-parity guardrail) | Unit 4 is part of the same PR. Branch does not merge until both docs are updated. |

## Documentation / Operational Notes

- No feature flags needed. The new subcommand is additive; users
  who don't type `process` don't see it.
- No migration needed. No changes to meta.json schema beyond adding
  `"concepts"` to `modes_completed`, which is already allowed by
  the existing schema (a list of arbitrary strings).
- Release notes should mention `process --file` as a new command
  and call out the observability logs (so users know what `usage
  mindmap prompt=... cached=...` lines in their console mean).
- After merge, a manual smoke test on a 5-10 minute local MP4 with
  implicit-cache hit / miss observation captured in a follow-up
  issue or doc. Not gating this PR.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-22-process-subcommand-one-upload-requirements.md](../brainstorms/2026-04-22-process-subcommand-one-upload-requirements.md)
- Structural analogue:
  [docs/plans/2026-04-17-feat-channel-scoped-local-mp4-plan.md](2026-04-17-feat-channel-scoped-local-mp4-plan.md)
- Gemini caching docs:
  [ai.google.dev/gemini-api/docs/caching](https://ai.google.dev/gemini-api/docs/caching)
- Constraint source: [specs/agent-rules.md](../../specs/agent-rules.md) §3 (TDD), §6 (verify-don't-assume), §7 (scope)
- Project constraints: [CLAUDE.md](../../CLAUDE.md) Code Review Guardrails
- Memory: `project_model_selection.md` - current model is
  `gemini-3-flash-preview`, implicit-cache minimum is 1024 tokens
