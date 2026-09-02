#!/usr/bin/env python3
"""
Translate YouTube video audio to BCS (Bosnian/Croatian/Serbian) subtitles.

Uses Gemini's multimodal video understanding with maxOutputTokens=65536
to translate entire videos in a single pass (up to ~2.5 hours of typical
dialogue density). Streams output progressively for live monitoring.

Usage:
    export GEMINI_API_KEY=your_key
    python translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID"
    python translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --model gemini-2.5-flash  # default: gemini-2.5-pro
    python translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --stdout
"""

import argparse
import logging
import os
import queue
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from youtube_captions import (
    CaptionsResult,
    fetch_english_captions,
    filter_snippets_by_range,
    format_captions_as_srt,
    format_captions_for_translation,
)

from gemini_common import (
    build_permissive_safety_settings,
    create_client,
    get_retry_delay,
    require_gemini,
    require_youtube,
)
from timestamp_utils import (
    normalize_mm_ss_zero_timestamp,
    normalize_timestamp,
    should_reinterpret_part_as_mm_ss_zero,
    timestamp_tolerance,
)

log = logging.getLogger("translate_video")

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = "./examples"
MAX_OUTPUT_TOKENS = 65536
SRT_DEFAULT_THINKING_BUDGET = 128


# ---------------------------------------------------------------------------
# Title translation
# ---------------------------------------------------------------------------


def translate_title(client, model: str, title: str) -> str:
    """Translate a video title to BCS via a single text-only Gemini call.

    Uses a reduced best-effort retry budget and falls back to the original
    title on failure. Stitch is a local file merge — it should never block
    for long on an optional title translation during a Gemini outage.

    Budget math (worst case before fallback):
      rate (2):   15s + 30s        ≈ 45s
      server (2): 60s + 120s       ≈ 3 min
    Contrast with the streaming path's 3/8 budget which can wait ~47 min.
    """
    contents = (
        "Translate this video title to BCS (Bosnian-neutral). "
        "Output ONLY the translated title, nothing else.\n\n" + title
    )
    max_retries_rate = 2
    max_retries_server = 2
    for attempt in range(max(max_retries_rate, max_retries_server) + 1):
        try:
            response = client.models.generate_content(model=model, contents=contents)
            return response.text.strip().strip('"')
        except Exception as e:
            retry = get_retry_delay(
                e,
                attempt,
                max_retries_rate=max_retries_rate,
                max_retries_server=max_retries_server,
            )
            if retry is None:
                log.warning("Title translation failed (%s); falling back to original title", e)
                return title
            kind, wait, max_for_type = retry
            log.warning("%s — retry %d/%d in %.0fs...", kind, attempt + 1, max_for_type, wait)
            time.sleep(wait)
    return title


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def extract_video_id(url: str) -> str | None:
    """Extract the 11-character YouTube video ID from a URL."""
    m = re.search(r"(?:v=|/)([a-zA-Z0-9_-]{11})", url)
    return m.group(1) if m else None


