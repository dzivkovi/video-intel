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
from datetime import UTC, datetime, timedelta
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


def normalize_prompt_name(name: str) -> str:
    """Strip path prefixes and .md extension from prompt name.

    Accepts 'mindmap-knowledge', 'prompts/mindmap-knowledge.md',
    or 'prompts\\mindmap-knowledge.md' and returns 'mindmap-knowledge'.
    """
    return Path(name).stem


def update_meta(meta_path: Path, fields: dict, mode: str) -> None:
    """Read existing meta.json, merge fields, ensure mode in modes_completed, write back."""
    meta: dict = {}
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(fields)
    modes = meta.get("modes_completed", [])
    if mode not in modes:
        modes.append(mode)
    meta["modes_completed"] = modes
    meta["last_error"] = None
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_taxonomy(output_dir: Path) -> dict:
    """Load existing taxonomy.json as normalization context, or return empty structure."""
    taxonomy_path = output_dir / "taxonomy.json"
    if taxonomy_path.exists():
        return json.loads(taxonomy_path.read_text(encoding="utf-8"))
    return {"version": 1, "built_from": 0, "concepts": {}}


def find_mindmap_source(channel_dir: Path, prefix: str) -> Path | None:
    """Find the best mindmap file for concept extraction.

    Prefers canonical .mindmap.md, falls back to .mindmap.knowledge.md,
    then any .mindmap*.md variant.
    """
    canonical = channel_dir / f"{prefix}.mindmap.md"
    if canonical.exists() and canonical.stat().st_size > 0:
        return canonical
    knowledge = channel_dir / f"{prefix}.mindmap.knowledge.md"
    if knowledge.exists() and knowledge.stat().st_size > 0:
        return knowledge
    variants = sorted(channel_dir.glob(f"{prefix}.mindmap*.md"))
    for v in variants:
        if v.stat().st_size > 0:
            return v
    return None


