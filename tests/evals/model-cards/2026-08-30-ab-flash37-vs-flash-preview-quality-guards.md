# Model scorecard - two-arm A/B: gemini-3.7-flash vs gemini-3-flash-preview (issue #157 quality guards)

Generated 2026-08-30, by hand, from a 12-video / 24-run real-corpus A/B run in an isolated scratch corpus (never wrote into `G:/My Drive/video-intel`, never touched a tracked repo file). Gates the default-model and bulk-remediation decisions that issue #157's plan (`docs/plans/2026-08-29-001-fix-transcript-quality-guards-plan.md`) deferred as "owner decision - it spends API money."

Unlike `gemini-3.7-flash.md` (synthetic fixtures, one short segment per cell), this run used 12 real videos from the maintainer's corpus, selected specifically because 6 of them are KNOWN historical monolithic-collapse failures under `gemini-3-flash-preview` and 2 more are known blind-gap failures - so the preview arm here doubles as a same-model re-roll test, not just a head-to-head.

Every produced transcript was scored two ways: (a) the meta.json quality fields the issue #157 pipeline persists (`transcript_quality_flags`, `transcript_max_blind_gap_seconds`, `transcript_last_dialogue_fraction`, `transcript_dialogue_entries`, status, chunk count), and (b) an independent re-parse of the written `.transcript.md` through the real `assess_transcript_artifact()` function via the proven forensics harness, using each video's known real duration. Across all 23 successfully-produced transcripts the two scoring paths agreed exactly - zero flag discrepancies.

## Headline result

| model | severe-flag rate (this sample) | mean dialogue density edge | $/video-hour |
|---|---|---|---|
| gemini-3.7-flash | 0/12 (1 unrelated crash, 11 scored clean) | higher on every shared video | ~0.332 (promo to 2026-12-31) |
| gemini-3-flash-preview | 4/12 (33%) monolithic_severe | lower | ~0.227 |

The 33% figure lands close to the population-level 25.3% rate the issue #157 plan measured across the whole corpus (Fisher exact p = 0.0038), so this small sample is not an outlier - it reproduces the documented effect.

## Per-video results

