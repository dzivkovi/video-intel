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
from collections import Counter
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


def is_channel_dir(path: Path) -> bool:
    """True for a directory that holds a channel's artifacts.

    Dot- and underscore-prefixed folders are never channels: `_briefings`,
    `_headlines`, `_wiki`, `_standalone` and `.lancedb` all sit beside the
    channel folders and must never be walked as one.
    """
    return path.is_dir() and not path.name.startswith(".") and not path.name.startswith("_")


def read_json_best_effort(path: Path, skipped: list[str], *, max_bytes: int = DEFAULT_MAX_JSON_BYTES) -> dict | None:
    """Read a JSON object from ``path``; None when it is absent or unusable.

    An ABSENT file is normal (no `concepts.json` yet) and is neither counted
    nor logged. A file that exists but cannot be used - unreadable, larger
    than ``max_bytes``, not UTF-8, not JSON, or JSON whose top level is not an
    object - is appended to the caller-owned ``skipped`` list and logged once
    at WARNING with its path.

    Bytes first, then decode and parse (issue #124): `read_text` would fold a
    content problem into the call that should only fail at the I/O layer, and
    `UnicodeDecodeError` subclasses ValueError, not OSError, so a meta torn
    mid-multibyte-character (the normal shape of a truncated write on a corpus
    carrying Cyrillic/BCS titles) needs the `(ValueError, OSError)` catch.

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
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        return _skip(path, skipped, f"not UTF-8 JSON ({exc})")
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


def artifact_count(record: dict) -> int:
    """How many of a video's optional artifacts (mindmap, concepts) are present."""
    return sum(1 for key in ("mindmap_path", "concepts_path") if record.get(key))


#: The name `video_intel` has always used; kept bound to the same function.
_artifact_count = artifact_count

# ---------------------------------------------------------------------------
# Transcript status and quality flags (as persisted in meta.json)
# ---------------------------------------------------------------------------

# Issue #54 / review C1: transcript writers populate `transcript_status` with
# different healthy literals depending on path: chunked merge writes "ok"
# (scripts/video_intel.py:1542), single-call success writes "complete"
# (:2419), salvage writes "partial" (:2449). Any non-healthy value triggers
# the partial-source mindmap marker. Keep this set in sync with the writers.
_HEALTHY_TRANSCRIPT_STATUSES = {"ok", "complete"}

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


def transcript_quality_flags_are_severe(flags: list | None) -> bool:
    """Whether a PERSISTED transcript_quality_flags list contains a severe flag.

    The single place every consumer (the writers computing transcript_status,
    resolve_mindmap_source's containment check, the process orchestrators'
    exit-code gate, dedupe's canonical selection) re-derives severity from
    what actually landed in meta.json, rather than each hand-rolling its own
    membership test against SEVERE_QUALITY_FLAGS. A missing or empty list is
    not severe.

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
    ignored rather than crashing the caller. A non-list `flags` argument
    (e.g. a bare string) is unaffected by this change: iterating a string's
    characters already returned False safely pre-#159, since no single
    character equals a multi-character flag name.
    """
    if not flags:
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
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    prefix = f"{key}:"
    for line in text[3:end].splitlines():
        line = line.strip()
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def briefing_video_counts(briefings_dir: Path) -> Counter:
    """How many briefing files under _briefings/**/*.md list each video id.

    A video listed twice in one briefing counts once for that file. Missing
    dir -> empty Counter. Recurses into subfolders (e.g. _briefings/sales/) so
    topic-based organization of the briefings dir doesn't silently un-see
    videos. Truthy non-string ids are coerced with `str()`, as
    `load_seen_video_ids` always has.
    """
    counts: Counter = Counter()
    if not briefings_dir.is_dir():
        return counts
    for md in sorted(briefings_dir.rglob("*.md")):
        try:
            front_matter, _ = parse_front_matter(md.read_text(encoding="utf-8"))
        except OSError:
            continue
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
