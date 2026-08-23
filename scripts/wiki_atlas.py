#!/usr/bin/env python3
"""Lead-Lag Creator Atlas v0 (issue #105).

Emits an Obsidian-readable wiki slice - creator dossiers, chain-concept pages,
an index.md Map of Content, and a log.md generation record - into a `_wiki/`
folder, generated from the SHIPPED lead-lag data only. It renders via
`lead_lag_report.build_report_data` / `ranked_creators` / `finding_chains`
and never recomputes (same contract as scripts/lead_lag_viz.py).

The script emits the DATA skeleton per page (facts, chains, quotes, links)
with `> [!todo] PROSE` callouts; the executing agent writes the synthesized
prose into the emitted pages. Non-dull checklist enforced at generation:
every claim cited (video + &t= link), wikilinks only where a validated
lead-lag relationship exists AND the target page exists, frontmatter stamps
for Obsidian Bases, small-sample rows carry the same honesty language as the
report and viz.

Read-only against the DuckDB store. One generator, not a framework.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from lead_lag_report import (
    DEFAULT_DB,
    MIN_ADOPTERS_DEFAULT,
    MIN_ARTIFACTS_DEFAULT,
    MIN_ELIGIBLE_DEFAULT,
    MIN_RANKED_CONCEPTS_DEFAULT,
    TOP_FINDINGS_DEFAULT,
    Chain,
    CreatorStats,
    FirstMention,
    ReportData,
    build_report_data,
    extract_quote,
    finding_chains,
    ranked_creators,
)
from lead_lag_viz import ROBUST_EXPECTED, SMALL_SAMPLE_EXPECTED

from timestamp_utils import timestamped_url

if TYPE_CHECKING:
    pass

# Listing caps keep dossiers readable; the full count is always stated so a
# cap never reads as "that's everything" (no-silent-caps rule).
MAX_LEADS_LISTED = 15
MAX_FOLLOWS_LISTED = 15

PROSE_TODO = "> [!todo] PROSE (agent)"
PROSE_INLINE = "(TODO-PROSE: one-line context)"


def slugify(concept_id: str) -> str:
    """Filesystem- and wikilink-safe page name for a concept id."""
    slug = re.sub(r"[^a-z0-9]+", "-", concept_id.lower()).strip("-")
    return slug or "concept"


def tier_label(expected: float) -> str:
    if expected >= ROBUST_EXPECTED:
        return "robust"
    if expected < SMALL_SAMPLE_EXPECTED:
        return "small sample"
    return "mid"


def _naive_ranks(data: ReportData) -> dict[str, int]:
    # source_id tiebreak: ties otherwise swap ranks run-to-run with the DB's
    # row order, making regeneration non-deterministic
    ordered = sorted(data.naive.items(), key=lambda kv: (-kv[1], kv[0]))
    return {sid: i + 1 for i, (sid, _) in enumerate(ordered)}


def _evidence_citation(m: FirstMention) -> str:
    link = timestamped_url(m.url, m.start_seconds if "youtube.com" in m.url else None)
    quote = extract_quote(m.segment_text, m.as_mentioned) if m.segment_text else "(mindmap-level mention, no segment)"
    return f'> "{quote}"\n>\n> - {m.source_id}, [{m.title}]({link}), {m.first_date.isoformat()}'


def _link(name: str, existing: set[str]) -> str:
    """Wikilink only when the target page exists; plain text otherwise.

    A chain member below the ranking threshold has no dossier, and a concept
    outside the top findings has no page - linking them would violate the
    "wikilinks are validated AND resolvable" rule for v0.
    """
    return f"[[{name}]]" if name in existing else name


def _dossier(
    s: CreatorStats,
    data: ReportData,
    naive_ranks: dict[str, int],
    corrected_rank: int,
    creator_pages: set[str],
    concept_pages: dict[str, str],
) -> str:
    cov = data.coverage[s.source_id]
    tier = tier_label(s.expected)
    leads: list[tuple[Chain, FirstMention]] = []
    follows: list[tuple[Chain, FirstMention, int]] = []
    for chain in data.chains:
        leader_date = chain.mentions[0].first_date
        for m in chain.mentions:
            if m.source_id != s.source_id:
                continue
            # tie on the leader date = shared first (precursor_stats splits the
            # credit fractionally); rendering a tied co-leader as a follower
            # would contradict the statistics this page claims to render
            if m.first_date == leader_date:
                leads.append((chain, m))
            else:
                follows.append((chain, m, (m.first_date - leader_date).days))

    lines = [
        "---",
        "type: creator",
        f"creator: {s.source_id}",
        f"first_covered: {cov.start.isoformat()}",
        f"tier: {tier.replace(' ', '-')}",
        f"lift: {s.lift:.2f}",
        "---",
        "",
        f"# {s.source_id}",
        "",
        PROSE_TODO,
        "> Write 2-3 paragraphs: what this creator's lead/follow pattern says, with a judgment.",
        "",
        "## Corrected scorecard",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Corrected rank | #{corrected_rank} (lift {s.lift:.2f}) |",
        f"| Observed vs expected firsts | {s.firsts:.1f} vs {s.expected:.1f} over {s.eligible_concepts} eligible concepts |",
        f"| Naive rank (uncorrected) | {f'#{naive_ranks[s.source_id]}' if s.source_id in naive_ranks else 'unranked (no naive firsts)'} |",
        f"| Mean lag behind leaders | {s.mean_lag_days:.0f} days |",
        f"| Coverage window | {cov.start.isoformat()} to {cov.end.isoformat()} ({cov.n_artifacts} artifacts) |",
        f"| Evidence tier | {tier} |",
    ]
    if tier == "small sample":
        lines.append("")
        lines.append(
            f"> [!warning] Small sample: expected firsts {s.expected:.1f} < {SMALL_SAMPLE_EXPECTED:.0f} - "
            "this lift is one lucky first away from noise. Read as a lead for inspection, not a verdict."
        )
    lines += ["", f"## Leads on ({len(leads)} concepts)", ""]
    leads.sort(key=lambda t: (t[1].first_date, t[0].concept_id))
    for chain, m in leads[:MAX_LEADS_LISTED]:
        target = (
            _link(concept_pages[chain.concept_id], set(concept_pages.values()))
            if chain.concept_id in concept_pages
            else chain.concept_id
        )
        lines.append(f"- {target} - first on {m.first_date.isoformat()}, {len(chain.mentions) - 1} followers")
    if len(leads) > MAX_LEADS_LISTED:
        lines.append(
            f"- ...and {len(leads) - MAX_LEADS_LISTED} more (full set in the [lead-lag report](../../_reports/))"
        )
    follow_window = data.params.get("follow_window_days", 0)
    lines += ["", f"## Follows on ({len(follows)} concepts)", ""]
    for chain, _m, lag in sorted(follows, key=lambda t: (t[2], t[0].concept_id))[:MAX_FOLLOWS_LISTED]:
        leader = chain.mentions[0].source_id
        target = (
            _link(concept_pages[chain.concept_id], set(concept_pages.values()))
            if chain.concept_id in concept_pages
            else chain.concept_id
        )
        # beyond the follow window the lag is a date fact, not a validated
        # lead->follow edge - state it, but do not wikilink the "leader"
        leader_ref = _link(leader, creator_pages) if lag <= follow_window else leader
        lines.append(f"- {target} - {lag} days behind {leader_ref}")
    if len(follows) > MAX_FOLLOWS_LISTED:
        lines.append(f"- ...and {len(follows) - MAX_FOLLOWS_LISTED} more")
    lines.append("")
    return "\n".join(lines)


def _concept_page(
    chain: Chain,
    page_name: str,
    creator_pages: set[str],
) -> str:
    leader = chain.mentions[0]
    lines = [
        "---",
        "type: concept",
        f"concept: {chain.concept_id}",
        f"first_covered: {leader.first_date.isoformat()}",
        f"adopters: {len(chain.mentions)}",
        "---",
        "",
        f"# {chain.concept_id}",
        "",
        PROSE_TODO,
        "> Write the adoption story: who led, how it spread, what the lags suggest - with a judgment.",
        "",
        "## Adoption chain",
        "",
        "| # | Creator | First covered | Lag behind leader |",
        "|---|---|---|---|",
    ]
    for i, m in enumerate(chain.mentions, start=1):
        lag = (m.first_date - leader.first_date).days
        lines.append(
            f"| {i} | {_link(m.source_id, creator_pages)} | {m.first_date.isoformat()} | {'-' if lag == 0 else f'{lag}d'} |"
        )
    lines += ["", "## Leader evidence", "", _evidence_citation(leader), ""]
    followers_with_quotes = [m for m in chain.mentions[1:] if m.segment_text]
    if followers_with_quotes:
        lines += ["## Follower evidence", ""]
        for m in followers_with_quotes[:2]:
            lines += [_evidence_citation(m), ""]
    return "\n".join(lines)


def _index(
    ranked: list[CreatorStats],
    chains: list[Chain],
    concept_pages: dict[str, str],
    data: ReportData,
) -> str:
    lines = [
        "---",
        "type: index",
        "---",
        "",
        "# Lead-Lag Creator Atlas",
        "",
        PROSE_TODO,
        "> Write the MOC intro: what this atlas is, how to wander it, what the correction means - and one line of context per link below.",
        "",
        "## Creators (corrected ranking)",
        "",
    ]
    for i, s in enumerate(ranked, start=1):
        lines.append(
            f"{i}. [[{s.source_id}]] - lift {s.lift:.2f} ({tier_label(s.expected)}, "
            f"{s.firsts:.1f} vs {s.expected:.1f} expected) - {PROSE_INLINE}"
        )
    lines += ["", "## Concept adoption stories", ""]
    for chain in chains:
        span = (chain.mentions[-1].first_date - chain.mentions[0].first_date).days
        lines.append(
            f"- [[{concept_pages[chain.concept_id]}]] - {chain.mentions[0].source_id} led, "
            f"{len(chain.mentions) - 1} followed over {span} days - {PROSE_INLINE}"
        )
    lines += [
        "",
        "## Method, in one breath",
        "",
        f"Corrected for coverage start and posting rate ({data.n_concepts_eligible} eligible of "
        f"{data.n_concepts_total} concepts; params {data.params}). "
        "Full method: the lead-lag report in `_reports/`.",
        "",
    ]
    return "\n".join(lines)


def _readme() -> str:
    """Static 'how to read this' page, emitted into every generated vault.

    The wandering guide is self-contained - it travels with the wiki and needs
    nothing else to browse it in Obsidian. The one optional convenience it
    mentions (the register-vault helper) needs the repo checkout, and is flagged
    as such.
    """
    return "\n".join(
        [
            "---",
            "type: help",
            "---",
            "",
            "# Start here: reading this atlas in Obsidian",
            "",
            "This folder is a generated knowledge wiki: synthesized, citation-bearing prose about who "
            "covers an AI idea first in a watched corpus and who follows. It is meant to be *browsed*, not "
            "read top to bottom. [Obsidian](https://obsidian.md) is the ideal reader because it turns the "
            "`[[double-bracket]]` links into a navigable web.",
            "",
            "## Open it in Obsidian (one time)",
            "",
            "1. Install Obsidian (free) and launch it.",
            "2. Bottom-left corner, click the **Open another vault** icon, then **Open folder as vault**, and "
            "pick *this* folder. (In the file dialog you can paste the folder path into the address bar.)",
            "3. That registers the folder as a vault; it stays in your vault list afterwards.",
            "",
            "If you have the repo checkout and the GUI fights you, it ships a helper that registers a vault by "
            "editing Obsidian's vault list directly (Obsidian must be closed): "
            "`python scripts/register_obsidian_vault.py <this-folder> --open --launch`. Full lecture: the repo's "
            "`docs/intelligence-layer.md`.",
            "",
            "## How to wander",
            "",
            "- Start at [[index]] - the map of content, with the ranked creators and the concept adoption stories.",
            "- Click any `[[name]]` to jump. Hover to preview without leaving the page.",
            "- Open the **graph view** (the constellation icon) to see the whole web; edges exist only where the "
            "data shows a validated lead-follow relationship, so it is sparse on purpose, not a hairball.",
            "- Use **Reading view** (not Editing view) so links render clean and the coloured note callouts show.",
            "- Plain-text names (not links) are creators too small to rank; they have no page by design.",
            "",
            "## It refreshes on its own",
            "",
            "Registering the vault is a one-time step per folder. Once open, Obsidian watches the folder live, so "
            "regenerating this atlas or adding notes shows up immediately - no re-registering.",
            "",
            "## Regenerating",
            "",
            "This wiki is rebuilt from the corpus by `scripts/wiki_atlas.py`. A regeneration overwrites the "
            "generated pages, so if you hand-edit prose you want to keep, note it - re-running the generator in "
            "place replaces `index.md`, the creator, and the concept pages.",
            "",
        ]
    )


def _log(data: ReportData, pages: dict[str, str], generated: str, db: str) -> str:
    lines = [
        "---",
        "type: log",
        "---",
        "",
        "# Generation log",
        "",
        f"- Generated: {generated}",
        f"- Store: {db} (read-only)",
        "- Generator: scripts/wiki_atlas.py (issue #105), rendering scripts/lead_lag_report.py data",
        f"- Params: {data.params}",
        f"- Concepts: {data.n_concepts_eligible} eligible of {data.n_concepts_total}",
        f"- Pages: {len(pages)}",
        "",
    ]
    lines += [f"  - {p}" for p in sorted(pages)]
    lines.append("")
    return "\n".join(lines)


def build_atlas(
    data: ReportData,
    min_ranked_concepts: int = MIN_RANKED_CONCEPTS_DEFAULT,
    top_findings: int = TOP_FINDINGS_DEFAULT,
    generated: str = "",
    db: str = "",
) -> dict[str, str]:
    """Pure renderer: ReportData -> {relative posix path: markdown}."""
    ranked = ranked_creators(data, min_ranked_concepts)
    chains = finding_chains(data, top_findings)
    creator_pages = {s.source_id for s in ranked}
    # Distinct concept_ids can slugify identically ("dom.pact" / "dom_pact");
    # disambiguate deterministically so one page never silently overwrites another.
    concept_pages: dict[str, str] = {}
    used_slugs: set[str] = set()
    for c in chains:
        slug = base = slugify(c.concept_id)
        n = 2
        while slug in used_slugs:
            slug = f"{base}-{n}"
            n += 1
        used_slugs.add(slug)
        concept_pages[c.concept_id] = slug
    naive_ranks = _naive_ranks(data)

    pages: dict[str, str] = {}
    for i, s in enumerate(ranked, start=1):
        pages[f"creators/{s.source_id}.md"] = _dossier(s, data, naive_ranks, i, creator_pages, concept_pages)
    for chain in chains:
        pages[f"concepts/{concept_pages[chain.concept_id]}.md"] = _concept_page(
            chain, concept_pages[chain.concept_id], creator_pages
        )
    pages["index.md"] = _index(ranked, chains, concept_pages, data)
    pages["README.md"] = _readme()  # static how-to-read, travels with the vault
    pages["log.md"] = ""  # placeholder so the log's own inventory includes itself
    pages["log.md"] = _log(data, pages, generated, db)
    return pages


def write_atlas(pages: dict[str, str], wiki_dir: Path) -> None:
    root = wiki_dir.resolve()
    for rel, content in pages.items():
        path = (root / rel).resolve()
        if root not in path.parents:
            raise ValueError(f"page path escapes wiki dir: {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the Lead-Lag Creator Atlas wiki slice (issue #105).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to intel.duckdb (read-only)")
    parser.add_argument(
        "--wiki-dir",
        type=Path,
        default=None,
        help="Output folder (e.g. <output_dir>/_wiki). Omit for a dry-run listing.",
    )
    parser.add_argument("--min-adopters", type=int, default=MIN_ADOPTERS_DEFAULT)
    parser.add_argument("--min-eligible", type=int, default=MIN_ELIGIBLE_DEFAULT)
    parser.add_argument("--min-artifacts", type=int, default=MIN_ARTIFACTS_DEFAULT)
    parser.add_argument("--min-ranked-concepts", type=int, default=MIN_RANKED_CONCEPTS_DEFAULT)
    parser.add_argument("--top-findings", type=int, default=TOP_FINDINGS_DEFAULT)
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"store not found: {args.db} (build it with scripts/intel_graph.py load)")
    import duckdb

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        data = build_report_data(
            con,
            min_adopters=args.min_adopters,
            min_eligible=max(1, args.min_eligible),
            min_artifacts=args.min_artifacts,
        )
    finally:
        con.close()

    generated = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    pages = build_atlas(
        data,
        min_ranked_concepts=args.min_ranked_concepts,
        top_findings=args.top_findings,
        generated=generated,
        db=str(args.db),
    )
    if args.wiki_dir is None:
        for rel in sorted(pages):
            print(f"{rel}\t{len(pages[rel])} chars")
        print(f"(dry run: {len(pages)} pages; pass --wiki-dir to write)")
        return
    write_atlas(pages, args.wiki_dir)
    todo = sum(p.count(PROSE_TODO) + p.count(PROSE_INLINE) for p in pages.values())
    print(f"wrote {len(pages)} pages to {args.wiki_dir} ({todo} prose slots for the agent to fill)")


if __name__ == "__main__":
    main()
