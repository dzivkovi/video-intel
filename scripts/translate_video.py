#!/usr/bin/env python3
"""
Translate YouTube video audio to BCS (Bosnian/Croatian/Serbian) subtitles.

Uses Gemini's multimodal video understanding with maxOutputTokens=65536
to translate entire videos in a single pass (up to ~2.5 hours of typical
dialogue density). Streams output progressively for live monitoring.

Usage:
    export GEMINI_API_KEY=your_key
    python translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID"
    python translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --model gemini-2.5-pro
    python translate_video.py "https://www.youtube.com/watch?v=VIDEO_ID" --stdout
"""

import argparse
import logging
import os
import random
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("translate_video")

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = "~/Dropbox/My Life/Transcripts"
MAX_OUTPUT_TOKENS = 65536


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------


def require_gemini():
    try:
        from google import genai
        from google.genai import types

        return genai, types
    except ImportError:
        log.error("google-genai not installed. Run: pip install google-genai")
        sys.exit(1)


def require_youtube():
    try:
        from googleapiclient.discovery import build

        return build
    except ImportError:
        log.error("google-api-python-client not installed. Run: pip install google-api-python-client")
        sys.exit(1)


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


def build_header(title: str, url: str, published: str, model: str) -> str:
    """Build the metadata header for the translation file."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"# Translation (BCS): {title}\n\n"
        f"**Source:** {url}\n"
        f"**Published:** {published}\n"
        f"**Translated:** {now}\n"
        f"**Model:** {model}\n\n"
        f"---\n\n"
    )


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
    low_res: bool = False,
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

    # Tell Gemini the absolute start time so timestamps are correct in chunked output
    if start_offset and start_offset > 0:
        instruction = f"Translate the video audio to BCS. Timestamps must start at {format_elapsed(start_offset)} (absolute video time, not relative to clip start)."
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
    if low_res:
        # Must use the full enum name, not "low" — the API rejects shorthand with 400 INVALID_ARGUMENT.
        # See: https://ai.google.dev/gemini-api/docs/video-understanding#technical-details-about-videos
        # Default ~300 tokens/sec, low ~100 tokens/sec. 1M context ÷ 100 = ~2.7 hours max.
        config_kwargs["media_resolution"] = "MEDIA_RESOLUTION_LOW"
    config = types.GenerateContentConfig(**config_kwargs)

    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            stream = client.models.generate_content_stream(
                model=model,
                contents=contents,
                config=config,
            )
            break
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = "429" in error_str or "resource exhausted" in error_str
            is_server_error = "503" in error_str or "overloaded" in error_str
            if (is_rate_limit or is_server_error) and attempt < max_retries:
                wait = (15 * (2**attempt)) + random.uniform(0, 5)
                log.warning("Rate limited, retrying in %ds...", wait)
                time.sleep(wait)
            else:
                raise

    # Start heartbeat thread
    start_time = time.time()
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(stop_heartbeat, start_time), daemon=True)
    heartbeat.start()

    accumulated = []
    usage = None
    first_chunk = True
    first_chunk_timeout = 600  # 10 minutes max wait for first chunk

    try:
        for chunk in stream:
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

            # Check for first-chunk timeout (stream may yield empty chunks while waiting)
            if first_chunk and (time.time() - start_time) > first_chunk_timeout:
                raise TimeoutError(f"No data received after {format_elapsed(first_chunk_timeout)}")

    except TimeoutError as e:
        stop_heartbeat.set()
        log.error("Chunk timed out: %s", e)
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
    low_res: bool = False,
    chunk_minutes: int = 30,
    start_minutes: int | None = None,
    end_minutes: int | None = None,
) -> None:
    """Translate a YouTube video's audio to BCS subtitles."""
    genai, types = require_gemini()

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
    # Auto-chunk when: video is long AND user didn't specify a manual range
    chunk_seconds = chunk_minutes * 60
    manual_range = start_minutes is not None or end_minutes is not None

    if manual_range:
        # User specified explicit range — single request with clipping
        needs_chunking = False
    else:
        # Auto-chunk if video exceeds chunk size
        needs_chunking = duration_seconds > chunk_seconds

    # Resolve output path and tmp file
    output_path = None
    tmp_path = None
    if not use_stdout:
        output_dir.mkdir(parents=True, exist_ok=True)
        if manual_range:
            # Include time range in filename for partial translations
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
        chunks = []
        pos = 0
        while pos < duration_seconds:
            end = min(pos + chunk_seconds, duration_seconds)
            chunks.append((pos, end))
            pos = end
        log.info("Duration:  %s (%d chunks of %dm)", format_elapsed(duration_seconds), len(chunks), chunk_minutes)
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
    if low_res:
        log.info("Resolution: low (~100 tokens/sec, fits videos up to ~2.7 hours)")
    if output_path:
        log.info("Output:    %s", output_path)
        log.info('Progress:  tail -f "%s"', tmp_path)

    # Load prompt and call Gemini with streaming
    system_prompt = load_prompt("translate-bcs")
    client = genai.Client(api_key=api_key)

    start_time = time.time()
    all_text_parts = []
    last_usage = None
    tmp_file = None

    try:
        # Open tmp file for progressive writes
        if tmp_path:
            tmp_file = open(tmp_path, "w", encoding="utf-8")  # noqa: SIM115

        for i, (s_off, e_off) in enumerate(chunks):
            use_clipping = s_off > 0 or e_off > 0
            if len(chunks) > 1:
                log.info("Chunk %d/%d: %s → %s", i + 1, len(chunks), format_elapsed(s_off), format_elapsed(e_off))
            elif use_clipping:
                log.info("Sending to Gemini (%s → %s)...", format_elapsed(s_off), format_elapsed(e_off))
            else:
                log.info("Sending to Gemini...")

            try:
                text, usage = call_gemini_translate(
                    client,
                    types,
                    canonical_url,
                    system_prompt,
                    model_name,
                    tmp_file,
                    low_res=low_res,
                    start_offset=s_off if use_clipping else None,
                    end_offset=e_off if use_clipping else None,
                )
                if text and text.strip():
                    all_text_parts.append(text)
                if usage:
                    last_usage = usage
            except TimeoutError:
                log.warning(
                    "Chunk %d/%d timed out (%s → %s). Skipping — retry with: --start %d --end %d",
                    i + 1,
                    len(chunks),
                    format_elapsed(s_off),
                    format_elapsed(e_off),
                    s_off // 60,
                    e_off // 60,
                )
                continue

    except Exception:
        if tmp_file:
            tmp_file.close()
        # On failure, tmp file preserves partial progress
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
Examples:
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID"
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --model gemini-2.5-pro
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --stdout
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --output-dir ./translations

  # Long video — auto-chunks into 30-min segments
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --low-res

  # Translate only the second hour (backfill after partial success)
  %(prog)s "https://www.youtube.com/watch?v=VIDEO_ID" --start 60 --end 120 --low-res
        """,
    )
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument(
        "--model",
        "-m",
        default="gemini-3.1-pro-preview",
        help="Gemini model (default: gemini-3.1-pro-preview)",
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
        default=30,
        help="Chunk size in minutes for auto-splitting long videos (default: 30)",
    )
    parser.add_argument("--stdout", action="store_true", help="Print translation to stdout instead of file")
    parser.add_argument("--force", action="store_true", help="Overwrite existing translation")
    parser.add_argument(
        "--low-res",
        action="store_true",
        help="Use low media resolution (~100 tokens/sec vs ~300). Required for videos over ~55 min.",
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

    translate_video(
        args.url,
        args.model,
        output_dir,
        title_override=args.title,
        date_override=args.date,
        use_stdout=args.stdout,
        force=args.force,
        low_res=args.low_res,
        chunk_minutes=args.chunk_minutes,
        start_minutes=args.start,
        end_minutes=args.end,
    )


if __name__ == "__main__":
    main()
