---
title: "A metric that looked like ML: how named-entity recall got a target before it had an instrument"
date: 2026-09-18
last_updated: 2026-10-31
category: process-issues
tags: [evals, metrics, goodhart, scope-discipline, measurement, prompt-tuning, provenance, golden-dataset, eval-cache]
components: [prompts/mindmap-from-transcript.md, tests/evals/mindmap_entity_recall.py, prompts/concepts.md, docs/answering-questions.md, tests/evals/golden_dataset.yaml, tests/evals/test_instrument.py, scripts/model_eval.py]
severity: medium
symptoms: >
  A real one-line prompt defect grew into a research programme with a >=90% acceptance
  target, a hand-labelling task for the owner, and a proposed entity-extraction layer -
  for a capability a free interactive tool already performed better, and whose stated
  justification was never checked against the code that would have to carry it. Later
  eval work repeated the family: a sample headline retracted after a free corpus-wide
  rescore, a scorecard that credited a prompt fix to a mixed-provenance arm, and a batch
  plan ordered on a premise the eval cache had already falsified.
root_cause: >
  The acceptance criterion was written before any instrument existed, borrowed from the
  shape of a familiar ML metric rather than derived from a measurement. The ticket's own
  impact claim ("it propagates to the taxonomy") was plausible, load-bearing, and false;
  verifying it cost one grep. No pass was made over how else the same question could be
  answered before committing to build. The later cases share the cause one layer down:
  the number was read before the ruler that produced it was checked.
---

# A metric that looked like ML

## What happened

A mindmap turned a named GitHub repo into "Polished interactive diagrams generated from raw codebase" - a named repo replaced by a description of it. Real defect, correctly spotted, worth fixing.

Issue #228 was filed with the acceptance criterion **"named-entity recall >= 90%"**. A prompt fix shipped (PR #233), measured over 15 videos across five content types: control 0.274, variant 0.329, every guard metric held. That fix is good and it stands.

Everything after it was wrong. The ticket stayed open against an unreachable target; a ground truth was built that mixed tools, people, sponsors, channel names and job titles; the owner was handed a 62-line hand-labelling worksheet; and a corpus-wide entity-extraction layer was drafted. The ticket closed as delivered at the narrow claim, and nothing further was built.

## The three failures, in the order they compounded

### 1. The target existed before the instrument

At the moment `>= 90%` was written there was no ground truth, no scorer and no measurement, so the number could not have been derived from anything. It came from the shape of a familiar metric: "recall" is a real ML term with real literature, and 90% is the kind of number that appears in it.

The repo already had this exact rule, one layer over. `CLAUDE.md` states that changing the transcript model without a scorecard **is** the regression, because a spec-sheet claim is not a measurement. The rule was applied to models and broken for metrics the same night.

**The damage is not that the target was unreachable. It is that an unreachable target makes every real improvement look like a failure** - a measured, guard-holding +0.055 read as falling 60 points short, which is what kept the ticket open.

### 2. The impact claim was load-bearing and never verified

The P1 justification read: *"a systematic loss of proper nouns degrades search, the taxonomy, and every briefing built on top of them."* Plausible, and it is what made the ticket P1.

It is false, and one grep settles it. `prompts/concepts.md:11`:

> `Skip proper nouns (product names, company names, person names) UNLESS they represent a concept category`

The live taxonomy confirmed the instruction is obeyed: **zero proper-noun labels.** Probed `LanceDB`, `Claude Code`, `Cursor`, `LangGraph`, `n8n`, `Zapier` and the repo name from the original defect - every one returned NONE. A name lost in the mindmap was never going to reach `taxonomy.json` or concept-mode search.

Search is unaffected for a second, independent reason: **the LanceDB index is built from transcripts, not mindmaps.** `search "<name>" --vector` ranked the right video first corpus-wide. The mindmap is not on the retrieval path at all.

So the true scope was always "the mindmap is a slightly worse reading surface" - a correctness property of one artifact, not a corpus-wide integrity risk.

### 3. Nobody asked how else the question could be answered

The decisive evidence came from the owner, not from the engineering. He asked YouTube's own Ask feature "which GitHub repos were mentioned in this video?" against the **same video the ticket was filed from**, and got 14 repos with timestamps, correctly separating audience submissions from the host's list, with zero junk entries.

The heuristic ground truth for that same video contained the sponsor's name, the channel's own name, `linkedin`, `Solution Architect`, `Audience Member` and `New York`.

**A free interactive tool beat the thing being built, on the originating example, with no ingest and no tuning.** And the cross-video half - "where else has this appeared" - was already covered by hybrid search. The capability had two working implementations before a line was written.

## What would have caught each one, and what it cost

| Failure | The check that catches it | Cost |
|---|---|---|
| Target before instrument | Measure first, state the observed range, then decide whether a threshold is even meaningful | zero - the harness was already free to run |
| Unverified impact claim | Grep the code that would have to carry the propagation | one grep |
| No alternatives pass | Ask the question of every tool already available, including the ones outside the repo | minutes |

A fourth, found the same week and worth repeating: **a claim about a corpus should be checked against the corpus when the corpus is on disk.** The "model-era regression" headline (0.415 vs 0.274) was a sampling artifact; a free corpus-wide rescore gave 0.393 vs 0.359, paired +0.017. The rescore cost nothing and would have prevented the claim being written.

## The rules this produces

**Does this metric deserve to exist?** Sibling question to [the feature-worthiness rubric](feature-worthiness-rubric-20260831.md), and the checks are:

