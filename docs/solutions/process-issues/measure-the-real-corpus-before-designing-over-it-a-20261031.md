---
title: "Measure the real corpus before designing over it: a meta field is a claim, and a threshold needs its would-flag count, margins and denominator"
date: 2026-10-31
category: process-issues
module: corpus-measurement
problem_type: best_practice
component: development_workflow
severity: medium
applies_when:
  - A plan, sweep or remediation is keyed on a meta.json field
  - A field is backfilled by inference on metas written before it existed
  - A contract or parser is written over hand-authored files such as briefing front-matter
  - A browsing default, detector or threshold is about to ship
symptoms:
  - A remediation plan keyed on quality fields that no meta carried yet
  - A topic-first default view that would have put about 71% of videos under "Unthreaded"
  - "\"0 currently severe\" offered as proof that a new assessor changes nothing"
root_cause: inadequate_documentation
related_components: [scripts/corpus_walk.py, scripts/atlas_build.py, scripts/video_intel.py, docs/meta-json-schema.md, docs/atlas.md]
tags: [measurement, corpus-survey, meta-json, thresholds, backfill, defaults]
---

# Measure the real corpus before designing over it

## Context

Several designs in this repo started from what `meta.json` said rather than from what was on disk. A meta field is a claim made by the writers that ran on that video. It is not a fact about what exists. A field added last month is absent on every video processed before it, and a field one writer forgot is absent on every video that writer touched. Designs built on those fields looked complete and measured nothing.

## Guidance

**Presence comes from a directory listing, not a field.** `modes_completed` named `mindmap` on only a small share of the metas whose `.mindmap.md` was sitting next to them. Ask the disk, matched with the writer's own naming: `corpus_walk.channel_sibling_index` lists a channel folder once and matches the four known suffixes literally, and `dedupe` verifies a `modes_completed` claim against disk with the writer's `_MODE_ARTIFACT_PATTERNS` before trusting it (issue #159).

**A sweep over older artifacts recomputes from disk.** Older metas carry no quality flags, because only new writes run the assessor. The timestamp-overrun sweep in `docs/troubleshooting.md` likewise finds only videos processed after `transcript_timestamp_overrun_entries` existed (issue #139). `assess-transcripts` (issue #269) closes that gap by reading `.transcript.md` and computing the same six fields the writers persist.

**An empty sweep is "not assessed", never "clean".** The catalog renders a missing quality verdict as "not assessed" (`scripts/atlas_catalog_template.html`, `docs/troubleshooting.md`). "Zero currently severe" over a population nobody evaluated is not evidence that a new assessor changes nothing.

**Validate an inference rule before backfilling with it.** Older metas carry no `transcript_source`. If a missing field is filled by inference (a `SCREEN` block or a speaker-attributed line implies Gemini, a captions header implies YouTube captions), first run the rule on every meta that already records the field and require it to match all of them. Until then the catalog reads only the stored value and shows `null` otherwise.

**Survey hand-authored files before writing a contract over them.** An amended spec's front-matter rule covered only a minority of briefings. Unquoted YAML turned `yes` into a boolean, which `str()` would have turned into a phantom member; topic derivation now keeps only genuine strings as video ids.

**Choose defaults by measured coverage.** Curated topics covered a minority of videos and one taxonomy domain held nearly all of them, so neither partitions the corpus. Only channel partitions every video, so channel is the safe browsing axis and topic stays a filter.

**Before a detector or threshold ships, report three numbers:** how many items it would flag, how close the near-misses sit, and how many items the measurement could evaluate at all. For the captions leading-gap rule the near-misses were 576 and 558 seconds against a 600-second line (#182 invariant 7). In the #157 labeling survey, 5 of 26 blind-gap files had no known duration, so a duration-based check could not judge the rest, and that denominator belongs in the report.

**Keep the decision tied to its measurement.** The worst lost tails and the longest healthy outro could not be separated by any threshold, so a trailing gap stays mild (#157 invariant 1). Writing the measurement next to the rule means new data reopens it, not a new argument. Save any forensic count that drives a plan next to the plan.

## Why This Matters

Each failure above produced a plan that was internally consistent and wrong about the corpus. A threshold with unmeasured margins false-alarms on its first near-miss.

## When to Apply

- Before any sweep, plan or report that counts a meta field
- Before backfilling a field on older metas
- Before writing a parser or contract over operator-authored files
- Before shipping a detector, threshold or browsing default

## Examples

- Before: "No metas are severe, so the new assessor is safe to enable." After: "No metas were assessed; run `assess-transcripts` in dry-run mode and report clean, mild and severe counts."
- Before: a default view grouped by topic. After: a coverage count per candidate partition, then the one that covers every video.

## Related

- `.claude/rules/transcript.md`: #139, #157 invariant 1, #182 invariant 7, #269
- `.claude/rules/scan-config.md`: #159 disk-verified `modes_completed`
- `.claude/rules/intelligence-layer.md`: `atlas_build` invariant 1
- `.claude/rules/search-index.md`: topics invariants
- `docs/meta-json-schema.md`, `scripts/atlas_catalog_template.html`, `docs/troubleshooting.md`
- `docs/plans/2026-08-29-001-fix-transcript-quality-guards-plan.md` (why a trailing gap cannot be separated by any threshold)
