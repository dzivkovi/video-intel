# Video-Intel as a Weak-Signal-Detection Playground

**Date:** 2026-07-10 / 2026-07-11
**Status:** research findings, consolidated and de-noised from the working chat. Decision-ready.
**Companion (public brainstorm):** [2026-07-10-weak-signal-semantic-experiment.md](2026-07-10-weak-signal-semantic-experiment.md) - the semantic-pairing experiment and its kill result. Local session notes (gitignored, not on GitHub): `work/2026-07-11/01` (empirical fact-check with the probe numbers), `work/2026-07-10/06` (the two-week story), `work/2026-07-10/07` (the seeded-detector resolution).

---

## TL;DR

1. **Unsupervised graph analytics on this corpus returns "the obvious," three ways.** Co-occurrence community detection -> popular topics; betweenness -> popular hubs (11-13 of top-15 identical to PageRank, even on a PMI-cleaned backbone); naive semantic pairing -> synonym soup. Proven, not asserted.
2. **Retire Neo4j + GDS.** Not as punishment - because none of the methods that *do* produce insight on a corpus like this ship in GDS, and GDS targets transactional-edge problems (fraud, recommendations) this data does not have.
3. **Keep LanceDB (hybrid search + embeddings) and keep the DuckDB truth store.** The next analytics layer is plain **DuckDB SQL + small Python stats + embeddings**. The DuckDB substrate (built by PR #86) already paid for itself: every probe below ran on it.
4. **The premise "no touch points between authors, so no graph value" is false.** The corpus contains two latent *actor* networks that were simply never computed: a **temporal lead-lag** network (who covers a concept first, who follows) and a **null-model-validated creator-creator** network (who shares far more concepts than chance). Both showed real structure on first contact. The crime-network instinct was right; it was pointed at the wrong edge type (terms-as-nodes instead of creators-as-nodes).

---

## Lineage and traceability

This brainstorm is the decision record behind a set of GitHub issues. Two-way trail for an open-source audit:

**Reshaped / retired by this analysis**

- [#85](https://github.com/dzivkovi/video-intel/issues/85) - the original "weak-signal / commonality-detection layer" spec. Its unsupervised-graph premise is retired; the DuckDB truth store it produced is kept.
- [PR #86](https://github.com/dzivkovi/video-intel/pull/86) - implements #85. Reframed to "DuckDB truth store (+ experimental Neo4j GDS lens, kept as research history)"; recommended action is reframe-and-merge.
- [#84](https://github.com/dzivkovi/video-intel/issues/84) - catch-up briefings; downstream consumer of the burst-detection method (#4 below).

**Spawned by this analysis (the forward direction)**

- [#95](https://github.com/dzivkovi/video-intel/issues/95) - DuckDB analytics roadmap (parent; the 6 methods, prioritized).
- [#93](https://github.com/dzivkovi/video-intel/issues/93) - coverage-corrected lead-lag report (P1; the first concrete task).
- [#94](https://github.com/dzivkovi/video-intel/issues/94) - non-graph visualizations research (P2).

---

## The corrected mental model (the paragraph to remember)

The co-occurrence **term** graph was the wrong graph, not the wrong instinct. Criminal-network SNA worked because edges were *actions between actors* (A called B, A met B). This corpus does contain actor edges - they were never computed: **time** (who moves first, who follows) and **statistically surprising overlap** (who shares more than chance). Both are creator-to-creator, directed or weighted, and both show structure. The pipeline that failed was terms-as-nodes; the pipeline that works is creators-as-nodes with derived, validated edges. GDS stays marginal either way - the value is DuckDB SQL + small Python stats on exactly the truth store PR #86 built.

---

## The empirical evidence (probes run overnight, all against `intel.duckdb`)

### Probe A - betweenness retrial on a fair (PMI-sparsified) graph
Objection handled: raw co-occurrence is popularity-dominated *by construction*, so the fair test rebuilds the graph with PMI weighting + a significance floor + a backbone cap (902 nodes / 4,968 edges from 99,577 raw). **Result: 11 of top-15 betweenness terms are still the top-15 popularity terms.** The exceptions (system architecture, design systems, memory files/layer) are generic, not hidden brokers. The collapse is a property of the corpus, not sloppy construction. Verdict: **the kill survives its appeal.** (Strictest standard, the Serrano disparity filter, is method #3 below and is the honest "second chance" before final judgment.)

> **Update 2026-07-13 (#99, the reserved second chance ran):** the Serrano disparity filter came back **INCONCLUSIVE (parked)**, so the kill neither hardened nor reversed. Two backbones (`scripts/disparity_backbone.py`, verdict note in `docs/reports/2026-07-13-disparity-backbone-diagnostic.md`): the *literal* PMI-weighted disparity is **degenerate** - 0 backbone edges at alpha=0.05, because PMI log-compresses each node's incident weights and the disparity filter needs the heterogeneity PMI just removed (the two hygiene steps work at cross-purposes here). The canonical *raw-weight* Serrano disparity gives betweenness-vs-weighted-degree overlap **9/15** - in the 7-9 inconclusive band (robust across alpha and unweighted betweenness; a weighted-betweenness variant touches 10). So terms-as-nodes analytics is neither confirmed dead at the strictest standard (would need >= 10) nor revived (would need <= 6). #99 stays OPEN for the owner's judgment.

### Probe B - temporal lead-lag (the analysis nobody had run)
For 175 concepts adopted by >= 4 creators: who mentioned it first, who followed, with dates. This produces **directed, dated edges** - the closest analog to calls-and-meetings this corpus supports. Real adoption chains exist, e.g.:

> `Remote AI Coding Sessions: ramjad(02-25) -> simonscrapes(02-27) -> mark_kashef(03-01) -> engineerprompt(03-18) -> colemedin(03-23)`

**Honest confound:** channels entered the corpus with very different lookback depths (engineerprompt from 2024-10, natebjones only from 2025-11), so deep-backfill channels *look* like leaders. The literature ("Precursors and Laggards", arXiv:1009.0119) fixes exactly this with posting-rate/coverage correction, and reports precursor scores that only weakly correlate with PageRank - i.e. it surfaces what centrality cannot. Verdict: **signal type is real; naive version is confounded; the corrected version is the single most promising next analysis.** Plain SQL + a correction step. No Neo4j.

### Probe C - validated creator-creator network (bipartite null model)
Instead of projecting co-occurrence naively, test each creator pair against a hypergeometric null ("do A and B share significantly more concepts than chance, given how prolific each is?") with FDR correction. On 27 creators x 543 concepts, sensible ties top the list:

| Creator A | Creator B | shared | expected | multiple |
|---|---|---|---|---|
| mark_kashef | ramjad | 53 | 10.9 | 4.9x |
| benai92 | simonscrapes | 36 | 6.0 | 6.0x |
| claude | engineerprompt | 64 | 18.5 | 3.5x |
| everyinc | thenextnewthingai | 78 | 23.3 | 3.3x |

**Honest confound:** 272 of 351 pairs came out "significant" - the plain hypergeometric null is too weak for a topically homogeneous corpus (everyone talks AI-coding). The literature's fix is the stricter degree-preserving SDSM null (Neal 2021, the `backbone` package). Zero "avoidance" pairs confirms the homogeneity. Verdict: **method works and surfaces the actor graph the SNA toolkit needs; needs the stricter null before numbers are quotable.**

### The catch-22 check (is the "DuckDB is the value" claim circular?)
Question raised: were the probes secretly consuming GDS results written back into DuckDB? Audited in code:
- `co_occurs` is a **DuckDB self-join at load time** (`intel_graph.py` ~line 517), explicitly "kept in DuckDB so verify runs without a live Neo4j." Not GDS.
- The **only** GDS write-backs are `entities.community_id` and `entities.pagerank` (~lines 573/633), which feed only the visualization artifacts.
- Probes B and C read only loader tables. Zero GDS.
- Probe A used GDS PageRank as its popularity baseline; recomputing that baseline as **pure SQL weighted degree** gives **13/15 identical** to GDS PageRank. Conclusion unchanged.
- Kicker: GDS PageRank being ~= a `GROUP BY` degree count is itself more evidence the lens adds little here, and both write-back columns are reproducible in-process (igraph, seconds at 7.4k nodes) if Neo4j is dropped entirely.

**No circularity. The forward path is genuinely Neo4j-free.**

---

## The method landscape: what actually produces "aha" on a creator/concept corpus

Sourced via EXA + web. Verdicts are for **this** corpus specifically.

| # | Method | Verdict | Why / needs Neo4j? |
|---|---|---|---|
| 1 | Temporal lead-lag / precursor-laggard scores | **GOLD** | Standard way to recover influence when there are no actor-actor edges; provably diverges from popularity. Plain SQL + correction. No Neo4j. |
| 2 | Bipartite creator-concept projection + null model (SDSM) | **GOLD** | Turns co-behavior into a *validated* actor network - back on home SNA turf. DuckDB + Python. No Neo4j. |
| 3 | Graph hygiene: PMI weighting + disparity-filter backbone | **GOLD (prerequisite)** | Raw co-occurrence is popularity-dominated by construction; the backbone deletes hub-hub edges so betweenness reroutes through real bridges. Python. No Neo4j. |
| 4 | Kleinberg burst detection over the timestamped mention stream | **GOLD-cheap** | Emerging-topic entry points straight off DuckDB provenance; near-free. No Neo4j. |
| 5 | Temporal link prediction as emerging-trend detection | **PROMISING** | Real literature (Krenn et al., Nature MI 2023), but built for corpora ~100x larger; run after #3. Adamic-Adar in SQL. No Neo4j. |
| 6 | Science-of-science (main-path / co-citation) | **MIXED** | Main-path/co-citation need citation links this corpus lacks (DEAD-END raw); but "Information Genealogy" (text+time) and burst+co-word maps transfer. No Neo4j. |
| - | Betweenness / PageRank / Leiden on raw co-occurrence | **DEAD-END (proven 3x)** | Returns popularity in three flavors. |
| - | Neo4j GDS as the analytics engine | **MARGINAL** | None of #1-6 ship in GDS; GDS targets transactional-edge problems. Neo4j's own docs route text corpora to GraphRAG, not GDS. |

### Top 3 by aha-per-effort
1. **Null-model hygiene (#3) + validated creator network (#4/bipartite):** disparity-filter/SDSM-validate both projections, then re-run community/betweenness. Every prior "obvious" result gets a fair second chance, and the validated creator network is the actor graph the SNA toolkit needs.
2. **Coverage-corrected precursor/laggard scores (#1):** a "who actually leads the AI-coding conversation" ranking that provably diverges from subscriber counts. Shareable, and unique to your timestamped data.
3. **Kleinberg burst detection (#4) now + temporal link prediction (#5) later:** bursts give "what is spiking this month" immediately; link prediction upgrades it to "which concepts converge next."

**One honest caveat throughout:** this corpus is ~2 orders of magnitude smaller than the cited studies, so treat statistical outputs as **leads for manual inspection, not verdicts** - which suits a single-user intelligence tool perfectly.

---

## PR #86 disposition

Facts: #86 is the only open PR; +1,954 / -0; isolated (nothing imports `intel_graph.py`); 988 tests green; one 1,107-line standalone script with a `load` command (builds the DuckDB truth store = the keeper) plus `project`/`verify` (the Neo4j GDS lens = the retire).

- **Codex verdict:** "Do not merge as-is. Lowest-regret: split it - merge/cherry-pick the DuckDB truth store + provenance schema + transactional load + tests; demote Neo4j/GDS to an experimental script. Do not carry Neo4j as default runtime debt. The next valuable layer is seeded, provenance-backed temporal and stance analysis on top of DuckDB." (Codex CLI, thread 019f4f55-c7c6-71c0-bbfa-34cfca8c8f1a)
- **My recommendation (same substance, cheaper mechanics for a solo maintainer):** **reframe-and-merge** - retitle to "DuckDB truth store (+ experimental graph lens)", add one paragraph marking the Neo4j subcommands experimental/optional, merge as-is. The graph code sits unused; nothing imports it; the substrate is preserved. Splitting a 2-commit additive PR is busywork. Close-and-cherry-pick is also fine. **The one option that destroys value is closing without salvaging the DuckDB truth store.**

---

## The seeded strand (why "just find me a hidden convergence" is ill-posed)

Separate from the actor-network methods above, the "same idea, different words" detector Daniel builds by feel was tested directly (semantic pairing, see [2026-07-10-weak-signal-semantic-experiment.md](2026-07-10-weak-signal-semantic-experiment.md)) and returned **synonym soup** - the third unsupervised dead end. Conclusion, confirmed by Codex: the honest detector is **seeded / semi-supervised**. The human supplies the hypothesis (which two tribes/dialects to compare); the machine verifies "same meaning?" via embeddings, confirms "different tribes?" via provenance, and surfaces the bridge. **The spark is not automatable - and that is the moat. The grind (verify, quantify, find the Rosetta-stone video, keep it current) is.** Anti-overfit guard (Codex): pre-register seeds, freeze the rule, evaluate on a held-out slice, beat baselines (popularity, keyword-union, semantic similarity); success = it surfaces a convergence you did NOT pre-name.

---

## Sources

Lead-lag / diffusion: Shi/Nallapati/Leskovec/Jurafsky "Who Leads Whom" (https://web.stanford.edu/~jurafsky/grants_v_papers.pdf); LeadLag LDA, ICWSM 2011 (https://ojs.aaai.org/index.php/ICWSM/article/view/14147); "Precursors and Laggards", arXiv:1009.0119 (https://ar5iv.labs.arxiv.org/html/1009.0119); BERTopic lead framework (https://doi.org/10.1002/widm.1561).
Link prediction / trend: Behrouzi et al. J. Informetrics 2020 (https://ideas.repec.org/a/eee/infome/v14y2020i4s1751157720300456.html); Huang et al. TFSC 2021 (https://www.sciencedirect.com/science/article/abs/pii/S0040162521003760); TermBall (https://doi.org/10.1109/access.2020.3000948); Krenn et al. Nature MI 2023 (https://www.nature.com/articles/s42256-023-00735-0).
Graph hygiene: Damani ACL 2013 (https://aclanthology.org/W13-3503.pdf); Serrano-Boguna-Vespignani disparity filter, PNAS 2009 (https://pubmed.ncbi.nlm.nih.gov/19357301/); PLOS One 2025 backbone comparison (https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0316141); NOLTA co-occurrence backbone (https://www.jstage.jst.go.jp/article/nolta/16/3/16_704/_article/-char/en).
Bipartite null models: Tumminello et al. PLOS One 2011 (https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0017994); Neal SDSM 2021 (https://doi.org/10.1038/s41598-021-03238-3, https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0244363).
Neo4j GDS fit: GDS getting-started (https://neo4j.com/docs/getting-started/gds/); fraud use case (https://neo4j.com/blog/developer/exploring-fraud-detection-neo4j-graph-data-science-summary/); GraphRAG for unstructured text (https://neo4j.com/developer/genai-ecosystem/importing-graph-from-unstructured-data/); "no native NLP embeddings in GDS" (https://github.com/neo4j/graph-data-science/issues/343).
Science-of-science: main-path review (https://doi.org/10.1108/gkmc-03-2024-0124); Information Genealogy, KDD 2007 (https://dl.acm.org/doi/10.1145/1281192.1281259); Kleinberg burst detection, KDD 2002 (https://www.cs.cornell.edu/home/kleinber/bhs.pdf); Mane & Borner PNAS 2004 (https://doi.org/10.1073/pnas.0307626100).
