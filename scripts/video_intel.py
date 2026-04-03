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
import logging
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

log = logging.getLogger("video_intel")

# ---------------------------------------------------------------------------
# Lazy imports with clear error messages
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
# Config
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent


def load_config():
    config_path = SKILL_DIR / "config.yaml"
    if not config_path.exists():
        log.error("Config not found at %s. Copy config.yaml.example to config.yaml and edit it.", config_path)
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
        log.error("Prompt file not found: %s", prompt_path)
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
        log.error("Invalid since format: %s. Use '10d' for relative days or 'YYYY-MM-DD' for absolute date.", since_str)
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
    config_kwargs = {"temperature": 0.3, "max_output_tokens": 16384}
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
                log.warning("Rate limited, retrying in %ds...", wait)
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
        tmp_path = mindmap_path.with_suffix(".md.tmp")
        tmp_path.write_text(header + result, encoding="utf-8")
        tmp_path.replace(mindmap_path)

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
        tmp_path = transcript_path.with_suffix(".md.tmp")
        tmp_path.write_text(header + fused, encoding="utf-8")
        tmp_path.replace(transcript_path)

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
                log.warning("Rate limited, retrying in %ds...", wait)
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

        # Atomic write: temp file then rename, so a killed process can't leave
        # a half-written .concepts.json that the next run silently skips.
        tmp_path = concepts_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
        tmp_path.replace(concepts_path)
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
    tmp_path = taxonomy_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(taxonomy, indent=2), encoding="utf-8")
    tmp_path.replace(taxonomy_path)
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
        log.error("GEMINI_API_KEY not set. Get a free key at https://aistudio.google.com/apikey")
        sys.exit(1)
    if not yt_key:
        log.error(
            "YOUTUBE_API_KEY not set. Get a free key at https://console.cloud.google.com/apis/credentials (enable YouTube Data API v3)"
        )
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
            log.error("Channel '%s' not found in config.yaml", args.channel)
            sys.exit(1)

    for ch in channels:
        ch_name = ch["name"]
        ch_url = ch["url"]

        # Resolve channel
        channel_id, channel_title = get_channel_id(youtube, ch_url)
        if not channel_id:
            log.warning("[%s] Channel not found: %s", ch_name, ch_url)
            continue

        log.info("[%s] %s", ch_name, channel_title)

        # Determine time window
        since_str = args.since or ch.get("since") or config.get("default_since", "10d")
        since_dt = parse_since(since_str)
        log.info("  Looking back to %s", since_dt.strftime("%Y-%m-%d"))

        # Fetch videos
        videos = fetch_channel_videos(youtube, channel_id, since_dt)
        if not videos:
            log.info("  No new videos found.")
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
        log.info("  Found %d videos, %d %s.", len(videos), len(new_videos), label)

        if args.dry_run:
            for v in new_videos:
                log.info("    %s - %s", v["published"], v["title"])
            continue

        prompt_name = ch.get("prompt") or config.get("default_prompt", "mindmap-light")
        prompt_text = load_prompt(prompt_name)

        if not new_videos:
            log.info("  All mind maps up to date.")
        else:
            # Process mind maps in parallel
            log.info("  Generating mind maps (%s)...", prompt_name)
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
                    log.info("    %s: %s", prefix, status)
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
                log.info("  Generating transcripts (%d videos)...", len(transcript_videos))
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
                        log.info("    %s: %s", prefix, status)
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
                log.info("  Extracting concepts (%d videos)...", len(concept_videos))
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
                    log.info("    %s: %s", prefix, status)
                    if status.startswith("error"):
                        errors.append((ch_name, prefix, status))

    if errors:
        log.warning("--- %d FAILED ---", len(errors))
        for ch, prefix, status in errors:
            log.warning("  [%s] %s: %s", ch, prefix, status)
        log.warning("Failed items will retry on next run.")
        log.warning('To skip permanently: set "skip": true in the video\'s .meta.json')

    log.info("Done.")


def cmd_mindmap(args, config):
    """Generate a mind map for a single video with a specific prompt."""
    genai, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
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
        log.error("Could not extract video ID from: %s", args.url)
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

    log.info("Generating mind map (%s): %s", prompt_name, video["url"])
    prefix, status = process_mindmap(
        client, types, video, prompt_text, model, output_dir, channel_name, prompt_name=prompt_name, force=args.force
    )
    log.info("  %s: %s", prefix, status)

    if status == "done":
        out_path = output_dir / channel_name / f"{prefix}.mindmap.md"
        log.info("  Saved: %s", out_path)


