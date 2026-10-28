#!/usr/bin/env python3
"""Atlas catalog generator, data layer (issue #260).

`build_rows(corpus)` walks a video-intel corpus once and returns one catalog
row per `video_id`, plus the referenced-concept label lookup and a summary.
Nothing is written to disk here; the writer and CLI live beside this in the
same module once they ship.

Contracts this module keeps:

* **Import weight.** Module-level imports are the standard library plus
  `corpus_walk`, `timestamp_utils` and `wiki_concepts`, all three stdlib-only
  at import. Never import `video_intel`, `intel_graph`, `yaml` or any
  third-party package here. PyYAML loads only when the briefing front-matter
  reader runs, never on `import atlas_build`.
* **Explicit projection.** Rows are built field by field in `ROW_KEYS` order,
  never by spreading a meta dict, so a free-text meta field (`description`,
  `last_error`, `prompt`, `skip_reason`, ...) cannot leak into the catalog.
* **Transcript text is never read.** A transcript's presence is a key in the
  walker's sibling index; this module never opens a `.transcript.md` file.
* **Reading never writes.** Unusable inputs are skipped, logged and counted,
  never quarantined or repaired.
* **Inputs are untrusted text.** Titles, headings and labels are stripped of
  C0/C1 controls and bidi overrides, NFC-normalized and capped; `<` and
  markdown link syntax stay literal, so renderers must escape them.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from wiki_concepts import mindmap_headings, parse_mindmap_timestamps

from corpus_walk import (
    _HEALTHY_TRANSCRIPT_STATUSES,
    CONCEPTS_SUFFIX,
    MINDMAP_SUFFIX,
    TRANSCRIPT_STATUS_TRUNCATED,
    TRANSCRIPT_SUFFIX,
    MetaRecord,
    briefing_video_counts,
    frontmatter_value,
    is_skipped_meta,
    iter_meta_records,
    read_json_best_effort,
    transcript_quality_flags_are_severe,
)
from timestamp_utils import timestamp_tolerance, youtube_deep_link

log = logging.getLogger("video_intel.atlas_build")

#: Row keys in schema order (R11).
ROW_KEYS = (
    "video_id",
    "title",
    "channel",
    "channels",
    "published",
    "duration_seconds",
    "url",
    "transcript_quality",
    "transcript_flag_count",
    "synopsis",
    "deep_links",
    "concept_ids",
    "topics",
    "in_briefings",
    "briefing_count",
    "wiki",
    "aliases",
)

#: Cap on any title, heading or label carried into the catalog (KTD9).
MAX_TEXT_CHARS = 200
#: Per-file read cap for the corpus-wide lookups (KTD9). Meta and concepts use
#: `corpus_walk.DEFAULT_MAX_JSON_BYTES`.
MAX_LOOKUP_JSON_BYTES = 50 * 1024 * 1024
#: Per-file read cap for a mindmap. A real one is tens of KB.
MAX_MINDMAP_BYTES = 4 * 1024 * 1024
#: Bytes read from a wiki page to find its frontmatter. Frontmatter is a few
#: lines at the top; a block longer than this has no closing `---` in view and
#: reads as no frontmatter.
WIKI_FRONTMATTER_BYTES = 64 * 1024
#: At most this many deep links per row (R7).
MAX_DEEP_LINKS = 3

_BIDI_OVERRIDES = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A)))
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass
class BuildResult:
    """The catalog's content before serialization.

    `max_processed` and `max_published` exist for the writer's deterministic
    `generated_at` fallback; neither is a wall-clock value.
    """

    rows: list[dict] = field(default_factory=list)
    concepts: dict[str, str] = field(default_factory=dict)
    summary: dict = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)
    max_processed: str | None = None
    max_published: str | None = None


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------


def clean_text(value: str) -> str:
    """Strip C0/C1 controls and bidi overrides, NFC-normalize, trim, cap (KTD9).

    `<`, `>` and `[...](...)` stay literal: this is plain text, and escaping is
    the renderer's job.
    """
    kept = "".join(ch for ch in value if unicodedata.category(ch) != "Cc" and ch not in _BIDI_OVERRIDES)
    return unicodedata.normalize("NFC", kept).strip()[:MAX_TEXT_CHARS]


def row_title(record: MetaRecord) -> str:
    title = record.meta.get("title")
    if isinstance(title, str):
        cleaned = clean_text(title)
        if cleaned:
            return cleaned
    log.warning("  %s: `title` is not a non-empty string; using the file prefix", record.prefix)
    return clean_text(record.prefix)


def row_published(value: object) -> str:
    """The first ten characters when they are a real `YYYY-MM-DD` date, else ""."""
    if not isinstance(value, str):
        return ""
    head = value[:10]
    if not _DATE_RE.fullmatch(head):
        return ""
    try:
        dt.date.fromisoformat(head)
    except ValueError:
        return ""
    return head


def row_duration(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def row_url(value: object) -> str | None:
    """The stored URL when it is a well-formed https URL of any host, else None (R9).

    Never built from an id: a meta without a usable `video_url` has no URL.
    """
    if not isinstance(value, str) or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc:
        return None
    return value


def transcript_quality(meta: dict, siblings: dict[str, Path]) -> str:
    """R8, decided in exactly this order."""
    if transcript_quality_flags_are_severe(meta.get("transcript_quality_flags")):
        return "severe"
    status = meta.get("transcript_status")
    if status == TRANSCRIPT_STATUS_TRUNCATED:
        return "truncated"
    if status == "partial":
        return "partial"
    if isinstance(status, str) and status in _HEALTHY_TRANSCRIPT_STATUSES:
        return "complete"
    if is_skipped_meta(meta, "transcript"):
        return "skipped"
    if status is not None or TRANSCRIPT_SUFFIX in siblings:
        return "unknown"
    return "none"


def transcript_flag_count(meta: dict) -> int:
    flags = meta.get("transcript_quality_flags")
    if not isinstance(flags, list):
        return 0
    return sum(1 for f in flags if isinstance(f, str))


def deep_links(mindmap_text: str, url: str | None, duration: int | None) -> list[dict]:
    """R7: at most three `{label, seconds, url}` entries, one per branch.

    Branches are taken in document order; within a branch the first leaf that
    has a timestamp, is not past `duration + timestamp_tolerance(duration)`
    (when the duration is known) and whose seconds no earlier link used, wins.
    No qualifying leaf means no link: a timestamp is never invented.
    """
    if youtube_deep_link(url, 0) is None:
        return []
    limit = None if duration is None else duration + timestamp_tolerance(duration)
    links: list[dict] = []
    used_seconds: set[int] = set()
    done_branches: set[str] = set()
    for entry in parse_mindmap_timestamps(mindmap_text):
        if len(links) >= MAX_DEEP_LINKS:
            break
        branch = entry.heading
        if not branch or branch in done_branches or entry.seconds is None:
            continue
        if entry.seconds in used_seconds or (limit is not None and entry.seconds > limit):
            continue
        link = youtube_deep_link(url, entry.seconds)
        if link is None:
            continue
        done_branches.add(branch)
        used_seconds.add(entry.seconds)
        links.append({"label": clean_text(branch), "seconds": entry.seconds, "url": link})
    return links


def concept_ids(path: Path | None, skipped: list[str]) -> tuple[list[str], bool]:
    """Concept ids from a `.concepts.json`, plus whether its shape was malformed.

    An absent file is normal and silent. An unreadable one is skipped by the
    reader (logged, counted in `skipped`).
    """
    if path is None:
        return [], False
    data = read_json_best_effort(path, skipped)
    if data is None:
        return [], False
    entries = data.get("concepts")
    if not isinstance(entries, list):
        log.warning("  %s: `concepts` is not a list; no concept ids for this row", path)
        return [], True
    ids: list[str] = []
    malformed = False
    for entry in entries:
        cid = entry.get("concept_id") if isinstance(entry, dict) else None
        if isinstance(cid, str) and cid:
            if cid not in ids:
                ids.append(cid)
        else:
            malformed = True
    if malformed:
        log.warning("  %s: some `concepts` entries lack a string `concept_id`; they were ignored", path)
    return ids, malformed


def _read_mindmap(path: Path | None, skipped: list[str]) -> str:
    if path is None:
        return ""
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_MINDMAP_BYTES + 1)
        if len(raw) > MAX_MINDMAP_BYTES:
            raise ValueError(f"larger than {MAX_MINDMAP_BYTES} bytes")
        return raw.decode("utf-8")
    except (ValueError, OSError) as exc:
        log.warning("skipping mindmap %s: %s", path, exc)
        skipped.append(str(path))
        return ""


# ---------------------------------------------------------------------------
# Corpus-wide lookups, each read once
# ---------------------------------------------------------------------------


def _read_lookup(path: Path, name: str, skipped: list[str], warnings: set[str]) -> dict | None:
    """The `name` key of a lookup JSON as a dict, or None with a warning code."""
    before = len(skipped)
    data = read_json_best_effort(path, skipped, max_bytes=MAX_LOOKUP_JSON_BYTES)
    if data is None:
        warnings.add(f"{path.stem}_malformed" if len(skipped) > before else f"{path.stem}_missing")
        return None
    inner = data.get(name)
    if not isinstance(inner, dict):
        log.warning("%s: `%s` is not an object; ignoring it", path, name)
        warnings.add(f"{path.stem}_malformed")
        return None
    return inner


def topics_by_video(corpus: Path, skipped: list[str], warnings: set[str]) -> dict[str, list[str]]:
    """video_id -> sorted topic slugs, inverted once from `topics.json`."""
    topics = _read_lookup(corpus / "topics.json", "topics", skipped, warnings)
    by_video: dict[str, set[str]] = {}
    if topics is None:
        return {}
    malformed = False
    for slug, entry in topics.items():
        videos = entry.get("videos") if isinstance(entry, dict) else None
        if not isinstance(videos, list):
            malformed = True
            continue
        for video in videos:
            vid = video.get("video_id") if isinstance(video, dict) else None
            if isinstance(vid, str) and vid:
                by_video.setdefault(vid, set()).add(slug)
            else:
                malformed = True
    if malformed:
        log.warning("%s: some topic entries are malformed; they were ignored", corpus / "topics.json")
        warnings.add("topics_malformed")
    return {vid: sorted(slugs) for vid, slugs in by_video.items()}


def wiki_page_keys(corpus: Path, skipped: list[str]) -> tuple[set[str], set[str]]:
    """(creators, concept ids) that have a `_wiki/` page, from page frontmatter (KTD6).

    `creator:` marks a `wiki_atlas` dossier, `concept_id:` a `wiki_concepts`
    page and `concept:` a `wiki_atlas` concept page. Keys, never re-slugified
    file names: the two generators slugify differently.
    """
    creators: set[str] = set()
    concepts: set[str] = set()
    wiki = corpus / "_wiki"
    if not wiki.is_dir():
        return creators, concepts
    for page in sorted(wiki.rglob("*.md")):
        try:
            with page.open("rb") as fh:
                text = fh.read(WIKI_FRONTMATTER_BYTES).decode("utf-8", errors="replace")
        except OSError as exc:
            log.warning("skipping wiki page %s: %s", page, exc)
            skipped.append(str(page))
            continue
        creator = frontmatter_value(text, "creator")
        if creator:
            creators.add(creator)
        for key in ("concept_id", "concept"):
            value = frontmatter_value(text, key)
            if value:
                concepts.add(value)
    return creators, concepts


def concept_labels(corpus: Path, referenced: set[str], skipped: list[str], warnings: set[str]) -> dict[str, str]:
    """id -> preferred label for referenced ids; the label falls back to the id."""
    taxonomy = _read_lookup(corpus / "taxonomy.json", "concepts", skipped, warnings) or {}
    labels: dict[str, str] = {}
    for cid in sorted(referenced):
        entry = taxonomy.get(cid)
        label = entry.get("preferred_label") if isinstance(entry, dict) else None
        cleaned = clean_text(label) if isinstance(label, str) else ""
        labels[cid] = cleaned or clean_text(cid)
    return labels


def _parse_processed(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_rows(corpus: Path) -> BuildResult:
    """Build every catalog row, the concept lookup and the summary. Writes nothing."""
    walk = iter_meta_records(corpus)
    skipped = list(walk.skipped_files)
    warnings: set[str] = set()
    topics = topics_by_video(corpus, skipped, warnings)
    briefing_counts = briefing_video_counts(corpus / "_briefings")
    wiki_creators, wiki_concepts = wiki_page_keys(corpus, skipped)

    rows: list[dict] = []
    processed: list[dt.datetime] = []
    for record in walk.records:
        meta = record.meta
        url = row_url(meta.get("video_url"))
        duration = row_duration(meta.get("duration_seconds"))
        mindmap = _read_mindmap(record.siblings.get(MINDMAP_SUFFIX), skipped)
        ids, malformed = concept_ids(record.siblings.get(CONCEPTS_SUFFIX), skipped)
        if malformed:
            warnings.add("concepts_malformed")
        count = briefing_counts.get(record.video_id, 0)
        row = {
            "video_id": record.video_id,
            "title": row_title(record),
            "channel": record.channel,
            "channels": list(record.channels),
            "published": row_published(meta.get("published")),
            "duration_seconds": duration,
            "url": url,
            "transcript_quality": transcript_quality(meta, record.siblings),
            "transcript_flag_count": transcript_flag_count(meta),
            "synopsis": [h for h in (clean_text(h) for h in mindmap_headings(mindmap)) if h],
            "deep_links": deep_links(mindmap, url, duration),
            "concept_ids": ids,
            "topics": topics.get(record.video_id, []),
            "in_briefings": count > 0,
            "briefing_count": count,
            "wiki": {
                "creator": record.channel if record.channel in wiki_creators else None,
                "concepts": sorted(cid for cid in ids if cid in wiki_concepts),
            },
            "aliases": [clean_text(a) for a in record.aliases],
        }
        rows.append(row)
        stamp = _parse_processed(meta.get("processed"))
        if stamp is not None:
            processed.append(stamp)
    rows.sort(key=lambda r: (r["published"], r["video_id"]))

    referenced = {cid for row in rows for cid in row["concept_ids"]}
    labels = concept_labels(corpus, referenced, skipped, warnings)
    unthreaded = sum(1 for row in rows if not row["topics"])
    published = [row["published"] for row in rows if row["published"]]
    summary = {
        "channels": dict(sorted(Counter(row["channel"] for row in rows).items())),
        "topics": dict(sorted(Counter(slug for row in rows for slug in row["topics"]).items())),
        "unthreaded": unthreaded,
        "unthreaded_share": round(unthreaded / len(rows), 4) if rows else None,
        "metas": walk.metas,
        "rows": len(rows),
        "duplicates": walk.duplicates,
        "skipped": walk.skipped,
        "no_id": walk.no_id,
        "excluded_metas": walk.excluded_metas,
        "warnings": sorted(warnings),
    }
    return BuildResult(
        rows=rows,
        concepts=labels,
        summary=summary,
        skipped_files=skipped,
        max_processed=max(processed).isoformat() if processed else None,
        max_published=max(published) if published else None,
    )
