#!/usr/bin/env python3
"""
video_intel.py - Multimodal video intelligence via Gemini API.

Scans YouTube channels for new videos, generates thematic mind maps,
and produces fused diarized transcripts with on-screen content.

Prerequisites:
  pip install google-genai google-api-python-client pyyaml
  export GEMINI_API_KEY=your_key
  export YOUTUBE_API_KEY=your_key
"""

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Lazy imports with clear error messages
# ---------------------------------------------------------------------------

def require_gemini():
    try:
        from google import genai
        from google.genai import types
        return genai, types
    except ImportError:
        print("ERROR: google-genai not installed. Run: pip install google-genai")
        sys.exit(1)

def require_youtube():
    try:
        from googleapiclient.discovery import build
        return build
    except ImportError:
        print("ERROR: google-api-python-client not installed.")
        print("Run: pip install google-api-python-client")
        sys.exit(1)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent

def load_config():
    config_path = SKILL_DIR / "config.yaml"
    if not config_path.exists():
        print(f"ERROR: Config not found at {config_path}")
        print("Copy config.yaml.example to config.yaml and edit it.")
        sys.exit(1)
    with open(config_path) as f:
        return yaml.safe_load(f)

def resolve_output_dir(config):
    output_dir = Path(config.get("output_dir", "~/video-intel")).expanduser()
    if not output_dir.is_absolute():
        output_dir = SKILL_DIR / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir

def load_prompt(prompt_name):
    prompt_path = SKILL_DIR / "prompts" / f"{prompt_name}.md"
    if not prompt_path.exists():
        print(f"ERROR: Prompt file not found: {prompt_path}")
        sys.exit(1)
    return prompt_path.read_text(encoding="utf-8")

def parse_since(since_str):
    """Parse '10d', '120d', '2026-03-01' into a datetime."""
    match = re.match(r"^(\d+)d$", since_str)
    if match:
        days = int(match.group(1))
        return datetime.now(timezone.utc) - timedelta(days=days)
    try:
        return datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
    except ValueError:
        print(f"ERROR: Invalid since format: {since_str}")
        print("Use '10d' for relative days or 'YYYY-MM-DD' for absolute date.")
        sys.exit(1)

# ---------------------------------------------------------------------------
# YouTube Data API
# ---------------------------------------------------------------------------

def get_channel_id(youtube, channel_url):
    """Resolve @handle or channel URL to channel ID."""
    handle = channel_url.rstrip("/").split("/")[-1]
    if handle.startswith("@"):
        handle = handle[1:]

    # Try handle-based lookup
    resp = youtube.channels().list(
        part="id,snippet",
        forHandle=handle
    ).execute()

    if resp.get("items"):
        ch = resp["items"][0]
        return ch["id"], ch["snippet"]["title"]

    # Fallback: try as channel ID directly
    resp = youtube.channels().list(part="id,snippet", id=handle).execute()
    if resp.get("items"):
        ch = resp["items"][0]
        return ch["id"], ch["snippet"]["title"]

    return None, None

