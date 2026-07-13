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
   posting rate among the rankable eligible adopters, so prolific channels only
   score when they beat their volume-implied chance. Precursor lift =
   observed / expected. Creators below --min-artifacts are observed (their
   first mentions set emergence dates and block false credit) but not ranked
   (their rates are too noisy to compete on).
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
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from intel_graph import timestamped_url

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("lead_lag_report")

DEFAULT_DB = Path.home() / ".cache" / "video-intel" / "intel.duckdb"
MIN_ADOPTERS_DEFAULT = 4  # concept must be adopted by >= N creators (issue #93 spec)
MIN_ELIGIBLE_DEFAULT = 3  # concept must have >= N adopters whose coverage was active at emergence
MIN_ARTIFACTS_DEFAULT = 5  # creators below this corpus size are observed but not ranked
MIN_RANKED_CONCEPTS_DEFAULT = 5  # creators with fewer eligible concepts are omitted from the ranked table
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
        """Artifacts per active day (inclusive window); the volume-implied chance of being first."""
        return self.n_artifacts / ((self.end - self.start).days + 1)


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
    win_probs: list[float] = field(default_factory=list)  # per-concept null win prob (Spec A.2)

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
    rankable: frozenset[str]
    stats: dict[str, CreatorStats]
    naive: dict[str, float]
    chains: list[Chain]
    n_concepts_total: int
    n_concepts_eligible: int
    params: dict[str, int]


def _eligible(mentions: list[FirstMention], coverage: dict[str, Coverage]) -> list[FirstMention]:
    """Adopters whose coverage window was already active at the concept's emergence."""
    known = [m for m in sorted(mentions, key=lambda m: m.first_date) if m.source_id in coverage]
    if not known:
        return []
    emergence = known[0].first_date
    return [m for m in known if coverage[m.source_id].start <= emergence]


def precursor_stats(
    first_mentions: dict[str, list[FirstMention]],
    coverage: dict[str, Coverage],
    min_eligible: int,
    rankable: frozenset[str] | None = None,
) -> dict[str, CreatorStats]:
    """Coverage-corrected, rate-normalized precursor statistics per creator.

    The leader date is taken over ALL eligible adopters, including creators
    outside `rankable` - so a sub-threshold true first-mover blocks false
    credit instead of silently crowning the second adopter. Only rankable
    creators accrue firsts/expected/lag (their concept simply goes unwon when
    a sub-threshold creator led it).
    """
    stats: dict[str, CreatorStats] = {}
    for mentions in first_mentions.values():
        eligible = _eligible(mentions, coverage)
        if len(eligible) < min_eligible:
            continue
        ranked_eligible = [m for m in eligible if rankable is None or m.source_id in rankable]
        if not ranked_eligible:
            continue
        total_rate = sum(coverage[m.source_id].rate for m in ranked_eligible)
        if total_rate <= 0:
            continue
        leader_date = eligible[0].first_date
        tied = [m for m in eligible if m.first_date == leader_date]
        for m in ranked_eligible:
            s = stats.setdefault(m.source_id, CreatorStats(source_id=m.source_id))
            s.eligible_concepts += 1
            win_prob = coverage[m.source_id].rate / total_rate
            s.expected += win_prob
            s.win_probs.append(win_prob)  # Spec A.2: the null's per-concept win prob
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


def poisson_binomial_sf(probs: list[float], threshold: float) -> float:
    """P(X >= threshold) where X = sum of independent Bernoulli(p) over probs.

    Exact tail via DP convolution over the success-count distribution (each
    creator has <= ~60 eligible concepts, so this is trivial and deterministic).
    X is integer-valued, so P(X >= threshold) sums the mass at ceil(threshold)
    and above.
    """
    dist = [1.0]
    for p in probs:
        p = min(1.0, max(0.0, p))
        nxt = [0.0] * (len(dist) + 1)
        for k, pk in enumerate(dist):
            nxt[k] += pk * (1 - p)
            nxt[k + 1] += pk * p
        dist = nxt
    lo = max(0, math.ceil(threshold - 1e-9))
    return sum(dist[lo:]) if lo < len(dist) else 0.0


