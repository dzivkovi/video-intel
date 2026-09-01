#!/usr/bin/env python
"""Detect and undo remediation regressions (issue #172).

A `--force` re-run occasionally produces a WORSE transcript than the one it
replaced. Observed on the first wave-3 run: a 55,475-byte transcript with 6,343
speech words became a 194-byte stub with zero - the chunked path returned a
single "thin" segment and the merge wrote it out. The quality machinery flagged
it correctly (`monolithic_severe`, `transcript_status: partial`), so it was not
silently wrong, but the CONTENT was gone from the live corpus.

`remediation_run.py` copies every artifact to `_reports/remediation-backup/`
before overwriting, which is what makes this recoverable. This script compares
what is on disk now against that backup and restores the pre-run state wherever
the re-run destroyed speech.

WHAT COUNTS AS DAMAGE - TWO CONDITIONS, NOT ONE
-----------------------------------------------
Entry count going DOWN is not a regression: a re-run can legitimately produce
coarser segmentation while keeping every word (measured: one video went 76 -> 25
entries with its speech intact). So the first test is the transcribed SPEECH
inside quoted dialogue, not how many blocks it was split into.

But speech-loss ALONE is not damage either, and this is the trap. A first cut of
this script flagged 7 videos on word-count alone; 6 of them had re-transcribed
CLEANLY (`transcript_status: complete`, no quality flags, 21-82 well-spread
entries) and merely differed by 10-17% in wording, which is ordinary variance
between two transcriptions of the same audio - the wave-2 validated sample moved
+4% in the other direction. Restoring those would have UNDONE six successful
remediations and put monolithic transcripts back. Caught only because the tool
was dry-run before it was trusted.

So a video is restored only when BOTH hold: the new transcript lost speech AND
it is still severe-flagged. If the re-run came back clean, it succeeded, whatever
the word delta. That left exactly one true casualty in the same batch: 6,343
speech words -> 0, still flagged `monolithic_severe` + `blind_gap_severe`.

ALL FOUR ARTIFACTS ARE RESTORED TOGETHER
----------------------------------------
The mindmap was regenerated FROM the bad transcript and concepts FROM that
mindmap (the issue #54 inversion), so restoring the transcript alone would leave
a video whose three artifacts disagree, and a meta claiming
`transcript_dialogue_entries: 0` next to a full transcript. Restoring the whole
pre-run set returns the video to a coherent - if still flagged - prior state.

Usage:
    python scripts/remediation_restore.py --dry-run    # report, change nothing
    python scripts/remediation_restore.py              # restore the losses
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import video_intel as vi

log = logging.getLogger("remediation_restore")

_DIALOGUE_RE = re.compile(r"^\[(\d{1,3}:\d{2}(?::\d{2})?)\]")
#: Fraction of pre-run speech below which the re-run counts as a loss. Not 1.0:
#: a re-transcription legitimately differs by several percent in wording, and
#: the validated wave-2 sample moved +4%. This is only HALF the test - see
#: `is_still_severe`, which must also hold before anything is restored.
LOSS_THRESHOLD = 0.9
RESTORE_SUFFIXES = (".transcript.md", ".mindmap.md", ".concepts.json", ".meta.json")


def speech_words(text: str) -> list[str]:
    """Words inside quoted dialogue on column-0 timestamped lines.

    Screen content is excluded for the same reason the sweep excludes it: it is
    not transcribed speech, and a collapsed transcript can retain rich screen
    content while having lost the talking.
    """
    out: list[str] = []
    for line in text.splitlines():
        if _DIALOGUE_RE.match(line):
            out.extend(" ".join(re.findall(r'"([^"]*)"', line)).split())
    return out


def is_still_severe(meta_path: Path) -> bool:
    """Whether the POST-run transcript still carries a severe quality flag.

    Uses the repo's shared severity predicate rather than a local rule, so this
    cannot drift from what the writers and the sweep call severe.
    """
    meta = vi._read_meta_best_effort(meta_path, raise_on_os_error=False)
    flags = meta.get("transcript_quality_flags")
    if not isinstance(flags, list):
        return False
    return vi.transcript_quality_flags_are_severe(flags)


def find_regressions(output_dir: Path) -> list[dict]:
    backup_root = output_dir / "_reports" / "remediation-backup"
    if not backup_root.is_dir():
        return []
    findings: list[dict] = []
    for channel_dir in sorted(p for p in backup_root.iterdir() if p.is_dir()):
        for bak in sorted(channel_dir.glob("*.transcript.md")):
            live = output_dir / channel_dir.name / bak.name
            if not live.exists():
                continue
            try:
                old = bak.read_bytes().decode("utf-8", errors="replace")
                new = live.read_bytes().decode("utf-8", errors="replace")
            except OSError as e:
                log.warning("could not compare %s (%s)", bak.name, e)
                continue
            ow, nw = speech_words(old), speech_words(new)
            if not ow:
                continue
            ratio = len(nw) / len(ow)
            if ratio >= LOSS_THRESHOLD:
                continue
            prefix = bak.name[: -len(".transcript.md")]
            # Second condition: a CLEAN re-transcription is a success even when
            # it words things differently. Only a still-severe result that also
            # lost speech is damage worth undoing.
            if not is_still_severe(output_dir / channel_dir.name / f"{prefix}.meta.json"):
                log.info(
                    "  [%s] %s kept %.0f%% of speech but re-transcribed CLEAN - a success, not a regression",
                    channel_dir.name,
                    prefix[:52],
                    ratio * 100,
                )
                continue
            findings.append(
                {
                    "channel": channel_dir.name,
                    "prefix": prefix,
                    "words_before": len(ow),
                    "words_after": len(nw),
                    "ratio": ratio,
                }
            )
    return findings


def restore(output_dir: Path, finding: dict) -> list[str]:
    backup_dir = output_dir / "_reports" / "remediation-backup" / finding["channel"]
    live_dir = output_dir / finding["channel"]
    restored: list[str] = []
    for suffix in RESTORE_SUFFIXES:
        src = backup_dir / f"{finding['prefix']}{suffix}"
        dst = live_dir / f"{finding['prefix']}{suffix}"
        if not src.exists():
            continue
        try:
            dst.write_bytes(src.read_bytes())
            restored.append(suffix)
        except OSError as e:
            log.error("   restore of %s failed: %s", dst.name, e)
    return restored


def main() -> int:
    ap = argparse.ArgumentParser(description="Undo destructive remediation re-runs (issue #172)")
    ap.add_argument("--dry-run", action="store_true", help="Report only; restore nothing")
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)-7s %(message)s")

    output_dir = vi.resolve_output_dir(vi.load_config())
    findings = find_regressions(output_dir)
    if not findings:
        log.info("No speech-loss regressions found.")
        return 0

    log.info("%d video(s) lost speech in a re-run:", len(findings))
    for f in findings:
        log.info(
            "  [%s] %s  %d -> %d words (%.0f%% kept)",
            f["channel"],
            f["prefix"][:52],
            f["words_before"],
            f["words_after"],
            f["ratio"] * 100,
        )
    if args.dry_run:
        log.info("--dry-run: nothing restored.")
        return 0

    for f in findings:
        restored = restore(output_dir, f)
        log.info("  restored %s/%s: %s", f["channel"], f["prefix"][:44], ", ".join(restored) or "nothing")
    log.info("Done. Re-run taxonomy-build and index --force so derived artifacts match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