def load_prompt(prompt_name: str) -> str:
    prompt_name = normalize_prompt_name(prompt_name)
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
        return datetime.now(UTC) - timedelta(days=days)
    try:
        return datetime.fromisoformat(since_str).replace(tzinfo=UTC)
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
    resp = youtube.channels().list(part="id,snippet", forHandle=handle).execute()

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
    """Fetch all videos published after since_dt from a channel's uploads playlist."""
    uploads_id = "UU" + channel_id[2:]
    videos = []
    next_page = None

    while True:
        resp = (
            youtube.playlistItems()
            .list(
                part="snippet,contentDetails",
                playlistId=uploads_id,
                maxResults=50,
                pageToken=next_page,
            )
            .execute()
        )

        for item in resp.get("items", []):
            published_str = item["contentDetails"].get("videoPublishedAt", item["snippet"]["publishedAt"])
            published_dt = datetime.fromisoformat(published_str.replace("Z", "+00:00"))

            if published_dt < since_dt:
                return videos

            video_id = item["contentDetails"]["videoId"]
            videos.append(
                {
                    "video_id": video_id,
                    "title": unescape(item["snippet"]["title"]),
                    "published": published_str[:10],
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            )

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


def is_processed(
    output_dir: Path,
    channel_name: str,
    video: dict,
    mode: str,
    *,
    any_variant: bool = False,
) -> bool:
    """Check if a video has already been processed for a given mode.

    For scan mode with any_variant=True: checks for ANY .mindmap*.md file (prevents backfill).
    For transcript mode: checks for .transcript.md (unchanged).
    """
    prefix = video_file_prefix(video)
    channel_dir = output_dir / channel_name

    if mode == "transcript":
        target = channel_dir / f"{prefix}.transcript.md"
        return target.exists() and target.stat().st_size > 0

    # mode == "scan"
    if any_variant:
        # Glob for any mindmap file — legacy .mindmap.md or .mindmap.*.md
        return (
            any(f.stat().st_size > 0 for f in channel_dir.glob(f"{prefix}.mindmap*.md"))
            if channel_dir.exists()
            else False
        )

    # Default: check for standard .mindmap.md
    target = channel_dir / f"{prefix}.mindmap.md"
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
                wait = (15 * (2**attempt)) + random.uniform(0, 5)
                print(f"      Rate limited, retrying in {wait:.0f}s...")
                time.sleep(wait)
            else:
                raise


# ---------------------------------------------------------------------------
# Mind map processing
# ---------------------------------------------------------------------------


def process_mindmap(
    client, types, video, prompt_text, model, output_dir, channel_name, *, prompt_name=None, force=False
):
    """Generate a mind map for a single video."""
    prefix = video_file_prefix(video)
    channel_dir = output_dir / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    mindmap_path = channel_dir / f"{prefix}.mindmap.md"
    meta_path = channel_dir / f"{prefix}.meta.json"

    if mindmap_path.exists() and not force:
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

        # Save or update metadata (merge, don't overwrite)
        meta_fields = {
            "video_url": video["url"],
            "video_id": video["video_id"],
            "channel": channel_name,
            "title": video["title"],
            "published": video["published"],
            "processed": datetime.now(UTC).isoformat(),
            "model": model,
        }
        if prompt_name:
            meta_fields["prompt"] = prompt_name
        update_meta(meta_path, meta_fields, "scan")

        return prefix, "done"

    except Exception as e:
        # Record failure in meta.json (also merge-safe)
        channel_dir.mkdir(parents=True, exist_ok=True)
        meta: dict = {}
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta.update(
            {
                "video_url": video["url"],
                "video_id": video["video_id"],
                "channel": channel_name,
                "title": video["title"],
                "published": video["published"],
                "model": model,
                "modes_completed": meta.get("modes_completed", []),
                "last_error": str(e),
            }
        )
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
            evidence_notes.append(f"- **{voice_names[s['voice']]}**: {s['evidence']}")
        if s.get("role"):
            voice_names[s["voice"]] += f" ({s['role']})"

    # Merge transcripts and screen_content by timestamp
    entries = []

    for t in raw_json.get("transcripts", []):
        entries.append(
            {
                "type": "speech",
                "start": t["start"],
                "sort_key": timestamp_to_seconds(t["start"]),
                "voice": t.get("voice"),
                "text": t.get("text", ""),
            }
        )

    for sc in raw_json.get("screen_content", []):
        entries.append(
            {
                "type": "screen",
                "start": sc["start"],
                "end": sc.get("end", sc["start"]),
                "sort_key": timestamp_to_seconds(sc["start"]),
                "screen_type": sc.get("type", "other"),
                "description": sc.get("description", ""),
                "code": sc.get("code"),
                "transcribed_text": sc.get("transcribed_text"),
            }
        )

    entries.sort(key=lambda e: e["sort_key"])

    for entry in entries:
        if entry["type"] == "speech":
            name = voice_names.get(entry["voice"], f"Speaker {entry['voice']}")
            lines.append(f'[{entry["start"]}] {name}: "{entry["text"]}"\n')
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
                lines.append(f'  On-screen text: "{entry["transcribed_text"]}"')
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


def process_transcript(client, types, video, prompt_text, model, output_dir, channel_name, *, force=False):
    """Generate a fused transcript for a single video."""
    prefix = video_file_prefix(video)
    channel_dir = output_dir / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = channel_dir / f"{prefix}.transcript.md"
    meta_path = channel_dir / f"{prefix}.meta.json"

    if transcript_path.exists() and not force:
        return prefix, "skipped (exists)"

    try:
        raw = call_gemini(client, types, video["url"], prompt_text, model, response_json=True)

        # Parse JSON response
        raw_json = json.loads(raw)

        # Merge into fused markdown
        fused = merge_transcript_json(raw_json, {})

        # Save
        header = (
            f"# Transcript: {video['title']}\n\n"
            f"**Source:** {video['url']}\n"
            f"**Published:** {video['published']}\n"
            f"**Processed:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            f"---\n\n"
        )
        transcript_path.write_text(header + fused, encoding="utf-8")

        # Update metadata (merge, don't overwrite)
        update_meta(meta_path, {"processed": datetime.now(UTC).isoformat()}, "transcript")

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
# Concept extraction
# ---------------------------------------------------------------------------


def call_gemini_text(client, types, text_content, model):
    """Send text-only content to Gemini and get a JSON response."""
    config_kwargs = {"temperature": 0.3, "response_mime_type": "application/json"}
    contents = types.Content(parts=[types.Part(text=text_content)])

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
                wait = (15 * (2**attempt)) + random.uniform(0, 5)
                print(f"      Rate limited, retrying in {wait:.0f}s...")
                time.sleep(wait)
            else:
                raise


def process_concepts(
    client,
    types,
    video,
    mindmap_text,
    taxonomy,
    model,
    output_dir,
    channel_name,
    *,
    source_file=None,
    source_prompt=None,
    force=False,
):
    """Extract and normalize concepts from a mindmap against the taxonomy."""
    prefix = video_file_prefix(video)
    channel_dir = output_dir / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    concepts_path = channel_dir / f"{prefix}.concepts.json"
    meta_path = channel_dir / f"{prefix}.meta.json"

    if concepts_path.exists() and not force:
        return prefix, "skipped (exists)"

    try:
        # Build the prompt with taxonomy context
        prompt_text = load_prompt("concepts")
        taxonomy_context = json.dumps(taxonomy.get("concepts", {}), indent=2)
        prompt_with_taxonomy = prompt_text.replace("{{taxonomy}}", taxonomy_context)

        full_text = f"{prompt_with_taxonomy}\n\n---\n\n## Mind Map to Analyze\n\n{mindmap_text}"
        raw = call_gemini_text(client, types, full_text, model)
        result = json.loads(raw)

        # Normalize: ensure it has the expected structure
        if isinstance(result, list):
            result = result[0] if result else {"concepts": []}
        if "concepts" not in result:
            result = {"concepts": result} if isinstance(result, list) else {"concepts": []}

        # Build the output
        output = {
            "video_id": video["video_id"],
            "extracted_from": source_file or "mindmap.md",
            "source_prompt": source_prompt or "unknown",
            "concepts": result["concepts"],
        }

        concepts_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        update_meta(meta_path, {"processed": datetime.now(UTC).isoformat()}, "concepts")

        n_new = sum(1 for c in result["concepts"] if c.get("status") == "new")
        n_uncertain = sum(1 for c in result["concepts"] if c.get("status") == "uncertain")
        summary = f"done ({len(result['concepts'])} concepts"
        if n_new:
            summary += f", {n_new} new"
        if n_uncertain:
            summary += f", {n_uncertain} uncertain"
        summary += ")"
        return prefix, summary

    except json.JSONDecodeError as e:
        return prefix, f"error parsing JSON: {e}"
    except Exception as e:
        return prefix, f"error: {e}"


def build_taxonomy(output_dir: Path) -> dict:
    """Rebuild taxonomy.json from all concepts.json files. Returns the taxonomy."""
    all_concepts: dict[str, dict] = {}
    file_count = 0

    for concepts_file in output_dir.rglob("*.concepts.json"):
        file_count += 1
        data = json.loads(concepts_file.read_text(encoding="utf-8"))
        video_id = data.get("video_id", "")

        # Try to find published date from sibling meta.json
        meta_file = concepts_file.with_suffix("").with_suffix(".meta.json")
        published = None
        if meta_file.exists():
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            published = meta.get("published")

        for concept in data.get("concepts", []):
            cid = concept.get("concept_id", "")
            if not cid:
                continue

            if cid not in all_concepts:
                all_concepts[cid] = {
                    "preferred_label": concept.get("preferred_label", cid),
                    "aliases": set(),
                    "domain": concept.get("domain", ""),
                    "first_seen": published,
                    "video_ids": set(),
                }

            entry = all_concepts[cid]
            # Collect alias from as_mentioned
            mentioned = concept.get("as_mentioned", "")
            if mentioned and mentioned != entry["preferred_label"]:
                entry["aliases"].add(mentioned)
            # Track video
            if video_id:
                entry["video_ids"].add(video_id)
            # Update first_seen
            if published and (entry["first_seen"] is None or published < entry["first_seen"]):
                entry["first_seen"] = published

    # Convert sets to sorted lists for JSON serialization
    taxonomy = {
        "version": 1,
        "built_from": file_count,
        "concepts": {},
    }
    for cid, entry in sorted(all_concepts.items()):
        taxonomy["concepts"][cid] = {
            "preferred_label": entry["preferred_label"],
            "aliases": sorted(entry["aliases"]),
            "domain": entry["domain"],
            "first_seen": entry["first_seen"],
            "video_count": len(entry["video_ids"]),
        }

    taxonomy_path = output_dir / "taxonomy.json"
    taxonomy_path.write_text(json.dumps(taxonomy, indent=2), encoding="utf-8")
    return taxonomy


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

        # Filter already processed or skipped (any_variant=True prevents backfill)
        if args.force:
            new_videos = [v for v in videos if not is_skipped(output_dir, ch_name, v)]
        else:
            new_videos = [
                v
                for v in videos
                if not is_processed(output_dir, ch_name, v, "scan", any_variant=True)
                and not is_skipped(output_dir, ch_name, v)
            ]
        label = "to regenerate" if args.force else "new"
        print(f"  Found {len(videos)} videos, {len(new_videos)} {label}.")

        if args.dry_run:
            for v in new_videos:
                print(f"    {v['published']} - {v['title']}")
            continue

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
                        client,
                        types,
                        v,
                        prompt_text,
                        model,
                        output_dir,
                        ch_name,
                        prompt_name=prompt_name,
                        force=args.force,
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
                v
                for v in videos
                if not is_processed(output_dir, ch_name, v, "transcript") and not is_skipped(output_dir, ch_name, v)
            ]
            if transcript_videos:
                print(f"  Generating transcripts ({len(transcript_videos)} videos)...")
                with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                    futures = {
                        executor.submit(
                            process_transcript,
                            client,
                            types,
                            v,
                            transcript_prompt,
                            model,
                            output_dir,
                            ch_name,
                        ): v
                        for v in transcript_videos
                    }
                    for future in as_completed(futures):
                        v = futures[future]
                        prefix, status = future.result()
                        print(f"    {prefix}: {status}")
                        if status.startswith("error"):
                            errors.append((ch_name, prefix, status))

        # Auto-concepts if configured
        auto_concepts = ch.get("auto_concepts", config.get("auto_concepts", False))
        if auto_concepts:
            taxonomy = load_taxonomy(output_dir)
            prompt_name = ch.get("prompt") or config.get("default_prompt", "mindmap-knowledge")
            concept_videos = []
            for v in videos:
                prefix = video_file_prefix(v)
                concepts_path = output_dir / ch_name / f"{prefix}.concepts.json"
                if concepts_path.exists():
                    continue
                mindmap_path = find_mindmap_source(output_dir / ch_name, prefix)
                if mindmap_path:
                    concept_videos.append((v, mindmap_path))

            if concept_videos:
                print(f"  Extracting concepts ({len(concept_videos)} videos)...")
                for v, mindmap_path in concept_videos:
                    mindmap_text = mindmap_path.read_text(encoding="utf-8")
                    prefix, status = process_concepts(
                        client,
                        types,
                        v,
                        mindmap_text,
                        taxonomy,
                        model,
                        output_dir,
                        ch_name,
                        source_file=mindmap_path.name,
                        source_prompt=prompt_name,
                    )
                    print(f"    {prefix}: {status}")
                    if status.startswith("error"):
                        errors.append((ch_name, prefix, status))

    if errors:
        print(f"\n--- {len(errors)} FAILED ---")
        for ch, prefix, status in errors:
            print(f"  [{ch}] {prefix}: {status}")
        print("Failed items will retry on next run.")
        print('To skip permanently: set "skip": true in the video\'s .meta.json')

    print("\nDone.")


