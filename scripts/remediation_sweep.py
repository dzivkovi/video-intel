#!/usr/bin/env python
"""Corpus-wide transcript quality sweep - the worklist builder for issue #172.

Read-only against the corpus except for the report it writes under
``_reports/``. Makes no Gemini calls. Optionally makes cheap YouTube
``videos.list`` calls (1 quota unit per 50 videos) to recover durations.

WHY THIS RE-DERIVES RATHER THAN QUERIES
---------------------------------------
Issue #172's plan says the runner "reads ``transcript_quality_flags`` /
``transcript_status`` across the corpus". Measured 2026-08-31: **zero** of the
2097 metas carry ``transcript_quality_flags`` at all, and ``transcript_status``
yields about 26 non-healthy files, not the ~274 the remediation is sized for.
The #157/#158 quality machinery only stamps those fields on transcripts written
AFTER it shipped, and nothing has been re-transcribed since. So the buckets have
to be re-derived by running the real assessor over the on-disk transcripts.
That is free and local.

It re-derives through ``assess_transcript_artifact`` - the SAME function the
writers use - never a local reimplementation of severity. This repo's standing
rule is that a verifier must use the writer's path (the PR #136 guardrail); a
sweep that scored quality its own way would disagree with the pipeline the
moment either changed, and this one decides where real money gets spent.

DURATION IS THE WHOLE BALLGAME
------------------------------
Without a duration the assessor cannot compute gap or density, and its
monolithic check is gated on a window > 300s - so a 12-minute video whose
entire dialogue collapsed into ONE entry reports ``severe: []`` and reads as
CLEAN. Measured: 46% of corpus transcripts (945 of 2050) have no
``duration_seconds``. Four sources are tried in order, and which one was used
is recorded per row so an estimate is never mistaken for a measurement:

1. ``duration_seconds`` from meta.json (authoritative)
2. YouTube ``videos.list`` (authoritative; ~1 quota unit per 50 ids)
3. the last timestamp in the transcript, dialogue OR screen content (a FLOOR,
   never the true length - it under-reports trailing gaps and over-reports
   density, so it can only ever make a file look HEALTHIER than it is; that is
   the safe direction for a floor, but it means a "clean" verdict from an
   estimated duration is weaker evidence than a flagged one)
4. nothing - the row lands in the ``unassessable`` bucket and is NEVER counted
   as clean (issue #172 calls this out explicitly)

Usage:
    python scripts/remediation_sweep.py                 # sweep + write report
    python scripts/remediation_sweep.py --dry-run       # print, write nothing
    python scripts/remediation_sweep.py --no-fetch      # skip YouTube lookups
    python scripts/remediation_sweep.py --channel NAME  # one channel
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import video_intel as vi

log = logging.getLogger("remediation_sweep")

#: A dialogue line starts at column 0 with a bracketed timestamp. Screen-content
#: lines are indented and prefixed SCREEN, and must NOT be counted: the assessor
#: measures DIALOGUE coverage, and feeding it screen entries would paper over
#: exactly the monolithic collapse this sweep exists to find (a collapsed
#: transcript often still has richly timestamped screen content).
_DIALOGUE_RE = re.compile(r"^\[(\d{1,3}:\d{2}(?::\d{2})?)\]")
#: Any timestamp anywhere, used only for the duration FLOOR fallback.
_ANY_TS_RE = re.compile(r"\[(\d{1,3}:\d{2}(?::\d{2})?)(?:-(\d{1,3}:\d{2}(?::\d{2})?))?\]")

#: Buckets, in remediation priority order. Order matters: a file is reported
#: under the FIRST bucket it qualifies for, so the table has one row per file.
BUCKET_ORDER = [
    "truncated",
    "monolithic_severe",
    "blind_gap_severe",
    "backward_jump_severe",
    "chunk_window_severe",
    "other_severe",
    "unassessable",
    "mild",
    "clean",
]

_SEVERE_BUCKET_BY_FLAG = {
    "monolithic_severe": "monolithic_severe",
    "blind_gap_severe": "blind_gap_severe",
    "backward_jump_severe": "backward_jump_severe",
    "chunk_window_mismatch_severe": "chunk_window_severe",
}


def parse_dialogue_entries(text: str) -> list[dict]:
    """Dialogue entries from a rendered .transcript.md, in file order.

    Shaped like the assessor's input: dicts with a ``start`` timestamp string.
    Order is preserved deliberately - `assess_transcript_artifact` reads the
    order given for backward-jump detection and sorts its own copy for the
    gap and density metrics.
    """
    return [{"start": m.group(1)} for line in text.splitlines() if (m := _DIALOGUE_RE.match(line))]


def last_timestamp_seconds(text: str) -> int | None:
    """Highest timestamp anywhere in the transcript, as a duration FLOOR."""
    best: int | None = None
    for m in _ANY_TS_RE.finditer(text):
        for raw in (m.group(1), m.group(2)):
            if not raw:
                continue
            secs = vi.timestamp_to_seconds(raw)
            if secs is not None and (best is None or secs > best):
                best = secs
    return best


def _read_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_bytes().decode("utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_rows(output_dir: Path, only_channel: str | None = None) -> list[dict]:
    """One row per .transcript.md in the corpus. No network, no writes."""
    rows: list[dict] = []
    channel_dirs = [
        d
        for d in sorted(output_dir.iterdir())
        if d.is_dir() and not d.name.startswith((".", "_")) and (only_channel is None or d.name == only_channel)
    ]
    for channel_dir in channel_dirs:
        for tx_path in sorted(channel_dir.glob("*.transcript.md")):
            prefix = tx_path.name[: -len(".transcript.md")]
            meta = _read_meta(channel_dir / f"{prefix}.meta.json")
            try:
                text = tx_path.read_bytes().decode("utf-8", errors="replace")
            except OSError as e:
                log.warning("unreadable transcript %s (%s)", tx_path.name, e)
                continue
            duration = meta.get("duration_seconds")
            rows.append(
                {
                    "channel": channel_dir.name,
                    "prefix": prefix,
                    "video_id": meta.get("video_id") if isinstance(meta.get("video_id"), str) else None,
                    "title": meta.get("title", prefix),
                    "url": meta.get("video_url", ""),
                    "model": meta.get("model"),
                    "transcript_status": meta.get("transcript_status"),
                    "duration_seconds": duration if isinstance(duration, int) and duration > 0 else None,
                    "duration_source": "meta" if isinstance(duration, int) and duration > 0 else None,
                    "floor_seconds": last_timestamp_seconds(text),
                    "entries": parse_dialogue_entries(text),
                    "bytes": len(text),
                }
            )
    return rows


def fill_durations_from_youtube(rows: list[dict]) -> int:
    """Recover missing durations via videos.list. Returns how many were filled.

    Cheap and authoritative: 1 quota unit per 50 ids. Never writes to meta.json -
    this sweep stays read-only against the corpus, and backfilling the field is a
    separate, deliberate decision.
    """
    need = [r for r in rows if r["duration_seconds"] is None and r["video_id"]]
    if not need:
        return 0
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.warning("YOUTUBE_API_KEY not set; skipping duration recovery for %d transcript(s)", len(need))
        return 0
    try:
        youtube = vi.require_youtube()("youtube", "v3", developerKey=api_key)
    except Exception as e:
        log.warning("could not build the YouTube client (%s); skipping duration recovery", e)
        return 0

    ids = sorted({r["video_id"] for r in need})
    log.info("Recovering durations for %d video(s) via videos.list (~%d quota units)...", len(ids), -(-len(ids) // 50))
    try:
        iso_by_id = vi.enrich_with_durations(youtube, ids)
    except Exception as e:
        log.warning("duration recovery failed (%s); those rows stay unassessable", e)
        return 0

    filled = 0
    for r in need:
        secs = vi._parse_iso8601_duration(iso_by_id.get(r["video_id"]))
        if secs:
            r["duration_seconds"] = secs
            r["duration_source"] = "youtube"
            filled += 1
    return filled


def classify(row: dict) -> dict:
    """Assess one row and assign its bucket. Pure; mutates nothing on disk."""
    duration = row["duration_seconds"]
    if duration is None and row["floor_seconds"]:
        # A floor can only make a file look healthier, never worse, so it is
        # safe to assess against - but the row records that it is an estimate.
        duration = row["floor_seconds"]
        row["duration_source"] = "timestamp_floor"

    assessment = vi.assess_transcript_artifact(row["entries"], duration)
    severe = list(assessment.get("severe") or [])
    mild = list(assessment.get("mild") or [])

    row["assessed_duration_seconds"] = duration
    row["dialogue_entries"] = assessment.get("dialogue_entries", 0)
    row["max_blind_gap_seconds"] = assessment.get("max_blind_gap_seconds")
    row["last_dialogue_fraction"] = assessment.get("last_dialogue_fraction")
    row["severe"] = severe
    row["mild"] = mild

    status = row.get("transcript_status")
    if status == getattr(vi, "TRANSCRIPT_STATUS_TRUNCATED", "truncated_output"):
        row["bucket"] = "truncated"
        return row
    for flag in severe:
        if flag in _SEVERE_BUCKET_BY_FLAG:
            row["bucket"] = _SEVERE_BUCKET_BY_FLAG[flag]
            return row
    if severe:
        row["bucket"] = "other_severe"
        return row
    if duration is None:
        # NEVER "clean". Without a duration the assessor's gap, density and
        # monolithic checks are all gated off, so a total collapse reports
        # severe: [] - the exact failure issue #172 says must not read as clean.
        row["bucket"] = "unassessable"
        return row
    row["bucket"] = "mild" if mild else "clean"
    return row


def load_briefed_video_ids(output_dir: Path) -> set[str]:
    """Video ids already cited by a briefing, topic or nugget (wave 2 scope)."""
    try:
        return set(vi.load_seen_video_ids(output_dir / "_briefings"))
    except Exception as e:
        log.warning("could not read briefing membership (%s); wave 2 scoping degrades to empty", e)
        return set()


def render_report(rows: list[dict], briefed: set[str], generated: str) -> str:
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_bucket[r["bucket"]].append(r)

    total = len(rows)
    remediate = [b for b in BUCKET_ORDER if b not in ("clean", "mild")]
    n_remediate = sum(len(by_bucket[b]) for b in remediate)

    out: list[str] = []
    out.append("# Transcript remediation sweep")
    out.append("")
    out.append(f"- **Generated:** {generated}")
    out.append(f"- **Transcripts assessed:** {total}")
    out.append(f"- **Candidates for remediation:** {n_remediate}")
    out.append("")
    out.append(
        "Derived by running `assess_transcript_artifact` (the same function the transcript "
        "writers use) over every on-disk `.transcript.md`. The corpus meta fields "
        "`transcript_quality_flags` and `transcript_status` were NOT the source: the first is "
        "empty corpus-wide and the second only labels a couple of dozen files, because the "
        "#157/#158 quality machinery stamps them only on transcripts written after it shipped."
    )
    out.append("")

    out.append("## Buckets")
    out.append("")
    out.append("| Bucket | Files | Share | Meaning |")
    out.append("|---|---:|---:|---|")
    meanings = {
        "truncated": "response hit the output cap; real content loss (issue #128)",
        "monolithic_severe": "dialogue collapsed into <=3 entries, or density < 0.1/min",
        "blind_gap_severe": "leading or internal dialogue hole >= 10 minutes",
        "backward_jump_severe": "timestamps jumped backwards >= 10 minutes (clock slip)",
        "chunk_window_severe": "a chunk's stamps landed outside its own window",
        "other_severe": "severe flag outside the classes above",
        "unassessable": "no duration from any source; gap/density/monolithic all gated off - NOT clean",
        "mild": "mild flags only; label-only, no remediation",
        "clean": "no flags",
    }
    for b in BUCKET_ORDER:
        n = len(by_bucket[b])
        if not n:
            continue
        out.append(f"| `{b}` | {n} | {n / total:.1%} | {meanings[b]} |")
    out.append("")

    src = Counter(r.get("duration_source") or "none" for r in rows)
    out.append("## Duration provenance")
    out.append("")
    out.append("| Source | Files | Note |")
    out.append("|---|---:|---|")
    notes = {
        "meta": "authoritative, already on disk",
        "youtube": "authoritative, recovered via videos.list this run",
        "timestamp_floor": "ESTIMATE - a floor, so it can only make a file look healthier",
        "none": "no duration at all -> unassessable",
    }
    for k, n in src.most_common():
        out.append(f"| {k} | {n} | {notes.get(k, '')} |")
    out.append("")

    models = Counter(r.get("model") or "unrecorded" for r in rows if r["bucket"] in remediate)
    if models:
        out.append("## Which model produced the damage")
        out.append("")
        out.append("| Model | Flagged files |")
        out.append("|---|---:|")
        for k, n in models.most_common():
            out.append(f"| `{k}` | {n} |")
        out.append("")

    out.append("## Waves")
    out.append("")
    w1 = by_bucket["truncated"]
    w2 = [
        r
        for b in (
            "monolithic_severe",
            "blind_gap_severe",
            "backward_jump_severe",
            "chunk_window_severe",
            "other_severe",
        )
        for r in by_bucket[b]
        if r["video_id"] in briefed
    ]
    w3 = [
        r
        for b in (
            "monolithic_severe",
            "blind_gap_severe",
            "backward_jump_severe",
            "chunk_window_severe",
            "other_severe",
        )
        for r in by_bucket[b]
        if r["video_id"] not in briefed
    ]
    out.append(f"- **Wave 1** - truncation class, real content loss: **{len(w1)}** file(s)")
    out.append(f"- **Wave 2** - severe AND already cited by a briefing/topic/nugget: **{len(w2)}** file(s)")
    out.append(f"- **Wave 3** - remaining severe: **{len(w3)}** file(s)")
    out.append(
        f"- **Deferred** - unassessable, needs a duration before it can be judged: **{len(by_bucket['unassessable'])}** file(s)"
    )
    out.append("")

    for bucket in remediate:
        items = by_bucket[bucket]
        if not items:
            continue
        out.append(f"## `{bucket}` ({len(items)})")
        out.append("")
        out.append("| Channel | Video | Dur | Entries | Max gap | Model | URL |")
        out.append("|---|---|---:|---:|---:|---|---|")
        for r in sorted(items, key=lambda x: (x["channel"], x["prefix"])):
            dur = r.get("assessed_duration_seconds")
            dur_s = f"{dur // 60}m" if dur else "?"
            if r.get("duration_source") == "timestamp_floor":
                dur_s += "*"
            gap = r.get("max_blind_gap_seconds")
            gap_s = f"{gap // 60}m" if gap else "-"
            out.append(
                f"| {r['channel']} | {str(r['title'])[:52]} | {dur_s} | {r.get('dialogue_entries', 0)} "
                f"| {gap_s} | {str(r.get('model') or '?').replace('gemini-', '')} | {r.get('url', '')} |"
            )
        out.append("")
        out.append("`*` = duration is a timestamp floor, not a measurement.")
        out.append("")

    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Corpus transcript quality sweep (issue #172 worklist)")
    parser.add_argument("--dry-run", action="store_true", help="Print the summary; write no report")
    parser.add_argument("--no-fetch", action="store_true", help="Skip YouTube duration recovery")
    parser.add_argument("--channel", help="Sweep only this channel")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)-7s %(message)s")

    config = vi.load_config()
    output_dir = vi.resolve_output_dir(config)
    log.info("Corpus: %s", output_dir)

    rows = collect_rows(output_dir, args.channel)
    log.info("Found %d transcript(s).", len(rows))
    if not rows:
        log.warning("Nothing to assess.")
        return 0

    if not args.no_fetch:
        filled = fill_durations_from_youtube(rows)
        if filled:
            log.info("Recovered %d duration(s) from YouTube.", filled)

    for r in rows:
        classify(r)

    briefed = load_briefed_video_ids(output_dir)
    counts = Counter(r["bucket"] for r in rows)
    for b in BUCKET_ORDER:
        if counts[b]:
            log.info("  %-22s %4d", b, counts[b])

    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    report = render_report(rows, briefed, generated)

    if args.dry_run:
        log.info("--dry-run: writing nothing.")
        print(report)
        return 0

    reports_dir = output_dir / "_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    md_path = reports_dir / f"{stamp}-remediation-sweep.md"
    json_path = reports_dir / f"{stamp}-remediation-sweep.json"
    md_path.write_text(report, encoding="utf-8")
    payload = [
        {k: v for k, v in r.items() if k != "entries"} | {"briefed": r["video_id"] in briefed}
        for r in rows
        if r["bucket"] not in ("clean", "mild")
    ]
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log.info("Wrote %s", md_path)
    log.info("Wrote %s  (%d remediation candidate(s))", json_path, len(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
