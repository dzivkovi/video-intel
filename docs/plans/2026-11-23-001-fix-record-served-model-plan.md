---
title: Record the Gemini model that served each call - Plan
type: fix
date: 2026-11-23
issue: 295
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Record the Gemini model that served each call - Plan

## Goal Capsule

- **Objective:** an operator reading a video's `meta.json` (or a model scorecard) can tell which Gemini model actually produced each artifact, even when Google silently serves a different model than the one requested.
- **Means:** read `model_version` off the response inside the existing `on_response` callbacks and persist it per mode in `meta.json`, beside the requested `model` where one is written (KTD1, KTD2).
- **Authority:** the issue #295 scope and the binding decisions recorded as R1-R9 win over this plan; the KTDs win on mechanism; units override neither.
- **Stop conditions:** stop and report if the SDK attribute cannot be read without a new call, or if any change would alter the `prompt == 0` guards, add a status literal, or change `translate_video.py` behavior.
- **Execution profile:** TDD, real-caller tests with only the Gemini client stubbed; no paid or network calls.

---

## Product Contract

### Summary

The transcript, mindmap and concepts writers record the served model in a new per-mode meta field when the response carries one. A single process-wide WARNING names each distinct requested/served mismatch once. The model A/B harness records the served model per scorecard cell. Nothing is recorded when the attribute is unreadable, and no call ever fails because of it.

### Problem Frame

Since early October Google serves `gemini-3.8-flash` for every request naming `gemini-3.7-flash`. Every artifact records the requested model, so `meta.json` misstates provenance and a scorecard cannot show that two "different" models were the same one.

### Requirements

**Recording**

- R1. Persist `transcript_model_served`, `mindmap_model_served` and `concepts_model_served` in `meta.json`, each written only by that mode's artifact writers (success, including salvage and partial writes) and only when a non-empty string was captured.
- R2. Never overwrite or rename the requested-model field (`model`).
- R3. A chunked transcript records the single value when every chunk that returned agreed, else the distinct values sorted and joined with `,` (always a string).
- R4. `transcript_model_served` is a transcript-artifact field: a captions transcript retires a predecessor's value through `retired_transcript_fields`.

**Reading and warning**

- R5. One reader in `scripts/gemini_common.py` returns the stripped served name with a leading `models/` removed, or `None` for an absent attribute, a non-string or an empty string; it never raises and logs at DEBUG when unreadable.
- R6. Log one WARNING per process per distinct (requested, served) pair when they differ, naming both and the `<mode>_model_served` field.

**Harness and boundaries**

- R7. `scripts/model_eval.py` records `model_served` (or null) per scorecard cell through the same reader, and the markdown card shows it when it differs from the requested model.
- R8. `translate_video.py` behaves byte-identically; the `prompt == 0` guards, status literals and `concepts.json` shape are unchanged (concepts.json records no requested model today, so it gets no served one).
- R9. `docs/meta-json-schema.md` documents the three fields.

### Scope Boundaries

- Out of scope: choosing the default model, any backfill of existing metas, recording the served model on error paths, and the `nugget` brief (it records no requested model today, the same reasoning as `concepts.json` in R8).

---

## Planning Contract

### Where the requested model lives today

- **Transcript:** `meta.json` `model` is written by the chunked writer (`_run_chunked_transcript_url`) and by the local-file identity blocks in `cmd_transcript --file` / `cmd_process --file`. The single-shot success and salvage writers and the captions writer do not write `model`.
- **Mindmap:** `meta.json` `model`, written by `process_mindmap` on both success and error paths.
- **Concepts:** nowhere. Neither `meta.json` nor `concepts.json` records the requested concepts model.
- `model` is one shared field, so the last mindmap or chunked-transcript writer wins. The new per-mode served fields do not share that defect.

### Key Technical Decisions

