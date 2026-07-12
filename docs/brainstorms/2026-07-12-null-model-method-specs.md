# Null-Model Method Specs: SDSM Creator Network + PMI/Disparity Hygiene

**Date:** 2026-07-12
**Status:** method specs, DRAFTED NOT BUILT (issue #95 guardrail: no framework before a cited finding needs it). Decision record for the two child issues filed under #95.
**Lineage:** [#95](https://github.com/dzivkovi/video-intel/issues/95) (parent roadmap) -> methods (2) and (3). Grounded in the ACTUAL results of [#93](https://github.com/dzivkovi/video-intel/issues/93) / PR #96 (the lead-lag report) and Probe C of [2026-07-11-weak-signal-findings.md](2026-07-11-weak-signal-findings.md).

---

## What #93's result tells us (the evidence these specs are drafted FROM)

The coverage-corrected lead-lag report (docs/reports/2026-07-12-lead-lag-report.md on the #96 branch) came back with a real, Codex-confirmed signal:

- The kill criterion did NOT fire: engineerprompt (largest, oldest-indexed: 226 artifacts, indexed from 2024-10) drops from naive rank 2 to corrected rank 8 of 9, lift 0.26. Spearman(lift, corpus size) = -0.50; Spearman(lift, coverage start) = -0.18.
- The robust row is seankochel: lift 1.79 from 33.0 observed firsts vs 18.5 volume-expected over 47 eligible concepts.
- The NOISY part: the top-of-table lift (lennyspodcast, 9.57) rides on 5.0 observed vs 0.5 expected over only 9 eligible concepts. Rate normalization alone cannot say whether that is signal or two lucky calls. This is the concrete trigger for the null-model work below - noise at the small-sample tail, NOT popularity contamination (the -0.50 corpus-size correlation rules that out).

Separately, Probe C (2026-07-11 findings) ran a plain hypergeometric null on the bipartite creator-concept matrix: 272 of 351 creator pairs came out "significant" - the null is too weak for a topically homogeneous corpus where everyone talks AI-coding. That is the concrete trigger for SDSM specifically (fix the null, not the projection).

---

## Spec A - SDSM validated creator-creator network (method 2, GOLD)

**Question it answers:** which creator pairs share significantly more concepts than chance, GIVEN both how prolific each creator is and how popular each concept is - the validated actor network the SNA toolkit needs.

**Why the plain hypergeometric null failed (Probe C):** it conditions only on row sums (creator degree). In a topically homogeneous corpus, concept popularity does the sharing for you: everyone covers "ai agents", so everyone "significantly" overlaps. 272/351 significant pairs is a null-model artifact, not 272 real ties.

**Method (Neal 2021, the `backbone` package's SDSM):**
1. Build the bipartite 0/1 matrix B: creators x concepts, B[i,j] = 1 iff creator i has >= 1 `has_concept` row for concept j (reuse the exact adopter definition from `lead_lag_report.load_first_mentions`).
2. Fit the Stochastic Degree Sequence Model: estimate p_ij = P(B[i,j] = 1) conditioning on BOTH row sums (creator prolificness) and column sums (concept popularity) - logistic regression of B[i,j] on row-sum + column-sum is the standard estimator (Neal's "scobit/logit" variant is fine at this size).
3. For each creator pair (i,k), the shared-concept count under the null is a Poisson-binomial over j of p_ij * p_kj. Compute the upper tail P(shared >= observed); two-sided if we also want avoidance ties (Probe C found zero - expect the same).
4. FDR-correct (Benjamini-Hochberg) across all pairs; keep edges with q < 0.05.
5. Output: an edge list `docs/reports/<date>-sdsm-creator-network.md` (and optionally a JSON for the #94 renderer) with observed shared, expected shared, multiple, q-value - the same table shape as Probe C so results are directly comparable.

**Acceptance gate (pass/fail, decided BEFORE running):** the plain hypergeometric null said 272/351 pairs are significant. SDSM passes if it prunes that to a set small enough to eyeball (< ~40 edges) AND the surviving top ties still look like the Probe C sensible ones (mark_kashef-ramjad 4.9x, benai92-simonscrapes 6.0x territory). If SDSM still returns > half of all pairs significant, the corpus is too homogeneous for pairwise validation - report honestly and stop (same discipline as #93's kill criterion).

**Scope guard:** one script (`scripts/sdsm_network.py` or a subcommand judgment call at implementation time), read-only against intel.duckdb, stdlib + numpy at most. Poisson-binomial via normal approximation is acceptable at 23 rankable creators x 551 concepts; do not pull in a stats framework for this.

### A.2 - The lead-lag null-model correction (DRAFT, triggered by the noisy top row)

The #93 ranking's small-sample tail needs a significance column, not a redesign:

- **Per-creator permutation test:** hold the eligible-concept structure fixed; under the null, for each concept the "first" slot goes to creator i with probability rate_i / sum(rates of that concept's rankable eligible adopters) - exactly the expectation model the lift already uses. Simulate N=10,000 draws of each creator's total firsts; p = P(sim_firsts >= observed). This directly answers "is lennyspodcast's 5.0 observed vs 0.5 expected luck?" (analytically: a Poisson-binomial tail, so the closed form is also fine - the permutation framing is just easier to audit).
- **Output change (when built):** one extra column in the corrected ranking table (`p (perm)`), plus a caveat line stating the number of creators clearing p < 0.05 after BH correction. NO changes to lift math, eligibility, or report structure - the #96 review guardrails (naive-vs-corrected tables, Spearman diagnostics) stay untouched.
- **Prediction to falsify:** seankochel's 33 vs 18.5 over 47 concepts should clear significance; lennyspodcast's 5 vs 0.5 over 9 concepts is genuinely uncertain - that uncertainty is the point of adding the column.

---

## Spec B - PMI weighting + disparity-filter backbone (method 3, GOLD prerequisite - no current trigger)

**Question it answers:** which co-occurrence edges are informative once popularity is removed by construction - the prerequisite hygiene before ANY future co-occurrence-graph analysis (community detection retrial, temporal link prediction per method 5).

**Trigger status, stated honestly:** #93's lead-lag result did NOT trigger this - lead-lag never touches the co-occurrence graph. Probe A already ran a PMI + significance floor + backbone cap and the betweenness kill survived (11/15 of top betweenness still the popularity top-15). The Serrano disparity filter is the one strictly stronger standard not yet tried - the "honest second chance" the 2026-07-11 findings doc reserved before final judgment on terms-as-nodes analytics. File it, prioritize it LOW, build it only when a co-occurrence-consuming method (5) or the retrial is actually scheduled.

**Method:**
1. Recompute edge weights as positive PMI over `co_occurs` (already a DuckDB table built at load time, no Neo4j): pmi(a,b) = log( p(a,b) / (p(a)p(b)) ), keep pmi > 0.
2. Serrano-Boguna-Vespignani disparity filter (PNAS 2009): for each node of degree k, an edge with normalized weight w survives if its disparity p-value alpha_ij = (1-w)^(k-1) < alpha (0.05); keep the union of edges surviving from either endpoint.
3. Re-run the two dead-end diagnostics ON the backbone: Leiden communities and betweenness top-15 vs weighted-degree top-15 overlap. The 2026-07-11 doc's kill stands if overlap stays >= ~10/15.
4. Output: backbone edge list + a one-page verdict note appended to the findings doc lineage.

**Acceptance gate:** this is a hygiene method, so the gate is diagnostic, not product: report the overlap number. Overlap dropping below ~7/15 would reopen terms-as-nodes analytics (falsifies the standing kill); overlap staying high closes the question permanently with the strictest standard on record.

**Scope guard:** pure Python over the existing `co_occurs` table; no Neo4j, no GDS, no framework. Do not build until a consumer exists.

---

## Sources

SDSM: Neal 2021 (https://doi.org/10.1038/s41598-021-03238-3); Tumminello et al. PLOS One 2011 (https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0017994). Disparity filter: Serrano-Boguna-Vespignani PNAS 2009 (https://pubmed.ncbi.nlm.nih.gov/19357301/); PLOS One 2025 backbone comparison (https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0316141). Lead-lag correction context: "Precursors and Laggards", arXiv:1009.0119.
