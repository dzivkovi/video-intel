#!/usr/bin/env python3
"""Atlas catalog generator, data layer (issue #260).

`build_rows(corpus)` walks a video-intel corpus once and returns one catalog
row per `video_id`, plus the referenced-concept label lookup and a summary.
`main` serializes that into `<corpus>/_atlas/atlas.json` (or `--out`) and
prints one counts-only line:

    python scripts/atlas_build.py --corpus <output_dir> [--out DIR] [--index-dir DIR] [--generated-at STAMP]

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
* **Byte-stable output, no wall clock.** `generated_at` comes from
  `--generated-at`, else the newest `processed` stamp, else the newest
  `published` date, else null. Unchanged inputs give identical bytes.
* **Single-file ownership.** An existing `atlas.json` is replaced only when
  its `generator` key is exactly `GENERATOR_NAME`; the write goes through a
  same-directory temp and one `os.replace`.
* **Counts-only stdout.** The one stdout line never names a channel, topic,
  title or path; per-file detail goes to stderr through the logger.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from wiki_concepts import mindmap_headings, parse_mindmap_timestamps

from corpus_walk import (
    CONCEPTS_SUFFIX,
    HEALTHY_TRANSCRIPT_STATUSES,
    MINDMAP_SUFFIX,
    SEARCH_INDEX_DIRNAME,
    SEARCH_INDEX_TABLE,
    TRANSCRIPT_STATUS_TRUNCATED,
    TRANSCRIPT_SUFFIX,
    MetaRecord,
    briefing_video_counts,
    frontmatter_values,
    is_skipped_meta,
    iter_meta_records,
    read_json_best_effort,
    read_text_best_effort,
    transcript_quality_flags_are_severe,
)
from timestamp_utils import is_youtube_deep_linkable, timestamp_tolerance, youtube_deep_link

log = logging.getLogger("video_intel.atlas_build")

#: The `generator` header value; ownership of an existing `atlas.json` (KTD8).
GENERATOR_NAME = "atlas_build.py"
GENERATOR_VERSION = "1"
SCHEMA_VERSION = 1
ATLAS_FILENAME = "atlas.json"
ATLAS_DIRNAME = "_atlas"
#: The only temp shape this generator writes (`atlas.json.<pid>.tmp`) and the
#: only thing the start-of-run sweep may remove.
TEMP_SUFFIX_PATTERN = re.compile(r"atlas\.json\.[0-9]+\.tmp")
#: A matching temp younger than this is left alone by the sweep: it may be a
#: concurrent build's live write rather than a crashed run's leftover.
STALE_TEMP_MIN_AGE_SECONDS = 3600

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
    "transcript_source",
    "synopsis",
    "deep_links",
    "concept_ids",
    "topics",
    "in_briefings",
    "briefing_count",
    "wiki",
    "aliases",
    "in_search_index",
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
WIKI_FRONTMATTER_BYTES = 8 * 1024
#: Read cap for an EXISTING `atlas.json` during the ownership check. The
#: catalog itself is megabytes (6.1 MB on the live corpus), far past the
#: per-meta default, and a cap that small would refuse every rebuild.
MAX_ATLAS_BYTES = 256 * 1024 * 1024
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
    if isinstance(status, str) and status in HEALTHY_TRANSCRIPT_STATUSES:
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


def transcript_source(meta: dict) -> str | None:
    """The writer's stored `transcript_source`, read from meta.json only (never the transcript)."""
    source = meta.get("transcript_source")
    return source if isinstance(source, str) and source else None


