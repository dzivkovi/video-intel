---
title: Gemini 2.5 Pro is structurally unfit for chunked video transcription on long political content
date: 2026-04-27
category: integration-issues
module: video_intel.py
problem_type: model-selection
component: chunked-transcript
symptoms:
  - "Pro 2.5 chunked transcription produces transcripts with chunk-2 content displaced into chunk-1 timeline (hour digit dropped)"
  - "Pro 2.5 chunk-3 has systematic speaker inversion: every Sachs line labeled Tucker, every Tucker line labeled Sachs"
  - "Pro 2.5 emits fractional-second timestamps (e.g. 00:00.040) that crashed the merge sort before the parser fix"
  - "Pro 2.5 returns task-wrapper JSON shape ([{task, output}, ...]) that the chunked path was not unwrapping"
  - "Pro 2.5 defaults media_resolution to HIGH, costing ~3x Flash 3 input tokens for no quality benefit"
root_cause: "Chunked transcript code path in scripts/video_intel.py was implicitly Flash-coded. Three fixable client-side gaps surfaced when running Pro 2.5; three remaining defects are Gemini-side output corruption that the chunked path cannot recover from."
resolution_type: model_selection_with_defensive_hardening
severity: high
tags:
  - gemini
  - gemini-3-flash-preview
  - gemini-2.5-pro
  - transcript
  - chunking
  - model-selection
  - cost
  - politically-heavy-content
related_components:
  - scripts/video_intel.py
  - scripts/timestamp_utils.py
  - tests/test_chunked_transcript.py
  - docs/solutions/integration-issues/gemini-soft-stop-political-content-20260411.md
  - docs/solutions/integration-issues/gemini-model-selection-preview-vs-ga.md
  - docs/solutions/integration-issues/gemini-pro-task-wrapper-and-cyrillic-intrusions-20260426.md
---

# Gemini 2.5 Pro chunked transcription investigation (2026-04-27)

## Context

`scripts/translate_video.py` was switched to `gemini-2.5-pro` as the default in April 2026 because Preview models hung on long videos and Flash gave wrong dialect / incomplete coverage on translation tasks (see [gemini-model-selection-preview-vs-ga](gemini-model-selection-preview-vs-ga.md)). That precedent — "Pro is the production tier when the prompt has multiple interacting constraints" — naturally raised a question for the **chunked-transcript** path: does the same rule apply?

A real test fell into our lap on 2026-04-27. While shipping the issue #58 timestamp-normalization fix, the closing-loop semantic comparison between our Flash 3 transcript and YouTube's SRT for a 2h2m35s Tucker Carlson / Jeffrey Sachs interview surfaced one compressed window (`1:27:15–1:39:59`) where Flash 3 collapsed dialogue into SCREEN markers — likely Flash's safety-filter response to politically heavy content per [gemini-soft-stop-political-content](gemini-soft-stop-political-content-20260411.md).