def cmd_transcript(args, config):
    """Generate a transcript for a single video."""
    genai, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)

    client = genai.Client(api_key=gemini_key)
    output_dir = resolve_output_dir(config)
    model = config.get("model", "gemini-3-flash-preview")
    prompt_text = load_prompt("transcript")

    # Build video object from URL
    video_id_match = re.search(r"(?:v=|/)([a-zA-Z0-9_-]{11})", args.url)
    if not video_id_match:
        log.error("Could not extract video ID from: %s", args.url)
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

    log.info("Transcribing: %s", video["url"])
    prefix, status = process_transcript(
        client, types, video, prompt_text, model, output_dir, channel_name, force=args.force
    )
    log.info("  %s: %s", prefix, status)

    if status == "done":
        out_path = output_dir / channel_name / f"{prefix}.transcript.md"
        log.info("  Saved: %s", out_path)


def cmd_concepts(args, config):
    """Extract and normalize concepts from existing mindmaps."""
    genai, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
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
        log.info("All concepts up to date.")
        return

    total = len(to_process)
    log.info("Extracting concepts from %d mindmaps...", total)

    if args.dry_run:
        for ch_name, video, _mindmap_path, _ in to_process:
            log.info("  [%s] %s - %s", ch_name, video["published"], video["title"])
        return

    t0 = time.monotonic()
    for i, (ch_name, video, mindmap_path, source_prompt) in enumerate(to_process, 1):
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
        log.info("[%d/%d] [%s] %s: %s", i, total, ch_name, prefix, status)

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

    elapsed = time.monotonic() - t0
    minutes, seconds = divmod(int(elapsed), 60)
    log.info(
        "Done. %d videos in %dm %ds. Run 'taxonomy-build' to rebuild the master taxonomy.", total, minutes, seconds
    )


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


# ---------------------------------------------------------------------------
# Vector search (Phase 2): LanceDB + Voyage AI
# ---------------------------------------------------------------------------

LANCEDB_DIR = ".lancedb"
LANCEDB_TABLE = "transcript_chunks"
VOYAGE_DOC_MODEL = "voyage-4-large"
VOYAGE_QUERY_MODEL = "voyage-4-lite"
VOYAGE_DIMS = 1024
VOYAGE_BATCH_SIZE = 128


def require_lancedb():
    try:
        import lancedb

        return lancedb
    except ImportError:
        log.error("lancedb not installed. Run: pip install 'video-intel[vector]'")
        sys.exit(1)


def require_voyageai():
    try:
        import voyageai

        return voyageai
    except ImportError:
        log.error("voyageai not installed. Run: pip install 'video-intel[vector]'")
        sys.exit(1)


def _parse_timestamp_seconds(ts: str) -> int:
    """Convert 'MM:SS' or 'HH:MM:SS' to seconds."""
    parts = ts.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


def chunk_transcript(transcript_path: Path, chunk_size: int = 5) -> list[dict]:
    """Split a transcript into timestamped chunks of ~chunk_size entries.

    Each entry is a [MM:SS] speech line or SCREEN block. Returns list of dicts
    with keys: text, timestamp, timestamp_seconds.
    """
    text = transcript_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # Parse into entries: each starts with [MM:SS] or '  SCREEN ['
    entries = []
    current_entry = []
    current_ts = None

    for line in lines:
        # Speech line: [MM:SS] Speaker: "text"
        speech_match = re.match(r"^\[(\d{1,2}:\d{2}(?::\d{2})?)\]", line)
        # Screen line:   SCREEN [MM:SS-MM:SS]
        screen_match = re.match(r"^\s+SCREEN \[(\d{1,2}:\d{2}(?::\d{2})?)", line)

        if speech_match or screen_match:
            # Save previous entry
            if current_entry:
                entries.append({"text": "\n".join(current_entry), "timestamp": current_ts or "00:00"})
            current_entry = [line]
            current_ts = (speech_match or screen_match).group(1)
        elif current_entry:
            # Continuation of current entry
            current_entry.append(line)
        # Skip header lines before first entry

    # Don't forget last entry
    if current_entry:
        entries.append({"text": "\n".join(current_entry), "timestamp": current_ts or "00:00"})

    if not entries:
        return []

    # Group entries into chunks of chunk_size
    chunks = []
    for i in range(0, len(entries), chunk_size):
        group = entries[i : i + chunk_size]
        chunk_text = "\n\n".join(e["text"] for e in group)
        first_ts = group[0]["timestamp"]
        chunks.append(
            {
                "text": chunk_text.strip(),
                "timestamp": first_ts,
                "timestamp_seconds": _parse_timestamp_seconds(first_ts),
            }
        )

    return chunks