def deep_links(mindmap_text: str, url: str | None, duration: int | None) -> list[dict]:
    """R7: at most three `{label, seconds, url}` entries, one per branch.

    Branches are taken in document order; within a branch the first leaf that
    has a timestamp, is not past `duration + timestamp_tolerance(duration)`
    (when the duration is known) and whose seconds no earlier link used, wins.
    No qualifying leaf means no link: a timestamp is never invented.
    """
    if not is_youtube_deep_linkable(url):
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
        done_branches.add(branch)
        used_seconds.add(entry.seconds)
        # The URL passed the gate above, so the link builder cannot refuse it here.
        links.append(
            {"label": clean_text(branch), "seconds": entry.seconds, "url": youtube_deep_link(url, entry.seconds)}
        )
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
    seen: set[str] = set()
    malformed = False
    for entry in entries:
        cid = entry.get("concept_id") if isinstance(entry, dict) else None
        if isinstance(cid, str) and cid:
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
        else:
            malformed = True
    if malformed:
        log.warning("  %s: some `concepts` entries lack a string `concept_id`; they were ignored", path)
    return ids, malformed


def _read_mindmap(path: Path | None, skipped: list[str]) -> str:
    if path is None:
        return ""
    return read_text_best_effort(path, skipped, max_bytes=MAX_MINDMAP_BYTES) or ""


# ---------------------------------------------------------------------------
# Corpus-wide lookups, each read once
# ---------------------------------------------------------------------------


#: Warning-code stems for the two corpus-wide lookups. Every code a lookup
#: raises (`<code>_missing`, `<code>_malformed`) is built from one of these.
TOPICS_CODE = "topics"
TAXONOMY_CODE = "taxonomy"


def _read_lookup(path: Path, name: str, code: str, skipped: list[str], warnings: set[str]) -> dict | None:
    """The `name` key of a lookup JSON as a dict, or None with a `<code>_...` warning code."""
    before = len(skipped)
    data = read_json_best_effort(path, skipped, max_bytes=MAX_LOOKUP_JSON_BYTES)
    if data is None:
        warnings.add(f"{code}_malformed" if len(skipped) > before else f"{code}_missing")
        return None
    inner = data.get(name)
    if not isinstance(inner, dict):
        log.warning("%s: `%s` is not an object; ignoring it", path, name)
        warnings.add(f"{code}_malformed")
        return None
    return inner


def topics_by_video(corpus: Path, skipped: list[str], warnings: set[str]) -> dict[str, list[str]]:
    """video_id -> sorted topic slugs, inverted once from `topics.json`."""
    topics = _read_lookup(corpus / "topics.json", "topics", TOPICS_CODE, skipped, warnings)
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
        warnings.add(f"{TOPICS_CODE}_malformed")
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
        values = frontmatter_values(text, ("creator", "concept_id", "concept"))
        if values["creator"]:
            creators.add(values["creator"])
        for key in ("concept_id", "concept"):
            if values[key]:
                concepts.add(values[key])
    return creators, concepts


def concept_labels(corpus: Path, referenced: set[str], skipped: list[str], warnings: set[str]) -> dict[str, str]:
    """id -> preferred label for referenced ids; the label falls back to the id."""
    taxonomy = _read_lookup(corpus / "taxonomy.json", "concepts", TAXONOMY_CODE, skipped, warnings) or {}
    labels: dict[str, str] = {}
    for cid in sorted(referenced):
        entry = taxonomy.get(cid)
        label = entry.get("preferred_label") if isinstance(entry, dict) else None
        cleaned = clean_text(label) if isinstance(label, str) else ""
        labels[cid] = cleaned or clean_text(cid)
    return labels


#: `IndexRead.state` values. Only `read` carries ids; `absent` is a confirmed
#: "no index here" (no folder, or no table in it), `unknown` is every failure
#: to find out. The scan's recipe treats the two differently, because the
#: answer to `absent` is a paid full `index` run.
INDEX_READ = "read"
INDEX_ABSENT = "absent"
INDEX_UNKNOWN = "unknown"


@dataclass(frozen=True)
class IndexRead:
    state: str
    video_ids: frozenset[str] | None = None


