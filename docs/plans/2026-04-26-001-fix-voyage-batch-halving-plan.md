---
title: "fix: Adaptive batch-halving for Voyage embed on token-cap errors"
type: fix
status: active
date: 2026-04-26
origin: https://github.com/dzivkovi/video-intel/issues/44
---

# fix: Adaptive batch-halving for Voyage embed on token-cap errors

## Overview

Make `build_search_index` resilient to Voyage's per-batch token cap (120,000 tokens for `voyage-4-large`) by catching the typed token-cap error inside `_embed_batch` and recursively halving the offending batch instead of failing the whole index build. The fix lives entirely inside `scripts/video_intel.py` (the `_embed_batch` helper) and is invisible to callers when no batch trips the cap.

In the same PR: restore `VOYAGE_BATCH_SIZE` to its pre-incident default (128) so most batches keep flying through at full size; only dense ones halve. Add a guardrail bullet to `CLAUDE.md` so future reviewers do not regress the bounded-recursion contract.

Skill-parity: this change does not introduce a new CLI surface or flag; behavior change is internal to `index`. SKILL.md updates are limited to a brief note that the index command no longer needs a manual `VOYAGE_BATCH_SIZE` lower-bound workaround.

## Problem Frame

Surfaced 2026-04-25:

```
voyageai.error.InvalidRequestError: Request to model 'voyage-4-large' failed.
The max allowed tokens per submitted batch is 120000.
Your batch has 128972 tokens after truncation. Please lower the number of tokens in the batch.
```