1. **Measure before you set a bar.** Run the control twice. A threshold chosen before the noise floor is known is a guess, and one tighter than the noise silently discards real improvements (this happened here too: a 10% guard against a 15% measured swing rejected two variants).
2. **Verify the propagation claim.** If the justification is "this degrades X downstream", open X and confirm it consumes the thing. Plausible-and-unchecked is how a P3 becomes a P1.
3. **Name what is already answering the question.** Including tools outside the repo. If something free and interactive does it better, the correct output is a closed ticket.
4. **Score a vector, never a scalar.** Held here: a variant scored highest on recall and collapsed quantitative detail 60 to 37. A scalar would have shipped it.
5. **A regression instrument is not a product feature.** Giving an internal QA number a product-shaped acceptance target is what converts a one-line fix into a research programme.
6. **Ask for the answer before asking for the feature.** "Which people appear on 3+ channels" was one regex over data already on disk, answered in a minute. Build only when the same question returns.

## Check the ruler before reading the number

Later eval work hit the same family of mistake in four more shapes. Each check below is cheaper than the measurement it protects, and each should run before a number is quoted, a verdict is written or a batch is ordered.

### 1. Does the ruler still cover the corpus?

`tests/evals/golden_dataset.yaml` was frozen when the corpus was about a fifth of its current size and drew on under a tenth of today's channels. Its 25 queries can only reward videos from those original channels, and one coverage analysis found that a large share of what hybrid search now returns comes from channels the set cannot reward at all. So the 1/25 that has held flat across several index states (the baseline paragraph in `CLAUDE.md`, and `docs/testing.md`) does not show that retrieval is stable. It shows the ruler sees a shrinking slice of the corpus.

The measurability audit (`tests/evals/test_instrument.py`) cannot catch this. It checks that each query's gating thresholds are reachable on the index on disk (dead ids, missing windows, unreachable channels, the dedup cap), not that the queries still represent the corpus. A green audit and a stale golden set can coexist. `docs/testing.md` reads the zero-recall queries as retrieval failure rather than coverage because every golden video is indexed; that holds in the narrow sense, not the broad one. Any change to the set still needs ADR-grade justification under ADR-0017 and an entry in the CHANGE LOG at the top of the file.

### 2. Does each arm's provenance match the claim?

An A/B that compares fresh output with artifacts already on disk must first filter the on-disk arm by each `meta.json`'s `model` and `prompt` fields. In the mindmap re-derivation A/B (issue #235, card at `tests/evals/model-cards/2026-09-18-mindmap-rederivation-ab.md`) a third of the on-disk arm was not built from a transcript: some came from the video path, the rest recorded no `prompt` at all. The whole-sample gain was that modality difference, and the first version of the card credited it to PR #233's prompt fix. Restricting to the matched subset removed the apparent gain, and the card's verdict became "do not re-derive".

The same card carried a pre-registered abandon rule (largest between-roll SD above the best between-model difference), and it fired: the instrument could not separate the models. Write the abandon rule before the run, so a null result is a finding rather than a disappointment.

### 3. Does the denominator depend on one arm?

The repeated-proper-noun ground truth grows with the richness of the transcript it is built from (`.claude/rules/evals.md` records this). When one arm's input is richer, its denominator is larger and its recall drops mechanically. That is a bias in one direction, not noise: more rolls do not average it away, and it can manufacture or flip a verdict. The "model-era regression" above had this as a second cause. Before comparing arms, ask whether any part of the scorer is computed from one arm's input.

### 4. Does cached output already answer the question?

`scripts/model_eval.py` caches every call's raw response and token usage under `tests/evals/model-cards/_raw/` (gitignored), so a re-run of the same cell costs nothing. Those files can also settle a suspicion before any re-run is considered. The issue #141 family of worry was that a fixture's 600-second window was ignored and the whole video sent. The cached usage files answer the input side directly: input tokens are near-identical across fixtures drawn from videos of very different lengths, which only happens if each call received the clipped window. Whether the model's output stayed inside the window is a separate question, answered by the `window_exceeded` field from issue #219.

The same habit applies to tickets: run the ticket's own falsifier and open the cached output before writing code or ordering a batch. In one run, checks that took minutes closed two tickets and removed a false ordering constraint from the batch plan.

### The four checks as a list

- Does the eval set still represent the corpus, or only the corpus it was frozen on?
- Does every on-disk artifact in an arm carry the `model` and `prompt` provenance the claim assumes?
- Is any part of the scorer, especially a denominator, computed from one arm's input?
- Does the cache already hold the answer (`in_tok`, raw output) before a re-run is paid for?

## The process lesson

The owner's framing, which is the real generalization: *do not jump on a semi-baked idea and build it - explore the possible futures first.*

A half-formed idea stated out loud is an invitation to think, not a specification. Before committing engineering to one, the cheap move is a pass over how else the outcome could arrive - existing features, tools outside the repo, an ad hoc query, or not needing it at all - and to say plainly which of those the build is better than. Here that pass would have cost minutes and would have ended the ticket at the shipped prompt fix.

Two model layers were used on the closing pass and they found different things: a high-thinking judgment pass produced the "seasoning, not a main course" verdict and found the concepts-prompt contradiction; the owner produced the decisive evidence by using the alternative tool. Neither was the engineering that built the thing.

## What survives

- **PR #233's prompt rule**, with its regression test. A bullet about a named thing carries the name. That is a genuine correctness property of the reading surface and it was measured.
- **The harness** (`tests/evals/mindmap_entity_recall.py`), retained because issue #235's model A/B used it for a relative between-model comparison, which needs no threshold.
- **[`docs/answering-questions.md`](../../answering-questions.md)**, the routing guide this produced: which questions go to YouTube Ask, which to this corpus, and what the corpus cannot do.
- **No entity layer was built**, and that is the outcome, not a deferral.