def cmd_mindmap(args, config):
    """Generate a mind map for a single video with a specific prompt."""
    genai, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        print("ERROR: GEMINI_API_KEY not set.")
        sys.exit(1)

    client = genai.Client(api_key=gemini_key)
    output_dir = resolve_output_dir(config)
    model = config.get("model", "gemini-3-flash-preview")

    # Resolve prompt
    prompt_name = normalize_prompt_name(args.prompt or config.get("default_prompt", "mindmap-light"))
    prompt_text = load_prompt(prompt_name)

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
            resp = youtube.videos().list(part="snippet", id=video_id).execute()
            if resp.get("items"):
                snippet = resp["items"][0]["snippet"]
                title = title or unescape(snippet["title"])
                date = date or snippet["publishedAt"][:10]
                if not channel_name:
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

    print(f"Generating mind map ({prompt_name}): {video['url']}")
    prefix, status = process_mindmap(
        client, types, video, prompt_text, model, output_dir, channel_name, prompt_name=prompt_name, force=args.force
    )
    print(f"  {prefix}: {status}")

    if status == "done":
        out_path = output_dir / channel_name / f"{prefix}.mindmap.md"
        print(f"  Saved: {out_path}")


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
            resp = youtube.videos().list(part="snippet", id=video_id).execute()
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
        client, types, video, prompt_text, model, output_dir, channel_name, force=args.force
    )
    print(f"  {prefix}: {status}")

    if status == "done":
        out_path = output_dir / channel_name / f"{prefix}.transcript.md"
        print(f"  Saved: {out_path}")