The hardcoded `VOYAGE_BATCH_SIZE = 128` at [scripts/video_intel.py:3084](../../scripts/video_intel.py#L3084) packs chunks sequentially into 128-sized groups. Adding 2-3 transcripts (Chip Huyen + Cat Wu + Kieran partial) to a 9,476-chunk corpus tipped one batch to 128,972 tokens (7.5% over). The previous build had succeeded only because chunk distribution happened to spread dense content across batches. **Structural fragility, not a regression.**

The retry path in `_embed_batch` at [scripts/video_intel.py:3194](../../scripts/video_intel.py#L3194) only catches errors whose lower-cased message contains both `"rate"` AND `"limit"` substrings. The Voyage token-cap error contains neither, so it raises immediately, killing the whole `index` invocation after the embedding call has already burned API budget on prior batches.

The current workaround is to lower `VOYAGE_BATCH_SIZE` to 64. This doubles wall time uniformly even when most batches are well under the cap, and it only postpones the failure — the next dense channel will trip 64 too.

## Requirements Trace

Source: issue #44 acceptance criteria (read via `gh issue view 44`).

- **R1.** *`_embed_batch` catches Voyage's token-cap error.* The catch keys on a stable substring of the error message (`"max allowed tokens"`) AND/OR isinstance check against `voyageai.error.InvalidRequestError` if importable, so future SDK versions that retain the message but rename the exception keep working. The catch must be more specific than the existing rate-limit catch: a `"max allowed tokens"` match takes precedence over the rate-limit branch even when both substrings appear in the same message. (origin: AC #1)

- **R2.** *Recursive halving down to a minimum batch size before raising.* Default `MIN_BATCH_SIZE = 4`. When a batch with `len > MIN_BATCH_SIZE` trips the token cap, split it `[:mid]` + `[mid:]` and prepend both halves to the pending queue (so processing continues in original chunk order). When a batch with `len <= MIN_BATCH_SIZE` trips the cap, raise — a single chunk over the cap means a single transcript line is genuinely pathological and the user needs to know. (origin: AC #2)

- **R3.** *Unit test simulating the token-cap error.* `tests/test_index.py` contains a test that injects a stub Voyage client whose `embed()` raises an `InvalidRequestError`-shaped exception once for a batch of size 128, then succeeds for the two halves of size 64. Test asserts: (a) `_embed_batch` returns 128 embeddings in the original order, (b) the stub was called 3 times (one fail + two success), (c) a log line containing `"split"` was emitted at WARNING level. (origin: AC #3)

- **R4.** *Recursion-bound test.* A second test injects a stub that always raises the token-cap error. Test asserts: (a) `_embed_batch` raises (not infinite-loops), (b) raised exception preserves the original Voyage error type or chains it via `__cause__`, (c) the stub was called at most `ceil(log2(initial_batch_size / MIN_BATCH_SIZE)) + 1` times — bounded by the halving depth, not the chunk count. (origin: issue #44 "prevents infinite recursion on a single pathological chunk")

- **R5.** *`VOYAGE_BATCH_SIZE` restored to 128.* The constant moves back to 128 in the same diff that lands the halving logic. (origin: AC #4)

- **R6.** *Log-line discipline distinguishes split-cause.* Two distinct WARNING messages:
  - `"Voyage batch too large (%d chunks, ~%d tokens reported); splitting %d + %d"` — emitted when halving for a token-cap error.
  - `"Voyage rate limited, waiting %ds (attempt %d/%d)..."` — existing rate-limit log, untouched.
  A reader scanning the log can tell at a glance whether their wall time grew because Voyage was slow or because their content was dense. (origin: AC #5)

- **R7.** *CLAUDE.md guardrail under "Code Review Guardrails".* New bullet: bounded recursion is a contract — `MIN_BATCH_SIZE` floor must not be removed; reviewers grep for `MIN_BATCH_SIZE` in any diff touching `_embed_batch`. (origin: AC #6)

- **R8.** *Skill-parity audit.* No CLI surface change, no new flag, no new subcommand. SKILL.md updates limited to removing any stale references to "lower VOYAGE_BATCH_SIZE if you hit the token cap" workarounds. Grep both `skills/video-intel/SKILL.md` and `skills/video-intel-search/SKILL.md` for `VOYAGE_BATCH_SIZE` and `batch size` substrings during plan execution. (origin: project guardrail "Skill-parity: same diff, not follow-up")

- **R9.** *Gate 1 smoke test recorded as evidence.* Re-run `python scripts/video_intel.py index --force` against the real corpus that previously failed. Capture stdout/stderr to a file under `docs/plans/gate1-evidence/` and reference it from the PR body. The observable signal is the new log line `"Voyage batch too large ... splitting"` appearing at least once for the dense channel that originally failed, AND the index build completes (non-zero chunk count returned). Tests passing alone is not sufficient per project chain rule.

## Scope Boundaries

- **No config-tunable batch size in this PR.** Issue #44 task C (expose `voyage_batch_size` and `voyage_min_batch_size` in `config.yaml`) is **deferred**. Adding a config knob without empirical evidence that users on different Voyage tiers actually need it is premature and increases carrying cost. Defer until a second user reports a different cap or a different ratio.

- **No raise of `VOYAGE_BATCH_SIZE` above 128.** Issue #44 hints "or higher" — `voyage-4-large` accepts 128-1024 chunks per call. We restore to the pre-incident default (128) only. Going higher invites more frequent halving without measured throughput gain in this corpus's chunk-size distribution.

- **No exponential-backoff change.** The existing rate-limit retry path (`25 * 2**attempt + jitter`, max 5 attempts) is untouched. Halving is orthogonal to backoff: a token-cap error means "this batch is too big," not "too soon." It does not get backoff or attempt-counting.

- **No changes to `chunk_transcript`** chunking heuristics. Per-chunk size is governed by `chunk_size=5` entries; modifying that to reduce the chance of triggering the cap is a different fix at a different layer.

- **No changes to `translate_video.py`.** Operationally separate.

- **No changes to the LanceDB write path** (`probe_atomic_writes`, `db.create_table`). Token-cap recovery happens upstream of the write.

- **No retroactive index rebuild as part of this PR.** The user runs `index --force` themselves post-merge under their own corpus state. The Gate 1 smoke test runs against an already-failing corpus, but the *production* rebuild is user-initiated.

### Deferred to Separate Tasks

- **Issue #44 task C (config.yaml knob)** — deferred unless a second concrete trigger emerges.
- **Token-aware pre-batching** (count tokens before sending to Voyage so we never see the error) — adds local tokenizer dep and pre-flight cost. Not justified until halving proves insufficient.
- **Adaptive batch-size memory across runs** (remember which channels need smaller batches) — speculative complexity. Stateless halving has no carrying cost.
- **Per-Voyage-tier configuration profiles** — Voyage's free vs paid tiers differ in RPM, not token cap; out of scope here.

## Context & Research

### Relevant Code and Patterns

- **`scripts/video_intel.py:3194-3223`** — `_embed_batch` body. The retry loop uses an attempt counter and a single `for batch_num, i in enumerate(range(...))` over the `texts` list. Halving requires changing this from a sequential `for` over a fixed slice list to a `while pending:` over a queue (so split halves can be re-queued). The `total_batches` denominator in the log line will need to grow as splits happen — issue #44's draft uses `done / ~total` notation; we adopt that.

- **`scripts/video_intel.py:3084`** — `VOYAGE_BATCH_SIZE = 128`. Currently 128 in main; was lowered to 64 in the workaround commit (per issue #44 narrative; verify on disk before changing). Restoration is a one-line diff.

- **Existing test patterns:** `tests/test_skip_shorts.py` and `tests/test_video_id_dedup.py` show how this repo stubs network-shaped helpers — local fakes, no `unittest.mock`. The Voyage client is small (one method, `embed(texts, model, input_type)` returns object with `.embeddings`), so a hand-rolled stub class is the right shape.

- **Test file naming:** issue says `tests/test_index.py`. No such file exists yet. Create it.

- **Existing log-line conventions:** `log.info("[%d/%d] Embedded %d chunks", batch_num + 1, total_batches, len(batch))` and `log.warning("Voyage %s, waiting %ds (attempt %d/%d)...", reason, wait, attempt + 1, max_retries)`. The new split-warning matches the second shape.

- **CLAUDE.md "Code Review Guardrails" precedent:** the existing `probe_atomic_writes` and "Bounded retries only" bullets are the closest existing siblings — both name a contract + tell reviewers what grep to run.

### Institutional Learnings

- **Probe-before-pay rule** ([ADR-0016](../../docs/adr/ADR-0016-vector-db-path-config.md)) — already enforced. The token-cap error fires *after* the probe and *during* the embedding call, so wasted-spend mitigation here is "fail mid-batch with prior batches' embeddings discarded" rather than "fail before any spend." Halving accepts that prior-batch spend is sunk; the win is finishing the run instead of needing a full re-attempt.

- **Bounded retries rule (CLAUDE.md "Bounded retries only")** — the transcript path's "one bounded retry if salvage fails" precedent. We follow it: halving has a hard floor (MIN_BATCH_SIZE) and a single raise at the floor.

- **Issue #42 sibling** — different layer (Gemini transcript output cap, not Voyage token cap). Not coupled, but the same structural pattern applies. Out of scope here.

## Test Plan

`tests/test_index.py` (new file) covers:

1. **Happy path.** Stub Voyage client returns `[[0.0]*1024 for _ in batch]` for any input. Call `_embed_batch` with `texts = ["hello"] * 128`. Assert: 128 embeddings returned, stub called once, no WARNING logs.

2. **Single token-cap error → split.** Stub raises `InvalidRequestError("The max allowed tokens per submitted batch is 120000. Your batch has 128972 tokens.")` on the first call (batch size 128), succeeds on the two halves (size 64 each). Assert: 128 embeddings returned, stub called 3 times, exactly one WARNING log containing `"splitting"`.

3. **Recursive halving.** Stub raises token-cap on size-128 and size-64, succeeds at size-32. Assert: 128 embeddings returned, stub called 1 (fail) + 2 (fail at 64 each) + 4 (success at 32) = 7 times, two WARNING logs containing `"splitting"`.

4. **Pathological single chunk → raise.** Stub raises token-cap on every call regardless of size. Assert: `_embed_batch` raises (preserves original error type or chains via `__cause__`), call count is bounded by `ceil(log2(128 / MIN_BATCH_SIZE))` + 1.

5. **Token-cap takes precedence over rate-limit substring overlap.** Construct an error message containing both `"rate limit"` AND `"max allowed tokens"`. Assert: halving path runs, NOT exponential backoff.

6. **Mixed errors.** First call: token-cap (split). Second call (one half): rate-limit (backoff + retry, success). Third call (other half): success. Assert: final embeddings count correct, log contains both `"splitting"` and `"rate limited"` lines.

The eval harness (`tests/evals/`) is not affected. Hybrid-search 1/25 baseline is orthogonal.

## Acceptance Smoke Test (Gate 1)

Pre-condition: a corpus state where the legacy `VOYAGE_BATCH_SIZE = 64` workaround is still in place OR a corpus where 128 will trip the cap.

Steps (recorded in PR description and in `docs/plans/gate1-evidence/issue-44-smoke.txt`):

```bash
# In the worktree, with VOYAGE_API_KEY set:
python scripts/video_intel.py index --force 2>&1 | tee docs/plans/gate1-evidence/issue-44-smoke.txt
```

Pass criteria:
- (a) Exit code 0.
- (b) Stdout/log contains at least one `Voyage batch too large ... splitting` line OR a clean run with no halving (which means the corpus does not currently trip the cap; in that case re-run with `VOYAGE_BATCH_SIZE` temporarily set to a value known to trigger the cap, capture both runs, and explain in the PR body).
- (c) Final log line `Indexed N chunks` with `N >= 9000` (the corpus size from the failure report).
- (d) Subsequent `python scripts/video_intel.py search "test query" --vector` returns hits (proves the index is queryable).

If (b) or (c) fails, escalate per rule #8 — append "## Open questions" section to this requirements doc and stop before merge.

## Skill-Parity Audit

- `skills/video-intel/SKILL.md` — grep for `VOYAGE_BATCH_SIZE`, `batch size`, `token cap`. If any reference is stale (e.g., "lower VOYAGE_BATCH_SIZE to 64 if you hit the cap"), remove it.
- `skills/video-intel-search/SKILL.md` — same grep. Search skill is read-only and unlikely to mention `index` mechanics; expect zero hits.
- `.claude-plugin/plugin.json` — no version bump required (this is a bug fix, not a CLI surface change). Patch-level bump (`1.11.x` → `1.11.x+1`) is optional under the multi-skill plugin contract; we apply it for changelog hygiene if no other unreleased changes exist.

## Implementation Plan

### Step 1 — RED tests

Create `tests/test_index.py` with the six tests in §Test Plan above. Use a hand-rolled `FakeVoyageClient` class that records call counts, accepts a `behaviors` list, and either returns a fake embeddings result (`SimpleNamespace(embeddings=[[0.0]*4]*len(batch))`) or raises a constructed exception per call. Use `caplog` for the WARNING-level log assertions. Run the suite — all six tests should fail with the current `_embed_batch` implementation.

### Step 2 — GREEN implementation

Refactor `_embed_batch` from a `for batch_num, i in enumerate(range(0, len(texts), VOYAGE_BATCH_SIZE)):` slice loop into a `while pending:` queue. Add module-level `MIN_BATCH_SIZE = 4`. Inside the except block, branch on:

1. `is_token_cap = "max allowed tokens" in error_str.lower()` AND `len(batch) > MIN_BATCH_SIZE` → split + re-queue + log WARN with both halves' sizes.
2. `is_token_cap and len(batch) <= MIN_BATCH_SIZE` → re-raise (chain via `from e` if a custom wrapper, else just `raise`).
3. existing `(is_rate_limit or is_connection)` → existing backoff path.
4. else → re-raise.

Token-cap check **precedes** rate-limit check (R1 precedence). Track `done` and `total` separately so the `[done/~total]` log progresses sensibly when splits add work. Iterate until all six tests pass.

### Step 3 — Restore default

Single-line edit: `VOYAGE_BATCH_SIZE = 128` (verify on disk first; may already be 128 in main since the workaround commit was per-developer).

### Step 4 — Skill-parity sweep

Grep both `skills/video-intel/SKILL.md` and `skills/video-intel-search/SKILL.md` for `VOYAGE_BATCH_SIZE`, `batch size`, `token cap`. Remove or update any stale references. Expect zero hits — those skills do not document Voyage internals — but the audit is mandatory.

### Step 5 — CLAUDE.md guardrail

Add one bullet to "Code Review Guardrails" section, modeled on the existing "Bounded retries only" bullet. Lock the `MIN_BATCH_SIZE` floor as a contract; tell reviewers what to grep.

### Step 6 — Ruff + tests + Gate 1

```bash
ruff format .
ruff check . --fix
pytest -m "not integration" -q
```

Then Gate 1 smoke (see §Acceptance Smoke Test). Capture stdout/stderr to `docs/plans/gate1-evidence/issue-44-smoke.txt`.

### Step 7 — `/ce-code-review`

Run on the diff. Address every P1. P2/P3 noted in PR body but not blocking.

### Step 8 — Commit + push + PR

Conventional commit shape. PR body uses `/blob/main/` absolute URLs (per memory `feedback_pr_description_absolute_urls.md`). Reference issue #44 with `Closes #44`. Attach Gate 1 smoke output.

**STOP for Gate 2.** Do not click merge until user approves after seeing diff + smoke output.

### Step 9 — Memory update

Capture two durable items:

- New CLAUDE.md guardrail (bounded recursion + MIN_BATCH_SIZE floor).
- The empirical token-cap value (120,000 for `voyage-4-large`) and the size-vs-rate-limit log-line discipline.

## Open Questions

(Reserved for use during execution if Gate 1 fails or an architectural decision surfaces. Empty at brainstorm time.)
