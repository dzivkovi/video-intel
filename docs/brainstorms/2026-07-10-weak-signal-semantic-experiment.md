# Research: Semantic Weak-Signal Detection (automating "same idea, different words")

**Date:** 2026-07-10
**Status:** the semantic-pairing experiment (came first, 07-10). Its result (synonym soup) was folded into, and superseded by, the consolidated findings: [2026-07-11-weak-signal-findings.md](2026-07-11-weak-signal-findings.md).
**Origin:** Daniel repeatedly finds convergences by feel (chatting with Fable, running manual vocabulary set-intersections), e.g. the "headless / server-side agents" theme where vendor and practitioner communities describe the same thing with non-overlapping words (`work/2026-07-10/05-...`). This asks: is there an algorithmic way to arrive at that WITHOUT him pre-naming the vocabularies, so the pain of manual inference does not recur?

## The core reframe (why the graph was dull, and what replaces it)

The intel-graph (issue #85 / PR #86) clustered terms by **co-occurrence** - which words appear in the same video. That measures "discussed together," a weak, popularity-biased proxy. Betweenness/PageRank on it collapse into "most popular topic" (proven twice; a blind bridge probe on 2026-07-10 could not even rank Daniel's hand-found Rosetta-stone video). Community detection on co-occurrence picks the corpus's dominant cut, not the vendor-vs-practitioner fault line Daniel cares about.

What Daniel actually does is infer **meaning**, not word overlap. The tool that encodes meaning is **embeddings** (already in this project via Voyage / LanceDB). Two phrases that share no words but mean the same thing sit close in embedding space. So the substrate changes from a graph-of-words to **meaning-space**, and the axis of the analysis becomes exactly the axis Daniel cares about ("same thing?"), instead of a dominant-but-irrelevant one.

## The signal, defined precisely

A weak signal = a pair (or cluster) of terms that is:

1. **High meaning-similarity** (cosine of embeddings above threshold) - they mean the same thing.
2. **Low lexical overlap** (share no significant word) - they are *different words*, not trivial synonyms.
3. **Low co-occurrence** (rarely or never in the same video) - the two ideas are not yet being discussed together.
4. **Low creator overlap** (used by largely non-overlapping creators/channels) - *different tribes*.

Conditions 1+2 = "same idea, different words." Conditions 3+4 = "separate worlds that have not converged yet." Together = a convergence in progress, before it is obvious. That is the definition of a weak signal, and it is the structural opposite of what the co-occurrence graph surfaced (popularity / what is already obvious).

## Why this is different from, and better than, the dull graph

| | Co-occurrence graph (dull) | Semantic detector (this) |
|---|---|---|
| Similarity by | words appearing together | meaning (embeddings) |
| Surfaces | popular / central / obvious topics | non-obvious convergences (by construction) |
| Aligned with Daniel's question? | no (picks dominant cut) | yes (the cut IS meaning) |
| Needs him to pre-seed vocab? | yes (must inject the theme) | no (embeddings find meaning automatically) |

The last row is the payoff: it can surface convergences he has NOT already felt, which is the only thing worth building. If it only re-finds what he already knows, it is not worth building (see kill criterion).

## Ingredients (all already exist)

- `intel.duckdb` (`C:\Users\danie\.cache\video-intel\intel.duckdb`): `entities` (terms), `mentions` -> `segments` -> `artifacts` -> `sources` (creator per term), `co_occurs` (co-occurrence).
- Voyage embeddings (`VOYAGE_API_KEY` set; `voyage-4-lite`). Bare-term embedding for v0; chunk-centroid embedding (average of the LanceDB chunk vectors where the term is mentioned) for v1 if bare terms prove too noisy.

## The experiment (v0, scoped)

`scratchpad/semantic_weak_signal.py`: top ~400 terms by PageRank -> embed -> for every pair, keep those with sim >= 0.55, no shared word, co-occurrence <= 1, creator-overlap (Jaccard) < 0.34. Rank by similarity. Read the top ~20 and judge: are these real "same idea, different words, different tribes" pairs?

## Go / kill criterion (the anti-overfit guard, per Codex 2026-07-10)

This is the whole point - it must not be a mirror that confirms hunches. Adopt the guard verbatim:

1. **Pre-register** the method (thresholds, scoring) BEFORE looking at output.
2. **Freeze** the rule; run on a **held-out slice** (e.g. only videos published after a cutoff, or a channel held out of the seed).
3. **Beat baselines:** the top pairs must be more meaningful than (a) popularity, (b) random matched pairs, (c) pure keyword-union search.
4. **GO** only if it surfaces a defensible NEW convergence Daniel did not pre-name (not just re-finding "headless agents"). **KILL** if it only rediscovers known themes or returns synonym noise.

## What success buys Daniel

A repeatable detector that, on each corpus refresh, hands him a short list of "two tribes are converging on X, calling it A and B, and have not connected yet." That is his consulting arbitrage (the vendor-to-practitioner translation) produced on a schedule instead of through the pain of manual inference. If v0 fails the guard, the honest answer is that his felt-inference is not yet reliably automatable and he should keep doing it by hand, seeded.

## v0 RESULT (2026-07-10, same night): KILL as designed

Ran it. 57,933 candidate pairs - and the top ones are **synonym soup**, not weak signals: "automation workflow" <-> "autonomous workflows" (0.95), "workflow automation" <-> "custom workflows", "business strategy" <-> "strategic planning", "access control" <-> "permission settings". These are morphological variants and obvious synonyms, not two tribes converging on a hidden idea. The "no shared token" filter is too weak (misses plurals/stems), and at the top-term level creator-overlap is naturally low so that filter barely bites.

**Verdict: naive unsupervised semantic term-pairing is a SYNONYM detector, not a weak-signal detector.** This is now the THIRD independent confirmation that fully-unsupervised methods over this corpus return the obvious: (1) co-occurrence community detection -> popularity clusters; (2) betweenness -> popular hubs; (3) semantic pairing -> synonyms. "Find me something surprising" with zero hint of what surprises you is ill-posed - every unsupervised method returns its own flavor of "the obvious."

## Revised direction (v1): seeded, tribe-level, embedding-judged

The signal Daniel wants is not term-pair synonymy. It is **dialect-cluster convergence**: two coherent *vocabularies* (each used by a distinct tribe of creators) that are semantically the same thing but socially separate. That is his vendor-vs-practitioner hemisphere finding, and it is cluster-level and tribe-aware, not term-pair-level.

The realistic tool = his `weak_signal_overlap.py` set-intersection, upgraded on two axes:
1. **Tribe-aware:** the "sides" are creator communities or seeded dialects, not arbitrary term pairs. Build a per-creator (or per-seed) vocabulary; compare vocabularies, not terms.
2. **Embedding-judged "same meaning":** instead of Daniel hand-picking every synonym, embeddings decide whether two dialects mean the same thing - so he supplies the *hunch* (which two tribes to compare) but not the tedious synonym-matching.

This automates the GRIND (reading, comparing, verifying that A and B are the same idea, finding the bridge content) but NOT the SPARK (which two tribes to suspect). That split is the honest ceiling: no method surfaced a NEW convergence unsupervised, so the hunch stays human.

## The decision this doc exists to force

Build v1 ONLY if the bottleneck is the grind (the hours of reading and comparing after the hunch). If the bottleneck is the spark itself (having the hunch), no tool helps - keep doing it by hand, seeded. Daniel decides GO/KILL fresh. No GitHub issue yet; this doc is the start-from artifact.
