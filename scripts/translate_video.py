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

from gemini_common import create_client, get_retry_delay, require_gemini, require_youtube

log = logging.getLogger("translate_video")

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = "./examples"
MAX_OUTPUT_TOKENS = 65536


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


def normalize_timestamp(line: str) -> str:
    """Fix malformed timestamps where Gemini puts total minutes in the HH field.

    Rule: parse [A:MM:SS]. If A <= 23, leave unchanged. If A > 23, treat A as
    total minutes: divmod into hours, add remainder to MM, carry if needed.
    """
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


def apply_timestamp_offset(line: str, offset_seconds: int, chunk_duration_seconds: int) -> str:
    """Add a time offset to the timestamp at the start of a line.

    Classifies each timestamp as relative, already-absolute, or implausible
    before deciding whether to apply the offset. Handles both [HH:MM:SS] and
    [MM:SS] formats; always outputs [HH:MM:SS].
    """
    tolerance = 300  # 5 minutes

    # First, fix legacy malformed timestamps (e.g. [120:05:30] → [02:05:30])
    line = normalize_timestamp(line)

    # Try [HH:MM:SS] first (more specific), then [MM:SS]
    m = re.match(r"\[(\d+):(\d{2}):(\d{2})\]", line)
    if m:
        total = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    else:
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


def build_chunk_list(duration_seconds: int, chunk_minutes: int = 20) -> list[tuple[int, int]]:
    """Build the list of (start_sec, end_sec) chunks for a video.

    Policy from experiments:
    - Videos <= 60 min: single request, no clipping → [(0, 0)]
    - Videos > 60 min: first hour as one chunk, then chunk_minutes for remainder
    """
    first_hour = 3600
    if duration_seconds <= first_hour:
        return [(0, 0)]  # (0, 0) = no clipping, full video

    chunk_seconds = chunk_minutes * 60
    chunks = [(0, first_hour)]
    pos = first_hour
    while pos < duration_seconds:
        end = min(pos + chunk_seconds, duration_seconds)
        chunks.append((pos, end))
        pos = end
    return chunks


def _format_hhmm(seconds: int) -> str:
    """Format seconds as HH:MM for coverage display."""
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def build_header(
    title: str,
    url: str,
    published: str,
    model: str,
    *,
    original_title: str | None = None,
    coverage: tuple[int, int] | None = None,
    duration_seconds: int | None = None,
) -> str:
    """Build the metadata header for the translation file.

    coverage: (start_min, end_min) of translated range.
    duration_seconds: total video duration from YouTube API.
    When both are provided, a Coverage line is added to the header.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Translation (BCS): {title}", ""]
    if original_title and original_title != title:
        lines.append(f"**Original:** {original_title}")
    lines.append(f"**Source:** {url}")
    lines.append(f"**Published:** {published}")
    lines.append(f"**Translated:** {now}")
    lines.append(f"**Model:** {model}")
    if coverage is not None and duration_seconds is not None:
        cov_start, cov_end = coverage
        total_hhmm = _format_hhmm(duration_seconds)
        cov_label = f"{_format_hhmm(cov_start * 60)} \u2013 {_format_hhmm(cov_end * 60)} / {total_hhmm} total"
        lines.append(f"**Coverage:** {cov_label}")
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

    for part_file in part_files:
        # Extract offset and duration from filename: part-START-END
        offset_match = re.search(r"\.part-(\d+)-(\d+)\.", part_file.name)
        if offset_match:
            offset_seconds = int(offset_match.group(1)) * 60
            chunk_duration_seconds = (int(offset_match.group(2)) - int(offset_match.group(1))) * 60
        else:
            offset_seconds = 0
            chunk_duration_seconds = 3600  # fallback: assume 1h

        content = part_file.read_text(encoding="utf-8")
        lines = content.splitlines()

        for line in lines:
            adjusted = apply_timestamp_offset(line, offset_seconds, chunk_duration_seconds)

            # Check monotonic timestamps
            ts_match = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]", adjusted)
            if ts_match:
                current_ts = (int(ts_match.group(1)), int(ts_match.group(2)), int(ts_match.group(3)))
                if last_ts is not None and current_ts < last_ts:
                    log.warning(
                        "Non-monotonic timestamp: [%02d:%02d:%02d] after [%02d:%02d:%02d]",
                        *current_ts,
                        *last_ts,
                    )
                last_ts = current_ts

            all_lines.append(adjusted)

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
    header = build_header(
        title,
        url,
        date,
        model,
        original_title=original_title,
        coverage=coverage,
        duration_seconds=duration_seconds,
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
) -> tuple[str, dict | None]:
    """Stream video translation from Gemini with heartbeat and progressive writes."""
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
        instruction = "Translate only the provided clip. Start timestamps at [00:00:00] relative to this clip, in [HH:MM:SS] format."
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

            # Capture usage metadata from final chunk
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                meta = chunk.usage_metadata
                usage = {
                    "prompt_token_count": getattr(meta, "prompt_token_count", None),
                    "candidates_token_count": getattr(meta, "candidates_token_count", None),
                    "total_token_count": getattr(meta, "total_token_count", None),
                }

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

    return "".join(accumulated), usage


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------


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
) -> None:
    """Translate a YouTube video's audio to BCS subtitles."""
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
        needs_chunking = duration_seconds > 3600

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
        chunks = build_chunk_list(duration_seconds, chunk_minutes)
        log.info(
            "Duration:  %s (1h first chunk + %d x %dm)",
            format_elapsed(duration_seconds),
            len(chunks) - 1,
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
                text, usage = call_gemini_translate(
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

                text, usage = call_gemini_translate(
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

        # Atomic promotion: write header + full text, then rename
        header = build_header(title, canonical_url, date, model_name)
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
  # Any talking-head video up to ~2.5 hours — single pass, low-res default
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID"

  # Partial translation — e.g. first hour only, skip the interview segment
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --end 63

  # Stitch auto-chunked parts for videos over ~2.5 hours
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --stitch

  # Backfill a failed or missing chunk
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --start 60 --end 80

  # Slide-driven talk where on-screen terminology matters (rare)
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --high-res

  # Custom output directory
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --output-dir ./translations
        """,
    )
    parser.add_argument("url", help="YouTube video URL")
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
            api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
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
    )


if __name__ == "__main__":
    main()
