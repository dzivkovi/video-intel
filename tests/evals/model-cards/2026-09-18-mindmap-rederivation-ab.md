# Mindmap re-derivation A/B, 2026-09-18 (issue #235)

**Question:** which model should re-derive the ~1,000 preview-era mindmaps, if any? Roughly $25 of spend plus a taxonomy rebuild and a re-index rides on it.

**Verdict: the three models are indistinguishable, and the measurable gain is the PROMPT, not the model. If you re-derive, use the incumbent `gemini-3.7-flash`. Whether to re-derive at all is a spend decision, not a measurement one.**

## Sample

30 preview-era videos with Gemini transcripts and mindmaps on disk, across 25 channels, including `ashmaurya` (4 videos) as the low-entity-density control. **27 of 30 carry a canonical name** derived from the video description, against the ticket's abandon floor of 20. 138 distinct canonical names; 355 ground-truth name groups in total.

Ground truth is description-derived canonical names (GitHub `owner/repo` slugs and product domains, with channel-boilerplate domains excluded) unioned with the repeated-proper-noun transcript heuristic - the same scorer as PR #233, now with the description half actually populated, which it was not when the original claim was made.

## Results

Two rolls per model, the shipped `mindmap-from-transcript.md` prompt, text-only against the on-disk transcripts. The on-disk arm is the existing artifact and has no roll variance by construction.

| Arm | roll 0 | roll 1 | mean | between-roll SD | vs on-disk | guards |
|---|---|---|---|---|---|---|
| **on-disk** (preview-era artifacts) | - | - | **0.3481** | - | - | - |
| `gemini-3-flash-preview` | 0.384 | 0.359 | **0.3715** | 0.0177 | +0.0234 | OK |
| `gemini-3.7-flash` | 0.376 | 0.412 | **0.3940** | 0.0255 | +0.0459 | OK |
| `gemini-3.8-flash` | 0.390 | 0.375 | **0.3825** | 0.0106 | +0.0344 | OK |

All five guard metrics held on every roll at the calibrated 0.20 tolerance.

## The ticket's own abandon criterion fires

> *"the first roll shows between-roll SD exceeding the best between-model difference - the instrument cannot resolve the question, and more rolls only buy precision on a null"*

- Best between-model difference: **0.0225** (0.3715 to 0.3940)
- Largest between-roll SD: **0.0255**

**0.0255 > 0.0225.** The instrument cannot tell the three models apart. Do not read 3.7's 0.394 as better than 3.8's 0.383 or preview's 0.372 - that ordering is inside the noise, and more rolls would only measure the null more precisely.

## What the measurement DOES resolve, and it is not what the ticket expected

Every model, including **`gemini-3-flash-preview` itself**, scores higher than the preview-era artifacts on disk. Same model, same transcripts, same scorer: **0.3715 today against 0.3481 on disk.**

The only variable between those two numbers is the prompt. Those artifacts were generated before PR #233 added the naming rule to `mindmap-from-transcript.md`.

**So the gain on offer is the prompt fix, not a model upgrade.** That reframes the decision: re-derivation buys #233's naming improvement across ~1,000 artifacts, and the model you use to buy it does not measurably matter.

It also closes the loop on the original claim. PR #233 reported on-disk 0.415 against 0.274 for "today's model" and read it as a model-era regression. Issue #235 diagnosed that as a sampling confound. This run confirms the direction is in fact **reversed**: today's models on today's prompt beat the artifacts on disk, by about 0.02 to 0.05.

## Decision rule, applied literally

> *"a model is preferred for re-derivation only if its mean recall beats the on-disk arm by more than the between-roll SD on that sample, with every guard holding."*

All three models clear it. But since they cannot be told apart from each other, "preferred" collapses to "any of them", and the correct default is the incumbent - there is no evidence to justify a switch, and the repo's standing rule is that `DEFAULT_MODEL` changes only on a measured scorecard.

## What this does NOT establish

- **That 3.8 is better or worse than 3.7 for anything.** This measured the mindmap step only, text-only, on 30 videos. The transcript step is issue #219 and is a different measurement on a different harness.
- **That the recall gain is worth $25.** +0.046 mean recall is roughly a 13% relative improvement on a metric whose ground truth is part heuristic. That is a spend judgment, and it belongs to the operator.
- **Generalization beyond these 30 videos.** Two rolls over one sample says nothing about unseen videos, and the sample skews to channels that carry description links.

## Cost

665,610 prompt tokens per arm (identical across all three - same inputs), 148,764 to 202,476 output tokens per arm. 180 text calls total. Comfortably inside the ticket's $6 hard cap.

Nothing was written into the corpus. Generated mindmaps went to a scratch directory.