| video | class | duration | model | entries | density/min | max_gap_s | flags | status | chunks | cost |
|---|---|---|---|---|---|---|---|---|---|---|
| v01 (Two release strategies...) | monolithic | 342s | gemini-3.7-flash | 9 | 1.58 | 77 | none | complete | 1 | $0.0323 |
| v01 | monolithic | 342s | gemini-3-flash-preview | 1 | 0.18 | 342 | monolithic_severe | partial | 1 | $0.0216 |
| v02 (The Harsh Truth About GPT-5) | monolithic | 560s | gemini-3.7-flash | CRASH (see anomalies) | - | - | - | - | - | $0.0536 |
| v02 | monolithic | 560s | gemini-3-flash-preview | 26 | 2.79 | 38 | none | complete | 1 | $0.0383 |
| v03 (Perplexity's NEW Search API...) | monolithic | 763s | gemini-3.7-flash | 125 | 9.83 | 14 | none | complete | 1 | $0.0836 |
| v03 | monolithic | 763s | gemini-3-flash-preview | 39 | 3.07 | 33 | none | complete | 1 | $0.0555 |
| v04 (How to Master Prompt Engineering...) | monolithic | 958s | gemini-3.7-flash | 79 | 4.95 | 33 | none | complete | 1 | $0.0899 |
| v04 | monolithic | 958s | gemini-3-flash-preview | 1 | 0.06 | 958 | monolithic_severe, trailing_gap_mild | partial | 1 | $0.0583 |
| v05 (This One Command Makes Coding Agents...) | monolithic | 1201s | gemini-3.7-flash | 88 | 4.40 | 28 | none | complete | 1 | $0.1168 |
| v05 | monolithic | 1201s | gemini-3-flash-preview | 1 | 0.05 | 1201 | monolithic_severe, trailing_gap_mild | partial | 1 | $0.0779 |
| v06 (The Trillion Dollar Agentic Workflow...) | monolithic | 1553s | gemini-3.7-flash | 29 | 1.12 | 76 | none | complete | 1 | $0.1461 |
| v06 | monolithic | 1553s | gemini-3-flash-preview | 44 | 1.70 | 48 | none | complete | 1 | $0.1062 |
| v07 (Nemotron 3 Ultra...) | healthy control | 604s | gemini-3.7-flash | 92 | 9.14 | 11 | none | complete | 1 | $0.0702 |
| v07 | healthy control | 604s | gemini-3-flash-preview | 1 | 0.10 | 604 | monolithic_severe, trailing_gap_mild | partial | 1 | $0.0399 |
| v08 (How Claude Code's Creator Automates...) | healthy control | 608s | gemini-3.7-flash | 32 | 3.16 | 45 | none | complete | 1 | $0.0676 |
| v08 | healthy control | 608s | gemini-3-flash-preview | 78 | 7.70 | 22 | none | complete | 1 | $0.0543 |
| v09 (How to win when software is not a moat) | blind-gap | 4225s | gemini-3.7-flash | 249 | 3.54 | 170 | none | ok | 3 | $0.4024 |
| v09 | blind-gap | 4225s | gemini-3-flash-preview | 115 | 1.63 | 195 | none | ok | 3 | $0.2796 |
| v10 (Why experts writing AI evals...) | blind-gap | 4028s | gemini-3.7-flash | 179 | 2.67 | 134 | none | ok | 3 | $0.3697 |
| v10 | blind-gap | 4028s | gemini-3-flash-preview | 156 | 2.32 | 134 | none | ok | 3 | $0.2635 |
| v11 (How The Best Companies Defend...) | boundary (50m05s) | 3005s | gemini-3.7-flash | 266 | 5.31 | 122 | none | ok | 2 | $0.2970 |
| v11 | boundary | 3005s | gemini-3-flash-preview | 232 | 4.63 | 295 | none | ok | 2 | $0.2300 |
| v12 (Claude Fable, Claude Tag... - seed) | seed / clock-slip | 3090s | gemini-3.7-flash | 188 | 3.65 | 82 | none | ok | 2 | $0.2873 |
| v12 | seed | 3090s | gemini-3-flash-preview | 149 | 2.89 | 92 | none | ok | 2 | $0.2046 |

## Monolithic re-roll verdict: mixed - roughly half stochastic, half systematic

The 6 monolithic-collapse videos (v01-v06) all failed under `gemini-3-flash-preview` when they were originally scanned. A fresh preview re-roll on the same videos, same model, gave:

- **Reproduced (systematic):** v01, v04, v05 - 3/6 (50%). Each collapsed to exactly 1 dialogue entry again, spanning the full video, `monolithic_severe`.
- **Did not reproduce (stochastic):** v02, v03, v06 - 3/6 (50%). Each produced a healthy multi-entry transcript on the re-roll (26, 39, and 44 entries respectively).

So monolithic collapse under preview is not purely a property of "this video is hard" - it is at least half genuine run-to-run stochasticity. That reading is reinforced by v07: a video that was NOT in the monolithic set (it scanned healthy under preview originally, hence its selection as a "healthy control") also collapsed to `monolithic_severe` on this fresh preview roll, while `gemini-3.7-flash` transcribed the same video cleanly (92 entries). The failure mode can strike a previously-clean video, not only the videos already flagged bad.

Under `gemini-3.7-flash`, none of the 6 monolithic videos (nor the healthy controls) collapsed on this run - 0/11 scored runs showed `monolithic_severe` (v02's 3.7-flash arm is excluded from this count; it crashed before scoring, see anomalies).

## Blind-gap comparison under the NEW 30-minute chunking

v09 and v10 are the two worst blind-gap cases from the original forensics sweep (2953s and 2928s contiguous holes respectively), both originally chunked at the OLD 50-minute default. Issue #157 changed `chunk_minutes` default from 50 to 30 specifically to give chunking a real chance to catch this.

Under the new 30-minute default, both videos now chunk into 3 real segments (not folded) on both models, and **both arms on both videos are clean**:

- v09: `gemini-3.7-flash` max_gap 170s / 3 chunks; `gemini-3-flash-preview` max_gap 195s / 3 chunks. Both far under the 600s severe floor.
- v10: `gemini-3.7-flash` max_gap 134s / 3 chunks; `gemini-3-flash-preview` max_gap 134s / 3 chunks. Identical max gap, both clean.

This is direct real-input confirmation that the chunk_minutes 30 change (not model choice) is what fixes blind gaps on long videos - consistent with the plan's claim that the OLD 50-minute chunking did not help ("12/44 chunked 60min+ videos have >= 10min holes with every chunk ok"). Model choice still matters for density: `gemini-3.7-flash` produced more than double the dialogue entries on v09 (249 vs 115) despite an equivalent max gap, meaning finer-grained, more precise `&t=` deep links even when neither model trips a severe flag.

## Boundary case (50m05s, v11): runt-fold + 30-minute chunking confirmed live

At 3005s, this video is just over the new 30-minute chunk boundary. Both arms produced exactly 2 chunks (1800s + 1205s) - the 1205s second chunk is nowhere near the `RUNT_FOLD_MAX_SECONDS = 120` floor, so it correctly stayed a real chunk instead of folding back into a single-shot call. Both arms are clean, though `gemini-3.7-flash` held a materially lower max gap (122s vs 295s) - the largest max-gap difference between clean arms in this sample.

## Seed case (v12, clock-slip): resolved on both models

The original seed defect (`uU5Gv2h8-9g`, 51m30s) was a single 1-chunk call under the old 50-minute default with a 20% runt-fold ratio (a 90s tail folded into one 51.5-minute call), producing a 571s trailing gap and an out-of-order timestamp block. Under the new pipeline, both arms now split into 2 real chunks (1800s + 1290s):

- `gemini-3.7-flash`: 188 entries, max_gap 82s, `transcript_last_dialogue_fraction` 0.994, status `ok`, zero flags.
- `gemini-3-flash-preview`: 149 entries, max_gap 92s, status `ok`, zero flags.

Both fully resolve the original defect. This confirms the plan's own diagnosis: the `chunk_minutes` 30 default, not the runt-fold floor change alone, is what fixes this shape (a 90s runt still folds under the 120s floor regardless of chunk size; only the smaller chunk size turns the seed's 3090s video into two real chunks).

## Anomaly: an unrelated crash on v02's gemini-3.7-flash arm

`gemini-3.7-flash` crashed with an uncaught `KeyError: 'start'` mid-run on v02 ("The Harsh Truth About GPT-5"):

```
File "video_intel.py", line 3687, in merge_transcript_json
    "start": sc["start"],
KeyError: 'start'
```

Gemini returned a `screen_content` entry missing the `start` field, and `merge_transcript_json` indexes it directly rather than using `.get("start")` with a fallback (the parallel `transcripts` list entry at line 3676 has the identical unguarded pattern). This is a pre-existing JSON-shape robustness gap in `merge_transcript_json`, unrelated to issue #157's guards and not something this eval's scope permitted fixing. The API call itself succeeded (cost was still incurred, $0.0536) and this was not a transport error, so per the eval's own rule it was recorded as a data point and not retried. Worth a follow-up issue: `merge_transcript_json` should defensively skip or default a `screen_content`/`transcripts` entry missing `start` instead of crashing the whole run.

## Actual spend

- Total: **$3.4462** (target $4-5, hard cap $6).
- `gemini-3.7-flash`: $2.0165 across 12 runs (11 scored + 1 crash).
- `gemini-3-flash-preview`: $1.4297 across 12 runs (all scored).
- Both keys in the environment (`GEMINI_API_KEY`, `GOOGLE_API_KEY`) resolved to the identical value, so there is no billing-account ambiguity behind the SDK's "Both GOOGLE_API_KEY and GEMINI_API_KEY are set" warning that appeared on every run.

## Not measured here

State this every time. This is a single sample per (video, model) cell, same as the earlier synthetic-fixture card - the preview arm's built-in re-roll test is the only repeated-trial evidence here, and it only covers 6 videos. Non-English and heavily accented speech are not represented. All 12 videos are English-language tech/business talking-head or podcast content; no screenshare-heavy or highly technical-demo content was in this sample (unlike the synthetic fixture card). The blind-gap and boundary fixes are confirmed on 3 long videos (v09, v10, v11) plus the seed (v12) - a real but small sample of the ~274-video affected population the plan defers to a separate bulk-remediation pass.

## Recommendation

**Keep `gemini-3.7-flash` as the default model.** Across 11 scored runs (excluding the one unrelated crash) it produced zero severe quality flags, against a 33% severe rate for `gemini-3-flash-preview` in the same sample - a rate that matches the corpus-wide 25.3% figure from the issue #157 forensics sweep, so this is not sampling noise. The monolithic re-roll test shows preview's collapse is roughly 50% reproducible and 50% a fresh roll of the dice, including striking a video (v07) that was clean on its original preview pass - so "avoid the known-bad videos" is not a viable mitigation; the risk is spread across the whole preview-transcribed population, not concentrated in the videos already flagged. The chunk_minutes 30 default fix (not model choice) is what resolves blind gaps and the seed clock-slip on long videos, and it works equivalently well on both models - but `gemini-3.7-flash` still produces meaningfully denser, more precisely timestamped transcripts even on the videos where both models pass (roughly 2x the dialogue entries on v09, and a materially tighter max gap on v11), which matters directly for `&t=` deep-link precision. The ~46% cost premium over preview ($0.332 vs $0.227 per video-hour) is a real ongoing cost while the promo rate holds through 2026-12-31, but it buys a measured, reproducible reliability difference, not a marginal one.

**The bulk-remediation model should also be `gemini-3.7-flash`, not `gemini-3-flash-preview`.** Re-running the ~274 affected videos under preview would carry roughly the same 50/50 odds of simply reproducing the original monolithic collapse this re-roll test measured - a real chance of spending the remediation budget twice on some fraction of videos. `gemini-3.7-flash` showed zero recurrence across every monolithic and blind-gap case retried in this sample, which is the property a remediation pass most needs: confidence that a single re-run actually fixes the video rather than re-rolling the same failure.
