#!/usr/bin/env python
"""Staged remediation runner - the wave driver for issue #172.

Reads the worklist `remediation_sweep.py` wrote to ``_reports/`` and re-runs
``process --url ... --force`` on each candidate, in waves, with the guards a
long paid batch needs.

Re-running the FULL pipeline (not just the transcript) is deliberate: since the
issue #54 inversion the mindmap is generated FROM the transcript, and concepts
from the mindmap, so a corrupt transcript has already propagated into both. The
transcript is the expensive call; mindmap-from-transcript and concepts are cheap
text calls on top of it.

THE THREE GUARDS, AND WHY EACH EXISTS
-------------------------------------
1. **Consecutive-failure abort.** Since PR #175 a systematic fault (a bad model
   name, an unmounted G: drive, an expired key) no longer stops at video 1 - the
   pipeline logs it and continues. Without this guard a 314-video sweep would
   pay 314 full transcript calls while every step failed. Recorded as a required
   design input on issue #172 on 2026-08-31.
2. **No-improvement abort.** The stronger signal, and the one exit codes cannot
   give: after each re-run the new transcript is re-assessed with the SAME
   assessor the sweep used. If the first few re-runs come back still-severe, the
   remediation premise itself is wrong for this corpus and the run stops before
   spending the rest. A green exit code on a still-collapsed transcript is
   exactly the failure this catches.
3. **Resumability.** A wave is hours long. Progress is written after every video,
   and `--resume` skips anything already completed, so an interruption costs one
   video rather than the run.

Usage:
    python scripts/remediation_run.py --wave 2 --dry-run     # plan, spend nothing
    python scripts/remediation_run.py --wave 2 --limit 1     # one video, real spend
    python scripts/remediation_run.py --wave 2               # the wave
    python scripts/remediation_run.py --wave 2 --resume      # continue after a stop

After each wave, per the issue's blast-radius discipline, run MANUALLY:
    python scripts/video_intel.py taxonomy-build
    python scripts/video_intel.py index --force
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import remediation_sweep as sweep

import video_intel as vi

log = logging.getLogger("remediation_run")

EXIT_OK = 0
EXIT_HARD_FAIL = 1
EXIT_PARTIAL = 3


def latest_sweep(reports_dir: Path) -> Path:
    candidates = sorted(reports_dir.glob("*-remediation-sweep.json"))
    if not candidates:
        raise SystemExit(f"No sweep worklist in {reports_dir}. Run: python scripts/remediation_sweep.py")
    return candidates[-1]


def select_wave(rows: list[dict], wave: str) -> list[dict]:
    """Wave 1 is the truncation class, 2 is severe-and-already-briefed, 3 the rest."""
    if wave == "1":
        return [r for r in rows if r["bucket"] == "truncated"]
    severe = [r for r in rows if r["bucket"] not in ("truncated", "unassessable")]
    if wave == "2":
        return [r for r in severe if r.get("briefed")]
    if wave == "3":
        return [r for r in severe if not r.get("briefed")]
    if wave == "all":
        return [r for r in rows if r["bucket"] != "unassessable"]
    raise SystemExit(f"unknown wave {wave!r}")


def reassess(output_dir: Path, row: dict) -> dict:
    """Re-read the transcript now on disk and score it with the sweep's assessor.

    Deliberately re-derives the prefix from the video_id index rather than
    reusing the pre-run prefix: a `--force` re-run can land under a different
    prefix if the title rotated, and checking the OLD path would report a
    successful remediation as a failure (this repo's PR #136 class).
    """
    channel_dir = output_dir / row["channel"]
    prefix = row["prefix"]
    if row.get("video_id"):
        vi._invalidate_video_id_cache(channel_dir)
        prefix = vi._load_video_id_index(channel_dir).get(row["video_id"], prefix)
    tx_path = channel_dir / f"{prefix}.transcript.md"
    if not tx_path.exists():
        return {"ok": False, "reason": "no transcript on disk", "entries": 0, "severe": ["missing"]}
    text = tx_path.read_bytes().decode("utf-8", errors="replace")
    entries = sweep.parse_dialogue_entries(text)
    duration = row.get("assessed_duration_seconds")
    a = vi.assess_transcript_artifact(entries, duration)
    severe = list(a.get("severe") or [])
    return {
        "ok": not severe,
        "reason": "still severe: " + ",".join(severe) if severe else "clean",
        "entries": a.get("dialogue_entries", 0),
        "severe": severe,
        "prefix": prefix,
    }


def backup_existing(output_dir: Path, row: dict) -> Path | None:
    """Copy the artifacts we are about to overwrite into `_reports/`.

    A `--force` re-run overwrites the transcript, mindmap and concepts in place
    with no undo. The current transcript is collapsed rather than empty - the
    speech is all there, just crammed into one untimestamped block - so it is
    not worthless, and a re-run that somehow returns LESS content would
    otherwise be an unrecoverable loss. Backups are small text files and are
    written once, never overwritten, so a re-run of a re-run cannot clobber the
    original.
    """
    channel_dir = output_dir / row["channel"]
    dest_dir = output_dir / "_reports" / "remediation-backup" / row["channel"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = None
    for suffix in (".transcript.md", ".mindmap.md", ".concepts.json", ".meta.json"):
        src = channel_dir / f"{row['prefix']}{suffix}"
        dst = dest_dir / f"{row['prefix']}{suffix}"
        if src.exists() and not dst.exists():
            try:
                dst.write_bytes(src.read_bytes())
                saved = dst
            except OSError as e:
                log.warning("   backup of %s failed (%s)", src.name, e)
    return saved


def run_one(row: dict, repo_root: Path, timeout_s: int) -> tuple[int, str]:
    cmd = [
        sys.executable,
        str(repo_root / "scripts" / "video_intel.py"),
        "process",
        "--url",
        row["url"],
        "--channel",
        row["channel"],
        "--force",
    ]
    try:
        proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return EXIT_HARD_FAIL, f"timed out after {timeout_s}s"
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    return proc.returncode, " | ".join(t.strip()[:120] for t in tail)


def main() -> int:
    ap = argparse.ArgumentParser(description="Staged transcript remediation (issue #172)")
    ap.add_argument("--wave", default="2", choices=["1", "2", "3", "all"])
    ap.add_argument("--limit", type=int, help="Process at most N videos")
    ap.add_argument("--dry-run", action="store_true", help="Plan only; spend nothing")
    ap.add_argument("--resume", action="store_true", help="Skip videos already completed in the state file")
    ap.add_argument("--max-consecutive-failures", type=int, default=3)
    ap.add_argument("--max-consecutive-no-improvement", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=3600, help="Per-video wall clock, seconds")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)-7s %(message)s")
    repo_root = Path(__file__).resolve().parent.parent

    config = vi.load_config()
    output_dir = vi.resolve_output_dir(config)
    reports_dir = output_dir / "_reports"
    worklist_path = latest_sweep(reports_dir)
    rows = json.loads(worklist_path.read_text(encoding="utf-8"))
    log.info("Worklist: %s (%d candidates)", worklist_path.name, len(rows))

    wave_rows = select_wave(rows, args.wave)
    wave_rows = [r for r in wave_rows if r.get("url")]
    wave_rows.sort(key=lambda r: (r["channel"], r["prefix"]))

    state_path = reports_dir / f"remediation-state-wave{args.wave}.json"
    done: dict[str, dict] = {}
    if args.resume and state_path.exists():
        try:
            done = json.loads(state_path.read_text(encoding="utf-8"))
            log.info("Resuming: %d video(s) already completed.", len(done))
        except (ValueError, OSError) as e:
            log.warning("Could not read state (%s); starting fresh.", e)
    pending = [r for r in wave_rows if r.get("video_id") not in done]
    if args.limit:
        pending = pending[: args.limit]

    total_minutes = sum((r.get("assessed_duration_seconds") or 0) for r in pending) / 60
    log.info(
        "Wave %s: %d pending of %d (%.0f video-minutes, rough estimate $%.2f)",
        args.wave,
        len(pending),
        len(wave_rows),
        total_minutes,
        total_minutes / 60 * 0.332 + len(pending) * 0.124,
    )
    if args.dry_run:
        for r in pending[:20]:
            log.info(
                "  [%s] %s (%dm, %d entries)",
                r["channel"],
                str(r["title"])[:56],
                (r.get("assessed_duration_seconds") or 0) // 60,
                r.get("dialogue_entries", 0),
            )
        if len(pending) > 20:
            log.info("  ... and %d more", len(pending) - 20)
        log.info("--dry-run: nothing spent.")
        return 0
    if not pending:
        log.info("Nothing to do.")
        return 0

    consecutive_failures = 0
    consecutive_no_improvement = 0
    started = time.monotonic()
    improved = regressed = failed = 0

    for i, row in enumerate(pending, 1):
        label = f"[{i}/{len(pending)}] {row['channel']}/{str(row['title'])[:46]}"
        before = row.get("dialogue_entries", 0)
        log.info("%s  (before: %d entries)", label, before)
        backup_existing(output_dir, row)
        rc, tail = run_one(row, repo_root, args.timeout)
        verdict = reassess(output_dir, row)

        if rc == EXIT_HARD_FAIL:
            failed += 1
            consecutive_failures += 1
            log.error("   exit %d - %s", rc, tail)
        else:
            consecutive_failures = 0
            if rc == EXIT_PARTIAL:
                log.warning("   exit 3 (a requested step produced nothing) - %s", tail)

        if verdict["ok"] and verdict["entries"] > before:
            improved += 1
            consecutive_no_improvement = 0
            log.info("   OK  %d -> %d entries, clean", before, verdict["entries"])
        else:
            if rc != EXIT_HARD_FAIL:
                consecutive_no_improvement += 1
            regressed += 1
            log.warning("   NOT IMPROVED  %d -> %d entries (%s)", before, verdict["entries"], verdict["reason"])

        done[row.get("video_id") or row["prefix"]] = {
            "channel": row["channel"],
            "title": row["title"],
            "exit": rc,
            "entries_before": before,
            "entries_after": verdict["entries"],
            "severe_after": verdict["severe"],
            "at": datetime.now(UTC).isoformat(),
        }
        state_path.write_text(json.dumps(done, indent=2), encoding="utf-8")

        if consecutive_failures >= args.max_consecutive_failures:
            log.error(
                "ABORT: %d consecutive hard failures. This looks systematic (bad key, unmounted "
                "corpus, model config), not per-video. Fix it, then --resume.",
                consecutive_failures,
            )
            break
        if consecutive_no_improvement >= args.max_consecutive_no_improvement:
            log.error(
                "ABORT: %d consecutive re-runs completed but did NOT improve. The remediation "
                "premise is wrong for these videos - stopping before the rest is spent. "
                "Investigate one by hand, then --resume.",
                consecutive_no_improvement,
            )
            break

    elapsed = (time.monotonic() - started) / 60
    log.info(
        "--- wave %s: %d improved, %d not improved, %d hard-failed, %.0f min ---",
        args.wave,
        improved,
        regressed,
        failed,
        elapsed,
    )
    log.info("State: %s", state_path)
    log.info(
        "Next, MANUALLY: python scripts/video_intel.py taxonomy-build && python scripts/video_intel.py index --force"
    )
    return 0 if failed == 0 else EXIT_PARTIAL


if __name__ == "__main__":
    sys.exit(main())