def firsts_significance(ranked: list[CreatorStats]) -> dict[str, tuple[float, float]]:
    """Per-creator significance of observed firsts vs the rate-proportional null (Spec A.2).

    Null: each eligible concept independently awards its single first slot to
    creator i with probability rate_i / sum(rates of that concept's rankable
    eligible adopters) - exactly the expectation the lift already uses. A
    creator's total firsts is then a Poisson-binomial over its per-concept win
    probabilities; p = P(firsts >= observed) is the closed-form tail (the
    acceptable substitute for the 10,000-draw permutation named in Spec A.2).
    Same-date co-leaders keep the fractional 1/k credit in the observed
    statistic (CreatorStats.firsts), so observed and the integer-valued null
    are compared on the same "each concept contributes 1.0 of first-credit"
    footing. Returns {source_id: (p_value, bh_q_value)} over the ranked set.
    """
    p_values = {s.source_id: poisson_binomial_sf(s.win_probs, s.firsts) for s in ranked}
    order = sorted(ranked, key=lambda s: p_values[s.source_id])
    m = len(order)
    q_values: dict[str, float] = {}
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        s = order[rank]
        prev = min(prev, p_values[s.source_id] * m / (rank + 1))
        q_values[s.source_id] = prev
    return {s.source_id: (p_values[s.source_id], q_values[s.source_id]) for s in ranked}


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
        if len(eligible) < max(1, min_eligible):
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
    """A single-line excerpt around the first occurrence of term (or the head).

    Dashes are normalized to '-' (repo-wide no-em-dash rule; the timestamped
    link, not the excerpt, is the ground truth).
    """
    flat = " ".join(text.split()).replace(chr(0x2014), "-").replace(chr(0x2013), "-")
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


def load_coverage(con: DuckDBPyConnection) -> dict[str, Coverage]:
    """Coverage windows for ALL creators - the rankable cut happens downstream.

    Filtering here would erase sub-threshold creators' observations entirely,
    letting a second adopter inherit false first-mover credit.
    """
    rows = con.execute(
        """
        SELECT source_id, MIN(published_at), MAX(published_at), COUNT(*)
        FROM artifacts
        WHERE published_at IS NOT NULL
        GROUP BY source_id
        """
    ).fetchall()
    return {r[0]: Coverage(source_id=r[0], start=r[1], end=r[2], n_artifacts=r[3]) for r in rows}


