## Issue #158 debrief

- Issue: https://github.com/dzivkovi/video-intel/issues/158
- Branch: `fix/158-chunk-window-mismatch-detector`
- PR: https://github.com/dzivkovi/video-intel/pull/163 (labeled `priority/p3`, matching the `P3:` title prefix; not merged - awaiting review as instructed)

### What shipped

`merge_chunked_transcripts` gained an optional `chunk_bounds` parameter (a positionally-matched list of each chunk's ACTUAL `(start, end)` window). When supplied, every classified dialogue stamp is checked, immediately after `_classify_and_offset_timestamp` decides its placement, against `[chunk_start - slack, chunk_actual_end + slack]` (`slack = timestamp_tolerance(chunk_duration_seconds)`, reused from the same nominal duration the classifier already uses for tolerance). `_classify_chunk_window_violations` turns the raw per-chunk counts into `chunk_window_mismatch_severe` (out-of-window fraction > 0.5 with >= 4 classified dialogue entries) or `chunk_window_mismatch_mild` (any lesser violation). The severe flag joins `_SEVERE_QUALITY_FLAGS`, so `transcript_status: partial`, `EXIT_PARTIAL`, and `resolve_mindmap_source`'s containment check all pick it up through the existing #157 machinery with no new plumbing. A raw total, `transcript_chunk_window_violations`, is persisted alongside the other quality metrics. `_run_chunked_transcript_url` is the one production caller that supplies `chunk_bounds`, built in lockstep with `chunk_results` from the ACTUAL per-chunk `(start_secs, end_secs)` it already receives (never the nominal duration).

The detector is label-only: it never drops, reorders, or reclassifies a dialogue entry, and never touches `screen_content`. `chunk_bounds` is opt-in - every pre-#158 caller, including the existing `tests/test_chunked_transcript.py` suite, gets the byte-identical prior return dict (no `"_chunk_window_violations"` key at all when omitted).

### Gate 1 smoke - pre-fix vs post-fix

The four forensic sidecars in the read-only corpus were all found empty:

```
G:/My Drive/video-intel/saminyasar/2026-07-10-hermes-agent-full-course-3-hours-build-sell-2026.transcript.raw.chunk1-0-3000.txt   (0 bytes)
G:/My Drive/video-intel/saminyasar/2026-07-10-hermes-agent-full-course-3-hours-build-sell-2026.transcript.raw.chunk2-3000-6000.txt (0 bytes)
G:/My Drive/video-intel/ycombinator/2026-08-07-how-to-design-in-the-agent-era.transcript.raw.chunk1-0-720.txt                       (0 bytes)
G:/My Drive/video-intel/databricks/2026-06-18-data-ai-summit-keynote-2026-day-2.transcript.raw.chunk1-0-3000.txt                    (0 bytes)
```

All four are empty timeout/confabulation responses (`raw=""`), not malformed-but-parseable JSON, so none of them can feed the merge+detector. Per the issue's fallback instruction, `docs/plans/gate1-evidence/issue-158-chunk-window-mismatch-smoke.py` instead takes REAL dialogue entries from a real, healthy 4-chunk transcript already in the corpus (`saminyasar/2026-07-10-hermes-agent-full-course-3-hours-build-sell-2026`, chunk_minutes=50 / 3000s nominal chunks) and mechanically double-offsets one chunk's entries to construct the failure shape. **The input is real content; the corruption is synthetic** - this is the explicit reason the PR carries `Refs #158`, not `Closes #158`.

Construction: chunk 2's real, correctly-classified absolute positions (3000-3484s, i.e. 50:00-58:04, taken verbatim from the rendered `.transcript.md`) are shifted by `+2 * chunk_duration_seconds` (6000s) - the literal "double offset" a misclassification-then-remisclassification would produce on an already-absolute stamp. The corrupted values (9000-9484s) sit inside chunk 4's real window, past even chunk 3's.

Verbatim output (also in the PR body):

```
==============================================================================
PRE-FIX equivalent: merge_chunked_transcripts() with no chunk_bounds
(this is byte-identical to the code path before issue #158)
==============================================================================
Returned keys: ['screen_content', 'speakers', 'transcripts']
'_chunk_window_violations' present: False
Chunk 2's classified (final, persisted) timestamps: ['2:30:00', '2:30:28', '2:31:10', '2:31:39', '2:32:45', '2:33:37', '2:35:00', '2:36:01', '2:36:54', '2:38:04']
-> These all landed inside chunk 4's real window (2:30:00-3:02:48) instead of chunk 2's real window (50:00-1:40:00) - silently wrong, and NOTHING in the pre-fix return value flags it.

==============================================================================
POST-FIX: merge_chunked_transcripts() with chunk_bounds supplied
==============================================================================
'_chunk_window_violations': [{'chunk_index': 1, 'classified_dialogue': 7, 'out_of_window': 0}, {'chunk_index': 2, 'classified_dialogue': 10, 'out_of_window': 10}]
_classify_chunk_window_violations(): {'severe': ['chunk_window_mismatch_severe'], 'mild': [], 'total_violations': 10}
transcript_quality_flags_are_severe(result['severe']): True

==============================================================================
VERDICT
==============================================================================
Chunk 2: 10/10 classified dialogue entries flagged out-of-window; SEVERE.
Pre-fix: silent (no signal). Post-fix: reported. Gate 1 PASSED.
```

### Premise-dependent claims and their falsifiers

- **Claim:** "the four forensic sidecars in the corpus don't parse, so a real-failure Gate 1 isn't available." Falsifier: any `*.transcript.raw.chunk*.txt` under the corpus with non-zero size that parses as JSON (or as text `_normalize_task_wrapper`/`isolate_json`/`salvage_transcript_sections` can recover) would let a REAL captured failure drive Gate 1 instead, and the PR should then say `Closes #158`. I ran `rg --files "G:/My Drive/video-intel" -g "*.raw.chunk*.txt"` and checked all four with `wc -c` (all 0 bytes) - a future scan could produce a new, non-empty sidecar.
- **Claim:** "chunk 2's window with these parameters is `[540, 1260]`/`[2700, 6300]`-shaped (chunk_start ± slack, chunk_actual_end + slack), so the classifier's own absolute-band upper bound structurally coincides with the detector's `window_hi` whenever a chunk's actual bounds match its nominal bounds." This is a real structural property of the current code (`_classify_and_offset_timestamp`'s absolute branch upper bound is `chunk_start_secs + chunk_duration_secs + tolerance`, identical to the detector's `window_hi` in the no-runt-fold case), verified empirically in the smoke script and in `tests/test_chunk_window_mismatch.py::test_single_stray_stamp_inside_slack_does_not_flag`. Falsifier: a future change to `_classify_and_offset_timestamp`'s branch boundaries (e.g. widening or narrowing the absolute band independently of `timestamp_tolerance`) would decouple the two and change which corruption magnitudes the detector can and cannot catch - worth re-verifying with the smoke script if that helper's branch logic changes.
- **Claim:** "a majority-shifted, non-runt-folded chunk will often ALSO trip the pre-existing per-chunk `blind_gap_severe` check (from issue #157's `_assess_chunk_coverage`), because the relativized position of an out-of-band entry is `classified_absolute - start_secs`, which for a majority-out-of-band chunk pushes the relativized `sorted_seconds[0]` (and hence the leading-gap metric) past `BLIND_GAP_SEVERE_SECONDS` too." I verified this directly: the writer-integration severe test in `tests/test_chunk_window_mismatch.py` and the Gate 1 smoke script both log a pre-existing `blind_gap_severe` warning alongside the new `chunk_window_mismatch_severe` flag. This means the new detector's SEVERE case is not the only demonstration that "invisible to every existing check" is possible - the MILD case (a single stray stamp) is the cleaner illustration, since it produces no other flag at all (verified in `test_single_stamp_beyond_slack_flags_mild` and in a by-hand check that its relativized "trailing" gap is negative and thus ignored by `assess_transcript_artifact`). This does not weaken the fix's value: the new severe flag is deterministic and directly tied to real chunk bounds (correctly handling the runt-fold case, which the pre-merge relativizer does NOT special-case since it uses per-chunk ACTUAL span for ITS OWN classification pass, a genuinely different code path from the final merge-time classification that uses the NOMINAL `chunk_duration_seconds` for every chunk uniformly - a second, independent motivation for this detector beyond what the issue text stated).

### Interpretations beyond the literal contract

- **Flag naming.** The issue text says "SEVERE flag `chunk_window_mismatch`" and "MILD `chunk_window_mismatch`" as if both severities share one literal string. I implemented two DISTINCT flag strings, `chunk_window_mismatch_severe` and `chunk_window_mismatch_mild`, matching the established `_severe`/`_mild` suffix convention every other pair in this file uses (`backward_jump_severe`/`backward_jump_mild`, etc.) - `transcript_quality_flags_are_severe` works by frozenset membership on the exact string, so one shared name for both severities would make it impossible to distinguish them once persisted to `transcript_quality_flags`. I judged the issue's phrasing as shorthand for the concept, not a literal naming instruction, and matched the codebase's own convention instead.
- **Where severity is computed.** The contract left it open ("pick what matches the existing style") whether `merge_chunked_transcripts` computes severity itself or returns raw counts for the caller to classify. I chose the latter (`_classify_chunk_window_violations` as a separate pure function, called from `_run_chunked_transcript_url`), mirroring the existing split between `_assess_chunk_coverage` (per-chunk, called in the loop) and `assess_transcript_artifact` (the shared pure assessor) - keeping `merge_chunked_transcripts` focused on merging plus raw counting, and severity policy in one place callers can reuse.
- **`chunk_bounds` return-shape backward compatibility.** The contract said "an additional key on the returned dict, or a second return value." I chose a key, `"_chunk_window_violations"`, added ONLY when `chunk_bounds` is supplied - this keeps the existing `tests/test_chunked_transcript.py::test_empty_chunk_list_returns_empty_merged` (an exact-dict-equality assertion) passing unmodified, rather than updating that pre-existing test file for a feature it doesn't exercise.
- **Two writer-integration tests, not the minimum one.** The contract asked for "severe flows to meta + exit code using the real writers." I added both a severe case and a healthy control (`test_healthy_chunks_do_not_flag_and_exit_clean`) so a future regression that always fires the flag (a `>` vs `>=` typo, an inverted condition) would be caught by the healthy test even if the severe test's assertions were loosened.

### Housekeeping

`ruff format .` / `ruff check . --fix` run at repo scope reformatted four files unrelated to this change (`docs/plans/gate1-evidence/issue-50-repair-yfjf-timestamps.py`, `scripts/register_obsidian_vault.py`, `skill-eval-workspace/run_trigger_eval_win.py`, `tests/test_identity_meta.py`) - pre-existing formatting drift, not something this PR should carry. Those were reverted with `git checkout --` before committing; only `scripts/video_intel.py`, `tests/test_chunk_window_mismatch.py`, `docs/plans/gate1-evidence/issue-158-chunk-window-mismatch-smoke.py`, and `CLAUDE.md` are in the diff. `skill-eval-workspace/run_trigger_eval_win.py` also has three pre-existing ruff findings (SIM105, SIM115, B007) unrelated to this change; left untouched.

### Nothing parked

Everything in the binding contract was implemented as specified, with the two interpretations noted above (flag naming, severity-computation location) called out rather than silently decided.