def read_search_index(index_dir: Path | None) -> IndexRead:
    """Which video ids have at least one row in the search index.

    Read-only: one `video_id` column projection, no embedding call, no write.
    `lancedb` is imported here and never at module level, so `import
    atlas_build` stays stdlib-only (#152). A projection that comes back short
    is `unknown`, never a partial set: a partial read would mark indexed
    videos as missing, the worst answer the facet can give.
    """
    if index_dir is None or not index_dir.is_dir():
        return IndexRead(INDEX_ABSENT)
    try:
        import lancedb
    except ImportError:
        log.warning("lancedb is not installed; the catalog leaves out the in-search-index facet")
        return IndexRead(INDEX_UNKNOWN)
    try:
        db = lancedb.connect(str(index_dir))
        if SEARCH_INDEX_TABLE not in db.list_tables().tables:
            return IndexRead(INDEX_ABSENT)
        table = db.open_table(SEARCH_INDEX_TABLE)
        arrow = table.search().select(["video_id"]).limit(0).to_arrow()
        expected = table.count_rows()
    except Exception as exc:  # any failure of a third-party read means "unknown", never a crash
        log.warning("cannot read the search index at %s (%s); the catalog leaves out that facet", index_dir, exc)
        return IndexRead(INDEX_UNKNOWN)
    if arrow.num_rows != expected:
        log.warning(
            "the search index returned %d of %d rows; the catalog leaves out the in-search-index facet",
            arrow.num_rows,
            expected,
        )
        return IndexRead(INDEX_UNKNOWN)
    ids = frozenset(v for v in arrow.column("video_id").to_pylist() if isinstance(v, str) and v)
    return IndexRead(INDEX_READ, ids)


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


def _row_sort_key(row: dict) -> tuple[str, str]:
    """The (`published`, `video_id`) row order; the builder sorts by it and the serializer checks it."""
    return (row.get("published") or "", row.get("video_id") or "")


def build_rows(corpus: Path, indexed: frozenset[str] | None = None) -> BuildResult:
    """Build every catalog row, the concept lookup and the summary. Writes nothing.

    `indexed` is `read_search_index(...).video_ids`: each row's
    `in_search_index` is membership in it, or None for every row when the
    index was not read.
    """
    walk = iter_meta_records(corpus)
    skipped = list(walk.skipped_files)
    warnings: set[str] = set()
    topics = topics_by_video(corpus, skipped, warnings)
    briefing_counts = briefing_video_counts(corpus / "_briefings", skipped)
    wiki_creators, wiki_concept_ids = wiki_page_keys(corpus, skipped)

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
            "transcript_source": transcript_source(meta),
            "synopsis": [h for h in (clean_text(h) for h in mindmap_headings(mindmap)) if h],
            "deep_links": deep_links(mindmap, url, duration),
            "concept_ids": ids,
            "topics": topics.get(record.video_id, []),
            "in_briefings": count > 0,
            "briefing_count": count,
            "wiki": {
                "creator": record.channel if record.channel in wiki_creators else None,
                "concepts": sorted(cid for cid in ids if cid in wiki_concept_ids),
            },
            "aliases": [clean_text(a) for a in record.aliases],
            "in_search_index": None if indexed is None else record.video_id in indexed,
        }
        rows.append(row)
        stamp = _parse_processed(meta.get("processed"))
        if stamp is not None:
            processed.append(stamp)
    rows.sort(key=_row_sort_key)

    if walk.unlistable_channels:
        warnings.add("channel_unlistable")
    if walk.unlistable_excluded:
        warnings.add("excluded_unlistable")
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
        "unlistable_channels": walk.unlistable_channels,
        "unlistable_excluded": walk.unlistable_excluded,
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


# ---------------------------------------------------------------------------
# Document, serialization and the single-file writer
# ---------------------------------------------------------------------------


def build_header(corpus: Path, result: BuildResult, generated_at: str | None = None) -> dict:
    """The whole `atlas.json` document, keys in schema order (R12).

    `generated_at` (KTD4): the caller's string verbatim when given, else the
    newest parseable `processed` (UTC), else the newest `published` date, else
    None. Never the wall clock: a clock stamp would make every unchanged
    rebuild a new file on the Drive mount.
    """
    if generated_at is None:
        generated_at = result.max_processed or result.max_published
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "generated_from": corpus.name,
        "generated_at": generated_at,
        "summary": result.summary,
        "concepts": dict(sorted(result.concepts.items())),
        "rows": result.rows,
    }


