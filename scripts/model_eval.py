#!/usr/bin/env python3
"""Non-destructive A/B harness for swapping the Gemini model.

Why this exists
---------------
A model swap was shipped on a synthetic *text* microbenchmark and generalized to
*video transcription*. Throughput was the only axis measured; price was never
looked at; and the dimension that actually decided the question - timestamp
granularity - was not measured until the owner asked for a real A/B. The result
was a default that put the heaviest step on the most expensive model in the
lineup while making deep-links up to six minutes imprecise.

The lesson is not "be more careful". The gap was known and written down in a
status note, and shipped anyway. So the correction is to make the experiment
cheap enough that there is no reason to skip it, and to score enough facets that
"I tested it" cannot mean "I tested one axis".

What it does
------------
Runs each (fixture x model) pair against the real ``call_gemini`` path with the
real transcript prompt, scores the result across mechanical + economic
dimensions, and writes a scorecard. It NEVER writes into the corpus - only into
``tests/evals/model-cards/``.

The headline metric is ``max_gap_s``: the largest interval between consecutive
timestamps. That is the worst-case error of a ``&t=<seconds>`` deep link, which
is what this corpus is FOR. Two models can emit identical text and identical
token counts while differing 5x on this number.

Usage
-----
    python scripts/model_eval.py --candidate gemini-3.7-flash --incumbent gemini-3.5-flash
    python scripts/model_eval.py --candidate gemini-3.7-flash --fixtures screenshare-demo
    python scripts/model_eval.py --candidate X --thinking low --dry-run
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

import video_intel as v

REPO = Path(__file__).resolve().parent.parent
_FIXTURES_REAL = REPO / "tests" / "evals" / "model_fixtures.yaml"
_FIXTURES_TEMPLATE = REPO / "tests" / "evals" / "model_fixtures.yaml.example"
# The real fixture set is gitignored: it points at videos from the maintainer's
# private corpus and annotates each with the defect it exposes, which is a
# curated-watchlist extract rather than a list of public URLs. Mirrors the
# config.yaml / config.yaml.example split. Falling back to the template keeps
# a fresh checkout runnable enough to fail with a useful message instead of a
# FileNotFoundError.
FIXTURES = _FIXTURES_REAL if _FIXTURES_REAL.exists() else _FIXTURES_TEMPLATE
CARDS = REPO / "tests" / "evals" / "model-cards"

# $ per 1M tokens: (input, output). Output INCLUDES thinking tokens - that is the
# whole reason thinking is an economic dimension and not just a latency one.
# Promo rates for 3.6/3.7 expire 2026-12-31 and then double; `promo_until` is
# carried into the scorecard so a cost ranking is never read as permanent.
PRICING = {
    "gemini-3-flash-preview": (0.50, 3.00, None),
    "gemini-3.5-flash": (1.50, 9.00, None),
    "gemini-3.6-flash": (0.75, 3.75, "2026-12-31"),
    "gemini-3.7-flash": (0.75, 3.75, "2026-12-31"),
    "gemini-2.5-flash": (0.30, 2.50, None),
    "gemini-2.5-pro": (1.25, 10.00, None),
}

# Thinking levels a model accepts. `minimal` is Flash-exclusive and 3.7 dropped
# it; sending it there is a hard 400. None means "let the helper decide".
DEFAULT_THINKING = {"gemini-3.7-flash": "low"}


def _secs(stamp: str) -> int | None:
    try:
        parts = [int(p) for p in str(stamp).split(":")]
    except (ValueError, AttributeError):
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def score(raw: str, usage: dict, model: str, seg_secs: int, wall: float | None) -> dict:
    """Score one run. Every field here is a dimension a model can regress on."""
    parsed, err = v.try_parse_transcript_json(raw)
    # A top-level list is Gemini returning the envelope wrapped in an array. The
    # pipeline recovers from it, but it IS a malformation and a model that emits
    # it is a worse citizen than one that does not - so it is scored, not hidden.
    shape = "dict" if isinstance(parsed, dict) else ("list-wrapped" if isinstance(parsed, list) else "unparsed")
    env = parsed[0] if isinstance(parsed, list) and parsed else parsed
    env = env if isinstance(env, dict) else {}

    tr = env.get("transcripts") or []
    stamps = sorted(s for s in (_secs(e.get("start")) for e in tr) if s is not None)
    gaps = [b - a for a, b in itertools.pairwise(stamps)] if len(stamps) > 1 else []
    # Trailing gap: from the last stamp to the end of the requested window. A
    # model that stops stamping at 03:45 of a 10-minute window has a 375s
    # trailing gap even though its text may run to the end.
    trailing = (seg_secs - (stamps[-1] - stamps[0])) if stamps else seg_secs
    chars = sum(len(e.get("text") or "") for e in tr)

    pin, pout, promo = PRICING.get(model, (None, None, None))
    itok = usage.get("prompt") or 0
    otok = usage.get("candidates") or 0
    think = usage.get("thoughts") or 0
    cost_seg = (itok / 1e6 * pin + (otok + think) / 1e6 * pout) if pin else None

    return {
        "model": model,
        "parse_ok": parsed is not None,
        "parse_error": (err or "")[:80] or None,
        "envelope_shape": shape,
        "segments": len(tr),
        "segments_per_min": round(len(tr) / (seg_secs / 60), 2) if seg_secs else 0,
        # THE headline metric: worst-case deep-link error in seconds.
        "max_gap_s": max(gaps + [trailing]) if (gaps or stamps) else None,
        "median_gap_s": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "trailing_gap_s": trailing if stamps else None,
        "chars": chars,
        "screen_content": len(env.get("screen_content") or []),
        "speakers": len(env.get("speakers") or []),
        "in_tok": itok,
        "out_tok": otok,
        "thinking_tok": think,
        "cost_segment": round(cost_seg, 4) if cost_seg is not None else None,
        "cost_per_video_hour": round(cost_seg * (3600 / seg_secs), 3) if cost_seg is not None else None,
        "promo_until": promo,
        "wall_s": round(wall, 1) if wall else None,
    }


def run_one(client, types, fx: dict, model: str, thinking: str | None, seg: int, scratch: Path) -> dict:
    """One (fixture, model) call. Cached on disk so a re-run costs nothing."""
    tag = f"{fx['id']}__{model}"
    raw_p, use_p = scratch / f"{tag}.json", scratch / f"{tag}.usage.json"
    wall = None
    if raw_p.exists() and use_p.exists():
        raw = raw_p.read_text(encoding="utf-8")
        usage = json.loads(use_p.read_text(encoding="utf-8"))
        print(f"  [cached] {tag}")
    else:
        level = thinking or DEFAULT_THINKING.get(model)
        tc = (
            types.ThinkingConfig(thinking_level=level)
            if level
            else v._make_thinking_config_for_transcript(types, model)
        )
        usage: dict = {}
        t0 = time.time()
        try:
            raw = v.call_gemini(
                client,
                types,
                fx["url"],
                v.load_prompt("transcript"),
                model,
                response_json=True,
                start_offset=fx["start"],
                end_offset=fx["start"] + seg,
                thinking_config=tc,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                on_response=lambda r, c=usage, m=model: c.update(v.log_usage_metadata(r, m) or {}),
            )
        except Exception as e:
            print(f"  [FAIL]   {tag}: {type(e).__name__}: {str(e)[:140]}")
            return {"model": model, "fixture": fx["id"], "error": f"{type(e).__name__}: {str(e)[:160]}"}
        wall = time.time() - t0
        raw_p.write_text(raw, encoding="utf-8")
        use_p.write_text(json.dumps(usage), encoding="utf-8")
        print(f"  [live]   {tag}  {wall:.1f}s")
    row = score(raw, usage, model, seg, wall)
    row["fixture"] = fx["id"]
    return row


def render(rows: list[dict], models: list[str], manifest: dict, incumbent: str | None) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Model scorecard - {', '.join(models)}", "", f"Generated {stamp} by `scripts/model_eval.py`.", ""]
    out += [
        "`max_gap_s` is the headline: the largest interval between consecutive",
        "timestamps, i.e. the worst-case error of a `&t=<seconds>` deep link.",
        "Lower is better. Two models can emit identical text and identical token",
        "counts while differing several-fold on this number.",
        "",
    ]
    cols = [
        ("fixture", "fixture", 22),
        ("model", "model", 22),
        ("shape", "envelope_shape", 13),
        ("segs", "segments", 5),
        ("max_gap_s", "max_gap_s", 10),
        ("scr", "screen_content", 4),
        ("spk", "speakers", 4),
        ("think", "thinking_tok", 6),
        ("$/vid-hr", "cost_per_video_hour", 9),
    ]
    out.append("| " + " | ".join(c[0] for c in cols) + " |")
    out.append("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        if r.get("error"):
            out.append(f"| {r['fixture']} | {r['model']} | **ERROR** | | | | | | |")
            out.append(f"| | | `{r['error']}` | | | | | | |")
            continue
        out.append("| " + " | ".join(str(r.get(c[1], "")) for c in cols) + " |")
    out += ["", "## Per-facet notes", ""]
    by_id = {f["id"]: f for f in manifest["fixtures"]}
    for fid in dict.fromkeys(r["fixture"] for r in rows):
        out.append(f"- **{fid}** - {' '.join((by_id.get(fid, {}).get('facet') or '').split())}")
    if incumbent:
        out += ["", "## Verdict", "", f"Incumbent: `{incumbent}`.", ""]
        for m in models:
            if m == incumbent:
                continue
            mine = [r for r in rows if r["model"] == m and not r.get("error")]
            theirs = [r for r in rows if r["model"] == incumbent and not r.get("error")]
            if not mine or not theirs:
                out.append(f"- `{m}`: insufficient data (a run errored).")
                continue
            g_new = [r["max_gap_s"] for r in mine if r["max_gap_s"] is not None]
            g_old = [r["max_gap_s"] for r in theirs if r["max_gap_s"] is not None]
            c_new = [r["cost_per_video_hour"] for r in mine if r["cost_per_video_hour"]]
            c_old = [r["cost_per_video_hour"] for r in theirs if r["cost_per_video_hour"]]
            am = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
            out.append(
                f"- `{m}` vs `{incumbent}`: mean max_gap "
                f"**{am(g_new)}s** vs {am(g_old)}s; mean cost/video-hour "
                f"**${am(c_new)}** vs ${am(c_old)}."
            )
    out += [
        "",
        "## Not measured here",
        "",
        "State this every time. Timestamp *drift* (do stamps match the actual",
        "audio) is unverified - only granularity is. Chunked long-video behavior",
        "near the 64k output cap is not exercised by a single short segment.",
        "Non-English and heavily accented speech are not represented in the",
        "current fixture set. Each run is a single sample per cell.",
        "",
    ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Non-destructive Gemini model A/B for the transcript step.")
    ap.add_argument("--candidate", required=True, help="Model to evaluate.")
    ap.add_argument("--incumbent", help="Model to compare against (the one in use today).")
    ap.add_argument("--thinking", help="Force a thinking_level for ALL models (default: per-model correct value).")
    ap.add_argument("--fixtures", help="Comma-separated fixture ids (default: all).")
    ap.add_argument("--segment-seconds", type=int, help="Override segment length.")
    ap.add_argument("--out", help="Scorecard path (default: tests/evals/model-cards/<candidate>.md).")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and cost shape; call nothing.")
    args = ap.parse_args()

    manifest = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
    if FIXTURES == _FIXTURES_TEMPLATE:
        print(
            f"No fixture set at {_FIXTURES_REAL}.",
        )
        print(
            f"Copy {_FIXTURES_TEMPLATE.name} to {_FIXTURES_REAL.name} and point each "
            "fixture at a real video from your corpus."
        )
        print("The template holds placeholder URLs; running against them would spend Gemini quota on nothing.")
        return 2
    seg = args.segment_seconds or manifest.get("defaults", {}).get("segment_seconds", 600)
    fixtures = manifest["fixtures"]
    if args.fixtures:
        want = {s.strip() for s in args.fixtures.split(",")}
        fixtures = [f for f in fixtures if f["id"] in want]
        if not fixtures:
            print(f"No fixture matched {sorted(want)}. Available: {[f['id'] for f in manifest['fixtures']]}")
            return 2
    models = [args.candidate] + ([args.incumbent] if args.incumbent and args.incumbent != args.candidate else [])

    print(f"{len(fixtures)} fixture(s) x {len(models)} model(s) = {len(fixtures) * len(models)} calls, {seg}s each")
    for m in models:
        if m not in PRICING:
            print(f"  WARNING: no pricing for {m}; cost columns will be blank.")
    if args.dry_run:
        for f in fixtures:
            print(f"  {f['id']:24} {f['url']}  start={f['start']}s")
        print("\nDry run: nothing called, nothing written.")
        return 0

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        print("GEMINI_API_KEY not set.")
        return 1
    v.require_gemini()
    from google.genai import types

    client = v.create_client(key)
    scratch = CARDS / "_raw"
    scratch.mkdir(parents=True, exist_ok=True)

    rows = []
    for fx in fixtures:
        print(f"\n{fx['id']}:")
        for m in models:
            rows.append(run_one(client, types, fx, m, args.thinking, seg, scratch))

    CARDS.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else CARDS / f"{args.candidate}.md"
    out_path.write_text(render(rows, models, manifest, args.incumbent), encoding="utf-8")
    (out_path.with_suffix(".json")).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nScorecard: {out_path}")
    errs = [r for r in rows if r.get("error")]
    if errs:
        print(f"{len(errs)} run(s) errored - the scorecard records them rather than omitting them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