def slugify(text: str, max_len: int = 80) -> str:
    """Create a filesystem-safe slug from a title."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len].rstrip("-")


def load_prompt(prompt_name: str) -> str:
    """Load a prompt template from prompts/ directory."""
    prompt_path = SKILL_DIR / "prompts" / f"{prompt_name}.md"
    if not prompt_path.exists():
        log.error("Prompt file not found: %s", prompt_path)
        sys.exit(1)
    return prompt_path.read_text(encoding="utf-8")


def apply_timestamp_offset(line: str, offset_seconds: int, chunk_duration_seconds: int) -> str:
    """Add a time offset to the timestamp at the start of a line.

    Classifies each timestamp as relative, already-absolute, or implausible
    before deciding whether to apply the offset. Handles both [HH:MM:SS] and
    [MM:SS] formats; always outputs [HH:MM:SS].
    """
    tolerance = timestamp_tolerance(chunk_duration_seconds)

    # First, fix legacy malformed timestamps (e.g. [120:05:30] → [02:05:30])
    line = normalize_timestamp(line)

    # Try [HH:MM:SS] first (more specific), then [MM:SS]
    # timestamp-literal-ok: runs AFTER normalize_timestamp above, so it must
    # accept the repaired shape rather than the shape the shared patterns gate.
    m = re.match(r"\[(\d+):(\d{2}):(\d{2})\]", line)
    if m:
        total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    else:
        # timestamp-literal-ok: same post-normalize position as the branch above.
        m = re.match(r"\[(\d+):(\d{2})\]", line)
        if not m:
            return line
        total = int(m.group(1)) * 60 + int(m.group(2))

    max_relative = chunk_duration_seconds + tolerance

    if total <= max_relative:
        # Relative timestamp — add offset
        total += offset_seconds
    elif offset_seconds <= total <= offset_seconds + max_relative:
        # Already absolute — keep as-is
        pass
    else:
        # Implausible — warn, pass through unchanged
        log.warning(
            "Implausible timestamp %s in chunk (offset=%ds, duration=%ds)",
            line.split("]")[0] + "]",
            offset_seconds,
            chunk_duration_seconds,
        )
        return line

    h, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"[{h:02d}:{mm:02d}:{ss:02d}]{line[m.end() :]}"


# Bracketed timestamp shapes, defined ONCE (issue #197, following issue #195's
# precedent in video_intel.py). The MINUTE field is UNBOUNDED: a video-intel
# transcript renders `MM:SS` verbatim from whatever the writer produced, so a
# cue past 99:59 carries a `[100:30]`-style stamp, and 25 such files exist in
# the corpus today. A `\d{1,2}` minute field does not merely mis-parse those -
# it does not match at all, which is worse in three different ways: coverage
# reported 0 instead of the real end (a guaranteed false truncation warning),
# overshoot detection silently stopped detecting, and the `--from-transcript`
# gate rejected a legitimate transcript as "not a transcript".
#
# The HOUR field stays `\d{1,2}` on purpose. It is hours, 99 is already absurd,
# and widening it would let a legacy malformed `[120:05:30]` parse as 120 hours
# instead of being caught. `normalize_timestamp` is what repairs that shape.
TS_HOURS = r"\d{1,2}"
TS_MINUTES = r"\d+"
# Inside `[HH:MM:SS]` the minute field is two digits and cannot carry the
# overflow - that is what the bare `[MM:SS]` shape is for. It gets its own
# constant rather than borrowing TS_SECONDS, so a future change to the seconds
# field cannot silently move the minute field with it. That coupling is the
# exact class this whole change exists to prevent.
TS_MINUTES_IN_HHMMSS = r"\d{2}"
TS_SECONDS = r"\d{2}"
# `[HH:MM:SS]` and `[MM:SS]`. NOT anchored - every current caller uses
# `.match()`, which anchors at the caller. A future `.search()` would match
# mid-line; add `^` here rather than relying on that.
HHMMSS_BRACKET_RE = re.compile(rf"\[({TS_HOURS}):({TS_MINUTES_IN_HHMMSS}):({TS_SECONDS})\]")
MMSS_BRACKET_RE = re.compile(rf"\[({TS_MINUTES}):({TS_SECONDS})\]")


def extract_last_timestamp_seconds(text: str) -> int | None:
    """Return the last parseable timestamp in `text`, in seconds.

    Handles [HH:MM:SS] and [MM:SS]. Callers must run
    `normalize_mm_ss_zero_timestamp` first on any [MM:SS:00]-drift parts —
    this helper operates on already-repaired text.
    """
    last: int | None = None
    for raw in text.splitlines():
        line = raw.lstrip("\ufeff").lstrip()
        m = HHMMSS_BRACKET_RE.match(line)
        if m:
            last = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            continue
        m = MMSS_BRACKET_RE.match(line)
        if m:
            last = int(m.group(1)) * 60 + int(m.group(2))
    return last


def _format_hhmmss(seconds: int) -> str:
    """Format seconds as HH:MM:SS for coverage annotations."""
    h, rem = divmod(max(0, seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_output_path(output_dir: Path, title: str, date: str) -> Path:
    """Build the output file path using the video-intel naming convention."""
    slug = slugify(title)
    return output_dir / f"{date}-{slug}.translate-bcs.txt"


def parse_iso8601_duration(duration: str) -> int:
    """Parse ISO 8601 duration (e.g., PT2H18M42S) to total seconds."""
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not m:
        return 0
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = int(m.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def fetch_video_metadata(video_id: str) -> dict[str, str | int] | None:
    """Fetch video title, publish date, and duration from YouTube Data API."""
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if not yt_key:
        log.debug("YOUTUBE_API_KEY not set, skipping metadata fetch")
        return None

    yt_build = require_youtube()
    youtube = yt_build("youtube", "v3", developerKey=yt_key)
    resp = youtube.videos().list(part="snippet,contentDetails", id=video_id).execute()
    if not resp.get("items"):
        log.warning("No YouTube metadata found for video ID: %s", video_id)
        return None

    item = resp["items"][0]
    snippet = item["snippet"]
    from html import unescape

    result = {
        "title": unescape(snippet["title"]),
        "published": snippet["publishedAt"][:10],
    }
    duration_str = item.get("contentDetails", {}).get("duration", "")
    if duration_str:
        result["duration_seconds"] = parse_iso8601_duration(duration_str)
    return result


# ---------------------------------------------------------------------------
# YouTube captions: SRT-first translation path
# ---------------------------------------------------------------------------
# CaptionsResult, fetch_english_captions, format_captions_for_translation,
# format_captions_as_srt, and filter_snippets_by_range were extracted into the
# shared scripts/youtube_captions.py module (issue #60) so video_intel.py's
# captions-failover path can reuse them without violating operational separation.
# They are imported above and re-exported here, so existing
# `from translate_video import fetch_english_captions` call sites keep working.


SINGLE_REQUEST_CAP_LOW_RES_SECONDS = 9000  # 150 min, below ~170 min theoretical
SINGLE_REQUEST_CAP_HIGH_RES_SECONDS = 3000  # 50 min, below ~55 min theoretical


def single_request_cap_seconds(high_res: bool) -> int:
    """Return the duration ceiling below which a video fits one Gemini request."""
    return SINGLE_REQUEST_CAP_HIGH_RES_SECONDS if high_res else SINGLE_REQUEST_CAP_LOW_RES_SECONDS


def build_chunk_list(
    duration_seconds: int,
    chunk_minutes: int = 20,
    *,
    high_res: bool = False,
) -> list[tuple[int, int]]:
    """Build the list of (start_sec, end_sec) chunks for a video.

    Resolution-aware: low-res single-request capacity is ~170 min and high-res
    is ~55 min, so the threshold below which we skip chunking depends on which
    mode we're calling Gemini in.

    - duration <= cap: single request, no clipping → [(0, 0)]
    - duration >  cap: uniform chunk_minutes segments from the start
    """
    cap = single_request_cap_seconds(high_res)
    if duration_seconds <= cap:
        return [(0, 0)]  # (0, 0) = no clipping, full video

    chunk_seconds = chunk_minutes * 60
    chunks = []
    pos = 0
    while pos < duration_seconds:
        end = min(pos + chunk_seconds, duration_seconds)
        chunks.append((pos, end))
        pos = end
    return chunks


def _format_hhmm(seconds: int) -> str:
    """Format seconds as HH:MM for coverage display."""
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def build_incomplete_notice(
    observed_end_seconds: int,
    requested_end_seconds: int,
    *,
    finish_reason: str | None = None,
) -> str:
    """Build a visible markdown warning when translation ends early.

    Emitted as a bordered H2 block before the transcript body so a reader
    opening the file cannot miss it. Never phrased in a way that could be
    mistaken for a transcript line. Lists finish_reason when available so
    the reader can distinguish safety blocks, length limits, and voluntary
    soft-stops without reading the logs.
    """
    observed = _format_hhmmss(observed_end_seconds)
    requested = _format_hhmmss(requested_end_seconds)
    missing_seconds = max(0, requested_end_seconds - observed_end_seconds)
    missing = _format_hhmmss(missing_seconds)
    ratio_pct = int((observed_end_seconds / requested_end_seconds) * 100) if requested_end_seconds > 0 else 0

    if finish_reason == "SAFETY":
        reason_line = "**Reason:** Gemini blocked output on safety filters (`finish_reason: SAFETY`)."
        advice = (
            "Safety filters are already relaxed in this pipeline. If this message "
            "appears, a filter still fired — try a different model or reduce the "
            "portion of the video containing triggering content."
        )
    elif finish_reason == "MAX_TOKENS":
        reason_line = "**Reason:** Output token budget exhausted (`finish_reason: MAX_TOKENS`)."
        advice = "Split the video with `--start` / `--end` into shorter segments and rerun."
    elif finish_reason and finish_reason != "STOP":
        reason_line = f"**Reason:** Gemini reported `finish_reason: {finish_reason}`."
        advice = "Check the Gemini docs for this finish_reason, or rerun with `--force`."
    else:
        reason_line = (
            "**Reason:** Gemini stopped voluntarily (`finish_reason: STOP`) before reaching "
            "the end of the video. This is model-level soft-stop behavior, often triggered "
            "on politically or emotionally heavy content. It is not a code bug."
        )
        advice = (
            "Try rerunning with `--force`, using a different model, or splitting the video "
            "with `--start` / `--end` into shorter segments."
        )

    return "\n".join(
        [
            "## \u26a0\ufe0f Incomplete translation",
            "",
            f"Gemini stopped at **{observed}** but the requested window ends at "
            f"**{requested}** ({ratio_pct}% covered, **{missing}** missing).",
            "",
            reason_line,
            "",
            f"**What to try:** {advice}",
            "",
            "The text below is partial. Do not treat it as a complete translation.",
        ]
    )


def detect_overshoot(
    body_text: str,
    last_input_seconds: int,
    *,
    tolerance_seconds: int = 30,
) -> tuple[int, int] | None:
    """Find the first output line whose timestamp exceeds the input's last
    timestamp by more than `tolerance_seconds`.

    Returns (overshoot_line_number, observed_last_seconds) when hallucinated
    overshoot is detected, or None when the output stays within tolerance.
    Line number is 1-indexed and counts only lines that carry a parseable
    timestamp — it is a best-effort pointer for a human reader, not a
    precise file-position index.

    Tolerant of the same `[HH:MM:SS]` and `[MM:SS]` formats that
    `extract_last_timestamp_seconds` accepts. Lines without a parseable
    timestamp are ignored entirely.
    """
    cutoff = last_input_seconds + tolerance_seconds
    hhmmss = HHMMSS_BRACKET_RE
    mmss = MMSS_BRACKET_RE
    first_over_line: int | None = None
    last_seconds = 0
    ts_line_number = 0
    for line in body_text.splitlines():
        stripped = line.lstrip("\ufeff").strip()
        m = hhmmss.match(stripped)
        if m:
            h, mn, s = map(int, m.groups())
            total = h * 3600 + mn * 60 + s
        else:
            m = mmss.match(stripped)
            if not m:
                continue
            mn, s = map(int, m.groups())
            total = mn * 60 + s
        ts_line_number += 1
        last_seconds = max(last_seconds, total)
        if first_over_line is None and total > cutoff:
            first_over_line = ts_line_number

    if first_over_line is None:
        return None
    return (first_over_line, last_seconds)


def build_overshoot_notice(
    last_input_seconds: int,
    last_observed_seconds: int,
    first_overshoot_line: int,
    *,
    tolerance_seconds: int = 30,
) -> str:
    """Build a markdown warning block to append at the END of the output file
    when Gemini produced timestamps past the real end of the input.

    Placed at the end of the file, not the header, because that is where the
    hallucinated content physically lives — a reader scrolling down to review
    the translation will see the warning right next to the questionable lines.
    Nothing is trimmed: the full translation is preserved and the user
    decides what to keep.
    """
    last_input = _format_hhmmss(last_input_seconds)
    last_observed = _format_hhmmss(last_observed_seconds)
    cutoff = _format_hhmmss(last_input_seconds + tolerance_seconds)
    overshoot_seconds = max(0, last_observed_seconds - last_input_seconds)
    overshoot = _format_hhmmss(overshoot_seconds)

    return "\n".join(
        [
            "---",
            "",
            "## \u26a0\ufe0f Possible hallucinated overshoot",
            "",
            f"The last English caption in the YouTube source ended at **{last_input}**.",
            f"The translation above extends to **{last_observed}** \u2014 about **{overshoot}** "
            f"past the end of the real video.",
            "",
            f"Content from approximately **line {first_overshoot_line}** onward (the first line "
            f"with a timestamp after **{cutoff}**) could not be verified against the source "
            f"and may have been invented by the translation model.",
            "",
            "The full translation is preserved as-is. Review manually and trim if needed.",
        ]
    )


SOURCE_MODE_LABELS = {
    "captions-manual": "YouTube captions (manually authored)",
    "captions-autogen": "YouTube captions (auto-generated, cleaned up via Gemini)",
    "video": "Direct video audio (no captions available)",
    "transcript": "Local transcript file (from video_intel.py transcript)",
}


def build_header(
    title: str,
    url: str,
    published: str,
    model: str,
    *,
    original_title: str | None = None,
    coverage: tuple[int, int] | None = None,
    duration_seconds: int | None = None,
    observed_end_seconds: int | None = None,
    segments_block: str | None = None,
    finish_reason: str | None = None,
    source_mode: str | None = None,
) -> str:
    """Build the metadata header for the translation file.

    coverage: (start_min, end_min) of translated range. If None but
        `duration_seconds` is set, defaults to full-video coverage.
    duration_seconds: total video duration from YouTube API.
    observed_end_seconds: last observed translation timestamp, in seconds.
        When materially below the requested end (< 95%), appended to the
        Coverage line as a TRUNCATED annotation AND a visible `## Incomplete
        translation` notice block is emitted before the transcript body.
    segments_block: optional pre-rendered markdown block (e.g. F2 coverage
        table) inserted between the metadata lines and the trailing `---`.
    finish_reason: Gemini's finish_reason from the final chunk. Used to tailor
        the incomplete-translation notice with a root-cause description.
    source_mode: one of "captions-manual", "captions-autogen", "video", or None.
        Emitted as a `**Source mode:**` line so the reader knows whether the
        BCS came from a YouTube caption track or from direct video audio.
        None omits the line entirely (backward-compatible).
    """
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Translation (BCS): {title}", ""]
    if original_title and original_title != title:
        lines.append(f"**Original:** {original_title}")
    lines.append(f"**Source:** {url}")
    lines.append(f"**Published:** {published}")
    lines.append(f"**Translated:** {now}")
    lines.append(f"**Model:** {model}")
    if source_mode is not None:
        label = SOURCE_MODE_LABELS.get(source_mode, source_mode)
        lines.append(f"**Source mode:** {label}")

    cov_end_s: int | None = None
    truncated = False
    if duration_seconds is not None:
        if coverage is not None:
            cov_start_s = coverage[0] * 60
            cov_end_s = coverage[1] * 60
        else:
            cov_start_s = 0
            cov_end_s = duration_seconds
        total_hhmm = _format_hhmm(duration_seconds)
        cov_label = f"{_format_hhmm(cov_start_s)} \u2013 {_format_hhmm(cov_end_s)} / {total_hhmm} total"
        if observed_end_seconds is not None and cov_end_s > 0:
            ratio = observed_end_seconds / cov_end_s
            if ratio < 0.95:
                cov_label += f" \u2014 observed end {_format_hhmmss(observed_end_seconds)} (TRUNCATED)"
                truncated = True
        lines.append(f"**Coverage:** {cov_label}")

    if segments_block:
        lines.append("")
        lines.append(segments_block.rstrip())
    lines.append("")
    lines.append("---")
    lines.append("")

    # Visible incomplete-translation notice: a reader scanning the file must
    # not confuse the partial body for a complete translation. Append after
    # the `---` separator and before the transcript body.
    if truncated and observed_end_seconds is not None and cov_end_s is not None:
        lines.append(
            build_incomplete_notice(
                observed_end_seconds,
                cov_end_s,
                finish_reason=finish_reason,
            )
        )
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines) + "\n"


def format_elapsed(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def format_stats(elapsed: float, line_count: int, usage: dict | None) -> str:
    """Format translation statistics for display."""
    lines = [
        "",
        "--- Statistics ---",
        f"Duration:       {format_elapsed(elapsed)}",
    ]
    if usage:
        out_tokens = usage.get("candidates_token_count", 0)
        in_tokens = usage.get("prompt_token_count", 0)
        if out_tokens:
            pct = (out_tokens / MAX_OUTPUT_TOKENS) * 100
            lines.append(f"Output tokens:  {out_tokens:,} / {MAX_OUTPUT_TOKENS:,} ({pct:.1f}% of max)")
        if in_tokens:
            lines.append(f"Input tokens:   {in_tokens:,}")
        total = usage.get("total_token_count", 0)
        if total:
            lines.append(f"Total tokens:   {total:,}")
    lines.append(f"Lines:          {line_count}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stitcher — merges part files into a single translation
# ---------------------------------------------------------------------------


def classify_segment_status(
    observed_last_relative: int | None,
    chunk_duration_seconds: int,
) -> str:
    """Return `ok` / `suspicious` / `truncated` for a single part file.

    Deterministic ratio thresholds, no heuristics:
    - ok          observed_last >= 95% of expected duration
    - suspicious  observed_last in [80%, 95%)
    - truncated   observed_last < 80% (or no parseable timestamps)
    """
    if observed_last_relative is None or chunk_duration_seconds <= 0:
        return "truncated"
    ratio = observed_last_relative / chunk_duration_seconds
    if ratio >= 0.95:
        return "ok"
    if ratio >= 0.80:
        return "suspicious"
    return "truncated"


def build_segments_block(rows: list[dict]) -> str:
    """Render per-part coverage rows as a compact markdown table.

    Columns: Range, Expected end, Observed last, Status. One row per part
    file. Omits the block entirely when there are no rows.
    """
    if not rows:
        return ""
    lines = [
        "**Segments:**",
        "",
        "| Range | Expected end | Observed last | Status |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(f"| {row['range']} | {row['expected_end']} | {row['observed_last']} | {row['status']} |")
    return "\n".join(lines) + "\n"


def stitch_parts(
    part_dir: Path,
    title: str,
    date: str,
    url: str,
    model: str,
    *,
    original_title: str | None = None,
    duration_seconds: int | None = None,
) -> Path:
    """Merge part files into a single stitched translation.

    Part files are the canonical artifacts. This produces a convenience merged file.
    No parts found → hard error. Missing intermediate parts → warn and stitch what exists.

    Part discovery uses original_title slug (how parts were named during translation).
    The title parameter is used only for the stitched file's header display.
    duration_seconds: total video duration (from YouTube API) for coverage metadata.
    """
    # Discover parts by original title slug (parts were named during translation with original title)
    discovery_slug = slugify(original_title) if original_title else slugify(title)
    pattern = f"{date}-{discovery_slug}.part-*.translate-bcs.txt"
    part_files = sorted(part_dir.glob(pattern))

    if not part_files:
        log.error("No part files matching pattern: %s/%s", part_dir, pattern)
        sys.exit(1)

    # Sort by start-minute extracted from filename (part-START-END)
    def sort_key(p: Path) -> int:
        m = re.search(r"\.part-(\d+)-", p.name)
        return int(m.group(1)) if m else 0

    part_files.sort(key=sort_key)

    # Detect gaps between consecutive parts
    for i in range(len(part_files) - 1):
        current_end = re.search(r"\.part-\d+-(\d+)\.", part_files[i].name)
        next_start = re.search(r"\.part-(\d+)-", part_files[i + 1].name)
        if current_end and next_start:
            end_min = int(current_end.group(1))
            start_min = int(next_start.group(1))
            if start_min > end_min:
                log.warning("Gap: no part for %d-%d min", end_min, start_min)

    # Read, apply offsets, and merge
    all_lines: list[str] = []
    last_ts: tuple[int, int, int] | None = None
    segment_rows: list[dict] = []
    pending_divider: str | None = None  # queued for next segment's first line

    for part_file in part_files:
        # Extract offset and duration from filename: part-START-END
        offset_match = re.search(r"\.part-(\d+)-(\d+)\.", part_file.name)
        if offset_match:
            start_min_raw = int(offset_match.group(1))
            end_min_raw = int(offset_match.group(2))
            offset_seconds = start_min_raw * 60
            chunk_duration_seconds = (end_min_raw - start_min_raw) * 60
        else:
            start_min_raw = 0
            end_min_raw = 60
            offset_seconds = 0
            chunk_duration_seconds = 3600  # fallback: assume 1h

        content = part_file.read_text(encoding="utf-8")
        raw_lines = content.splitlines()
        reinterpret_mm_ss_zero = should_reinterpret_part_as_mm_ss_zero(
            raw_lines, offset_seconds, chunk_duration_seconds
        )
        if reinterpret_mm_ss_zero:
            log.warning(
                "Detected malformed [MM:SS:00] timestamps in %s; reinterpreting as clip-relative [MM:SS]",
                part_file.name,
            )
            repaired_lines = [normalize_mm_ss_zero_timestamp(line) for line in raw_lines]
        else:
            repaired_lines = raw_lines

        # Emit any divider queued by the previous non-ok segment before
        # this segment's first line, so the body position matches the row.
        if pending_divider is not None:
            all_lines.append(pending_divider)
            pending_divider = None

        # F2 observed-last: walk the ADJUSTED lines and track the last
        # timestamp that falls inside the expected absolute window for this
        # chunk. Using the adjusted output means already-absolute timestamps
        # (part files that arrive as [01:11:00] rather than [00:11:00]) are
        # handled correctly, and implausible pass-throughs are excluded by
        # the window check. Values beyond the window are skipped rather
        # than trusted; classify_segment_status then reads the clip-
        # relative observation (= absolute minus offset).
        chunk_tolerance = timestamp_tolerance(chunk_duration_seconds)
        abs_window_min = offset_seconds - chunk_tolerance
        abs_window_max = offset_seconds + chunk_duration_seconds + chunk_tolerance
        part_last_absolute: int | None = None

        for line in repaired_lines:
            adjusted = apply_timestamp_offset(line, offset_seconds, chunk_duration_seconds)

            # Check monotonic timestamps
            # timestamp-literal-ok: reads the output of apply_timestamp_offset's
            # own `f"[{h:02d}:{m:02d}:{s:02d}]"` writer, so it must match the
            # WRITER's zero-padded shape - the checker-uses-the-writer's-shape
            # rule, not a fourth copy of the input grammar.
            ts_match = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]", adjusted)
            if ts_match:
                hh, mm, ss = int(ts_match.group(1)), int(ts_match.group(2)), int(ts_match.group(3))
                current_ts = (hh, mm, ss)
                if last_ts is not None and current_ts < last_ts:
                    log.warning(
                        "Non-monotonic timestamp: [%02d:%02d:%02d] after [%02d:%02d:%02d]",
                        *current_ts,
                        *last_ts,
                    )
                last_ts = current_ts

                total_adjusted = hh * 3600 + mm * 60 + ss
                if abs_window_min <= total_adjusted <= abs_window_max:
                    part_last_absolute = total_adjusted

            all_lines.append(adjusted)

        # Derive clip-relative observation for status classification and
        # absolute display. None → no valid in-window timestamps → truncated.
        if part_last_absolute is not None:
            observed_last_relative = max(0, part_last_absolute - offset_seconds)
            observed_last_label = _format_hhmmss(part_last_absolute)
        else:
            observed_last_relative = None
            observed_last_label = "—"
        status = classify_segment_status(observed_last_relative, chunk_duration_seconds)

        range_label = f"{_format_hhmm(start_min_raw * 60)}\u2013{_format_hhmm(end_min_raw * 60)}"
        expected_end_label = _format_hhmmss(end_min_raw * 60)
        segment_rows.append(
            {
                "range": range_label,
                "expected_end": expected_end_label,
                "observed_last": observed_last_label,
                "status": status,
            }
        )
        if status != "ok":
            log.warning(
                "Segment %s %s: observed end %s, expected %s",
                range_label,
                status.upper(),
                observed_last_label,
                expected_end_label,
            )
            # Queue a divider so the NEXT segment (or end of body) carries
            # the annotation inline with the coverage-table entry.
            pending_divider = (
                f"<!-- segment {range_label} {status} at {observed_last_label} (expected {expected_end_label}) -->"
            )

    # If the last segment was non-ok, flush its divider at the end of body
    if pending_divider is not None:
        all_lines.append(pending_divider)

    # Calculate coverage range from part filenames (first start, last end in minutes)
    first_match = re.search(r"\.part-(\d+)-\d+\.", part_files[0].name)
    last_match = re.search(r"\.part-\d+-(\d+)\.", part_files[-1].name)
    coverage_start = int(first_match.group(1)) if first_match else 0
    coverage_end = int(last_match.group(1)) if last_match else 0
    coverage = (coverage_start, coverage_end) if coverage_end > 0 else None

    is_partial = (
        coverage is not None
        and duration_seconds is not None
        and coverage_end * 60 < duration_seconds - 60  # >1 min gap = partial
    )

    # Build output
    segments_block = build_segments_block(segment_rows)
    header = build_header(
        title,
        url,
        date,
        model,
        original_title=original_title,
        coverage=coverage,
        duration_seconds=duration_seconds,
        segments_block=segments_block or None,
    )
    bcs_note = ""
    if is_partial:
        cov_hhmm = _format_hhmm(coverage_end * 60)
        total_hhmm = _format_hhmm(duration_seconds)
        bcs_note = f"> Ovaj prevod pokriva prvih {cov_hhmm} od ukupno {total_hhmm} videa.\n\n"

    body = "\n".join(all_lines)
    # Ensure single trailing newline
    if not body.endswith("\n"):
        body += "\n"

    output_path = part_dir / f"{date}-{discovery_slug}.translate-bcs.txt"
    output_path.write_text(header + bcs_note + body, encoding="utf-8")

    log.info("Stitched %d parts (%d lines) → %s", len(part_files), len(all_lines), output_path)
    return output_path


# ---------------------------------------------------------------------------
# Heartbeat thread
# ---------------------------------------------------------------------------


def _heartbeat_loop(stop_event: threading.Event, start_time: float) -> None:
    """Print elapsed time every 30s until stopped. Runs as daemon thread."""
    while not stop_event.wait(timeout=30):
        elapsed = time.time() - start_time
        log.info("Waiting for Gemini... (%s)", format_elapsed(elapsed))


# ---------------------------------------------------------------------------
# Gemini streaming translation
# ---------------------------------------------------------------------------


def _stream_with_timeouts(
    client,
    model: str,
    contents,
    config,
    tmp_file=None,
) -> tuple[str, dict | None, str | None]:
    """Stream a Gemini generation with heartbeat, wall-clock timeouts, and progressive writes.

    Used by both the video-translation path (translate_video) and the
    captions-translation path (translate_captions_text). Pre-built
    `contents` and `config` are passed in by the caller — this helper
    only owns the streaming/retry/draining/diagnostic machinery so the
    two callers stay consistent.

    Returns: (text, usage_metadata, finish_reason). finish_reason is
    captured from `chunk.candidates[0].finish_reason` and normalized
    to its `.name` ("STOP", "SAFETY", "MAX_TOKENS", ...).
    """
    max_retries_rate = 3  # 429 rate-limit: short bursts, resolve quickly
    max_retries_server = 8  # 503 overload: capacity issues, may last minutes
    for attempt in range(max(max_retries_rate, max_retries_server) + 1):
        try:
            stream = client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
            break
        except Exception as e:
            retry = get_retry_delay(
                e,
                attempt,
                max_retries_rate=max_retries_rate,
                max_retries_server=max_retries_server,
            )
            if retry is None:
                raise
            kind, wait, max_for_type = retry
            log.warning(
                "%s — retry %d/%d in %.0fs (Ctrl+C to abort)",
                kind,
                attempt + 1,
                max_for_type,
                wait,
            )
            time.sleep(wait)

    start_time = time.time()
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(stop_heartbeat, start_time), daemon=True)
    heartbeat.start()

    accumulated = []
    usage = None
    finish_reason = None
    first_chunk = True
    # Wall-clock timeout for first chunk. Gemini Pro can take 10-15 min to start
    # on long videos, so 20 min is generous but bounded.  The httpx read=1200
    # timeout is unreliable here because HTTP-level keepalives reset it.
    first_chunk_timeout = 1200  # 20 min
    stall_timeout = 300  # 5 min gap after streaming starts

    # Drain stream in a background thread so we can enforce wall-clock timeouts
    # via queue.get().  The iterator blocks inside __next__() when Gemini is
    # "thinking", and in-loop timeout checks never fire if __next__ never returns.
    chunk_q: queue.Queue = queue.Queue()

    def _drain():
        try:
            for chunk in stream:
                chunk_q.put(("chunk", chunk))
            chunk_q.put(("done", None))
        except Exception as exc:
            chunk_q.put(("error", exc))

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    try:
        while True:
            timeout = first_chunk_timeout if first_chunk else stall_timeout
            try:
                kind, value = chunk_q.get(timeout=timeout)
            except queue.Empty:
                if first_chunk:
                    raise TimeoutError(f"No data received after {format_elapsed(first_chunk_timeout)}") from None
                raise TimeoutError(f"Stream stalled for {format_elapsed(stall_timeout)}") from None

            if kind == "done":
                break
            if kind == "error":
                raise value

            chunk = value
            if first_chunk:
                stop_heartbeat.set()
                ttfc = time.time() - start_time
                log.info("First chunk received (%s). Streaming translation...", format_elapsed(ttfc))
                first_chunk = False

            text = chunk.text
            if text:
                accumulated.append(text)
                if tmp_file:
                    tmp_file.write(text)
                    tmp_file.flush()
                print(text, end="", file=sys.stderr, flush=True)

            # finish_reason lives on chunk.candidates[0].finish_reason per the
            # google-genai SDK — NOT on content.parts. Getting this path wrong
            # silently produces `finish_reason = None` and masks exactly the
            # diagnostic this capture is meant to surface.
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                meta = chunk.usage_metadata
                usage = {
                    "prompt_token_count": getattr(meta, "prompt_token_count", None),
                    "candidates_token_count": getattr(meta, "candidates_token_count", None),
                    "total_token_count": getattr(meta, "total_token_count", None),
                }
            if hasattr(chunk, "candidates") and chunk.candidates:
                candidate_reason = getattr(chunk.candidates[0], "finish_reason", None)
                if candidate_reason is not None:
                    finish_reason = getattr(candidate_reason, "name", str(candidate_reason))

    except TimeoutError as e:
        stop_heartbeat.set()
        log.error("Timed out: %s", e)
        raise
    except Exception as e:
        stop_heartbeat.set()
        partial = "".join(accumulated)
        if partial:
            log.error("Stream interrupted after %d chars: %s", len(partial), e)
            log.error("Partial output preserved in .txt.tmp file")
        else:
            log.error("Stream failed before any output: %s", e)
        raise
    finally:
        stop_heartbeat.set()
        print("", file=sys.stderr)

    return "".join(accumulated), usage, finish_reason


def call_gemini_translate(
    client,
    types,
    video_url: str,
    system_prompt: str,
    model: str,
    tmp_file=None,
    *,
    high_res: bool = False,
    start_offset: int | None = None,
    end_offset: int | None = None,
) -> tuple[str, dict | None, str | None]:
    """Stream video translation from Gemini with heartbeat and progressive writes.

    Returns: (text, usage_metadata, finish_reason)
    finish_reason: "STOP", "MAX_TOKENS", etc. from final chunk, or None if unavailable.
    """
    # Build video part with optional clipping via video_metadata
    part_kwargs = {"file_data": types.FileData(file_uri=video_url)}
    if start_offset is not None or end_offset is not None:
        meta_kwargs = {}
        if start_offset is not None:
            meta_kwargs["start_offset"] = f"{start_offset}s"
        if end_offset is not None:
            meta_kwargs["end_offset"] = f"{end_offset}s"
        part_kwargs["video_metadata"] = types.VideoMetadata(**meta_kwargs)

    # Clip-relative instruction for chunks; stitcher applies absolute offsets later
    if start_offset is not None and end_offset is not None:
        instruction = (
            "Translate only the provided clip. Start timestamps at [00:00:00] relative to this clip, "
            "in [HH:MM:SS] format. For seconds, use the LAST field: 5 seconds must be [00:00:05], "
            "not [00:05:00]."
        )
    else:
        instruction = "Translate the entire video audio to BCS."

    contents = types.Content(
        parts=[
            types.Part(**part_kwargs),
            types.Part(text=instruction),
        ]
    )
    config_kwargs = {
        "system_instruction": system_prompt,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.2,
        "safety_settings": build_permissive_safety_settings(types),
    }
    if not high_res:
        # Default to low media resolution: translation reads audio only, and audio tokens
        # (32/sec) are unaffected by media_resolution — only video frame tokens change
        # (66/frame low vs 258/frame default). Low-res is 3x cheaper and extends single-
        # request capacity from ~55 min to ~170 min. Opt in to --high-res only when the
        # prompt needs to read on-screen text (slides, burned-in captions). Must use the
        # full enum name; the API rejects shorthand with 400 INVALID_ARGUMENT.
        # See: https://ai.google.dev/gemini-api/docs/video-understanding#technical-details-about-videos
        config_kwargs["media_resolution"] = "MEDIA_RESOLUTION_LOW"
    config = types.GenerateContentConfig(**config_kwargs)

    max_retries_rate = 3  # 429 rate-limit: short bursts, resolve quickly
    max_retries_server = 8  # 503 overload: capacity issues, may last minutes
    for attempt in range(max(max_retries_rate, max_retries_server) + 1):
        try:
            stream = client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
            break
        except Exception as e:
            retry = get_retry_delay(
                e,
                attempt,
                max_retries_rate=max_retries_rate,
                max_retries_server=max_retries_server,
            )
            if retry is None:
                raise
            kind, wait, max_for_type = retry
            log.warning(
                "%s — retry %d/%d in %.0fs (Ctrl+C to abort)",
                kind,
                attempt + 1,
                max_for_type,
                wait,
            )
            time.sleep(wait)

    # Start heartbeat thread
    start_time = time.time()
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(stop_heartbeat, start_time), daemon=True)
    heartbeat.start()

    accumulated = []
    usage = None
    finish_reason = None
    first_chunk = True
    # Wall-clock timeout for first chunk. Gemini Pro can take 10-15 min to start
    # on long videos, so 20 min is generous but bounded.  The httpx read=1200
    # timeout is unreliable here because HTTP-level keepalives reset it.
    first_chunk_timeout = 1200  # 20 min
    stall_timeout = 300  # 5 min gap after streaming starts

    # Drain stream in a background thread so we can enforce wall-clock timeouts
    # via queue.get().  The iterator blocks inside __next__() when Gemini is
    # "thinking", and in-loop timeout checks never fire if __next__ never returns.
    chunk_q: queue.Queue = queue.Queue()

    def _drain():
        try:
            for chunk in stream:
                chunk_q.put(("chunk", chunk))
            chunk_q.put(("done", None))
        except Exception as exc:
            chunk_q.put(("error", exc))

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    try:
        while True:
            timeout = first_chunk_timeout if first_chunk else stall_timeout
            try:
                kind, value = chunk_q.get(timeout=timeout)
            except queue.Empty:
                if first_chunk:
                    raise TimeoutError(f"No data received after {format_elapsed(first_chunk_timeout)}") from None
                raise TimeoutError(f"Stream stalled for {format_elapsed(stall_timeout)}") from None

            if kind == "done":
                break
            if kind == "error":
                raise value

            chunk = value
            if first_chunk:
                stop_heartbeat.set()
                ttfc = time.time() - start_time
                log.info("First chunk received (%s). Streaming translation...", format_elapsed(ttfc))
                first_chunk = False

            text = chunk.text
            if text:
                accumulated.append(text)
                # Progressive write to tmp file
                if tmp_file:
                    tmp_file.write(text)
                    tmp_file.flush()
                # Live progress to stderr
                print(text, end="", file=sys.stderr, flush=True)

            # Capture usage metadata and finish reason from final chunk.
            # finish_reason lives on chunk.candidates[0].finish_reason per the
            # google-genai SDK — NOT on content.parts. Getting this path
            # wrong silently produces `finish_reason = None` and masks
            # exactly the diagnostic this capture is meant to surface.
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                meta = chunk.usage_metadata
                usage = {
                    "prompt_token_count": getattr(meta, "prompt_token_count", None),
                    "candidates_token_count": getattr(meta, "candidates_token_count", None),
                    "total_token_count": getattr(meta, "total_token_count", None),
                }
            if hasattr(chunk, "candidates") and chunk.candidates:
                candidate_reason = getattr(chunk.candidates[0], "finish_reason", None)
                if candidate_reason is not None:
                    # Normalize enum or string to a bare name ("STOP", "SAFETY", ...)
                    finish_reason = getattr(candidate_reason, "name", str(candidate_reason))

    except TimeoutError as e:
        stop_heartbeat.set()
        log.error("Timed out: %s", e)
        raise
    except Exception as e:
        stop_heartbeat.set()
        partial = "".join(accumulated)
        if partial:
            log.error("Stream interrupted after %d chars: %s", len(partial), e)
            log.error("Partial output preserved in .txt.tmp file")
        else:
            log.error("Stream failed before any output: %s", e)
        raise
    finally:
        stop_heartbeat.set()
        # Newline after streaming output
        print("", file=sys.stderr)

    return "".join(accumulated), usage, finish_reason


# ---------------------------------------------------------------------------
# Captions-path translation (text-only, no video)
# ---------------------------------------------------------------------------


AUTO_GEN_CLEANUP_NOTE = (
    "The English input is auto-generated from YouTube's ASR and may lack "
    "punctuation or contain minor misrecognitions. Apply natural punctuation "
    "and capitalization during translation."
)


def build_srt_prompt(
    is_auto_generated: bool,
    video_duration_hms: str,
    input_line_count: int,
) -> str:
    """Load the SRT-translation prompt with the template slots filled.

    Substitutes three template variables:
    - `{{VIDEO_DURATION}}`: a human-readable duration string like `"1h 4m 5s"`
      so Gemini is grounded in the actual length of the source video.
    - `{{INPUT_LINE_COUNT}}`: the exact number of timestamped input lines,
      used by the prompt's positive 1-to-1 count invariant to anchor the
      model on "produce exactly this many output lines". Empirically
      observed to reduce hallucinated continuation on long SRT inputs.
    - `{{AUTO_GEN_NOTE}}`: an empty string for manual caption tracks, or a
      short instruction for auto-generated tracks to silently apply
      punctuation and capitalization repairs during translation.
    """
    base = load_prompt("translate-bcs-from-srt")
    note = AUTO_GEN_CLEANUP_NOTE if is_auto_generated else ""
    return (
        base.replace("{{VIDEO_DURATION}}", video_duration_hms)
        .replace("{{INPUT_LINE_COUNT}}", str(input_line_count))
        .replace("{{AUTO_GEN_NOTE}}", note)
        .strip()
        + "\n"
    )


def validate_thinking_budget(model: str, budget: int | None) -> None:
    """Validate `--thinking-budget N` against the target model's allowed range.

    Ranges sourced from https://ai.google.dev/gemini-api/docs/thinking as of
    Apr 2026. 2.5 Pro cannot disable thinking (minimum 128); 2.5 Flash can
    (minimum 0); Gemini 3.x uses `thinking_level` (low/medium/high) instead
    and rejects `thinking_budget` with a 400. None is a no-op — leave the
    SDK's dynamic-thinking default in place.
    """
    if budget is None:
        return
    if "gemini-3" in model:
        raise SystemExit(
            f"--thinking-budget is not valid for {model}. "
            "Gemini 3.x uses thinking_level (low/medium/high), not thinking_budget."
        )
    if "2.5-pro" in model:
        if not (128 <= budget <= 32768):
            raise SystemExit(
                f"Gemini 2.5 Pro requires --thinking-budget in [128, 32768] (got {budget}). "
                "Pro cannot disable thinking; pass 128 for the absolute minimum."
            )
        return
    if "2.5-flash" in model:
        if not (0 <= budget <= 24576):
            raise SystemExit(
                f"Gemini 2.5 Flash requires --thinking-budget in [0, 24576] (got {budget}). "
                "Pass 0 to disable thinking entirely on Flash."
            )
        return
    # Unknown 2.5 variant or future model: let the API decide.


def translate_captions_text(
    client,
    types,
    model: str,
    captions_block: str,
    is_auto_generated: bool,
    video_duration_hms: str,
    input_line_count: int,
    thinking_budget: int | None = None,
) -> tuple[str, dict | None, str | None]:
    """Translate a [HH:MM:SS]-prefixed English caption block to BCS via Gemini.

    Streaming text-only request. Routes through `_stream_with_timeouts` for
    the same heartbeat / wall-clock first-chunk timeout / stall timeout /
    finish_reason capture as the video path. Non-streaming was tried first
    and hung for 12+ minutes on a 2098-line input with no progress signal,
    which is why this path now insists on streaming end-to-end.

    `video_duration_hms` is a human-readable duration string (e.g. "1h 4m 5s")
    that is substituted into the prompt to ground Gemini in the actual length
    of the source video. Passing a fallback like "an unknown length" is fine
    when YouTube metadata is unavailable.

    `input_line_count` is the exact number of timestamped lines in
    `captions_block`; it is substituted into the prompt's 1-to-1 count
    invariant so Gemini has an explicit output-count target.

    `thinking_budget`, if set, caps Gemini 2.5 internal reasoning tokens via
    ThinkingConfig. None leaves the SDK's dynamic-thinking default in place.
    See `validate_thinking_budget` for model-specific ranges.

    Returns: (text, usage_metadata, finish_reason). finish_reason is
    normalized to a bare name ("STOP", "SAFETY", ...) when available.
    """
    system_prompt = build_srt_prompt(is_auto_generated, video_duration_hms, input_line_count)
    config_kwargs: dict = {
        "system_instruction": system_prompt,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.2,
        "safety_settings": build_permissive_safety_settings(types),
    }
    if thinking_budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    config = types.GenerateContentConfig(**config_kwargs)
    # google-genai accepts a bare string as the contents argument and
    # treats it as a single user-role text part. No need to wrap.
    return _stream_with_timeouts(client, model, captions_block, config)


def build_transcript_prompt(video_title: str, source_url: str) -> str:
    """Load the transcript-translation prompt with grounding context filled.

    Unlike `build_srt_prompt`, this prompt has no line-count invariant —
    a rich transcript mixes speech lines, SCREEN sections, and optional
    OCR lines, so "one output per input" does not apply. Instead the
    prompt lists the structural elements to preserve verbatim and the
    content elements to translate.
    """
    base = load_prompt("translate-bcs-from-transcript")
    return base.replace("{{VIDEO_TITLE}}", video_title).replace("{{SOURCE_URL}}", source_url).strip() + "\n"


def translate_transcript_text(
    client,
    types,
    model: str,
    transcript_body: str,
    video_title: str,
    source_url: str,
    thinking_budget: int | None = None,
) -> tuple[str, dict | None, str | None]:
    """Translate a rich transcript markdown body to BCS via Gemini.

    Parallel to `translate_captions_text` — separate helper rather than a
    shared refactor to keep the diff small and leave the stable captions
    path untouched. Streaming text-only request through the same
    `_stream_with_timeouts` pipeline, same safety settings, same
    thinking_budget plumbing.

    `transcript_body` should be the post-header body of a
    `video_intel.py transcript` output file (the caller strips the YAML-ish
    header lines before passing the content in).

    Returns: (text, usage_metadata, finish_reason).
    """
    system_prompt = build_transcript_prompt(video_title=video_title, source_url=source_url)
    config_kwargs: dict = {
        "system_instruction": system_prompt,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.2,
        "safety_settings": build_permissive_safety_settings(types),
    }
    if thinking_budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    config = types.GenerateContentConfig(**config_kwargs)
    return _stream_with_timeouts(client, model, transcript_body, config)


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------


def _write_srt_only(
    *,
    captions: CaptionsResult,
    output_dir: Path,
    title: str,
    date: str,
    use_stdout: bool,
    force: bool,
) -> None:
    """Write the fetched English captions to `.en.srt` and exit.

    Mirrors the filename convention of `_translate_via_captions` so the
    resulting SRT can sit alongside a future BCS translation of the same
    video. Respects `--stdout` (prints SRT text to stdout, writes no
    file) and `--force` (overwrites an existing SRT). Assumes captions
    came back with durations populated; falls back silently to the
    two-second default inside `format_captions_as_srt` if not.
    """
    srt_text = format_captions_as_srt(captions.snippets, captions.durations)

    if use_stdout:
        print(srt_text)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(title)
    srt_path = output_dir / f"{date}-{slug}.en.srt"

    if srt_path.exists() and not force:
        log.info("SRT already exists: %s (use --force to regenerate)", srt_path)
        return

    srt_path.write_text(srt_text, encoding="utf-8")
    log.info("SRT:       %s", srt_path)


def _translate_via_captions(
    *,
    video_id: str,
    canonical_url: str,
    title: str,
    date: str,
    model_name: str,
    duration_seconds: int,
    captions: CaptionsResult,
    output_dir: Path,
    use_stdout: bool,
    force: bool,
    start_minutes: int | None,
    end_minutes: int | None,
    thinking_budget: int | None = None,
) -> None:
    """Captions-first translation path.

    Runs when YouTube has an English caption track for the video. One
    non-streaming Gemini call per video, text-only input, no chunking,
    no stitch step. Writes the same output file format as the video path
    (header + `[HH:MM:SS]` body) with a `**Source mode:**` field added
    so the reader knows the text came from captions, not video audio.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)

    _, types = require_gemini()
    client = create_client(api_key)

    manual_range = start_minutes is not None or end_minutes is not None
    filtered = filter_snippets_by_range(captions.snippets, start_minutes, end_minutes)
    if not filtered:
        log.error("Captions filtered to empty range (start=%s, end=%s)", start_minutes, end_minutes)
        sys.exit(1)

    # Resolve output path
    output_path = None
    tmp_path = None
    if not use_stdout:
        output_dir.mkdir(parents=True, exist_ok=True)
        if manual_range:
            slug = slugify(title)
            range_tag = f"part-{start_minutes or 0}-{end_minutes or 'end'}"
            output_path = output_dir / f"{date}-{slug}.{range_tag}.translate-bcs.txt"
        else:
            output_path = build_output_path(output_dir, title, date)

        if output_path.exists() and not force:
            log.info("Already translated: %s (use --force to redo)", output_path)
            return

        tmp_path = output_path.with_suffix(".txt.tmp")
        log.info("Output:    %s", output_path)

        # Write the raw English SRT sibling BEFORE the Gemini call so the
        # artifact survives even when translation fails, times out, or gets
        # soft-stopped. The SRT is the *full* caption track (unfiltered),
        # not the Gemini-input range — it's a reviewable reference for the
        # user and a free replacement for third-party YouTube SRT downloaders.
        # Not written in manual-range runs because those produce a partial
        # translation output, and the full SRT would be confusingly broader
        # than its BCS sibling. Silent on failure: a write error here is
        # never worth aborting the translation.
        if captions.durations and not manual_range:
            # Strip the full ".translate-bcs.txt" double-extension rather
            # than using Path.with_suffix (which only peels one level)
            # to produce "<date>-<slug>.en.srt" cleanly.
            base_name = output_path.name.removesuffix(".translate-bcs.txt")
            srt_path = output_path.parent / f"{base_name}.en.srt"
            try:
                srt_text = format_captions_as_srt(captions.snippets, captions.durations)
                srt_path.write_text(srt_text, encoding="utf-8")
                log.info("SRT:       %s", srt_path)
            except OSError as e:
                log.warning("Failed to write SRT sibling %s: %s", srt_path, e)

    log.info("Model:     %s", model_name)
    log.info("Video:     %s", canonical_url)
    log.info("Title:     %s", title)
    if duration_seconds:
        log.info("Duration:  %s", format_elapsed(duration_seconds))

    # Build the [HH:MM:SS]-prefixed input block
    captions_block = format_captions_for_translation(filtered)
    input_line_count = captions_block.count("\n") + 1
    last_input_seconds = int(filtered[-1][0]) if filtered else 0
    log.info(
        "Input:     %d caption snippets, last timestamp %s",
        input_line_count,
        _format_hhmmss(last_input_seconds),
    )

    # Human-readable duration string for the prompt. Falls back gracefully
    # when YouTube metadata is unavailable (no duration_seconds) so Gemini
    # still gets a coherent sentence instead of a raw {{VIDEO_DURATION}} token.
    video_duration_hms = format_elapsed(duration_seconds) if duration_seconds else "an unknown length"

    effective_budget = thinking_budget
    if effective_budget is None and "2.5-pro" in model_name:
        effective_budget = SRT_DEFAULT_THINKING_BUDGET
        log.info("Thinking:  budget=%d (SRT default for 2.5 Pro)", effective_budget)

    start_time = time.time()
    log.info("Sending to Gemini...")
    text, usage, finish_reason = translate_captions_text(
        client,
        types,
        model_name,
        captions_block,
        is_auto_generated=captions.is_generated,
        video_duration_hms=video_duration_hms,
        input_line_count=input_line_count,
        thinking_budget=effective_budget,
    )
    elapsed = time.time() - start_time

    if finish_reason and finish_reason != "STOP":
        log.warning("finish_reason: %s (not STOP)", finish_reason)

    if not text or not text.strip():
        log.error("Gemini returned empty response after %s", format_elapsed(elapsed))
        sys.exit(1)

    if use_stdout:
        print(text)
        line_count = text.count("\n") + 1
        log.info(format_stats(elapsed, line_count, usage))
        return

    # Coverage sanity check on the captions path. Reference point is the
    # LAST INPUT TIMESTAMP, not the video duration, because caption tracks
    # legitimately end before the video runs out (music-only tails, rolling
    # credits, silence). The question we want answered is "did Gemini
    # translate every caption line we gave it?", not "did the captions
    # cover the whole video?".
    observed_end = extract_last_timestamp_seconds(text)

    # Coverage tuple for the header. For a full-captions run we synthesize
    # one based on the actual caption span (not the video duration) so the
    # header's Coverage line and the truncation check use the same anchor.
    cov_for_header: tuple[int, int] | None = None
    if manual_range:
        cov_start_min = start_minutes or 0
        cov_end_min = (
            end_minutes if end_minutes is not None else (duration_seconds // 60 if duration_seconds else cov_start_min)
        )
        cov_for_header = (cov_start_min, cov_end_min)
    elif duration_seconds:
        # Coverage window is [0, ceil(last_input / 60)] so the header
        # matches the span Gemini was actually asked to translate.
        captions_end_min = (last_input_seconds + 59) // 60
        cov_for_header = (0, captions_end_min)

    observed_end_for_header: int | None = None
    if observed_end is not None and last_input_seconds > 0:
        ratio = observed_end / last_input_seconds
        if ratio < 0.95:
            observed_end_for_header = observed_end
            log.warning(
                "Captions translation ended at %s but input goes to %s (%.0f%% covered) - Gemini may have truncated.",
                _format_hhmmss(observed_end),
                _format_hhmmss(last_input_seconds),
                ratio * 100,
            )

    # Overshoot detection (Phase 5): Gemini sometimes produces timestamps
    # past the real end of the input, either by drifting or by inventing
    # hallucinated content. We do NOT truncate — the user pays for the
    # translation and keeps full control of what to trim. We surface the
    # overshoot as a stderr WARNING and append a visible markdown warning
    # block to the END of the output file so a reader scrolling to the
    # tail sees it right next to the questionable content.
    overshoot_notice = ""
    if last_input_seconds > 0:
        overshoot = detect_overshoot(text, last_input_seconds)
        if overshoot is not None:
            first_over_line, last_obs = overshoot
            log.warning(
                "Possible hallucinated overshoot: output extends to %s but input ended at %s "
                "(first overshoot line ~%d). Content preserved; review manually.",
                _format_hhmmss(last_obs),
                _format_hhmmss(last_input_seconds),
                first_over_line,
            )
            overshoot_notice = (
                "\n\n"
                + build_overshoot_notice(
                    last_input_seconds=last_input_seconds,
                    last_observed_seconds=last_obs,
                    first_overshoot_line=first_over_line,
                )
                + "\n"
            )

    source_mode = "captions-autogen" if captions.is_generated else "captions-manual"
    header = build_header(
        title,
        canonical_url,
        date,
        model_name,
        coverage=cov_for_header,
        duration_seconds=duration_seconds or None,
        observed_end_seconds=observed_end_for_header,
        finish_reason=finish_reason,
        source_mode=source_mode,
    )

    tmp_path.write_text(header + text + overshoot_notice, encoding="utf-8")
    tmp_path.replace(output_path)

    line_count = text.count("\n") + 1
    log.info("Written to %s", output_path)
    log.info(format_stats(elapsed, line_count, usage))


# ---------------------------------------------------------------------------
# Transcript-input translation path (`--from-transcript`)
# ---------------------------------------------------------------------------


TRANSCRIPT_MAX_BYTES = 500_000
TRANSCRIPT_TIMESTAMP_RE = re.compile(rf"^\[(?:{TS_HOURS}:)?{TS_MINUTES}:{TS_SECONDS}\]", re.MULTILINE)


def parse_transcript_header(text: str) -> dict[str, str]:
    """Best-effort extraction of title / source URL / published date.

    Scans the first ~25 lines. Missing fields come back absent from the
    dict — the caller falls back to filename-derived defaults. Never
    raises on malformed headers; this is grounding context for the
    prompt, not a correctness gate.
    """
    header: dict[str, str] = {}
    for line in text.splitlines()[:25]:
        stripped = line.strip()
        if stripped.startswith("# Transcript:") and "title" not in header:
            header["title"] = stripped[len("# Transcript:") :].strip()
        elif stripped.startswith("**Source:**") and "source" not in header:
            header["source"] = stripped[len("**Source:**") :].strip()
        elif stripped.startswith("**Published:**") and "published" not in header:
            header["published"] = stripped[len("**Published:**") :].strip()
    return header


def _translate_from_transcript(
    *,
    transcript_path: Path,
    model_name: str,
    output_dir: Path,
    use_stdout: bool,
    force: bool,
    thinking_budget: int | None = None,
) -> None:
    """Translate a pre-generated transcript markdown file into BCS.

    Permissive validation: file exists, within size guard, contains at
    least one `[MM:SS]` timestamp line. No structural canaries, no
    overshoot detector — a rich transcript has no single invariant that
    would flag hallucination reliably. If `thinking_budget` is None and
    the model is 2.5 Pro, applies `SRT_DEFAULT_THINKING_BUDGET` (same
    mitigation used on the captions path).

    Output: `.translate-bcs.txt` in `output_dir` (default `./examples`,
    same as every other translate_video.py output). Respects `--stdout`
    (print, write nothing) and `--force` (overwrite existing).
    """
    if not transcript_path.exists():
        log.error("Transcript file not found: %s", transcript_path)
        sys.exit(1)

    try:
        size = transcript_path.stat().st_size
    except OSError as e:
        log.error("Cannot stat transcript file %s: %s", transcript_path, e)
        sys.exit(1)

    if size > TRANSCRIPT_MAX_BYTES:
        log.error(
            "Transcript file %s is %d bytes (>%d limit). Is this really a transcript?",
            transcript_path,
            size,
            TRANSCRIPT_MAX_BYTES,
        )
        sys.exit(1)

    try:
        text = transcript_path.read_text(encoding="utf-8")
    except OSError as e:
        log.error("Cannot read transcript file %s: %s", transcript_path, e)
        sys.exit(1)

    if not TRANSCRIPT_TIMESTAMP_RE.search(text):
        log.error(
            "Transcript %s contains no [MM:SS] timestamp lines - does not look like a "
            "transcript produced by `video_intel.py transcript`.",
            transcript_path,
        )
        sys.exit(1)

    header_fields = parse_transcript_header(text)
    title = header_fields.get("title") or transcript_path.stem.removesuffix(".transcript")
    source_url = header_fields.get("source") or f"file://{transcript_path.resolve()}"
    published = header_fields.get("published") or "unknown"

    # Output: `.translate-bcs.txt` in output_dir (same convention as URL-based translation).
    name = transcript_path.name
    if name.endswith(".transcript.md"):
        output_name = name.removesuffix(".transcript.md") + ".translate-bcs.txt"
    else:
        output_name = transcript_path.stem + ".translate-bcs.txt"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / output_name

    if not use_stdout and output_path.exists() and not force:
        log.info("Already translated: %s (use --force to redo)", output_path)
        return

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)

    _, types = require_gemini()
    client = create_client(api_key)

    # Translate the title to BCS for the output header (same helper the stitch path uses).
    bcs_title = translate_title(client, model_name, title)
    log.info("Title:     %s → %s", title, bcs_title)

    effective_budget = thinking_budget
    if effective_budget is None and "2.5-pro" in model_name:
        effective_budget = SRT_DEFAULT_THINKING_BUDGET
        log.info("Thinking:  budget=%d (SRT default for 2.5 Pro)", effective_budget)

    log.info("Model:     %s", model_name)
    log.info("Input:     %s (%d bytes)", transcript_path, size)
    if not use_stdout:
        log.info("Output:    %s", output_path)

    start_time = time.time()
    log.info("Sending to Gemini...")
    translated, usage, finish_reason = translate_transcript_text(
        client,
        types,
        model_name,
        text,
        video_title=title,
        source_url=source_url,
        thinking_budget=effective_budget,
    )
    elapsed = time.time() - start_time

    if finish_reason and finish_reason != "STOP":
        log.warning("finish_reason: %s (not STOP)", finish_reason)

    if not translated or not translated.strip():
        log.error("Gemini returned empty response after %s", format_elapsed(elapsed))
        sys.exit(1)

    # Minimal sanity check per plan: output must contain at least one
    # [MM:SS] timestamp. Zero means catastrophic structural collapse.
    if not TRANSCRIPT_TIMESTAMP_RE.search(translated):
        log.warning(
            "Translated output contains no [MM:SS] timestamps - structure may have been lost. "
            "Content preserved; review manually."
        )

    if use_stdout:
        print(translated)
        line_count = translated.count("\n") + 1
        log.info(format_stats(elapsed, line_count, usage))
        return

    header_block = build_header(
        bcs_title,
        source_url,
        published,
        model_name,
        original_title=title,
        finish_reason=finish_reason,
        source_mode="transcript",
    )
    tmp_path = output_path.with_suffix(".txt.tmp")
    tmp_path.write_text(header_block + translated, encoding="utf-8")
    tmp_path.replace(output_path)

    line_count = translated.count("\n") + 1
    log.info("Written to %s", output_path)
    log.info(format_stats(elapsed, line_count, usage))


