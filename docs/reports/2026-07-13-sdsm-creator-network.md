# SDSM-validated creator-creator network

Generated: 2026-07-13 | Issue #98 (method 2 of #95) | Substrate: DuckDB truth store (PR #86). Bipartite Configuration Model (max-entropy SDSM) null over the creator-concept adoption matrix.

Matrix: 31 creators x 549 concepts, 465 creator pairs. A cell is 1 iff the creator has >= 1 dated `has_concept` row for the concept (the lead-lag adopter definition).

## Acceptance-gate verdict (pre-registered, Spec A)

**PASS (clean).** SDSM prunes the plain hypergeometric null's 314/465 significant pairs to **10** edges at q<0.05 - below the 40-edge eyeball threshold. Ship the network.

Baseline for comparison: the plain hypergeometric null (Probe C's method, conditions on creator degree only) flags **314 of 465** pairs on this same matrix. SDSM adds the concept-popularity condition and cuts that to the set below.

Estimator sensitivity: the primary null above is the exact-margin BiCM. Neal's 2-predictor logit approximation (row-sum + column-sum only) yields **106** edges on the same matrix but misfits creator margins by up to 55 concepts, so it under-conditions on prolificness and keeps looser edges; the exact-margin form is the stricter standard and is used here.

## Validated edges (q < 0.05, strongest overlap-multiple first)

| Creator A | Creator B | observed shared | expected shared | multiple | q-value |
|---|---|---|---|---|---|
| benai92 | simonscrapes | 36 | 22.3 | 1.6x | 1.66e-02 |
| mark_kashef | ramjad | 53 | 33.4 | 1.6x | 2.68e-03 |
| benai92 | thenextnewthingai | 47 | 32.6 | 1.4x | 3.71e-02 |
| claude | engineerprompt | 64 | 44.6 | 1.4x | 6.89e-03 |
| everyinc | thenextnewthingai | 78 | 56.1 | 1.4x | 3.31e-03 |
| gregisenberg | thenextnewthingai | 58 | 42.6 | 1.4x | 3.91e-02 |
| chase_h_ai | seankochel | 86 | 63.2 | 1.4x | 3.31e-03 |
| chase_h_ai | thenextnewthingai | 81 | 61.3 | 1.3x | 1.66e-02 |
| chase_h_ai | engineerprompt | 92 | 71.6 | 1.3x | 1.66e-02 |
| engineerprompt | vanishinggradients | 100 | 79.7 | 1.3x | 2.54e-02 |

## Example shared concepts (top edges, for grounding)

- **benai92 + simonscrapes**: ai engineering.agent_configuration, ai engineering.agent_execution_environments, ai engineering.agent_memory, ai engineering.agent_monitoring, ai engineering.agent_personas
- **mark_kashef + ramjad**: ai engineering.agent_configuration, ai engineering.agent_execution_environments, ai engineering.agent_memory, ai engineering.agent_monitoring, ai engineering.agent_performance_benchmarking
- **benai92 + thenextnewthingai**: ai engineering.agent_configuration, ai engineering.agent_execution_environments, ai engineering.agent_memory, ai engineering.agent_monitoring, ai engineering.agent_personas
- **claude + engineerprompt**: ai engineering.adaptive_reasoning, ai engineering.agent_configuration, ai engineering.agent_execution_environments, ai engineering.agent_handoff_patterns, ai engineering.agent_memory
- **everyinc + thenextnewthingai**: ai engineering.agent_communication_protocols, ai engineering.agent_configuration, ai engineering.agent_execution_environments, ai engineering.agent_memory, ai engineering.agent_monitoring

## Caveats

- This corpus is ~100x smaller than the studies SDSM comes from: every edge is a lead for manual inspection, not a verdict (issue #95 guardrail).
- The overlap multiple compresses toward 1.0 vs the hypergeometric null on purpose: SDSM bakes concept popularity into the expectation, so a surviving 1.6x edge is a *stronger* claim than a hypergeometric 6x edge (which was mostly popularity).
- Edges are undirected co-adoption ties, not influence. 'Who leads whom' is the separate lead-lag report (issue #93).
- A shared-concept count depends on concept-extraction granularity (issue #85 lineage); two creators sharing many generic concepts is weaker evidence than sharing specific ones.