- KTD1. **Reader and comparison live in `gemini_common.py`.** `served_model_from_response(response)` implements R5. A sibling `served_model_differs(requested, served)` owns the comparison rule (KTD6) so the video_intel warning and the model_eval card cannot disagree [core: one-definition]. `translate_video.py` does not import `log_usage_metadata` and will not import these, so it is untouched.
- KTD2. **The served model rides the existing per-call capture dict.** A helper in `video_intel.py` (`_capture_served_model(response, capture, *, requested, mode)`) puts the reader's value under one key (`served_model`) in the same dict the `on_response` callback already fills, removes the key when unreadable, and fires the warning. `log_usage_metadata` is not changed, so the counts dict and its `None`-on-missing-usage contract stay as they are and the served model survives a response whose `usage_metadata` is missing. The three callbacks that are bare lambdas today (mindmap-from-transcript and the two concepts call sites) become small closures writing a per-call dict; for concepts that is one dict shared by the cached and inline call sites and cleared before each call (U4). In every callback that clears and refills its dict on a counts update (`process_transcript` `_on_resp`, the chunked `_on_chunk_resp`, the mindmap-video `_on_resp`), the helper runs after the counts update so the refill cannot wipe the key. The dicts' annotations widen to admit the string value.
- KTD3. **Warning at capture time, memoized per pair.** A module-level set guarded by a lock (scan runs writers under `max_parallel` threads) records warned (requested, served) pairs. The message names both models and says the served model is recorded in `<mode>_model_served` when the artifact is written (error paths record nothing). Tests save, clear and restore the memo around each test, following `tests/conftest.py::no_premiere_probe`; production never resets it.
- KTD4. **Chunked capture stays per chunk.** Each chunk's existing `usage_capture` dict receives its own served value. After the timeout handler, a chunk whose call returned contributes its served value to a run-level list. The writer stores the single value or the sorted distinct values joined by `,`.
- KTD5. **Each success writer owns its served field.** The transcript field joins `TRANSCRIPT_ARTIFACT_FIELDS`, so every transcript writer that does not set it retires it automatically. The mindmap and concepts success writers pass `drop_fields=(<their field>,)` when nothing was captured, so a `--force` re-run with an unreadable attribute cannot leave the previous run's value describing a new artifact. Dedupe provenance: `_mode_provenance_fields` already sweeps every `transcript_*` key, `_MINDMAP_PROVENANCE_FIELDS` gains `mindmap_model_served`, and concepts mode has no provenance list (it copies nothing today), so it is left alone.
- KTD6. **Two comparisons: tolerant for the WARNING, strict for the scorecard.** `served_model_differs` treats an equal name or a hyphenated version suffix on the requested name (`gemini-2.0-flash` reported as `gemini-2.0-flash-001`) as the same model, because provider documentation shows version-suffixed reported names for ordinary requests; everything else (including an alias resolving to its backing model) warns, in neutral wording that names both causes. `reported_name_differs` flags any difference, because a scorecard is where the dated version matters. Both strip `models/` first, and the recorded field always keeps the reported name. Still unverified: whether a server-side redirect of a retired model is visible in `model_version` at all.
- KTD7. **model_eval stores the value in its `usage` capture dict.** The same dict is cached to `<tag>.usage.json`, so a warm re-render keeps the value; legacy cache files read as `null`. `score()` emits `model_served` from that dict. The card shows `requested (served X)` in the model cell when `served_model_differs` is true, so the table keeps its column count.

### Assumptions

- "Every chunk that returned" (R3) means every chunk whose Gemini call returned a response, including one later discarded by the confab guard or a parse failure; a timed-out chunk contributes nothing.
- Error paths record nothing (R1 says success writers only); a failed call's served model reaches only the warning.

---

## Implementation Units

### U1. Reader and comparison helpers