def translate_video(
    video_url: str,
    model_name: str,
    output_dir: Path,
    *,
    title_override: str | None = None,
    date_override: str | None = None,
    use_stdout: bool = False,
    force: bool = False,
    high_res: bool = False,
    chunk_minutes: int = 20,
    start_minutes: int | None = None,
    end_minutes: int | None = None,
    force_video: bool = False,
    srt_only: bool = False,
    thinking_budget: int | None = None,
) -> None:
    """Translate a YouTube video's audio to BCS subtitles.

    Default behavior is SRT-first: if the video has an English caption
    track on YouTube, fetch it and translate the text through Gemini in
    a single non-streaming call. This avoids the silent-truncation
    failure mode of long-video understanding. Falls back to the
    video-understanding path when no captions are available.

    Set `force_video=True` to skip the captions check and go straight
    to the video-understanding path (for testing the fallback, or when
    caption quality is known to be bad).
    """
    _, types = require_gemini()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        log.error("GEMINI_API_KEY not set. Get a free key at https://aistudio.google.com/apikey")
        sys.exit(1)

    # Resolve video metadata
    video_id = extract_video_id(video_url)
    if not video_id:
        log.error("Could not extract video ID from: %s", video_url)
        sys.exit(1)

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    title = title_override
    date = date_override
    duration_seconds = 0

    # Always fetch metadata for duration (needed for auto-chunking even with --title/--date)
    meta = fetch_video_metadata(video_id)
    if meta:
        title = title or meta["title"]
        date = date or meta["published"]
        duration_seconds = meta.get("duration_seconds", 0)

    # Stable fallback when no metadata available
    title = title or video_id
    date = date or "0000-00-00"

    # --srt-only: fetch captions and write the .en.srt sibling, then exit.
    # No Gemini call. Useful as a free replacement for downsubs.com-style
    # third-party sites, and as a fast shortcut when you only need the
    # English source (monolingual summarization, review, quoting). Strict
    # on missing captions — falling through to video translation here
    # would violate the "just give me the SRT" contract.
    if srt_only:
        captions = fetch_english_captions(video_id)
        if captions is None:
            log.error(
                "No English captions available for %s — cannot produce SRT. "
                "Remove --srt-only to fall back to video translation.",
                canonical_url,
            )
            sys.exit(1)
        log.info(
            "Captions:  YouTube %s (%s)",
            captions.language,
            "auto-generated" if captions.is_generated else "manual",
        )
        _write_srt_only(
            captions=captions,
            output_dir=output_dir,
            title=title,
            date=date,
            use_stdout=use_stdout,
            force=force,
        )
        return

    # SRT-first: try to use YouTube's English captions before spending any
    # tokens on video understanding. This avoids the silent-truncation
    # failure mode documented in ADR-0015 and its companion solution doc.
    # Any caption-path failure falls through to the existing video path.
    if not force_video:
        captions = fetch_english_captions(video_id)
        if captions is not None:
            log.info(
                "Captions:  YouTube %s (%s) - skipping video translation",
                captions.language,
                "auto-generated" if captions.is_generated else "manual",
            )
            _translate_via_captions(
                video_id=video_id,
                canonical_url=canonical_url,
                title=title,
                date=date,
                model_name=model_name,
                duration_seconds=duration_seconds,
                captions=captions,
                output_dir=output_dir,
                use_stdout=use_stdout,
                force=force,
                start_minutes=start_minutes,
                end_minutes=end_minutes,
                thinking_budget=thinking_budget,
            )
            return

    # Validate chunk size
    if chunk_minutes < 1:
        log.error("--chunk-minutes must be at least 1 (got %d)", chunk_minutes)
        sys.exit(1)

    # Resolve the time range to translate
    user_start = (start_minutes * 60) if start_minutes is not None else 0
    if end_minutes is not None:
        user_end = end_minutes * 60
    elif start_minutes is not None and not duration_seconds:
        # --start without --end and no duration metadata: require --end
        log.error("--start requires --end when video duration is unknown (set YOUTUBE_API_KEY or provide --end)")
        sys.exit(1)
    else:
        user_end = duration_seconds or 0

    # Determine if we need chunking
    manual_range = start_minutes is not None or end_minutes is not None

    if manual_range:
        needs_chunking = False
    else:
        needs_chunking = duration_seconds > single_request_cap_seconds(high_res)

    # Resolve output directory
    if not use_stdout:
        output_dir.mkdir(parents=True, exist_ok=True)

    # For single-request and manual-range modes, resolve output path up front.
    # For auto-chunking, each chunk gets its own part file (resolved in the loop).
    output_path = None
    tmp_path = None
    if not use_stdout and not needs_chunking:
        if manual_range:
            slug = slugify(title)
            range_tag = f"part-{start_minutes or 0}-{end_minutes or 'end'}"
            output_path = output_dir / f"{date}-{slug}.{range_tag}.translate-bcs.txt"
        else:
            output_path = build_output_path(output_dir, title, date)

        if output_path.exists() and not force:
            log.info("Already translated: %s (use --force to redo)", output_path)
            return

        tmp_path = output_path.with_suffix(".txt.tmp")

    # Build chunk list
    if needs_chunking:
        chunks = build_chunk_list(duration_seconds, chunk_minutes, high_res=high_res)
        log.info(
            "Duration:  %s (auto-chunked into %d x ~%dm segments)",
            format_elapsed(duration_seconds),
            len(chunks),
            chunk_minutes,
        )
    elif manual_range:
        chunks = [(user_start, user_end)]
        log.info("Range:     %s → %s", format_elapsed(user_start), format_elapsed(user_end))
    elif duration_seconds:
        chunks = [(0, 0)]  # (0, 0) means no clipping — full video
        log.info("Duration:  %s", format_elapsed(duration_seconds))
    else:
        chunks = [(0, 0)]

    log.info("Model:     %s", model_name)
    log.info("Video:     %s", canonical_url)
    log.info("Title:     %s", title)
    if high_res:
        log.info("Resolution: high (~300 tokens/sec, ~55 min per request)")
    if output_path:
        log.info("Output:    %s", output_path)
        log.info('Progress:  tail -f "%s"', tmp_path)

    # Load prompt and call Gemini with streaming
    system_prompt = load_prompt("translate-bcs")
    client = create_client(api_key)
    slug = slugify(title)

    start_time = time.time()
    last_usage = None
    part_files_written: list[Path] = []

    # Clear stale part files before either branch runs. Without this, a video
    # that previously went through the chunked path and is now running in
    # single-request mode (e.g. after F1 lowered the chunking threshold) would
    # leave old part-N-N files on disk alongside the new single-request output.
    # The stitcher would then see them as canonical artifacts and produce a
    # garbage merge.
    if force:
        stale_parts = sorted(output_dir.glob(f"{date}-{slug}.part-*.translate-bcs.txt"))
        stale_tmps = sorted(output_dir.glob(f"{date}-{slug}.part-*.translate-bcs.txt.tmp"))
        for stale_path in stale_parts + stale_tmps:
            stale_path.unlink(missing_ok=True)
        if stale_parts or stale_tmps:
            log.info("Cleared %d existing part file(s) for forced re-run", len(stale_parts) + len(stale_tmps))

    if needs_chunking:
        # Auto-chunk mode: each chunk produces its own part file (canonical artifacts)
        for i, (s_off, e_off) in enumerate(chunks):
            s_min, e_min = s_off // 60, e_off // 60
            part_path = output_dir / f"{date}-{slug}.part-{s_min}-{e_min}.translate-bcs.txt"
            part_tmp = part_path.with_suffix(".txt.tmp")

            if part_path.exists() and not force:
                log.info("Chunk %d/%d already exists: %s", i + 1, len(chunks), part_path.name)
                part_files_written.append(part_path)
                continue

            log.info("Chunk %d/%d: %s → %s", i + 1, len(chunks), format_elapsed(s_off), format_elapsed(e_off))
            tmp_file = None
            try:
                tmp_file = open(part_tmp, "w", encoding="utf-8")  # noqa: SIM115
                text, usage, finish_reason_chunk = call_gemini_translate(
                    client,
                    types,
                    canonical_url,
                    system_prompt,
                    model_name,
                    tmp_file,
                    high_res=high_res,
                    start_offset=s_off,
                    end_offset=e_off,
                )
                if finish_reason_chunk and finish_reason_chunk != "STOP":
                    log.warning("Chunk %d/%d finish_reason: %s (not STOP)", i + 1, len(chunks), finish_reason_chunk)
                if usage:
                    last_usage = usage
            except TimeoutError:
                log.warning(
                    "Chunk %d/%d timed out (%s → %s). Skipping — retry with: --start %d --end %d",
                    i + 1,
                    len(chunks),
                    format_elapsed(s_off),
                    format_elapsed(e_off),
                    s_min,
                    e_min,
                )
                if tmp_file:
                    tmp_file.close()
                continue
            except Exception:
                if tmp_file:
                    tmp_file.close()
                if part_tmp.exists() and part_tmp.stat().st_size > 0:
                    log.error("Partial chunk saved: %s", part_tmp)
                raise
            finally:
                if tmp_file and not tmp_file.closed:
                    tmp_file.close()

            if text and text.strip():
                part_tmp.write_text(text, encoding="utf-8")
                part_tmp.replace(part_path)
                part_files_written.append(part_path)
                log.info("Written part: %s", part_path.name)
            elif part_tmp.exists():
                part_tmp.unlink()

        elapsed = time.time() - start_time
        if not part_files_written:
            log.error("No chunks completed after %s", format_elapsed(elapsed))
            sys.exit(1)

        log.info(
            "Completed %d/%d chunks in %s. Stitch with: --stitch",
            len(part_files_written),
            len(chunks),
            format_elapsed(elapsed),
        )
        if last_usage:
            log.info(format_stats(elapsed, 0, last_usage))

    else:
        # Single-request mode (no chunking, or manual range)
        all_text_parts = []
        last_finish_reason: str | None = None
        tmp_file = None
        try:
            if tmp_path:
                tmp_file = open(tmp_path, "w", encoding="utf-8")  # noqa: SIM115

            for s_off, e_off in chunks:
                use_clipping = s_off > 0 or e_off > 0
                if use_clipping:
                    log.info("Sending to Gemini (%s → %s)...", format_elapsed(s_off), format_elapsed(e_off))
                else:
                    log.info("Sending to Gemini...")

                text, usage, finish_reason_req = call_gemini_translate(
                    client,
                    types,
                    canonical_url,
                    system_prompt,
                    model_name,
                    tmp_file,
                    high_res=high_res,
                    start_offset=s_off if use_clipping else None,
                    end_offset=e_off if use_clipping else None,
                )
                if finish_reason_req:
                    last_finish_reason = finish_reason_req
                    if finish_reason_req != "STOP":
                        log.warning("finish_reason: %s (not STOP)", finish_reason_req)
                if text and text.strip():
                    all_text_parts.append(text)
                if usage:
                    last_usage = usage

        except Exception:
            if tmp_file:
                tmp_file.close()
            if tmp_path and tmp_path.exists() and tmp_path.stat().st_size > 0:
                log.error("Partial translation saved: %s", tmp_path)
            raise
        finally:
            if tmp_file and not tmp_file.closed:
                tmp_file.close()

        elapsed = time.time() - start_time
        full_text = "\n".join(all_text_parts)

        if not full_text.strip():
            log.error("Gemini returned empty response after %s", format_elapsed(elapsed))
            sys.exit(1)

        if use_stdout:
            print(full_text)
            line_count = full_text.count("\n") + 1
            log.info(format_stats(elapsed, line_count, last_usage))
            return

        # F1b: single-request coverage sanity check.
        # Taking the video out of chunking does not take it out of the
        # truncation blast radius — if Gemini silently stops early,
        # there's no stitch step to surface the problem. Compare the last
        # observed timestamp against the known video duration and warn
        # loudly if the run under-covers.
        observed_end = None
        if duration_seconds and not manual_range:
            observed_end = extract_last_timestamp_seconds(full_text)
            if observed_end is not None:
                ratio = observed_end / duration_seconds
                if ratio < 0.95:
                    log.warning(
                        "Translation ended at %s but video duration is %s "
                        "(%.0f%% covered) — Gemini may have truncated. "
                        "Verify output or rerun with --force.",
                        _format_hhmmss(observed_end),
                        _format_hhmmss(duration_seconds),
                        ratio * 100,
                    )

        # For manual --start/--end, pass a coverage tuple matching the
        # requested range so the header does not falsely claim full-video
        # coverage. F1b annotation is skipped (observed_end stays None) for
        # the same reason — observed clip time is not comparable to total
        # duration for partial translations.
        cov_for_header: tuple[int, int] | None = None
        if manual_range and duration_seconds:
            cov_start_min = start_minutes or 0
            cov_end_min = end_minutes if end_minutes is not None else (duration_seconds // 60)
            cov_for_header = (cov_start_min, cov_end_min)

        # Atomic promotion: write header + full text, then rename
        header = build_header(
            title,
            canonical_url,
            date,
            model_name,
            coverage=cov_for_header,
            duration_seconds=duration_seconds or None,
            observed_end_seconds=observed_end,
            finish_reason=last_finish_reason,
        )
        tmp_path.write_text(header + full_text, encoding="utf-8")
        tmp_path.replace(output_path)

        line_count = full_text.count("\n") + 1
        log.info("Written to %s", output_path)
        log.info(format_stats(elapsed, line_count, last_usage))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate YouTube video audio to BCS subtitles via Gemini",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Token budget:
  Gemini's input limit is 1M tokens. Translation reads audio only, so this
  script defaults to low media resolution (~100 tokens/sec, fits videos up
  to ~170 min in a single request). Audio quality is unaffected — the
  media_resolution setting only controls video frame tokens, and audio is
  tokenized at a fixed 32 tokens/sec regardless. Use --high-res (~300
  tokens/sec, ~55 min per request) only when the prompt needs to read
  on-screen text such as slides or burned-in captions. The default prompt
  (translate-bcs) does not.

Examples:
  # Any talking-head video up to ~150 min — single pass, low-res default
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID"

  # Partial translation — e.g. first hour only, skip the interview segment
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --end 63

  # Stitch auto-chunked parts (triggered past 150 min low-res / 50 min high-res)
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --stitch

  # Backfill a failed or missing chunk
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --start 60 --end 80

  # Slide-driven talk where on-screen terminology matters (rare)
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --high-res

  # Custom output directory
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --output-dir ./translations
        """,
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="YouTube video URL (required unless --from-transcript is used)",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="gemini-2.5-pro",
        help="Gemini model (default: gemini-2.5-pro)",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument("--title", default=None, help="Override video title (auto-detected from YouTube)")
    parser.add_argument("--date", default=None, help="Override publish date YYYY-MM-DD (auto-detected from YouTube)")
    parser.add_argument("--start", type=int, default=None, help="Start time in minutes (for partial translation)")
    parser.add_argument("--end", type=int, default=None, help="End time in minutes (for partial translation)")
    parser.add_argument(
        "--chunk-minutes",
        type=int,
        default=20,
        help="Chunk size in minutes for auto-splitting long videos (default: 20)",
    )
    parser.add_argument(
        "--stitch", action="store_true", help="Stitch part files into a single translation (no Gemini call)"
    )
    parser.add_argument("--stdout", action="store_true", help="Print translation to stdout instead of file")
    parser.add_argument("--force", action="store_true", help="Overwrite existing translation")
    parser.add_argument(
        "--high-res",
        action="store_true",
        help=(
            "Use high media resolution (~300 tokens/sec vs the ~100 default). "
            "Only needed when the prompt reads on-screen text such as slides or burned-in captions — "
            "the default translate-bcs prompt does not. Caps single-request capacity at ~55 min."
        ),
    )
    parser.add_argument(
        "--force-video",
        action="store_true",
        help=(
            "Skip the YouTube captions check and go straight to video-understanding translation. "
            "Default is captions-first (fetch YouTube English captions if available, fall back to "
            "video only when no captions exist). Use this flag when caption quality is known to be "
            "bad or to force a full video-based rerun."
        ),
    )
    parser.add_argument(
        "--srt-only",
        action="store_true",
        help=(
            "Fetch YouTube English captions and write the .en.srt sibling file only. "
            "No Gemini call, no BCS translation. Exits nonzero if no captions are available. "
            "Useful as a free replacement for downsubs.com-style sites or as input for "
            "monolingual English summarization workflows."
        ),
    )
    parser.add_argument(
        "--from-transcript",
        dest="from_transcript",
        type=str,
        default=None,
        metavar="PATH",
        help=(
            "Translate a pre-generated transcript markdown file (from "
            "`video_intel.py transcript`) into BCS. Preserves timestamps, SCREEN "
            "sections, speaker labels, and on-screen text. Use when YouTube "
            "captions miss too much on-screen context. Output is written as a "
            "`.translate-bcs.txt` sibling next to the input transcript. "
            "Mutually exclusive with URL and with --srt-only/--force-video/--stitch/"
            "--chunk-minutes/--start/--end."
        ),
    )
    parser.add_argument(
        "--thinking-budget",
        type=int,
        default=None,
        help=(
            "Cap Gemini 2.5 thinking tokens via ThinkingConfig. "
            "Ranges: 2.5 Pro 128-32768 (cannot disable), 2.5 Flash 0-24576 (0 disables). "
            "SRT path defaults to 128 for 2.5 Pro (frees output capacity); "
            "override with an explicit value. Video path uses SDK dynamic. "
            "Not valid for Gemini 3.x."
        ),
    )
    parser.add_argument(
        "--ipv4",
        action="store_true",
        help="Force IPv4 connections (workaround for IPv6 socket stalls, see googleapis/python-genai#1893)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Set logging verbosity (default: info)",
    )
    args = parser.parse_args()

    # Mutual exclusions that argparse's builtin mutex groups can't express
    # cleanly (we want friendly error messages, not "unrecognized combo").
    if args.srt_only and args.force_video:
        parser.error("--srt-only and --force-video are mutually exclusive")
    if args.srt_only and args.thinking_budget is not None:
        parser.error("--srt-only and --thinking-budget are mutually exclusive (no Gemini call to configure)")
    if args.srt_only and args.stitch:
        parser.error("--srt-only and --stitch are mutually exclusive")

    # Exactly one input mode: either a URL (positional) or a transcript file.
    if args.from_transcript is None and args.url is None:
        parser.error("either URL or --from-transcript PATH is required")
    if args.from_transcript is not None and args.url is not None:
        parser.error("URL and --from-transcript are mutually exclusive")
    if args.from_transcript is not None:
        if args.srt_only:
            parser.error("--from-transcript and --srt-only are mutually exclusive")
        if args.force_video:
            parser.error("--from-transcript and --force-video are mutually exclusive")
        if args.stitch:
            parser.error("--from-transcript and --stitch are mutually exclusive")
        if args.start is not None or args.end is not None:
            parser.error("--from-transcript and --start/--end are mutually exclusive")
        # --chunk-minutes has a non-None default (20), so compare against that
        # to detect an actual user override rather than the argparse default.
        if args.chunk_minutes != 20:
            parser.error("--from-transcript and --chunk-minutes are mutually exclusive")

    # Validate --thinking-budget against the target model's allowed range.
    # Raises SystemExit with a clear message when misconfigured.
    validate_thinking_budget(args.model, args.thinking_budget)

    # IPv4 workaround — process-global monkey-patch on socket.getaddrinfo().
    # Affects ALL network calls in this process, not just Gemini.
    # See: https://github.com/googleapis/python-genai/issues/1893
    # The SDK can hang indefinitely when IPv6 routing is broken — no timeout, no error.
    # This forces IPv4-only resolution. Opt-in because IPv6 works on most networks.
    if args.ipv4:
        import socket

        _original_getaddrinfo = socket.getaddrinfo

        def _ipv4_only_getaddrinfo(*a, **kw):
            return [r for r in _original_getaddrinfo(*a, **kw) if r[0] == socket.AF_INET]

        socket.getaddrinfo = _ipv4_only_getaddrinfo
        log.info("Forcing IPv4 connections (--ipv4)")

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.setLevel(getattr(logging, args.log_level.upper()))

    output_dir = Path(args.output_dir).expanduser()

    if args.stitch:
        # Stitch mode: merge existing part files, no Gemini call
        video_id = extract_video_id(args.url)
        if not video_id:
            log.error("Could not extract video ID from: %s", args.url)
            sys.exit(1)
        meta = fetch_video_metadata(video_id)
        original_title = meta["title"] if meta else video_id
        date = args.date or (meta["published"] if meta else "0000-00-00")
        # --title overrides; otherwise auto-translate the title to BCS
        if args.title:
            display_title = args.title
        else:
            api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
            client = create_client(api_key)
            display_title = translate_title(client, args.model, original_title)
            log.info("Translated title: %s", display_title)
        url = f"https://www.youtube.com/watch?v={video_id}"
        total_duration = meta.get("duration_seconds") if meta else None
        result = stitch_parts(
            output_dir,
            display_title,
            date,
            url,
            args.model,
            original_title=original_title,
            duration_seconds=total_duration,
        )
        log.info("Stitched → %s", result)
        return

    # --from-transcript: translate a local transcript file, no YouTube calls.
    if args.from_transcript is not None:
        _translate_from_transcript(
            transcript_path=Path(args.from_transcript).expanduser(),
            model_name=args.model,
            output_dir=output_dir,
            use_stdout=args.stdout,
            force=args.force,
            thinking_budget=args.thinking_budget,
        )
        return

    translate_video(
        args.url,
        args.model,
        output_dir,
        title_override=args.title,
        date_override=args.date,
        use_stdout=args.stdout,
        force=args.force,
        high_res=args.high_res,
        chunk_minutes=args.chunk_minutes,
        start_minutes=args.start,
        end_minutes=args.end,
        force_video=args.force_video,
        srt_only=args.srt_only,
        thinking_budget=args.thinking_budget,
    )


if __name__ == "__main__":
    main()
