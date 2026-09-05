#!/usr/bin/env python3
"""Windows-safe skill trigger-eval runner.

The upstream skill-creator `run_eval.py` reads the `claude -p` subprocess pipe
with `select.select(...)`, which on Windows only accepts sockets and raises
`WinError 10038` for every query (all-zero results). This is a faithful
re-implementation that:
  - runs `claude -p <query> --output-format stream-json` per query,
  - reads the stream in a daemon thread + queue (no `select`), so it works on
    Windows, and
  - detects whether Claude consulted the REAL installed skill (the Skill tool
    with skill == <skill-id>) as its first action - which is exactly what the
    user experiences, rather than the synthetic-command proxy.

Usage:
  python run_trigger_eval_win.py --eval-set <eval.json> --skill-id video-intel:video-intel \
      --model claude-opus-4-8 --runs 3 --timeout 60 > results.json
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time


def _drain(pipe, q):
    try:
        for line in iter(pipe.readline, b""):
            q.put(line)
    finally:
        q.put(None)  # sentinel: EOF


def run_once(query, skill_id, model, timeout):
    """Return True if Claude consulted the Skill tool for skill_id on this query."""
    cmd = ["claude", "-p", query, "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=_drain, args=(proc.stdout, q), daemon=True).start()

    triggered = False
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            try:
                line = q.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            try:
                event = json.loads(line.decode("utf-8", "replace").strip())
            except (json.JSONDecodeError, AttributeError):
                continue
            # The first tool_use Claude makes is the decision point.
            items = []
            if event.get("type") == "assistant":
                items = event.get("message", {}).get("content", [])
            elif event.get("type") == "stream_event":
                se = event.get("event", {})
                if se.get("type") == "content_block_start":
                    cb = se.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        items = [cb]
            decided = False
            for it in items:
                if it.get("type") != "tool_use":
                    continue
                decided = True
                name = it.get("name", "")
                inp = it.get("input", {}) or {}
                # Real-skill detection: the Skill tool naming EXACTLY our skill id.
                # Exact (not substring) so video-intel:video-intel-search does not
                # false-match video-intel:video-intel (they share a prefix).
                if name == "Skill":
                    val = str(inp.get("skill", "")).strip()
                    if val == skill_id or val == skill_id.split(":")[-1]:
                        triggered = True
                # First action was something else (e.g. answered directly, or a
                # different skill) -> not a trigger for THIS skill.
                break
            if decided or event.get("type") == "result":
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
    return triggered


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval-set", required=True)
    ap.add_argument("--skill-id", required=True, help="e.g. video-intel:video-intel")
    ap.add_argument("--model", default=None)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--threshold", type=float, default=0.5)
    args = ap.parse_args()

    evals = json.load(open(args.eval_set, encoding="utf-8"))
    results = []
    for i, e in enumerate(evals):
        q = e["query"]
        want = bool(e.get("should_trigger"))
        hits = 0
        for _ in range(args.runs):
            try:
                if run_once(q, args.skill_id, args.model, args.timeout):
                    hits += 1
            except Exception as exc:  # never let one query kill the run
                print(f"  query error: {exc}", file=sys.stderr)
        rate = hits / args.runs
        got = rate >= args.threshold
        ok = got == want
        results.append(
            {
                "query": q,
                "tag": e.get("tag", ""),
                "should_trigger": want,
                "trigger_rate": rate,
                "triggers": hits,
                "runs": args.runs,
                "pass": ok,
            }
        )
        flag = "PASS" if ok else "FAIL"
        print(f"  [{flag}] rate={hits}/{args.runs} want={want} | {q[:65]}", file=sys.stderr)

    passed = sum(r["pass"] for r in results)
    summary = {"total": len(results), "passed": passed, "failed": len(results) - passed}
    print(json.dumps({"skill_id": args.skill_id, "results": results, "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