def load_first_mentions(con: DuckDBPyConnection, min_adopters: int) -> tuple[dict[str, list[FirstMention]], int]:
    """First mention per (concept, creator), with the best evidence row attached.

    Evidence preference: a grounded row with a segment timestamp beats an
    ungrounded mindmap-level row, because it yields a quotable excerpt and a
    &t= deep link.
    """
    rows = con.execute(
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
    n_total = con.execute("SELECT COUNT(DISTINCT concept_id) FROM has_concept").fetchone()[0]
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


def backfill_evidence(con: DuckDBPyConnection, chains: list[Chain]) -> list[Chain]:
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
        if not chain.mentions:
            result.append(chain)
            continue
        leader = chain.mentions[0]
        if leader.segment_text is None:
            row = _find_evidence_segment(con, leader)
            if row is not None:
                leader = dataclasses.replace(leader, start_seconds=row[0], segment_text=row[1])
                result.append(Chain(chain.concept_id, (leader, *chain.mentions[1:]), chain.edges))
                continue
        result.append(chain)
    return result


def _find_evidence_segment(con: DuckDBPyConnection, m: FirstMention) -> tuple[int | None, str] | None:
    term = m.as_mentioned.lower().strip()
    if term:
        row = con.execute(
            "SELECT start_seconds, text FROM segments WHERE artifact_id = ? AND contains(lower(text), ?)"
            " ORDER BY start_seconds ASC NULLS LAST LIMIT 1",
            [m.artifact_id, term],
        ).fetchone()
        if row is not None:
            return row
    row = con.execute(
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
        row = con.execute(
            f"SELECT start_seconds, text FROM segments WHERE artifact_id = ? AND {clause}"
            " ORDER BY start_seconds ASC NULLS LAST LIMIT 1",
            [m.artifact_id, *tokens],
        ).fetchone()
        if row is not None:
            return row
    return None


def build_report_data(
    con: DuckDBPyConnection,
    min_adopters: int = MIN_ADOPTERS_DEFAULT,
    min_eligible: int = MIN_ELIGIBLE_DEFAULT,
    min_artifacts: int = MIN_ARTIFACTS_DEFAULT,
    follow_window_days: int = FOLLOW_WINDOW_DAYS_DEFAULT,
) -> ReportData:
    coverage = load_coverage(con)
    rankable = frozenset(s for s, c in coverage.items() if c.n_artifacts >= min_artifacts)
    first_mentions, n_total = load_first_mentions(con, min_adopters)
    stats = precursor_stats(first_mentions, coverage, min_eligible, rankable)
    chains = backfill_evidence(con, adoption_chains(first_mentions, coverage, min_eligible, follow_window_days))
    return ReportData(
        coverage=coverage,
        rankable=rankable,
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


def ranked_creators(data: ReportData, min_ranked_concepts: int) -> list[CreatorStats]:
    """Shared ranked-table selection for the report AND the HTML renderer.

    Lives here so the two surfaces cannot drift (PR #97 review): any change to
    the filter or sort updates both in one place.
    """
    return sorted(
        (s for s in data.stats.values() if s.eligible_concepts >= min_ranked_concepts),
        key=lambda s: s.lift,
        reverse=True,
    )


def finding_chains(data: ReportData, top_findings: int) -> list[Chain]:
    """Shared findings selection: quotable leaders first, then most followers, then tightest span."""

    def chain_key(c: Chain) -> tuple[int, int, int]:
        span = (c.mentions[-1].first_date - c.mentions[0].first_date).days
        has_quote = c.mentions[0].segment_text is not None
        return (0 if has_quote else 1, -len(c.edges), span)

    return sorted((c for c in data.chains if c.edges), key=chain_key)[:top_findings]


def _evidence_line(m: FirstMention) -> str:
    link = timestamped_url(m.url, m.start_seconds if "youtube.com" in m.url else None)
    quote = extract_quote(m.segment_text, m.as_mentioned) if m.segment_text else "(mindmap-level mention, no segment)"
    return f'> "{quote}"\n>\n> - {m.source_id}, [{m.title}]({link}), {m.first_date.isoformat()}'


def _chain_line(chain: Chain) -> str:
    hops = " -> ".join(f"{m.source_id}({m.first_date.strftime('%y-%m-%d')})" for m in chain.mentions)
    return hops


def render_report(
    data: ReportData,
    top_findings: int = TOP_FINDINGS_DEFAULT,
    min_ranked_concepts: int = MIN_RANKED_CONCEPTS_DEFAULT,
) -> str:
    ranked = ranked_creators(data, min_ranked_concepts)
    # count against rankable, not stats: a rankable creator with ZERO eligible
    # concepts never enters stats but is still omitted from the table (Codex
    # peer-review finding on PR #96)
    omitted = len(data.rankable) - len(ranked)
    naive_ranked = sorted(data.naive.items(), key=lambda kv: kv[1], reverse=True)

    # kill-criterion diagnostics over creators present in the corrected ranking
    if len(ranked) >= 2:
        lifts = [s.lift for s in ranked]
        cov_starts = [float(data.coverage[s.source_id].start.toordinal()) for s in ranked]
        sizes = [float(data.coverage[s.source_id].n_artifacts) for s in ranked]
        rho_start_s = f"{spearman(lifts, cov_starts):+.2f}"
        rho_size_s = f"{spearman(lifts, sizes):+.2f}"
    else:
        rho_start_s = rho_size_s = "n/a (fewer than 2 ranked creators - diagnostics undefined)"

    findings = finding_chains(data, top_findings)

    p = data.params
    lines: list[str] = []
    lines.append("# Who Leads the AI-Coding Conversation - Coverage-Corrected Lead-Lag Report")
    lines.append("")
    lines.append(f"Generated: {dt.date.today().isoformat()} | Issue #93 | Substrate: DuckDB truth store (PR #86)")
    lines.append("")
    lines.append(
        f"Corpus: {sum(c.n_artifacts for c in data.coverage.values())} artifacts across "
        f"{len(data.coverage)} creators ({len(data.rankable)} rankable at >= {p['min_artifacts']} artifacts); "
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
        "than their posting volume predicts. Creators below the artifact floor still set emergence dates (so "
        "nobody inherits a first they did not earn) but are not themselves ranked."
    )
    lines.append("")
    lines.append("## Corpus coverage windows (the confound, stated)")
    lines.append("")
    lines.append("| Creator | Coverage start | Coverage end | Artifacts | Rate/day | Ranked |")
    lines.append("|---|---|---|---|---|---|")
    for c in sorted(data.coverage.values(), key=lambda c: c.start):
        ranked_mark = "yes" if c.source_id in data.rankable else "no"
        lines.append(f"| {c.source_id} | {c.start} | {c.end} | {c.n_artifacts} | {c.rate:.3f} | {ranked_mark} |")
    lines.append("")
    lines.append("## Corrected leader ranking (precursor lift)")
    lines.append("")
    lines.append(
        f"Creators shown: >= {p['min_artifacts']} artifacts and >= {min_ranked_concepts} eligible concepts "
        f"({omitted} rankable creators omitted for too few eligible concepts)."
    )
    lines.append("")
    sig = firsts_significance(ranked)
    n_clearing = sum(1 for _, q in sig.values() if q < 0.05)
    lines.append(
        "| # | Creator | Lift | Firsts (obs) | Firsts (expected) | Eligible concepts | Mean lag (days) | p (perm) |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for i, s in enumerate(ranked, 1):
        p_value, q_value = sig[s.source_id]
        # raw permutation p is shown (that is what `p (perm)` means); the `*`
        # marks the rows that still clear after BH correction, so nobody reads a
        # raw p < 0.05 as significant when the multiple-comparison-corrected q is
        # not (Codex peer-review finding, PR #111).
        mark = " *" if q_value < 0.05 else ""
        lines.append(
            f"| {i} | {s.source_id} | {s.lift:.2f} | {s.firsts:.1f} | {s.expected:.1f} "
            f"| {s.eligible_concepts} | {s.mean_lag_days:.0f} | {p_value:.4f}{mark} |"
        )
    lines.append("")
    lines.append(
        f"`p (perm)` (Spec A.2): the RAW P(firsts >= observed) under the rate-proportional null - each "
        f"concept's single first slot goes to a rankable eligible adopter with probability proportional to its "
        f"posting rate (the closed-form Poisson-binomial tail of the 10,000-draw permutation). A trailing `*` "
        f"marks the **{n_clearing} of {len(ranked)}** ranked creators that still clear p < 0.05 AFTER "
        "Benjamini-Hochberg correction (the raw p alone is not multiple-comparison safe). A small-sample "
        "creator can clear this rate-null and still be a coverage artifact - the column tests 'beyond "
        "volume-implied luck', not 'beyond every confound'; read it with the small-sample caveat below."
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
        f"- Spearman(corrected lift, coverage-start date): **{rho_start_s}**. Negative means earlier-indexed "
        "channels still rank higher (coverage artifact); near zero means the correction removed the "
        "indexing-age effect."
    )
    lines.append(
        f"- Spearman(corrected lift, corpus size): **{rho_size_s}**. Positive means bigger channels still rank "
        "higher (popularity artifact); negative means smaller channels out-lead their posting volume."
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
        tied_with = [x.source_id for x in chain.mentions[1:] if x.first_date == leader.first_date]
        leader_label = f"{leader.source_id} first on {leader.first_date}"
        if tied_with:
            leader_label += f", tied with {', '.join(tied_with)}"
        lines.append(f"### {i}. `{chain.concept_id}`")
        lines.append("")
        lines.append(f"Chain: {_chain_line(chain)}")
        lines.append("")
        lines.append(f"Leader evidence ({leader_label}):")
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


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Coverage-corrected lead-lag report (issue #93)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"DuckDB path (default {DEFAULT_DB})")
    parser.add_argument("--out", help="write markdown report here (default: stdout)")
    parser.add_argument(
        "--min-adopters",
        type=_positive_int,
        default=MIN_ADOPTERS_DEFAULT,
        help=f"concept must be adopted by >= N creators to be analyzed (default {MIN_ADOPTERS_DEFAULT})",
    )
    parser.add_argument(
        "--min-eligible",
        type=_positive_int,
        default=MIN_ELIGIBLE_DEFAULT,
        help="concept must have >= N adopters whose coverage was active at emergence, else 'first' is a "
        f"corpus artifact (default {MIN_ELIGIBLE_DEFAULT})",
    )
    parser.add_argument(
        "--min-artifacts",
        type=_positive_int,
        default=MIN_ARTIFACTS_DEFAULT,
        help="creators below this corpus size are observed (set emergence dates) but not ranked "
        f"(default {MIN_ARTIFACTS_DEFAULT})",
    )
    parser.add_argument(
        "--min-ranked-concepts",
        type=_positive_int,
        default=MIN_RANKED_CONCEPTS_DEFAULT,
        help="hide creators with fewer eligible concepts from the ranked table and diagnostics "
        f"(default {MIN_RANKED_CONCEPTS_DEFAULT})",
    )
    parser.add_argument(
        "--follow-window-days",
        type=_positive_int,
        default=FOLLOW_WINDOW_DAYS_DEFAULT,
        help=f"max days between mentions for an 'A leads, B follows' edge (default {FOLLOW_WINDOW_DAYS_DEFAULT})",
    )
    parser.add_argument("--top", type=_positive_int, default=TOP_FINDINGS_DEFAULT, help="number of findings to render")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    try:
        import duckdb
    except ImportError:
        log.error("duckdb not installed. Run: pip install 'video-intel[intelligence]'")
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
    if data.n_concepts_eligible == 0:
        log.warning(
            "no concepts passed the adoption + eligibility filters - the report will be empty; "
            "check --db points at a loaded store and consider lowering --min-adopters/--min-eligible"
        )
    report = render_report(data, top_findings=args.top, min_ranked_concepts=args.min_ranked_concepts)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        log.info("Report written to %s", out)
    else:
        print(report)


if __name__ == "__main__":
    main()
