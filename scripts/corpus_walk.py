"""Stdlib-only reader primitives shared by the curate stack and the catalog (issue #260).

Every decision a corpus READER makes - which folders are channels, which
sidecar suffixes belong to a video, whether a meta file is usable at all,
whether a video id can serve as a join key, whether a transcript's persisted
quality flags are severe, which videos a briefing has already surfaced -
lives here once, and `video_intel.py` re-exports each name so every existing
caller and test keeps resolving it from the old module (the issue #152
pattern `timestamp_utils.py` set). Two homes for one decision drift apart;
one home with N consumers cannot.

Import weight is part of the contract. This module imports only the standard
library at module level, so a standalone read-only script (the catalog
generator, `wiki_concepts.py`) can use it without dragging in the curate
stack. PyYAML is imported lazily, inside `parse_front_matter` only. Do not
import `video_intel`, `intel_graph`, `wiki_concepts` or any third-party
package at module level here. Test contract:
`tests/test_timestamp_utils.py::TestStandaloneScriptImportIsolation`.

Reading never writes. `read_json_best_effort` skips and counts a file it
cannot use; it never quarantines or rewrites it. The writer-side reader that
DOES quarantine, `video_intel._read_meta_best_effort`, stays in
`video_intel.py` on purpose (issue #124).
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("video_intel.corpus_walk")

# ---------------------------------------------------------------------------
# Corpus layout
# ---------------------------------------------------------------------------

META_SUFFIX = ".meta.json"
CONCEPTS_SUFFIX = ".concepts.json"
MINDMAP_SUFFIX = ".mindmap.md"
TRANSCRIPT_SUFFIX = ".transcript.md"

#: Per-file read cap for a meta or concepts JSON. A real meta is a few KB; a
#: file this large is not one, and reading it whole would only cost memory.
DEFAULT_MAX_JSON_BYTES = 4 * 1024 * 1024


def _is_channel_name(name: str) -> bool:
    """The name half of `is_channel_dir`: dot- and underscore-prefixed names are never channels."""
    return not name.startswith(".") and not name.startswith("_")


def is_channel_dir(path: Path) -> bool:
    """True for a directory that holds a channel's artifacts.

    Dot- and underscore-prefixed folders are never channels: `_briefings`,
    `_headlines`, `_wiki`, `_standalone` and `.lancedb` all sit beside the
    channel folders and must never be walked as one.
    """
    return path.is_dir() and _is_channel_name(path.name)


def read_text_best_effort(path: Path, skipped: list[str], *, max_bytes: int, errors: str = "strict") -> str | None:
    """Read UTF-8 text from ``path``; None when it is absent or unusable.

    An ABSENT file is normal (no mindmap yet) and is neither counted nor
    logged. A file that exists but cannot be used - unreadable, larger than
    ``max_bytes``, or not UTF-8 under ``errors`` - is appended to the
    caller-owned ``skipped`` list and logged once at WARNING with its path.

    Bytes first, then decode (issue #124): `read_text` would fold a content
    problem into the call that should only fail at the I/O layer, and
    `UnicodeDecodeError` subclasses ValueError, not OSError, so a file torn
    mid-multibyte-character (the normal shape of a truncated write on a corpus
    carrying Cyrillic/BCS titles) needs the `(ValueError, OSError)` split.

    Never writes, never quarantines: a reader has no business moving a file
    another process may be about to repair.
    """
    try:
        with path.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return _skip(path, skipped, f"unreadable ({exc})")
    if len(raw) > max_bytes:
        return _skip(path, skipped, f"larger than {max_bytes} bytes")
    try:
        return raw.decode("utf-8", errors=errors)
    except ValueError as exc:
        return _skip(path, skipped, f"not UTF-8 ({exc})")


def read_json_best_effort(path: Path, skipped: list[str], *, max_bytes: int = DEFAULT_MAX_JSON_BYTES) -> dict | None:
    """Read a JSON object from ``path``; None when it is absent or unusable.

    An ABSENT file is normal (no `concepts.json` yet) and is neither counted
    nor logged. A file that exists but cannot be used - unreadable, larger
    than ``max_bytes``, not UTF-8, not JSON, or JSON whose top level is not an
    object - is appended to the caller-owned ``skipped`` list and logged once
    at WARNING with its path. The read and the decode are
    `read_text_best_effort`'s, so the issue #124 bytes-first rule lives in one
    place.

    Never writes, never quarantines: a reader has no business moving a file
    another process may be about to repair.
    """
    text = read_text_best_effort(path, skipped, max_bytes=max_bytes)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError as exc:
        return _skip(path, skipped, f"not JSON ({exc})")
    if not isinstance(data, dict):
        return _skip(path, skipped, f"top level is a {type(data).__name__}, not an object")
    return data


def _skip(path: Path, skipped: list[str], reason: str) -> None:
    log.warning("skipping %s: %s", path, reason)
    skipped.append(str(path))
    return None


def usable_video_id(value: Any) -> bool:
    """True when ``value`` can serve as the join key between corpus artifacts.

    A YouTube video id is always a string; anything else on disk is malformed
    data, and coercing it with ``str()`` would silently merge a hypothetical
    ``123`` and ``"123"`` into one identity. Callers skip-with-WARNING instead.
    """
    return isinstance(value, str) and bool(value.strip())


#: The four per-video artifact suffixes a channel's sibling index knows about.
#: Matched by exact `endswith`, so `.meta.corrupt.json`, the `.transcript.raw*`
#: sidecars and translate-bcs `.srt` files are never mistaken for one of them.
SIBLING_SUFFIXES = (META_SUFFIX, CONCEPTS_SUFFIX, MINDMAP_SUFFIX, TRANSCRIPT_SUFFIX)

#: The suffixes the duplicate-id winner rule counts (mindmap, concepts), read by
#: `_sibling_artifact_count`, the ONE definition of that rule. A transcript or
#: any other sidecar never counts, so a transcript-plus-mindmap record cannot
#: beat a mindmap-plus-concepts one and silently change which title briefings show.
WINNER_ARTIFACT_SUFFIXES = (MINDMAP_SUFFIX, CONCEPTS_SUFFIX)


def channel_sibling_index(channel_dir: Path) -> dict[str, dict[str, Path]]:
    """prefix -> {suffix -> path} for one channel folder, from ONE `iterdir()`.

    On a cloud-mounted corpus a directory listing is the expensive call, so a
    channel is listed once and every video's siblings come out of that one
    listing. Matching is literal (`endswith` on the four known suffixes, the
    prefix is the filename minus the suffix), never a glob: a bracketed title
    like `2026-01-01-[live]-title` is glob syntax and would lose its siblings
    (the `wiki_concepts.sibling_artifacts` lesson, issue #150). Only regular
    files are indexed. `os.scandir` answers `is_file()` from the listing
    itself (the directory entry's attributes on Windows, `d_type` on Linux),
    so the file-type check costs no stat per artifact on the Drive mount.
    """
    index: dict[str, dict[str, Path]] = {}
    with os.scandir(channel_dir) as entries:
        for entry in entries:
            name = entry.name
            for suffix in SIBLING_SUFFIXES:
                if name.endswith(suffix) and len(name) > len(suffix):
                    if entry.is_file():
                        index.setdefault(name[: -len(suffix)], {})[suffix] = Path(entry.path)
                    break
    return index


def _sibling_artifact_count(siblings: dict[str, Path]) -> int:
    """The duplicate-id winner rule: how many `WINNER_ARTIFACT_SUFFIXES` siblings a record has."""
    return sum(1 for suffix in WINNER_ARTIFACT_SUFFIXES if suffix in siblings)


@dataclass
class MetaRecord:
    """One readable meta with a usable `video_id`, plus its sibling artifacts.

    On a winner, `losers` holds the other records that share its `video_id`
    (title rotation, or one id filed under two channel folders), `aliases` the
    losing titles and `channels` every channel the id was found under.
    """

    video_id: str
    prefix: str
    channel: str
    channel_dir: Path
    meta: dict
    siblings: dict[str, Path]
    losers: list[MetaRecord] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    channels: list[str] = field(default_factory=list)


@dataclass
class WalkResult:
    """Winners plus counters that partition the eligible metas.

    Invariant: ``metas == len(records) + duplicates + skipped + no_id``.
    ``excluded_metas`` counts metas under `.`/`_` top-level folders, which are
    never walked and so sit outside that partition.

    ``unlistable_channels`` counts channel folders whose listing raised
    OSError: their metas are in NO counter, so the partition above still
    balances while a whole channel is missing. A caller that publishes the
    walk must treat a non-zero value as a failed walk, not a smaller corpus.
    ``unlistable_excluded`` is the same for excluded folders, whose metas are
    then missing from ``excluded_metas``. Folder names go to the WARNING log
    only, never into a counter or a result field.
    """

    records: list[MetaRecord] = field(default_factory=list)
    metas: int = 0
    duplicates: int = 0
    skipped: int = 0
    no_id: int = 0
    excluded_metas: int = 0
    unlistable_channels: int = 0
    unlistable_excluded: int = 0
    skipped_files: list[str] = field(default_factory=list)


def _record_channel(meta: dict, channel_dir: Path, meta_name: str) -> str:
    channel = meta.get("channel")
    if isinstance(channel, str) and channel:
        return channel
    # `channels` is sorted and rolled up per topic, so a non-string breaks the
    # ordering downstream. The folder the meta actually lives in is the
    # accurate fallback, not an invented label.
    log.warning(
        "  %s: `channel` is %s, not a non-empty string; using the folder name %s",
        meta_name,
        "missing" if channel is None else f"a {type(channel).__name__}",
        channel_dir.name,
    )
    return channel_dir.name


def _count_excluded_metas(folder: Path) -> int | None:
    """Metas directly inside an excluded top-level folder, counted, never read; None when unlistable."""
    try:
        with os.scandir(folder) as entries:
            return sum(1 for entry in entries if entry.name.endswith(META_SUFFIX) and entry.is_file())
    except OSError as exc:
        log.warning("cannot list excluded folder %s: %s", folder, exc)
        return None


def _meta_filename_key(prefix: str) -> str:
    """Sort key for a prefix: its meta FILENAME, never the bare prefix.

    The pre-#260 walker took the first of `sorted(glob("*.meta.json"))`, and
    the two orders disagree: `x-foo-2.meta.json` sorts before `x-foo.meta.json`
    ('-' < '.'), while the bare prefix `x-foo` sorts before `x-foo-2`. Sorting
    on the prefix flipped which record won an equal-count tie, and so which
    title briefings show.
    """
    return prefix + META_SUFFIX


def iter_meta_records(corpus: Path) -> WalkResult:
    """Walk every channel folder once and return one record per `video_id`.

    Channel folders (`is_channel_dir`) are walked in sorted name order and
    each folder's metas in sorted meta FILENAME order (`_meta_filename_key`). A meta `read_json_best_effort`
    rejects counts as `skipped`; a JSON object whose `video_id` fails
    `usable_video_id` counts as `no_id` with a WARNING naming the file (never
    `str()`-coerced: `123` must not merge with a real `"123"`). Records that
    share an id are grouped: the winner has strictly more mindmap-plus-concepts
    siblings, and a tie keeps the FIRST record in walk order; every other
    record counts as a duplicate and hangs off the winner. Reading never
    writes. A corpus that does not exist yields an empty result.

    The corpus root is listed once with `os.scandir`, so each top-level
    entry's directory check comes from the listing rather than a stat per
    folder on the Drive mount.
    """
    result = WalkResult()
    try:
        with os.scandir(corpus) as entries:
            folders = sorted((entry.name, Path(entry.path)) for entry in entries if entry.is_dir())
    except (FileNotFoundError, NotADirectoryError):
        return result
    by_id: dict[str, MetaRecord] = {}
    for name, folder in folders:
        if not _is_channel_name(name):
            excluded = _count_excluded_metas(folder)
            if excluded is None:
                result.unlistable_excluded += 1
            else:
                result.excluded_metas += excluded
            continue
        try:
            index = channel_sibling_index(folder)
        except OSError as exc:
            # Counted, not just logged: a skipped channel leaves the partition
            # balanced, so the count is the only signal a caller can act on.
            log.warning("cannot list channel folder %s: %s", folder, exc)
            result.unlistable_channels += 1
            continue
        for prefix in sorted(index, key=_meta_filename_key):
            siblings = index[prefix]
            meta_path = siblings.get(META_SUFFIX)
            if meta_path is None:
                continue
            result.metas += 1
            meta = read_json_best_effort(meta_path, result.skipped_files)
            if meta is None:
                result.skipped += 1
                continue
            video_id = meta.get("video_id")
            if not usable_video_id(video_id):
                log.warning(
                    "  %s: `video_id` is %s, not a non-empty string; skipping this meta",
                    meta_path.name,
                    "missing" if video_id is None else f"a {type(video_id).__name__}",
                )
                result.no_id += 1
                continue
            record = MetaRecord(
                video_id=video_id,
                prefix=prefix,
                channel=_record_channel(meta, folder, meta_path.name),
                channel_dir=folder,
                meta=meta,
                siblings=siblings,
            )
            existing = by_id.get(video_id)
            if existing is None:
                by_id[video_id] = record
                continue
            result.duplicates += 1
            if _sibling_artifact_count(record.siblings) > _sibling_artifact_count(existing.siblings):
                record.losers = [existing, *existing.losers]
                existing.losers = []
                by_id[video_id] = record
            else:
                existing.losers.append(record)
    for winner in by_id.values():
        # Losers in walk order, so aliases follow the sorted (folder, filename) order.
        winner.losers.sort(key=lambda r: (r.channel_dir.name, _meta_filename_key(r.prefix)))
        own_title = winner.meta.get("title")
        aliases: list[str] = []
        for loser in winner.losers:
            title = loser.meta.get("title")
            if isinstance(title, str) and title and title != own_title and title not in aliases:
                aliases.append(title)
        winner.aliases = aliases
        winner.channels = sorted({winner.channel, *(loser.channel for loser in winner.losers)})
    result.records = sorted(by_id.values(), key=lambda r: (r.channel, _meta_filename_key(r.prefix), r.channel_dir.name))
    return result


# ---------------------------------------------------------------------------
# Transcript status and quality flags (as persisted in meta.json)
# ---------------------------------------------------------------------------

# Issue #54 / review C1: transcript writers populate `transcript_status` with
# different healthy literals depending on path: the chunked merge writer
# writes "ok", the single-call success writer writes "complete", and the
# salvage writer writes "partial". Any non-healthy value triggers the
# partial-source mindmap marker. Keep this set in sync with the writers.
HEALTHY_TRANSCRIPT_STATUSES = {"ok", "complete"}
#: The name `video_intel` has always used; kept bound to the same object.
_HEALTHY_TRANSCRIPT_STATUSES = HEALTHY_TRANSCRIPT_STATUSES

#: meta.json transcript_status for a transcript salvaged from a response that
#: hit the output cap. Distinct from the generic "partial" on purpose: a
#: salvage-from-malformed-JSON and a salvage-from-output-truncation were
#: indistinguishable in meta.json, so there was no way to sweep the corpus for
#: the videos that a chunked re-run would actually fix (issue #128).
TRANSCRIPT_STATUS_TRUNCATED = "truncated_output"

#: The severe half of each severe/mild quality-flag pair. The thresholds that
#: decide them, and the mild names, stay with the assessor in video_intel.py.
QUALITY_FLAG_MONOLITHIC_SEVERE = "monolithic_severe"
QUALITY_FLAG_BLIND_GAP_SEVERE = "blind_gap_severe"
QUALITY_FLAG_BACKWARD_JUMP_SEVERE = "backward_jump_severe"
#: Issue #158: a wholesale cross-chunk misclassification of a chunk's stamps.
QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE = "chunk_window_mismatch_severe"

#: Flags that change transcript_status to "partial", count as a pipeline gap
#: (exit EXIT_PARTIAL), and make resolve_mindmap_source's "auto" branch treat
#: the transcript as unavailable. Membership-tested against a PERSISTED
#: transcript_quality_flags list, not recomputed - see
#: transcript_quality_flags_are_severe(). `video_intel._SEVERE_QUALITY_FLAGS`
#: is bound to this same object.
SEVERE_QUALITY_FLAGS = frozenset(
    {
        QUALITY_FLAG_MONOLITHIC_SEVERE,
        QUALITY_FLAG_BLIND_GAP_SEVERE,
        QUALITY_FLAG_BACKWARD_JUMP_SEVERE,
        QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE,
    }
)


#: The container shapes `transcript_quality_flags_are_severe` judges. A list is
#: the persisted shape; the chunked writer passes the `set` it accumulates into
#: before sorting, so a set (and its tuple/frozenset siblings) must stay judged.
_FLAG_COLLECTION_TYPES = (list, tuple, set, frozenset)


def transcript_quality_flags_are_severe(flags: list | set | None) -> bool:
    """Whether a PERSISTED transcript_quality_flags list contains a severe flag.

    The single place every consumer (the writers computing transcript_status,
    resolve_mindmap_source's containment check, the process orchestrators'
    exit-code gate, dedupe's canonical selection) re-derives severity from
    what actually landed in meta.json, rather than each hand-rolling its own
    membership test against SEVERE_QUALITY_FLAGS. A missing or empty list,
    or a value that is not a list (or the set the chunked writer accumulates
    into), is not severe.

    Issue #159 dual-review: the membership test filters to string entries
    (`if isinstance(f, str)`) BEFORE evaluating `f in SEVERE_QUALITY_FLAGS`,
    for two reasons, not one. First, order-independence - `any()` short-
    circuits on the first entry that satisfies the expression, so without
    the filter a malformed list like `[{"x": 1}, "monolithic_severe"]` used
    to raise TypeError on the unhashable dict *before* `any()` ever reached
    the genuine severe string a few entries later, while the reordered list
    `["monolithic_severe", {"x": 1}]` returned True correctly - the verdict
    depended on where the bad entry happened to sit, in either direction.
    Second, safety - a non-string entry (dict, list, custom object) can be
    unhashable, and `in` on a frozenset requires hashing its left operand;
    filtering by `isinstance(f, str)` first means the `in` test only ever
    runs on a value that is guaranteed hashable, so no entry shape can raise
    here. A non-string sibling anywhere in the list can now never mask - or
    fake - a genuine severe flag, and malformed entries degrade to being
    ignored rather than crashing the caller.

    Issue #260 review: a non-collection `flags` (an int, a bare string, a
    dict) returns False before iteration. An int survived the `if not flags`
    guard and raised TypeError on iteration, so one malformed meta aborted the
    whole catalog build; a dict was iterated by its KEYS, so
    `{"monolithic_severe": true}` faked a severe verdict.
    """
    if not flags or not isinstance(flags, _FLAG_COLLECTION_TYPES):
        return False
    return any(f in SEVERE_QUALITY_FLAGS for f in flags if isinstance(f, str))


def is_skipped_meta(meta: dict, mode: str | None = None) -> bool:
    """Decide whether a video should be skipped for a given processing mode.

    Resolution order (issue #42):
      1. If meta has a `skip_modes` list, check membership against `mode`.
         skip_modes wins outright; legacy `skip` is ignored when both exist.
      2. Else if `skip == True` (legacy boolean), treat as full-skip
         (every mode is skipped).
      3. Else False.

    Passing mode=None preserves the pre-issue-42 "any skip" semantics so
    existing call sites that do not care about a specific mode (e.g. dry-run
    listing, dedupe candidates) keep behaving the same.
    """
    skip_modes = meta.get("skip_modes")
    if isinstance(skip_modes, list):
        if mode is None:
            return len(skip_modes) > 0
        return mode in skip_modes
    return meta.get("skip") is True


# ---------------------------------------------------------------------------
# Front matter and briefings
# ---------------------------------------------------------------------------


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a markdown doc into (front_matter_dict, body).

    Front matter is a leading YAML block delimited by '---' lines. Returns
    ({}, text) when no well-formed front matter is present and never raises on
    malformed YAML - a hand-edited briefing must not crash the catch-up scan.

    PyYAML is imported here, not at module level, so importing this module
    stays stdlib-only.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return {}, text
    import yaml

    try:
        data = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, "\n".join(lines[closing + 1 :])


def frontmatter_value(text: str, key: str) -> str | None:
    """Best-effort read of one scalar `key:` value from a page's frontmatter.

    Generalized from `wiki_concepts._frontmatter_generator` (issue #150
    FIX1c). A line-level scan, not a YAML parse, so it needs no dependency and
    cannot raise. Returns None when there is no frontmatter block, the block
    is never closed, the key is absent, or its value is empty. The match is
    on `key:` exactly, so `concept` never matches a `concept_id:` line.
    """
    return _first_frontmatter_value(_frontmatter_lines(text), key)


def frontmatter_values(text: str, keys: tuple[str, ...]) -> dict[str, str | None]:
    """`frontmatter_value` for several keys, finding the frontmatter block once."""
    lines = _frontmatter_lines(text)
    return {key: _first_frontmatter_value(lines, key) for key in keys}


def _frontmatter_lines(text: str) -> list[str]:
    """The stripped lines of a page's frontmatter block; empty when there is none or it is never closed."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []
    return [line.strip() for line in text[3:end].splitlines()]


def _first_frontmatter_value(lines: list[str], key: str) -> str | None:
    prefix = f"{key}:"
    for line in lines:
        if line.startswith(prefix):
            return line[len(prefix) :].strip() or None
    return None


#: Per-file read cap for a briefing. Front matter can carry a long `video_ids`
#: list (a catch-up briefing lists dozens), so this sits well above a meta's.
MAX_BRIEFING_BYTES = 1024 * 1024


def briefing_video_counts(briefings_dir: Path, skipped: list[str] | None = None) -> Counter:
    """How many briefing files under _briefings/**/*.md list each video id.

    A video listed twice in one briefing counts once for that file. Missing
    dir -> empty Counter. Recurses into subfolders (e.g. _briefings/sales/) so
    topic-based organization of the briefings dir doesn't silently un-see
    videos. Truthy non-string ids are coerced with `str()`, as
    `load_seen_video_ids` always has.

    Each briefing is read through `read_text_best_effort`, so an unreadable,
    oversized or non-UTF-8 file is skipped with a WARNING and appended to the
    caller-owned ``skipped`` list (a throwaway list when omitted) instead of
    raising out of the catalog build. A bare `read_text` caught only OSError,
    and `UnicodeDecodeError` is a ValueError.
    """
    counts: Counter = Counter()
    if skipped is None:
        skipped = []
    if not briefings_dir.is_dir():
        return counts
    for md in sorted(briefings_dir.rglob("*.md")):
        text = read_text_best_effort(md, skipped, max_bytes=MAX_BRIEFING_BYTES)
        if text is None:
            continue
        front_matter, _ = parse_front_matter(text)
        ids = front_matter.get("video_ids") or []
        if isinstance(ids, list):
            counts.update({str(v) for v in ids if v})
    return counts


def load_seen_video_ids(briefings_dir: Path) -> set[str]:
    """Union of `video_ids` across every _briefings/**/*.md front matter.

    This is the strict set-difference basis for "unseen": a video that has
    appeared in ANY briefing is considered surfaced. Missing dir -> empty set.
    The key set of `briefing_video_counts`, so the two can never disagree
    about which videos a briefing has surfaced.
    """
    return set(briefing_video_counts(briefings_dir))
