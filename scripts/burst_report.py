#!/usr/bin/env python3
"""Concept burst detection over the DuckDB truth store (issue #103).

Kleinberg's two-state burst model (KDD 2002) over each concept's mention
stream: events are the DISTINCT videos mentioning a concept, ordered by
publish date. A burst is a maximal run where the inter-arrival gaps are
better explained by an elevated rate (s x the concept's own baseline) than
by the baseline, with a one-time entry cost (gamma * ln n) that keeps noise
out. This finds "just caught fire" - a rate jump against the concept's OWN
history - not "popular overall", and it is the principled sequential upgrade
of PR #62's fixed-window spike_ratio: it yields a start date, an intensity,
and a rising/cooled status instead of a window ratio.

Corpus caveat (issue #103): this corpus is ~100x smaller than the
literature's; outputs are leads for inspection, not verdicts.

One read-only report script over intel.duckdb, not a framework (#95
guardrail). No Gemini calls.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lead_lag_report import DEFAULT_DB

from timestamp_utils import timestamped_url

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

MIN_EVENTS_DEFAULT = 6  # below this, a "burst" is indistinguishable from noise
S_DEFAULT = 2.0  # burst rate multiplier (Kleinberg's s)
GAMMA_DEFAULT = 1.0  # burst entry cost multiplier (Kleinberg's gamma)
RISING_WINDOW_DAYS = 45  # a burst reaching the corpus edge within this window is "still rising"
MIN_GAP_DAYS = 0.5  # same-day events: clamp gap so exponential densities stay finite
TOP_DEFAULT = 20


@dataclass(frozen=True)
class Event:
    date: dt.date
    source_id: str
    title: str
    url: str


@dataclass(frozen=True)
class Burst:
    concept_id: str
    start: dt.date
    end: dt.date
    weight: float
    n_events: int
    rising: bool
    first_event: Event


def kleinberg_burst_spans(gaps: list[float], n: int, s: float, gamma: float) -> list[tuple[int, int, float]]:
    """Optimal two-state labeling of inter-arrival gaps; returns (i, j, weight) burst runs.

    States: 0 = baseline rate lam, 1 = burst rate s*lam. Cost of a gap g under
    rate r is the negative log of the exponential density, r*g - ln(r).
    Entering the burst state costs gamma * ln(n); leaving is free (Kleinberg
    2002, two-state automaton). Viterbi over the gap sequence.
    """
    if not gaps:
        return []
    total = sum(gaps)
    lam = len(gaps) / total if total > 0 else 1.0
    rates = (lam, s * lam)
    enter_cost = gamma * math.log(n) if n > 1 else 0.0

    def gap_cost(rate: float, g: float) -> float:
        return rate * g - math.log(rate)

    cost = [gap_cost(rates[0], gaps[0]), enter_cost + gap_cost(rates[1], gaps[0])]
    back: list[tuple[int, int]] = [(0, 1)]
    for g in gaps[1:]:
        c0 = min(cost[0], cost[1])
        p0 = 0 if cost[0] <= cost[1] else 1
        c1 = min(cost[0] + enter_cost, cost[1])
        p1 = 0 if cost[0] + enter_cost < cost[1] else 1
        cost = [c0 + gap_cost(rates[0], g), c1 + gap_cost(rates[1], g)]
        back.append((p0, p1))

    state = 0 if cost[0] <= cost[1] else 1
    states = [0] * len(gaps)
    for i in range(len(gaps) - 1, -1, -1):
        states[i] = state
        state = back[i][state]

    spans: list[tuple[int, int, float]] = []
    i = 0
    while i < len(states):
        if states[i] == 1:
            j = i
            while j + 1 < len(states) and states[j + 1] == 1:
                j += 1
            weight = sum(gap_cost(rates[0], gaps[k]) - gap_cost(rates[1], gaps[k]) for k in range(i, j + 1))
            spans.append((i, j, weight))
            i = j + 1
        else:
            i += 1
    return spans


def detect_bursts(
    events_by_concept: dict[str, list[Event]],
    corpus_end: dt.date,
    min_events: int = MIN_EVENTS_DEFAULT,
    s: float = S_DEFAULT,
    gamma: float = GAMMA_DEFAULT,
) -> list[Burst]:
    bursts: list[Burst] = []
    for concept_id, events in events_by_concept.items():
        if len(events) < min_events:
            continue
        gaps = [max(MIN_GAP_DAYS, (b.date - a.date).days) for a, b in itertools.pairwise(events)]
        for i, j, weight in kleinberg_burst_spans(gaps, len(events), s, gamma):
            # gap k sits between events k and k+1: the burst covers events i..j+1
            start_ev, end_ev = events[i], events[j + 1]
            rising = j + 1 == len(events) - 1 and (corpus_end - end_ev.date).days <= RISING_WINDOW_DAYS
            bursts.append(
                Burst(
                    concept_id=concept_id,
                    start=start_ev.date,
                    end=end_ev.date,
                    weight=weight,
                    n_events=j - i + 2,
                    rising=rising,
                    first_event=start_ev,
                )
            )
    return sorted(bursts, key=lambda b: (not b.rising, -b.start.toordinal(), -b.weight))


def load_events(con: DuckDBPyConnection) -> tuple[dict[str, list[Event]], dt.date, int]:
    """Returns (events per concept, corpus end date, dropped undated rows).

    corpus_end comes from ALL dated artifacts, not just concept-bearing ones -
    otherwise later concept-less artifacts would never age out "rising" bursts.
    Undated rows are counted, not silently dropped (Codex review, PR #107).
    """
    rows = con.execute(
        """
        SELECT hc.concept_id, a.published_at::DATE AS d, a.source_id, a.title, a.url
        FROM (SELECT DISTINCT artifact_id, concept_id FROM has_concept) hc
        JOIN artifacts a USING (artifact_id)
        WHERE a.published_at IS NOT NULL
        ORDER BY hc.concept_id, d, a.artifact_id
        """
    ).fetchall()
    dropped = con.execute(
        """
        SELECT count(*) FROM (SELECT DISTINCT artifact_id, concept_id FROM has_concept) hc
        JOIN artifacts a USING (artifact_id) WHERE a.published_at IS NULL
        """
    ).fetchone()[0]
    end_row = con.execute("SELECT max(published_at::DATE) FROM artifacts WHERE published_at IS NOT NULL").fetchone()
    corpus_end = end_row[0] if end_row and end_row[0] else dt.date.min
    events: dict[str, list[Event]] = {}
    for concept_id, d, source_id, title, url in rows:
        events.setdefault(concept_id, []).append(Event(d, source_id, title or "", url or ""))
    return events, corpus_end, dropped


def load_monthly_volume(con: DuckDBPyConnection, months: int = 12) -> list[tuple[str, int]]:
    """Corpus-wide artifacts per month - the confounder readers must see.

    Kleinberg measures each concept against its OWN baseline, but every
    stream rides the corpus's indexing volume: a scan surge makes many
    concepts "burst" simultaneously. This table lets the reader separate
    "topic caught fire" from "corpus grew".
    """
    rows = con.execute(
        """
        SELECT strftime(published_at, '%Y-%m') AS month, count(DISTINCT artifact_id) AS n
        FROM artifacts WHERE published_at IS NOT NULL
        GROUP BY month ORDER BY month DESC LIMIT ?
        """,
        [months],
    ).fetchall()
    return sorted(rows)


def _burst_line(b: Burst) -> str:
    status = "still rising" if b.rising else f"cooled {b.end.isoformat()}"
    link = timestamped_url(b.first_event.url, None) if b.first_event.url else ""
    first = (
        f"first in burst: {b.first_event.source_id}, [{b.first_event.title}]({link}), {b.first_event.date.isoformat()}"
        if link
        else f"first in burst: {b.first_event.source_id}, {b.first_event.date.isoformat()}"
    )
    return (
        f"- **{b.concept_id}** - began {b.start.isoformat()}, {status} "
        f"(intensity {b.weight:.1f}, {b.n_events} videos) - {first}"
    )


def render_report(
    bursts: list[Burst],
    corpus_end: dt.date,
    params: dict[str, float],
    top: int,
    monthly_volume: list[tuple[str, int]] | None = None,
    dropped_undated: int = 0,
) -> str:
    rising = [b for b in bursts if b.rising]
    cooled = [b for b in bursts if not b.rising][: max(0, top - len(rising[:top]))]
    lines = [
        "# Concept burst report",
        "",
        f"Kleinberg two-state burst detection over per-concept video streams (issue #103). Corpus end: {corpus_end.isoformat()}. Params: {params}. A burst is a rate jump against the concept's OWN baseline - 'just caught fire', not 'popular overall'. Intensity is the burst run's log-likelihood advantage over that concept's own baseline: it is NOT comparable across concepts with different baselines - use it to rank a concept's bursts against each other, not concept vs concept. Same-day videos are spaced at the min_gap_days floor before rate fitting. This corpus is small; every row is a lead for inspection, not a verdict.",
        "",
    ]
    if dropped_undated:
        lines += [
            f"> {dropped_undated} concept-video rows were excluded for missing publish dates - their absence can shift gaps and rising status for the affected concepts.",
            "",
        ]
    if monthly_volume:
        lines += [
            "## Corpus volume context (read this first)",
            "",
            f"Every concept stream rides the corpus's indexing volume: when the corpus itself grows, many concepts 'burst' at once. {len(rising)} of {len(bursts)} bursts are currently rising - before reading any single row as a topic catching fire, check whether its start date coincides with a volume surge below.",
            "",
            "| Month | Videos published |",
            "|---|---|",
        ]
        lines += [f"| {m} | {n} |" for m, n in monthly_volume]
        lines.append("")
    lines += [
        f"## Bursting now ({len(rising)})",
        "",
    ]
    lines += [_burst_line(b) for b in rising[:top]] or ["(none)"]
    if len(rising) > top:
        lines.append(f"- ...and {len(rising) - top} more rising bursts (raise --top)")
    n_cooled = len(bursts) - len(rising)
    lines += ["", f"## Recent bursts, cooled ({n_cooled})", ""]
    # "(none)" only when none EXIST - a top-budget that empties the listing
    # must not read as "no cooled bursts"
    lines += [_burst_line(b) for b in cooled] or (["(none)"] if n_cooled == 0 else [])
    hidden = n_cooled - len(cooled)
    if hidden > 0:
        lines.append(f"- ...and {hidden} more cooled bursts (raise --top)")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Kleinberg concept-burst report over intel.duckdb (issue #103).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path to intel.duckdb (read-only)")
    parser.add_argument("--out", type=Path, default=None, help="Write markdown here (default: stdout)")
    parser.add_argument("--min-events", type=int, default=MIN_EVENTS_DEFAULT)
    parser.add_argument("--s", type=float, default=S_DEFAULT, help="Burst rate multiplier")
    parser.add_argument("--gamma", type=float, default=GAMMA_DEFAULT, help="Burst entry cost multiplier")
    parser.add_argument("--top", type=int, default=TOP_DEFAULT, help="Max bursts listed per report")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"store not found: {args.db} (build it with scripts/intel_graph.py load)")
    import duckdb

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        events, corpus_end, dropped_undated = load_events(con)
        monthly_volume = load_monthly_volume(con)
    finally:
        con.close()

    bursts = detect_bursts(events, corpus_end, min_events=args.min_events, s=args.s, gamma=args.gamma)
    report = render_report(
        bursts,
        corpus_end,
        {"min_events": args.min_events, "s": args.s, "gamma": args.gamma, "min_gap_days": MIN_GAP_DAYS},
        args.top,
        monthly_volume=monthly_volume,
        dropped_undated=dropped_undated,
    )
    if args.out is None:
        print(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out} ({len(bursts)} bursts, {sum(b.rising for b in bursts)} rising)")


if __name__ == "__main__":
    main()
