"""One-off repair script: undo the double-offset bug in the YFjfBk8HI5o
transcript that was already written to the real corpus.

Bug: merge_chunked_transcripts wrongly added each chunk's start_offset to
already-absolute timestamps Gemini returned. Result: timestamps in chunks
2-4 are inflated by 3000s, 6000s, 9000s respectively.

Fix: walk the file, parse [HH:MM:SS] and [MM:SS] timestamps, subtract the
appropriate offset based on which chunk they came from.

Mapping (with old buggy offsets applied):
  chunk 1 (real 0-50min):    appears as 0:00 to 50:00         -> subtract 0
  chunk 2 (real 50-1:40):    appears as 1:40:00 to 2:30:00    -> subtract 3000
  chunk 3 (real 1:40-2:30):  appears as 3:20:00 to 4:10:00    -> subtract 6000
  chunk 4 (real 2:30-3:15:52): appears as 5:00:00 to 5:45:52  -> subtract 9000

These ranges don't overlap, so a timestamp's bucket is unambiguous.
"""

from __future__ import annotations

import re
from pathlib import Path

FILE = Path(
    "G:/My Drive/video-intel/lexfridman/2026-02-12-openclaw-the-viral-ai-agent-that-broke-the-internet-peter-steinberger-lex-fridma.transcript.md"
)
BACKUP = Path(str(FILE) + ".broken-stitching.bak")

# Final coverage table at top of file references chunk ranges in MM:SS - HH:MM:SS
# format. We must NOT touch those rows (they were generated correctly from the
# stitch routine's _format_chunk_range_label, which used start_secs as-is).
# So we only repair body timestamps below the coverage table.

COVERAGE_TABLE_END_MARKER = "| 4 | 2:30:00 - 3:15:52 | ok |"


def parse_timestamp_to_seconds(ts: str) -> int | None:
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return None
    return None


def fmt_compact(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def offset_for(seconds: int) -> int:
    """Return the offset that was wrongly added, by chunk bucket."""
    if seconds < 50 * 60:
        return 0  # chunk 1 - no fix needed
    if seconds < 2 * 3600 + 30 * 60:
        return 3000  # chunk 2 - displayed as 1:40-2:30
    if seconds < 4 * 3600 + 10 * 60:
        return 6000  # chunk 3 - displayed as 3:20-4:10
    return 9000  # chunk 4 - displayed as 5:00-5:45:52


def repair_match(m: re.Match) -> str:
    """Replace [HH:MM:SS] / [MM:SS] with the offset-corrected form."""
    ts = m.group(1)
    secs = parse_timestamp_to_seconds(ts)
    if secs is None:
        return m.group(0)
    fixed = secs - offset_for(secs)
    if fixed < 0:
        return m.group(0)
    return f"[{fmt_compact(fixed)}]"


def repair_screen_match(m: re.Match) -> str:
    """SCREEN [HH:MM:SS-HH:MM:SS] [type]: ..."""
    start_ts, end_ts = m.group(1), m.group(2)
    start_s = parse_timestamp_to_seconds(start_ts)
    end_s = parse_timestamp_to_seconds(end_ts)
    if start_s is None or end_s is None:
        return m.group(0)
    fixed_start = start_s - offset_for(start_s)
    fixed_end = end_s - offset_for(end_s)
    if fixed_start < 0 or fixed_end < 0:
        return m.group(0)
    return f"SCREEN [{fmt_compact(fixed_start)}-{fmt_compact(fixed_end)}]"


def main() -> None:
    if not BACKUP.exists():
        raise SystemExit(f"Backup missing at {BACKUP} - aborting (no recovery point)")

    text = FILE.read_text(encoding="utf-8")

    # Split header (coverage table) from body.
    end_marker_pos = text.find(COVERAGE_TABLE_END_MARKER)
    if end_marker_pos < 0:
        raise SystemExit("Could not find coverage-table end marker - file shape unexpected")
    header_end = text.find("\n", end_marker_pos) + 1
    header = text[:header_end]
    body = text[header_end:]

    # Repair speech timestamps: [HH:MM:SS] or [MM:SS]
    body_fixed = re.sub(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]", repair_match, body)

    # Repair SCREEN start-end ranges
    body_fixed = re.sub(
        r"SCREEN \[(\d{1,2}:\d{2}(?::\d{2})?)-(\d{1,2}:\d{2}(?::\d{2})?)\]",
        repair_screen_match,
        body_fixed,
    )

    out_text = header + body_fixed
    FILE.write_text(out_text, encoding="utf-8")

    # Sanity print
    lines = out_text.splitlines()
    print(f"Repair complete: {FILE.name}")
    print(f"  File size: {FILE.stat().st_size} bytes ({len(lines)} lines)")
    # Show last 3 timestamps in the file
    timestamps = re.findall(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]", out_text)
    if timestamps:
        print(f"  Last timestamp in file: [{timestamps[-1]}]")
        print(f"  First timestamp in file: [{timestamps[0]}]")
        print(f"  Total timestamp count: {len(timestamps)}")


if __name__ == "__main__":
    main()