def _load_concepts_for_video(concepts_path: Path) -> list[str]:
    """Load concept_ids from a video's concepts.json."""
    if not concepts_path.exists():
        return []
    data = json.loads(concepts_path.read_text(encoding="utf-8"))
    return [c.get("concept_id", "") for c in data.get("concepts", []) if c.get("concept_id")]


def _extract_video_metadata(prefix: str, channel_dir: Path, channel_name: str) -> dict:
    """Extract video metadata from meta.json for index records."""
    meta_path = channel_dir / f"{prefix}.meta.json"
    title = prefix
    published = ""
    video_id = ""
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        title = meta.get("title", prefix)
        published = meta.get("published", "")
        video_id = meta.get("video_id", "")
    return {"title": title, "published": published, "video_id": video_id, "channel": channel_name}


def _embed_batch(vo_client, texts: list[str], model: str, input_type: str) -> list[list[float]]:
    """Embed a list of texts with Voyage AI, handling batch size and rate limits."""
    all_embeddings = []
    total_batches = (len(texts) + VOYAGE_BATCH_SIZE - 1) // VOYAGE_BATCH_SIZE

    for batch_num, i in enumerate(range(0, len(texts), VOYAGE_BATCH_SIZE)):
        batch = texts[i : i + VOYAGE_BATCH_SIZE]
        max_retries = 5
        for attempt in range(max_retries + 1):
            try:
                result = vo_client.embed(batch, model=model, input_type=input_type)
                all_embeddings.extend(result.embeddings)
                log.info("[%d/%d] Embedded %d chunks", batch_num + 1, total_batches, len(batch))
                # Pace requests to stay under rate limits (3 RPM on free tier)
                if batch_num < total_batches - 1:
                    time.sleep(1)
                break
            except Exception as e:
                error_str = str(e).lower()
                is_rate_limit = "rate" in error_str and "limit" in error_str
                is_connection = "connection" in error_str or "resolve" in error_str or "timeout" in error_str
                if (is_rate_limit or is_connection) and attempt < max_retries:
                    wait = 25 * (2**attempt) + random.uniform(0, 5)
                    reason = "rate limited" if is_rate_limit else "connection error"
                    log.warning("Voyage %s, waiting %ds (attempt %d/%d)...", reason, wait, attempt + 1, max_retries)
                    time.sleep(wait)
                else:
                    raise

    return all_embeddings


def build_search_index(output_dir: Path, *, channel_filter: str | None = None, force: bool = False) -> int:
    """Build or rebuild the LanceDB vector index from transcripts + concepts.

    Returns the number of chunks indexed.
    """
    lancedb = require_lancedb()
    voyageai = require_voyageai()

    vo_key = os.environ.get("VOYAGE_API_KEY")
    if not vo_key:
        log.error("VOYAGE_API_KEY not set. Sign up free at https://dash.voyageai.com/")
        sys.exit(1)

    vo = voyageai.Client()
    db_path = str(output_dir / LANCEDB_DIR)
    db = lancedb.connect(db_path)

    # Drop existing table if force rebuild
    if force and LANCEDB_TABLE in db.list_tables().tables:
        db.drop_table(LANCEDB_TABLE)
        log.info("Dropped existing table '%s' for rebuild", LANCEDB_TABLE)

    # Collect all transcript chunks
    all_records = []
    channels = [d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]

    for channel_dir in sorted(channels):
        ch_name = channel_dir.name
        if channel_filter and ch_name != channel_filter:
            continue

        transcripts = sorted(channel_dir.glob("*.transcript.md"))
        log.info("[%s] Found %d transcripts", ch_name, len(transcripts))

        for tx_path in transcripts:
            prefix = tx_path.name.replace(".transcript.md", "")
            concepts_path = channel_dir / f"{prefix}.concepts.json"
            concept_ids = _load_concepts_for_video(concepts_path)
            meta = _extract_video_metadata(prefix, channel_dir, ch_name)

            chunks = chunk_transcript(tx_path)
            for chunk in chunks:
                all_records.append(
                    {
                        "text": chunk["text"],
                        "timestamp": chunk["timestamp"],
                        "timestamp_seconds": chunk["timestamp_seconds"],
                        "video_id": meta["video_id"],
                        "channel": meta["channel"],
                        "title": meta["title"],
                        "published": meta["published"],
                        "concept_ids": json.dumps(concept_ids),
                        "source_file": str(tx_path),
                    }
                )

    if not all_records:
        log.warning("No transcript chunks found to index.")
        return 0

    log.info("Embedding %d chunks with %s...", len(all_records), VOYAGE_DOC_MODEL)
    texts = [r["text"] for r in all_records]
    embeddings = _embed_batch(vo, texts, VOYAGE_DOC_MODEL, input_type="document")

    # Attach vectors to records
    for rec, vec in zip(all_records, embeddings, strict=True):
        rec["vector"] = vec

    # Create or overwrite table
    table = db.create_table(LANCEDB_TABLE, data=all_records, mode="overwrite")

    # Create indices for efficient search
    if len(all_records) >= 256:
        table.create_index(metric="cosine", vector_column_name="vector")
    table.create_fts_index("text")
    table.create_fts_index("title")

    log.info("Indexed %d chunks into %s", len(all_records), db_path)
    return len(all_records)


