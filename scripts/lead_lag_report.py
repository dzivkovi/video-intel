#!/usr/bin/env python3
"""Coverage-corrected lead-lag report (issue #93).

Answers "who covers a concept FIRST, who follows" over the DuckDB truth store
built by scripts/intel_graph.py (PR #86), corrected for each creator's
corpus-coverage start date so deep-backfill channels do not fake precedence.

Method (minimal form of the correction in "Precursors and Laggards",
arXiv:1009.0119):

1. First-mention date per (concept, creator) via has_concept -> artifacts.
2. Eligibility: for a concept emerging at date T (earliest first mention among
   its adopters), only adopters whose coverage window was already active at T
   count. A first mention with fewer than --min-eligible eligible adopters is
   discarded: nobody else could have been observed covering it earlier, so
   "first" is a corpus artifact, not a signal.
3. Rate normalization: expected firsts per creator are proportional to their
   posting rate among the eligible adopters, so prolific channels only score
   when they beat their volume-implied chance. Precursor lift = observed / expected.
4. Kill-criterion diagnostics: Spearman correlation of the corrected ranking
   against coverage-start rank and corpus-size rank, plus the naive ranking
   side by side. High correlation means the "signal" is the corpus artifact
   issue #93 says to kill on.

One report script, not a framework. Read-only against the DuckDB store.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import itertools
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("lead_lag_report")

DEFAULT_DB = Path.home() / ".cache" / "video-intel" / "intel.duckdb"
MIN_ADOPTERS_DEFAULT = 4  # concept must be adopted by >= N creators (issue #93 spec)
MIN_ELIGIBLE_DEFAULT = 3  # concept must have >= N adopters whose coverage was active at emergence
MIN_ARTIFACTS_DEFAULT = 5  # creators below this corpus size are too noisy to rank
FOLLOW_WINDOW_DAYS_DEFAULT = 90  # max lag for an "A leads, B follows" edge
TOP_FINDINGS_DEFAULT = 10
QUOTE_WIDTH = 220


@dataclass(frozen=True)
class Coverage:
    """A creator's observed corpus window."""

    source_id: str
    start: dt.date
    end: dt.date
    n_artifacts: int

    @property
    def rate(self) -> float:
        """Artifacts per active day; the volume-implied chance of being first."""
        return self.n_artifacts / max(1, (self.end - self.start).days)


@dataclass(frozen=True)
class FirstMention:
    concept_id: str
    source_id: str
    first_date: dt.date
    artifact_id: str
    title: str
    url: str
    start_seconds: int | None
    segment_text: str | None
    as_mentioned: str


@dataclass
class CreatorStats:
    source_id: str
    firsts: float = 0.0
    expected: float = 0.0
    eligible_concepts: int = 0
    lag_days: list[float] = field(default_factory=list)

    @property
    def lift(self) -> float:
        return self.firsts / self.expected if self.expected > 0 else 0.0

    @property
    def mean_lag_days(self) -> float:
        return sum(self.lag_days) / len(self.lag_days) if self.lag_days else 0.0


@dataclass(frozen=True)
class Chain:
    concept_id: str
    mentions: tuple[FirstMention, ...]  # eligible adopters, ordered by first date
    edges: tuple[tuple[str, str, int], ...]  # (leader, follower, lag_days) within window


@dataclass(frozen=True)
class ReportData:
    coverage: dict[str, Coverage]
    stats: dict[str, CreatorStats]
    naive: dict[str, float]
    chains: list[Chain]
    n_concepts_total: int
    n_concepts_eligible: int
    params: dict[str, int]


def _eligible(mentions: list[FirstMention], coverage: dict[str, Coverage]) -> list[FirstMention]:
    """Adopters whose coverage window was already active at the concept's emergence.

    Emergence is the earliest first mention among adopters that are in the
    coverage map at all (creators filtered out by --min-artifacts don't count).
    """
    known = [m for m in sorted(mentions, key=lambda m: m.first_date) if m.source_id in coverage]
    if not known:
        return []
    emergence = known[0].first_date
    return [m for m in known if coverage[m.source_id].start <= emergence]


