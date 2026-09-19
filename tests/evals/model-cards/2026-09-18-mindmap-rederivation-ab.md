# Mindmap re-derivation A/B, 2026-09-18 (issue #235)

**Question:** which model should re-derive the ~1,000 preview-era mindmaps, if any? Roughly $25 of spend plus a taxonomy rebuild and a re-index rides on it.

**Verdict: do not re-derive. The three models are indistinguishable from each other, and on a fair like-for-like comparison none of them beats the artifacts already on disk.**

> **This card was corrected before merge.** The first version claimed the gain was PR #233's prompt fix. A claims audit found that a third of the sample's on-disk artifacts were built by a *different prompt on a different input modality*, which manufactured the entire apparent gain. The corrected analysis is below, and the retraction is kept rather than quietly edited out - see "What the first version got wrong".

## Sample

30 preview-era videos with Gemini transcripts and mindmaps on disk, **one video each from 30 distinct channels**. All 30 verified preview-era (`model: gemini-3-flash-preview` in every meta, processed 2026-04-02 to 2026-08-14). **27 of 30 carry a description-derived canonical name**, against the ticket's abandon floor of 20. 355 ground-truth name groups.

**There is no low-entity-density control channel.** The sample builder was written to include `ashmaurya` for that purpose and silently included none, because `ashmaurya` was ingested on 2026-09-17 and therefore has no preview-era artifacts at all. The one-video-per-channel shape gives breadth but provides no repeated-video variance check, so the between-roll SDs below are the only variance evidence this card carries.

Ground truth is description-derived canonical names unioned with the repeated-proper-noun transcript heuristic. **Its precision is imperfect**: spot-checking finds `engineerprompt.ai` and `ycombinator.com` (each channel's own domain), a rotating sponsor domain, and link shorteners among the "names". That junk is unreachable for every arm about equally, so it raises the noise floor rather than favouring one model - but the ground truth should not be described as clean.

## Headline result: the three models cannot be told apart

Two rolls per model, the shipped `mindmap-from-transcript.md` prompt, text-only against the on-disk transcripts. Statistics computed from full-precision per-roll values, not from the rounded display column.

| Arm | roll 0 | roll 1 | mean | between-roll SD | guards |
|---|---|---|---|---|---|
| `gemini-3-flash-preview` | 0.38398 | 0.35929 | **0.37164** | 0.01746 | OK |
| `gemini-3.7-flash` | 0.37639 | 0.41182 | **0.39411** | 0.02505 | OK |
| `gemini-3.8-flash` | 0.39047 | 0.37516 | **0.38281** | 0.01082 | OK |

**The ticket's pre-registered abandon criterion fires.** Largest between-roll SD **0.02505** exceeds the best between-model difference **0.02247**. The instrument cannot resolve which model is better, and more rolls would only measure the null more precisely.

An independent check corroborates it. Per-video paired comparison across 29 commonly-scored videos: preview against 3.7 is 7 wins / 10 losses / 12 ties; 3.7 against 3.8 is 13 / 10 / 6. **No model dominates the pairing.**

## The comparison that decides the spend, done like-for-like

The whole-sample numbers above cannot be compared against the on-disk arm, because **10 of the 30 on-disk artifacts were not built from a transcript at all**:

| on-disk `prompt` provenance | count |
|---|---|
| `mindmap-from-transcript` | 20 |
| `mindmap-knowledge` (video path) | 6 |
| absent (legacy) | 4 |

`mindmap-knowledge` consumes video frames and audio, not the transcript text. For those 10 videos "same transcripts, same scorer, only the prompt differs" is simply untrue - the on-disk artifact never saw the transcript.

Restricting to the **20 videos whose on-disk artifact genuinely came from `mindmap-from-transcript`** (19 score, one has no ground truth) gives the only apples-to-apples test of whether re-generating helps:

| Arm | matched-subset mean recall (n=19) | vs on-disk |
|---|---|---|
| **on-disk** | **0.3539** | - |
| `gemini-3-flash-preview` | 0.3450 | **-0.0089** |
| `gemini-3.7-flash` | 0.3585 | +0.0046 |
| `gemini-3.8-flash` | 0.3483 | -0.0056 |

**The cleanest available comparison - the same model, re-run today, against its own output on disk - shows no gain and a slight loss.** All three deltas are far inside the roll-to-roll spread on this subset (3.7's two rolls were 0.320 and 0.397).

So the decision rule's "beats the on-disk arm by more than the between-roll SD" test **fails for every model** once the comparison is made fairly. Per the rule's own second clause, the on-disk artifacts stay and re-derivation closes as not worth it.

## What the first version of this card got wrong

It reported +0.0234 to +0.0459 against on-disk across the whole 30-video sample and read that as PR #233's naming rule paying off.

**That gain lives almost entirely in the 10 mismatched videos.** On those, the video-sourced on-disk artifact scores ~0.337 while every arm's transcript-sourced regeneration scores 0.41 to 0.49. That is the already-documented transcript-versus-video modality effect - CLAUDE.md records it as transcript-sourced 0.393 against video-sourced lower - and PR #233 never touched `mindmap-knowledge.md`.

The error is worth naming precisely because it is the same shape as the one this ticket was filed to correct. Issue #235 exists because PR #233 compared two arms that differed in more than the variable being studied. **The first version of this card did it again, one layer down.** The lesson is not "check the sample" in the abstract - it is that a provenance field (`meta.json`'s `prompt`) recorded the answer the whole time and was never read.

## What this does NOT establish

- **That re-derivation could never help.** It establishes that on 19 like-for-like videos it does not, by a margin the instrument cannot resolve. A different sample, or a metric other than named-entity recall, could say otherwise.
- **Anything about 3.8 for the transcript step.** That is issue #219, a different harness and a different measurement.
- **Generalization.** Two rolls over one sample, skewed toward channels that carry description links, says nothing about unseen videos.

## Cost

665,610 prompt tokens per arm (identical across all three - same inputs), 148,764 to 202,476 output tokens per arm, 180 text calls. Inside the ticket's $6 hard cap. Nothing was written into the corpus; generated mindmaps went to a scratch directory.