def cmd_concepts(args, config):
    """Extract and normalize concepts from existing mindmaps."""
    genai, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        print("ERROR: GEMINI_API_KEY not set.")
        sys.exit(1)

    client = genai.Client(api_key=gemini_key)
    output_dir = resolve_output_dir(config)
    model = config.get("model", "gemini-3-flash-preview")
    taxonomy = load_taxonomy(output_dir)

    # Collect all videos that have mindmaps but no concepts.json
    to_process = []
    channels = config.get("channels", [])
    if args.channel:
        channels = [c for c in channels if c["name"] == args.channel]

    for ch in channels:
        ch_name = ch["name"]
        channel_dir = output_dir / ch_name
        if not channel_dir.exists():
            continue

        for meta_file in sorted(channel_dir.glob("*.meta.json")):
            prefix = meta_file.name.replace(".meta.json", "")
            concepts_path = channel_dir / f"{prefix}.concepts.json"

            if concepts_path.exists() and not args.force:
                continue

            mindmap_path = find_mindmap_source(channel_dir, prefix)
            if not mindmap_path:
                continue

            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            video = {
                "video_id": meta.get("video_id", ""),
                "url": meta.get("video_url", ""),
                "title": meta.get("title", prefix),
                "published": meta.get("published", ""),
            }

            to_process.append((ch_name, video, mindmap_path, meta.get("prompt")))

    if not to_process:
        print("All concepts up to date.")
        return

    print(f"Extracting concepts from {len(to_process)} mindmaps...")

    if args.dry_run:
        for ch_name, video, _mindmap_path, _ in to_process:
            print(f"  [{ch_name}] {video['published']} - {video['title']}")
        return

    for ch_name, video, mindmap_path, source_prompt in to_process:
        mindmap_text = mindmap_path.read_text(encoding="utf-8")
        source_file = mindmap_path.name

        prefix, status = process_concepts(
            client,
            types,
            video,
            mindmap_text,
            taxonomy,
            model,
            output_dir,
            ch_name,
            source_file=source_file,
            source_prompt=source_prompt,
            force=args.force,
        )
        print(f"  [{ch_name}] {prefix}: {status}")

        # Accumulate new concepts into in-memory taxonomy so the next video
        # can normalize against concepts discovered in earlier videos.
        concepts_path = output_dir / ch_name / f"{prefix}.concepts.json"
        if concepts_path.exists():
            data = json.loads(concepts_path.read_text(encoding="utf-8"))
            for c in data.get("concepts", []):
                cid = c.get("concept_id", "")
                if cid and cid not in taxonomy.get("concepts", {}):
                    taxonomy.setdefault("concepts", {})[cid] = {
                        "preferred_label": c.get("preferred_label", cid),
                        "aliases": [],
                        "domain": c.get("domain", ""),
                    }

    print("\nDone. Run 'taxonomy-build' to rebuild the master taxonomy.")