def fetch_channel_videos(youtube, channel_id, since_dt):
    """Fetch all videos published after since_dt from a channel."""
    videos = []
    seen_ids = set()
    next_page = None

    while True:
        resp = youtube.search().list(
            part="id,snippet",
            channelId=channel_id,
            type="video",
            order="date",
            publishedAfter=since_dt.isoformat(),
            maxResults=50,
            pageToken=next_page,
        ).execute()

        for item in resp.get("items", []):
            video_id = item["id"]["videoId"]
            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            vid = {
                "video_id": video_id,
                "title": unescape(item["snippet"]["title"]),
                "published": item["snippet"]["publishedAt"][:10],
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
            videos.append(vid)

        next_page = resp.get("nextPageToken")
        if not next_page:
            break

    return videos

# ---------------------------------------------------------------------------
# File naming and idempotency
# ---------------------------------------------------------------------------

def slugify(text, max_len=80):
    """Create a filesystem-safe slug from a title."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len].rstrip("-")

def video_file_prefix(video):
    """Generate the date-slug prefix for a video's output files."""
    return f"{video['published']}-{slugify(video['title'])}"

def is_processed(output_dir, channel_name, video, mode):
    """Check if a video has already been processed for a given mode."""
    prefix = video_file_prefix(video)
    ext = "mindmap.md" if mode == "scan" else "transcript.md"
    target = output_dir / channel_name / f"{prefix}.{ext}"
    return target.exists() and target.stat().st_size > 0

def is_skipped(output_dir, channel_name, video):
    """Check if a video is marked to skip permanently."""
    prefix = video_file_prefix(video)
    meta_path = output_dir / channel_name / f"{prefix}.meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return meta.get("skip", False)
    return False

# ---------------------------------------------------------------------------
# Gemini API calls
# ---------------------------------------------------------------------------

def call_gemini(client, types, video_url, prompt_text, model, response_json=False):
    """Send a video to Gemini for multimodal analysis with retry on rate limits."""
    config_kwargs = {"temperature": 0.3}
    if response_json:
        config_kwargs["response_mime_type"] = "application/json"

    contents = types.Content(
        parts=[
            types.Part(file_data=types.FileData(file_uri=video_url)),
            types.Part(text=prompt_text),
        ]
    )

    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            return response.text
        except Exception as e:
            error_str = str(e).lower()
            is_rate_limit = "429" in error_str or "resource exhausted" in error_str
            is_server_error = "503" in error_str or "overloaded" in error_str
            if (is_rate_limit or is_server_error) and attempt < max_retries:
                wait = (15 * (2 ** attempt)) + random.uniform(0, 5)
                print(f"      Rate limited, retrying in {wait:.0f}s...")
                time.sleep(wait)
            else:
                raise

# ---------------------------------------------------------------------------
# Mind map processing
# ---------------------------------------------------------------------------

def process_mindmap(client, types, video, prompt_text, model, output_dir, channel_name):
    """Generate a mind map for a single video."""
    prefix = video_file_prefix(video)
    channel_dir = output_dir / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    mindmap_path = channel_dir / f"{prefix}.mindmap.md"
    meta_path = channel_dir / f"{prefix}.meta.json"

    if mindmap_path.exists():
        return prefix, "skipped (exists)"

    try:
        result = call_gemini(client, types, video["url"], prompt_text, model)

        # Save mind map
        header = (
            f"<!-- video: {video['url']} -->\n"
            f"<!-- title: {video['title']} -->\n"
            f"<!-- published: {video['published']} -->\n\n"
        )
        mindmap_path.write_text(header + result, encoding="utf-8")

        # Save or update metadata
        meta = {
            "video_url": video["url"],
            "video_id": video["video_id"],
            "channel": channel_name,
            "title": video["title"],
            "published": video["published"],
            "processed": datetime.now(timezone.utc).isoformat(),
            "model": model,
            "modes_completed": ["scan"],
            "last_error": None,
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return prefix, "done"

    except Exception as e:
        # Record failure in meta.json
        channel_dir.mkdir(parents=True, exist_ok=True)
        meta = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update({
            "video_url": video["url"],
            "video_id": video["video_id"],
            "channel": channel_name,
            "title": video["title"],
            "published": video["published"],
            "model": model,
            "modes_completed": meta.get("modes_completed", []),
            "last_error": str(e),
        })
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return prefix, f"error: {e}"

# ---------------------------------------------------------------------------
# Transcript processing
# ---------------------------------------------------------------------------

def merge_transcript_json(raw_json, speakers_map):
    """Merge three-task JSON into a fused markdown transcript."""
    # Gemini sometimes wraps the response in an array
    if isinstance(raw_json, list):
        raw_json = raw_json[0] if raw_json else {}

    lines = []

    # Build voice-to-name mapping
    voice_names = {}
    evidence_notes = []
    for s in raw_json.get("speakers", []):
        voice_names[s["voice"]] = s.get("name", f"Speaker {s['voice']}")
        if s.get("evidence"):
            evidence_notes.append(
                f"- **{voice_names[s['voice']]}**: {s['evidence']}"
            )
        if s.get("role"):
            voice_names[s["voice"]] += f" ({s['role']})"

    # Merge transcripts and screen_content by timestamp
    entries = []

    for t in raw_json.get("transcripts", []):
        entries.append({
            "type": "speech",
            "start": t["start"],
            "sort_key": timestamp_to_seconds(t["start"]),
            "voice": t.get("voice"),
            "text": t.get("text", ""),
        })

    for sc in raw_json.get("screen_content", []):
        entries.append({
            "type": "screen",
            "start": sc["start"],
            "end": sc.get("end", sc["start"]),
            "sort_key": timestamp_to_seconds(sc["start"]),
            "screen_type": sc.get("type", "other"),
            "description": sc.get("description", ""),
            "code": sc.get("code"),
            "transcribed_text": sc.get("transcribed_text"),
        })

    entries.sort(key=lambda e: e["sort_key"])

    for entry in entries:
        if entry["type"] == "speech":
            name = voice_names.get(entry["voice"], f"Speaker {entry['voice']}")
            lines.append(f"[{entry['start']}] {name}: \"{entry['text']}\"\n")
        else:
            desc = entry["description"]
            st = entry["screen_type"]
            time_range = entry["start"]
            if entry.get("end") and entry["end"] != entry["start"]:
                time_range = f"{entry['start']}-{entry['end']}"

            lines.append(f"\n  SCREEN [{time_range}] [{st}]: {desc}")

            if entry.get("code"):
                lines.append(f"  ```\n  {entry['code']}\n  ```")
            if entry.get("transcribed_text"):
                lines.append(f"  On-screen text: \"{entry['transcribed_text']}\"")
            lines.append("")

    # Add evidence footer
    if evidence_notes:
        lines.append("\n---\n## Speaker Identification Evidence\n")
        lines.extend(evidence_notes)

    return "\n".join(lines)

def timestamp_to_seconds(ts):
    """Convert MM:SS or H:MM:SS to seconds for sorting."""
    parts = ts.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0

def process_transcript(client, types, video, prompt_text, model, output_dir, channel_name):
    """Generate a fused transcript for a single video."""
    prefix = video_file_prefix(video)
    channel_dir = output_dir / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = channel_dir / f"{prefix}.transcript.md"
    meta_path = channel_dir / f"{prefix}.meta.json"

    if transcript_path.exists():
        return prefix, "skipped (exists)"

    try:
        raw = call_gemini(
            client, types, video["url"], prompt_text, model, response_json=True
        )

        # Parse JSON response
        raw_json = json.loads(raw)

        # Merge into fused markdown
        fused = merge_transcript_json(raw_json, {})

        # Save
        header = (
            f"# Transcript: {video['title']}\n\n"
            f"**Source:** {video['url']}\n"
            f"**Published:** {video['published']}\n"
            f"**Processed:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"---\n\n"
        )
        transcript_path.write_text(header + fused, encoding="utf-8")

        # Update metadata
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            if "transcript" not in meta.get("modes_completed", []):
                meta["modes_completed"].append("transcript")
            meta["last_error"] = None
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return prefix, "done"

    except json.JSONDecodeError as e:
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta["last_error"] = f"JSON parse error: {e}"
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return prefix, f"error parsing JSON: {e}"
    except Exception as e:
        if meta_path.exists():
            meta = json.loads(meta_path.read_text())
            meta["last_error"] = str(e)
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return prefix, f"error: {e}"

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_scan(args, config):
    """Scan channels for new videos and generate mind maps."""
    errors = []
    genai, types = require_gemini()
    yt_build = require_youtube()

    # Check API keys
    gemini_key = os.environ.get("GEMINI_API_KEY")
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if not gemini_key:
        print("ERROR: GEMINI_API_KEY not set.")
        print("Get a free key at https://aistudio.google.com/apikey")
        sys.exit(1)
    if not yt_key:
        print("ERROR: YOUTUBE_API_KEY not set.")
        print("Get a free key at https://console.cloud.google.com/apis/credentials")
        print("Enable 'YouTube Data API v3'.")
        sys.exit(1)

    client = genai.Client(api_key=gemini_key)
    youtube = yt_build("youtube", "v3", developerKey=yt_key)
    output_dir = resolve_output_dir(config)
    model = config.get("model", "gemini-3-flash-preview")
    max_parallel = config.get("max_parallel", 10)

    # Filter channels if --channel specified
    channels = config.get("channels", [])
    if args.channel:
        channels = [c for c in channels if c["name"] == args.channel]
        if not channels:
            print(f"ERROR: Channel '{args.channel}' not found in config.yaml")
            sys.exit(1)

    for ch in channels:
        ch_name = ch["name"]
        ch_url = ch["url"]

        # Resolve channel
        channel_id, channel_title = get_channel_id(youtube, ch_url)
        if not channel_id:
            print(f"[{ch_name}] ERROR: Channel not found: {ch_url}")
            continue

        print(f"\n[{ch_name}] {channel_title}")

        # Determine time window
        since_str = args.since or ch.get("since") or config.get("default_since", "10d")
        since_dt = parse_since(since_str)
        print(f"  Looking back to {since_dt.strftime('%Y-%m-%d')}")

        # Fetch videos
        videos = fetch_channel_videos(youtube, channel_id, since_dt)
        if not videos:
            print("  No new videos found.")
            continue

        # Filter already processed or skipped
        new_videos = [
            v for v in videos
            if not is_processed(output_dir, ch_name, v, "scan")
            and not is_skipped(output_dir, ch_name, v)
        ]
        print(f"  Found {len(videos)} videos, {len(new_videos)} new.")

        if args.dry_run:
            for v in new_videos:
                print(f"    {v['published']} - {v['title']}")
            continue

        # Load prompt
        prompt_name = ch.get("prompt") or config.get("default_prompt", "mindmap-light")
        prompt_text = load_prompt(prompt_name)

        if not new_videos:
            print("  All mind maps up to date.")
        else:
            # Process mind maps in parallel
            print(f"  Generating mind maps ({prompt_name})...")
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                futures = {
                    executor.submit(
                        process_mindmap,
                        client, types, v, prompt_text, model, output_dir, ch_name,
                    ): v
                    for v in new_videos
                }
                for future in as_completed(futures):
                    v = futures[future]
                    prefix, status = future.result()
                    print(f"    {prefix}: {status}")
                    if status.startswith("error"):
                        errors.append((ch_name, prefix, status))

        # Auto-transcript if configured
        auto = ch.get("auto_transcript", "none")
        if auto == "all":
            transcript_prompt = load_prompt("transcript")
            transcript_videos = [
                v for v in videos
                if not is_processed(output_dir, ch_name, v, "transcript")
                and not is_skipped(output_dir, ch_name, v)
            ]
            if transcript_videos:
                print(f"  Generating transcripts ({len(transcript_videos)} videos)...")
                with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                    futures = {
                        executor.submit(
                            process_transcript,
                            client, types, v, transcript_prompt,
                            model, output_dir, ch_name,
                        ): v
                        for v in transcript_videos
                    }
                    for future in as_completed(futures):
                        v = futures[future]
                        prefix, status = future.result()
                        print(f"    {prefix}: {status}")
                        if status.startswith("error"):
                            errors.append((ch_name, prefix, status))

    if errors:
        print(f"\n--- {len(errors)} FAILED ---")
        for ch, prefix, status in errors:
            print(f"  [{ch}] {prefix}: {status}")
        print("Failed items will retry on next run.")
        print("To skip permanently: set \"skip\": true in the video's .meta.json")

    print("\nDone.")

def cmd_transcript(args, config):
    """Generate a transcript for a single video."""
    genai, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        print("ERROR: GEMINI_API_KEY not set.")
        sys.exit(1)

    client = genai.Client(api_key=gemini_key)
    output_dir = resolve_output_dir(config)
    model = config.get("model", "gemini-3-flash-preview")
    prompt_text = load_prompt("transcript")

    # Build video object from URL
    video_id_match = re.search(r"(?:v=|/)([a-zA-Z0-9_-]{11})", args.url)
    if not video_id_match:
        print(f"ERROR: Could not extract video ID from: {args.url}")
        sys.exit(1)

    video_id = video_id_match.group(1)
    channel_name = args.channel
    title = args.title
    date = args.date

    # Fetch video metadata from YouTube API
    if not channel_name or not title or not date:
        yt_key = os.environ.get("YOUTUBE_API_KEY")
        if yt_key:
            yt_build = require_youtube()
            youtube = yt_build("youtube", "v3", developerKey=yt_key)
            resp = youtube.videos().list(
                part="snippet", id=video_id
            ).execute()
            if resp.get("items"):
                snippet = resp["items"][0]["snippet"]
                title = title or unescape(snippet["title"])
                date = date or snippet["publishedAt"][:10]
                if not channel_name:
                    # Match against configured channels by channel ID
                    yt_channel_id = snippet["channelId"]
                    for ch in config.get("channels", []):
                        ch_id, _ = get_channel_id(youtube, ch["url"])
                        if ch_id == yt_channel_id:
                            channel_name = ch["name"]
                            break
                    if not channel_name:
                        channel_name = slugify(snippet["channelTitle"])

    channel_name = channel_name or "_standalone"

    video = {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title or video_id,
        "published": date or datetime.now().strftime("%Y-%m-%d"),
    }

    print(f"Transcribing: {video['url']}")
    prefix, status = process_transcript(
        client, types, video, prompt_text, model, output_dir, channel_name
    )
    print(f"  {prefix}: {status}")

    if status == "done":
        out_path = output_dir / channel_name / f"{prefix}.transcript.md"
        print(f"  Saved: {out_path}")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Video Intel - Multimodal video scanning and transcription",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s scan                           # Scan all configured channels
  %(prog)s scan --channel natebjones      # Scan one channel
  %(prog)s scan --since 30d               # Override lookback window
  %(prog)s scan --dry-run                 # Preview without processing
  %(prog)s transcript --url URL           # Transcribe a specific video
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Scan channels for new videos")
    scan_parser.add_argument("--channel", help="Scan only this channel name")
    scan_parser.add_argument("--since", help="Override lookback window (e.g. 14d, 2026-01-01)")
    scan_parser.add_argument("--dry-run", action="store_true", help="Preview without processing")

    # transcript command
    tx_parser = subparsers.add_parser("transcript", help="Transcribe a specific video")
    tx_parser.add_argument("--url", required=True, help="YouTube video URL")
    tx_parser.add_argument("--channel", help="Channel name for output folder")
    tx_parser.add_argument("--title", help="Video title (auto-detected if omitted)")
    tx_parser.add_argument("--date", help="Publish date YYYY-MM-DD (defaults to today)")

    args = parser.parse_args()
    config = load_config()

    if args.command == "scan":
        cmd_scan(args, config)
    elif args.command == "transcript":
        cmd_transcript(args, config)

if __name__ == "__main__":
    main()