- **Goal:** R5 and the comparison half of R6/R7.
- **Requirements:** R5, R8; KTD1, KTD6.
- **Dependencies:** none.
- **Files:** `scripts/gemini_common.py`, `tests/test_served_model.py` (new).
- **Approach:** add `served_model_from_response` and `served_model_differs` beside `log_usage_metadata`. Guard the attribute read with a broad `except` the way `log_usage_metadata` guards its own reads.
- **Test scenarios:**
  - `"gemini-3.8-flash"` returns itself; `" models/gemini-3.8-flash "` returns `gemini-3.8-flash`.
  - Absent attribute, `None`, `""`, `"   "`, `"models/"`, an int, a `MagicMock` attribute, and a property raising `RuntimeError` all return `None` without raising, with a DEBUG record.
  - `served_model_differs` is false for equal names and for `models/`-prefixed equal names, true for `gemini-3.7-flash` vs `gemini-3.8-flash`, false when served is `None`.
  - `translate_video` source does not reference the new names (parity with the issue #231 opt-in test).
- **Verification:** unit tests green.

### U2. Capture helper, warning memo, and the transcript writers

- **Goal:** R1 (transcript), R2, R3, R4, R6.
- **Requirements:** R1-R4, R6, R8; KTD2-KTD5.
- **Dependencies:** U1.
- **Files:** `scripts/video_intel.py`, `tests/test_served_model.py`.
- **Approach:**
  1. Add the capture-key constant, the memo set plus lock, and `_capture_served_model`.
  2. Call it from `process_transcript`'s `_on_resp` and from the chunked `_on_chunk_resp`, after the counts update in both (KTD2).
  3. Single-shot success and salvage writers add `transcript_model_served` when captured.
  4. The chunked loop appends each returned chunk's value; the writer stores the joined value when the list is non-empty.
  5. Add `transcript_model_served` to `TRANSCRIPT_ARTIFACT_FIELDS`.
- **Patterns to follow:** `finish_capture` in `process_transcript` (second fact captured in the same callback); the per-chunk `usage_capture` default-argument binding.
- **Test scenarios:** drive the real writers with a fake client (`client.models.generate_content`) and the real `google.genai.types`; an autouse fixture saves, clears and restores the warning memo (KTD3).
  - Single-shot: served differs from requested; meta carries `transcript_model_served`, `model` is absent or unchanged, the WARNING fires once across two calls with the same pair.
  - Chunked, two chunks agreeing: single value; `model` still the requested name.
  - Chunked, chunks disagreeing: `"a,b"` sorted.
  - Response without the attribute, and with a non-string: no field, status unaffected, nothing raised.
  - Captions transcript after a Gemini transcript that recorded the field: field retired.
  - A `prompt == 0` response still trips the guard with a served model present (guards unchanged).
- **Verification:** new tests green; `TestFieldInventoryCannotDrift`, `test_usage_shape_guard_parity.py` green.

### U3. Mindmap writer

- **Goal:** R1 (mindmap).
- **Requirements:** R1, R2, R6; KTD2, KTD5.
- **Dependencies:** U2.
- **Files:** `scripts/video_intel.py`, `tests/test_served_model.py`.
- **Approach:** capture in the video-source `_on_resp` and in a new closure replacing the transcript-source lambda; the success writer adds `mindmap_model_served` or drops it; add the field to `_MINDMAP_PROVENANCE_FIELDS`.
- **Test scenarios:**
  - Mindmap from video and from transcript (text call): field persisted, `model` equals the requested name, warning once.
  - Unreadable attribute on a `--force` re-run over a meta holding an old value: field removed.
- **Verification:** new tests green.

### U4. Concepts writer

- **Goal:** R1 (concepts).
- **Requirements:** R1, R8; KTD2, KTD5.
- **Dependencies:** U2.
- **Files:** `scripts/video_intel.py`, `tests/test_served_model.py`.
- **Approach:** one per-call capture dict shared by the cached and inline call sites of `process_concepts` (cleared before each call); the success writer adds `concepts_model_served` or drops it. `concepts.json` unchanged.
- **Test scenarios:**
  - Real `process_concepts` with a fake client: field persisted, warning once, `concepts.json` keys unchanged.
  - Unreadable attribute: no field, status `ok`.
- **Verification:** new tests green.

### U5. model_eval scorecard

- **Goal:** R7.
- **Requirements:** R7; KTD1, KTD7.
- **Dependencies:** U1.
- **Files:** `scripts/model_eval.py`, `tests/test_served_model.py`.
- **Approach:** the `on_response` lambda also stores the reader's value in the `usage` dict; `score()` emits `model_served`; `render()` decorates the model cell when it differs.
- **Test scenarios:**
  - `run_one` with a fake client and a scratch dir: the row carries `model_served`, and the cached `usage.json` holds it.
  - Legacy cached usage without the key: `model_served` is `None`.
  - `render` shows `(served X)` only when it differs.
- **Verification:** new tests and `tests/test_model_eval_window_fidelity.py` green.

### U6. Documentation and guardrails

- **Goal:** R9 and the review guardrail.
- **Requirements:** R9.
- **Dependencies:** U2-U5.
- **Files:** `docs/meta-json-schema.md`, `.claude/rules/transcript.md`, `.claude/rules/evals.md`, `docs/troubleshooting.md` (only if a natural row home exists).
- **Approach:** three schema rows next to `model`; one short transcript.md entry (one reader, requested field never overwritten, per-chunk capture and join, inventory membership, the KTD6 rule with its falsifier, unreadable = nothing recorded) and the new test file in its `paths:`; one evals.md line.
- **Test expectation:** `tests/test_guardrail_inventory.py` and `tests/test_docs_currency.py` stay green.

---

## Verification Contract

| Gate | Command |
|---|---|
| Format changed files | `ruff format <changed files>` |
| Format check | `ruff format --check .` |
| Lint | `ruff check scripts tests` |
| Suite | `python -m pytest tests/ -q --ignore=tests/evals -p no:cacheprovider` |

Falsify each writer's persistence separately (remove the field from one writer and only that writer's test fails) and the once-per-process memo.

**Operator-run premise check (before merge, not run by the implementer: it is a paid network call).** One minimal `call_gemini_text` request naming `gemini-3.7-flash` and one naming a model Google does not redirect, logging `response.model_version` for each. Record both values in the PR. If the redirected call returns the requested name, the field cannot show the redirect and the Objective is not met; if the un-redirected call returns a suffixed name, widen KTD6.

## Definition of Done

- R1-R9 each proven by a named test in `tests/test_served_model.py` or an existing inventory test.
- All gates pass; no paid call made; `translate_video.py` unchanged in the diff.
- No experimental or abandoned code left in the diff.