Hypothesis: Pro 2.5 should handle politically heavy content better than Flash 3 (per the soft-stop doc's own evidence on translate-bcs).

This doc records what we found.

## Tested matrix (Tucker × Jeffrey Sachs, sFow6dOMfgQ, 2h2m35s, 3 chunks of 50 min)

| Run | Model | Outcome | Cost | Notes |
|---|---|---|---:|---|
| 1 | gemini-3-flash-preview | Corrupted timestamps `[100:XX:XX]` | $0.50 | Surfaced issue #58 |
| 2 | gemini-3-flash-preview | Timestamps clean, chunk 2 thin | $0.50 | Surfaced thinking-budget burn (Gate 2) |
| 3 | gemini-3-flash-preview | **Clean and complete** (with 12-min compressed window) | $0.50 | Shipped baseline in PR #59 |
| 4 | gemini-2.5-pro (HIGH res default) | Crashed: chunk-2 RemoteProtocolError | $4.00 | Server-side disconnect |
| 5 | gemini-2.5-pro (LOW res, task-wrapper unwrap) | Crashed: fractional-second timestamp parse error | $2.65 | Surfaced parser quirk |
| 6 | gemini-2.5-pro (all client-side fixes) | Completed but **structurally corrupt** | $2.65 | Three model-side defects identified |

**Total tonight:** ~$11. Worth it for the durable findings below.

## Five chunked-path bugs found in the client code (we fix these)

| # | Bug | Surfaced via | Fix shipped | File |
|---|---|---|---|---|
| 1 | Missing `normalize_timestamp` pre-pass (PR #51 follow-up) | Flash 3 chunk-3 emitted `[100:XX:XX]` malformation | PR #59 | `scripts/video_intel.py:_classify_and_offset_timestamp` |
| 2 | Dynamic-thinking budget can stochastically consume output | Flash 3 chunk-2 produced 1,484 output tokens with 15,013 thinking | PR #59 (`_make_thinking_config_for_transcript`) | `scripts/video_intel.py` |
| 3 | No per-chunk content sanity check; silent thin chunks shipped as `status: ok` | Same Flash 3 thin run as #2 | PR #59 (`_assess_chunk_coverage`) | `scripts/video_intel.py` |
| 4 | Task-wrapper JSON shape (Pro 2.5) not unwrapped in chunked path | Pro 2.5 attempt 2 false-positive thin (data was there) | This commit | `scripts/video_intel.py` chunked-path JSON parse |
| 5 | `timestamp_to_seconds` crashed on fractional seconds (Pro 2.5 quirk) | Pro 2.5 attempt 2 merge-sort ValueError | This commit | `scripts/video_intel.py:timestamp_to_seconds` |
| 6 | `media_resolution` not set explicitly; Pro 2.5 default is HIGH (~3× Flash 3 cost) | Pro 2.5 attempt 1 prompt=885k vs Flash 3's 273k | This commit (set explicit LOW) | `scripts/video_intel.py:_run_chunked_transcript_url` |

All six fixes are **defensive code that helps any model**, not Pro-specific:
- Fix #1 handles a Flash quirk that Pro never emitted
- Fix #4 is a no-op for Flash (returns flat envelope) and a real fix for Pro
- Fix #5 is a no-op for whole-second timestamps (Flash) and a real fix for fractional (Pro)
- Fix #6 is a no-op for Flash (which already defaults to LOW per empirical token math) and a 3× cost reduction for Pro

When Gemini ships their next model, these defenses cost nothing and may save the next investigation.

## Three Pro 2.5 structural defects we CANNOT fix on our side

These are Gemini-side output corruption that no parser layer can recover from. They are why Pro 2.5 is unfit for purpose on this code path:

### Defect A: Chunk-1 timeline contains chunk-2 content with hour digit dropped

Pro 2.5 chunk 1's output contained 16+ entries that semantically belong to chunk 2 (50:00–1:40:00 territory) but with timestamps stamped as if they were chunk-1 entries — the leading hour digit was simply omitted.

| Pro 2.5 timestamp | Pro 2.5 content (excerpt) | Where it actually belongs (Flash 3) |
|---|---|---|
| `[01:33.801]` | "Wait, Ben-Gurion said that?" | `[1:01:33]` Tucker |
| `[02:53.301]` | 1500-line Christian-Zionism megablock | `[1:02:53]` to `[1:13:18]` Sachs |
| `[03:19.481]` | "AIPAC money…" | `[1:13:19]` Tucker |

This means a downstream BCS translator working from Pro 2.5 would produce subtitles that don't sync with the video at all in the second hour.

### Defect B: Chunk-3 has systematic speaker inversion from `[1:47:46]` onward

Every Sachs line is labeled Tucker, every Tucker line labeled Sachs. The interview's most-quotable Sachs content is misattributed:

| Sachs's actual content | Pro 2.5 attribution |
|---|---|
| "I've studied history…Cuban Missile Crisis…World Wars I and II upside down" | **Tucker** |
| "Curtis LeMay said no go blow up the commies…Kennedy did save the world" | **Tucker** |
| "Rand Paul…I regard him as the best senator…Fetterman" | **Tucker** |
| "**We have one overwhelming delusion**…America reigns supreme" (Sachs's signature closing) | **Tucker** |
| "Or ends. Exactly. Thank you." | mangled, then label flips again at the goodbye |

A BCS translation from this would invert the interview's meaning: Tucker becomes the public intellectual, Sachs the passive listener.

### Defect C: Six SRT-confirmed anchor terms missing

| Anchor | YouTube SRT | Flash 3 | Pro 2.5 |
|---|---|---|---|
| Mossadegh | YES | YES | **MISSING** (spelled "Mosadek" once in chunk 1, never again) |
| JCPOA | YES x2 | YES | **MISSING** |
| Danny Danon (the name) | YES | YES (`[54:54]`) | **MISSING as proper name** |
| Half-trillion (Trump budget) | YES | YES | **MISSING** |
| Maven (Pentagon AI) | YES | YES | **MISSING** (garbled to "uhman system") |
| Palantir | YES | YES (`[10:40]`, "160 schoolgirls killed") | **MISSING** entirely |

The Palantir / 160-schoolgirls passage is a load-bearing concrete claim in the interview. Its absence in Pro 2.5 is not compression, it's omission.

## Decision matrix: which path for which use case

The user has three distinct ways to produce a Bosnian/Croatian/Serbian translation:

| Use case | Recommended path | Why |
|---|---|---|
| **Short video (<90 min), reliable English captions, captions-only is acceptable** | `translate_video.py --url URL` (default) | Cheap (~$0.10–$0.30), fast (<2 min). Issue #49 SRT drift only matters above ~90 min. |
| **Long video (>90 min) OR captions of poor quality** | Two-step: `video_intel.py transcript --url URL` → `translate_video.py --from-transcript PATH` | Avoids issue #49 SRT drift. Rich transcript captures speaker labels and on-screen content. |
| **Politically heavy long-form** (war, finance, policy interviews) | Same two-step flow, **stay on Flash 3 for the transcript step.** Do NOT use Pro 2.5 for chunked transcription. | Pro 2.5's three structural defects (above) make it unfit despite its better single-call performance. |
| **Mixed content (slides + dialogue, OCR-heavy)** | Two-step flow with `--chunk-minutes 30` (smaller chunks per ivendrov empirical pattern) | Smaller chunks degrade less on dense content; CLI flag exists. |

## YouTube SRT vs rich transcript — pros and cons

| | YouTube SRT (auto-generated) | Rich transcript (`video_intel.py transcript`) |
|---|---|---|
| Word coverage | High (~25k words for 2h video) | Medium (~15k words; paragraph-aggregated by Flash, more by Pro) |
| Speaker labels | None | Yes (with role parentheticals) |
| On-screen content | None | Yes (SCREEN markers, OCR overlays) |
| Cost | Free | ~$0.50 per 2h video on Flash 3 |
| Time | Instant | ~5 min |
| Long-video drift (#49) | Yes | No (chunked + normalized) |
| Politically heavy compression | None (verbatim) | Some (Flash 3 compresses ~12-min window on Tucker case) |
| Best for | Watching with subtitles | Translation, search indexing, knowledge graphs |

**Combine the best of both:** use SRT as a sanity-check baseline against the rich transcript (tonight's closing-loop methodology). When they diverge by content, investigate which is more faithful and document the gap.

## Cost numbers (Tucker baseline, 2h2m35s)

| Path | Cost per run | Notes |
|---|---:|---|
| `translate_video.py --url` (SRT-first) | $0.10–$0.30 | One Gemini call against ~10–15k caption tokens |
| `translate_video.py --url --force-video` | $1–$2 | One Gemini call at LOW media res for the audio |
| `video_intel.py transcript --url` (Flash 3 chunked, default) | $0.50 | 3 chunks × ~273k input tokens × Flash 3 pricing |
| `video_intel.py transcript --url --model gemini-2.5-pro` (with media_resolution=LOW after fix #6) | $2.65 | 3 chunks × ~310k input tokens × Pro 2.5 pricing — but **structurally broken output** |
| `video_intel.py transcript --url --model gemini-2.5-pro` (without fix #6, HIGH default) | $7+ | Was $4 for one chunk before crash on attempt 1 |
| Two-step (Flash 3 transcript + transcript→BCS) | $0.50 + $1–2 = $1.50–$2.50 | Recommended for long political content |

For comparison: SRT-first translation of Tucker on the original 2026-04-26 attempt that surfaced issue #49 cost ~$0.50 but produced drifted timestamps that the user manually corrected.

## Comparison methodology (worth reusing)

Tonight we ran semantic comparisons three times:

1. **Flash 3 thin-run vs SRT** — surfaced the chunk-2 content gap (47 min missing)
2. **Flash 3 clean-run vs SRT** — confirmed coverage; found the 12-min compression in chunk 2's politically heavy window
3. **Pro 2.5 vs Flash 3 (direct)** — surfaced the three structural defects in Pro 2.5

The pattern that worked:

- Spawn a fresh-context sub-agent (no model bias from the orchestrator's investment)
- Provide both files explicitly with byte counts and word counts
- Ask for: coverage map at fixed timestamps, chunk-boundary scrutiny, topic-flow check, anchor-phrase check, length sanity, format observations, verdict
- Be specific about what each previous run found; ask "did this fix the issue or introduce a new one?"
- Cross-check log-token metrics (`thoughts`, `candidates`) against transcript content; large `thoughts` value with thin transcript = budget burn
- Three sub-agents on the same question with different framings tend to converge on the same answer; useful triangulation

Token-level metrics from the run log (`thoughts=N candidates=M`) are leading indicators that the transcript file alone won't reveal. Always inspect both.

## Future-proofing: when models change again

The six client-side fixes (above) are kept as defensive code even though Pro 2.5 is unfit today. The next model release may behave like Pro 2.5 (task-wrapper, fractional seconds, HIGH default) or like Flash 3 (flat envelope, whole seconds, LOW default) — our chunked path now handles both.

When the next model arrives, the playbook is:

1. Run the same Tucker URL through `transcript --url --model <new-model>` with `--force`.
2. Compare the result to `work/2026-04-27/11-tucker-flash3-baseline-pre-pro-test.transcript.md` (preserved baseline).
3. If new defects emerge: document them in a follow-up file matching this template.
4. If the new model is cleaner: update `config.yaml` default and re-validate the whole corpus.

## Related solutions

- [`gemini-model-selection-preview-vs-ga.md`](gemini-model-selection-preview-vs-ga.md) — translate_video.py model choice
- [`gemini-soft-stop-political-content-20260411.md`](gemini-soft-stop-political-content-20260411.md) — Pro 2.5 silent truncation on heavy content
- [`gemini-pro-task-wrapper-and-cyrillic-intrusions-20260426.md`](gemini-pro-task-wrapper-and-cyrillic-intrusions-20260426.md) — Pro 2.5's task-wrapper format in the salvage path

## Evidence files (preserved)

All under `c:/Users/danie/ws/Skills/video-intel/work/2026-04-27/`:

- `06-tucker-transcript-bg.log` — Flash 3 run #1 (corrupted)
- `07-tucker-chunk3-corruption-baseline.transcript.md` — Flash 3 run #1 artifact
- `09-tucker-revalidation.log` — Flash 3 Gate 1 (thin chunk 2)
- `10-tucker-gate2-revalidation.log` — Flash 3 Gate 1+2 (clean baseline)
- `11-tucker-flash3-baseline-pre-pro-test.transcript.md` — **the validated Flash 3 baseline used for comparison**
- `12-tucker-pro25-validation.log` — Pro 2.5 attempt 1 (HIGH res, RemoteProtocolError)
- `13-tucker-pro25-attempt2.log` — Pro 2.5 attempt 2 (LOW res, fractional-seconds crash)
- `14-tucker-pro25-attempt3.log` — Pro 2.5 attempt 3 (completed, structurally corrupt)
- `16-tucker-pro25-BROKEN-evidence.transcript.md` — Pro 2.5 attempt 3 artifact (forensic record)
- `17-tucker-flash3-with-hardening.log` — Flash 3 re-run with all six fixes (validation that hardening doesn't regress Flash 3)
