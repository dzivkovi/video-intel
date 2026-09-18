"""Run one mindmap-prompt variant over the sample and score it (issue #228).

Composes the call exactly the way `process_mindmap(source="transcript")` does:
`prompt_text + "\\n\\n# TRANSCRIPT TO PROCESS\\n\\n" + transcript_text` through
`call_gemini_text(..., response_mime_type="text/plain")`. If that composition
drifts from the real writer, this harness stops measuring the product.

WRITES NOTHING INTO THE CORPUS. Every generated mindmap lands under --out.

    python tests/evals/run_mindmap_variant.py --prompt prompts/mindmap-from-transcript.md \
        --out <scratch>/v0 --rolls 1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mindmap_entity_recall import aggregate, holds_guards, load_sample, score_mindmap

import video_intel as vi

CORPUS = Path(r"G:/My Drive/video-intel")
# The exact joiner process_mindmap uses. Kept as a module constant so a drift
# check can compare it against the writer's own source.
JOINER = "\n\n# TRANSCRIPT TO PROCESS\n\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", required=True, help="Prompt file to test.")
    ap.add_argument("--out", required=True, help="Scratch directory for generated mindmaps.")
    ap.add_argument("--rolls", type=int, default=1, help="Generations per video (variance matters here).")
    ap.add_argument("--model", default=None)
    ap.add_argument("--sample", default=str(Path(__file__).with_name("mindmap_sample.json")))
    ap.add_argument("--baseline", default=str(Path(__file__).with_name("mindmap_baseline.json")))
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    prompt_text = Path(args.prompt).read_text(encoding="utf-8")
    label = args.label or Path(args.prompt).stem

    sample = [tuple(x) for x in json.loads(Path(args.sample).read_text(encoding="utf-8"))]
    loaded = load_sample(CORPUS, sample)
    if not loaded:
        print("no sample videos resolved; nothing to do")
        return 1

    genai, types = vi.require_gemini()
    import os

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set")
        return 1
    client = vi.create_client(api_key)
    model = args.model or vi.DEFAULT_MODEL

    tokens = {"prompt": 0, "out": 0}

    def note(resp):
        u = vi.log_usage_metadata(resp, "mindmap-eval")
        if u:
            tokens["prompt"] += u.get("prompt") or 0
            tokens["out"] += (u.get("candidates") or 0) + (u.get("thoughts") or 0)

    per_roll: list[dict] = []
    for roll in range(args.rolls):
        rows = []
        for gt, transcript, _ in loaded:
            dest = out / f"{gt.channel}__{gt.prefix}__r{roll}.md"
            if dest.exists():
                text = dest.read_text(encoding="utf-8")
            else:
                try:
                    text = vi.call_gemini_text(
                        client,
                        types,
                        prompt_text + JOINER + transcript,
                        model,
                        response_mime_type="text/plain",
                        on_response=note,
                    )
                except Exception as e:  # a single video must not kill the sweep
                    print(f"  !! {gt.channel}/{gt.prefix} roll{roll}: {type(e).__name__}: {e}")
                    continue
                dest.write_text(text, encoding="utf-8")
            rows.append(score_mindmap(text, gt))
        agg = aggregate(rows)
        per_roll.append(agg)
        mr = agg["mean_recall"]
        print(f"[{label}] roll {roll}: mean_recall={mr:.3f}  names={agg['names_found']}/{agg['names_expected']}")

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    best = max(per_roll, key=lambda a: a["mean_recall"] or 0)
    worst = min(per_roll, key=lambda a: a["mean_recall"] or 0)
    mean_of_rolls = sum(a["mean_recall"] or 0 for a in per_roll) / len(per_roll)
    ok, broken = holds_guards(baseline, best)

    summary = {
        "label": label,
        "prompt": str(args.prompt),
        "rolls": args.rolls,
        "baseline_recall": baseline.get("mean_recall"),
        "mean_recall_over_rolls": mean_of_rolls,
        "worst_roll_recall": worst["mean_recall"],
        "best_roll_recall": best["mean_recall"],
        "guards_hold": ok,
        "guards_broken": broken,
        "per_roll": per_roll,
        "tokens": tokens,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(
        f"[{label}] baseline={baseline.get('mean_recall'):.3f} -> mean={mean_of_rolls:.3f} "
        f"(worst {worst['mean_recall']:.3f}, best {best['mean_recall']:.3f})  "
        f"guards={'OK' if ok else 'BROKEN ' + '; '.join(broken)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
