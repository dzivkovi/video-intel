"""Shared timestamp normalization helpers.

Pure functions that fix Gemini's known timestamp malformations before
chunk-offset classification. Imported by both `translate_video.py` (SRT
translation chunk stitching) and `video_intel.py` (chunked transcript
classifier). Splitting these four helpers into one module keeps the
Gemini-quirk knowledge in a single place — see issue #58 for the bug
class that motivated the extraction.

All four helpers expect bracketed input (`[HH:MM:SS] ...` or `[MM:SS] ...`)
at the start of `line`. Bare timestamps without surrounding brackets pass
through unchanged — callers receiving bare strings must wrap before calling.
"""

import re


def normalize_timestamp(line: str) -> str:
    """Fix malformed timestamps where Gemini puts total minutes in the HH field.

    Rule: parse [A:MM:SS]. If A <= 23, leave unchanged. If A > 23, treat A as
    total minutes: divmod into hours, add remainder to MM, carry if needed.
    """
    line = line.lstrip("﻿")
    m = re.match(r"\[(\d+):(\d{2}):(\d{2})\]", line)
    if not m:
        return line
    a, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a <= 23:
        return line
    hours, add_minutes = divmod(a, 60)
    new_minutes = add_minutes + mm
    if new_minutes >= 60:
        extra_hours, new_minutes = divmod(new_minutes, 60)
        hours += extra_hours
    return f"[{hours:02d}:{new_minutes:02d}:{ss:02d}]{line[m.end() :]}"


def timestamp_tolerance(chunk_duration_seconds: int) -> int:
    """Return slack for timestamp classification.

    Long clips can drift by minutes; short clips should stay much tighter or we
    start mistaking malformed [MM:SS:00] output for real [HH:MM:SS].
    """
    return min(300, max(30, chunk_duration_seconds // 10))


def normalize_mm_ss_zero_timestamp(line: str) -> str:
    """Fix Gemini's malformed [MM:SS:00] output by converting it to [MM:SS]."""
    line = line.lstrip("﻿")
    m = re.match(r"\[(\d+):(\d{2}):00\]", line)
    if not m:
        return line
    mm, ss = int(m.group(1)), int(m.group(2))
    return f"[{mm:02d}:{ss:02d}]{line[m.end() :]}"


def should_reinterpret_part_as_mm_ss_zero(
    lines: list[str],
    offset_seconds: int,
    chunk_duration_seconds: int,
) -> bool:
    """Detect parts where Gemini emitted [MM:SS:00] instead of [HH:MM:SS].

    We only switch modes when the malformed interpretation clearly explains far
    more lines than the standard interpretation.
    """
    max_relative = chunk_duration_seconds + timestamp_tolerance(chunk_duration_seconds)
    candidates = 0
    standard_fit = 0
    alt_fit = 0

    for raw_line in lines:
        line = normalize_timestamp(raw_line)
        m = re.match(r"\[(\d+):(\d{2}):(\d{2})\]", line)
        if not m:
            continue

        hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if ss != 0:
            continue

        candidates += 1
        standard_total = hh * 3600 + mm * 60 + ss
        if standard_total <= max_relative or offset_seconds <= standard_total <= offset_seconds + max_relative:
            standard_fit += 1

        alt_total = hh * 60 + mm
        if alt_total <= max_relative:
            alt_fit += 1

    return candidates >= 3 and alt_fit >= 3 and alt_fit > standard_fit
