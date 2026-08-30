# Fix plan: silent transcript coverage corruption (blind gaps, monolithic collapse, clock slip)

Date: 2026-08-29. Status: approved for implementation this session (working tree only, no commits until owner review).
Provenance: three-way design review (Opus orchestrator + Fable design pass + Codex cross-layer pass) over a 2,049-file corpus forensics sweep. Seed case: `uU5Gv2h8-9g` (aidotengineer, 51m30s), repaired 2026-08-29 by desk reconstruction at zero API cost.

## Evidence summary (verified, not inferred)

Two distinct phenomena with opposite duration profiles:

- **Monolithic collapse** (<= 3 dialogue entries for the whole video): short-video, model-quality problem. 25.3% of 5-30min videos under `gemini-3-flash-preview` vs 3.4% under `gemini-3.7-flash`, Fisher exact p = 0.0038, stratified by duration (confound refuted).
- **Blind gaps** (>= 10min contiguous dialogue hole): long-video problem. 9.8% at 60-90min, 45% at 90min+. Chunking does NOT fix it: 12/44 chunked 60min+ videos have >= 10min holes with every chunk `ok` and file status `ok`. Gap onset: median 28.6 minutes into a 50-minute chunk (n=17). Gemini degrades ~30min into a call regardless of the requested window.
- **Clock slip** (seed case): one response's timestamps jumped backward ~20min mid-call; `merge_transcript_json()` sorts by timestamp, so the real tail was sorted INTO the middle - an interleave, no text lost.

Corpus dialogue density is bimodal: 166 files < 0.1 entries/min, valley 0.1-0.5, healthy median 1.79/min.

## Code defects (all verified by direct reading)

