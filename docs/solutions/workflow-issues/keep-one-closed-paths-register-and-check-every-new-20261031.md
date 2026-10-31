---
title: "Keep one closed-paths register and check every new proposal against it"
date: 2026-10-31
category: workflow-issues
module: decision-records
problem_type: workflow_issue
component: development_workflow
severity: medium
root_cause: inadequate_documentation
applies_when:
  - A proposal touches the knowledge layer, a graph or analytics engine, a model or provider swap, or a cost-cutting change
  - A path was closed, deferred or narrowed in an ADR, a brainstorm or a work note, and someone is about to suggest it again
  - Answering "did we already decide this?" needs more than one file
tags: [decision-records, adr, closed-paths, reopen-trigger, scope, proposal-review]
---

# Keep one closed-paths register and check every new proposal against it

## Context

Several paths in this repo have been closed, deferred or narrowed over time: knowledge-layer options, graph analytics, alternative providers, payload trims and similar cost ideas. Each decision is real and written down somewhere, but "somewhere" was spread across ADRs, brainstorms, a rule file and dated work notes. Each of these paths was re-proposed or nearly misread while its decision lived only in a work note. Rebuilding the status of a single area took a full read across several ADRs and brainstorms.

The costly mistakes were not forgotten decisions. They were scope errors: reading a narrow conclusion as a broad one, or a deferral as a rejection. Typical shapes:

- "This technique returns noise on this corpus" read as "the whole technology class is useless", when a sibling use of it shipped.
- "Component A shipped" read as "the view built on A shipped too", when only A did.
- "Deferred behind named signals" read as "rejected", or the reverse: "a signal fired" claimed when the measurement behind it came from an instrument later found broken.

## Guidance

Keep ONE closed-paths table, in one place, and run every knowledge-layer, graph, provider or cost-cutting proposal against it before it goes further. Each row has exactly four fields plus a name column and a link:

| Field | What it holds |
|---|---|
| Status | One word: `rejected`, `deferred`, `narrowed`, `superseded` |
| Reason | One line: the measured or stated reason, not the history |
| Scope of the closure | What exactly was closed, and what was explicitly NOT closed |
| Reopen trigger | The observable condition that would reopen it, or "none named" |

The row does not restate the decision. It links to the record that holds it, which for anything architectural is an ADR under `docs/adr/` (the ADR's own status keeps the vocabulary in `docs/adr/template.md`; the register's status describes the path, not the record). If no ADR exists, link the brainstorm or work note, and treat the missing ADR as a gap to close when the path next comes up.

Two fields carry most of the value:

- **Scope of the closure** is where the misreads happen. Write the narrow claim and the adjacent thing that stays open in the same cell, so nobody has to infer the boundary.
- **Reopen trigger** turns "we decided no" into "we decided not yet, and here is what changes that". A trigger must name something observable (an eval score, a recurrence count, a cost threshold, a second consumer), never "when it feels right". `docs/adr/ADR-0018-nugget-cli-cross-creator-synthesis.md` ("Gating Criteria") is the model for how a trigger reads.

## Why This Matters

A proposal that matches a closed row costs one lookup to answer. Without the table it costs an archaeology pass, and the archaeology is where the scope errors creep in, because the reader reconstructs the decision from fragments and fills gaps with the broader reading. The table also protects the other direction: a reopen trigger written down in advance lets a deferred path come back on evidence, not on persistence.

## When to Apply

- Before filing an issue, writing a plan or starting a spike in any of the covered areas.
- When a reviewer or agent proposes an alternative tool, provider or analytics engine.
- When an ADR, brainstorm or work note closes, defers or narrows a path: add or update the row in the same change.
- When a reopen trigger's measurement is later found unreliable: mark the row so the trigger is not treated as already met or already missed.

## Examples

A row, with a placeholder topic:

| Path | Status | Reason | Scope of the closure | Reopen trigger | Record |
|---|---|---|---|---|---|
| Engine X for corpus analytics | narrowed | Returned popularity, not signal, on this corpus | Engine X for term co-occurrence only; other uses of the same store stay open | A named aggregate question that X answers and the current layer cannot | an ADR under `docs/adr/` |

Check before proposing: find the row, read the scope cell, and either show the reopen trigger has fired (with the evidence) or move on.

## Related

- `docs/adr/README.md` (ADR index and status vocabulary)
- `docs/adr/ADR-0017-kb-layer-strategy.md`, `docs/adr/ADR-0018-nugget-cli-cross-creator-synthesis.md`, `docs/adr/ADR-0019-derived-analytical-layer-beside-retrieval.md` (records for the knowledge-layer and analytics paths)
- `.claude/rules/intelligence-layer.md` (guardrails for the analytics scripts)
- `docs/solutions/process-issues/feature-worthiness-rubric-20260831.md` (sibling rubric for whether a new knob earns existence)
