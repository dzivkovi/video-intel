# Knowledge-Base Browsing Surface: Research Synthesis + Codex Second Opinion

**Date:** 2026-07-12
**Status:** decision-ready research synthesis for issue #104. Two independent inputs: a web research pass (sources linked throughout) and a Codex (GPT-5) design opinion grounded in this repo's own findings docs (session 019f57e8-2fc4-75a3-a0b2-4e79859de275). They converge on the direction and differ only on prototype scope.
**Question:** what browsing surface should exist over the corpus - Obsidian graph? Karpathy-style LLM wiki? More interactive HTML? BI? - for "librarian-style insight browsing."

---

## The joint verdict (both inputs agree)

1. **The primary surface is an LLM-generated evidence wiki: markdown pages of synthesized PROSE with wikilinks, generated from the DuckDB validated structures, read in Obsidian.** Prose carries the insight; links are byproducts of claims made in the prose ("X covered this 3 weeks before Y", "A and B share 6x more concepts than chance"). Karpathy's exact framing fits: "Obsidian is the IDE; the LLM is the programmer; the wiki is the codebase" ([the llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)).
2. **The old entity vault was dull for a provable reason, and regenerating it prettier would be dull again.** It had links without prose, and its links encoded co-occurrence - which this repo has now killed empirically three ways (2026-07-11 findings: communities -> popular topics, betweenness ~= PageRank, semantic pairing -> synonym soup). Codex, bottom line: "avoid anything that makes the corpus look connected by adding unvalidated edges."
3. **Force-directed global graph views are never the insight surface.** Community consensus matches the repo's own Neo4j result: force layouts encode degree, i.e. popularity ([analysis](https://codeculture.store/blogs/developer-culture/obsidian-graph-view-useful)). The graph pane's only honest uses: a depth-1-2 filtered local graph as a "what's adjacent" sidebar, and Karpathy's usage - wiki HEALTH linting (hubs, orphans).
4. **The one structure that deserves a graph render is the #98 SDSM network** (sparse, validated, ~40 edges max at the acceptance gate) - as a STATIC image embedded in wiki pages, not an interactive graph product. Backbone extraction exists precisely to yield a readable sparse graph ([Neal](https://www.sciencedirect.com/science/article/abs/pii/S0378873314000343)).
5. **Temporal structures are charts, not notes.** Lead-lag chains fight force layouts by construction; they want creator-swimlane timelines (the shipped #94 HTML already does this) and dated "adoption chronology" prose sections in wiki pages. Bursts (#103) are a feed for briefings, never a map.

## Where the two inputs differ (and the resolution)

- **Web research prototype:** a 15-page wiki slice (10 concept pages + 5 creator pages + index.md MOC), one evening, falsifiable immediately.
- **Codex prototype:** narrower - "Lead-Lag Creator Atlas v0": creator dossiers generated from the shipped lead-lag report only (corrected ranking, per-creator lead/lag sections, quoted timestamped evidence), explicitly NOT a 551-concept wiki, and do not wait for SDSM/bursts.
- **Resolution (adopted):** Codex's scope with the research's structure - generate from the lead-lag data only: ~9 creator dossiers (the rankable table) + the top-10 chain concepts as concept pages + index.md as the MOC. Open in Obsidian, wander for an evening. If page-to-page hops feel like insight, scale; if it feels like the old vault, the pattern is falsified for ~an evening's cost.

## What makes the wiki non-dull (the checklist, enforced at generation)

1. Every page is synthesized prose with judgments and flagged tensions, not extracted stubs.
2. Every claim carries an inline citation (video + `&t=` timestamp) - unsourced claims are lint defects (lesson from the [one-month field review](https://www.rdworldonline.com/is-karpathys-viral-llm-wiki-helpful-mostly-yes-one-month-in/)).
3. Wikilinks appear only where a validated relationship exists (lead-lag edge, SDSM tie, shared burst) - never from co-occurrence.
4. index.md is an authored Map of Content with context per link ([why MOCs work](https://www.dsebastien.net/2022-05-15-maps-of-content/)); log.md records each regeneration.
5. Frontmatter stamps (creator, concept, first_covered) so Obsidian Bases gives faceted table browsing for free ([Bases vs Dataview](https://practicalpkm.com/moving-to-obsidian-bases-from-dataview/)).
6. Regeneration is agent-driven from DuckDB at scan time - the human reads, the LLM does the bookkeeping (Karpathy: "the tedious part ... is the bookkeeping").

## The three-surface picture (shared data layer, three cheap surfaces)

| Insight class | Surface | Cost | Status |
|---|---|---|---|
| Synthesis + browsing | LLM evidence wiki in `_wiki/` (Obsidian as reader) | low-medium | prototype next (this doc) |
| Temporal analytics | Self-contained HTML per analysis (shipped #94 pattern); Evidence.dev/Observable if pages multiply ([BI-as-code](https://motherduck.com/blog/the-future-of-bi-bi-as-code-duckdb-impact/)) | shipped / medium | #94 done; BI deferred until needed |
| Semantic neighborhoods | [Apple Embedding Atlas](https://github.com/apple/embedding-atlas) over the existing Voyage embeddings (local, one command) | hours | optional exploration instrument |

Ad-hoc table poking: `duckdb -ui` (#102 documents it). Chat-over-corpus: already exists (the search skill); receipts must persist per the roadmap's receipts-vs-synthesis contract, so chat never replaces the wiki.

## What NOT to build (both inputs, verbatim intent)

- No raw/auto entity vault, no force-directed term graph, no interactive graph product for SDSM.
- No Neo4j/GDS revival; no general framework before the prototype earns it (#95 guardrail).
- No BI dashboard as the primary owner-facing surface (analyst tooling only).
- No 551-page wiki generation before the 15-page slice proves the browsing shape.
- No SDSM visualization before #98 ships and passes its acceptance gate.

## Karpathy LLM-wiki: signal or fashion? (both inputs, same answer)

Signal for THIS corpus, under constraints: the corpus already has the truth store, normalized concepts, quoted evidence, and now temporal structure - so the wiki writes prose ABOUT validated discovery outputs. It becomes fashion the moment pages are generated from raw co-occurrence ("the wiki is not the discovery engine; it is the durable reading surface over validated discovery outputs" - Codex). Ecosystem exists if wanted later: an [Agent-Skills-compatible implementation](https://github.com/Astro-Han/karpathy-llm-wiki), [Quartz 4](https://notes.hamatti.org/technology/building-a-digital-garden-with-obsidian-and-quartz) for publishing.

## Proposed next step (needs owner go - not filed as an issue yet)

One evening-scale prototype: `P2: feat(wiki): Lead-Lag Creator Atlas v0` - a generator that reads the DuckDB store via the #93 selection helpers and writes `_wiki/` (9 creator dossiers, 10 chain-concept pages, index.md MOC, log.md), citation-disciplined per the checklist above. Judged by wandering, discarded cheaply if dull.

## Sources

Karpathy: [llm-wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f); [community synthesis](https://gist.github.com/deanjstone/98141cb836bb97c555ae9d6ce2484b5f); [one-month field review](https://www.rdworldonline.com/is-karpathys-viral-llm-wiki-helpful-mostly-yes-one-month-in/); [Claude Code skill impl](https://github.com/Astro-Han/karpathy-llm-wiki); practitioner writeups ([vanja.io](https://vanja.io/the-knowledge-base-that-builds-itself/), [MindStudio](https://www.mindstudio.ai/blog/andrej-karpathy-llm-wiki-knowledge-base-claude-code)). Obsidian practice: [graph-view analysis](https://codeculture.store/blogs/developer-culture/obsidian-graph-view-useful); [MOCs](https://www.dsebastien.net/2022-05-15-maps-of-content/); [Bases migration](https://practicalpkm.com/moving-to-obsidian-bases-from-dataview/); [Breadcrumbs](https://community.obsidian.md/plugins/breadcrumbs); [Quartz 4](https://notes.hamatti.org/technology/building-a-digital-garden-with-obsidian-and-quartz). Analytics surfaces: [DuckDB local UI](https://duckdb.org/2025/03/12/duckdb-ui); [Evidence BI comparison](https://dev.to/de_clerke/i-shipped-12-bi-dashboards-with-5-different-tools-here-is-the-honest-comparison-13g0); [Observable Framework DuckDB](https://observablehq.com/framework/lib/duckdb); [Embedding Atlas](https://machinelearning.apple.com/research/embedding-atlas). Networks/timelines: [Neal, bipartite backbones](https://www.sciencedirect.com/science/article/abs/pii/S0378873314000343); [backbone package](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0244363); [force-directed timelines](https://www.sciencedirect.com/science/article/abs/pii/S2214579621001088).