1. **Dead monotonicity check** `scripts/video_intel.py:2389-2405`: the sort at :2390 orders by the identical key the check then tests, so `secs < last_secs` is unsatisfiable; and `is_partial` at :2421 never reads `monotonicity_warnings` despite the log line claiming `transcript_status=partial`.
2. **Span metric blind to holes** `:2900`: `_assess_chunk_coverage` uses `max(ts) - min(ts)`; a hollow chunk with entries at minute 0 and 49 scores 98% and passes. This is why the 12 blind files show `['ok','ok','ok']`.
3. **50% threshold too low** `:2903`: a chunk dying at minute 26 of 50 passes at 52%.
4. **Single-shot path unassessed** `:2872`: the `(0,0)` sentinel returns `ok` immediately; the sentinel conflates "no clipping" with "no assessment".
5. **Runt-fold widens exposure** `:1882-1886`: any tail < 20% of chunk size folds into the previous chunk, silently converting a just-chunked video back into an effectively single-shot call up to 60min (the seed's exact path: 3090s -> [(0,3000),(3000,3090)] -> folded to one 51.5min chunk).
6. **No containment** (Codex): a corrupt-labeled transcript still feeds mindmap-from-transcript (`resolve_mindmap_source` consults file existence only, :6044), then concepts, taxonomy, and the LanceDB index.

## Design decisions (MoE-adjudicated)

- **No new `transcript_status` literal.** Reuse `partial` for detected corruption; add persisted quality fields. Sweepability comes from the fields. (Codex over Fable; keeps the writer-literal union small and every existing consumer correct without a same-change rewrite.)
- **Two-tier severity.** SEVERE = monolithic (<= 3 entries or density < 0.1/min with known duration > 5min), blind gap >= 600s, backward jump >= 600s. MILD = density < 0.25/min, backward jump >= 60s, trailing gap >= 600s with otherwise-healthy body. Severe changes status to `partial`, counts as a pipeline gap (exit 3), and blocks `auto` mindmap-from-transcript. Mild is label-only. This amends issue #129 invariant (b) deliberately and narrowly: a monolithic 3-line transcript of a 60-minute video is not "degraded but real"; a 95% salvage still is, and must never false-alarm.
- **Primary trigger is max blind gap, not coverage.** Coverage (last stamp / duration) is renamed `transcript_last_dialogue_fraction` and demoted to telemetry: the 12 worst real cases all show coverage >= 0.99.
- **Monolithic metric is entries per known-duration minute**, never per covered minute (covered span ~ 0 in a true collapse).
- **Emission-order backward-jump check runs per raw chunk, BEFORE `_classify_and_offset_timestamp` and before any sort.** The classifier rewrites timestamps and the sort destroys emission-order evidence. Never compare across chunk boundaries. The dead post-sort check and its misleading log line are removed.
- **No auto-repair, no auto-retry, no captions failover trigger.** Quality statuses never start with `error`, so the #120 livestream suppression and the captions failover stay untouched. Remediation is explicit `--force` per the remediate-on-demand convention.
- **Containment rule:** `resolve_mindmap_source` with `auto` treats a severe-flagged transcript as unavailable (falls back to mindmap-from-video, the pre-#54 safe default); explicit `mindmap_source: transcript` is honored and stamps the existing partial-source provenance header. Scope: Gemini writers only (chunked + single-shot); the captions writer is exempt (whole-track by construction).
- **`is_processed()` unchanged.** Severe artifacts stay on disk and are not auto-requeued.

## Implementation (single change set, working tree)

1. **Pure assessor** `assess_transcript_artifact(transcripts, duration_seconds, window=None)` in `scripts/video_intel.py`: returns metrics (`last_dialogue_fraction`, `max_blind_gap_seconds` + position + kind leading/internal/trailing, `dialogue_entries`, `density_per_min`, `max_backward_jump_seconds`) and `severe`/`mild` flag lists. Named constants with the corpus distribution in comments: `BLIND_GAP_SEVERE_SECONDS = 600`, `MONOLITHIC_MAX_ENTRIES = 3`, `DENSITY_SEVERE_PER_MIN = 0.1`, `DENSITY_MILD_PER_MIN = 0.25`, `BACKWARD_JUMP_MILD_SECONDS = 60`, `BACKWARD_JUMP_SEVERE_SECONDS = 600`.
2. **Rewrite `_assess_chunk_coverage`** to use the assessor per chunk window (max internal gap incl. gaps to window edges, entries, density, raw-order backward jump). Verdict vocabulary stays `ok`/`thin` (writer contract unchanged); the old span ratio survives only as a logged diagnostic.
3. **Integrate into both Gemini success writers before rendering** (chunked ~:2380-2422; single-shot ~:4142-4183). Persist metrics on every Gemini transcript meta write, healthy or not: `transcript_quality_flags` (sorted), `transcript_max_blind_gap_seconds`, `transcript_blind_gap_at_seconds`, `transcript_last_dialogue_fraction`, `transcript_dialogue_entries`. Severe -> `transcript_status: partial`. Pass real duration to the single-shot assessment separately from the `(0,0)` clipping sentinel.
4. **Per-chunk emission-order check** in `_run_chunked_transcript_url` after parse, before merge. Remove the dead post-sort check (:2383-2411) and its false log claim; keep both sorts.
5. **Exit-code integration:** `missing_pipeline_artifacts` counts a transcript step with severe flags as a gap (exit 3). Mild flags keep exit 0.
6. **Containment** in `resolve_mindmap_source` per the design decision above.
7. **Runt-fold:** replace the 20% ratio with an absolute `RUNT_FOLD_MAX_SECONDS = 120` floor.
8. **`chunk_minutes` default 50 -> 30** (constant at :1751). Note: this, not the fold floor, is what fixes the seed shape (90s runt still folds under 120s; at 30min the seed becomes two real chunks).
9. **CLAUDE.md guardrails in the same change:** new entry for the quality assessor invariants; amend the #128 entry's monolithic disclaimer (the shape now has its claimant); document the #129(b) amendment; note the resolver containment rule. Repoint `test_does_not_claim_the_monolithic_early_stop_shape`.
10. **Tests** `tests/test_transcript_quality_guard.py`: seed-shape clock slip flagged severe; hollow chunk (entries at 0 and 49min) severe; monolithic single-shot severe; 95% salvage NOT flagged (false-alarm lock); 3-min outro tail not flagged; unknown duration = metrics only; `(0,0)` sentinel still assessed with known duration; runt-fold boundary cases (3001s@50 folds, 3300s@50 does not, 3090s@30 chunks in two); exit-code severe->3 mild->0 with checker deriving the writer's real path independently (the `TestCheckerAndWriterAgreeOnPaths` shape); resolver containment both branches; writer-status literals parametrized.

## Explicitly out of scope

- Bulk remediation of the ~274 affected videos (staged separately, worst class first, after the A/B; findings table goes to `_reports/` in the corpus, never the repo).
- The $5 two-arm model A/B (12 stratified videos, both models; also answers whether same-model re-roll fixes monolithic collapse). Owner decision - it spends API money.
- Default model change. Blocked on the A/B per the measurement guardrail.
- Dedupe canonical-selection ordering (a newer low-quality rerun can beat an older healthy duplicate at :7608-7615). Real but rare; follow-up issue.
- Retrieval/eval re-run: no retrieval logic is touched.

## Validation plan (after implementation)

Two independent validators plus the full suite:
1. **Executing adversarial validator**: constructs the seed shapes as fixtures and runs them through the REAL assessor and writers (no stubs), verifying flags, status, exit codes, and that healthy salvage stays clean. Stub-blind tests are the known failure mode (PR #136).
2. **Guardrail-conformance reviewer**: walks every reviewer-grep instruction in CLAUDE.md's Code Review Guardrails against the diff (confab guard seams, meta-write contracts, identity stamping, #120/#127 predicates, prune allowlists, argparse/backup inventory).
3. `pytest tests/ --ignore=tests/evals` full pass + `ruff format` + `ruff check`.

## Validation record (2026-08-29, post-implementation)

Two independent validators ran against the working-tree implementation. Full suite: 1902 passed, 1 skipped (pre-existing Neo4j), ruff clean.

**Guardrail conformance (12-point checklist): all PASS, zero violations.** Every reviewer-grep in CLAUDE.md's Code Review Guardrails executed against the diff; confab-guard seams, meta-write contracts, #120/#127 predicates, resolver contract, and inventory checks all intact. Bonus finding: the runt-fold 120s floor IMPROVES classifier consistency (old 20% ratio could stretch a merged tail to ~1.2x nominal, past `timestamp_tolerance`'s slack window; 120s stays inside it).

**Executing validation against 375 labeled real corpus files** (harness discrimination proven first on the repaired seed vs its corrupted backup):

- Monolithic: 199/199 detected (100%).
- Internal blind gaps: 19/19 severe (100%).
- Known-good sample: 0/150 false severe. The 4 "false mild" were genuine >= 600s trailing gaps the labeling recipe missed - assessor correct, labels under-specified.
- Every threshold boundary behaves exactly as documented (inclusive at floor, exclusive below).

**Accepted-risk quantification (trailing-never-severe):** 2/21 assessable known-bad blind videos (9.5%) have their gap in trailing position and get only MILD. The seed case itself, reconstructed post-hoc from disk, shows a 571s trailing gap - 29s under the severe floor - so a corpus sweep cannot use it as a positive control; only the live pipeline's raw-order backward-jump check catches that class. **Decision: keep trailing at mild.** The measured bad cases (854s/829s gaps at last-dialogue fractions ~0.81) and the largest known-good outro (1951s) are inseparable by any gap-size or fraction threshold the data supports - a proportional ceiling would miss the real cases and false-severe the outliers. Trailing cases stay sweepable via `transcript_quality_flags` + `transcript_last_dialogue_fraction`.

**Remediation-sweep requirement discovered:** 5/26 labeled blind-gap files (19%) persist NO duration anywhere (no meta `duration_seconds`, no header line; all single-shot `gemini-3-flash-preview`). The assessor is structurally blind on them by contract. Any future remediation sweep MUST bucket duration-less files as "unassessable", never "clean".

**Residual items (reviewer-identified, filed as follow-up issues):** cross-chunk timestamp misclassification remains undetectable by any backward-jump check (pre-existing, not closed by #157); dedupe canonical selection prefers a newer low-quality rerun over an older healthy duplicate (quality does not enter the ordering).