def cmd_status(args, config):
    """Show corpus status: output directory, channels, and artifact counts."""
    output_dir = resolve_output_dir(config)
    print(f"Output directory: {output_dir}")

    taxonomy_path = output_dir / "taxonomy.json"
    if taxonomy_path.exists():
        taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
        print(f"Taxonomy: {len(taxonomy.get('concepts', {}))} concepts from {taxonomy.get('built_from', 0)} files")
        print(f"Taxonomy path: {taxonomy_path}")
    else:
        print("Taxonomy: not yet built (run 'taxonomy-build')")

    print("\nChannels:")
    for ch in config.get("channels", []):
        ch_name = ch["name"]
        ch_dir = output_dir / ch_name
        if ch_dir.exists():
            mindmaps = len(list(ch_dir.glob("*.mindmap*.md")))
            transcripts = len(list(ch_dir.glob("*.transcript.md")))
            concepts = len(list(ch_dir.glob("*.concepts.json")))
            print(f"  {ch_name}: {mindmaps} mindmaps, {transcripts} transcripts, {concepts} concepts")
        else:
            print(f"  {ch_name}: not yet scanned")


def cmd_taxonomy_build(args, config):
    """Rebuild taxonomy.json from all concepts.json files."""
    output_dir = resolve_output_dir(config)
    taxonomy = build_taxonomy(output_dir)

    n_concepts = len(taxonomy["concepts"])
    n_files = taxonomy["built_from"]
    n_with_aliases = sum(1 for c in taxonomy["concepts"].values() if c.get("aliases"))
    print(f"Taxonomy built from {n_files} concept files.")
    print(f"  {n_concepts} canonical concepts ({n_with_aliases} with aliases)")
    print(f"  Saved: {output_dir / 'taxonomy.json'}")


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
  %(prog)s mindmap --url URL --prompt P   # Mind map a single video with a specific prompt
  %(prog)s concepts --backfill            # Extract concepts from all existing mindmaps
  %(prog)s taxonomy-build                 # Rebuild taxonomy.json from concept files
        """,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Scan channels for new videos")
    scan_parser.add_argument("--channel", help="Scan only this channel name")
    scan_parser.add_argument("--since", help="Override lookback window (e.g. 14d, 2026-01-01)")
    scan_parser.add_argument("--dry-run", action="store_true", help="Preview without processing")
    scan_parser.add_argument("--force", action="store_true", help="Regenerate mindmaps even if they exist")

    # mindmap command
    mm_parser = subparsers.add_parser("mindmap", help="Generate mind map for a specific video")
    mm_parser.add_argument("--url", required=True, help="YouTube video URL")
    mm_parser.add_argument("--prompt", help="Prompt name (default: config default_prompt)")
    mm_parser.add_argument("--channel", help="Channel name for output folder")
    mm_parser.add_argument("--title", help="Video title (auto-detected if omitted)")
    mm_parser.add_argument("--date", help="Publish date YYYY-MM-DD (defaults to today)")
    mm_parser.add_argument("--force", action="store_true", help="Regenerate even if mindmap exists")

    # transcript command
    tx_parser = subparsers.add_parser("transcript", help="Transcribe a specific video")
    tx_parser.add_argument("--url", required=True, help="YouTube video URL")
    tx_parser.add_argument("--channel", help="Channel name for output folder")
    tx_parser.add_argument("--title", help="Video title (auto-detected if omitted)")
    tx_parser.add_argument("--date", help="Publish date YYYY-MM-DD (defaults to today)")
    tx_parser.add_argument("--force", action="store_true", help="Regenerate even if transcript exists")

    # concepts command
    concepts_parser = subparsers.add_parser("concepts", help="Extract concepts from existing mindmaps")
    concepts_parser.add_argument("--backfill", action="store_true", help="Process all mindmaps missing concepts")
    concepts_parser.add_argument("--channel", help="Process only this channel")
    concepts_parser.add_argument("--force", action="store_true", help="Re-extract even if concepts.json exists")
    concepts_parser.add_argument("--dry-run", action="store_true", help="Preview without processing")

    # taxonomy-build command
    subparsers.add_parser("taxonomy-build", help="Rebuild taxonomy.json from all concept files")

    # status command
    subparsers.add_parser("status", help="Show corpus status: output dir, channels, artifact counts")

    args = parser.parse_args()
    config = load_config()

    if args.command == "scan":
        cmd_scan(args, config)
    elif args.command == "mindmap":
        cmd_mindmap(args, config)
    elif args.command == "transcript":
        cmd_transcript(args, config)
    elif args.command == "concepts":
        cmd_concepts(args, config)
    elif args.command == "taxonomy-build":
        cmd_taxonomy_build(args, config)
    elif args.command == "status":
        cmd_status(args, config)


if __name__ == "__main__":
    main()