def _nfc(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {_nfc(k): _nfc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_nfc(v) for v in value]
    return value


def serialize_atlas(document: dict) -> bytes:
    """UTF-8 bytes of `document`: NFC text, no NaN, LF only, one trailing newline.

    Rows must already be sorted by (`published`, `video_id`); an unsorted list
    is refused rather than silently re-sorted, because it means the builder's
    order contract broke. A NaN or infinity raises ValueError here, before any
    file is touched.
    """
    keys = [_row_sort_key(row) for row in document.get("rows", [])]
    if keys != sorted(keys):
        raise ValueError("atlas rows are not sorted by (published, video_id)")
    text = json.dumps(_nfc(document), ensure_ascii=False, allow_nan=False, indent=2)
    return (text + "\n").encode("utf-8")


def check_ownership(path: Path) -> None:
    """Refuse (SystemExit) unless `path` is absent or stamped by this generator.

    Fail-closed, as `wiki_concepts.write_pages` is: an unreadable file, a file
    that is not a JSON object, and one without a `generator` key are all
    refused, never overwritten.
    """
    unusable: list[str] = []
    data = read_json_best_effort(path, unusable, max_bytes=MAX_ATLAS_BYTES)
    if data is None and not unusable:
        return  # absent: nothing to own
    if data is None:
        reason = "unreadable or not a JSON object"
    else:
        owner = data.get("generator")
        if owner == GENERATOR_NAME:
            return
        reason = f"generated by '{owner}'" if owner else "not stamped by any generator (no `generator` key)"
    raise SystemExit(
        f"refusing to overwrite {path}: it is {reason}, not {GENERATOR_NAME}. "
        "Move the file aside, or point --out at a directory not shared with another generator's output."
    )


def _file_age_seconds(path: Path) -> float:
    """Seconds since `path` was last modified.

    The module's ONE clock read, and it decides only whether a leftover temp
    is old enough to sweep; it never reaches the output, so `generated_at`
    stays clock-free (`TestDeterminism::test_module_never_reads_the_wall_clock`
    exempts exactly this function).
    """
    return time.time() - path.stat().st_mtime


def sweep_stale_temps(out_dir: Path) -> None:
    """Remove leftover `atlas.json.<pid>.tmp` files from a crashed run; nothing else.

    A temp younger than `STALE_TEMP_MIN_AGE_SECONDS` is left alone: it may be
    another build's live write, and deleting it would make that build's
    `os.replace` fail. Every listing, stat and unlink failure is a WARNING,
    never a crash: a temp the sweep cannot remove costs disk, not the build.
    """
    try:
        with os.scandir(out_dir) as entries:
            temps = sorted(
                Path(entry.path) for entry in entries if TEMP_SUFFIX_PATTERN.fullmatch(entry.name) and entry.is_file()
            )
    except (FileNotFoundError, NotADirectoryError):
        return
    except OSError as exc:
        log.warning("cannot list %s to sweep leftover temps: %s", out_dir, exc)
        return
    for path in temps:
        try:
            if _file_age_seconds(path) < STALE_TEMP_MIN_AGE_SECONDS:
                log.info("leaving a recent temp alone (another build may own it): %s", path)
                continue
            log.info("removing a leftover temp from an earlier run: %s", path)
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            log.warning("cannot remove leftover temp %s: %s", path, exc)


def write_atlas(out_dir: Path, document: bytes) -> Path:
    """Write serialized `document` to `out_dir/atlas.json` atomically; return that path.

    Ownership is checked first (KTD8). The bytes go to `atlas.json.<pid>.tmp`
    in the same directory and are moved into place with one `os.replace`; the
    temp is removed in `finally` whenever it survives. `os.replace` on a
    cloud-synced mount is best effort (ADR-0016).
    """
    target = out_dir / ATLAS_FILENAME
    check_ownership(target)
    out_dir.mkdir(parents=True, exist_ok=True)
    temp = out_dir / f"{ATLAS_FILENAME}.{os.getpid()}.tmp"
    try:
        temp.write_bytes(document)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the corpus catalog (_atlas/atlas.json) from a video-intel corpus, read-only (issue #260)."
    )
    parser.add_argument("--corpus", type=Path, required=True, help="Path to the corpus (output_dir), read-only")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"Output folder for {ATLAS_FILENAME} (default: <corpus>/{ATLAS_DIRNAME})",
    )
    parser.add_argument(
        "--index-dir",
        type=Path,
        default=None,
        help=f"Search index folder (vector_db_dir) read for the in-search-index facet "
        f"(default: <corpus>/{SEARCH_INDEX_DIRNAME}; the facet is left out when no index is found there)",
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="Value for the generated_at header, emitted verbatim "
        "(default: the newest processed stamp, else the newest published date)",
    )
    return parser