def hybrid_search(
    output_dir: Path,
    query: str,
    *,
    channel_filter: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search the LanceDB index with hybrid BM25 + vector + RRF fusion.

    Returns ranked chunks deduplicated by video.
    """
    lancedb = require_lancedb()
    voyageai = require_voyageai()

    vo_key = os.environ.get("VOYAGE_API_KEY")
    if not vo_key:
        log.error("VOYAGE_API_KEY not set. Sign up free at https://dash.voyageai.com/")
        sys.exit(1)

    db_path = str(output_dir / LANCEDB_DIR)
    db = lancedb.connect(db_path)

    if LANCEDB_TABLE not in db.list_tables().tables:
        log.error("Search index not found. Run: video_intel.py index")
        return []

    table = db.open_table(LANCEDB_TABLE)

    # Embed query with lite model (asymmetric retrieval)
    vo = voyageai.Client()
    query_embedding = vo.embed([query], model=VOYAGE_QUERY_MODEL, input_type="query").embeddings[0]

    # Hybrid search: BM25 (FTS on title+text) + vector, merged by RRF (K=60 default)
    fetch_count = max(50, limit * 5)
    search_builder = (
        table.search(query_type="hybrid", fts_columns=["title", "text"])
        .vector(query_embedding)
        .text(query)
        .limit(fetch_count)
    )
    if channel_filter:
        search_builder = search_builder.where(f"channel = '{channel_filter}'")

    results = search_builder.to_pandas()

    # Convert rows to dicts — hybrid returns _relevance_score (higher = better)
    raw_hits = []
    for _, row in results.iterrows():
        raw_hits.append(
            {
                "text": row["text"],
                "timestamp": row["timestamp"],
                "video_id": row.get("video_id", ""),
                "channel": row.get("channel", ""),
                "title": row.get("title", ""),
                "published": row.get("published", ""),
                "source_file": row.get("source_file", ""),
                "concept_ids": row.get("concept_ids", "[]"),
                "relevance": float(row.get("_relevance_score", 0.0)),
            }
        )

    return _dedup_by_video(raw_hits, limit)


def _dedup_by_video(hits: list[dict], limit: int) -> list[dict]:
    """Keep only the best-scoring chunk per video_id, return top `limit` videos."""
    best_per_video: dict[str, dict] = {}
    for hit in hits:
        vid = hit.get("video_id", "")
        if not vid:
            vid = hit.get("source_file", "")  # fallback key
        score = hit["relevance"]
        if vid not in best_per_video or score > best_per_video[vid]["relevance"]:
            best_per_video[vid] = hit

    deduped = sorted(best_per_video.values(), key=lambda h: h["relevance"], reverse=True)
    return deduped[:limit]


def cmd_index(args, config):
    """Build or rebuild the vector search index."""
    output_dir = resolve_output_dir(config)
    t0 = time.time()
    count = build_search_index(output_dir, channel_filter=args.channel, force=args.force)
    elapsed = time.time() - t0

    if count == 0:
        print("No transcripts found to index.")
    else:
        mins, secs = divmod(int(elapsed), 60)
        print(f"Indexed {count} chunks in {mins}m {secs:02d}s.")
        print(f"  Index: {output_dir / LANCEDB_DIR}")
        print("  Run 'search --vector \"query\"' to search.")


def search_corpus(output_dir: Path, query: str, *, channel_filter: str | None = None, limit: int = 20) -> dict:
    """Search taxonomy + concepts for matching videos. Returns structured results."""
    taxonomy = load_taxonomy(output_dir)
    query_lower = query.lower()
    query_terms = query_lower.split()

    # Search concepts by preferred_label and aliases
    matching_concepts = []
    for cid, concept in taxonomy.get("concepts", {}).items():
        label = concept.get("preferred_label", "")
        aliases = concept.get("aliases", [])
        searchable = f"{label} {' '.join(aliases)}".lower()

        # Score: count how many query terms match
        matched_terms = sum(1 for term in query_terms if term in searchable)
        if matched_terms > 0:
            matching_concepts.append(
                {
                    "concept_id": cid,
                    "preferred_label": label,
                    "aliases": aliases,
                    "video_count": concept.get("video_count", 0),
                    "domain": concept.get("domain", ""),
                    "_match_score": matched_terms / len(query_terms),  # 1.0 = all terms matched
                }
            )

    # Sort by match score (exact > partial), then video_count
    matching_concepts.sort(key=lambda c: (-c["_match_score"], -c["video_count"]))

    # If we have exact matches (1.0), only use those for video lookup.
    # If no exact matches, fall back to partial matches (top 5 to limit noise).
    exact = [c for c in matching_concepts if c["_match_score"] == 1.0]
    if exact:
        concepts_for_videos = exact
    else:
        concepts_for_videos = matching_concepts[:5]

    # Find videos that contain these concepts
    matching_cids = {c["concept_id"] for c in concepts_for_videos}
    matching_videos = []
    seen_video_ids = set()

    channels = [d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    for channel_dir in sorted(channels):
        ch_name = channel_dir.name
        if channel_filter and ch_name != channel_filter:
            continue

        for concepts_file in sorted(channel_dir.glob("*.concepts.json")):
            data = json.loads(concepts_file.read_text(encoding="utf-8"))
            video_id = data.get("video_id", "")
            if video_id in seen_video_ids:
                continue

            # Check if this video has any matching concepts
            video_concepts = data.get("concepts", [])
            matched_in_video = [c for c in video_concepts if c.get("concept_id") in matching_cids]

            if not matched_in_video:
                continue

            seen_video_ids.add(video_id)

            # Find artifact paths
            prefix = concepts_file.name.replace(".concepts.json", "")
            mindmap_path = find_mindmap_source(channel_dir, prefix)
            transcript_path = channel_dir / f"{prefix}.transcript.md"
            meta_path = channel_dir / f"{prefix}.meta.json"

            title = prefix
            published = ""
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = meta.get("title", prefix)
                published = meta.get("published", "")

            matching_videos.append(
                {
                    "channel": ch_name,
                    "title": title,
                    "published": published,
                    "video_id": video_id,
                    "matched_concepts": [c.get("concept_id") for c in matched_in_video],
                    "mindmap": str(mindmap_path) if mindmap_path else None,
                    "transcript": str(transcript_path) if transcript_path.exists() else None,
                }
            )

    # Sort by number of matched concepts (most relevant first), then date
    matching_videos.sort(key=lambda v: (-len(v["matched_concepts"]), v.get("published", "")))

    return {
        "query": query,
        "concepts": matching_concepts[:limit],
        "videos": matching_videos[:limit],
    }


def cmd_search(args, config):
    """Search the corpus for videos matching a query."""
    output_dir = resolve_output_dir(config)

    # Resolve per-mode default limit (None means user didn't pass --limit)
    if args.limit is None:
        args.limit = 10 if getattr(args, "vector", False) else 20

    # Hybrid search mode (BM25 + vector + RRF)
    if getattr(args, "vector", False):
        hits = hybrid_search(output_dir, args.query, channel_filter=args.channel, limit=args.limit)
        if not hits:
            print(f'No results for "{args.query}". Is the index built? Run: video_intel.py index')
            return

        # Filter out weak matches below relevance threshold
        min_rel = getattr(args, "min_relevance", 0.0)
        strong_hits = [h for h in hits if h["relevance"] >= min_rel]

        if not strong_hits:
            print(f'No strong matches for "{args.query}" (best relevance: {hits[0]["relevance"]:.4f}).')
            print("Try broader terms, lower --min-relevance, or use concept search without --vector.")
            return

        print(f'Hybrid results for "{args.query}" ({len(strong_hits)} videos):\n')
        preview_mode = getattr(args, "preview", False)
        for i, hit in enumerate(strong_hits, 1):
            print(f"  [{i}] [{hit['channel']}] {hit['published']}  {hit['title']}")
            print(f"      Timestamp: [{hit['timestamp']}]  Relevance: {hit['relevance']:.4f}")
            if preview_mode:
                display = hit["text"][:200].replace("\n", " ")
                if len(hit["text"]) > 200:
                    display += "..."
                print(f"      {display}")
            else:
                display = hit["text"][:3000]
                if len(hit["text"]) > 3000:
                    display += "\n      [truncated — see source]"
                # Indent each line for visual grouping under the header
                for line in display.split("\n"):
                    print(f"      {line}")
            print(f"      Source: {hit['source_file']}")
            print()
        return

    # Concept search mode (default)
    results = search_corpus(output_dir, args.query, channel_filter=args.channel, limit=args.limit)

    if not results["concepts"]:
        print(f'No concepts matching "{args.query}".')
        print("Try broader terms, or run 'taxonomy-build' if concepts are stale.")
        return

    print("Matching concepts:")
    for c in results["concepts"]:
        aliases_str = ", ".join(c["aliases"][:5]) if c["aliases"] else "(no aliases)"
        match_pct = int(c.get("_match_score", 1.0) * 100)
        match_label = "" if match_pct == 100 else f" [partial {match_pct}%]"
        print(f"  {c['concept_id']} ({c['video_count']} videos){match_label}")
        print(f"    Label: {c['preferred_label']}")
        print(f"    Aliases: {aliases_str}")
        print()

    if not results["videos"]:
        print("No videos found with these concepts.")
        return

    print(f"Videos ({len(results['videos'])}):")
    for v in results["videos"]:
        print(f"  [{v['channel']}] {v['published']}  {v['title']}")
        if v["mindmap"]:
            print(f"    mindmap:    {v['mindmap']}")
        if v["transcript"]:
            print(f"    transcript: {v['transcript']}")


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
  %(prog)s concepts                       # Extract concepts from all existing mindmaps
  %(prog)s taxonomy-build                 # Rebuild taxonomy.json from concept files
  %(prog)s search "skills standard"       # Search corpus by concept
  %(prog)s search "context window" --channel natebjones
  %(prog)s index                           # Build vector search index
  %(prog)s search "permission problems" --vector  # Semantic search
        """,
    )
    parser.add_argument(
        "--log-level",
        default="warning",
        choices=["debug", "info", "warning", "error"],
        help="Set logging verbosity (default: warning)",
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
    concepts_parser.add_argument("--channel", help="Process only this channel")
    concepts_parser.add_argument("--force", action="store_true", help="Re-extract even if concepts.json exists")
    concepts_parser.add_argument("--dry-run", action="store_true", help="Preview without processing")

    # taxonomy-build command
    subparsers.add_parser("taxonomy-build", help="Rebuild taxonomy.json from all concept files")

    # search command
    search_parser = subparsers.add_parser("search", help="Search corpus by concept or vector similarity")
    search_parser.add_argument("query", help="Search terms (matched against concept labels and aliases)")
    search_parser.add_argument("--channel", help="Filter results to this channel")
    search_parser.add_argument(
        "--limit", type=int, default=None, help="Max results (default: 10 for --vector, 20 for concept)"
    )
    search_parser.add_argument(
        "--vector", action="store_true", help="Use vector search (requires index; see 'index' command)"
    )
    search_parser.add_argument(
        "--preview", action="store_true", help="Show compact 200-char previews instead of full chunk text"
    )
    search_parser.add_argument(
        "--min-relevance",
        type=float,
        default=0.0,
        dest="min_relevance",
        help="Minimum relevance score for hybrid results (default: 0.0, RRF scale)",
    )

    # index command
    index_parser = subparsers.add_parser("index", help="Build vector search index from transcripts")
    index_parser.add_argument("--channel", help="Index only this channel")
    index_parser.add_argument("--force", action="store_true", help="Rebuild index from scratch")

    # status command
    subparsers.add_parser("status", help="Show corpus status: output dir, channels, artifact counts")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log.setLevel(getattr(logging, args.log_level.upper()))
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
    elif args.command == "search":
        cmd_search(args, config)
    elif args.command == "index":
        cmd_index(args, config)
    elif args.command == "status":
        cmd_status(args, config)


if __name__ == "__main__":
    main()
