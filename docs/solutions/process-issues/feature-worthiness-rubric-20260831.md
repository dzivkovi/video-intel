# Feature-worthiness rubric: when a proposed knob does not earn existence

**Date:** 2026-08-31
**Category:** process-issues
**Status:** decision record (append, never rewrite)

## The situation this covers

A one-off pain produces a proposed fix: a new config knob, a new flag value, a new subcommand, or a GitHub issue. The proposal can come from the operator, from a reviewer, or from an earlier turn of the same working session. The failure mode is agreeing by momentum: the pain was real, the fix is small, someone with standing asked for it. This document records the rubric that was used to reject one such proposal, so future sessions (including ones running on smaller models) can apply the same judgment instead of re-deriving it or caving.

## The worked example

Two off-topic episodes (sports, ancient history) from a newly added interview channel were scanned into a corpus built for AI-engineering content. They put 582 chunks into the 53,368-chunk vector index and roughly 18 concepts like "Combat Sports Psychology" and "Imperial Decline" into `taxonomy.json`, which also feeds the inferred interest model that ranks briefings. The proposed fix: extend `skip_modes` (values: `mindmap | transcript | concepts`) with a new `index` value so a video's artifacts can be kept on disk while excluded from the search index.

**Verdict: rejected, unfiled.** The channel got `enabled: false` (curate by URL instead of scanning the feed), which fixed the cause rather than the symptom.

## The rubric

1. **Map the proposed fix to the MEASURED harm, not the discussed harm.** The index knob targeted retrieval dilution, which measured at ~1% against a relevance-ranked index - negligible. The real measured harm was taxonomy and interest-model skew, and the existing `skip_modes: ["concepts"]` already covers that surface. A fix that misses the measured harm is rejected regardless of how cheap it is.
2. **Inventory existing primitives before adding one.** Every realistic case was already covered by composition: do not scan it (`enabled: false`, `skip_video_ids`), do not taxonomize it (`skip_modes: ["concepts"]`), do not keep it (delete the artifact), or scope at query time (`search --topic`, channel filters). A new primitive must serve a case the composition cannot.
3. **Count occurrences honestly.** The scenario had occurred exactly once, and the cause was fixed upstream in the same session. The project's durability ladder (CLAUDE.md, "Compounding operational recoveries") demands recurrence across sessions before a recovery becomes code.
4. **Apply the operator-knowledge test.** The ladder's decisive test: would a correct automated response require knowing something only the operator knows? "This video is off-topic" has no runtime signal - it is pure operator judgment. That caps the value of any automation and means a new manual lever must beat the existing manual levers on its own merits.
5. **Check semantic fit.** `skip_modes` means "pipeline stages that PRODUCE this video's artifacts". Index membership is CONSUMPTION of a corpus-wide derived artifact. Overloading a vocabulary with a value of a different kind taxes every future reader of every future diff that touches it.
6. **Price the repo-specific carrying cost.** In this repo a shipped feature is never just code: it carries a CLAUDE.md guardrail entry, a test contract, and reviewer grep instructions. That overhead is the point (it is what makes the guardrails trustworthy), and it means marginal knobs cost more here than in most codebases.
7. **Look for a contradicted premise in actual usage.** The corpus already contained deliberately kept off-topic videos (personal-interest podcast episodes) that ARE indexed on purpose, because the corpus is personal rather than thematically pure and the operator does search them. The proposal's implicit premise - off-topic content must be unsearchable - was contradicted by the operator's own curation history.
8. **Ship every rejection with a concrete revisit trigger.** This one: a video whose transcript the operator wants to keep AND read AND hide from search, occurring twice. Naming the trigger converts a "no" from obstinacy into a standing decision that future evidence can legitimately overturn.

## Delivery notes for agents applying this

- Lead with the verdict. Steelman the opposing side genuinely - the second look at this example produced two NEW arguments against filing (points 1 and 7) that the first look missed, which is what re-examination is for.
- When the operator asks the same question twice, re-derive from scratch; never flip solely because the question was repeated, and never defend a prior answer out of consistency.
- A rejection that names its revisit trigger is a completed deliverable, not a refusal.

## Related

- CLAUDE.md "Compounding operational recoveries (durability ladder)" - the code-vs-doc decisive test this rubric extends to knob-vs-nothing.
- Issue #173 - the counter-example from the same weekend: a bug found by a subagent, verified against the code by hand, filed with repro and root cause. Real defects with runtime signals clear the bar immediately; this rubric is for everything that does not.