def summary_line(out_dir: Path, corpus: Path, summary: dict) -> str:
    """The one stdout line: counts only, never a channel, topic, title or path.

    The location is `<corpus name>/<folder name>` when the output folder sits
    inside the corpus, and only the folder's own name when `--out` points
    elsewhere: prefixing the corpus name there would claim a location the file
    is not in.
    """
    rows = summary["rows"]
    share = f"{100 * summary['unthreaded'] / rows:.1f}%" if rows else "n/a"
    where = f"{corpus.name}/{out_dir.name}" if out_dir.is_relative_to(corpus) else out_dir.name
    return (
        f"atlas_build: wrote {ATLAS_FILENAME} to {where} "
        f"(metas={summary['metas']} rows={rows} duplicates={summary['duplicates']} "
        f"skipped={summary['skipped']} no_id={summary['no_id']} excluded_metas={summary['excluded_metas']} "
        f"unthreaded={summary['unthreaded']} ({share}) warnings={len(summary['warnings'])})"
    )


#: Exit code when the walk could not list a channel folder: nothing is written,
#: so the previous catalog survives.
EXIT_UNLISTABLE = 2


def refused_line(summary: dict) -> str:
    """The one stdout line when an unlistable channel stops the write: counts only."""
    n = summary["unlistable_channels"]
    return (
        f"atlas_build: did not write {ATLAS_FILENAME}: {n} channel folder(s) could not be listed "
        f"(metas={summary['metas']} rows={summary['rows']} duplicates={summary['duplicates']} "
        f"skipped={summary['skipped']} no_id={summary['no_id']} excluded_metas={summary['excluded_metas']} "
        f"unlistable={n})"
    )


def build_catalog(
    corpus: Path, out_dir: Path, index_dir: Path | None, generated_at: str | None = None
) -> tuple[int, str]:
    """Write `out_dir/atlas.json` from `corpus`; return (exit code, the index read's state).

    The one build behind both this script's CLI and the end of `video_intel
    scan`, which reads the state to choose its index recipe. Prints the one
    counts-only stdout line.
    """
    sweep_stale_temps(out_dir)
    # Refuse before the walk too: a foreign file should not cost a full corpus read.
    check_ownership(out_dir / ATLAS_FILENAME)
    index = read_search_index(index_dir)
    result = build_rows(corpus, index.video_ids)
    if result.summary["unlistable_channels"]:
        # A channel the walk could not list is absent from every counter, so
        # the partition still balances; publishing would ship a catalog with
        # that channel silently missing. Keep the previous file instead.
        print(refused_line(result.summary))
        return EXIT_UNLISTABLE, index.state
    document = serialize_atlas(build_header(corpus, result, generated_at))
    write_atlas(out_dir, document)
    print(summary_line(out_dir, corpus, result.summary))
    return 0, index.state


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    corpus = args.corpus.resolve()
    if not corpus.is_dir():
        sys.exit(f"corpus not found: {corpus}")
    out_dir = args.out.resolve() if args.out else corpus / ATLAS_DIRNAME
    index_dir = args.index_dir.expanduser().resolve() if args.index_dir else corpus / SEARCH_INDEX_DIRNAME
    code, _ = build_catalog(corpus, out_dir, index_dir, args.generated_at)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
