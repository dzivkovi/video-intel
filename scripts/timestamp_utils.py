"""Shared timestamp helpers: normalization, parsing, and deep-link construction.

Pure functions with no dependency beyond the standard library. That last part
is a contract, not an accident (issue #152): this module is the import-weight
firebreak that lets the standalone read-only analytics scripts
(`wiki_concepts`, `wiki_atlas`, `lead_lag_report`, `lead_lag_viz`,
`burst_report`) import without dragging in the curate stack. Adding a
third-party import here re-couples all of them.

Four groups live here, and they do NOT share a calling convention - check the
group before assuming an argument shape:

* **Line normalization** - `normalize_timestamp`, `normalize_mm_ss_zero_timestamp`.
  These two, and only these two, take a single `line` string and expect
  bracketed input (`[HH:MM:SS] ...` or `[MM:SS] ...`) at its start; a bare
  timestamp without surrounding brackets passes through unchanged, so a caller
  holding a bare string must wrap it before calling. They fix Gemini's known
  timestamp malformations before chunk-offset classification, and keeping that
  Gemini-quirk knowledge in one place is what issue #58 motivated.
* **Classification support** - `should_reinterpret_part_as_mm_ss_zero(lines,
  offset_seconds, chunk_duration_seconds)` takes a LIST of lines plus two
  integers, and `timestamp_tolerance(chunk_duration_seconds)` takes a single
  integer. Neither takes a `line`, and neither is interchangeable with the two
  above.
* **Parsing** - `parse_time_to_seconds`, which takes a BARE time string
  (`MM:SS`, `HH:MM:SS`, or raw seconds) and raises on anything else. Moved
  here from `video_intel.py`, which re-exports it.
* **Link construction** - `timestamped_url`, which appends a `t=<seconds>`
  deep link to a video URL. Moved here from `intel_graph.py`, which
  re-exports it.

Imported by `translate_video.py` (SRT chunk stitching) and `video_intel.py`
(chunked transcript classifier) as well; `translate_video.py` stays
operationally separate, and the stdlib-only rule is what keeps that shared
seam safe.
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


def parse_time_to_seconds(value: str) -> int:
    """Parse time string to seconds. Accepts 'MM:SS', 'HH:MM:SS', or raw seconds.

    Examples: '05:30' -> 330, '01:15:45' -> 4545, '330' -> 330.
    """
    if not value or not value.strip():
        raise ValueError("Empty time value")
    stripped = value.strip()
    parts = stripped.split(":")
    if len(parts) == 1:
        return int(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    raise ValueError(f"Invalid time format: {value!r}. Use MM:SS, HH:MM:SS, or raw seconds.")


def timestamped_url(url: str | None, seconds: int | None) -> str:
    if not url:
        return ""
    if seconds is None:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}t={seconds}"
