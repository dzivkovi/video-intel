# Canonical `plugin-dev:skill-reviewer` rerun — 2026-04-25 post-Unit-3

**Why this file exists.** The autonomous chain agent that shipped this PR
could not invoke `plugin-dev:skill-reviewer` from its subagent context
(deferred-tool registry mismatch — the autonomous chain documented this
limitation in `skill-reviewer-rerun.md` alongside this file). The main
session DID have access to the agent and ran it directly to fill the
canonical-evidence gap that R2 requires.

**Inputs reviewed.**

- `skills/video-intel-search/SKILL.md` (post-Unit-1 state — 6 new
  verification triggers in frontmatter, anti-grep callout in body, new
  routing-table row)
- `skills/video-intel/SKILL.md` (post-Unit-2 state — "Wrong skill"
  pointer row updated)
- Branch: `feat/skill-factcheck-triggers` at commit `3b3f314`

**Scenario re-rated:** *"Verify whether Nate B. Jones really said 'zero
human keystrokes' in his videos."*

## Verdict: **4.5 / 5** (baseline was 1.5/5, +3.0 absolute)

**R2 ("rerun rates higher than 1.5/5 baseline") — SATISFIED with margin.**

## Side-by-side subdimension breakdown

| Subdimension | Baseline | After | Evidence |
|---|---|---|---|
| Trigger-phrase coverage | 1/5 | 5/5 | 6 verification phrases in `video-intel-search` frontmatter (lines 23-26). The user prompt verb "verify" matches phrase 1 nearly verbatim. |
| Routing-table fit | 1/5 | 5/5 | New row in Mode reference table (line 116) explicitly maps verification intents to `search "<key noun phrase>" --vector --channel C`, with escalation to `nugget`. Bolded routing logic ("Paraphrase verification is a semantic question, not a keyword question") closes the gap. |
| Anti-pattern guidance (anti-grep) | 0/5 | 5/5 | New blockquote callout immediately after `## How to Use` (lines 86-91) tells the agent in plain language not to grep `output_dir` for paraphrases and explains *why* (vocabulary mismatch). |
| Vocabulary-mismatch awareness | 1/5 | 4/5 | The routing-table row says "speaker's vocabulary likely differs from the paraphrase" and prescribes "Try 2-3 noun-phrase variants if the first returns nothing." Strong, but could be slightly more explicit that the *first* attempted query should strip stopwords/quotes. Minor. |
| Cross-skill bounce (curate -> search) | 2/5 | 5/5 | `video-intel/SKILL.md` line 121 now has `"verify quote"` and `"fact-check claim against [creator]"` in the "Wrong skill" row, so an agent that lands on curate first gets bounced. |

## First-action prediction

A fresh agent receiving the verify prompt now has three independent signals
pointing at `video-intel-search`: frontmatter trigger phrases, the bolded
routing-table row, and the anti-grep callout. Channel-name resolution
("Nate B. Jones" -> `natebjones`) is unchanged but covered by the existing
channel resolution logic in curate. First action will almost certainly be:

```bash
python .../video_intel.py search "zero human keystrokes" --vector --channel natebjones
```

That is the prescribed path — **R1 contract met under the rubric**.

## False-positive risk assessment on new triggers

Reviewed each new phrase against unrelated query shapes:

- `"verify whether [creator] said [paraphrase]"` — narrow; needs both
  creator and quote-shaped object. **Low FP risk.**
- `"fact-check this quote against [creator]'s videos"` — explicit
  "creator's videos" anchor. **Very low FP risk.**
- `"did [creator] really say [X]"` — could marginally fire on rhetorical
  questions, but the action (running `search --vector`) is the right move
  regardless. **Acceptable.**
- `"is this [creator] quote real"` — narrow shape. **Low FP risk.**
- `"find the source for this [creator] claim"` — could conflict with
  curate-flavored "find the source video" intent. **Low risk** because
  curate does not surface a "find source" verb.
- `"check the corpus for the quote [paraphrase]"` — explicit "the corpus"
  anchor. **Very low FP risk.**

No bleed into curate-shaped intents (scan / transcribe / process). No
bleed into `translate-bcs`.

## Pre-merge flags for Daniel (the half-point I held back)

1. The routing-table row tells the agent to try "2-3 noun-phrase variants"
   but does not show an example of the *transformation* (full quote ->
   key noun phrase). An agent might still pass the full paraphrase
   verbatim on attempt 1. Not a blocker — semantic search tolerates
   verbose queries — but a one-line example like `search "zero human
   keystrokes" --vector` (not `search "did he really say zero human
   keystrokes" --vector`) would make it concrete. Optional follow-up
   commit, not merge-blocking.
2. Mutex test coverage assertion: 59 tests passing locally
   (`tests/test_skill_descriptions.py` post-Unit-1+Unit-2). Agent-side
   review did not run pytest; that's verified separately by the
   autonomous chain's test report.
3. No regressions spotted in either SKILL.md's curate-side or search-side
   routing for non-verification queries — frontmatter additions are
   additive, routing-table rows are insertions not edits.

## Why two evidence files exist

- `skill-reviewer-rerun.md` — the **manual structural validation** the
  autonomous chain agent captured because it could not invoke
  `plugin-dev:skill-reviewer` from subagent context. That file is also
  honest about its limitation (manual rubric estimate was ~4.5/5).
- `skill-reviewer-rerun-canonical.md` (this file) — the actual
  `plugin-dev:skill-reviewer` agent invocation result. **4.5/5
  confirmed.**

The two agree, which is good signal that the manual estimate was
calibrated. The canonical run is the one R2 actually requires.

---

Reviewed: 2026-04-25 ~21:55 EDT
Agent: `plugin-dev:skill-reviewer` (canonical)
Caller: main Claude Code session, post-autonomous-chain validation pass
