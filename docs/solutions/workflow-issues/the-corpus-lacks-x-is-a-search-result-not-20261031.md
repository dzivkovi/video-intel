---
title: "\"The corpus lacks X\" is a search result, not an inference: check absence with several passes, verify every cited id, cite the primary source"
date: 2026-10-31
category: workflow-issues
module: corpus-question-answering
problem_type: workflow_issue
component: development_workflow
severity: medium
applies_when:
  - A briefing, brief or answer is about to claim that a topic is absent or thin in the corpus
  - A question is an inventory (everything the corpus says about a term) rather than a lookup
  - A synthesis cites video ids or `&t=` deep links that have not been opened
  - Two sources disagree and one of them is a creator retelling a primary artifact
related_components:
  - documentation
  - tooling
tags: [search, nugget, briefings, absence-claims, citation-verification, primary-sources, verification-discipline]
---

# "The corpus lacks X" is a search result, not an inference

## Context

A briefing claimed a topic was nearly absent from the corpus, and one hybrid search disproved it. A `nugget` brief reported its own gap ("the corpus does not contain..."), the gap turned out to be a retrieval miss, and the persisted brief had to be deleted by hand. Both mistakes read an absence off a single read surface without asking what that surface can and cannot see.

## Guidance

Each read surface has a structural blind spot. Know it before trusting a "nothing here":

- `briefings --unseen` ranks against an interest model that, when no `profile.yaml` is persisted, is inferred from the configured channel list and the most recurring `taxonomy.json` concepts (`_infer_profile`). It amplifies interests the corpus already has; it is not a map of what is missing.
- `nugget` synthesizes from top-K hybrid retrieval (`--limit`, default 15 excerpts). Its "the corpus does not contain" describes the retrieved pool, not the corpus. It saves to the `_briefings/nuggets/` folder under the corpus output_dir (not in this repo) by default, so an off-target brief becomes a persisted artifact; pass `--no-save` while retrieval quality is unknown.
- The corpus only knows the watchlist. Corpus consensus means "this watchlist says", not "the field says" (`docs/answering-questions.md`: it cannot see a video you never ingested).

For an inventory question, run several passes and record which pass found each hit:

1. `search --vector` (hybrid BM25 + vector + RRF). In one run it drifted to the wrong sense of a coined phrase and ranked passing mentions above substantive coverage.
2. Concept search. The taxonomy holds ideas, not names, so a term with no concept returns nothing; an empty concept result proves nothing about the transcripts.
3. A literal transcript-text pass for the exact term. This is the case the search skill allows direct file search for: an exact phrase known to appear. It is never the tool for verifying a paraphrase, which stays on `search --vector`.

Filter hits to on-topic titles before summing across queries. Hybrid search has no relevance floor (`--min-relevance` defaults to 0.0; RRF scores are rank-derived, not calibrated), so it fills its `--limit` whether or not the topic is present.

Before publishing, verify citations:

- "Represented by title only" is not an acceptable caveat when the transcript is on disk. Read it.
- Open every cited video id and deep link and check it resolves to the video the sentence describes. One correction pass found four fabricated ids; another found a citation pointing at the wrong video.
- A creator's retelling of a primary artifact is one source, not corroboration, and it can drop columns or recast meanings. Resolve conflicts toward the most primary source: official docs over press, a session transcript over a paraphrase of it.

## Why This Matters

Nobody goes looking for what a document says is not there, so a false absence claim survives and steers what gets curated next. A fabricated id breaks the provenance that is the product (`docs/answering-questions.md`: "Provenance is the product; verification is yours"), and a retelling counted as a second source repeats the "popularity is not corroboration" mistake (`docs/reading-layer.md`, `.claude/rules/briefings.md`).

## When to Apply

- Any sentence of the form "the corpus has little or nothing on X".
- Any brief or briefing about to be saved or shared that cites ids or timestamps.
- Any synthesis that leans on a creator's summary of a paper, doc, spec or talk.

## Examples

Before: one `search --vector` call, ten hits, half off-topic, summed with a second query's hits; the brief says the topic is "barely covered" and cites a video by title only.

After: three passes, each hit tagged with its pass, off-topic titles dropped before counting, every cited transcript read and every `&t=` link opened, retellings checked against the artifact they retell. `nugget --no-save` until the pool looks right; `nugget --topic <slug>` when the question belongs to a curated thread.

## Related

- `skills/video-intel-search/SKILL.md` (direct file search only for a known exact phrase; `nugget --no-save`)
- `docs/search-internals.md` (`--limit`, `--min-relevance`, RRF not calibrated)
- `docs/answering-questions.md` ("What this tool cannot do")
- `docs/reading-layer.md` ("Popularity is not corroboration")
- `.claude/rules/search-index.md` (issue #188, `nugget --topic`)
- `CLAUDE.md` ("Paraphrase verification uses `search --vector`, not `Grep`")