def precursor_stats(
    first_mentions: dict[str, list[FirstMention]],
    coverage: dict[str, Coverage],
    min_eligible: int,
) -> dict[str, CreatorStats]:
    """Coverage-corrected, rate-normalized precursor statistics per creator."""
    stats: dict[str, CreatorStats] = {}
    for mentions in first_mentions.values():
        eligible = _eligible(mentions, coverage)
        if len(eligible) < min_eligible:
            continue
        total_rate = sum(coverage[m.source_id].rate for m in eligible)
        if total_rate <= 0:
            continue
        leader_date = eligible[0].first_date
        tied = [m for m in eligible if m.first_date == leader_date]
        for m in eligible:
            s = stats.setdefault(m.source_id, CreatorStats(source_id=m.source_id))
            s.eligible_concepts += 1
            s.expected += coverage[m.source_id].rate / total_rate
            s.lag_days.append(float((m.first_date - leader_date).days))
            if m.first_date == leader_date:
                s.firsts += 1.0 / len(tied)
    return stats


def naive_leader_counts(first_mentions: dict[str, list[FirstMention]]) -> dict[str, float]:
    """Uncorrected firsts: what a naive MIN(published_at) query would report."""
    counts: dict[str, float] = {}
    for mentions in first_mentions.values():
        ordered = sorted(mentions, key=lambda m: m.first_date)
        tied = [m for m in ordered if m.first_date == ordered[0].first_date]
        for m in tied:
            counts[m.source_id] = counts.get(m.source_id, 0.0) + 1.0 / len(tied)
    return counts


def adoption_chains(
    first_mentions: dict[str, list[FirstMention]],
    coverage: dict[str, Coverage],
    min_eligible: int,
    follow_window_days: int,
) -> list[Chain]:
    """Ordered adoption chains over eligible adopters, with lead->follow edges."""
    chains: list[Chain] = []
    for concept_id, mentions in first_mentions.items():
        eligible = _eligible(mentions, coverage)
        if len(eligible) < min_eligible:
            continue
        edges: list[tuple[str, str, int]] = []
        for a, b in itertools.pairwise(eligible):
            lag = (b.first_date - a.first_date).days
            if 0 < lag <= follow_window_days:
                edges.append((a.source_id, b.source_id, lag))
        chains.append(Chain(concept_id=concept_id, mentions=tuple(eligible), edges=tuple(edges)))
    return chains


def spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation with average ranks for ties. Pure stdlib."""

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                result[order[k]] = avg
            i = j + 1
        return result

    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def extract_quote(text: str, term: str, width: int = QUOTE_WIDTH) -> str:
    """A single-line excerpt around the first occurrence of term (or the head)."""
    flat = " ".join(text.split())
    idx = flat.lower().find(term.lower())
    if idx < 0:
        excerpt = flat[:width]
        return excerpt + ("..." if len(flat) > width else "")
    half = max(0, (width - len(term)) // 2)
    start = max(0, idx - half)
    end = min(len(flat), idx + len(term) + half)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def load_coverage(con: object, min_artifacts: int) -> dict[str, Coverage]:
    rows = con.execute(  # type: ignore[attr-defined]
        """
        SELECT source_id, MIN(published_at), MAX(published_at), COUNT(*)
        FROM artifacts
        WHERE published_at IS NOT NULL
        GROUP BY source_id
        HAVING COUNT(*) >= ?
        """,
        [min_artifacts],
    ).fetchall()
    return {r[0]: Coverage(source_id=r[0], start=r[1], end=r[2], n_artifacts=r[3]) for r in rows}


def load_first_mentions(con: object, min_adopters: int) -> tuple[dict[str, list[FirstMention]], int]:
    """First mention per (concept, creator), with the best evidence row attached.

    Evidence preference: a grounded row with a segment timestamp beats an
    ungrounded mindmap-level row, because it yields a quotable excerpt and a
    &t= deep link.
    """
    rows = con.execute(  # type: ignore[attr-defined]
        """
        WITH firsts AS (
            SELECT hc.concept_id, a.source_id, MIN(a.published_at) AS first_date
            FROM has_concept hc
            JOIN artifacts a ON hc.artifact_id = a.artifact_id
            WHERE a.published_at IS NOT NULL
            GROUP BY 1, 2
        ),
        adopted AS (
            SELECT concept_id
            FROM firsts
            GROUP BY concept_id
            HAVING COUNT(DISTINCT source_id) >= ?
        ),
        evidence AS (
            SELECT
                hc.concept_id, a.source_id, a.published_at, a.artifact_id, a.title, a.url,
                s.start_seconds, s.text AS segment_text, hc.as_mentioned,
                ROW_NUMBER() OVER (
                    PARTITION BY hc.concept_id, a.source_id
                    ORDER BY hc.grounded DESC, s.start_seconds ASC NULLS LAST
                ) AS rn
            FROM has_concept hc
            JOIN artifacts a ON hc.artifact_id = a.artifact_id
            JOIN firsts f
              ON f.concept_id = hc.concept_id
             AND f.source_id = a.source_id
             AND f.first_date = a.published_at
            LEFT JOIN segments s ON hc.segment_id = s.segment_id
            WHERE hc.concept_id IN (SELECT concept_id FROM adopted)
        )
        SELECT concept_id, source_id, published_at, artifact_id, title, url,
               start_seconds, segment_text, as_mentioned
        FROM evidence WHERE rn = 1
        """,
        [min_adopters],
    ).fetchall()
    n_total = con.execute("SELECT COUNT(DISTINCT concept_id) FROM has_concept").fetchone()[0]  # type: ignore[attr-defined]
    mentions: dict[str, list[FirstMention]] = {}
    for r in rows:
        mentions.setdefault(r[0], []).append(
            FirstMention(
                concept_id=r[0],
                source_id=r[1],
                first_date=r[2],
                artifact_id=r[3],
                title=r[4] or "",
                url=r[5] or "",
                start_seconds=r[6],
                segment_text=r[7],
                as_mentioned=r[8] or "",
            )
        )
    return mentions, n_total


def backfill_evidence(con: object, chains: list[Chain]) -> list[Chain]:
    """Give chain leaders without a grounded segment a second chance at a quote.

    A first mention can come from an ungrounded (mindmap-level) row while the
    same artifact's transcript still contains the term. Issue #93 wants quoted
    evidence with timestamps, so try three tiers against the artifact's
    segments: (1) the as-mentioned term verbatim, (2) a segment the mentions
    table links to the same entity, (3) a segment containing every
    distinctive token of the term. contains() avoids LIKE escaping issues
    with terms like 'C%' or 'k_s'.
    """
    result: list[Chain] = []
    for chain in chains:
        leader = chain.mentions[0]
        if leader.segment_text is None:
            row = _find_evidence_segment(con, leader)
            if row is not None:
                leader = dataclasses.replace(leader, start_seconds=row[0], segment_text=row[1])
                result.append(Chain(chain.concept_id, (leader, *chain.mentions[1:]), chain.edges))
                continue
        result.append(chain)
    return result


def _find_evidence_segment(con: object, m: FirstMention) -> tuple[int | None, str] | None:
    term = m.as_mentioned.lower().strip()
    if term:
        row = con.execute(  # type: ignore[attr-defined]
            "SELECT start_seconds, text FROM segments WHERE artifact_id = ? AND contains(lower(text), ?)"
            " ORDER BY start_seconds ASC NULLS LAST LIMIT 1",
            [m.artifact_id, term],
        ).fetchone()
        if row is not None:
            return row
    row = con.execute(  # type: ignore[attr-defined]
        """
        SELECT s.start_seconds, s.text
        FROM segments s
        JOIN mentions mn ON s.segment_id = mn.segment_id
        JOIN has_concept hc ON hc.entity_id = mn.entity_id
        WHERE s.artifact_id = ? AND hc.artifact_id = ? AND hc.concept_id = ?
        ORDER BY s.start_seconds ASC NULLS LAST LIMIT 1
        """,
        [m.artifact_id, m.artifact_id, m.concept_id],
    ).fetchone()
    if row is not None:
        return row
    tokens = [t for t in term.split() if len(t) > 3]
    if tokens:
        clause = " AND ".join("contains(lower(text), ?)" for _ in tokens)
        row = con.execute(  # type: ignore[attr-defined]
            f"SELECT start_seconds, text FROM segments WHERE artifact_id = ? AND {clause}"
            " ORDER BY start_seconds ASC NULLS LAST LIMIT 1",
            [m.artifact_id, *tokens],
        ).fetchone()
        if row is not None:
            return row
    return None


def build_report_data(
    con: object,
    min_adopters: int = MIN_ADOPTERS_DEFAULT,
    min_eligible: int = MIN_ELIGIBLE_DEFAULT,
    min_artifacts: int = MIN_ARTIFACTS_DEFAULT,
    follow_window_days: int = FOLLOW_WINDOW_DAYS_DEFAULT,
) -> ReportData:
    coverage = load_coverage(con, min_artifacts)
    first_mentions, n_total = load_first_mentions(con, min_adopters)
    stats = precursor_stats(first_mentions, coverage, min_eligible)
    chains = backfill_evidence(con, adoption_chains(first_mentions, coverage, min_eligible, follow_window_days))
    return ReportData(
        coverage=coverage,
        stats=stats,
        naive=naive_leader_counts(first_mentions),
        chains=chains,
        n_concepts_total=n_total,
        n_concepts_eligible=len(chains),
        params={
            "min_adopters": min_adopters,
            "min_eligible": min_eligible,
            "min_artifacts": min_artifacts,
            "follow_window_days": follow_window_days,
        },
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _evidence_line(m: FirstMention) -> str:
    link = m.url
    if m.start_seconds is not None and "youtube.com" in m.url:
        link = f"{m.url}&t={m.start_seconds}s"
    quote = extract_quote(m.segment_text, m.as_mentioned) if m.segment_text else "(mindmap-level mention, no segment)"
    return f'> "{quote}"\n>\n> - {m.source_id}, [{m.title}]({link}), {m.first_date.isoformat()}'


def _chain_line(chain: Chain) -> str:
    hops = " -> ".join(f"{m.source_id}({m.first_date.strftime('%y-%m-%d')})" for m in chain.mentions)
    return hops


def render_report(data: ReportData, top_findings: int = TOP_FINDINGS_DEFAULT) -> str:
    ranked = sorted(
        (s for s in data.stats.values() if s.eligible_concepts >= 5),
        key=lambda s: s.lift,
        reverse=True,
    )
    naive_ranked = sorted(data.naive.items(), key=lambda kv: kv[1], reverse=True)

    # kill-criterion diagnostics over creators present in the corrected ranking
    lifts = [s.lift for s in ranked]
    cov_starts = [float(data.coverage[s.source_id].start.toordinal()) for s in ranked]
    sizes = [float(data.coverage[s.source_id].n_artifacts) for s in ranked]
    rho_start = spearman(lifts, cov_starts)
    rho_size = spearman(lifts, sizes)

    # strongest findings: quotable leaders first (issue #93 requires quoted
    # evidence), then longest connected chains (most followers within window),
    # tie-broken by tightness (shortest total span)
    def chain_key(c: Chain) -> tuple[int, int, int]:
        span = (c.mentions[-1].first_date - c.mentions[0].first_date).days
        has_quote = c.mentions[0].segment_text is not None
        return (0 if has_quote else 1, -len(c.edges), span)

    findings = sorted((c for c in data.chains if c.edges), key=chain_key)[:top_findings]

    p = data.params
    lines: list[str] = []
    lines.append("# Who Leads the AI-Coding Conversation - Coverage-Corrected Lead-Lag Report")
    lines.append("")
    lines.append(f"Generated: {dt.date.today().isoformat()} | Issue #93 | Substrate: DuckDB truth store (PR #86)")
    lines.append("")
    lines.append(
        f"Corpus: {sum(c.n_artifacts for c in data.coverage.values())} artifacts across "
        f"{len(data.coverage)} creators (>= {p['min_artifacts']} artifacts each); "
        f"{data.n_concepts_total} concepts total, {data.n_concepts_eligible} pass the adoption + eligibility "
        f"filters (adopted by >= {p['min_adopters']} creators, >= {p['min_eligible']} of them with coverage "
        f"active at emergence)."
    )
    lines.append("")
    lines.append("## Method in one paragraph")
    lines.append("")
    lines.append(
        "First-mention dates come from `has_concept -> artifacts.published_at`. The confound: creators entered "
        "the corpus with different lookback depths, so a deep-backfill channel is 'first' on anything that "
        "emerged before the others were indexed. Correction (minimal form of arXiv:1009.0119): (1) a concept "
        "only counts if enough adopters' coverage windows were active at its emergence; (2) expected firsts are "
        "proportional to posting rate among those eligible adopters, so `lift = observed firsts / expected "
        "firsts` rewards leading beyond volume-implied chance. Lift > 1 means the creator is first more often "
        "than their posting volume predicts."
    )
    lines.append("")
    lines.append("## Corpus coverage windows (the confound, stated)")
    lines.append("")
    lines.append("| Creator | Coverage start | Coverage end | Artifacts | Rate/day |")
    lines.append("|---|---|---|---|---|")
    for c in sorted(data.coverage.values(), key=lambda c: c.start):
        lines.append(f"| {c.source_id} | {c.start} | {c.end} | {c.n_artifacts} | {c.rate:.3f} |")
    lines.append("")
    lines.append("## Corrected leader ranking (precursor lift)")
    lines.append("")
    lines.append("| # | Creator | Lift | Firsts (obs) | Firsts (expected) | Eligible concepts | Mean lag (days) |")
    lines.append("|---|---|---|---|---|---|---|")
    for i, s in enumerate(ranked, 1):
        lines.append(
            f"| {i} | {s.source_id} | {s.lift:.2f} | {s.firsts:.1f} | {s.expected:.1f} "
            f"| {s.eligible_concepts} | {s.mean_lag_days:.0f} |"
        )
    lines.append("")
    lines.append("## Naive ranking (uncorrected, for contrast)")
    lines.append("")
    lines.append("| # | Creator | Naive firsts |")
    lines.append("|---|---|---|")
    for i, (src, n) in enumerate(naive_ranked[:15], 1):
        lines.append(f"| {i} | {src} | {n:.1f} |")
    lines.append("")
    lines.append("## Kill-criterion diagnostics")
    lines.append("")
    lines.append(
        f"- Spearman(corrected lift, coverage-start date): **{rho_start:+.2f}** "
        "(strongly negative = older-indexed channels still dominate = corpus artifact)"
    )
    lines.append(
        f"- Spearman(corrected lift, corpus size): **{rho_size:+.2f}** "
        "(strongly positive = biggest channels still dominate = corpus artifact)"
    )
    lines.append(
        "- Issue #93 kill criterion: if, after coverage correction, the leaders are just the biggest / "
        "oldest-indexed channels, the influence signal is not there."
    )
    lines.append("")
    lines.append(f"## Top {len(findings)} findings (adoption chains with evidence)")
    lines.append("")
    for i, chain in enumerate(findings, 1):
        leader = chain.mentions[0]
        lines.append(f"### {i}. `{chain.concept_id}`")
        lines.append("")
        lines.append(f"Chain: {_chain_line(chain)}")
        lines.append("")
        lines.append(f"Leader evidence ({leader.source_id} first on {leader.first_date}):")
        lines.append("")
        lines.append(_evidence_line(leader))
        lines.append("")
    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- This corpus is ~100x smaller than the studies the method comes from: every row above is a lead for "
        "manual inspection, not a verdict (issue #95 guardrail)."
    )
    lines.append(
        "- `published_at` is upload date; a concept discussed in a members-only or unindexed video earlier is "
        "invisible. Eligibility bounds this but cannot eliminate it."
    )
    lines.append(
        "- Concept extraction granularity is uneven (issue #85 lineage); a chain over a generic concept "
        "(e.g. 'ai agents') is weaker evidence than one over a specific pattern."
    )
    lines.append(
        "- Small-sample lifts: a creator with expected firsts < 2 can post an extreme lift from one or two "
        "lucky firsts. Read the lift column together with the observed/expected columns; lifts backed by "
        "expected >= 5 are the trustworthy ones."
    )
    lines.append(
        "- Quotes are located by searching the leader's transcript for the extracted term (verbatim, then "
        "entity-link, then token match). A token-matched quote can set the topic's context rather than land on "
        "the exact utterance; the timestamped link is the ground truth."
    )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coverage-corrected lead-lag report (issue #93)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"DuckDB path (default {DEFAULT_DB})")
    parser.add_argument("--out", help="write markdown report here (default: stdout)")
    parser.add_argument("--min-adopters", type=int, default=MIN_ADOPTERS_DEFAULT)
    parser.add_argument("--min-eligible", type=int, default=MIN_ELIGIBLE_DEFAULT)
    parser.add_argument("--min-artifacts", type=int, default=MIN_ARTIFACTS_DEFAULT)
    parser.add_argument("--follow-window-days", type=int, default=FOLLOW_WINDOW_DAYS_DEFAULT)
    parser.add_argument("--top", type=int, default=TOP_FINDINGS_DEFAULT, help="number of findings to render")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        import duckdb
    except ImportError:
        log.error("duckdb not installed. Run: pip install duckdb")
        sys.exit(1)
    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DuckDB store not found at %s. Build it first: python scripts/intel_graph.py load", db_path)
        sys.exit(1)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        data = build_report_data(
            con,
            min_adopters=args.min_adopters,
            min_eligible=args.min_eligible,
            min_artifacts=args.min_artifacts,
            follow_window_days=args.follow_window_days,
        )
    finally:
        con.close()
    report = render_report(data, top_findings=args.top)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        log.info("Report written to %s", out)
    else:
        print(report)


if __name__ == "__main__":
    main()
