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
import functools
import json
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import unescape
from pathlib import Path
from types import MappingProxyType
from typing import Any

import httpx
import yaml
from googleapiclient.errors import HttpError
from youtube_captions import CaptionsResult, fetch_english_captions

from gemini_common import (
    MAX_RETRIES_TRANSPORT,
    build_permissive_safety_settings,
    create_client,
    get_retry_delay,
    is_transient_transport_error,
    log_usage_metadata,
    require_gemini,
    require_youtube,
)
from timestamp_utils import normalize_timestamp, timestamp_tolerance

log = logging.getLogger("video_intel")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = "gemini-3-flash-preview"
MAX_OUTPUT_TOKENS = 65536
TRANSCRIPT_PARSE_RETRY_LIMIT = 1
SALVAGE_MIN_SPEECH_ENTRIES = 5
# Issue #120: completed-livestream VODs route captions-first. These two strings
# are part of the contract (meta.json provenance + the scan status line), so
# they live next to the other transcript constants rather than inline.
LIVESTREAM_CAPTIONS_FIRST_REASON = "completed livestream VOD: captions-first routing (issue #120)"
LIVESTREAM_MINDMAP_SKIP_STATUS = "skipped (livestream VOD: transcript failed; mindmap-from-video not attempted)"
KEYWORD_MAX_PAGES = 4  # 200 results max per keyword, 400 quota units
LARGE_FILE_THRESHOLD_BYTES = (
    1024 * 1024 * 1024
)  # 1GB - segment required above this. Gemini Files API allows up to 2GB; this ceiling stays comfortably below the platform cap while accommodating typical hour-long recordings.


def _user_config_path() -> Path:
    """User-level config override location.

    Extracted as a function so tests can monkeypatch the path without touching
    `Path.home()` globally.
    """
    return Path.home() / ".video-intel" / "config.yaml"


_USER_CONFIG_SUPPORTED_KEYS = frozenset({"output_dir", "vector_db_dir"})
_LAST_RESOLVED_SOURCE: str | None = None


def load_config() -> dict[str, Any]:
    """Resolve the video-intel config via a four-step precedence chain.

    1. ``SKILL_DIR/config.yaml`` - plugin-local config (gitignored; authored
       by the developer running curate workflows).
    2. ``$VIDEO_INTEL_OUTPUT_DIR`` - env-var override for installed users
       pointing a cached plugin at a non-default corpus. Must be absolute.
    3. ``~/.video-intel/config.yaml`` - user-level minimal config accepting
       ``output_dir`` (required) and ``vector_db_dir`` (optional). Extra
       keys are ignored with one INFO log.
    4. Hard error with an actionable message naming both overrides.

    KD1: plugin-local config wins over env var to prevent a stale shell
    variable from silently redirecting a curate scan away from the author's
    canonical corpus. Env var and user config only apply when the plugin
    file is absent.

    KD7: emits one INFO log naming the winning source; the source string is
    stored in module-level :data:`_LAST_RESOLVED_SOURCE` for downstream
    helpers (e.g. ``require_channels_config``) that surface diagnostics.
    """
    global _LAST_RESOLVED_SOURCE

    # Step 1: plugin-local config
    skill_config = SKILL_DIR / "config.yaml"
    if skill_config.exists():
        with open(skill_config) as f:
            config = yaml.safe_load(f) or {}
        _LAST_RESOLVED_SOURCE = f"SKILL_DIR/config.yaml ({skill_config})"
        log.info("Config resolved from %s", _LAST_RESOLVED_SOURCE)
        return config

    # Step 2: env var
    env_value = (os.environ.get("VIDEO_INTEL_OUTPUT_DIR") or "").strip()
    if env_value:
        if not Path(env_value).is_absolute():
            log.error(
                "VIDEO_INTEL_OUTPUT_DIR must be an absolute path, got: %s",
                env_value,
            )
            sys.exit(1)
        _LAST_RESOLVED_SOURCE = f"VIDEO_INTEL_OUTPUT_DIR={env_value}"
        log.info("Config resolved from %s", _LAST_RESOLVED_SOURCE)
        return {"output_dir": env_value}

    # Step 3: user-level config
    user_config = _user_config_path()
    if user_config.exists():
        try:
            raw = yaml.safe_load(user_config.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            log.error("Failed to parse %s: %s", user_config, exc)
            sys.exit(1)
        if "output_dir" not in raw:
            log.error(
                "%s is missing required key 'output_dir'. Add 'output_dir: <path>' and retry.",
                user_config,
            )
            sys.exit(1)
        supported = {k: v for k, v in raw.items() if k in _USER_CONFIG_SUPPORTED_KEYS}
        extras = sorted(k for k in raw if k not in _USER_CONFIG_SUPPORTED_KEYS)
        if extras:
            log.info(
                "Ignoring unsupported keys in ~/.video-intel/config.yaml: %s",
                ", ".join(extras),
            )
        _LAST_RESOLVED_SOURCE = "~/.video-intel/config.yaml"
        log.info("Config resolved from %s", _LAST_RESOLVED_SOURCE)
        return supported

    # Step 4: all absent
    log.error(
        "No config found. Set VIDEO_INTEL_OUTPUT_DIR=<corpus-path> (absolute) or "
        "create ~/.video-intel/config.yaml with 'output_dir: <corpus-path>'. "
        "See CLAUDE.md for the user-level install procedure."
    )
    sys.exit(1)


def require_channels_config(config: dict[str, Any]) -> None:
    """Fail fast when a curate command runs without ``channels:`` configured.

    Curate commands (scan, concepts, dedupe, and the ``--channel`` branch of
    mindmap/transcript/process) require ``channels:`` in the resolved config.
    The user-level fallback config (step 3 of :func:`load_config`) intentionally
    omits ``channels:``; reaching a curate command with that config means the
    user is trying to drive scan behavior from outside the plugin repo - not
    a supported workflow.

    Search-side commands (search, nugget, status, index, taxonomy-build) and
    the loose-file branch of mindmap/transcript/process do not call this
    helper; they read ``output_dir`` only.

    The error message steers users toward the two supported workflows: run
    from the plugin repo (step 1 of the precedence), or point the env var at
    a checkout that has ``channels:`` populated.
    """
    if not config.get("channels"):
        log.error(
            "This command requires 'channels:' in config.yaml. Run from the "
            "plugin repo, or set VIDEO_INTEL_OUTPUT_DIR to point at a checkout "
            "that has channels configured."
        )
        sys.exit(1)


def resolve_output_dir(config, *, create: bool = True):
    """Resolve the corpus root, creating it by default.

    `create=False` is for read-only commands that promise zero filesystem side
    effects (`profile show`): the default mkdir would otherwise materialize the
    corpus tree while the command prints "nothing was written".
    """
    output_dir = Path(config.get("output_dir", "~/video-intel")).expanduser()
    if not output_dir.is_absolute():
        output_dir = SKILL_DIR / output_dir
    if create:
        output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def resolve_vector_db_dir(config: dict[str, Any], output_dir: Path) -> Path:
    """Resolve the LanceDB vector index location.

    Precedence: config.yaml `vector_db_dir` (tilde-expanded) > output_dir / LANCEDB_DIR.
    See ADR-0016 for why the index may need to live outside a cloud-synced output_dir.
    """
    override = config.get("vector_db_dir")
    if override:
        return Path(override).expanduser()
    return output_dir / LANCEDB_DIR


def probe_atomic_writes(path: Path) -> tuple[bool, str | None]:
    """Probe whether LanceDB can commit at `path` by doing a tiny round-trip.

    Returns (True, None) on success, (False, reason) on failure. Best-effort
    cleans up the probe subdir on every exit path.

    Why integration probe, not file-level probe: empirically (2026-04-18 smoke
    test, two iterations) Google Drive File Stream permits Python-level
    `os.replace` - including rename-over-existing - but still fails LanceDB's
    Rust-side commit with `ERROR_INVALID_FUNCTION (os error 1)`. The failing
    call is inside `lance-table/src/io/commit.rs` and uses object_store's
    LocalFileSystem copy/rename path, which is a different Windows syscall
    family than Python's. Mechanism probes that mimic the syscall all produce
    false negatives on GDFS. The only reliable oracle is LanceDB itself, so
    the probe creates a throwaway 1-row table in a sibling subdir and catches
    whatever LanceDB raises. See ADR-0016.

    Cost (measured on NTFS, 5 runs): ~2s on first call in a fresh process
    (dominated by LanceDB's Rust init, which `build_search_index` would pay
    anyway on its own `connect`+`create_table`), ~20-30ms on warm calls.
    Trivial tax for catching the failure before paying Voyage embedding
    tokens.
    """
    lancedb = require_lancedb()
    probe_dir = path / f"_probe_{uuid.uuid4().hex[:12]}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(str(probe_dir))
        db.create_table("probe", data=[{"v": 1}], mode="overwrite")
        db.drop_table("probe")
        return True, None
    except Exception as e:
        # Blanket catch is deliberate - see docstring. Probe is a safety net; any
        # failure here means the path is unusable, regardless of LanceDB's exception taxonomy.
        reason = (
            f"LanceDB commit failed at probe: {e}. This usually means the path is "
            f"on a cloud-synced filesystem (Google Drive File Stream, OneDrive, "
            f"Dropbox) that does not support the atomic file operations LanceDB's "
            f"MVCC commit path requires. Set vector_db_dir in config.yaml to a "
            f"local path."
        )
        return False, reason
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        if probe_dir.exists():
            log.debug("probe cleanup could not remove %s; residual files may remain", probe_dir)


def resolve_model(args: argparse.Namespace, config: dict[str, Any]) -> str:
    """Resolve Gemini model: --model flag > config.yaml > DEFAULT_MODEL."""
    return args.model or config.get("model") or DEFAULT_MODEL


def normalize_prompt_name(name: str) -> str:
    """Strip path prefixes and .md extension from prompt name.

    Accepts 'mindmap-knowledge', 'prompts/mindmap-knowledge.md',
    or 'prompts\\mindmap-knowledge.md' and returns 'mindmap-knowledge'.
    """
    return Path(name).stem


#: Quarantine copy of a meta.json we could read but not use. Mirrors the
#: `.transcript.raw.txt` / `.mindmap.raw.txt` forensic-sidecar convention: the
#: bytes we discarded stay recoverable by hand instead of being overwritten.
CORRUPT_META_SUFFIX = ".meta.corrupt.json"


def _quarantine_corrupt_meta(meta_path: Path, raw_bytes: bytes) -> Path | None:
    """Save the unusable bytes aside so a rewrite is never a silent loss.

    Takes the bytes the caller ALREADY read rather than re-reading the file.
    Re-reading would open a race: a concurrent healthy writer landing between
    the two reads would get its fresh bytes quarantined and then overwritten by
    the caller, turning a recovery mechanism into the data loss it exists to
    prevent.

    Never overwrites an existing quarantine - a second corruption must not erase
    the evidence from the first - and never raises, because every caller is
    already handling a failure.
    """
    stem = meta_path.name.replace(".meta.json", "")
    try:
        for suffix in ("", *(f".{n}" for n in range(2, 10))):
            sidecar = meta_path.with_name(f"{stem}{suffix}{CORRUPT_META_SUFFIX}")
            if not sidecar.exists():
                sidecar.write_bytes(raw_bytes)
                return sidecar
        return None
    except OSError:
        return None


def _read_meta_best_effort(meta_path: Path, *, raise_on_os_error: bool) -> dict:
    """Read a meta.json for merging, returning ``{}`` when its content is unusable.

    Writer-side callers use this instead of a bare ``json.loads`` because they
    run inside - or feed - exception handlers whose entire job is to RECORD a
    failure. A torn meta.json raising from inside such a handler propagates out
    of the writer and masks the original error, which is the one piece of
    information the handler existed to preserve (issue #124). The cost is
    diagnostic blindness at exactly the moment something else has gone wrong.

    Two failure classes are NOT the same thing, and conflating them trades one
    bug for a worse one:

    * **Content we read but cannot use** - unparseable JSON, invalid UTF-8, or
      a value that parses to something other than an object. The bytes are on
      disk and they are garbage; merging is impossible. We quarantine them to a
      ``.meta.corrupt.json`` sidecar, log a WARNING, and return ``{}``.
    * **A read that never happened** (``OSError``). The bytes may be perfectly
      intact; we just could not get at them, which is a live hazard on the
      cloud-synced mount the production ``output_dir`` uses. Returning ``{}``
      here would make the caller OVERWRITE a healthy file with whatever handful
      of fields it happens to re-supply, destroying ``alt_titles`` (title-
      rotation history exists nowhere else) and ``skip_modes`` (the operator's
      deliberate stage suppression, issue #42). Callers on a success path pass
      ``raise_on_os_error=True`` and get the pre-issue-#124 behavior: loud, and
      the file survives. Callers inside an error handler pass ``False``,
      because there the alternative is destroying the error being recorded.

    The exception tuple is ``(ValueError, OSError)`` deliberately.
    ``UnicodeDecodeError`` subclasses ``ValueError``, NOT ``OSError``, and a
    write torn mid-multibyte-character is the normal shape of a truncated write
    on a corpus with Cyrillic/BCS titles. This file has already been bitten by
    that twice (see ``_load_video_id_index`` and ``load_interest_model``);
    narrowing this back to ``json.JSONDecodeError`` reopens the bug.

    Reader-side consumers that WANT strictness (``_load_video_id_index``, which
    must not invent identity from a damaged file) deliberately do not use this.
    """
    if not meta_path.exists():
        return {}
    # Read BYTES, then decode+parse separately. read_text() would fold the two
    # failure classes back together: it raises UnicodeDecodeError for bad bytes,
    # which is a content problem surfacing from the call that is supposed to
    # only be able to fail at the I/O layer.
    try:
        raw_bytes = meta_path.read_bytes()
    except OSError as exc:
        if raise_on_os_error:
            raise
        log.warning("  %s: could not be read (%s); proceeding without it", meta_path.name, exc)
        return {}
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError both land here
        sidecar = _quarantine_corrupt_meta(meta_path, raw_bytes)
        log.warning(
            "  %s: unusable meta.json (%s); discarding it%s",
            meta_path.name,
            exc,
            f" (saved to {sidecar.name})" if sidecar else "",
        )
        return {}
    if not isinstance(data, dict):
        sidecar = _quarantine_corrupt_meta(meta_path, raw_bytes)
        log.warning(
            "  %s: meta.json is a JSON %s, not an object; discarding it%s",
            meta_path.name,
            type(data).__name__,
            f" (saved to {sidecar.name})" if sidecar else "",
        )
        return {}
    return data


def update_meta(meta_path: Path, fields: dict, mode: str) -> None:
    """Read existing meta.json, merge fields, ensure mode in modes_completed, write back.

    The sentinel mode="identity" is a no-op for modes_completed: identity is metadata
    bootstrap (filling video_id, title, etc.) before a processing stage runs, not a
    stage itself. See plan rev 4 F11 and technical approach section 6.

    Unusable CONTENT is survivable here (quarantined and replaced), but an
    ``OSError`` propagates: this is the shared success-path writer, and
    overwriting a file we merely failed to read would destroy `alt_titles` and
    `skip_modes`, which exist nowhere else. See `_read_meta_best_effort`.
    """
    meta: dict = _read_meta_best_effort(meta_path, raise_on_os_error=True)
    meta.update(fields)
    if mode != "identity":
        modes = meta.get("modes_completed", [])
        if not isinstance(modes, list):
            # Hand-editing meta.json is this project's documented skip_modes
            # recovery flow, so a scalar here is a realistic typo - and
            # `.append` on a str raises AttributeError straight out of the
            # writer, which is the same masking failure the read guard above
            # exists to stop. is_skipped_meta defends the same way.
            log.warning(
                "  %s: modes_completed was a %s, not a list; rebuilding it",
                meta_path.name,
                type(modes).__name__,
            )
            modes = []
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


# ---------------------------------------------------------------------------
# Stage 1 query expansion (ADR-0017, docs/plans/2026-04-20-feat-kb-stage1-*)
# ---------------------------------------------------------------------------

MIN_ALIAS_LEN = 2
MAX_ALIAS_ADDITIONS = 12


def _alias_boundary_pattern(term: str) -> re.Pattern[str]:
    """Punctuation-aware word-boundary regex for a taxonomy term.

    stdlib `\\b` only matches between a word and a non-word character, so it
    silently fails for aliases that begin or end with punctuation
    (e.g. "C++", ".NET", "(MCP)"). The boundary here treats any non-word
    character OR the string edges as a boundary, which is what taxonomy
    aliases actually need.
    """
    return re.compile(
        r"(?:^|(?<=[^\w]))" + re.escape(term) + r"(?=$|[^\w])",
        flags=re.IGNORECASE,
    )


def expand_query_via_taxonomy(
    query: str,
    taxonomy: dict,
) -> tuple[str, list[dict]]:
    """Expand a search query by appending taxonomy aliases for any concept
    whose canonical label or alias appears in the query.

    Stage 1 of ADR-0017. Bridges user vocabulary to creator vocabulary at
    query time. See docs/plans/2026-04-20-feat-kb-stage1-query-expansion-plan.md
    for the contract this honors.

    Returns:
        (expanded_query, match_records) where match_records is a list of
        {"concept_id": str, "matched_term": str, "added": [str, ...]}.
    """
    concepts = taxonomy.get("concepts") or {}
    if not concepts:
        return query, []

    match_records: list[dict] = []
    added_terms: list[str] = []
    seen_added_lower: set[str] = set()
    remaining_cap = MAX_ALIAS_ADDITIONS

    for cid, concept in concepts.items():
        if remaining_cap <= 0:
            break

        label = (concept.get("preferred_label") or "").strip()
        aliases = [a for a in (concept.get("aliases") or []) if isinstance(a, str)]

        # Candidate terms to test against the query, ordered canonical-first
        # so a canonical-label hit is preferred over an alias hit for the
        # matched_term diagnostic.
        candidates: list[str] = []
        if label and len(label) >= MIN_ALIAS_LEN:
            candidates.append(label)
        for a in aliases:
            a = a.strip()
            if len(a) >= MIN_ALIAS_LEN:
                candidates.append(a)

        matched_term: str | None = None
        for term in candidates:
            if _alias_boundary_pattern(term).search(query):
                matched_term = term
                break
        if matched_term is None:
            continue

        # Siblings = canonical + aliases, minus the term that matched.
        all_siblings: list[str] = []
        if label:
            all_siblings.append(label)
        all_siblings.extend(aliases)

        to_add: list[str] = []
        matched_lower = matched_term.lower()
        for sibling in all_siblings:
            s = sibling.strip()
            if not s or len(s) < MIN_ALIAS_LEN:
                continue
            s_lower = s.lower()
            if s_lower == matched_lower:
                continue
            if s_lower in seen_added_lower:
                continue
            to_add.append(s)
            seen_added_lower.add(s_lower)
            remaining_cap -= 1
            if remaining_cap <= 0:
                break

        match_records.append(
            {
                "concept_id": cid,
                "matched_term": matched_term,
                "added": to_add,
            }
        )
        added_terms.extend(to_add)

    if not added_terms:
        return query, match_records

    expanded = query + " " + " ".join(added_terms)
    return expanded, match_records


_VALID_MINDMAP_SOURCE_VALUES = {"auto", "video", "transcript", "none"}

# Issue #54 / review C1: transcript writers populate `transcript_status` with
# different healthy literals depending on path: chunked merge writes "ok"
# (scripts/video_intel.py:1542), single-call success writes "complete"
# (:2419), salvage writes "partial" (:2449). Any non-healthy value triggers
# the partial-source mindmap marker. Keep this set in sync with the writers.
_HEALTHY_TRANSCRIPT_STATUSES = {"ok", "complete"}

# Issue #60: how the transcript step sources its text.
_VALID_TRANSCRIPT_SOURCE_VALUES = {"gemini", "yt-captions", "auto"}
# meta.json transcript_source value written when a transcript is built from the
# YouTube caption track (mirrors the existing "local_file" value for uploads).
TRANSCRIPT_SOURCE_CAPTIONS = "youtube_captions"
TRANSCRIPT_SOURCE_GEMINI = "gemini"


def resolve_transcript_source(channel_config: dict, cli_override: str | None = None) -> str:
    """Decide how the transcript step sources its text (issue #60).

    Returns one of ``"gemini" | "yt-captions" | "auto"``.

    - ``"gemini"`` (default): Gemini multimodal transcript - current behavior,
      now with the confabulation guard (a ``prompt == 0`` / thin response is
      never written as ``transcript_status: complete``).
    - ``"yt-captions"``: skip Gemini, build the transcript from the public
      YouTube English caption track. Cheap, but speech-only (no SCREEN /
      diarization). Fails (returns an error status) when no captions exist.
    - ``"auto"``: try Gemini; on failure (exception, 403, token-cap, or the
      confabulation guard tripping), fall back to ``yt-captions``. Falls all
      the way through to the normal Gemini failure path when captions are also
      unavailable, so behavior is never worse than ``gemini``.

    Precedence: CLI override > channel config ``transcript_source`` > ``"gemini"``.
    Unknown values raise ``ValueError`` to surface typos at call time.
    """
    raw = cli_override if cli_override is not None else channel_config.get("transcript_source", "gemini")
    if raw not in _VALID_TRANSCRIPT_SOURCE_VALUES:
        raise ValueError(
            f"Invalid transcript_source={raw!r}. Expected one of: {sorted(_VALID_TRANSCRIPT_SOURCE_VALUES)}"
        )
    return raw


def livestream_captions_first_applies(
    transcript_source: str,
    channel_config: dict | None = None,
    cli_override: str | None = None,
) -> bool:
    """Whether a completed-livestream VOD goes to captions BEFORE Gemini (#120).

    ``resolve_transcript_source`` collapses "the operator said gemini" and
    "nobody said anything" into the same ``"gemini"`` string, so the decision
    has to be made from the RAW provenance instead:

    - **implicit default** (no `transcript_source` anywhere): captions-first.
      Nobody expressed a preference, so issue #120's reliability finding wins.
    - **explicit ``"auto"``**: captions-first. `auto` explicitly delegates the
      ordering choice to the tool, and captions-first is that choice for a VOD.
    - **explicit ``"gemini"``** (CLI flag or the channel dict literally carrying
      the key): Gemini-first, exactly as before issue #120. The operator chose
      multimodal on purpose - captions are known-garbage, the wrong language, or
      the on-screen content is the point - and silently handing them a
      speech-only transcript would violate the documented config contract. This
      is also the escape hatch when the premiere heuristic misfires.
    - **explicit ``"yt-captions"``**: irrelevant here, that branch never reaches
      Gemini at all.

    Precedence mirrors ``resolve_transcript_source``: a CLI ``--transcript-source
    gemini`` counts as explicit even when the channel config says ``auto``.
    """
    if transcript_source != "gemini":
        return transcript_source == "auto"
    if cli_override == "gemini":
        return False
    # Membership test, never `.get("transcript_source", "gemini")`: the whole
    # point is telling an ABSENT key apart from a key whose value happens to
    # equal the default. A `.get` with a default erases exactly that difference.
    return not (cli_override is None and channel_config is not None and "transcript_source" in channel_config)


STANDALONE_CHANNEL = "_standalone"


def channel_config_by_name(config: dict, channel_name: str | None) -> dict:
    """Look up a channel's config dict by name, or ``{}`` when there is none.

    ``{}`` is the correct answer for an unconfigured or sentinel channel
    (``_standalone`` is matched explicitly, not merely assumed absent from the
    watchlist; also a slugified channel title nobody configured):
    every resolver that takes a channel dict already treats an empty one as
    "no preference expressed", and `livestream_captions_first_applies` in
    particular distinguishes an ABSENT key from a present one, so an empty dict
    must never be faked into carrying defaults.

    Exists so the manual `--url` commands resolve the channel dict the same way
    (issue #127): `cmd_transcript` used to hand the resolver a literal ``{}``
    and silently ignore a channel's configured `transcript_source`.
    """
    if not channel_name or channel_name == STANDALONE_CHANNEL:
        return {}
    return next((c for c in (config.get("channels") or []) if c.get("name") == channel_name), {})


def resolve_mindmap_source(channel_config: dict, *, transcript_available: bool) -> str:
    """Decide which input the mindmap step should consume.

    Returns one of ``"video" | "transcript" | "skip"`` (issue #54).

    The ``transcript_available`` flag is a pure file-presence signal — the
    caller is responsible for deciding whether a transcript artifact exists
    on disk for this video. This function does NOT inspect ``skip_modes``;
    the upstream transcript loop is what honors that knob, and a stale
    on-disk transcript still flips ``transcript_available`` to True (which
    is the right behavior — use what we have).

    Channel config knob ``mindmap_source`` accepts:
    - ``"auto"`` (default): transcript when available, else fall back to video.
      The auto path makes the inversion safe to ship as the new default — a
      missing transcript silently routes back to the legacy video code.
    - ``"transcript"``: must use transcript. Raises ``ValueError`` when none
      is available. Common cause: the user set
      ``skip_modes['transcript']`` (issue #42) AND ``mindmap_source: transcript``
      on the same channel; the conflict surfaces here so callers can either
      remove the skip or change the source.
    - ``"video"``: explicit legacy path, always watches the video.
    - ``"none"``: no mindmap at all, matches existing notify-only intent.

    Unknown values raise ``ValueError`` to surface typos at scan time.
    """
    raw = channel_config.get("mindmap_source", "auto")
    if raw not in _VALID_MINDMAP_SOURCE_VALUES:
        raise ValueError(f"Invalid mindmap_source={raw!r}. Expected one of: {sorted(_VALID_MINDMAP_SOURCE_VALUES)}")
    if raw == "none":
        return "skip"
    if raw == "video":
        return "video"
    if raw == "transcript":
        if not transcript_available:
            raise ValueError(
                "mindmap_source='transcript' but no transcript is available. "
                "Either remove skip_modes['transcript'] (so transcript runs) "
                "or change mindmap_source to 'video' or 'auto'."
            )
        return "transcript"
    # auto
    return "transcript" if transcript_available else "video"


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


_QUOTA_EXCEEDED_REASONS = frozenset(
    {"quotaExceeded", "dailyLimitExceeded", "userRateLimitExceeded", "rateLimitExceeded"}
)


def _is_quota_exceeded(error: HttpError) -> bool:
    """Decide whether an HttpError represents a YouTube Data API quota error.

    Reads the canonical ``error.errors[*].reason`` field from the response
    body rather than substring-matching on str(error), because googleapiclient
    formats messages differently across versions and a substring match on
    "quotaExceeded" can both miss adjacent quota reasons (dailyLimitExceeded,
    userRateLimitExceeded) and false-positive on unrelated debug strings.
    """
    try:
        body = json.loads(error.content.decode("utf-8"))
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return False
    reasons = {item.get("reason") for item in body.get("error", {}).get("errors", []) if isinstance(item, dict)}
    return bool(reasons & _QUOTA_EXCEEDED_REASONS)


def enrich_with_durations(youtube, video_ids: list[str]) -> dict[str, str | None]:
    """Fetch ISO-8601 contentDetails.duration for each video_id.

    Batches into chunks of 50 (YouTube videos.list API limit). For each
    video_id that does not appear in the response — deleted, members-only,
    region-restricted, etc. — the key is present with value None so callers
    can apply their own fail-safe logic. No retry per CLAUDE.md "bounded
    retries only"; transient batch failures bubble up to the caller.

    Quota cost: 1 unit per batch (50 ids), independent of how many parts are
    requested. So a 200-video channel costs 4 units.
    """
    durations: dict[str, str | None] = dict.fromkeys(video_ids)
    if not video_ids:
        return durations
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = youtube.videos().list(id=",".join(batch), part="contentDetails").execute()
        for item in resp.get("items", []):
            durations[item["id"]] = item.get("contentDetails", {}).get("duration")
    return durations


def _is_completed_livestream(item: dict) -> bool:
    """True when a videos.list item is a finished livestream VOD (issue #120).

    Two conditions, both required: the video carries ``liveStreamingDetails``
    (YouTube only attaches that resource to broadcasts), and it is not still
    ``upcoming``/``live`` - a scheduled premiere also carries the resource, but
    it has not aired and is skipped by ``preflight_skip_reason`` instead. A
    missing ``liveBroadcastContent`` is treated as "not a live broadcast now",
    which matches YouTube's own ``none`` default.
    """
    if not item.get("liveStreamingDetails"):
        return False
    return item.get("snippet", {}).get("liveBroadcastContent") not in ("upcoming", "live")


def fetch_preflight_status(youtube, video_ids: list[str]) -> dict[str, dict]:
    """Fetch per-video liveBroadcastContent + privacyStatus + was_livestream.

    Issue #70 established the first two fields; issue #120 added
    ``was_livestream`` by asking the SAME call for one more part
    (``liveStreamingDetails``) - parts are free, so this is still 1 quota unit
    per 50-id batch and there is no extra API round-trip.

    Returns ``{video_id: {"live_broadcast_content": str|None,
    "privacy_status": str|None, "was_livestream": bool}}``. Ids missing from
    the response (deleted, gated) map to an empty dict so the caller's
    fail-safe keeps them - a missing status is never a positive skip signal,
    and ``.get("was_livestream")`` on it is falsy, so an unknown video keeps
    today's routing.
    """
    result: dict[str, dict] = {vid: {} for vid in video_ids}
    if not video_ids:
        return result
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        resp = youtube.videos().list(id=",".join(batch), part="snippet,status,liveStreamingDetails").execute()
        for item in resp.get("items", []):
            result[item["id"]] = {
                "live_broadcast_content": item.get("snippet", {}).get("liveBroadcastContent"),
                "privacy_status": item.get("status", {}).get("privacyStatus"),
                "was_livestream": _is_completed_livestream(item),
            }
    return result


def _lookup_was_livestream(video_id: str) -> bool:
    """Classify a single video as a completed-livestream VOD (issue #120).

    The manual ``--url`` commands have no scan pre-flight to inherit the flag
    from, so they pay one YouTube quota unit here and route through the same
    ``fetch_preflight_status`` helper the scan uses - exactly one place knows
    how ``liveStreamingDetails`` maps to the flag. Returns False (today's
    routing) when there is no API key or the lookup fails: an absent signal
    must never become a positive livestream signal.
    """
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if not yt_key:
        return False
    try:
        yt_build = require_youtube()
        yt = yt_build("youtube", "v3", developerKey=yt_key)
        return bool(fetch_preflight_status(yt, [video_id]).get(video_id, {}).get("was_livestream"))
    except Exception as e:
        log.warning("Could not classify livestream status for %s: %s", video_id, e)
        return False


def should_skip_video_mindmap_for_livestream(
    *,
    was_livestream: bool,
    resolved_source: str,
    transcript_status: str | None,
) -> bool:
    """Whether a mindmap-from-video call must NOT be spent (issue #120).

    Fires only when all three hold: the video is a completed livestream VOD,
    the mindmap resolver landed on ``"video"`` (no transcript on disk), and a
    transcript attempt for this video actually FAILED in this run. That last
    condition is what keeps the rule narrow: a failed attempt is direct
    evidence that Gemini cannot ingest this URI, so a mindmap-from-video call
    against the same URI would hard-fail or confabulate the same way. When no
    transcript was attempted (``transcript_status is None`` - e.g.
    ``auto_transcript: none``, or the long-video guard filtered it out), the
    URI was never proven broken and today's routing is preserved.
    """
    if not was_livestream or resolved_source != "video":
        return False
    return transcript_status is not None and transcript_status.startswith("error")


def _log_livestream_recovery_recipe(video: dict, channel_name: str) -> None:
    """Loud WARNING + the local-file recovery recipe (issue #120).

    Mirrors the members-only 403 recipe in ``cmd_scan``: the operator gets the
    two commands to run without leaving the scan log to read documentation.
    """
    log.warning(
        "      -> Livestream VOD %s: no captions and the Gemini transcript attempt failed. "
        "NOT spending a mindmap-from-video call against the same URI (issue #120).",
        video.get("video_id", "?"),
    )
    log.warning("         To recover: save the MP4 as %s.mp4 in any folder, then run:", video.get("video_id", "?"))
    log.warning("           python scripts/video_intel.py transcript --file <PATH> --channel %s", channel_name)
    log.warning("           python scripts/video_intel.py mindmap    --file <PATH> --channel %s", channel_name)


def _log_chunk_recovery_recipe(video: dict, duration_seconds: int | None, chunk_minutes: int) -> None:
    """Print the smaller-``--chunk-minutes`` recovery recipe (issue #129).

    Sibling of ``_log_livestream_recovery_recipe``, for the other failure whose
    fix the operator has to know rather than derive: a long-video transcript
    that ended in error. Empirically (the 2026-08-11/12 bulk ingest) a serial
    re-run with a smaller chunk size recovered every one of these cheaply,
    because the already-transcribed prefix comes back as an implicit cache hit.

    Only fires when the duration is known and above the chunk threshold -
    suggesting a smaller chunk size for a video that was never chunked would
    send the operator down the wrong path.
    """
    if not duration_seconds or duration_seconds <= chunk_minutes * 60:
        return
    smaller = max(5, chunk_minutes // 2)
    log.warning(
        "      -> %s is %s and its transcript failed at %dm chunks. "
        "A serial re-run with smaller chunks usually recovers it (the already-ingested "
        "prefix bills as an implicit cache hit):",
        video.get("video_id", "?"),
        _fmt_hms(duration_seconds),
        chunk_minutes,
    )
    log.warning(
        "         python scripts/video_intel.py process --url %s --chunk-minutes %d --force",
        video.get("url", ""),
        smaller,
    )


def preflight_skip_reason(status: dict) -> str | None:
    """Return why a video should be skipped before any Gemini call, or None to keep.

    Issue #70, enforcing the principle "the corpus indexes things that have
    happened, not things that will happen." Skips only on POSITIVE signals, so a
    missing/empty status fails safe to KEEP (mirrors the duration fail-safe):

    - ``liveBroadcastContent`` is ``upcoming`` or ``live``: a scheduled premiere
      or in-progress stream that has not finished. Gemini fetches no playable
      stream and confabulates a stub (the 2026-06-18 ``prompt=0`` failures).
    - ``privacyStatus`` is present and not ``public`` (private/unlisted): scan
      cannot reliably process these and Gemini's YouTube-URL ingestion is
      public-only.
    """
    lbc = status.get("live_broadcast_content")
    if lbc in ("upcoming", "live"):
        return f"not yet aired (liveBroadcastContent={lbc})"
    privacy = status.get("privacy_status")
    if privacy and privacy != "public":
        return f"non-public (privacyStatus={privacy})"
    return None


# ---------------------------------------------------------------------------
# Selective scanning: playlists and keywords
# ---------------------------------------------------------------------------


def resolve_playlist_ids(youtube, channel_id: str, playlist_names: list[str]) -> list[tuple[str, str]]:
    """Resolve playlist names to (playlist_id, playlist_title) pairs.

    Uses case-insensitive contains matching. Logs warning for unresolved names
    with available playlist titles.
    """
    # Enumerate all playlists on the channel
    all_playlists: list[dict] = []
    next_page = None
    while True:
        resp = (
            youtube.playlists()
            .list(
                part="snippet",
                channelId=channel_id,
                maxResults=50,
                pageToken=next_page,
            )
            .execute()
        )
        all_playlists.extend(resp.get("items", []))
        next_page = resp.get("nextPageToken")
        if not next_page:
            break

    # Match each requested name
    matched: list[tuple[str, str]] = []
    available_titles = [p["snippet"]["title"] for p in all_playlists]

    for name in playlist_names:
        name_lower = name.lower()
        found = False
        for p in all_playlists:
            title = p["snippet"]["title"]
            if name_lower in title.lower():
                matched.append((p["id"], title))
                log.info('    Resolved playlist: "%s" -> %s (%s)', name, p["id"], title)
                found = True
        if not found:
            log.warning('    Playlist "%s" not found. Available: %s', name, available_titles)

    return matched


def fetch_playlist_videos(youtube, playlist_id: str) -> list[dict]:
    """Fetch all videos from a specific playlist (no date filtering)."""
    videos: list[dict] = []
    next_page = None

    while True:
        resp = (
            youtube.playlistItems()
            .list(
                part="snippet,contentDetails",
                playlistId=playlist_id,
                maxResults=50,
                pageToken=next_page,
            )
            .execute()
        )

        for item in resp.get("items", []):
            published_str = item["contentDetails"].get("videoPublishedAt", item["snippet"]["publishedAt"])
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


def fetch_keyword_videos(youtube, channel_id: str, keyword: str, *, max_pages: int = KEYWORD_MAX_PAGES) -> list[dict]:
    """Search a channel for videos matching a keyword (capped pagination)."""
    videos: list[dict] = []
    next_page = None
    pages = 0

    log.info('    Keyword search: "%s" (~%d quota units)', keyword, max_pages * 100)

    while pages < max_pages:
        resp = (
            youtube.search()
            .list(
                part="snippet",
                channelId=channel_id,
                q=keyword,
                type="video",
                order="date",
                maxResults=50,
                pageToken=next_page,
            )
            .execute()
        )
        pages += 1

        for item in resp.get("items", []):
            video_id = item["id"]["videoId"]
            published_str = item["snippet"]["publishedAt"]
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


def fetch_selective_videos(
    youtube, channel_id: str, channel_config: dict, *, since_dt: datetime | None = None
) -> list[dict]:
    """Fetch videos from playlists and/or keywords, deduplicated by video_id.

    If since_dt is provided, also fetches recent uploads as an additional source
    (additive - playlists and keywords are still fetched regardless of date).
    """
    all_videos: list[dict] = []
    seen_ids: set[str] = set()

    # Playlist sources
    playlist_names = channel_config.get("playlists", [])
    if playlist_names:
        resolved = resolve_playlist_ids(youtube, channel_id, playlist_names)
        for pl_id, pl_title in resolved:
            pl_videos = fetch_playlist_videos(youtube, pl_id)
            log.info('    Playlist "%s": %d videos', pl_title, len(pl_videos))
            for v in pl_videos:
                if v["video_id"] not in seen_ids:
                    seen_ids.add(v["video_id"])
                    all_videos.append(v)

    # Keyword sources
    keywords = channel_config.get("keywords", [])
    for kw in keywords:
        kw_videos = fetch_keyword_videos(youtube, channel_id, kw)
        log.info('    Keyword "%s": %d results', kw, len(kw_videos))
        for v in kw_videos:
            if v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                all_videos.append(v)

    # Recent uploads (additive when since is configured)
    if since_dt is not None:
        recent = fetch_channel_videos(youtube, channel_id, since_dt)
        new_count = 0
        for v in recent:
            if v["video_id"] not in seen_ids:
                seen_ids.add(v["video_id"])
                all_videos.append(v)
                new_count += 1
        if recent:
            log.info(
                "    Recent uploads (since %s): %d videos, %d new",
                since_dt.strftime("%Y-%m-%d"),
                len(recent),
                new_count,
            )

    log.info("  Total: %d unique videos after dedup", len(all_videos))
    return all_videos


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


# Regex for an 11-character YouTube video ID: base64url-ish charset, exactly 11.
# Used by local-file identity resolution to decide when a filename stem is a video_id.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def infer_channel_from_file_path(input_path: Path, output_dir: Path, config: dict) -> str | None:
    """Return the configured channel name if input_path lives directly under output_dir/<channel>/.

    Used by --file local-recovery paths to derive the channel from the file's parent folder,
    matching Daniel's workflow of dropping gated videos straight into the corpus folder.

    Only the input file's immediate parent is checked against the configured channel list;
    files nested more deeply under a channel folder (e.g., output_dir/everyinc/archive/x.mp4)
    do not match, because we cannot safely assume they belong to the channel.
    """
    try:
        output_dir_r = output_dir.resolve()
        parent_r = input_path.parent.resolve()
    except OSError:
        return None

    # Parent must be exactly output_dir/<something>
    try:
        rel = parent_r.relative_to(output_dir_r)
    except ValueError:
        return None
    if len(rel.parts) != 1:
        return None

    candidate = rel.parts[0]
    configured = {c["name"] for c in config.get("channels", [])}
    return candidate if candidate in configured else None


def _find_canonical_meta_by_video_id(channel_dir: Path, video_id: str) -> Path | None:
    """Search a channel folder for a .meta.json whose 'video_id' matches.

    Per plan F11 uniqueness invariant, at most one such file should exist per
    {channel, video_id}. If more than one is found, emit a WARNING log (the
    situation is a pre-existing data integrity issue worth surfacing) and
    deterministically return the lexicographically-first filename so the
    resolver can still make progress. Do not fail hard: blocking recovery on
    a pre-existing data issue is worse than proceeding with a well-defined
    pick.
    """
    if not channel_dir.exists() or not video_id:
        return None
    matches: list[Path] = []
    for meta_file in sorted(channel_dir.glob("*.meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("video_id") == video_id:
            matches.append(meta_file)
    if len(matches) > 1:
        log.warning(
            "Multiple canonical meta.json files share video_id=%s in %s: %s. "
            "Picking the first (%s). Investigate and remove duplicates to honor F11.",
            video_id,
            channel_dir,
            [m.name for m in matches],
            matches[0].name,
        )
    return matches[0] if matches else None


def resolve_local_file_identity(
    input_path: Path,
    *,
    channel_name: str | None,
    channel_dir: Path | None,
    args,
) -> dict:
    """Resolve identity for a local MP4 under the --file path.

    Priority order (plan rev 4, with the flag-override rule applied to both
    persistent-state steps for consistency):
      1. Sibling .meta.json for the same filename stem. Adopts fields verbatim
         EXCEPT where an explicit CLI flag (--video-id, --title, --date) is
         given; those flags override the sibling field on a per-field basis.
         This is what makes ``--force --video-id <id>`` actually update a stale
         sibling's empty ``video_id`` / ``video_url``.
      2. G2 dedup: channel-wide video_id match against canonical scan metas.
         Same flag-override rule as step 1 applies to content fields; prefix
         stays canonical regardless of flags (F11 uniqueness).
      3. Explicit CLI flags (--video-id, --title, --date) when no sibling / G2
         match is available.
      4. Parent-folder inference fills channel (done by caller, passed in).
      5. Filename stem becomes title by default.
      6. Filename stem becomes video_id only if it matches ^[A-Za-z0-9_-]{11}$.
      7. mtime fallback for published (published_source="mtime").
      8. video_url derived from video_id when known; empty otherwise.

    Returns a dict with: channel, video_id, url, title, published, published_source,
    prefix, channel_dir, meta_path.
    """
    stem = input_path.stem

    # --- Step 1: sibling meta.json next to the input file ---
    # Explicit CLI flags override sibling-meta fields on a per-field basis. Reason:
    # a stale sibling from a prior run should NEVER silently shadow explicit user
    # input (`--video-id`, `--title`, `--date`). Only unflagged fields are adopted
    # verbatim. This mirrors the flag-override rule for step 2 (G2 canonical match).
    sibling_meta = input_path.with_suffix(".meta.json")
    if sibling_meta.exists():
        try:
            meta = json.loads(sibling_meta.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        if meta:
            flag_video_id = getattr(args, "video_id", None)
            flag_title = getattr(args, "title", None)
            flag_date = getattr(args, "date", None)

            final_video_id = flag_video_id or meta.get("video_id", "")
            final_title = flag_title or meta.get("title", stem)
            if flag_date:
                final_published = flag_date
                final_source: str = "cli_flag"
            else:
                final_published = meta.get("published", "")
                final_source = "sibling_meta"
            # Derive URL from whichever video_id won (flag or stored)
            if flag_video_id:
                final_url = f"https://www.youtube.com/watch?v={flag_video_id}"
            else:
                final_url = meta.get("video_url", "")

            return {
                "channel": meta.get("channel") or channel_name,
                "video_id": final_video_id,
                "url": final_url,
                "title": final_title,
                "published": final_published,
                "published_source": final_source,
                "prefix": stem,
                "channel_dir": input_path.parent,
                "meta_path": sibling_meta,
            }

    # --- Step 2: G2 dedup by video_id against canonical scan metas ---
    # Candidate video_id comes from --video-id flag first, else stem if it looks like one.
    candidate_video_id = getattr(args, "video_id", None)
    if not candidate_video_id and _VIDEO_ID_RE.match(stem):
        candidate_video_id = stem

    if candidate_video_id and channel_dir is not None:
        canonical = _find_canonical_meta_by_video_id(channel_dir, candidate_video_id)
        if canonical is not None:
            canonical_meta = json.loads(canonical.read_text(encoding="utf-8"))
            canonical_prefix = canonical.name[: -len(".meta.json")]

            # Flag-override precedence within G2: flags update content fields in place,
            # but prefix / channel_dir / meta_path stay canonical (F11).
            flag_title = getattr(args, "title", None)
            flag_date = getattr(args, "date", None)

            final_title = flag_title or canonical_meta.get("title", stem)
            if flag_date:
                final_published = flag_date
                final_source = "cli_flag"
            else:
                final_published = canonical_meta.get("published", "")
                final_source = canonical_meta.get("published_source", "youtube_api")

            return {
                "channel": canonical_meta.get("channel") or channel_name,
                "video_id": candidate_video_id,
                "url": canonical_meta.get("video_url") or f"https://www.youtube.com/watch?v={candidate_video_id}",
                "title": final_title,
                "published": final_published,
                "published_source": final_source,
                "prefix": canonical_prefix,
                "channel_dir": channel_dir,
                "meta_path": canonical,
            }

    # --- Steps 3-8: flags + parent folder + filename + mtime fallback ---
    effective_channel_dir = channel_dir if channel_dir is not None else input_path.parent

    flag_video_id = getattr(args, "video_id", None)
    flag_title = getattr(args, "title", None)
    flag_date = getattr(args, "date", None)

    # video_id: flag > stem-if-11-char > empty
    video_id = flag_video_id or (stem if _VIDEO_ID_RE.match(stem) else "")

    # title: --title flag > stem. Warn if --video-id was given but --title wasn't,
    # since an arbitrary filename stem is a low-confidence title for a specific video.
    if flag_video_id and not flag_title:
        log.warning(
            "%s: --video-id given without --title; falling back to filename stem %r as title. "
            "Pass --title to override if the stem is not meaningful.",
            input_path.name,
            stem,
        )
    title = flag_title or stem

    # published: --date flag > mtime fallback
    if flag_date:
        published = flag_date
        published_source = "cli_flag"
    else:
        if flag_video_id:
            log.warning(
                "%s: --video-id given without --date; falling back to mtime for published. "
                "Pass --date YYYY-MM-DD to override.",
                input_path.name,
            )
        mtime = datetime.fromtimestamp(input_path.stat().st_mtime)
        published = mtime.strftime("%Y-%m-%d")
        published_source = "mtime"

    url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

    return {
        "channel": channel_name,
        "video_id": video_id,
        "url": url,
        "title": title,
        "published": published,
        "published_source": published_source,
        "prefix": stem,
        "channel_dir": effective_channel_dir,
        "meta_path": effective_channel_dir / f"{stem}.meta.json",
    }


# Per-channel {video_id: prefix} index. Populated lazily on first is_processed()
# call for a given channel dir and reused for the rest of the run. The cache is
# what gives us O(1) dedup after one glob per channel, and it survives across
# all modes (scan / transcript / concepts) in the same process.
#
# Why this exists: YouTube creators rotate video titles for SEO A/B testing.
# When the title changes, video_file_prefix() produces a different slug, so
# a slug-only is_processed() check misses the match and re-processes the same
# video_id under a second prefix. Production sweep on 2026-04-22 found 6 such
# duplicate groups across 4 channels. The video_id index catches these.
_VIDEO_ID_CACHE: dict[str, dict[str, str]] = {}


def _load_video_id_index(channel_dir: Path) -> dict[str, str]:
    """Return {video_id: prefix} for all meta.json files in channel_dir.

    Cached per channel for the lifetime of the process. If the directory does
    not exist, returns an empty dict (cached so we do not re-stat on misses).
    Malformed meta.json files are skipped, not fatal - the index is advisory.
    """
    key = str(channel_dir)
    if key in _VIDEO_ID_CACHE:
        return _VIDEO_ID_CACHE[key]
    index: dict[str, str] = {}
    if channel_dir.exists():
        for meta_path in channel_dir.glob("*.meta.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            vid = data.get("video_id")
            if vid:
                prefix = meta_path.name.removesuffix(".meta.json")
                # First-wins: earliest prefix seen for this video_id stays
                # canonical from the prevention path's perspective. dedupe
                # picks the true canonical separately by processed-timestamp.
                index.setdefault(vid, prefix)
    _VIDEO_ID_CACHE[key] = index
    return index


def _invalidate_video_id_cache(channel_dir: Path | None = None) -> None:
    """Drop the video_id index cache for one channel or all channels.

    Called by dedupe and record_alt_title_if_rotated after mutating meta.json
    files so subsequent is_processed() calls re-glob.
    """
    if channel_dir is None:
        _VIDEO_ID_CACHE.clear()
    else:
        _VIDEO_ID_CACHE.pop(str(channel_dir), None)


# ---------------------------------------------------------------------------
# YouTube Shorts classification
# ---------------------------------------------------------------------------
# is_short() decides whether a video is a Short via duration < 60s OR a
# /shorts/<id> HEAD-redirect check (covers the 60-180s "raised cap" Shorts
# that YouTube allowed starting late 2024). Failure mode is fail-safe to
# long-form so prune-shorts never deletes a video it cannot confidently
# classify. See docs/plans/2026-04-24-002-feat-skip-shorts-and-prune-plan.md
# for design rationale.

_SHORT_URL_RETRY_DELAY: float = 0.5  # one retry on 5xx/timeout, then fail-safe


def _parse_iso8601_duration(iso: str | None) -> int | None:
    """Parse an ISO-8601 duration string from YouTube's contentDetails.duration
    into total seconds. Returns None if the input is missing or unparseable."""
    if not iso:
        return None
    match = re.match(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", iso)
    if not match or not any(match.groups()):
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


# Issue #42: ceiling on transcript duration. Above this, the structured-JSON
# response truncates and the bounded retry / salvage cannot recover. Log the
# manual-clipping recipe instead and let the mindmap path proceed.
TRANSCRIPT_MAX_DURATION_DEFAULT = 7200  # 2 hours - leaves headroom for technical talks


def _fmt_hms(seconds: int) -> str:
    """Format an integer second count as compact h/m/s (e.g. 8682 -> '2h24m42s').

    Drops leading zero components so a 12-minute video reads '12m', not '0h12m0s'.
    Trailing zero seconds are dropped too unless that would leave the entire
    string empty (only '0' input keeps '0s' for readability).
    """
    if seconds <= 0:
        return "0s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts: list[str] = []
    if h:
        parts.append(f"{h}h")
    if m or (h and s):
        # Keep the minutes slot when hours+seconds are present so '1h0m1s' reads
        # right; otherwise drop a zero-minute middle component.
        parts.append(f"{m}m")
    if s:
        parts.append(f"{s}s")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Issue #50: chunked transcript helpers (port of translate_video.py pattern,
# adapted for the structured-JSON merge that transcript needs).
# ---------------------------------------------------------------------------


TRANSCRIPT_CHUNK_MINUTES_DEFAULT = 50

#: Fraction of MAX_OUTPUT_TOKENS at or above which a response is treated as
#: having hit the OUTPUT cap. Empirically the confirmed truncation reported
#: candidates=65522 against a 65536 cap (99.98%), while healthy per-chunk
#: responses in the same sessions ran 1,028-10,742 - three orders of magnitude
#: of headroom, so the threshold is nowhere near the healthy range.
OUTPUT_CAP_RATIO = 0.98

#: meta.json transcript_status for a transcript salvaged from a response that
#: hit the output cap. Distinct from the generic "partial" on purpose: a
#: salvage-from-malformed-JSON and a salvage-from-output-truncation were
#: indistinguishable in meta.json, so there was no way to sweep the corpus for
#: the videos that a chunked re-run would actually fix (issue #128).
TRANSCRIPT_STATUS_TRUNCATED = "truncated_output"


def _finish_reason_of(response: object) -> str | None:
    """Best-effort read of the first candidate's finish_reason.

    Lives on ``response.candidates[0].finish_reason``, NOT on ``content.parts``.
    Returns None when the shape is unreadable - observability must never break
    the caller, and an unreadable reason is not evidence of anything.
    """
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        reason = getattr(candidates[0], "finish_reason", None)
        if reason is None:
            return None
        return getattr(reason, "name", None) or str(reason)
    except Exception:  # pragma: no cover - defensive, shape-agnostic
        return None


def hit_output_cap(candidates: int | None, finish_reason: str | None, *, max_output_tokens: int) -> bool:
    """Whether a Gemini response stopped because it ran out of OUTPUT budget.

    Two independent signals, either of which is sufficient:

    * ``finish_reason`` says ``MAX_TOKENS``. Authoritative when present.
    * ``candidates`` sits at or above ``OUTPUT_CAP_RATIO`` of the cap. Needed
      because ``candidates_token_count`` can come back unreadable or absent
      while the response really did truncate, and conversely the finish reason
      is not always exposed.

    Deliberately NOT claimed: this does not catch the *other* early-stop shape
    observed in the same sessions, where Gemini collapses a whole window into
    one monolithic block and stops with candidates nowhere near the cap. That
    failure has no output-budget signature and needs its own detector; calling
    it "truncation" here would be a false negative dressed as coverage.
    """
    if finish_reason and finish_reason.upper().endswith("MAX_TOKENS"):
        return True
    if candidates is None:
        return False
    return candidates >= max_output_tokens * OUTPUT_CAP_RATIO


# Issue #74: hard wall-clock cap (seconds) on a single transcript Gemini call.
# A hung call never returns and the httpx read timeout (1200s) only catches
# per-byte silence, not total time - so without this a hang deadlocks scan.
# On expiry the call raises TimeoutError, which the existing failover treats
# like any other Gemini failure (captions fallback under transcript_source=auto).
# Default 600s (10 min): comfortably above a legitimate hour-long transcript's
# wall-clock, well below the 1200s httpx read timeout so this fires first.
TRANSCRIPT_TIMEOUT_DEFAULT = 600


def resolve_chunk_minutes(channel_config: dict, config: dict, cli_override: int | None = None) -> int:
    """Chunk size for the transcript step, in minutes.

    Precedence matches every other knob in this config (`transcript_max_duration_seconds`,
    `transcript_timeout_seconds`, `skip_shorts`, `since`): CLI flag > per-channel >
    top-level > `TRANSCRIPT_CHUNK_MINUTES_DEFAULT`.

    Exists because `scan` could not reach chunking at all (issue #128). Duration
    is not the predictor of an output-cap truncation - density is - so a dense
    42-minute keynote sailed under the 50-minute chunk trigger and truncated,
    with no way to lower the trigger for the conference channels where that
    keeps happening.
    """
    for candidate in (cli_override, channel_config.get("chunk_minutes"), config.get("chunk_minutes")):
        if candidate is None:
            continue
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            # A YAML list/dict/string here is a config typo; keep the documented
            # ValueError contract so callers need exactly one except clause.
            raise ValueError(f"chunk_minutes must be an integer, got {candidate!r}") from None
        if value <= 0:
            raise ValueError(f"chunk_minutes must be positive, got {candidate!r}")
        return value
    return TRANSCRIPT_CHUNK_MINUTES_DEFAULT


def _build_transcript_chunks(
    duration_seconds: int,
    chunk_minutes: int,
) -> list[tuple[int, int]]:
    """Return [(start_sec, end_sec), ...] chunks for a transcript run.

    Convention (matches translate_video.build_chunk_list):
      - duration <= chunk threshold -> single (0, 0) marker = no clipping.
      - over threshold -> uniform chunk_minutes segments from 0 to duration.

    The (0, 0) sentinel lets the caller skip the chunking branch entirely
    and run a single Gemini call without VideoMetadata offsets.
    """
    if chunk_minutes <= 0:
        raise ValueError(f"chunk_minutes must be positive, got {chunk_minutes}")
    if duration_seconds <= 0:
        return [(0, 0)]
    chunk_seconds = chunk_minutes * 60
    if duration_seconds <= chunk_seconds:
        return [(0, 0)]
    chunks: list[tuple[int, int]] = []
    pos = 0
    while pos < duration_seconds:
        end = min(pos + chunk_seconds, duration_seconds)
        chunks.append((pos, end))
        pos = end
    # Fold a runt tail into the previous chunk (issue #128 review). A video of
    # chunk_seconds + 1 would otherwise produce a 1-second final chunk: one
    # wasted Gemini call that _assess_chunk_coverage then flags as thin,
    # forcing transcript_status: partial on a perfectly healthy video - noise
    # poured into exactly the bucket the truncation detection exists to clean
    # up. A tail under 20% of a chunk carries too little content to justify
    # its own call; the merged chunk stays well under 1.2x the requested size.
    if len(chunks) >= 2 and (chunks[-1][1] - chunks[-1][0]) < chunk_seconds * 0.2:
        _, last_end = chunks.pop()
        prev_start, _ = chunks.pop()
        chunks.append((prev_start, last_end))
    return chunks


def _offset_timestamp(ts: str, offset_seconds: int) -> str:
    """Add offset_seconds to a 'MM:SS' or 'HH:MM:SS' timestamp string.

    Unconditional offset addition - the caller has already decided this
    timestamp needs the offset applied. For per-timestamp absolute-vs-
    relative classification (Gemini's chunk inconsistency), use
    _classify_and_offset_timestamp instead.
    """
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            total = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            total = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            return ts
    except ValueError:
        return ts
    total += offset_seconds
    if total < 0:
        return ts
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _classify_and_offset_timestamp(
    ts: str,
    chunk_start_secs: int,
    chunk_duration_secs: int,
) -> str:
    """Per-timestamp classifier handling Gemini's inconsistent absolute-vs-
    relative timestamp behavior across chunks. Ported from translate_video.py's
    apply_timestamp_offset (proven empirically on BCS translation runs).

    Issue #58: normalize_timestamp runs first as a malformation pre-pass so
    Gemini's minutes-in-HH-field output (e.g. "100:08:57" meaning 1h48m57s)
    gets unscrambled before classification. Without this, the malformed input
    looks like 100 hours and falls through as "implausible" (PR #51 ported
    the classifier but missed the pre-pass; Tucker chunk 3 surfaced the gap).

    Three branches per timestamp:
      1. total <= chunk_duration + tolerance -> relative, add chunk_start offset
      2. chunk_start <= total <= chunk_start + chunk_duration + tolerance ->
         already absolute, leave as-is
      3. otherwise -> implausible, log warning and pass through unchanged

    Returns the offset-applied (or unchanged) timestamp in the most compact
    form: under one hour stays MM:SS; one hour or more becomes H:MM:SS to
    match merge_transcript_json's rendering convention.
    """
    # Wrap-and-strip around normalize_timestamp: classifier receives bare
    # strings, normalize_timestamp expects bracketed input. Defensively
    # strip any pre-existing brackets so an already-bracketed caller does
    # not double-bracket and corrupt the result (issue #58 review feedback).
    ts_clean = ts.strip().lstrip("[").rstrip("]")
    normalized = normalize_timestamp(f"[{ts_clean}]")
    if normalized.startswith("[") and "]" in normalized:
        ts = normalized[1 : normalized.index("]")]
    else:
        ts = ts_clean

    parts = ts.split(":")
    try:
        if len(parts) == 2:
            total = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            total = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            return ts
    except ValueError:
        return ts

    tolerance = timestamp_tolerance(chunk_duration_secs)
    max_relative = chunk_duration_secs + tolerance

    # Branch order: prefer ABSOLUTE when both interpretations are plausible
    # (e.g. value exactly at chunk_start = chunk_duration boundary). The
    # transcript prompt explicitly tells Gemini to use absolute timestamps,
    # so absolute is the expected case; relative is the defensive fallback.
    # This ordering differs from translate_video.py's apply_timestamp_offset
    # (which prefers relative-first) because translate's video-translation
    # prompt does not carry the same instruction.
    if chunk_start_secs > 0 and chunk_start_secs <= total <= chunk_start_secs + max_relative:
        # Absolute: in [chunk_start, chunk_start + chunk_duration + tolerance]
        # range. Leave alone. Skipped for chunk_start=0 since absolute and
        # relative coincide there.
        pass
    elif total <= max_relative:
        # Chunk-relative: add the chunk's start offset.
        total += chunk_start_secs
    else:
        # Implausible (probably a Gemini hallucination or stale prefix).
        log.warning(
            "Implausible timestamp [%s] in chunk (start=%ds, duration=%ds); passing through.",
            ts,
            chunk_start_secs,
            chunk_duration_secs,
        )
        return ts

    if total < 0:
        return ts
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _lookup_video_duration_seconds(video_id: str) -> int | None:
    """Resolve a YouTube video's duration via contentDetails.duration.

    Returns parsed seconds, or None when the lookup fails (no API key, video
    not found, network error). Caller decides how to react to None - the
    chunking decision must fail-safe to single-call when duration is unknown
    rather than blindly chunk the wrong shape.
    """
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if not yt_key:
        return None
    try:
        yt_build = require_youtube()
        yt = yt_build("youtube", "v3", developerKey=yt_key)
        resp = yt.videos().list(part="contentDetails", id=video_id).execute()
        if not resp.get("items"):
            return None
        return _parse_iso8601_duration(resp["items"][0]["contentDetails"]["duration"])
    except Exception as e:
        log.warning("Could not look up duration for %s: %s", video_id, e)
        return None


def _format_chunk_range_label(start_secs: int, end_secs: int) -> str:
    """'0:00 - 50:00' style label for the coverage table."""

    def fmt(s: int) -> str:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{sec:02d}"
        return f"{m:02d}:{sec:02d}"

    return f"{fmt(start_secs)} - {fmt(end_secs)}"


def _build_chunked_transcript_coverage_block(
    duration_seconds: int,
    chunk_minutes: int,
    segment_results: list[dict],
) -> str:
    """Markdown block prepended to the stitched transcript so a reader can see
    coverage at a glance. Mirrors the translate_video.py pattern but for
    transcript-shaped output."""
    lines = [
        "**Source:** chunked transcript",
        f"**Total duration:** {_fmt_hms(duration_seconds)}",
        f"**Chunk size:** {chunk_minutes} minutes",
        f"**Segments:** {len(segment_results)}",
        "",
        "| Segment | Range | Status | Speakers |",
        "|---|---|---|---|",
    ]
    for i, seg in enumerate(segment_results, start=1):
        speakers = ", ".join(seg.get("speakers", [])) or "—"
        lines.append(f"| {i} | {seg['range']} | {seg['status']} | {speakers} |")
    lines.append("")
    return "\n".join(lines)


def merge_chunked_transcripts(
    chunks: list[tuple[int, dict]],
    chunk_duration_seconds: int = 3000,
) -> dict:
    """Merge per-chunk transcript JSONs into a single transcript JSON.

    Input: [(chunk_start_seconds, chunk_json), ...] in chronological order.
    chunk_duration_seconds: nominal length of each chunk in seconds (default
    3000 = 50 min, the typical chunk_minutes default). Used by the per-
    timestamp classifier for absolute-vs-relative detection. Last chunk may
    be shorter; the tolerance in timestamp_tolerance handles the slack.

    Output: dict with the same shape as a single Gemini transcript response
    (transcripts, screen_content, speakers) with speakers deduplicated by
    name with globally-unique voice ids and timestamps classified per-entry
    via _classify_and_offset_timestamp.

    **Hard-won finding (Gate-1 smoke on YFjfBk8HI5o, 2026-04-26):** Gemini's
    timestamp behavior is INCONSISTENT across chunks of the same video.
    Some chunks return absolute timestamps (relative to full video start),
    others return chunk-relative. There's no prompt incantation that
    guarantees consistency. The fix is per-timestamp classification:
    decide for each timestamp whether it falls in [0, chunk_duration] (=
    relative) or [chunk_start, chunk_start + chunk_duration] (= absolute),
    apply offset only in the relative case. This is the same pattern
    translate_video.py's apply_timestamp_offset uses, proven on long-form
    BCS translation runs.

    Speaker dedup is by exact name match. If Gemini renumbers a speaker
    mid-stream (chunk 1's voice=1 = "Lex"; chunk 2's voice=1 = "Peter"),
    the merger correctly maps them to different global voice ids because
    the lookup keys on (chunk_idx, original_voice) -> name -> global_voice.
    """
    merged: dict = {"transcripts": [], "screen_content": [], "speakers": []}
    name_to_global: dict[str, int] = {}
    next_global = 1

    for _chunk_idx, (start_secs, chunk_json) in enumerate(chunks):
        if not isinstance(chunk_json, dict):
            continue
        # Per-chunk (original_voice -> global_voice) map.
        voice_remap: dict[int, int] = {}
        for s in chunk_json.get("speakers", []):
            voice = s.get("voice")
            name = s.get("name") or f"Speaker {voice}"
            if name not in name_to_global:
                name_to_global[name] = next_global
                next_global += 1
                merged["speakers"].append({**s, "voice": name_to_global[name]})
            if voice is not None:
                voice_remap[voice] = name_to_global[name]

        # Per-timestamp absolute-vs-relative classification.
        for t in chunk_json.get("transcripts", []):
            new_t = dict(t)
            if "start" in new_t:
                new_t["start"] = _classify_and_offset_timestamp(new_t["start"], start_secs, chunk_duration_seconds)
            if t.get("voice") in voice_remap:
                new_t["voice"] = voice_remap[t["voice"]]
            merged["transcripts"].append(new_t)

        for sc in chunk_json.get("screen_content", []):
            new_sc = dict(sc)
            if "start" in new_sc:
                new_sc["start"] = _classify_and_offset_timestamp(new_sc["start"], start_secs, chunk_duration_seconds)
            if new_sc.get("end"):
                new_sc["end"] = _classify_and_offset_timestamp(new_sc["end"], start_secs, chunk_duration_seconds)
            merged["screen_content"].append(new_sc)

    return merged


class TranscriptTimeout(TimeoutError):
    """Raised when a transcript Gemini call exceeds its wall-clock budget (issue #74)."""


def _run_with_timeout(fn: Callable, timeout_seconds: int):
    """Run ``fn()`` with a hard wall-clock timeout; raise ``TranscriptTimeout`` on expiry.

    Issue #74. A hung Gemini call never returns, and the httpx ``read`` timeout only
    bounds per-byte silence, not total time - so a slow-dribble or SDK-internal retry
    hang deadlocks the scan. This wraps the call in a **daemon** thread and joins with
    a timeout: on expiry we raise (the caller's existing ``except`` routes it to the
    captions failover, same as any other Gemini failure), and the orphaned worker
    thread - which we cannot kill in Python - is a daemon, so it never blocks process
    exit (it dies when the interpreter exits, or sooner when the underlying httpx read
    timeout finally fires).

    ``signal.alarm`` is deliberately not used: it is Unix-only and this runs on Windows.
    ``ThreadPoolExecutor`` is not used either: its ``shutdown`` joins workers and would
    re-block on the still-hung call. ``timeout_seconds <= 0`` disables the cap (runs
    inline) so tests and power users can opt out.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        return fn()

    box: dict = {}

    def _runner():
        try:
            box["result"] = fn()
        except BaseException as exc:  # re-raised in the caller's thread below
            box["error"] = exc

    worker = threading.Thread(target=_runner, name="transcript-gemini-call", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise TranscriptTimeout(f"transcript Gemini call exceeded {timeout_seconds}s wall-clock timeout (hang)")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _run_chunked_transcript_url(
    *,
    client,
    types,
    video: dict,
    prompt_text: str,
    model: str,
    channel_dir: Path,
    prefix: str,
    chunks: list[tuple[int, int]],
    duration_seconds: int,
    chunk_minutes: int,
    force: bool,
    media_uri: str | None = None,
    transcript_timeout_seconds: int = TRANSCRIPT_TIMEOUT_DEFAULT,
) -> str:
    """Run transcript in N chunks against a YouTube URL or Files API URI, merge,
    write artifact.

    The function name keeps `_url` for backward compatibility, but the
    `media_uri` keyword override lets callers point it at a Gemini Files API
    URI (`files/xyz`) for local-file chunked transcription. Each chunk is a
    separate Gemini call with `VideoMetadata.start_offset/end_offset` against
    the same `media_uri` — Gemini's implicit cache makes follow-up chunks
    cheaper than the first (empirically verified at `cached=560495` on a
    follow-up call against the same upload). No re-upload occurs across
    chunks; the "one upload" guarantee is preserved when `media_uri` points
    to a single Files API URI.

    Skipped (exists, not forced) -> "skipped (exists)".
    All chunks succeeded -> "done".
    Some chunks failed -> "partial" with raw sidecars for failed chunks.
    """
    channel_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = channel_dir / f"{prefix}.transcript.md"
    if transcript_path.exists() and not force:
        return "skipped (exists)"

    if media_uri is None:
        media_uri = video["url"]
    thinking_config = _make_thinking_config_for_transcript(types, model)
    # Force LOW media resolution for transcription regardless of model. Tucker-
    # class interview content (talking heads + overlay text) doesn't need HIGH
    # frame detail; Pro 2.5 default is HIGH which 3x's the input token cost
    # without quality benefit for our prompt's needs (issue #58 Gate 3).
    media_resolution_low = types.MediaResolution.MEDIA_RESOLUTION_LOW
    chunk_results: list[tuple[int, dict]] = []
    segment_rows: list[dict] = []
    failed_chunks: list[tuple[int, int, str]] = []
    thin_chunk_count = 0
    confabulated_chunks = 0

    for chunk_idx, (start_secs, end_secs) in enumerate(chunks, start=1):
        gemini_start: int | None = None if (start_secs == 0 and end_secs == 0) else start_secs
        gemini_end: int | None = None if (start_secs == 0 and end_secs == 0) else end_secs
        chunk_label_for_log = _format_chunk_range_label(start_secs, end_secs or duration_seconds)
        log.info("    chunk %s -> Gemini", chunk_label_for_log)
        # log_usage_metadata emits 'usage transcript-chunkN prompt=N cached=N
        # candidates=N total=N' so the user can audit per-chunk implicit cache
        # hits. cached>0 across later chunks means Gemini's implicit cache
        # deduplicated the URL prefix; cached=0 means each chunk paid full
        # input tokens. The label includes chunk index so multiple-call
        # observability stays readable.
        # Issue #123: capture the per-chunk counts off the same callback that
        # logs them, so the prompt == 0 confabulation guard can run here too.
        # Until now this path only LOGGED the usage, so a fabricated chunk was
        # parsed and stitched into the final transcript with status "ok".
        usage_capture: dict[str, int | None] = {}

        # Both loop variables are bound as defaults rather than closed over. The
        # dict is fresh per iteration, so a late-firing callback can only ever
        # write to its OWN chunk's capture - chunk N must never inherit chunk
        # N-1's counts, which is how a guard silently starts judging the wrong
        # window.
        def _on_chunk_resp(r, _idx=chunk_idx, _capture=usage_capture):
            counts = log_usage_metadata(r, f"transcript-chunk{_idx}")
            if counts is not None:
                _capture.clear()
                _capture.update(counts)

        try:
            # Issue #74: wall-clock cap per chunk. A hung chunk raises
            # TranscriptTimeout, which we treat as a per-chunk failure (mark it
            # FAILED and continue) so one hang loses a single chunk, not the whole
            # video, and never deadlocks the scan.
            raw = _run_with_timeout(
                lambda _s=gemini_start, _e=gemini_end, _cb=_on_chunk_resp: call_gemini(
                    client,
                    types,
                    media_uri,
                    prompt_text,
                    model,
                    response_json=True,
                    start_offset=_s,
                    end_offset=_e,
                    on_response=_cb,
                    thinking_config=thinking_config,
                    media_resolution=media_resolution_low,
                ),
                transcript_timeout_seconds,
            )
        except TranscriptTimeout as e:
            log.warning("    chunk %s: %s", chunk_label_for_log, e)
            failed_chunks.append((start_secs, end_secs, ""))
            segment_rows.append(
                {
                    "range": _format_chunk_range_label(start_secs, end_secs or duration_seconds),
                    "status": "FAILED (timeout)",
                    "speakers": [],
                }
            )
            continue

        # Issue #123 confabulation guard, the chunked member of the family that
        # #60 (single-shot transcript) and #119 (video mindmap) already closed.
        # prompt == 0 means Gemini ingested no video for this window and wrote
        # from priors. Discarding one chunk is cheap; the alternative is a
        # fabricated 50-minute stretch sitting inside an otherwise real
        # transcript, invisible because the neighbouring chunks are genuine and
        # the coverage table shows the window as present.
        #
        # The comparison is `== 0` exactly, never falsy: log_usage_metadata
        # returns None for a count it could not read, and unreadable is not
        # proof of confabulation (issue #125 makes both encodings of a genuine
        # zero - a literal 0 and one omitted on the wire - arrive here as 0).
        if usage_capture.get("prompt") == 0:
            confabulated_chunks += 1
            log.warning(
                "    chunk %s: confabulation guard tripped - Gemini reported prompt=0 (no video ingested); discarding",
                chunk_label_for_log,
            )
            # Every comparable guard in this file logs how to recover. Without
            # it, discarding a fabricated window converts fabrication into
            # permanently MISSING content: the video is still marked processed,
            # is_processed() skips it on every future scan, and the captions
            # failover never fires because it keys on a status starting "error".
            log.warning(
                "      Recover this window with: transcript --url %s --start %d --end %d --force"
                "  (clobbers the canonical file - back it up and merge by absolute timestamp)",
                video.get("url", ""),
                start_secs,
                end_secs or duration_seconds,
            )
            failed_chunks.append((start_secs, end_secs, raw or ""))
            segment_rows.append(
                {
                    "range": _format_chunk_range_label(start_secs, end_secs or duration_seconds),
                    "status": "FAILED (confabulation: prompt=0)",
                    "speakers": [],
                }
            )
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            try:
                parsed = json.loads(isolate_json(raw or ""))
            except (json.JSONDecodeError, TypeError):
                salvaged, _ = salvage_transcript_sections(raw or "")
                parsed = salvaged or None

        # Real-input gotcha: Gemini sometimes wraps the JSON response in [...]
        # instead of {...}. Two distinct list shapes need handling:
        # 1) Pro 2.5 task-wrapper: [{"task": "transcripts", "output": [...]}, ...]
        #    → use _wrapper_to_envelope_dict to rebuild flat envelope (PR #46
        #    fixed this for single-call path; mirror it here for chunked).
        # 2) Simple list-wrap: [{...flat envelope...}] → take first element.
        # Without (1), Pro 2.5 chunks false-positive as thin (issue #58 Gate 3).
        if isinstance(parsed, list):
            envelope = _wrapper_to_envelope_dict(parsed)
            parsed = envelope if envelope is not None else (parsed[0] if parsed else None)

        chunk_label = _format_chunk_range_label(start_secs, end_secs or duration_seconds)
        if not parsed or not isinstance(parsed, dict):
            failed_chunks.append((start_secs, end_secs, raw or ""))
            segment_rows.append({"range": chunk_label, "status": "FAILED (parse)", "speakers": []})
            continue

        # Issue #58 Gate 2: detect chunks that returned successful JSON but
        # transcribed only a fraction of the allotted time window. Tucker
        # chunk 2 trip: candidates=1484 thoughts=15013 -> Gemini burned its
        # output budget on thinking and stopped after ~3 min of a 50-min
        # window. With thinking_config now capped above this should be rare,
        # but the sanity check makes any future stochastic dropout loud.
        chunk_status = _assess_chunk_coverage(parsed, start_secs, end_secs, duration_seconds, chunk_idx)
        if chunk_status == "thin":
            thin_chunk_count += 1

        chunk_results.append((start_secs, parsed))
        speaker_names = [s.get("name", f"Speaker {s.get('voice')}") for s in parsed.get("speakers", [])]
        segment_rows.append({"range": chunk_label, "status": chunk_status, "speakers": speaker_names})

    if not chunk_results:
        # All chunks failed - persist concatenated raw responses for forensics
        for i, (sstart, send, raw) in enumerate(failed_chunks, start=1):
            sidecar = channel_dir / f"{prefix}.transcript.raw.chunk{i}-{sstart}-{send}.txt"
            sidecar.write_text(raw, encoding="utf-8")
        if confabulated_chunks == len(chunks):
            # This string is persisted as transcript_failover_reason by the
            # captions failover, so it routes a future operator to a
            # troubleshooting row. "parse failure" would be the wrong one.
            return "error: all chunks confabulated (prompt=0; raw sidecars saved)"
        return "error: all chunks failed parsing (raw sidecars saved)"

    chunk_duration = chunk_minutes * 60
    merged = merge_chunked_transcripts(chunk_results, chunk_duration_seconds=chunk_duration)

    # Sort transcripts by classified timestamp before monotonicity check.
    # merge_transcript_json's downstream rendering also sorts; mirror that
    # here so the check operates on the same order as the final output.
    # Without this sort, the check fires false alarms on per-chunk insert
    # order (chunks emit entries in chronological-within-chunk order, but
    # the merged list interleaves chunks).
    merged["transcripts"].sort(key=lambda t: timestamp_to_seconds(t.get("start", "")))

    # Post-merge monotonicity check (Gate-1 finding 2026-04-26): even with
    # the per-timestamp classifier, Gemini hallucinations or edge cases can
    # produce backward jumps. Log them so the user knows which sections to
    # eyeball, and mark transcript_status: "partial" so downstream automation
    # can detect quality issues.
    monotonicity_warnings: list[tuple[str, str]] = []
    last_secs: int | None = None
    for t in merged["transcripts"]:
        ts = t.get("start", "")
        secs = timestamp_to_seconds(ts)
        if last_secs is not None and secs < last_secs:
            prev_label = _format_chunk_range_label(last_secs, last_secs).split(" - ")[0]
            curr_label = _format_chunk_range_label(secs, secs).split(" - ")[0]
            monotonicity_warnings.append((prev_label, curr_label))
        last_secs = secs
    if monotonicity_warnings:
        log.warning(
            "  Stitched transcript has %d non-monotonic timestamp jumps (transcript_status=partial). First 5: %s",
            len(monotonicity_warnings),
            ", ".join(f"{a}->{b}" for a, b in monotonicity_warnings[:5]),
        )
    body = merge_transcript_json(merged, speakers_map={})
    coverage = _build_chunked_transcript_coverage_block(duration_seconds, chunk_minutes, segment_rows)
    transcript_path.write_text(coverage + "\n" + body, encoding="utf-8")

    # Persist raw sidecars only for failed chunks so the user can retry.
    for i, (sstart, send, raw) in enumerate(failed_chunks, start=1):
        sidecar = channel_dir / f"{prefix}.transcript.raw.chunk{i}-{sstart}-{send}.txt"
        sidecar.write_text(raw, encoding="utf-8")

    is_partial = bool(failed_chunks) or thin_chunk_count > 0
    meta_fields = {
        "video_url": video["url"],
        "video_id": video["video_id"],
        "channel": channel_dir.name,
        "title": video["title"],
        "published": video["published"],
        "model": model,
        "transcript_status": "partial" if is_partial else "ok",
        "transcript_chunks": len(chunks),
        "transcript_chunk_minutes": chunk_minutes,
        "transcript_thin_chunks": thin_chunk_count,
        "transcript_confabulated_chunks": confabulated_chunks,
    }
    if confabulated_chunks:
        # A dedicated field, NOT last_error: update_meta resets last_error to
        # None after merging, so anything written there would be silently
        # dropped. This is what makes "which transcripts have a fabricated
        # hole?" answerable from meta.json instead of by grepping run logs -
        # the generic `partial` alone is byte-identical to a parse failure.
        meta_fields["transcript_confabulation_note"] = (
            f"{confabulated_chunks} of {len(chunks)} chunks reported prompt=0 and were discarded"
        )
    update_meta(channel_dir / f"{prefix}.meta.json", meta_fields, mode="transcript")
    return "partial" if is_partial else "done"


def _scan_transcribe_one(
    *,
    client,
    types,
    video: dict,
    prompt_text: str,
    model: str,
    channel_dir: Path,
    prefix: str,
    transcript_source: str,
    transcript_timeout_seconds: int,
    livestream_captions_first: bool,
    duration_seconds: int | None,
    chunk_minutes: int,
) -> tuple[str, str]:
    """One video's transcript for `scan`, chunking when the duration warrants it.

    Until issue #128 the scan loop called `process_transcript` single-shot for
    every video, so chunking was reachable only from `transcript --url`,
    `process --url` and `process --file`. A dense sub-threshold video would blow
    the OUTPUT cap, salvage to `partial`, exit 0, and have to be found and
    re-run by hand.

    Three cases deliberately keep the single-shot path:

    * **duration unknown** - fail-safe to today's behavior rather than guessing
      a chunk layout from a duration we could not parse;
    * **`yt-captions`** - the caption track comes back whole, so chunking it
      would be pointless work;
    * **a livestream VOD routed captions-first** (issue #120) - `process_transcript`
      owns that ordering, and diverting it into the chunker here would silently
      spend N Gemini calls on a URI we have not yet established is fetchable.
      Its routing stays byte-identical.
    """
    if (
        duration_seconds is not None
        and duration_seconds > chunk_minutes * 60
        and transcript_source != "yt-captions"
        and not livestream_captions_first
    ):
        chunks = _build_transcript_chunks(duration_seconds, chunk_minutes)
        log.info(
            "    %s: %s exceeds %dm - transcribing in %d chunks",
            prefix,
            _fmt_hms(duration_seconds),
            chunk_minutes,
            len(chunks),
        )
        # process_transcript catches its own failures and returns an
        # "error: ..." status; the chunked helper does not, and the scan's
        # `future.result()` is unguarded - so an uncaught exception here would
        # abort the ENTIRE scan, not just this video. Mirror the contract, and
        # keep the captions failover that the single-shot path would have run.
        try:
            status = _run_chunked_transcript_url(
                client=client,
                types=types,
                video=video,
                prompt_text=prompt_text,
                model=model,
                channel_dir=channel_dir,
                prefix=prefix,
                chunks=chunks,
                duration_seconds=duration_seconds,
                chunk_minutes=chunk_minutes,
                force=False,
                transcript_timeout_seconds=transcript_timeout_seconds,
            )
        except Exception as e:
            log.warning("    %s: chunked transcript raised: %s", prefix, e)
            status = f"error: {e}"
        if status.startswith("error") and transcript_source == "auto":
            fb = _try_captions_transcript(
                video,
                channel_dir / f"{prefix}.transcript.md",
                channel_dir / f"{prefix}.meta.json",
                prefix,
                reason=f"chunked transcript failed: {status}",
                force=False,
            )
            if fb is not None:
                return fb
        if status.startswith("error"):
            _log_chunk_recovery_recipe(video, duration_seconds, chunk_minutes)
        return prefix, status

    return process_transcript(
        client,
        types,
        video,
        prompt_text,
        model,
        channel_dir,
        prefix,
        transcript_source=transcript_source,
        transcript_timeout_seconds=transcript_timeout_seconds,
        livestream_captions_first=livestream_captions_first,
    )


@functools.cache
def _is_youtube_short_url(video_id: str) -> bool:
    """Return True when https://www.youtube.com/shorts/<id> renders as a Short.

    YouTube serves the canonical Shorts page (HTTP 200) for actual Shorts and
    redirects (303 with Location: /watch?v=<id>) for long-form. We treat any
    non-200 status as long-form. One bounded retry on 5xx or timeout, then
    fail-safe to False (per CLAUDE.md "bounded retries only").

    Cached per video_id for the lifetime of the process via lru_cache. Tests
    must call cache_clear() between cases to avoid bleed-through.
    """
    if not video_id:
        return False
    url = f"https://www.youtube.com/shorts/{video_id}"
    for attempt in range(2):
        try:
            response = httpx.head(url, follow_redirects=False, timeout=5.0)
        except httpx.HTTPError:
            if attempt == 0:
                if _SHORT_URL_RETRY_DELAY:
                    time.sleep(_SHORT_URL_RETRY_DELAY)
                continue
            return False
        if 500 <= response.status_code < 600 and attempt == 0:
            if _SHORT_URL_RETRY_DELAY:
                time.sleep(_SHORT_URL_RETRY_DELAY)
            continue
        return response.status_code == 200
    return False


def is_short(video_id: str | None, duration_iso: str | None) -> bool:
    """Decide whether a YouTube video is a Short.

    Two-signal predicate: duration < 60s OR /shorts/<id> redirect returns 200.
    Fail-safe to long-form (False) on any classification ambiguity — false
    negatives are recoverable (re-run prune-shorts), false positives delete
    real videos.
    """
    if not video_id:
        return False
    duration = _parse_iso8601_duration(duration_iso)
    if duration is not None and duration < 60:
        return True
    try:
        return _is_youtube_short_url(video_id)
    except Exception:
        return False


def is_processed(
    output_dir: Path,
    channel_name: str,
    video: dict,
    mode: str,
    *,
    any_variant: bool = False,
) -> bool:
    """Check if a video has already been processed for a given mode.

    Primary path: consult the per-channel video_id index. If the video_id is
    already claimed by some prefix, check that prefix's mode artifact. This
    catches title-rotation duplicates (same video_id, different slug).

    Fallback: if the video has no id or the id is not in the index, use the
    slug-based existence check. This preserves behavior for legacy artifacts
    missing meta.json and for genuinely new videos.
    """
    channel_dir = output_dir / channel_name

    vid = video.get("video_id")
    if vid:
        index = _load_video_id_index(channel_dir)
        existing_prefix = index.get(vid)
        if existing_prefix:
            return _mode_artifact_present(channel_dir, existing_prefix, mode, any_variant=any_variant)

    prefix = video_file_prefix(video)
    return _mode_artifact_present(channel_dir, prefix, mode, any_variant=any_variant)


def _mode_artifact_present(
    channel_dir: Path,
    prefix: str,
    mode: str,
    *,
    any_variant: bool,
) -> bool:
    """True when the given mode's artifact exists and is non-empty under prefix."""
    if mode == "transcript":
        target = channel_dir / f"{prefix}.transcript.md"
        return target.exists() and target.stat().st_size > 0

    if any_variant:
        return (
            any(f.stat().st_size > 0 for f in channel_dir.glob(f"{prefix}.mindmap*.md"))
            if channel_dir.exists()
            else False
        )

    target = channel_dir / f"{prefix}.mindmap.md"
    return target.exists() and target.stat().st_size > 0


def record_alt_title_if_rotated(
    output_dir: Path,
    channel_name: str,
    video: dict,
) -> bool:
    """If this video_id already has a meta but the incoming title differs,
    append the incoming title to that meta's alt_titles list.

    Returns True if a write happened. Idempotent: no write if title matches
    canonical or is already in alt_titles. Cache is invalidated on write so
    the next is_processed() call sees the updated meta.
    """
    vid = video.get("video_id")
    new_title = video.get("title")
    if not vid or not new_title:
        return False

    channel_dir = output_dir / channel_name
    index = _load_video_id_index(channel_dir)
    existing_prefix = index.get(vid)
    if not existing_prefix:
        return False

    meta_path = channel_dir / f"{existing_prefix}.meta.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    if data.get("title") == new_title:
        return False
    alts = list(data.get("alt_titles", []))
    if new_title in alts:
        return False

    alts.append(new_title)
    data["alt_titles"] = alts
    meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _invalidate_video_id_cache(channel_dir)
    return True


SKIP_MODES_VALID = ("mindmap", "transcript", "concepts")


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


def is_skipped(output_dir, channel_name, video, mode: str | None = None) -> bool:
    """Disk-backed wrapper: read meta.json then delegate to is_skipped_meta.

    Backward compatible: callers without `mode=` get "any skip" semantics
    (the pre-issue-42 behavior).
    """
    prefix = video_file_prefix(video)
    meta_path = output_dir / channel_name / f"{prefix}.meta.json"
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return is_skipped_meta(meta, mode=mode)


# ---------------------------------------------------------------------------
# Gemini API calls
# ---------------------------------------------------------------------------


def _make_thinking_config_for_transcript(types, model: str):
    """Build a minimum-thinking ThinkingConfig for chunked transcription.

    Transcription is mechanical (diarize + log on-screen content + return JSON),
    not analytical. Letting Gemini's dynamic-thinking default consume output
    tokens for thinking is a stochastic failure mode (issue #58 Gate 2:
    Tucker chunk 2 burned 15,013 thinking tokens, produced 1,484 output tokens,
    truncated ~47 minutes of content). Mirrors translate_video.py's
    SRT_DEFAULT_THINKING_BUDGET=128 mitigation but model-aware: 2.5 Pro
    minimum is 128, 2.5 Flash can disable with 0, Gemini 3.x uses
    thinking_level. Returns None for unknown models so the SDK default
    applies and we don't 400 on a model we don't recognize.
    """
    if "gemini-3-flash" in model:
        # Flash-exclusive level: lower than LOW. Confirmed in official docs at
        # ai.google.dev/gemini-api/docs/thinking and Firebase AI Logic guide.
        return types.ThinkingConfig(thinking_level="minimal")
    if "gemini-3" in model:
        # Pro variants: LOW is the lowest available value (Pro models have
        # no MINIMAL level). Default would be HIGH.
        return types.ThinkingConfig(thinking_level="low")
    if "2.5-flash" in model:
        return types.ThinkingConfig(thinking_budget=0)
    if "2.5-pro" in model:
        return types.ThinkingConfig(thinking_budget=128)
    return None


def _local_file_duration_seconds(path: Path) -> int | None:
    """Return the duration of a local media file in seconds, via ffprobe.

    Returns None if ffprobe is not available, the file is unreadable, or
    the duration cannot be parsed. Callers must fall back to single-shot
    transcription (current behavior) when this returns None.

    Used by cmd_process / cmd_transcript local-file paths to decide whether
    to chunk a long video transcript. The chunked-transcript path is
    significantly more reliable than single-shot on hour-long inputs
    (single-shot returns malformed JSON intermittently — see the
    2026-05-02 raw sidecars for the empirical evidence).
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return None
        duration_str = result.stdout.strip()
        if not duration_str:
            return None
        return int(float(duration_str))
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None


def _resolve_media_resolution(types, choice: str):
    """Map a CLI string choice to the matching genai MediaResolution enum value.

    Used at the cmd_process / cmd_mindmap boundary to turn the
    --media-resolution {low,high} flag into the SDK enum that call_gemini
    expects. Centralized so both subcommands use identical mapping.
    """
    mapping = {
        "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
        "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
    }
    if choice not in mapping:
        # Defensive — argparse `choices=` should have rejected this, but a
        # programmatic caller could pass an invalid value.
        raise ValueError(f"Invalid media_resolution={choice!r}. Expected 'low' or 'high'.")
    return mapping[choice]


def _assess_chunk_coverage(
    parsed: dict,
    start_secs: int,
    end_secs: int,
    duration_seconds: int,
    chunk_idx: int,
) -> str:
    """Detect chunks that returned successful JSON but transcribed only a
    fraction of the allotted time window.

    Returns "ok" when the chunk's observed timestamp span covers >= 50% of
    its allotted window, "thin" otherwise. "thin" propagates to overall
    transcript_status as partial so downstream automation can detect the
    quality issue without re-parsing the transcript.

    Issue #58 Gate 2: Tucker chunk 2 returned candidates=1484, thoughts=15013
    -> ~3 minutes of content for a 50-minute window. With thinking_config
    capped via _make_thinking_config_for_transcript this should be rare,
    but the check makes any future stochastic dropout loud rather than
    silently shipping a 50%-empty transcript with status=ok.
    """
    if start_secs == 0 and end_secs == 0:
        return "ok"
    transcripts = parsed.get("transcripts") or []
    if not transcripts:
        log.warning(
            "Chunk %d transcribed 0 entries for window %ds-%ds; flagged as thin.",
            chunk_idx,
            start_secs,
            end_secs,
        )
        return "thin"
    ts_seconds: list[int] = []
    for t in transcripts:
        ts_str = t.get("start") if isinstance(t, dict) else None
        if not ts_str:
            continue
        try:
            ts_seconds.append(timestamp_to_seconds(str(ts_str)))
        except (ValueError, AttributeError):
            continue
    if len(ts_seconds) < 2:
        log.warning(
            "Chunk %d has fewer than 2 parseable timestamps for window %ds-%ds; flagged as thin.",
            chunk_idx,
            start_secs,
            end_secs,
        )
        return "thin"
    observed_span = max(ts_seconds) - min(ts_seconds)
    allotted_span = max(1, end_secs - start_secs)
    ratio = observed_span / allotted_span
    if ratio < 0.5:
        log.warning(
            "Chunk %d transcribed %ds of %ds (%.1f%% of allotted window); flagged as thin. "
            "Re-run may be needed to recover content.",
            chunk_idx,
            observed_span,
            allotted_span,
            ratio * 100,
        )
        return "thin"
    return "ok"


def call_gemini(
    client,
    types,
    media_uri,
    prompt_text,
    model,
    response_json=False,
    *,
    start_offset: int | None = None,
    end_offset: int | None = None,
    fps: float | None = None,
    on_response: Callable[[object], None] | None = None,
    thinking_config=None,
    media_resolution: str | None = None,
):
    """Send a video to Gemini for multimodal analysis with retry on rate limits.

    ``media_uri`` is the Gemini-side input URI: either a YouTube URL (canonical case)
    or a Gemini Files API URI (``files/xyz``) from ``upload_local_video``. Callers
    can keep a separate canonical ``video_url`` on the video dict for persistence
    while passing ``media_uri`` here for the actual upload source. This split is
    what keeps ``file_uri`` out of persisted artifacts on the local-recovery path
    (plan rev 4 F8).

    Optional start_offset/end_offset (in seconds) clip the video to a segment
    via Gemini's VideoMetadata.

    Optional fps (frames per second) reduces Gemini's frame extraction rate.
    Default is 1.0 (one frame per second). At 1fps, the model caps requests
    at 10800 frames (= 3 hours of video). Pass fps=0.5 to halve the frame
    count for videos longer than 3h - this is what `process --url` uses for
    its mindmap step on long-form podcasts (issue #50 Gate-1 finding).

    Optional on_response callback receives the raw response object before
    ``response.text`` is returned. Used for usage-token observability without
    changing the return contract (callers already rely on a string return).
    Observability must never break the call: the callback is invoked inside a
    try/except that logs at warning on failure.
    """
    config_kwargs = {
        "temperature": 0.3,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "safety_settings": build_permissive_safety_settings(types),
    }
    if response_json:
        config_kwargs["response_mime_type"] = "application/json"
    if thinking_config is not None:
        config_kwargs["thinking_config"] = thinking_config
    if media_resolution is not None:
        # Per ai.google.dev/gemini-api/docs/media-resolution: video low/medium
        # are treated identically at 70 tok/frame; HIGH (280 tok/frame) is
        # only needed for OCR-dense or fine-detail video. For Pro 2.5
        # transcription of interview content (talking heads, overlay text),
        # LOW is the documented right choice and brings input cost to parity
        # with Flash 3 (~3x reduction vs Pro 2.5 default HIGH).
        config_kwargs["media_resolution"] = media_resolution

    part_kwargs = {"file_data": types.FileData(file_uri=media_uri)}
    if start_offset is not None or end_offset is not None or fps is not None:
        meta_kwargs = {}
        if start_offset is not None:
            meta_kwargs["start_offset"] = f"{start_offset}s"
        if end_offset is not None:
            meta_kwargs["end_offset"] = f"{end_offset}s"
        if fps is not None:
            meta_kwargs["fps"] = fps
        part_kwargs["video_metadata"] = types.VideoMetadata(**meta_kwargs)

    contents = types.Content(
        parts=[
            types.Part(**part_kwargs),
            types.Part(text=prompt_text),
        ]
    )

    max_retries_rate = 3
    max_retries_server = 8
    # Transport retries are counted SEPARATELY from `attempt` (PR #136 review):
    # `attempt` counts failures of every class, so with a budget of 1 a drop
    # that follows any 429/5xx retry would arrive already over budget.
    transport_attempts = 0
    for attempt in range(max(max_retries_rate, max_retries_server) + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            if on_response is not None:
                try:
                    on_response(response)
                except Exception as obs_exc:
                    log.warning("on_response callback failed: %s", obs_exc)
            return response.text
        except Exception as e:
            retry = get_retry_delay(
                e,
                attempt,
                max_retries_rate=max_retries_rate,
                max_retries_server=max_retries_server,
                max_retries_transport=MAX_RETRIES_TRANSPORT,
                transport_attempt=transport_attempts,
            )
            if retry is None:
                raise
            if is_transient_transport_error(e):
                transport_attempts += 1
            kind, wait, max_for_type = retry
            log.warning("%s — retry %d/%d in %.0fs...", kind, attempt + 1, max_for_type, wait)
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Mind map processing
# ---------------------------------------------------------------------------


def process_mindmap(
    client,
    types,
    video,
    prompt_text,
    model,
    output_dir,
    channel_name,
    *,
    prompt_name=None,
    force=False,
    prefix: str | None = None,
    channel_dir_override: Path | None = None,
    media_uri: str | None = None,
    fps: float | None = None,
    source: str = "video",
    transcript_path: Path | None = None,
    media_resolution=None,
):
    """Generate a mind map for a single video.

    When ``source="video"`` (default, legacy path), Gemini watches the media at
    ``video["url"]`` (or ``media_uri`` override). When ``source="transcript"``
    (issue #54), the function reads the on-disk
    ``<channel_dir>/<prefix>.transcript.md`` (or the explicit ``transcript_path``)
    and sends the text to Gemini via ``call_gemini_text`` with
    ``response_mime_type="text/plain"``. This is roughly 10x cheaper and side-steps
    Gemini's 10800-frame video cap on long content.

    Artifact shape is identical between both paths: same `<!-- video: -->`
    header, same `.mindmap.md` filename, same downstream concepts contract. The
    transcript path additionally writes ``mindmap_source="transcript"`` to
    meta.json, and when the source transcript was partial it adds
    ``mindmap_source_status="partial"`` plus a ``<!-- source: partial transcript -->``
    HTML comment line in the markdown output so readers know.

    On the ``source="video"`` path the response is subject to the ``prompt == 0``
    confabulation guard (issue #119, the mindmap-side twin of issue #60's transcript
    guard): a Gemini call that ingested zero video tokens produces a plausible mind
    map about some other video, so nothing is written, the raw text is kept as
    ``<prefix>.mindmap.raw.txt`` for forensics, and the failure is recorded in
    meta.json via the same handler as any other error.

    The three keyword overrides ``prefix`` / ``channel_dir_override`` / ``media_uri``
    exist for the local-file recovery path (plan rev 4): the caller can route
    artifacts to a different folder/prefix (e.g. a canonical scan-generated prefix
    when G2 dedup fires) and feed Gemini a Files API URI while keeping
    ``video["url"]`` canonical.
    """
    if source not in ("video", "transcript"):
        raise ValueError(f"Invalid source={source!r}. Expected 'video' or 'transcript'.")

    resolved_prefix = prefix if prefix is not None else video_file_prefix(video)
    resolved_channel_dir = channel_dir_override if channel_dir_override is not None else output_dir / channel_name
    resolved_channel_dir.mkdir(parents=True, exist_ok=True)

    mindmap_path = resolved_channel_dir / f"{resolved_prefix}.mindmap.md"
    meta_path = resolved_channel_dir / f"{resolved_prefix}.meta.json"

    if mindmap_path.exists() and not force:
        return resolved_prefix, "skipped (exists)"

    try:
        if source == "transcript":
            resolved_transcript = (
                transcript_path
                if transcript_path is not None
                else resolved_channel_dir / f"{resolved_prefix}.transcript.md"
            )
            if not resolved_transcript.exists():
                raise FileNotFoundError(f"transcript not found at {resolved_transcript}")
            transcript_text = resolved_transcript.read_text(encoding="utf-8")

            # Detect partial-source state from the sibling meta.json (best effort:
            # a missing meta or missing field is treated as healthy). The marker
            # is additive — readers who don't care can ignore the comment, but
            # anyone auditing mindmap quality has a clear breadcrumb back to the
            # source. Healthy values are 'ok' (chunked + scan single-shot
            # writers, line 1542) and 'complete' (single-call success writer,
            # line 2419) — both populated by paths that produced a full
            # transcript. Anything else (notably 'partial' from salvage) signals
            # the upstream had to recover and the mindmap inherits the gap.
            transcript_status = "ok"
            if meta_path.exists():
                # Best-effort: an unreadable meta leaves the healthy default in
                # place rather than raising. Routed through the shared helper
                # (issue #124) so an OSError - not just a JSONDecodeError - is
                # survivable too, and so the corrupt file gets named in the log.
                transcript_status = _read_meta_best_effort(meta_path, raise_on_os_error=False).get(
                    "transcript_status", "ok"
                )

            result = call_gemini_text(
                client,
                types,
                prompt_text + "\n\n# TRANSCRIPT TO PROCESS\n\n" + transcript_text,
                model,
                response_mime_type="text/plain",
                on_response=lambda r: log_usage_metadata(r, "mindmap"),
            )

            header_lines = [
                f"<!-- video: {video['url']} -->",
                f"<!-- title: {video['title']} -->",
                f"<!-- published: {video['published']} -->",
            ]
            if transcript_status not in _HEALTHY_TRANSCRIPT_STATUSES:
                header_lines.append(f"<!-- source: partial transcript ({transcript_status}) -->")
            header = "\n".join(header_lines) + "\n\n"
        else:
            effective_media_uri = media_uri if media_uri is not None else video["url"]
            # Default to LOW media resolution: mirrors the chunked-transcript
            # path's pattern (line 1466) and applies the same issue #58 Gate 3
            # finding — for our prompt's needs (theme/concept extraction from
            # talking-head + occasional slide content), LOW yields equivalent
            # quality at 3x lower input-token cost. HIGH would re-introduce
            # Gemini's 1M-token ceiling on hour-long videos. Override via the
            # --media-resolution CLI flag for the rare case where the prompt
            # depends on reading fine on-screen text.
            effective_media_resolution = (
                media_resolution if media_resolution is not None else types.MediaResolution.MEDIA_RESOLUTION_LOW
            )
            # Issue #119 confabulation guard: capture the prompt-token count off
            # the same callback that logs it, so we can refuse a prompt == 0
            # response before anything is written to disk.
            # int | None per field: log_usage_metadata reports an unreadable
            # count as None so SDK drift cannot masquerade as a reported zero
            # (issue #125). The guard below compares == 0 for exactly that reason.
            usage_capture: dict[str, int | None] = {}

            def _on_resp(r: object) -> None:
                counts = log_usage_metadata(r, "mindmap")
                if counts is not None:
                    usage_capture.clear()
                    usage_capture.update(counts)

            result = call_gemini(
                client,
                types,
                effective_media_uri,
                prompt_text,
                model,
                fps=fps,
                media_resolution=effective_media_resolution,
                on_response=_on_resp,
            )
            # prompt == 0 means Gemini ingested zero video tokens (gated,
            # unfetchable, or a future premiere) and generated a plausible
            # mindmap from priors. The header below is built locally, so the
            # artifact would carry the correct video/title/published stamp and
            # pass every downstream check on its way into concepts extraction,
            # taxonomy.json, and the vector index. Same invariant the transcript
            # path has enforced since issue #60. Compare to 0 explicitly: the
            # count is absent when usage_metadata was unreadable, and missing
            # data must never be flagged as a confabulation.
            if usage_capture.get("prompt") == 0:
                # Keep the discarded text for forensics, mirroring the transcript
                # path's `.transcript.raw.txt` sidecar: the fabrication is worth
                # inspecting (it is how the failure mode was diagnosed) but must
                # never sit under the `.mindmap.md` name that concepts, taxonomy,
                # and the index read. No parsing - raw bytes only.
                raw_path = resolved_channel_dir / f"{resolved_prefix}.mindmap.raw.txt"
                raw_path.write_text(result or "", encoding="utf-8")
                log.warning(
                    "  %s: confabulation guard tripped - Gemini reported prompt=0 (no video ingested); discarded to %s",
                    resolved_prefix,
                    raw_path.name,
                )
                raise RuntimeError("confabulation guard: Gemini prompt=0 (no video ingested)")
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
            "mindmap_source": source,
        }
        if source == "transcript" and transcript_status not in _HEALTHY_TRANSCRIPT_STATUSES:
            meta_fields["mindmap_source_status"] = "partial"
        if prompt_name:
            meta_fields["prompt"] = prompt_name
        # Forward-fix: persist duration_seconds when scan-time enrichment supplied it,
        # so future prune-shorts runs avoid re-fetching from YouTube. Optional —
        # legacy metas without this field still classify via on-demand fallback.
        duration_seconds = _parse_iso8601_duration(video.get("duration_iso"))
        if duration_seconds is not None:
            meta_fields["duration_seconds"] = duration_seconds
        update_meta(meta_path, meta_fields, "scan")

        return resolved_prefix, "done"

    except Exception as e:
        # Record failure in meta.json (also merge-safe). The read is best-effort
        # (issue #124): a corrupt meta raising here would propagate out of this
        # handler and mask `e`, the failure this block exists to record.
        resolved_channel_dir.mkdir(parents=True, exist_ok=True)
        meta: dict = _read_meta_best_effort(meta_path, raise_on_os_error=False)
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
        return resolved_prefix, f"error: {e}"


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
    """Convert MM:SS or H:MM:SS to seconds for sorting.

    Issue #58 Gate 3: Gemini 2.5 Pro can emit fractional seconds (e.g.
    "00:00.040" for 40-millisecond precision) where Flash emits whole-second
    "00:00". Use int(float(...)) on the seconds field to strip the decimal,
    and wrap the whole parse in try/except so any future timestamp variant
    falls through to 0 instead of crashing the merge sort.
    """
    parts = ts.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        elif len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
    except (ValueError, TypeError):
        return 0
    return 0


def parse_time_to_seconds(value: str) -> int:
    """Parse time string to seconds. Accepts 'MM:SS', 'HH:MM:SS', or raw seconds.

    Examples: '05:30' -> 330, '01:15:45' -> 4545, '330' -> 330.
    """
    if not value or not value.strip():
        raise ValueError("Empty time value")
    stripped = value.strip()
    parts = stripped.split(":")
    if len(parts) == 1:
        return int(parts[0])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    raise ValueError(f"Invalid time format: {value!r}. Use MM:SS, HH:MM:SS, or raw seconds.")


FILE_ACTIVE_POLL_SECONDS = 5
FILE_ACTIVE_TIMEOUT_SECONDS = 600


def upload_local_video(client, path: Path) -> str:
    """Upload a local video to Gemini Files API, return the file URI.

    Polls until the file reaches ACTIVE state (video files require server-side
    processing after upload). Raises TimeoutError after FILE_ACTIVE_TIMEOUT_SECONDS.
    Does not manage lifecycle - Gemini auto-deletes uploads after 48 hours.
    """
    if not path.exists():
        raise FileNotFoundError(f"Video file not found: {path}")
    if path.suffix.lower() not in {".mp4", ".mov", ".webm", ".mkv", ".avi"}:
        log.warning("Unusual video extension: %s (expected .mp4/.mov/.webm)", path.suffix)
    log.info("Uploading video: %s (%.1f MB)", path.name, path.stat().st_size / 1024 / 1024)
    file_obj = client.files.upload(file=path)
    log.info("Uploaded: %s (processing...)", file_obj.uri)

    # Poll until file is ACTIVE (videos need server-side processing)
    waited = 0
    while waited < FILE_ACTIVE_TIMEOUT_SECONDS:
        file_obj = client.files.get(name=file_obj.name)
        state = getattr(file_obj.state, "name", str(file_obj.state))
        if state == "ACTIVE":
            log.info("File ready: %s", file_obj.uri)
            return file_obj.uri
        if state == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {file_obj.uri}")
        time.sleep(FILE_ACTIVE_POLL_SECONDS)
        waited += FILE_ACTIVE_POLL_SECONDS
        log.info("  ...still processing (%ds, state=%s)", waited, state)
    raise TimeoutError(f"File did not become ACTIVE within {FILE_ACTIVE_TIMEOUT_SECONDS}s: {file_obj.uri}")


def isolate_json(text: str) -> str:
    """Best-effort JSON isolation: strip fences, trim prose, find outermost JSON."""
    cleaned = text.strip()
    # Strip markdown code fences
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove opening fence (```json or ```)
        lines = lines[1:]
        # Remove closing fence
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    # Find outermost { ... } or [ ... ]
    for opener, closer in [("{", "}"), ("[", "]")]:
        start = cleaned.find(opener)
        if start == -1:
            continue
        end = cleaned.rfind(closer)
        if end > start:
            return cleaned[start : end + 1]
    return text


def try_parse_transcript_json(text: str | None) -> tuple[dict | list | None, str | None]:
    """Attempt to parse transcript JSON: direct first, then after isolation.
    If the parsed value is the Pro task-wrapper shape, normalize to a flat
    envelope so downstream `merge_transcript_json` sees a valid dict (issue #45).

    None or empty input is treated as a parse failure (returns parse_error)
    rather than raising — Gemini's thinking-budget overflow can produce
    `candidates=0` and `text=None`, and the caller should fall through to
    the salvage path with a clear error rather than crash with a TypeError.
    """
    # Defensive: empty / None / non-string input cannot be valid JSON.
    if not text:
        return None, "Empty response from Gemini (likely thinking-budget overflow: candidates=0)"
    # Try direct parse
    try:
        parsed = json.loads(text)
        return _wrapper_to_envelope_dict(parsed) or parsed, None
    except (json.JSONDecodeError, ValueError):
        pass
    # Try after isolation
    isolated = isolate_json(text)
    if isolated != text:
        try:
            parsed = json.loads(isolated)
            return _wrapper_to_envelope_dict(parsed) or parsed, None
        except (json.JSONDecodeError, ValueError) as e:
            return None, str(e)
    return None, "No valid JSON found"


_KNOWN_TASK_KEYS = ("transcripts", "screen_content", "speakers")
_CYRILLIC_BEFORE_TEXT_KEY = re.compile(r"\s*[Ѐ-ӿ]+\s*(\"text\"\s*:)")
_CYRILLIC_BEFORE_VALUE = re.compile(r"\s*[Ѐ-ӿ]+\s+(\"[^\"]+\")")


def _strip_cyrillic_for_structure(text: str) -> str:
    """Strip Cyrillic-token intrusions that block JSON.loads of a wrapper.

    Scoped helper for `_normalize_task_wrapper` only - issue #45 rejects a
    global pre-strip because verbatim foreign content can be legitimate.
    """
    fixed, _ = _CYRILLIC_BEFORE_TEXT_KEY.subn(r" \1", text)
    fixed, _ = _CYRILLIC_BEFORE_VALUE.subn(r' "text": \1', fixed)
    return fixed


def _wrapper_to_envelope_dict(parsed) -> dict | None:
    """If `parsed` matches one of Pro's known wrapper shapes, rebuild a flat
    envelope dict. Otherwise return None so callers can pass the original
    parsed value through.

    Recognized shapes (any list-of-dicts where the items match):
      A. Task-wrapper (issue #45): ``[{"task": "transcripts", "output": [...]}, ...]``
         where ``task`` is in ``_KNOWN_TASK_KEYS``.
      B. Single-key wrapper (observed 2026-05-02 on long single-shot transcripts):
         ``[{"transcripts": [...]}, {"screen_content": [...]}, {"speakers": [...]}]``
         where each dict has exactly one key from ``_KNOWN_TASK_KEYS`` mapping
         to a list. Pro emits this when it skips the explicit ``task``/``output``
         scaffolding but still produces the three-task structure.

    Used by both `try_parse_transcript_json` (full-parse path) and
    `_normalize_task_wrapper` (text-level salvage path), so a wrapper response
    is normalized whether or not it also has a Cyrillic intrusion that breaks
    the full parse.
    """
    if not (isinstance(parsed, list) and parsed):
        return None

    envelope: dict[str, list] = {k: [] for k in _KNOWN_TASK_KEYS}

    # Shape A: task-wrapper with explicit `task` field.
    known_items = [it for it in parsed if isinstance(it, dict) and it.get("task") in _KNOWN_TASK_KEYS]
    if known_items:
        for item in known_items:
            task = item["task"]
            output = item.get("output", [])
            if isinstance(output, list):
                envelope[task] = output
        return envelope

    # Shape B: list of single-key dicts where the key is one of the known tasks.
    # Each dict must have exactly one key from _KNOWN_TASK_KEYS mapping to a list.
    matched_any = False
    for item in parsed:
        if not isinstance(item, dict) or len(item) != 1:
            continue
        key = next(iter(item.keys()))
        value = item[key]
        if key in _KNOWN_TASK_KEYS and isinstance(value, list):
            envelope[key] = value
            matched_any = True
    if matched_any:
        return envelope

    return None


def _normalize_task_wrapper(text: str) -> str:
    """Detect Pro's task-wrapper shape in a raw text response and rewrite it
    into the flat envelope salvage's regex scan expects.

    Returns the input unchanged when the wrapper shape is absent or the
    rebuild fails. Composition with downstream salvage is monotone: a
    wrapper-free input is unchanged (no rewrite); a wrapper input is at
    least as recoverable as today (which is zero). See issue #45.
    """
    for candidate in (text, _strip_cyrillic_for_structure(text)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        envelope = _wrapper_to_envelope_dict(parsed)
        if envelope is None:
            return text
        return json.dumps(envelope)
    return text


def salvage_transcript_sections(text: str) -> tuple[dict, str | None]:
    """Try to recover valid JSON arrays for transcripts/screen_content/speakers."""
    text = _normalize_task_wrapper(text)
    result: dict[str, list] = {"transcripts": [], "screen_content": [], "speakers": []}
    warning = None

    for key in ("transcripts", "screen_content", "speakers"):
        # Find "key": [ and try to extract the array
        pattern = f'"{key}"\\s*:\\s*\\['
        match = re.search(pattern, text)
        if not match:
            continue
        arr_start = match.end() - 1  # Position of the [
        # Try progressively shorter substrings to find valid JSON array
        depth = 0
        last_valid_end = None
        for i in range(arr_start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    last_valid_end = i + 1
                    break
        # If we found a balanced array, try to parse it
        if last_valid_end:
            try:
                result[key] = json.loads(text[arr_start:last_valid_end])
                continue
            except (json.JSONDecodeError, ValueError):
                pass
        # Fallback: try to parse entries one at a time from the array content
        if arr_start < len(text):
            entries = []
            # Find individual objects within the array
            obj_depth = 0
            obj_start = None
            for i in range(arr_start + 1, len(text)):
                if text[i] == "{" and obj_depth == 0:
                    obj_start = i
                    obj_depth = 1
                elif text[i] == "{":
                    obj_depth += 1
                elif text[i] == "}" and obj_depth > 0:
                    obj_depth -= 1
                    if obj_depth == 0 and obj_start is not None:
                        try:
                            entry = json.loads(text[obj_start : i + 1])
                            entries.append(entry)
                        except (json.JSONDecodeError, ValueError):
                            pass
                        obj_start = None
                elif text[i] == "]" and obj_depth == 0:
                    break
            if entries:
                result[key] = entries

    if result["transcripts"] or result["screen_content"]:
        warning = "Salvaged from malformed JSON response"
    return result, warning


def _build_captions_transcript_body(captions: CaptionsResult) -> str:
    """Render a CaptionsResult as the body of a video-intel transcript.md.

    Speech-only: one ``[MM:SS] "text"`` line per caption snippet, no SCREEN
    sections and no speaker diarization (the caption track carries neither).
    Overlapping auto-caption cues (rolling-window ASR repeats the same words
    across adjacent cues) are de-duplicated to one line per start-second,
    keeping the longest text, to cut repeated phrases. Pure function.

    Known limitation (accepted for this lower-fidelity artifact): when two
    distinct cues round to the same second, only the longer is kept, so a
    short tail cue sharing a second with a longer one is dropped. This trades
    a little content loss for far fewer duplicate phrases; the artifact is
    already flagged speech-only and is replaceable via ``process --file``.
    """
    seen: dict[int, tuple[int, str]] = {}
    for start, text in captions.snippets:
        clean = " ".join(text.split())
        if not clean:
            continue
        key = round(start)
        if key not in seen or len(clean) > len(seen[key][1]):
            seen[key] = (key, clean)
    lines = []
    for start, clean in (seen[k] for k in sorted(seen)):
        mm, ss = divmod(start, 60)
        lines.append(f'[{mm:02d}:{ss:02d}] "{clean}"')
    return "\n".join(lines)


def _write_transcript_md(
    path: Path,
    video: dict,
    fused: str,
    *,
    status: str = "Complete",
    recovery: str | None = None,
    warning: str | None = None,
    transcript_source: str | None = None,
) -> None:
    """Write a transcript markdown file with header and optional warning block.

    ``transcript_source`` (issue #60) records provenance in the header. When it
    is the captions source, a banner + reader note flag the lower fidelity
    (speech-only, no SCREEN / diarization) so the file is never mistaken for a
    Gemini-multimodal transcript.
    """
    header = (
        f"# Transcript: {video['title']}\n\n"
        f"**Source:** {video['url']}\n"
        f"**Published:** {video['published']}\n"
        f"**Processed:** {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"**Status:** {status}\n"
    )
    if transcript_source:
        header += f"**Transcript source:** {transcript_source}\n"
    if recovery:
        header += f"**Recovery:** {recovery}\n"
    header += "\n"
    if transcript_source == TRANSCRIPT_SOURCE_CAPTIONS:
        header += (
            "<!-- source: youtube-auto-captions; no SCREEN sections, no speaker "
            "diarization. Speech-only transcript from the public caption track. "
            "Replace via `process --file` for full fidelity. -->\n\n"
            "> NOTE: Derived from YouTube auto-captions, not Gemini multimodal - "
            "spoken content only (no on-screen text, slides, code, or speaker IDs).\n\n"
        )
    header += "---\n\n"

    body = ""
    if warning:
        body += (
            f"## Warning: Incomplete transcript\n\n"
            f"{warning}\n\n"
            f"- Speech coverage may stop early.\n"
            f"- `SCREEN` sections may be missing or incomplete.\n"
            f"- Speaker identification may be incomplete.\n\n"
            f"---\n\n"
        )
    body += fused

    # Ensure the destination folder exists before the atomic tmp+replace write.
    # Every other artifact writer in this file mkdirs its own channel dir; this
    # one used to rely on its callers having done so, which held only because a
    # Gemini attempt (and its meta handling) always ran first. Issue #120's
    # captions-first routing made _try_captions_transcript the FIRST writer for
    # a channel, so a brand-new channel folder had nobody to create it and the
    # tmp write raised FileNotFoundError. Fixed here, at the shared seam, so
    # every caller is covered rather than just the one that surfaced it.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".md.tmp")
    tmp_path.write_text(header + body, encoding="utf-8")
    tmp_path.replace(path)


def _record_transcript_error(meta_path: Path, error: str) -> None:
    """Record last_error on an existing meta.json without clobbering other fields."""
    if not meta_path.exists():
        return
    meta = _read_meta_best_effort(meta_path, raise_on_os_error=False)
    meta["last_error"] = error
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def _record_concepts_error(meta_path: Path, video: dict, channel_dir: Path, error: str) -> None:
    """Persist a concepts-step failure into meta.json (issue #129).

    Before this existed, ``process_concepts`` returned ``"error: ..."`` and wrote
    NOTHING. That made a concepts failure the only pipeline failure with no
    durable trace anywhere: no artifact, no meta field, and ``is_processed()``
    never looks at concepts, so the video stayed "processed" forever and simply
    never entered ``taxonomy.json`` or the search index. The transcript path has
    had ``transcript_status``/``last_error`` for exactly this reason.

    Three contracts this writer has to honor:

    * **Issue #66** - stamp full identity. A meta carrying only
      ``{concepts_status: ...}`` is one ``_load_video_id_index`` skips, which
      re-queues the video for a full re-transcribe. The failure record must not
      cost more than the failure did.
    * **Issue #124** - this is an error path, so the read is
      ``_read_meta_best_effort(..., raise_on_os_error=False)``. A corrupt or
      unreadable meta must never raise from inside the handler that is trying to
      preserve the error.
    * **Not** ``update_meta``. That is the shared SUCCESS-path writer: it clears
      ``last_error`` and appends to ``modes_completed``, so routing a failure
      through it would erase the record it is meant to leave and mark the step
      done.

    ``concepts_status`` is the durable per-stage field. ``last_error`` is written
    too but is best-effort only: it is a single field shared across stages, so a
    concepts failure can overwrite a transcript failure's message.

    No separate timestamp field: a later success overwrites ``concepts_status``
    with ``"ok"`` but could not delete a ``concepts_failed_at``, and a stale
    failure timestamp on a healthy video reads worse than no timestamp at all.
    """
    if not meta_path.exists():
        # NEVER create a meta that did not exist - mirrors _record_transcript_error.
        # ce-code-review (adversarial, PR #136) executed the alternative: callers
        # that do not pass an explicit `prefix` (cmd_concepts) let process_concepts
        # recompute one from the current title, so on a title-rotated video a
        # single dropped socket wrote a SECOND meta claiming the same video_id -
        # manufacturing a dedupe group that never existed, where a meta with no
        # `processed` key can lose the tie-break to the phantom. Recording a
        # cheap failure must not corrupt identity; if there is no meta to
        # annotate, the log line is the record.
        log.warning("  concepts failure for %s not recorded: no meta.json at %s", video.get("video_id"), meta_path)
        return
    meta = _read_meta_best_effort(meta_path, raise_on_os_error=False)
    meta.update(_transcript_identity_fields(video, channel_dir))
    meta["concepts_status"] = f"error: {error}"
    meta["last_error"] = f"concepts: {error}"
    try:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except (OSError, ValueError) as exc:
        # An error-path writer that raises destroys the error it exists to
        # preserve - the same principle as issue #124's read side. Uncaught,
        # this would propagate through cmd_scan's concepts loop and abort the
        # scan before its failure summary ever printed.
        log.warning("  could not record concepts failure in %s (%s)", meta_path, exc)


def _transcript_identity_fields(video: dict, channel_dir: Path) -> dict:
    """Identity fields every transcript meta.json must carry (issue #66).

    The single-shot and captions transcript writers used to persist a meta with
    only ``{processed, transcript_status, ...}`` - no ``video_id`` - so when the
    transcript loop is the first writer (inverted ordering, #54) it left an
    identity-less meta that ``_load_video_id_index`` skips, breaking idempotency
    (the video is re-transcribed every scan). This mirrors the complete-meta
    shape the scan/mindmap and chunked-transcript paths already write
    (scripts/video_intel.py: the chunked ``meta_fields`` block).
    """
    fields = {
        "video_url": video.get("url"),
        "video_id": video.get("video_id"),
        "channel": channel_dir.name,
        "title": video.get("title"),
        "published": video.get("published"),
    }
    # Drop falsy values so a re-stamp can only ADD identity, never downgrade a
    # previously-good field to None/"" - e.g. local-file flows where video["url"]
    # may be empty (ce-data-integrity review, #66). channel is always truthy.
    return {k: v for k, v in fields.items() if v}


def _identity_from_transcript_header(transcript_path: Path) -> dict | None:
    """Reconstruct identity fields from a ``.transcript.md`` header (issue #66 backfill).

    Headers written by ``_write_transcript_md`` carry ``# Transcript: {title}``,
    ``**Source:** {url}``, and ``**Published:** {date}``. Returns the identity
    fields (``video_url``, ``video_id``, ``channel``, ``title``, ``published``)
    or ``None`` when no Source URL with a parseable 11-char video id is present.
    """
    try:
        text = transcript_path.read_text(encoding="utf-8")[:4000]
    except (OSError, UnicodeDecodeError):
        return None
    src_m = re.search(r"^\*\*Source:\*\*\s*(\S+)", text, re.MULTILINE)
    if not src_m:
        return None
    url = src_m.group(1).strip()
    # Exactly 11 id chars with a right boundary: an over-long token after v=
    # (e.g. a non-canonical URL) fails the match instead of being truncated to a
    # wrong id (ce-data-integrity + Codex review, #66).
    vid_m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])", url)
    if not vid_m:
        return None
    fields = {
        "video_url": url,
        "video_id": vid_m.group(1),
        "channel": transcript_path.parent.name,
    }
    title_m = re.search(r"^# Transcript:\s*(.+)$", text, re.MULTILINE)
    if title_m:
        fields["title"] = title_m.group(1).strip()
    pub_m = re.search(r"^\*\*Published:\*\*\s*(\S+)", text, re.MULTILINE)
    if pub_m:
        fields["published"] = pub_m.group(1).strip()
    return fields


def _try_captions_transcript(
    video: dict,
    transcript_path: Path,
    meta_path: Path,
    prefix: str,
    *,
    reason: str | None = None,
    start_offset: int | None = None,
    end_offset: int | None = None,
    force: bool = False,
) -> tuple[str, str] | None:
    """Build a transcript from the YouTube English caption track (issue #60).

    Returns ``(prefix, status)`` on success, or ``None`` when no usable captions
    are available so the caller can decide what to do next (the ``yt-captions``
    source records an error; the ``auto`` source lets the normal Gemini failure
    path run). Writes a speech-only transcript flagged
    ``transcript_source: youtube_captions`` so it is never mistaken for a
    Gemini-multimodal transcript. ``reason`` records why captions were used
    (e.g. the Gemini error that triggered the auto-failover).

    ``start_offset``/``end_offset`` (seconds) clip the caption snippets to
    ``[start, end)`` so ``--start``/``--end`` segments behave like the Gemini
    path instead of silently transcribing the whole video.

    Idempotency lives HERE, not in the callers. An existing transcript is
    returned as ``None`` (no write, no caption fetch) unless ``force``. This
    used to be safe to leave to callers because captions only ever ran after a
    Gemini attempt that had already made the exists-check; issue #120's
    captions-first routing moved it ahead of that check on the chunked paths,
    where an unguarded call would silently replace a good multimodal transcript
    (SCREEN sections, diarization) with a speech-only one on a plain re-run.
    Every call site threads its own ``force`` so the policy is stated once.
    """
    video_id = video.get("video_id")
    if not video_id:
        return None
    if transcript_path.exists() and not force:
        log.info("  %s: transcript already on disk; leaving it alone (pass --force to replace)", prefix)
        return None
    captions = fetch_english_captions(video_id)
    if captions is None:
        return None
    snippets = captions.snippets
    if start_offset is not None or end_offset is not None:
        lo = start_offset or 0
        snippets = [(s, t) for (s, t) in snippets if s >= lo and (end_offset is None or s < end_offset)]
    body = _build_captions_transcript_body(CaptionsResult(snippets, captions.is_generated, captions.language))
    if not body.strip():
        log.info("  %s: caption track present but empty after dedup/clip - not usable", prefix)
        return None
    _write_transcript_md(
        transcript_path,
        video,
        body,
        status="Captions (YouTube auto-generated)" if captions.is_generated else "Captions (manual)",
        transcript_source=TRANSCRIPT_SOURCE_CAPTIONS,
    )
    fields = {
        **_transcript_identity_fields(video, meta_path.parent),
        "processed": datetime.now(UTC).isoformat(),
        "transcript_status": "complete",
        "transcript_source": TRANSCRIPT_SOURCE_CAPTIONS,
        "captions_is_generated": bool(captions.is_generated),
    }
    if reason:
        fields["transcript_failover_reason"] = reason
    update_meta(meta_path, fields, "transcript")
    log.info(
        "  %s: transcript from YouTube captions (%d cues, %s)",
        prefix,
        len(snippets),
        "auto-gen" if captions.is_generated else "manual",
    )
    return prefix, "done (captions)"


def process_transcript(
    client,
    types,
    video,
    prompt_text,
    model,
    channel_dir: Path,
    prefix: str,
    *,
    force=False,
    start_offset: int | None = None,
    end_offset: int | None = None,
    media_uri: str | None = None,
    media_resolution=None,
    transcript_source: str = "gemini",
    transcript_timeout_seconds: int = TRANSCRIPT_TIMEOUT_DEFAULT,
    livestream_captions_first: bool = False,
):
    """Generate a fused transcript for a single video with layered JSON resilience.

    Output paths are derived from channel_dir + prefix:
    - {channel_dir}/{prefix}.transcript.md
    - {channel_dir}/{prefix}.meta.json
    - {channel_dir}/{prefix}.transcript.raw.txt (on parse failure)

    Optional start_offset/end_offset (in seconds) pass through to Gemini for
    segment clipping. Applies on initial call and any retries.

    Optional media_uri overrides what is sent to Gemini as the media source
    (e.g. a Gemini Files API URI for locally-uploaded MP4s) while video["url"]
    stays the canonical YouTube URL used in the transcript header and meta.json.
    When media_uri is not set, video["url"] is used for both.

    ``livestream_captions_first`` (issue #120) is the ALREADY-ADJUDICATED
    routing decision for a completed livestream VOD, not the raw classification:
    callers combine "is this a VOD" with ``livestream_captions_first_applies``
    (which honors an explicit ``transcript_source: gemini``) and pass the result.
    When true, captions are tried BEFORE any Gemini call and a captionless VOD
    gets exactly ONE guarded attempt instead of two. When false - including a
    VOD whose operator explicitly asked for ``gemini`` - this function behaves
    exactly as it did before issue #120, full parse-retry budget included.
    """
    channel_dir.mkdir(parents=True, exist_ok=True)

    transcript_path = channel_dir / f"{prefix}.transcript.md"
    meta_path = channel_dir / f"{prefix}.meta.json"

    if transcript_path.exists() and not force:
        return prefix, "skipped (exists)"

    # Issue #60: explicit captions-only source skips Gemini entirely (cheap,
    # speech-only). Fails when no captions exist - the caller chose this source.
    if transcript_source == "yt-captions":
        captioned = _try_captions_transcript(
            video, transcript_path, meta_path, prefix, start_offset=start_offset, end_offset=end_offset, force=force
        )
        if captioned is not None:
            return captioned
        _record_transcript_error(meta_path, "no English captions available (transcript_source=yt-captions)")
        return prefix, "error: no captions available (yt-captions)"

    # Issue #120: completed-livestream VODs go to captions FIRST, whatever the
    # channel's transcript_source says. Empirically their YouTube-URI ingestion
    # breaks at a wholly different rate than regular uploads (10 of 22 vs 0 of
    # 377 in the 2026-07-24 corpus sample), either hard-failing with a generic
    # 400 INVALID_ARGUMENT or ingesting prompt=0 and confabulating. Captions
    # cost nothing and are unaffected by whatever makes the VOD unfetchable.
    captions_already_tried = False
    if livestream_captions_first:
        captions_already_tried = True
        captioned = _try_captions_transcript(
            video,
            transcript_path,
            meta_path,
            prefix,
            reason=LIVESTREAM_CAPTIONS_FIRST_REASON,
            start_offset=start_offset,
            end_offset=end_offset,
            force=force,
        )
        if captioned is not None:
            return captioned
        log.info(
            "  %s: livestream VOD with no caption track - allowing one guarded Gemini attempt",
            prefix,
        )

    effective_media_uri = media_uri if media_uri is not None else video["url"]
    # Default to LOW media resolution: same justification as process_mindmap
    # (issue #58 Gate 3 — LOW = same quality at 3x lower input-token cost for
    # talking-head + slide content). Without this default, single-shot transcript
    # calls hit Gemini's 1M-token cap on hour-long videos. The chunked-transcript
    # path (line 1466) already uses LOW; this brings the single-shot path into
    # parity. CLI override flows from the --media-resolution flag through cmd_*.
    effective_media_resolution = (
        media_resolution if media_resolution is not None else types.MediaResolution.MEDIA_RESOLUTION_LOW
    )
    # Cap thinking budget. Without this cap, Gemini 2.5 Pro can stochastically
    # burn the entire output token budget on internal thinking, returning
    # candidates=0 and text=None — empirically observed on a 91-min video that
    # produced thoughts=65533, candidates=0. Mirrors the chunked-transcript path's
    # mitigation (line 1493): same model-aware helper, same justification (issue
    # #58 Gate 2). Transcription is mechanical (diarize + log on-screen content),
    # not analytical — minimum thinking is the right setting.
    transcript_thinking_config = _make_thinking_config_for_transcript(types, model)
    # Issue #60 confabulation guard: capture the prompt-token count off the usage
    # callback so we can detect prompt == 0 (Gemini ingested no video tokens).
    usage_capture: dict[str, int | None] = {}
    finish_capture: dict[str, str | None] = {}

    def _on_resp(r):
        counts = log_usage_metadata(r, "transcript")
        if counts is not None:
            usage_capture.clear()
            usage_capture.update(counts)
        # Issue #128: the output-cap check needs the finish reason too, because
        # candidates_token_count can be unreadable on a response that really did
        # truncate (Gemini can also reach MAX_TOKENS with thinking consuming the
        # budget, leaving the candidates count absent entirely).
        finish_capture["reason"] = _finish_reason_of(r)

    # Issue #120: a livestream VOD gets exactly ONE Gemini call. The parse retry
    # exists for stochastic JSON malformation on a healthy ingest; when the URI
    # itself is what Gemini cannot fetch, a second call fails identically and
    # only doubles the bill.
    parse_retry_limit = 0 if livestream_captions_first else TRANSCRIPT_PARSE_RETRY_LIMIT

    for attempt in range(1 + parse_retry_limit):
        usage_capture.clear()
        finish_capture.clear()
        try:
            # Issue #74: hard wall-clock cap so a hung call raises instead of
            # deadlocking. TranscriptTimeout is an Exception, so it falls into the
            # handler below and (under transcript_source=auto) the captions failover.
            raw = _run_with_timeout(
                lambda: call_gemini(
                    client,
                    types,
                    effective_media_uri,
                    prompt_text,
                    model,
                    response_json=True,
                    start_offset=start_offset,
                    end_offset=end_offset,
                    media_resolution=effective_media_resolution,
                    thinking_config=transcript_thinking_config,
                    on_response=_on_resp,
                ),
                transcript_timeout_seconds,
            )
        except Exception as e:
            # Issue #60: on auto, try the captions fallback before recording error.
            # Issue #120: skipped when captions-first already proved there is no
            # caption track - re-fetching cannot change the answer.
            if transcript_source == "auto" and not captions_already_tried:
                fb = _try_captions_transcript(
                    video,
                    transcript_path,
                    meta_path,
                    prefix,
                    reason=f"gemini error: {e}",
                    start_offset=start_offset,
                    end_offset=end_offset,
                    force=force,
                )
                if fb is not None:
                    return fb
            _record_transcript_error(meta_path, str(e))
            return prefix, f"error: {e}"

        # Issue #60 confabulation guard: prompt == 0 means Gemini ingested zero
        # video tokens (gated / future-premiere / unfetchable) and confabulated a
        # stub. Never write that as a healthy transcript. The count is absent
        # (None) when usage_metadata was unreadable - do not flag on missing data.
        if usage_capture.get("prompt") == 0:
            log.warning(
                "  %s: confabulation guard tripped - Gemini reported prompt=0 (no video ingested); discarding",
                prefix,
            )
            if transcript_source == "auto" and not captions_already_tried:
                fb = _try_captions_transcript(
                    video,
                    transcript_path,
                    meta_path,
                    prefix,
                    reason="gemini prompt=0 (confabulation)",
                    start_offset=start_offset,
                    end_offset=end_offset,
                    force=force,
                )
                if fb is not None:
                    return fb
            _record_transcript_error(meta_path, "confabulation guard: Gemini prompt=0 (no video ingested)")
            return prefix, "error: confabulation guard (prompt=0)"

        # Layer 1: try full parse (direct + isolation)
        raw_json, parse_error = try_parse_transcript_json(raw)

        if raw_json is not None:
            # Observability: log the parsed JSON's shape so an empty fused body
            # is diagnosable without re-running. Catches the silent regression
            # where Gemini returns a wrapper or unknown shape that parses but
            # produces zero entries (see docs/solutions/integration-issues/
            # gemini-pro-task-wrapper-and-cyrillic-intrusions-20260426.md).
            if isinstance(raw_json, dict):
                log.info(
                    "  transcript JSON parsed: dict keys=%s, transcripts=%d, screen_content=%d, speakers=%d",
                    sorted(list(raw_json.keys()))[:8],
                    len(raw_json.get("transcripts", [])),
                    len(raw_json.get("screen_content", [])),
                    len(raw_json.get("speakers", [])),
                )
            elif isinstance(raw_json, list):
                first_keys = (
                    sorted(list(raw_json[0].keys()))[:8] if raw_json and isinstance(raw_json[0], dict) else None
                )
                log.info(
                    "  transcript JSON parsed: list of %d items, first_keys=%s",
                    len(raw_json),
                    first_keys,
                )
            else:
                log.info("  transcript JSON parsed: type=%s", type(raw_json).__name__)
            # Full parse succeeded - write transcript, no raw sidecar
            fused = merge_transcript_json(raw_json, {})
            _write_transcript_md(transcript_path, video, fused, transcript_source=TRANSCRIPT_SOURCE_GEMINI)
            update_meta(
                meta_path,
                {
                    **_transcript_identity_fields(video, channel_dir),
                    "processed": datetime.now(UTC).isoformat(),
                    "transcript_status": "complete",
                    "transcript_source": TRANSCRIPT_SOURCE_GEMINI,
                },
                "transcript",
            )
            return prefix, "done"

        # Full parse failed - save raw sidecar for forensics
        raw_suffix = ".transcript.raw.txt" if attempt == 0 else f".transcript.raw.{attempt + 1}.txt"
        raw_path = channel_dir / f"{prefix}{raw_suffix}"
        raw_path.write_text(raw or "", encoding="utf-8")
        log.warning("  %s: JSON parse failed (attempt %d): %s", prefix, attempt + 1, parse_error)

        # Layer 2: try salvage
        salvaged, salvage_warning = salvage_transcript_sections(raw or "")
        speech_count = len(salvaged.get("transcripts", []))

        if speech_count >= SALVAGE_MIN_SPEECH_ENTRIES:
            # Salvage produced usable content - write partial transcript
            fused = merge_transcript_json(salvaged, {})
            _write_transcript_md(
                transcript_path,
                video,
                fused,
                status="Partial transcript",
                recovery="salvaged_sections",
                warning="Gemini returned malformed JSON. This transcript was salvaged from a partial response and may be incomplete.",
                transcript_source=TRANSCRIPT_SOURCE_GEMINI,
            )
            # Issue #128: a salvage caused by the OUTPUT cap gets its own status
            # so the corpus is sweepable. Without it, "the JSON was malformed"
            # and "the response ran out of output budget" look identical in
            # meta.json, and only the second one is fixed by a chunked re-run.
            truncated = hit_output_cap(
                usage_capture.get("candidates"),
                finish_capture.get("reason"),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            salvage_fields = {
                **_transcript_identity_fields(video, channel_dir),
                "processed": datetime.now(UTC).isoformat(),
                "transcript_status": TRANSCRIPT_STATUS_TRUNCATED if truncated else "partial",
                "transcript_source": TRANSCRIPT_SOURCE_GEMINI,
                "transcript_recovery": "salvaged_sections",
                "transcript_parse_error": parse_error,
                "transcript_warning": salvage_warning,
            }
            if truncated:
                salvage_fields["transcript_output_tokens"] = usage_capture.get("candidates")
                salvage_fields["transcript_finish_reason"] = finish_capture.get("reason")
                log.warning(
                    "  %s: response hit the OUTPUT cap (candidates=%s, finish_reason=%s). "
                    "Recover with: process --url %s --chunk-minutes 20 --force",
                    prefix,
                    usage_capture.get("candidates"),
                    finish_capture.get("reason"),
                    video.get("url", ""),
                )
            update_meta(meta_path, salvage_fields, "transcript")
            log.info("  %s: salvaged partial transcript (%d speech entries)", prefix, speech_count)
            status_word = TRANSCRIPT_STATUS_TRUNCATED if truncated else "partial"
            return prefix, f"{status_word} ({speech_count} entries salvaged)"

        # Salvage insufficient - retry if budget remains
        if attempt < parse_retry_limit:
            log.info("  %s: salvage insufficient (%d entries), retrying...", prefix, speech_count)
            continue

    # All attempts exhausted. Issue #60: on auto, try captions before giving up
    # (unless issue #120's captions-first pass already established there are none).
    if transcript_source == "auto" and not captions_already_tried:
        fb = _try_captions_transcript(
            video,
            transcript_path,
            meta_path,
            prefix,
            reason=f"gemini parse failure: {parse_error}",
            start_offset=start_offset,
            end_offset=end_offset,
            force=force,
        )
        if fb is not None:
            return fb
    if meta_path.exists():
        # Best-effort read (issue #124): this branch is recording a parse
        # failure, so a second parse failure - on the meta file this time - must
        # not throw away the first one.
        meta = _read_meta_best_effort(meta_path, raise_on_os_error=False)
        meta["last_error"] = f"JSON parse error: {parse_error}"
        meta["transcript_parse_error"] = parse_error
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return prefix, f"error parsing JSON: {parse_error}"


# ---------------------------------------------------------------------------
# Concept extraction
# ---------------------------------------------------------------------------


def call_gemini_text(
    client,
    types,
    text_content,
    model,
    *,
    on_response: Callable[[object], None] | None = None,
    response_mime_type: str = "application/json",
):
    """Send text-only content to Gemini and get the raw response text.

    The default ``response_mime_type`` is ``"application/json"`` for back-compat
    with the concepts caller. Callers that need markdown (issue #54
    mindmap-from-transcript) should pass ``response_mime_type="text/plain"`` so
    Gemini does not wrap the markdown in a JSON envelope.

    Optional on_response callback mirrors ``call_gemini``'s behavior: the raw
    response object is passed to the callback before ``.text`` is returned.
    Callback failures are caught and logged at warning — they never break the call.
    """
    config_kwargs = {
        "temperature": 0.3,
        "response_mime_type": response_mime_type,
        "safety_settings": build_permissive_safety_settings(types),
    }
    contents = types.Content(parts=[types.Part(text=text_content)])

    max_retries_rate = 3
    max_retries_server = 8
    # Transport retries are counted SEPARATELY from `attempt` (PR #136 review):
    # `attempt` counts failures of every class, so with a budget of 1 a drop
    # that follows any 429/5xx retry would arrive already over budget.
    transport_attempts = 0
    for attempt in range(max(max_retries_rate, max_retries_server) + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            if on_response is not None:
                try:
                    on_response(response)
                except Exception as obs_exc:
                    log.warning("on_response callback failed: %s", obs_exc)
            return response.text
        except Exception as e:
            retry = get_retry_delay(
                e,
                attempt,
                max_retries_rate=max_retries_rate,
                max_retries_server=max_retries_server,
                max_retries_transport=MAX_RETRIES_TRANSPORT,
                transport_attempt=transport_attempts,
            )
            if retry is None:
                raise
            if is_transient_transport_error(e):
                transport_attempts += 1
            kind, wait, max_for_type = retry
            log.warning("%s — retry %d/%d in %.0fs...", kind, attempt + 1, max_for_type, wait)
            time.sleep(wait)


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
    prefix: str | None = None,
):
    """Extract and normalize concepts from a mindmap against the taxonomy.

    When ``prefix`` is provided, it overrides ``video_file_prefix(video)`` for
    determining artifact filenames. Used by the local-recovery path where the
    meta.json filename stem is the authoritative prefix (plan rev 4 F12).
    """
    resolved_prefix = prefix if prefix is not None else video_file_prefix(video)
    channel_dir = output_dir / channel_name
    channel_dir.mkdir(parents=True, exist_ok=True)

    concepts_path = channel_dir / f"{resolved_prefix}.concepts.json"
    meta_path = channel_dir / f"{resolved_prefix}.meta.json"
    prefix = resolved_prefix

    if concepts_path.exists() and not force:
        return prefix, "skipped (exists)"

    try:
        # Build the prompt with taxonomy context
        prompt_text = load_prompt("concepts")
        taxonomy_context = json.dumps(taxonomy.get("concepts", {}), indent=2)
        prompt_with_taxonomy = prompt_text.replace("{{taxonomy}}", taxonomy_context)

        full_text = f"{prompt_with_taxonomy}\n\n---\n\n## Mind Map to Analyze\n\n{mindmap_text}"
        raw = call_gemini_text(
            client,
            types,
            full_text,
            model,
            on_response=lambda r: log_usage_metadata(r, "concepts"),
        )
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
        # Stamp identity alongside the timestamp (issue #66 contract). A
        # fields dict of {"processed": ...} alone is only safe while the
        # existing meta is readable; if it ever is not, this writer would
        # leave an identity-less meta that _load_video_id_index skips,
        # re-queueing the video for a full re-transcribe.
        # "concepts_status": "ok" is not decoration - it OVERWRITES a
        # concepts_status left by an earlier failed attempt (issue #129).
        # update_meta resets last_error but knows nothing about per-stage
        # fields, so without this a recovered video would carry an error
        # record forever and read as broken in any corpus sweep.
        update_meta(
            meta_path,
            {
                **_transcript_identity_fields(video, channel_dir),
                "processed": datetime.now(UTC).isoformat(),
                "concepts_status": "ok",
            },
            "concepts",
        )

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
        _record_concepts_error(meta_path, video, channel_dir, f"parsing JSON: {e}")
        return prefix, f"error parsing JSON: {e}"
    except Exception as e:
        _record_concepts_error(meta_path, video, channel_dir, str(e))
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
    require_channels_config(config)
    errors = []
    _, types = require_gemini()
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

    client = create_client(gemini_key)
    youtube = yt_build("youtube", "v3", developerKey=yt_key)
    output_dir = resolve_output_dir(config)
    model = resolve_model(args, config)
    max_parallel = config.get("max_parallel", 10)

    # Filter channels if --channel specified
    channels = config.get("channels", [])
    if args.channel:
        channels = [c for c in channels if c["name"] == args.channel]
        if not channels:
            log.error("Channel '%s' not found in config.yaml", args.channel)
            sys.exit(1)

    # Skip channels not enabled for the primary pipeline. Lets a creator stay in
    # config for one-off mindmap/transcript --url --channel routing (and concepts
    # extraction) without being pulled into every regular scan. The gate is a
    # STRICT boolean (see _channel_scan_enabled): a non-boolean `enabled` is never
    # truthy-admitted here (issue #113 - a stray "headlines" string must not reach
    # full Gemini processing).
    for ch in [c for c in channels if not _channel_scan_enabled(c)]:
        log.info(
            "[%s] Skipping (enabled: %r). Use mindmap/transcript --url --channel %s for one-offs.",
            ch["name"],
            ch.get("enabled"),
            ch["name"],
        )
    channels = [c for c in channels if _channel_scan_enabled(c)]

    for ch in channels:
        ch_name = ch["name"]
        ch_url = ch["url"]

        # Resolve channel
        channel_id, channel_title = get_channel_id(youtube, ch_url)
        if not channel_id:
            log.warning("[%s] Channel not found: %s", ch_name, ch_url)
            continue

        log.info("[%s] %s", ch_name, channel_title)

        # Validate selective config fields
        skip_channel = False
        for field in ("playlists", "keywords"):
            val = ch.get(field)
            if val is not None and (not isinstance(val, list) or not all(isinstance(v, str) for v in val)):
                log.error("[%s] '%s' must be a list of strings, got: %s", ch_name, field, type(val).__name__)
                skip_channel = True
        if skip_channel:
            continue

        # Determine fetch strategy: selective (playlists/keywords) or date-based
        is_selective = bool(ch.get("playlists") or ch.get("keywords"))

        if is_selective:
            log.info("  Selective mode: playlists=%s, keywords=%s", ch.get("playlists", []), ch.get("keywords", []))
            # since is additive for selective channels (also fetch recent uploads)
            since_str = args.since or ch.get("since") or config.get("default_since")
            selective_since = parse_since(since_str) if since_str else None
            videos = fetch_selective_videos(youtube, channel_id, ch, since_dt=selective_since)
        else:
            since_str = args.since or ch.get("since") or config.get("default_since", "10d")
            since_dt = parse_since(since_str)
            log.info("  Looking back to %s", since_dt.strftime("%Y-%m-%d"))
            videos = fetch_channel_videos(youtube, channel_id, since_dt)

        # Issue #42 follow-up: declarative video-id blocklist. Filter pre-Gemini and
        # pre-enrich so listed IDs never trigger meta writes, duration lookups, or
        # processing. Override path is removing the ID from config.yaml.
        # Per-video log lines so silent-typo failures are visible: a listed ID that
        # never matches a fetched video produces no log line, which is the user's
        # signal that the entry is stale.
        skip_ids = set(ch.get("skip_video_ids") or [])
        if skip_ids:
            matched = [v for v in videos if v.get("video_id") in skip_ids]
            videos = [v for v in videos if v.get("video_id") not in skip_ids]
            for m in matched:
                log.info('    skip_video_ids: %s "%s"', m.get("video_id"), m.get("title", ""))
            if matched:
                log.info("  Filtered %d video(s) per skip_video_ids config.", len(matched))
            unmatched = skip_ids - {m.get("video_id") for m in matched}
            for missing in sorted(unmatched):
                log.warning(
                    '  skip_video_ids: "%s" listed but NOT in fetched videos (deleted, typo, or wrong channel?)',
                    missing,
                )

        if not videos:
            # With auto_concepts enabled we still want to enumerate local-recovery
            # artifacts via the *.meta.json glob below, even when YouTube has no
            # new videos to pull. Without auto_concepts, there is nothing more to
            # do for this channel and we can short-circuit.
            auto_concepts_enabled = ch.get("auto_concepts", config.get("auto_concepts", False))
            if not auto_concepts_enabled:
                log.info("  No new videos found.")
                continue
            log.info("  No new videos from YouTube; checking local artifacts for concepts...")

        # Capture title rotations before filtering: any video whose id is already
        # in the channel index but whose title has changed gets its new title
        # recorded as an alt_title on the existing meta. Idempotent; no-op when
        # there is no rotation. Runs on every scan regardless of --force so SEO
        # A/B-test signal is preserved continuously. Gated on not args.dry_run
        # so --dry-run keeps its preview-only contract (the recorder mutates
        # meta.json when it fires).
        if not args.dry_run:
            for v in videos:
                record_alt_title_if_rotated(output_dir, ch_name, v)

        # Shorts classification + filter (per docs/plans/2026-04-24-002).
        # Always fetch durations so meta.json carries duration_seconds going
        # forward; the filter only applies when skip_shorts is true (default).
        # Quota-exhaustion: bail this channel cleanly instead of silently
        # admitting Shorts the filter was supposed to drop.
        skip_shorts = ch.get("skip_shorts", config.get("skip_shorts", True))
        try:
            durations = enrich_with_durations(youtube, [v["video_id"] for v in videos])
        except HttpError as e:
            if e.resp.status == 403 and _is_quota_exceeded(e):
                log.error(
                    "[%s] YouTube quota exhausted while classifying Shorts; aborting this channel.",
                    ch_name,
                )
                log.error("  Re-run scan after quota resets (typically next midnight Pacific).")
                continue
            raise
        for v in videos:
            v["duration_iso"] = durations.get(v["video_id"])

        # Issue #70: pre-flight metadata filter. Drop videos that have not aired
        # (scheduled premieres / live) or are non-public BEFORE any Gemini call.
        # Gemini ingests no playable stream for these and confabulates a stub
        # (the 2026-06-18 prompt=0 garbage). This is a separate videos.list call
        # from the duration enrich above, so it costs 1 quota unit per 50-id batch
        # (negligible: ~4 units for a 200-video channel). Same quota-exhaustion
        # fail-out as durations.
        try:
            statuses = fetch_preflight_status(youtube, [v["video_id"] for v in videos])
        except HttpError as e:
            if e.resp.status == 403 and _is_quota_exceeded(e):
                log.error(
                    "[%s] YouTube quota exhausted during pre-flight metadata check; aborting this channel.",
                    ch_name,
                )
                continue
            raise
        kept = []
        n_preflight = 0
        for v in videos:
            status = statuses.get(v["video_id"], {})
            reason = preflight_skip_reason(status)
            if reason:
                log.info('  Pre-flight skip "%s": %s', v.get("title", v["video_id"]), reason)
                n_preflight += 1
            else:
                # Issue #120: carry the completed-livestream flag on the video
                # dict so the transcript and mindmap loops below can route on it
                # without a second API call.
                v["was_livestream"] = bool(status.get("was_livestream"))
                if v["was_livestream"]:
                    # One line PER VIDEO, not an aggregate count. YouTube attaches
                    # liveStreamingDetails to aired PREMIERES of ordinary uploads
                    # exactly as it does to genuine livestreams, and exposes no
                    # field that separates them - so this flag can misfire, and a
                    # premiere-every-upload channel would quietly slide to
                    # captions-only transcripts. Naming each video makes that
                    # auditable from the scan log instead of invisible.
                    log.info(
                        '  Livestream/premiere VOD, routing captions-first: %s "%s"',
                        v["video_id"],
                        v.get("title", ""),
                    )
                kept.append(v)
        if n_preflight:
            log.info("  Pre-flight: skipped %d not-yet-aired/non-public video(s).", n_preflight)
        videos = kept

        if skip_shorts:
            kept = [v for v in videos if not is_short(v["video_id"], v["duration_iso"])]
            n_skipped = len(videos) - len(kept)
            if n_skipped:
                log.info("  Skipped %d Shorts (skip_shorts=true).", n_skipped)
            videos = kept

        # Issue #42 follow-up: per-channel min_duration_seconds. Drops anything
        # shorter than the threshold. Useful for long-form podcasters (Lex
        # Fridman) where the user's mental model of "too short to bother" is
        # higher than the standard 60s YouTube Shorts cutoff. Unparseable
        # durations fail-safe to KEEP (matches transcript_max_duration_seconds
        # invariant - silent drops are worse than visible truncation).
        min_duration = ch.get("min_duration_seconds")
        if min_duration:
            kept = []
            n_dropped = 0
            for v in videos:
                secs = _parse_iso8601_duration(v.get("duration_iso"))
                if secs is None or secs >= min_duration:
                    kept.append(v)
                else:
                    n_dropped += 1
                    log.info(
                        '    min_duration_seconds: dropped %s (%s < %s) "%s"',
                        v.get("video_id", "?"),
                        _fmt_hms(secs),
                        _fmt_hms(min_duration),
                        v.get("title", "")[:60],
                    )
            if n_dropped:
                log.info(
                    "  Filtered %d video(s) under %s (min_duration_seconds).",
                    n_dropped,
                    _fmt_hms(min_duration),
                )
            videos = kept

        # Filter already processed or skipped (any_variant=True prevents backfill).
        # mode="mindmap" so per-mode skip_modes=["transcript"] does NOT block the
        # mindmap loop. See is_skipped_meta() and issue #42.
        if args.force:
            new_videos = [v for v in videos if not is_skipped(output_dir, ch_name, v, mode="mindmap")]
        else:
            new_videos = [
                v
                for v in videos
                if not is_processed(output_dir, ch_name, v, "scan", any_variant=True)
                and not is_skipped(output_dir, ch_name, v, mode="mindmap")
            ]
        label = "to regenerate" if args.force else "new"
        log.info("  Found %d videos, %d %s.", len(videos), len(new_videos), label)

        if args.dry_run:
            for v in new_videos:
                log.info("    %s - %s", v["published"], v["title"])
            continue

        prompt_name = ch.get("prompt") or config.get("default_prompt", "mindmap-light")
        prompt_text = load_prompt(prompt_name)

        # ----------------------------------------------------------------------
        # Issue #54: scan order is now transcript -> mindmap -> concepts. The
        # transcript loop runs first so the mindmap loop below can read on-disk
        # transcripts via the new mindmap-from-transcript path. The legacy
        # mindmap-from-video path stays available via the resolver's auto/video
        # branches and powers users who keep auto_transcript=none.
        # ----------------------------------------------------------------------

        # Issue #120: per-video transcript outcome, keyed by prefix. The mindmap
        # loop below reads it to decide whether a failed livestream VOD may still
        # fall back to mindmap-from-video (it may not). Stays empty when the
        # transcript loop does not run, which preserves today's routing.
        transcript_results: dict[str, str] = {}

        # Auto-transcript if configured (Step 1/2 of the inverted ordering).
        auto = ch.get("auto_transcript", "none")
        if auto == "all":
            transcript_prompt = load_prompt("transcript")
            # Issue #60: per-channel transcript source (gemini | yt-captions | auto).
            # Default "gemini" preserves current behavior; "auto" adds the captions
            # failover when Gemini fails (token-cap, 403, confabulation).
            transcript_source = resolve_transcript_source(ch)
            # Issue #120 provenance rule: captions-first is mandatory for a VOD
            # only when nobody asked for Gemini. An explicit
            # `transcript_source: gemini` on the channel is honored (documented
            # config contract), and is the escape hatch when the flag misfires.
            vod_captions_first = livestream_captions_first_applies(transcript_source, ch)
            # Issue #74: wall-clock cap so a hung Gemini call raises (-> failover
            # under auto) instead of deadlocking the whole batch. Per-channel
            # override > top-level > default, matching every other knob.
            transcript_timeout_seconds = ch.get(
                "transcript_timeout_seconds",
                config.get("transcript_timeout_seconds", TRANSCRIPT_TIMEOUT_DEFAULT),
            )
            # Long-video guard (issue #42): videos longer than the threshold
            # truncate the structured-JSON transcript response. Filter them out
            # of the transcript loop and log the manual-clipping recipe.
            # Mindmap loop is downstream and unaffected for filtered videos:
            # the resolver falls back to "video" source when no transcript
            # is on disk (with the existing fps fallback for long videos).
            # Per-channel override wins over top-level over default - matches
            # every other knob in this config (skip_shorts, since, prompt, etc).
            threshold = ch.get(
                "transcript_max_duration_seconds",
                config.get("transcript_max_duration_seconds", TRANSCRIPT_MAX_DURATION_DEFAULT),
            )
            # Issue #128: same precedence as every other knob here. Conference
            # channels can lower this so their dense talks chunk before they hit
            # the output cap.
            try:
                chunk_minutes = resolve_chunk_minutes(ch, config, getattr(args, "chunk_minutes", None))
            except ValueError as e:
                # Matches the defensive pattern for bad playlists/keywords a few
                # lines up: one channel's config typo must not abort the whole
                # scan after quota and Gemini spend are already sunk, and
                # --dry-run returns before this point so it cannot catch it.
                log.error("[%s] invalid chunk_minutes (%s); skipping channel", ch_name, e)
                continue
            transcript_videos: list[dict] = []
            for v in videos:
                if is_processed(output_dir, ch_name, v, "transcript"):
                    continue
                if is_skipped(output_dir, ch_name, v, mode="transcript"):
                    continue
                duration_s = _parse_iso8601_duration(v.get("duration_iso"))
                if duration_s is not None and duration_s > threshold:
                    log.warning(
                        '[%s] Skipping transcript for "%s" (%s > %dm).',
                        ch_name,
                        v["title"],
                        _fmt_hms(duration_s),
                        threshold // 60,
                    )
                    log.warning(
                        "  To process manually with clipping: transcript --url %s --start 0 --end %d",
                        v.get("url", ""),
                        threshold,
                    )
                    log.warning(
                        "  Note: that command captures only the first %dm; pass --start/--end to cover later segments.",
                        threshold // 60,
                    )
                    continue
                transcript_videos.append(v)
            if transcript_videos:
                log.info("  Generating transcripts (%d videos)...", len(transcript_videos))
                with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                    futures = {
                        executor.submit(
                            _scan_transcribe_one,
                            client=client,
                            types=types,
                            video=v,
                            prompt_text=transcript_prompt,
                            model=model,
                            channel_dir=output_dir / ch_name,
                            prefix=video_file_prefix(v),
                            transcript_source=transcript_source,
                            transcript_timeout_seconds=transcript_timeout_seconds,
                            livestream_captions_first=(vod_captions_first and bool(v.get("was_livestream"))),
                            duration_seconds=_parse_iso8601_duration(v.get("duration_iso")),
                            chunk_minutes=chunk_minutes,
                        ): v
                        for v in transcript_videos
                    }
                    for future in as_completed(futures):
                        v = futures[future]
                        prefix, status = future.result()
                        transcript_results[prefix] = status
                        log.info("    %s: %s", prefix, status)
                        if status.startswith("error"):
                            errors.append((ch_name, prefix, status))

        # Issue #42 follow-up (notify-only mode): per-channel auto_mindmap=none
        # discovers and logs new videos without paying the mindmap Gemini call.
        # Combined with auto_transcript=none, the channel becomes pure
        # notification - useful for long-form podcasters (Lex Fridman) where
        # the user wants to cherry-pick episodes manually.
        auto_mindmap = ch.get("auto_mindmap", "all")
        if auto_mindmap == "none":
            if new_videos:
                log.info(
                    "  auto_mindmap=none: %d new video(s) listed below, NOT processed.",
                    len(new_videos),
                )
                for v in new_videos:
                    log.info("    %s - %s", v["published"], v["title"])
            else:
                log.info("  auto_mindmap=none: no new videos.")
        elif not new_videos:
            log.info("  All mind maps up to date.")
        else:
            # Issue #54: per-video source resolution. Transcripts from Step 1
            # are now on disk (or absent if the threshold/skip filter rejected
            # them); resolve_mindmap_source() picks transcript vs video accordingly.
            channel_dir_for_mindmap = output_dir / ch_name
            mindmap_from_transcript_prompt = load_prompt("mindmap-from-transcript")

            # Bind loop-scope variables as defaults to avoid B023 closure-over-loop-var.
            def _build_mindmap_call(
                v,
                _ch=ch,
                _ch_name=ch_name,
                _channel_dir=channel_dir_for_mindmap,
                _video_prompt_text=prompt_text,
                _video_prompt_name=prompt_name,
                _transcript_prompt_text=mindmap_from_transcript_prompt,
                _transcript_results=transcript_results,
            ):
                v_prefix = video_file_prefix(v)
                v_transcript_path = _channel_dir / f"{v_prefix}.transcript.md"
                transcript_available = v_transcript_path.exists()
                try:
                    src = resolve_mindmap_source(_ch, transcript_available=transcript_available)
                except ValueError as exc:
                    return v_prefix, f"error: {exc}"
                if src == "skip":
                    return v_prefix, "skipped (mindmap_source=none)"
                # Issue #120: a livestream VOD whose transcript attempt just
                # failed has a URI Gemini demonstrably cannot ingest. Falling
                # back to mindmap-from-video would hard-fail or confabulate the
                # same way, so the call is never spent. The issue #119 prompt=0
                # guard remains the backstop for any path that still gets here.
                if should_skip_video_mindmap_for_livestream(
                    was_livestream=bool(v.get("was_livestream")),
                    resolved_source=src,
                    transcript_status=_transcript_results.get(v_prefix),
                ):
                    return v_prefix, LIVESTREAM_MINDMAP_SKIP_STATUS
                if src == "transcript":
                    return process_mindmap(
                        client,
                        types,
                        v,
                        _transcript_prompt_text,
                        model,
                        output_dir,
                        _ch_name,
                        prompt_name="mindmap-from-transcript",
                        force=args.force,
                        source="transcript",
                        transcript_path=v_transcript_path,
                    )
                # Legacy video path
                return process_mindmap(
                    client,
                    types,
                    v,
                    _video_prompt_text,
                    model,
                    output_dir,
                    _ch_name,
                    prompt_name=_video_prompt_name,
                    force=args.force,
                    source="video",
                )

            log.info("  Generating mind maps (%s)...", prompt_name)
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                futures = {executor.submit(_build_mindmap_call, v): v for v in new_videos}
                for future in as_completed(futures):
                    v = futures[future]
                    prefix, status = future.result()
                    log.info("    %s: %s", prefix, status)
                    if status == LIVESTREAM_MINDMAP_SKIP_STATUS:
                        _log_livestream_recovery_recipe(v, ch_name)
                    if status.startswith("error"):
                        errors.append((ch_name, prefix, status))
                        # Plan rev 4: on 403 PERMISSION_DENIED, print a recovery
                        # recipe so the user can fix a members-only gated video
                        # without having to read documentation mid-scan.
                        if "PERMISSION_DENIED" in status:
                            log.info(
                                "      -> Likely members-only. To recover: save the MP4 as %s.mp4 "
                                "in any folder, then run:",
                                v["video_id"],
                            )
                            log.info(
                                "        python scripts/video_intel.py mindmap    --file <PATH> --channel %s",
                                ch_name,
                            )
                            log.info(
                                "        python scripts/video_intel.py transcript --file <PATH> --channel %s",
                                ch_name,
                            )

        # Auto-concepts if configured.
        # Plan rev 4 F12: enumerate via *.meta.json glob so both scan-generated
        # ({date}-{slug}) and local-recovery (stem) artifacts are picked up without
        # special-casing. Prefix is derived from the meta filename, not recomputed.
        auto_concepts = ch.get("auto_concepts", config.get("auto_concepts", False))
        if auto_concepts:
            taxonomy = load_taxonomy(output_dir)
            prompt_name = ch.get("prompt") or config.get("default_prompt", "mindmap-knowledge")
            channel_dir_for_concepts = output_dir / ch_name
            concept_candidates: list[tuple[dict, Path, str]] = []
            if channel_dir_for_concepts.exists():
                for meta_file in sorted(channel_dir_for_concepts.glob("*.meta.json")):
                    prefix = meta_file.name[: -len(".meta.json")]
                    concepts_path = channel_dir_for_concepts / f"{prefix}.concepts.json"
                    if concepts_path.exists():
                        continue
                    mindmap_path = find_mindmap_source(channel_dir_for_concepts, prefix)
                    if not mindmap_path:
                        continue
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        log.warning("Skipping malformed meta.json: %s", meta_file)
                        continue
                    if is_skipped_meta(meta, mode="concepts"):
                        continue
                    synthetic_video = {
                        "video_id": meta.get("video_id", ""),
                        "url": meta.get("video_url", ""),
                        "title": meta.get("title", prefix),
                        "published": meta.get("published", ""),
                    }
                    concept_candidates.append((synthetic_video, mindmap_path, prefix))

            if concept_candidates:
                log.info("  Extracting concepts (%d videos)...", len(concept_candidates))
                for v, mindmap_path, prefix in concept_candidates:
                    mindmap_text = mindmap_path.read_text(encoding="utf-8")
                    out_prefix, status = process_concepts(
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
                        prefix=prefix,
                    )
                    log.info("    %s: %s", out_prefix, status)
                    if status.startswith("error"):
                        errors.append((ch_name, out_prefix, status))

    if errors:
        log.warning("--- %d FAILED ---", len(errors))
        for ch, prefix, status in errors:
            log.warning("  [%s] %s: %s", ch, prefix, status)
        log.warning("Failed items will retry on next run.")
        log.warning('To skip permanently: set "skip": true in the video\'s .meta.json')

    # Headline digest (issue #113): peripheral vision over channels the user does
    # not actively follow (enabled:false + headline_digest:true). Rendered LAST -
    # after all primary processing AND the failure summary - so headline-quota
    # failures are non-fatal and peripheral work never delays wanted work. It is a
    # full-scan concept, so it is skipped on focused `scan --channel X` runs.
    if not args.channel:
        try:
            render_headline_digest(youtube, config, output_dir, dry_run=args.dry_run)
        except Exception as e:  # non-fatal: never let peripheral work fail a scan
            log.warning("Headline digest failed (non-fatal): %s", e)

    log.info("Done.")


def cmd_mindmap(args, config):
    """Generate a mind map for a single video (YouTube URL or local MP4)."""
    _, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)

    client = create_client(gemini_key)
    output_dir = resolve_output_dir(config)
    model = resolve_model(args, config)
    # Resolve --media-resolution flag once at command boundary; threaded into
    # every process_mindmap call below that uses source="video". Only meaningful
    # on the mindmap-from-video path; transcript-source calls ignore it.
    # Guarded against types=None for test stubs that bypass require_gemini —
    # process_mindmap accepts media_resolution=None and falls back to LOW.
    media_resolution_enum = (
        _resolve_media_resolution(types, getattr(args, "media_resolution", "low")) if types is not None else None
    )

    # Resolve prompt
    prompt_name = normalize_prompt_name(getattr(args, "prompt", None) or config.get("default_prompt", "mindmap-light"))
    prompt_text = load_prompt(prompt_name)

    # --- Local-file path (plan rev 4) ---
    if getattr(args, "file", None):
        input_path = Path(args.file).resolve()
        if not input_path.exists():
            log.error("File not found: %s", input_path)
            sys.exit(1)

        channel_name = args.channel or infer_channel_from_file_path(input_path, output_dir, config)

        if args.channel:
            require_channels_config(config)
            configured = {c["name"] for c in config.get("channels", [])}
            if args.channel not in configured:
                log.error("Channel '%s' not found in config.yaml", args.channel)
                sys.exit(1)

        if channel_name:
            channel_dir_hint = output_dir / channel_name
            identity = resolve_local_file_identity(
                input_path, channel_name=channel_name, channel_dir=channel_dir_hint, args=args
            )

            # F7: honor skip flag from existing meta.json before any Gemini work.
            if identity["meta_path"].exists():
                existing = _read_meta_best_effort(identity["meta_path"], raise_on_os_error=False)
                if is_skipped_meta(existing, mode="mindmap"):
                    log.info("Skipping %s (skip flag in meta.json blocks mindmap)", identity["prefix"])
                    return

            video = {
                "video_id": identity["video_id"],
                "url": identity["url"],
                "title": identity["title"],
                "published": identity["published"],
            }
            file_uri = upload_local_video(client, input_path)
            log.info("Generating mind map (%s, channel=%s): %s", prompt_name, channel_name, input_path.name)
            prefix, status = process_mindmap(
                client,
                types,
                video,
                prompt_text,
                model,
                output_dir,
                identity["channel"],
                prompt_name=prompt_name,
                force=args.force,
                prefix=identity["prefix"],
                channel_dir_override=identity["channel_dir"],
                media_uri=file_uri,
                media_resolution=media_resolution_enum,
            )
            log.info("  %s: %s", prefix, status)
            if status == "done":
                log.info("  Saved: %s", identity["channel_dir"] / f"{prefix}.mindmap.md")
            return

        # No channel inferrable: fall through to next-to-source output for loose files
        file_uri = upload_local_video(client, input_path)
        video = {
            "video_id": input_path.stem,
            "url": file_uri,  # no canonical URL known; file_uri is least-bad fallback
            "title": input_path.stem,
            "published": datetime.fromtimestamp(input_path.stat().st_mtime).strftime("%Y-%m-%d"),
        }
        log.info("Generating mind map (%s): %s", prompt_name, input_path.name)
        prefix, status = process_mindmap(
            client,
            types,
            video,
            prompt_text,
            model,
            output_dir,
            "_standalone",
            prompt_name=prompt_name,
            force=args.force,
            prefix=input_path.stem,
            channel_dir_override=input_path.parent,
            media_uri=file_uri,
            media_resolution=media_resolution_enum,
        )
        log.info("  %s: %s", prefix, status)
        if status == "done":
            log.info("  Saved: %s", input_path.parent / f"{prefix}.mindmap.md")
        return

    # --- YouTube URL path (unchanged) ---
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

    # Issue #54: route through resolver. When a transcript is already on disk
    # for this URL, this command becomes a fast text-only call. Otherwise it
    # falls back to the legacy mindmap-from-video path with the same fps
    # fallback for the 10800-frame cap.
    #
    # Title-rotation safety net (PR #31 follow-up): consult the channel's
    # video_id index BEFORE computing the lookup prefix. When YouTube creators
    # A/B-test titles, the API returns the current title, but the on-disk
    # transcript is named after the title at write-time. Without this check,
    # rotated-title videos would miss their existing transcript and fall back
    # to source=video — failing on the 1M-token cap on long content.
    # Reproducer: simonscrapes / Master Claude Code retitled to "How the 1%...".
    channel_dir = output_dir / channel_name
    computed_prefix = video_file_prefix(video)
    indexed_prefix = _load_video_id_index(channel_dir).get(video["video_id"])
    prefix_for_lookup = indexed_prefix if indexed_prefix else computed_prefix
    if indexed_prefix and indexed_prefix != computed_prefix:
        log.info(
            "Title-rotation detected for %s; using existing prefix %r (computed would have been %r)",
            video["video_id"],
            indexed_prefix,
            computed_prefix,
        )
    transcript_path = channel_dir / f"{prefix_for_lookup}.transcript.md"
    transcript_available = transcript_path.exists()
    channel_cfg: dict = next(
        (c for c in config.get("channels", []) if c.get("name") == channel_name),
        {},
    )
    try:
        resolved_source = resolve_mindmap_source(channel_cfg, transcript_available=transcript_available)
    except ValueError as exc:
        log.error("Mindmap source unresolvable for %s: %s", video_id, exc)
        sys.exit(1)
    if resolved_source == "skip":
        log.info("mindmap_source=none for channel %s; nothing to do.", channel_name)
        return

    log.info(
        "Generating mind map (source=%s, %s): %s",
        resolved_source,
        prompt_name if resolved_source == "video" else "mindmap-from-transcript",
        video["url"],
    )
    if resolved_source == "transcript":
        prefix, status = process_mindmap(
            client,
            types,
            video,
            load_prompt("mindmap-from-transcript"),
            model,
            output_dir,
            channel_name,
            prompt_name="mindmap-from-transcript",
            force=args.force,
            prefix=prefix_for_lookup,
            source="transcript",
            transcript_path=transcript_path,
        )
    else:
        # Issue #50 Gate-1 finding: Gemini caps at 10800 frames per request.
        # Preserved here only - text input has no frame cap.
        duration_seconds = _lookup_video_duration_seconds(video_id)
        mindmap_fps: float | None = None
        if duration_seconds and duration_seconds > 10000:
            mindmap_fps = 0.5
            log.info(
                "Long video (%s); reducing mindmap fps to %.1f to fit Gemini's 10800-frame cap.",
                _fmt_hms(duration_seconds),
                mindmap_fps,
            )
        prefix, status = process_mindmap(
            client,
            types,
            video,
            prompt_text,
            model,
            output_dir,
            channel_name,
            prompt_name=prompt_name,
            force=args.force,
            prefix=prefix_for_lookup,
            fps=mindmap_fps,
            source="video",
            media_resolution=media_resolution_enum,
        )
    log.info("  %s: %s", prefix, status)

    if status == "done":
        out_path = output_dir / channel_name / f"{prefix}.mindmap.md"
        log.info("  Saved: %s", out_path)


def cmd_transcript(args, config):
    """Generate a transcript for a single video (YouTube URL or local MP4)."""
    _, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)

    client = create_client(gemini_key)
    model = resolve_model(args, config)
    prompt_text = load_prompt("transcript")
    # Resolve --media-resolution flag once at command boundary; threaded into
    # all process_transcript calls below. Defaults to LOW to match the chunked-
    # transcript path's pattern and stay under Gemini's 1M-token cap on
    # hour-long videos. Guarded against types=None for test stubs.
    media_resolution_enum = (
        _resolve_media_resolution(types, getattr(args, "media_resolution", "low")) if types is not None else None
    )
    # Issue #60/#127: the channel dict is not known until the branches below
    # resolve it, so bind the CLI-only answer here and let the --url branch
    # re-resolve once it has a channel. Precedence is unchanged either way:
    # CLI flag > channel config > "gemini".
    cli_transcript_source = getattr(args, "transcript_source", None)
    transcript_source = resolve_transcript_source({}, cli_transcript_source)
    # Raw channel dict, kept alongside the resolved string because
    # livestream_captions_first_applies needs the PROVENANCE (was the key
    # present?), which the resolved string collapses away.
    channel_cfg: dict = {}

    # Parse segment offsets (shared between URL and file paths)
    start_offset = parse_time_to_seconds(args.start) if args.start else None
    end_offset = parse_time_to_seconds(args.end) if args.end else None
    # Issue #120: set on the YouTube URL branch below; a local file has no
    # YouTube identity to classify, and no caption track to fetch, so it keeps
    # today's routing. Both must be bound here - the --file branch skips the
    # URL branch entirely and still reaches the shared process_transcript call.
    was_livestream = False
    vod_captions_first = False

    if args.file:
        # Local file path
        input_path = Path(args.file).resolve()
        if not input_path.exists():
            log.error("File not found: %s", input_path)
            sys.exit(1)

        size = input_path.stat().st_size
        has_segment = start_offset is not None or end_offset is not None
        if size > LARGE_FILE_THRESHOLD_BYTES and not has_segment:
            size_gb = size / 1024 / 1024 / 1024
            log.error(
                "File is %.1fGB. Specify --start and --end to transcribe a segment "
                "(Gemini's 2GB upload limit applies).",
                size_gb,
            )
            sys.exit(1)

        # Channel resolution: explicit --channel wins, else infer from parent folder.
        output_dir = resolve_output_dir(config)
        channel_name = args.channel or infer_channel_from_file_path(input_path, output_dir, config)

        if args.channel:
            require_channels_config(config)
            configured = {c["name"] for c in config.get("channels", [])}
            if args.channel not in configured:
                log.error("Channel '%s' not found in config.yaml", args.channel)
                sys.exit(1)

        media_uri: str | None = None
        if channel_name:
            # Channel-scoped in-place recovery path (plan rev 4).
            channel_dir_hint = output_dir / channel_name
            identity = resolve_local_file_identity(
                input_path, channel_name=channel_name, channel_dir=channel_dir_hint, args=args
            )
            video = {
                "video_id": identity["video_id"],
                "url": identity["url"],  # canonical YouTube URL (or empty); never file_uri
                "title": identity["title"],
                "published": identity["published"],
            }
            channel_dir = identity["channel_dir"]
            prefix = identity["prefix"]

            # F7: honor skip flag from existing meta.json before any Gemini work.
            if identity["meta_path"].exists():
                existing = _read_meta_best_effort(identity["meta_path"], raise_on_os_error=False)
                if is_skipped_meta(existing, mode="transcript"):
                    log.info("Skipping %s (skip flag in meta.json blocks transcript)", prefix)
                    return

            # Two-step meta write: identity block lands BEFORE Gemini call so
            # failures/partials still leave a complete meta.json. See plan F11.
            identity_fields = {
                "video_url": identity["url"],
                "video_id": identity["video_id"],
                "channel": identity["channel"],
                "title": identity["title"],
                "published": identity["published"],
                "published_source": identity["published_source"],
                "model": model,
                "transcript_source": "local_file",
            }
            if start_offset is not None or end_offset is not None:
                identity_fields["segments"] = [{"start": start_offset, "end": end_offset}]
            channel_dir.mkdir(parents=True, exist_ok=True)
            update_meta(identity["meta_path"], identity_fields, mode="identity")

            file_uri = upload_local_video(client, input_path)
            media_uri = file_uri
            log.info("Transcribing local file (channel=%s): %s", channel_name, input_path.name)
        else:
            # No channel: preserve existing behavior (output next to source, stem prefix).
            file_uri = upload_local_video(client, input_path)
            video = {
                "video_id": input_path.stem,
                "url": file_uri,
                "title": input_path.stem,
                "published": datetime.fromtimestamp(input_path.stat().st_mtime).strftime("%Y-%m-%d"),
            }
            channel_dir = input_path.parent
            prefix = input_path.stem
            log.info("Transcribing local file: %s", input_path.name)
    else:
        # YouTube URL path
        output_dir = resolve_output_dir(config)
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

        # Issue #127: honor the channel's configured transcript_source on manual
        # --url runs. This path used to hand the resolver a literal {}, so an
        # operator who set transcript_source: yt-captions on a channel for cost
        # control still paid for a full Gemini call on a one-off transcript.
        # Scoped to --url on purpose: a local --file is an explicit instruction
        # to transcribe THAT file, and a channel-level captions preference
        # cannot be honored for it (there may be no corresponding caption track
        # at all), so the --file branch keeps the CLI-only answer above.
        # An unconfigured or _standalone channel resolves to {} and behaves
        # exactly as before.
        channel_cfg = channel_config_by_name(config, channel_name)
        # A manually clipped segment keeps the CLI-only answer, for the same
        # reason --file does: it is an explicit, targeted instruction. This is
        # not cosmetic. Under `transcript_source: auto` every Gemini failure
        # branch falls back to _try_captions_transcript, whose only overwrite
        # guard is `exists() and not force` - so the documented high-res segment
        # recovery (transcript --url --force --start .. --end .. --media-resolution
        # high) could replace a good full multimodal transcript with a
        # segment-clipped, speech-only captions one. Pre-#127 that was
        # unreachable here because the source was always "gemini".
        manual_segment_requested = start_offset is not None or end_offset is not None
        if manual_segment_requested:
            log.info(
                "  Manual --start/--end: keeping transcript_source=%s (channel config not applied to a clipped segment).",
                transcript_source,
            )
        else:
            transcript_source = resolve_transcript_source(channel_cfg, cli_transcript_source)
            origin = (
                "CLI flag"
                if cli_transcript_source is not None
                else ("channel config" if "transcript_source" in channel_cfg else "default")
            )
            log.info("  transcript_source=%s (from %s)", transcript_source, origin)

        video = {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": title or video_id,
            "published": date or datetime.now().strftime("%Y-%m-%d"),
        }
        channel_dir = output_dir / channel_name
        prefix = video_file_prefix(video)
        media_uri = None  # YouTube URL path: video["url"] is the media source
        # Issue #120: the manual path has no scan pre-flight to inherit the flag
        # from, so classify this one id (1 quota unit, same helper as the scan).
        # A local --file has no YouTube identity to classify, so it stays False.
        # Gated on the exists/force check that every writer downstream makes
        # anyway: a no-op re-run must not spend a YouTube quota unit to compute
        # a flag nothing will act on.
        if (channel_dir / f"{prefix}.transcript.md").exists() and not args.force:
            was_livestream = False
        else:
            was_livestream = _lookup_was_livestream(video["video_id"])
            if was_livestream:
                log.info("  Completed livestream/premiere VOD detected (issue #120).")
        # Provenance rule: an explicit transcript_source=gemini is honored, so
        # this manual run stays Gemini-first exactly as it did pre-#120. Issue
        # #127: the channel dict is passed now instead of {}, so "explicit"
        # covers a channel-level gemini too - the same view _cmd_process_url
        # has always had. Two adjacent decisions on one invocation must not
        # read different views of the same config.
        vod_captions_first = was_livestream and livestream_captions_first_applies(
            transcript_source, channel_cfg, cli_transcript_source
        )
        if was_livestream:
            log.info(
                "    VOD transcript routing: %s (transcript_source=%s)",
                "captions-first" if vod_captions_first else "Gemini-first",
                transcript_source,
            )
        log.info("Transcribing: %s", video["url"])

    # Issue #50: chunked transcript path. Auto-trigger when (a) caller is the
    # YouTube URL path, (b) no manual --start/--end is set, (c) duration lookup
    # succeeds and exceeds chunk_minutes. Otherwise fall through to the
    # single-call process_transcript path that has existed since PR #48.
    # Issue #128 review: one resolver for all four chunking sites, so a channel
    # that sets chunk_minutes: 20 gets 20 from scan AND from this command - the
    # documented recovery for the exact failure the knob exists for.
    chunk_minutes = resolve_chunk_minutes(channel_cfg, config, getattr(args, "chunk_minutes", None))
    manual_segment = start_offset is not None or end_offset is not None
    # Issue #60: yt-captions never needs chunking (the caption track is returned
    # whole regardless of length), so route it to the single-shot path which
    # handles the captions source.
    use_chunking = args.url and not manual_segment and transcript_source != "yt-captions"

    if use_chunking:
        duration_seconds = _lookup_video_duration_seconds(video["video_id"])
        if duration_seconds and duration_seconds > chunk_minutes * 60:
            # Issue #120: captions-first for a completed livestream VOD, before
            # any Gemini video call. The single-shot path gets this inside
            # process_transcript; the chunked path has to ask here, ahead of the
            # per-chunk calls. Same mechanism (_try_captions_transcript), same
            # youtube_captions provenance marker.
            if vod_captions_first:
                fb = _try_captions_transcript(
                    video,
                    channel_dir / f"{prefix}.transcript.md",
                    channel_dir / f"{prefix}.meta.json",
                    prefix,
                    reason=LIVESTREAM_CAPTIONS_FIRST_REASON,
                    force=args.force,
                )
                if fb is not None:
                    _, captions_status = fb
                    log.info("  %s: %s", prefix, captions_status)
                    log.info("  Saved: %s", channel_dir / f"{prefix}.transcript.md")
                    return
            chunks = _build_transcript_chunks(duration_seconds, chunk_minutes)
            log.info(
                "  %s is %s; running %d chunks of %d min each.",
                video["video_id"],
                _fmt_hms(duration_seconds),
                len(chunks),
                chunk_minutes,
            )
            status = _run_chunked_transcript_url(
                client=client,
                types=types,
                video=video,
                prompt_text=prompt_text,
                model=model,
                channel_dir=channel_dir,
                prefix=prefix,
                chunks=chunks,
                duration_seconds=duration_seconds,
                chunk_minutes=chunk_minutes,
                force=args.force,
            )
            out_path = channel_dir / f"{prefix}.transcript.md"
            # Issue #60: on auto, fall back to captions if the whole chunked run
            # failed (all chunks unparseable). A partial keeps the higher-fidelity
            # Gemini content; only a hard error triggers the captions failover.
            # Issue #120: skipped for a livestream VOD - captions were already
            # tried first above and there were none.
            if transcript_source == "auto" and not vod_captions_first and status.startswith("error"):
                fb = _try_captions_transcript(
                    video,
                    out_path,
                    channel_dir / f"{prefix}.meta.json",
                    prefix,
                    reason=f"chunked transcript failed: {status}",
                    force=args.force,
                )
                if fb is not None:
                    _, status = fb
            log.info("  %s: %s", prefix, status)
            if status.startswith("done"):
                log.info("  Saved: %s", out_path)
            return

    prefix, status = process_transcript(
        client,
        types,
        video,
        prompt_text,
        model,
        channel_dir,
        prefix,
        force=args.force,
        start_offset=start_offset,
        end_offset=end_offset,
        media_uri=media_uri,
        media_resolution=media_resolution_enum,
        transcript_source=transcript_source,
        livestream_captions_first=vod_captions_first,
    )
    out_path = channel_dir / f"{prefix}.transcript.md"
    log.info("  %s: %s", prefix, status)

    if status == "done":
        log.info("  Saved: %s", out_path)
    elif "skipped" in status:
        log.info("  Exists: %s", out_path)


_FILE_EXPIRY_POSITIVE_MARKERS: tuple[str, ...] = (
    "failed state",
    "not found",
    "expired",
)

_FILE_EXPIRY_NEGATIVE_MARKERS: tuple[str, ...] = (
    "quota",
    "rate",
    "safety",
    "blocked",
    "members only",
    "permission_denied",
    "permission denied",
)


def _is_file_expiry_error_status(status: str) -> bool:
    """Decide whether a helper's error-status string signals a stale Gemini file_uri.

    The helpers (``process_mindmap``, ``process_transcript``) catch exceptions
    internally and return ``(prefix, "error: <stringified-exception>")``. This
    detector parses that string and returns True only when the message references
    a Gemini Files API resource AND carries a positive expiry/not-found/failed
    marker AND lacks a negative marker that would indicate an unrelated failure
    (quota, rate-limit, safety filter, members-only permission denial).

    The files/<resource> presence matters: unrelated 403s rarely reference the
    Files API path, so that anchor disambiguates file-expiry from quota /
    safety / permission-denied errors that would otherwise share substrings.
    """
    if not status or not status.startswith("error"):
        return False
    lowered = status.lower()
    if "files/" not in lowered:
        return False
    if any(neg in lowered for neg in _FILE_EXPIRY_NEGATIVE_MARKERS):
        return False
    return any(pos in lowered for pos in _FILE_EXPIRY_POSITIVE_MARKERS)


#: Exit code for "the pipeline ran, some artifacts landed, and at least one step
#: it was asked to run produced nothing usable" (issue #129).
#:
#: A THIRD code, not a reuse of 1, because the two states need different
#: reactions. ``1`` stays reserved for a hard failure that stopped the run
#: (mindmap failed, upload failed, bad config); ``3`` says the run completed but
#: the corpus is incomplete, which is the state a batch driver has to be able to
#: see. Before #129 that state was indistinguishable from success: a concepts
#: step could report ``error``, write no ``.concepts.json``, and exit 0, and
#: because ``is_processed()`` only ever looks at transcript and mindmap
#: artifacts the video was never re-queued and simply never reached
#: ``taxonomy.json`` or the search index.
EXIT_PARTIAL = 3


def missing_pipeline_artifacts(steps: list[dict]) -> list[str]:
    """Return the labels of REQUESTED pipeline steps with no usable artifact.

    Each step is ``{"label", "requested", "status", "path"}``. A step counts as
    complete only when BOTH hold:

    * its artifact exists and is non-empty (matching ``_mode_artifact_present``'s
      existing notion of a real artifact), and
    * its status does not start with ``"error"``.

    Both halves are load-bearing. Presence alone is not enough because under
    ``--force`` a stale artifact from an earlier successful run survives a
    failed regeneration, so the file on disk can be real while the work this run
    was asked to do did not happen. Status alone is not enough either, because
    the salvage paths legitimately report a degraded status (``partial``,
    ``truncated_output``, ``thin``) while writing a genuine artifact - those are
    designed partial success and must stay exit 0, not become a false alarm.

    ``requested=False`` steps are skipped entirely and can never contribute a
    gap. That is what preserves every deliberate skip as a success: per-mode
    ``skip_modes``, ``mindmap_source: none``, the issue #120 livestream mindmap
    suppression, and concepts on an unconfigured (``_standalone``) channel. A
    skip the operator asked for is not an incomplete corpus.

    Every key is read with REQUIRED access, never ``.get()``. A ``.get("requested")``
    would treat a missing or misspelled key as ``False`` and silently drop that
    step from gap detection - a guard against silent incompleteness that can
    itself be silently disabled, which is the exact failure class this function
    exists to close. A malformed step must raise at the call site instead.
    """
    missing: list[str] = []
    for step in steps:
        if not step["requested"]:
            continue
        path = step["path"]
        present = path is not None and path.exists() and path.stat().st_size > 0
        if not present or str(step["status"] or "").startswith("error"):
            missing.append(step["label"])
    return missing


def finish_pipeline_run(steps: list[dict], *, label: str) -> None:
    """Exit ``EXIT_PARTIAL`` when a requested step left no usable artifact.

    Returns normally (exit 0) when everything requested is on disk. Called at
    every terminal point of both ``process`` orchestrators, including the
    deliberate-skip early returns - a skip still has to prove the steps that DID
    run left their artifacts behind.
    """
    gaps = missing_pipeline_artifacts(steps)
    if not gaps:
        return
    log.error(
        "Pipeline incomplete for %s: no usable artifact from %s. Exiting %d (partial); re-run to fill the gap.",
        label,
        ", ".join(gaps),
        EXIT_PARTIAL,
    )
    sys.exit(EXIT_PARTIAL)


def _cmd_process_url(args, config):
    """`process --url` orchestrator (issue #54 ordering): transcript first
    (chunked if long, per PR #51), then mindmap built from the on-disk
    transcript text (text-only Gemini call), then concepts.

    Falls back to mindmap-from-video when no transcript is available AND the
    channel's ``mindmap_source`` resolves to ``"auto"`` or ``"video"``. The
    legacy mindmap-fps fallback for the 10800-frame video cap is preserved
    only on that fallback path because text input has no frame cap.

    Shares process --file's exit-code contract - see ``cmd_process``.
    """
    _, types = require_gemini()
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)
    client = create_client(gemini_key)
    output_dir = resolve_output_dir(config)
    model = resolve_model(args, config)
    # Resolve --media-resolution flag for the legacy single-shot transcript fallback
    # below. Default LOW; guarded against types=None for test stubs.
    media_resolution_enum = (
        _resolve_media_resolution(types, getattr(args, "media_resolution", "low")) if types is not None else None
    )

    video_id_match = re.search(r"(?:v=|/)([a-zA-Z0-9_-]{11})", args.url)
    if not video_id_match:
        log.error("Could not extract video ID from: %s", args.url)
        sys.exit(1)
    video_id = video_id_match.group(1)

    channel_name = args.channel
    title = args.title
    date = args.date
    if not channel_name or not title or not date:
        yt_key = os.environ.get("YOUTUBE_API_KEY")
        if yt_key:
            yt_build = require_youtube()
            yt = yt_build("youtube", "v3", developerKey=yt_key)
            resp = yt.videos().list(part="snippet", id=video_id).execute()
            if resp.get("items"):
                snippet = resp["items"][0]["snippet"]
                title = title or unescape(snippet["title"])
                date = date or snippet["publishedAt"][:10]
                if not channel_name:
                    yt_channel_id = snippet["channelId"]
                    for ch in config.get("channels", []):
                        ch_id, _ = get_channel_id(yt, ch["url"])
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
    channel_dir = output_dir / channel_name
    computed_prefix = video_file_prefix(video)
    # Title-rotation safety net (PR #31 follow-up): consult the channel's
    # video_id index so transcript+mindmap stay co-located under the
    # ORIGINAL prefix when YouTube creators A/B-test titles. Otherwise
    # transcript Step 1 would write to the new prefix while any prior
    # transcript at the old prefix becomes orphaned, AND the mindmap
    # resolver in Step 2 would miss the prior transcript and fall back
    # to source=video. Reproducer: simonscrapes / Master Claude Code.
    indexed_prefix = _load_video_id_index(channel_dir).get(video_id)
    prefix = indexed_prefix if indexed_prefix else computed_prefix
    if indexed_prefix and indexed_prefix != computed_prefix:
        log.info(
            "  Title-rotation: using existing prefix %r (computed would have been %r)",
            indexed_prefix,
            computed_prefix,
        )

    prompt_name = normalize_prompt_name(
        getattr(args, "prompt", None) or config.get("default_prompt", "mindmap-knowledge")
    )
    mindmap_prompt = load_prompt(prompt_name)
    transcript_prompt = load_prompt("transcript")
    mindmap_from_transcript_prompt = load_prompt("mindmap-from-transcript")

    log.info("[process --url] %s", video["url"])

    # Resolve mindmap source from the channel config (default "auto").
    channel_cfg: dict = channel_config_by_name(config, channel_name)
    # Issue #60: transcript source - CLI flag overrides the per-channel knob.
    transcript_source = resolve_transcript_source(channel_cfg, getattr(args, "transcript_source", None))

    duration_seconds = _lookup_video_duration_seconds(video_id)
    transcript_path = channel_dir / f"{prefix}.transcript.md"
    # Issue #120: classify this one id (1 quota unit) so the manual --url path
    # routes a completed livestream VOD the same way the scan does. Gated on the
    # same exists/force check every writer downstream makes, so a no-op re-run
    # does not spend a quota unit on a flag nothing will act on. (When the
    # transcript is already on disk the Step 2 resolver picks source=transcript,
    # which the livestream mindmap block never applies to.)
    if transcript_path.exists() and not args.force:
        was_livestream = False
    else:
        was_livestream = _lookup_was_livestream(video_id)
        if was_livestream:
            log.info("  Completed livestream/premiere VOD detected (issue #120).")
    # Provenance rule: captions-first only when nobody explicitly asked for
    # Gemini. Both provenances are available here - the channel dict and the
    # CLI flag - so this is the one site that exercises the full precedence.
    vod_captions_first = was_livestream and livestream_captions_first_applies(
        transcript_source, channel_cfg, getattr(args, "transcript_source", None)
    )
    if was_livestream:
        log.info(
            "    VOD transcript routing: %s",
            "captions-first" if vod_captions_first else "Gemini-first (explicit transcript_source=gemini)",
        )

    # Step 1/3: transcript (chunked if long, per PR #51 path).
    # Review K1: catch any uncaught exception so the mindmap step (the AI's
    # primary discovery surface) still runs with the legacy video-source
    # fallback. Without this guard, a single transient transcript failure
    # would skip mindmap entirely - breaking the user's "mindmap always
    # runs" invariant (memory: feedback_long_video_keep_mindmap).
    log.info("  Step 1/3: transcript")
    chunk_minutes = resolve_chunk_minutes(channel_cfg, config, getattr(args, "chunk_minutes", None))
    try:
        # Issue #60: yt-captions never chunks (caption track is whole); route it
        # to the single-shot path which builds from captions.
        if duration_seconds and duration_seconds > chunk_minutes * 60 and transcript_source != "yt-captions":
            # Issue #120: captions-first for a completed livestream VOD, before
            # any Gemini video call. The chunked path has to ask here, ahead of
            # the per-chunk calls; the single-shot branch below inherits the
            # same behavior from process_transcript(livestream_captions_first=).
            captions_first = (
                _try_captions_transcript(
                    video,
                    transcript_path,
                    channel_dir / f"{prefix}.meta.json",
                    prefix,
                    reason=LIVESTREAM_CAPTIONS_FIRST_REASON,
                    force=args.force,
                )
                if vod_captions_first
                else None
            )
            if captions_first is not None:
                _, transcript_status = captions_first
            else:
                chunks = _build_transcript_chunks(duration_seconds, chunk_minutes)
                log.info(
                    "    %s is %s; running %d chunks of %d min each.",
                    video_id,
                    _fmt_hms(duration_seconds),
                    len(chunks),
                    chunk_minutes,
                )
                transcript_status = _run_chunked_transcript_url(
                    client=client,
                    types=types,
                    video=video,
                    prompt_text=transcript_prompt,
                    model=model,
                    channel_dir=channel_dir,
                    prefix=prefix,
                    chunks=chunks,
                    duration_seconds=duration_seconds,
                    chunk_minutes=chunk_minutes,
                    force=args.force,
                )
                # Issue #60: on auto, fall back to captions if the chunked run
                # failed outright (a partial keeps the higher-fidelity Gemini
                # content). Issue #120: skipped for a livestream VOD - captions
                # were already tried first above and there were none.
                if transcript_source == "auto" and not vod_captions_first and transcript_status.startswith("error"):
                    fb = _try_captions_transcript(
                        video,
                        transcript_path,
                        channel_dir / f"{prefix}.meta.json",
                        prefix,
                        reason=f"chunked transcript failed: {transcript_status}",
                        force=args.force,
                    )
                    if fb is not None:
                        _, transcript_status = fb
        else:
            _, transcript_status = process_transcript(
                client,
                types,
                video,
                transcript_prompt,
                model,
                channel_dir,
                prefix,
                force=args.force,
                media_uri=None,
                media_resolution=media_resolution_enum,
                transcript_source=transcript_source,
                livestream_captions_first=vod_captions_first,
            )
    except Exception as exc:
        transcript_status = f"error: {exc}"
        log.warning("    transcript [%s] raised: %s", prefix, exc)
    log.info("    transcript [%s]: %s", prefix, transcript_status)
    if transcript_status.startswith("error"):
        _log_chunk_recovery_recipe(video, duration_seconds, chunk_minutes)
    # Issue #129: accumulate what this run was asked to produce, so every exit
    # below can tell "we are done" from "we ran and left a hole".
    steps: list[dict] = [
        {"label": "transcript", "requested": True, "status": transcript_status, "path": transcript_path}
    ]

    # Step 2/3: mindmap. Resolver picks source based on channel config and
    # whether transcript exists on disk now (it may have been written by Step 1
    # this run, or already present from a prior run).
    transcript_available = transcript_path.exists()
    try:
        resolved_source = resolve_mindmap_source(channel_cfg, transcript_available=transcript_available)
    except ValueError as exc:
        log.error("Mindmap source unresolvable for %s: %s", video_id, exc)
        sys.exit(1)

    if resolved_source == "skip":
        log.info("  Step 2/3: mindmap [skipped (mindmap_source=none)]")
        log.info("  Step 3/3: concepts [skipped (no mindmap)]")
        finish_pipeline_run(steps, label=prefix)
        return

    # Issue #120: same rule the scan applies - a livestream VOD whose transcript
    # attempt just failed has a URI Gemini cannot ingest, so the fallback
    # mindmap-from-video call is never spent against it. The skip itself is
    # deliberate and contributes no gap (requested=False, so it is never in
    # `steps`). The exit code here is decided entirely by the transcript
    # step, which by this branch's own precondition just failed - so this
    # path exits EXIT_PARTIAL, not 0 (issue #129 changed that; before, a
    # livestream VOD with no transcript and no mindmap exited 0).
    if should_skip_video_mindmap_for_livestream(
        was_livestream=was_livestream,
        resolved_source=resolved_source,
        transcript_status=transcript_status,
    ):
        log.warning("  Step 2/3: mindmap [%s]", LIVESTREAM_MINDMAP_SKIP_STATUS)
        _log_livestream_recovery_recipe(video, channel_name)
        log.info("  Step 3/3: concepts [skipped (no mindmap)]")
        finish_pipeline_run(steps, label=prefix)
        return

    log.info("  Step 2/3: mindmap (source=%s)", resolved_source)
    if resolved_source == "transcript":
        mindmap_prefix, mindmap_status = process_mindmap(
            client,
            types,
            video,
            mindmap_from_transcript_prompt,
            model,
            output_dir,
            channel_name,
            prompt_name="mindmap-from-transcript",
            force=args.force,
            # Title rotation (ce-code-review, adversarial, PR #136): without an
            # explicit prefix, process_mindmap recomputes it from the CURRENT
            # title and writes under the new slug, while everything downstream
            # - the exit-code check, find_mindmap_source, concepts - looks under
            # the indexed prefix. The comment above already promises co-location;
            # this is what delivers it.
            prefix=prefix,
            source="transcript",
            transcript_path=transcript_path,
        )
    else:
        # Legacy path: mindmap watches the video. Issue #50 fps fallback for
        # the 10800-frame cap is preserved here only - text input has no cap.
        mindmap_fps: float | None = None
        if duration_seconds and duration_seconds > 10000:
            mindmap_fps = 0.5
            log.info(
                "  Long video (%s); reducing mindmap fps to %.1f to fit Gemini's 10800-frame cap.",
                _fmt_hms(duration_seconds),
                mindmap_fps,
            )
        mindmap_prefix, mindmap_status = process_mindmap(
            client,
            types,
            video,
            mindmap_prompt,
            model,
            output_dir,
            channel_name,
            prompt_name=prompt_name,
            force=args.force,
            fps=mindmap_fps,
            prefix=prefix,
            source="video",
        )
    log.info("    mindmap [%s]: %s", mindmap_prefix, mindmap_status)
    if mindmap_status.startswith("error"):
        log.error("Mindmap failed; skipping concepts.")
        sys.exit(1)
    steps.append(
        {
            "label": "mindmap",
            "requested": True,
            "status": mindmap_status,
            "path": find_mindmap_source(channel_dir, prefix),
        }
    )

    log.info("  Step 3/3: concepts")
    if channel_name == "_standalone":
        log.warning("    No configured channel for %s; skipping concepts.", video_id)
        finish_pipeline_run(steps, label=prefix)
        return
    mindmap_path = find_mindmap_source(channel_dir, prefix)
    if not mindmap_path or not mindmap_path.exists():
        log.warning("    Mindmap not on disk; skipping concepts.")
        finish_pipeline_run(steps, label=prefix)
        return
    mindmap_text = mindmap_path.read_text(encoding="utf-8")
    taxonomy = load_taxonomy(output_dir)
    concepts_prompt_name = "mindmap-from-transcript" if resolved_source == "transcript" else prompt_name
    concepts_prefix, concepts_status = process_concepts(
        client,
        types,
        video,
        mindmap_text,
        taxonomy,
        model,
        output_dir,
        channel_name,
        source_file=mindmap_path.name,
        source_prompt=concepts_prompt_name,
        prefix=prefix,
    )
    log.info("    concepts [%s]: %s", concepts_prefix, concepts_status)
    steps.append(
        {
            "label": "concepts",
            "requested": True,
            "status": concepts_status,
            "path": channel_dir / f"{prefix}.concepts.json",
        }
    )
    finish_pipeline_run(steps, label=prefix)


def cmd_process(args, config):
    """Run the full pipeline (mindmap + transcript + concepts) on a single video.

    Two input modes:
      --file PATH: local MP4 - one Gemini upload, lazy-skipped when meta records
        all modes complete and artifacts exist (legacy local-file path).
      --url URL: YouTube URL - chunked transcript via _run_chunked_transcript_url
        when duration exceeds chunk_minutes; otherwise single call (issue #50).

    Exit-code contract (issue #129 made this tri-state; it was 0-or-1 before):

    * ``0`` - every step this run was asked to produce left a usable artifact.
      Deliberate skips (``skip_modes``, ``mindmap_source: none``, the issue #120
      livestream mindmap suppression, concepts on an unconfigured channel) are
      not "asked for" and keep exit 0. So do the degraded-but-real salvage
      statuses (``partial``, ``truncated_output``, ``thin``).
    * ``EXIT_PARTIAL`` (3) - the run finished but a requested step produced no
      usable artifact. Most often a transcript that failed while the mindmap
      succeeded, or a concepts step that failed.
    * ``1`` - hard failure that stopped the run: mindmap failed, upload failed,
      unresolvable config.

    What changed and why: exit 0 used to mean "the mindmap succeeded", so a
    batch driver checking the exit code could not see an incomplete video at
    all. The preservation half of that contract is unchanged - a transcript or
    concepts failure still leaves the mindmap on disk and never rolls anything
    back - but "some artifacts landed" is now reported as 3 rather than
    disguised as success. A caller that wants the old lenient behavior treats
    ``rc in (0, 3)`` as non-fatal; ``modes_completed`` and the per-stage
    ``transcript_status`` / ``concepts_status`` fields in meta.json remain the
    finer-grained record.
    """
    if getattr(args, "url", None):
        return _cmd_process_url(args, config)
    _, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)

    client = create_client(gemini_key)
    output_dir = resolve_output_dir(config)
    model = resolve_model(args, config)
    # Resolve --media-resolution flag once at command boundary; threaded into
    # the mindmap step below. Local-file path stays on legacy mindmap-from-video,
    # so this flag is the user's only knob to override the LOW default.
    # Guarded against types=None for test stubs that bypass require_gemini —
    # process_mindmap accepts media_resolution=None and falls back to LOW.
    media_resolution_enum = (
        _resolve_media_resolution(types, getattr(args, "media_resolution", "low")) if types is not None else None
    )

    start_offset = parse_time_to_seconds(args.start) if args.start else None
    end_offset = parse_time_to_seconds(args.end) if args.end else None

    input_path = Path(args.file).resolve()
    if not input_path.exists():
        log.error("File not found: %s", input_path)
        sys.exit(1)

    size = input_path.stat().st_size
    has_segment = start_offset is not None or end_offset is not None
    if size > LARGE_FILE_THRESHOLD_BYTES and not has_segment:
        size_gb = size / 1024 / 1024 / 1024
        log.error(
            "File is %.1fGB. Specify --start and --end to process a segment (Gemini's 2GB upload limit applies).",
            size_gb,
        )
        sys.exit(1)

    # Channel resolution mirrors cmd_transcript's --file path.
    channel_name = args.channel or infer_channel_from_file_path(input_path, output_dir, config)
    if args.channel:
        require_channels_config(config)
        configured = {c["name"] for c in config.get("channels", [])}
        if args.channel not in configured:
            log.error("Channel '%s' not found in config.yaml", args.channel)
            sys.exit(1)

    prompt_name = args.prompt or config.get("default_prompt", "mindmap-knowledge")
    prompt_name = normalize_prompt_name(prompt_name)
    mindmap_prompt = load_prompt(prompt_name)
    transcript_prompt = load_prompt("transcript")

    if channel_name:
        channel_dir_hint = output_dir / channel_name
        identity = resolve_local_file_identity(
            input_path, channel_name=channel_name, channel_dir=channel_dir_hint, args=args
        )
        video = {
            "video_id": identity["video_id"],
            "url": identity["url"],
            "title": identity["title"],
            "published": identity["published"],
        }
        channel_dir = identity["channel_dir"]
        prefix = identity["prefix"]
        meta_path = identity["meta_path"]

        if meta_path.exists():
            existing_meta = _read_meta_best_effort(meta_path, raise_on_os_error=False)
            # Issue #42: legacy `skip: true` still hard-exits; `skip_modes` is
            # honored per-mode below by gating needs_mindmap / needs_transcript.
            if existing_meta.get("skip") is True and "skip_modes" not in existing_meta:
                log.info("Skipping %s (skip=true in meta.json)", prefix)
                return
        else:
            existing_meta = {}
    else:
        # No channel: loose-file mode, artifacts next to source (stem prefix).
        video = {
            "video_id": input_path.stem,
            "url": "",
            "title": input_path.stem,
            "published": datetime.fromtimestamp(input_path.stat().st_mtime).strftime("%Y-%m-%d"),
        }
        channel_dir = input_path.parent
        prefix = input_path.stem
        meta_path = channel_dir / f"{prefix}.meta.json"
        if meta_path.exists():
            existing_meta = _read_meta_best_effort(meta_path, raise_on_os_error=False)
        else:
            existing_meta = {}

    # Lazy-upload decision: gated on meta.json modes_completed, not just filesystem.
    modes_done = set(existing_meta.get("modes_completed", []))
    mindmap_path = channel_dir / f"{prefix}.mindmap.md"
    transcript_path = channel_dir / f"{prefix}.transcript.md"
    raw_sidecar = channel_dir / f"{prefix}.transcript.raw.txt"

    # Issue #42: per-mode skip from meta.skip_modes silences the corresponding
    # step. Legacy `skip: true` was hard-exited above and never reaches here.
    skip_mindmap = is_skipped_meta(existing_meta, mode="mindmap")
    skip_transcript = is_skipped_meta(existing_meta, mode="transcript")
    # The issue #42 contract accepts any subset of mindmap|transcript|concepts,
    # but only the first two were ever gated here, so `skip_modes: ["concepts"]`
    # silently did nothing and the step ran anyway. A documented mode that does
    # nothing is the same class of lie as a finished ticket left open.
    skip_concepts = is_skipped_meta(existing_meta, mode="concepts")
    needs_mindmap = not skip_mindmap and (args.force or "scan" not in modes_done or not mindmap_path.exists())
    needs_transcript = not skip_transcript and (
        args.force or "transcript" not in modes_done or not transcript_path.exists() or raw_sidecar.exists()
    )
    # When the orchestrator has decided a step needs work (missing artifact, stale
    # modes_completed, or salvage sidecar present), the per-helper `force` flag
    # must match - otherwise process_mindmap / process_transcript short-circuit on
    # their own `file.exists() and not force` check and we pay for an upload with
    # nothing to show for it.
    mindmap_force = args.force or needs_mindmap
    transcript_force = args.force or needs_transcript

    file_uri: str | None = None
    if needs_mindmap or needs_transcript:
        channel_dir.mkdir(parents=True, exist_ok=True)
        # Two-step meta write: identity block lands before the first Gemini call.
        identity_fields = {
            "video_url": video["url"],
            "video_id": video["video_id"],
            "channel": channel_name or "",
            "title": video["title"],
            "published": video["published"],
            "model": model,
            "prompt": prompt_name,
            "transcript_source": "local_file",
        }
        if channel_name and "published_source" in identity:
            identity_fields["published_source"] = identity["published_source"]
        if start_offset is not None or end_offset is not None:
            identity_fields["segments"] = [{"start": start_offset, "end": end_offset}]
        update_meta(meta_path, identity_fields, mode="identity")
        try:
            file_uri = upload_local_video(client, input_path)
        except Exception as e:
            log.error("Upload failed: %s", e)
            # update_meta resets last_error to None at the end, so write directly
            # to persist the failure marker for later runs / dashboards. Mirrors
            # process_mindmap's error-recording pattern.
            # Best-effort read (issue #124): a corrupt meta here would mask the
            # upload error AND skip the sys.exit(1) below, silently changing the
            # exit code after a multi-minute upload.
            meta: dict = _read_meta_best_effort(meta_path, raise_on_os_error=False)
            meta["last_error"] = f"upload: {e}"
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            sys.exit(1)
        log.info("Processing local file (channel=%s): %s", channel_name or "_loose", input_path.name)
    else:
        log.info("All pipeline artifacts up to date for %s (no upload).", prefix)

    # Shared re-upload counter: bounded at one re-upload per invocation across
    # both transcript and mindmap steps (file-expiry fallback).
    reupload_available = [True]

    def _call_with_file_expiry_retry(label: str, call_fn):
        """Invoke a helper; on file-expiry, re-upload once and retry once."""
        nonlocal file_uri
        result = call_fn(file_uri)
        status = result[1]
        if _is_file_expiry_error_status(status) and reupload_available[0]:
            reupload_available[0] = False
            log.warning(
                "File-expiry detected on %s (%s); re-uploading once and retrying.",
                label,
                status,
            )
            try:
                file_uri = upload_local_video(client, input_path)
            except Exception as e:
                log.error("Re-upload failed: %s. Giving up on %s.", e, label)
                return result
            result = call_fn(file_uri)
        return result

    # Resolve channel config for the mindmap_source resolver below.
    channel_cfg: dict = next(
        (c for c in config.get("channels", []) if c.get("name") == channel_name),
        {},
    )
    chunk_minutes = resolve_chunk_minutes(channel_cfg, config, getattr(args, "chunk_minutes", None))
    mindmap_from_transcript_prompt = load_prompt("mindmap-from-transcript")

    # ============================================================================
    # Step 1/3: transcript (chunked for long videos, single-shot otherwise).
    #
    # Issue #54 ordering applied to local files (2026-05-02): transcript runs
    # FIRST so the mindmap step below can read the on-disk transcript via the
    # cheap text-only path. This mirrors `_cmd_process_url` and resolves the
    # cost asymmetry where mindmap-from-video on hour-long local files cost
    # ~10x more than mindmap-from-transcript would. It also makes long single-
    # shot transcripts (which Pro returns malformed JSON for intermittently)
    # work reliably via chunking — same upload, multiple Gemini calls with
    # VideoMetadata.start_offset/end_offset offsets, "one upload" guarantee
    # preserved (the empirical cached=560495 hit on a follow-up call against
    # the same file_uri proves implicit caching kicks in).
    #
    # Wrapped in try/except so an uncaught transcript exception still lets the
    # mindmap step run (with `source="video"` fallback via the resolver). This
    # mirrors `_cmd_process_url`'s "mindmap is the AI's discovery surface and
    # must always run" invariant from CLAUDE.md.
    # ============================================================================
    duration_seconds = _local_file_duration_seconds(input_path) if not has_segment else None

    if skip_transcript:
        transcript_status = "skipped (skip_modes)"
        log.info("  Step 1/3: transcript [%s]: %s", prefix, transcript_status)
    else:
        log.info("  Step 1/3: transcript")
        try:
            if duration_seconds and duration_seconds > chunk_minutes * 60:
                # Long file: chunk via VideoMetadata.start_offset/end_offset against the same file_uri.
                # No re-upload; the existing single upload is referenced by N calls.
                chunks = _build_transcript_chunks(duration_seconds, chunk_minutes)
                log.info(
                    "    %s is %s; running %d chunks of %d min each.",
                    input_path.name,
                    _fmt_hms(duration_seconds),
                    len(chunks),
                    chunk_minutes,
                )

                def _chunked_transcript_call(uri):
                    status = _run_chunked_transcript_url(
                        client=client,
                        types=types,
                        video=video,
                        prompt_text=transcript_prompt,
                        model=model,
                        channel_dir=channel_dir,
                        prefix=prefix,
                        chunks=chunks,
                        duration_seconds=duration_seconds,
                        chunk_minutes=chunk_minutes,
                        force=transcript_force,
                        media_uri=uri,
                    )
                    return prefix, status

                _, transcript_status = _call_with_file_expiry_retry("transcript", _chunked_transcript_call)
            else:
                # Short file or manual --start/--end segment: single-shot.
                def _transcript_call(uri):
                    return process_transcript(
                        client,
                        types,
                        video,
                        transcript_prompt,
                        model,
                        channel_dir,
                        prefix,
                        force=transcript_force,
                        start_offset=start_offset,
                        end_offset=end_offset,
                        media_uri=uri,
                        media_resolution=media_resolution_enum,
                    )

                _, transcript_status = _call_with_file_expiry_retry("transcript", _transcript_call)
        except Exception as exc:
            transcript_status = f"error: {exc}"
            log.warning("    transcript [%s] raised: %s", prefix, exc)
        log.info("    transcript [%s]: %s", prefix, transcript_status)

    # Issue #129: a per-mode skip is `requested=False` and can never be a gap;
    # a step that ran and left nothing can.
    steps: list[dict] = [
        {
            "label": "transcript",
            "requested": not skip_transcript,
            "status": transcript_status,
            "path": transcript_path,
        }
    ]

    # ============================================================================
    # Step 2/3: mindmap. Resolver picks source based on channel config and whether
    # transcript exists on disk now (it may have been written by Step 1 this run,
    # or already present from a prior run). Falls back to `source="video"` when
    # no transcript exists and `mindmap_source` is auto/video.
    # ============================================================================
    transcript_available = transcript_path.exists()
    try:
        resolved_source = resolve_mindmap_source(channel_cfg, transcript_available=transcript_available)
    except ValueError as exc:
        log.error("Mindmap source unresolvable: %s", exc)
        sys.exit(1)

    if skip_mindmap:
        mindmap_status = "skipped (skip_modes)"
        log.info("  Step 2/3: mindmap [%s]: %s", prefix, mindmap_status)
    elif resolved_source == "skip":
        mindmap_status = "skipped (mindmap_source=none)"
        log.info("  Step 2/3: mindmap [%s]: %s", prefix, mindmap_status)
    else:
        log.info("  Step 2/3: mindmap (source=%s)", resolved_source)
        if resolved_source == "transcript":
            # Cheap text-only call against the on-disk transcript. No file_uri needed.
            _, mindmap_status = process_mindmap(
                client,
                types,
                video,
                mindmap_from_transcript_prompt,
                model,
                output_dir,
                channel_name or "",
                prompt_name="mindmap-from-transcript",
                force=mindmap_force,
                prefix=prefix,
                channel_dir_override=channel_dir,
                source="transcript",
                transcript_path=transcript_path,
            )
        else:
            # Legacy mindmap-from-video fallback. Watches the same upload.
            def _mindmap_call(uri):
                return process_mindmap(
                    client,
                    types,
                    video,
                    mindmap_prompt,
                    model,
                    output_dir,
                    channel_name or "",
                    prompt_name=prompt_name,
                    force=mindmap_force,
                    prefix=prefix,
                    channel_dir_override=channel_dir,
                    media_uri=uri,
                    media_resolution=media_resolution_enum,
                )

            _, mindmap_status = _call_with_file_expiry_retry("mindmap", _mindmap_call)
        log.info("    mindmap [%s]: %s", prefix, mindmap_status)
        if mindmap_status.startswith("error"):
            log.error("Mindmap failed; aborting concepts step.")
            sys.exit(1)
    steps.append(
        {
            "label": "mindmap",
            "requested": not skip_mindmap and resolved_source != "skip",
            "status": mindmap_status,
            "path": find_mindmap_source(channel_dir, prefix),
        }
    )

    # If transcript failed (and we're past the mindmap step which may have
    # succeeded via video fallback), abort before concepts.
    #
    # Issue #129 note: this skip is NOT modelled as `requested=False`. Unlike
    # skip_modes or mindmap_source=none, nobody asked for it - it is a
    # consequence of a failure, and the concepts artifact really is missing.
    # The transcript step has already failed by this point so the run exits
    # EXIT_PARTIAL regardless; what matters is that we do not relabel a
    # failure-driven omission as a deliberate skip. (Which step to run after a
    # transcript failure is deliberately left as-is: the --url path runs
    # concepts here and --file does not. Changing that spends a Gemini call
    # that was not spent before, so it belongs in its own ticket.)
    if not skip_transcript and transcript_status.startswith("error"):
        log.warning("Transcript failed; skipping concepts. Mindmap artifact preserved if any.")
        finish_pipeline_run(steps, label=prefix)
        return

    # Step 3: concepts (text-only; channel must be configured).
    if skip_concepts:
        log.info("  Step 3/3: concepts [%s]: skipped (skip_modes)", prefix)
        steps.append({"label": "concepts", "requested": False, "status": "skipped (skip_modes)", "path": None})
        finish_pipeline_run(steps, label=prefix)
        return

    if not channel_name:
        log.warning("Channel not configured for %s; skipping concepts.", input_path.name)
        finish_pipeline_run(steps, label=prefix)
        return

    if not mindmap_path.exists():
        log.warning("Mindmap file not on disk; skipping concepts.")
        finish_pipeline_run(steps, label=prefix)
        return

    mindmap_text = mindmap_path.read_text(encoding="utf-8")
    taxonomy = load_taxonomy(output_dir)
    try:
        _, concepts_status = process_concepts(
            client,
            types,
            video,
            mindmap_text,
            taxonomy,
            model,
            output_dir,
            channel_name,
            source_file=mindmap_path.name,
            source_prompt=prompt_name,
            force=args.force,
            prefix=prefix,
        )
        log.info("  concepts [%s]: %s", prefix, concepts_status)
    except Exception as e:
        concepts_status = f"error: {e}"
        log.warning("Concepts failed for %s: %s (mindmap and transcript preserved)", prefix, e)
        _record_concepts_error(meta_path, video, channel_dir, str(e))
    steps.append(
        {
            "label": "concepts",
            "requested": True,
            "status": concepts_status,
            # process_concepts hardcodes output_dir / channel_name; the
            # sibling-meta identity branch sets channel_dir to input_path.parent,
            # so re-deriving from channel_dir here reported a gap for an artifact
            # that WAS written - a permanent false exit 3 on a healthy run
            # (ce-code-review: correctness AND adversarial, both with repros).
            # Ask for the writer's destination, never re-derive a second one.
            "path": output_dir / channel_name / f"{prefix}.concepts.json",
        }
    )
    finish_pipeline_run(steps, label=prefix)


def cmd_concepts(args, config):
    """Extract and normalize concepts from existing mindmaps."""
    require_channels_config(config)
    _, types = require_gemini()

    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        log.error("GEMINI_API_KEY not set.")
        sys.exit(1)

    client = create_client(gemini_key)
    output_dir = resolve_output_dir(config)
    model = resolve_model(args, config)
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


def cmd_mark_skip(args, config) -> None:
    """Set ``skip_modes`` (and optional ``skip_reason``) on a video's meta.json.

    Issue #42: instead of hand-editing JSON to silence transcript-only on a
    long-form video, the user runs:

        mark-skip --url URL --mode transcript [--mode concepts] [--reason TEXT]

    Resolution: extract video_id from --url, walk every configured channel's
    output folder for any meta.json carrying that video_id, then update.
    Errors clearly when nothing matches. Validation is positive (argparse
    `choices`); unknown modes never reach this function.
    """
    output_dir = resolve_output_dir(config)

    video_id_match = re.search(r"(?:v=|/)([a-zA-Z0-9_-]{11})", args.url or "")
    if not video_id_match:
        log.error("Could not extract a YouTube video ID from --url: %s", args.url)
        sys.exit(1)
    video_id = video_id_match.group(1)

    found: list[Path] = []
    if output_dir.exists():
        for meta_path in output_dir.glob("*/*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if meta.get("video_id") == video_id:
                found.append(meta_path)

    if not found:
        log.error(
            "No meta.json found for video_id %s under %s. "
            "Run scan or transcript --url first so the meta exists, then re-run mark-skip.",
            video_id,
            output_dir,
        )
        sys.exit(1)

    new_modes = list(args.mode or [])
    for meta_path in found:
        meta = _read_meta_best_effort(meta_path, raise_on_os_error=False)
        existing = meta.get("skip_modes")
        existing_list = list(existing) if isinstance(existing, list) else []
        merged = list(existing_list)
        for mode in new_modes:
            if mode not in merged:
                merged.append(mode)
        # Carry video_id forward: update_meta re-reads the file, and if that
        # read comes back unusable this writer would otherwise persist a meta
        # with no identity - destroying the very key the lookup above used.
        update_fields: dict = {"skip_modes": merged}
        if meta.get("video_id"):
            update_fields["video_id"] = meta["video_id"]
        if args.reason:
            update_fields["skip_reason"] = args.reason
        update_meta(meta_path, update_fields, mode="identity")
        log.info("[%s] skip_modes=%s -> %s", meta_path.parent.name, existing_list, merged)


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
# Floor for adaptive batch-halving on Voyage token-cap errors. When a batch
# at this size still trips the cap, the chunk content itself is pathological
# and we raise rather than recurse further. See issue #44.
MIN_BATCH_SIZE = 4


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
    """Embed a list of texts with Voyage AI, with adaptive batch-halving on
    per-batch token-cap errors plus exponential backoff on rate-limit and
    connection errors.

    Token-cap recovery (issue #44): Voyage rejects batches whose total token
    count exceeds the model's per-batch cap (120,000 for voyage-4-large) with
    an InvalidRequestError. The match is substring-based against two stable
    phrases ("max allowed tokens" and "tokens per submitted batch") so a minor
    SDK rewording does not silently regress the recovery path. Token-cap
    detection takes precedence over rate-limit detection: a message that
    happens to contain both substrings means split, not backoff -- the
    underlying problem is sizing, not pacing.

    Halving stops at MIN_BATCH_SIZE: below that, a single chunk is genuinely
    pathological and we raise. On any raise from this helper, no LanceDB
    write occurs and the previous index (if any) is preserved -- recovery is
    to re-run `index --force` from scratch; there is no resume.
    """
    all_embeddings: list[list[float]] = []
    pending: list[list[str]] = [texts[i : i + VOYAGE_BATCH_SIZE] for i in range(0, len(texts), VOYAGE_BATCH_SIZE)]
    done = 0

    try:
        while pending:
            batch = pending.pop(0)
            max_retries = 5
            attempt = 0
            while True:
                try:
                    result = vo_client.embed(batch, model=model, input_type=input_type)
                    all_embeddings.extend(result.embeddings)
                    done += 1
                    # ~total grows as splits happen; the tilde signals an estimate.
                    log.info("[%d/~%d] Embedded %d chunks", done, done + len(pending), len(batch))
                    if pending:
                        time.sleep(1)
                    break
                except Exception as e:
                    error_str = str(e).lower()
                    is_token_cap = "max allowed tokens" in error_str or "tokens per submitted batch" in error_str
                    is_rate_limit = "rate" in error_str and "limit" in error_str
                    is_connection = "connection" in error_str or "resolve" in error_str or "timeout" in error_str

                    # Token-cap precedence: split rather than retry.
                    if is_token_cap and len(batch) > MIN_BATCH_SIZE:
                        mid = len(batch) // 2
                        log.warning(
                            "Voyage batch too large (%d chunks); splitting into %d + %d",
                            len(batch),
                            mid,
                            len(batch) - mid,
                        )
                        pending = [batch[:mid], batch[mid:], *pending]
                        break  # exit inner retry loop, dequeue next batch

                    if is_token_cap:
                        # At MIN_BATCH_SIZE floor: a single chunk is too large.
                        log.error(
                            "Voyage batch at floor size %d still exceeds token cap; raising",
                            len(batch),
                        )
                        raise

                    if (is_rate_limit or is_connection) and attempt < max_retries:
                        wait = 25 * (2**attempt) + random.uniform(0, 5)
                        reason = "rate limited" if is_rate_limit else "connection error"
                        log.warning("Voyage %s, waiting %ds (attempt %d/%d)...", reason, wait, attempt + 1, max_retries)
                        time.sleep(wait)
                        attempt += 1
                        continue

                    raise
    except Exception:
        # Mid-run failure: caller's `db.create_table(... mode="overwrite")` is
        # downstream of us, so the previous index survives. Surface the sunk
        # Voyage spend so the user can see it before re-running.
        if done > 0:
            log.warning(
                "Voyage spend before failure: %d batches embedded (%d chunks); results discarded; LanceDB not written",
                done,
                len(all_embeddings),
            )
        raise

    return all_embeddings


def build_search_index(
    output_dir: Path,
    *,
    channel_filter: str | None = None,
    force: bool = False,
    config: dict[str, Any] | None = None,
) -> int:
    """Build or rebuild the LanceDB vector index from transcripts + concepts.

    Returns the number of chunks indexed.
    """
    lancedb = require_lancedb()
    voyageai = require_voyageai()

    vo_key = os.environ.get("VOYAGE_API_KEY")
    if not vo_key:
        log.error("VOYAGE_API_KEY not set. Sign up free at https://dash.voyageai.com/")
        sys.exit(1)

    db_path = resolve_vector_db_dir(config or {}, output_dir)
    ok, reason = probe_atomic_writes(db_path)
    if not ok:
        log.error("Cannot use vector_db_dir=%s", db_path)
        log.error("%s", reason)
        sys.exit(1)

    vo = voyageai.Client()
    db = lancedb.connect(str(db_path))

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
    since_iso: str | None = None,
    limit: int = 10,
    config: dict[str, Any] | None = None,
    expand: bool = True,
    return_diagnostics: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    """Search the LanceDB index with hybrid BM25 + vector + RRF fusion.

    Returns ranked chunks deduplicated by video. `since_iso` (YYYY-MM-DD) filters
    chunks whose `published` column is >= the given date, applied pre-rank so
    recency scope does not get crowded out by older, higher-relevance hits.

    When `expand=True` (default), the query is preprocessed through
    `expand_query_via_taxonomy()` and the expanded string is sent to both the
    BM25 FTS call (`.text()`) and the Voyage embedding call. Original query
    stays at the prefix so BM25 TF/IDF still favors the user's terms.

    When `return_diagnostics=True`, returns `(hits, expansion_record)` where
    `expansion_record` is `{"expand_enabled": bool, "original_query": str,
    "expanded_query": str, "matches": [...]}`. Eval harness uses this to
    write per-query JSONL logs independent of logging configuration.
    """
    lancedb = require_lancedb()
    voyageai = require_voyageai()

    vo_key = os.environ.get("VOYAGE_API_KEY")
    if not vo_key:
        log.error("VOYAGE_API_KEY not set. Sign up free at https://dash.voyageai.com/")
        sys.exit(1)

    # --- Stage 1 query expansion (ADR-0017) --------------------------------
    effective_query = query
    expansion_matches: list[dict] = []
    if expand:
        taxonomy = load_taxonomy(output_dir)
        effective_query, expansion_matches = expand_query_via_taxonomy(query, taxonomy)
        if expansion_matches:
            added_flat = [a for m in expansion_matches for a in m["added"]]
            log.info(
                "query_expansion input=%r matched=%d added=%s",
                query,
                len(expansion_matches),
                added_flat,
            )

    db_path = resolve_vector_db_dir(config or {}, output_dir)
    # No probe here: search is read-only and does not exercise LanceDB's commit path.
    # The probe lives in build_search_index where it prevents wasted Voyage embeddings.
    db = lancedb.connect(str(db_path))

    diagnostics = {
        "expand_enabled": expand,
        "original_query": query,
        "expanded_query": effective_query,
        "matches": expansion_matches,
    }

    if LANCEDB_TABLE not in db.list_tables().tables:
        log.error("Search index not found. Run: video_intel.py index")
        if return_diagnostics:
            return [], diagnostics
        return []

    table = db.open_table(LANCEDB_TABLE)

    # Embed query with lite model (asymmetric retrieval)
    vo = voyageai.Client()
    query_embedding = vo.embed([effective_query], model=VOYAGE_QUERY_MODEL, input_type="query").embeddings[0]

    # Hybrid search: BM25 (FTS on title+text) + vector, merged by RRF (K=60 default)
    fetch_count = max(50, limit * 5)
    search_builder = (
        table.search(query_type="hybrid", fts_columns=["title", "text"])
        .vector(query_embedding)
        .text(effective_query)
        .limit(fetch_count)
    )
    where_clauses = []
    if channel_filter:
        where_clauses.append(f"channel = '{channel_filter}'")
    if since_iso:
        where_clauses.append(f"published >= '{since_iso}'")
    if where_clauses:
        search_builder = search_builder.where(" AND ".join(where_clauses))

    results = search_builder.to_pandas()

    # Convert rows to dicts — hybrid returns _relevance_score (higher = better)
    raw_hits = []
    for _, row in results.iterrows():
        raw_hits.append(
            {
                "text": row["text"],
                "timestamp": row["timestamp"],
                "timestamp_seconds": int(row.get("timestamp_seconds", 0)),
                "video_id": row.get("video_id", ""),
                "channel": row.get("channel", ""),
                "title": row.get("title", ""),
                "published": row.get("published", ""),
                "source_file": row.get("source_file", ""),
                "concept_ids": row.get("concept_ids", "[]"),
                "relevance": float(row.get("_relevance_score", 0.0)),
            }
        )

    hits = _dedup_by_video(raw_hits, limit)
    if return_diagnostics:
        return hits, diagnostics
    return hits


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
    count = build_search_index(output_dir, channel_filter=args.channel, force=args.force, config=config)
    elapsed = time.time() - t0

    if count == 0:
        print("No transcripts found to index.")
    else:
        mins, secs = divmod(int(elapsed), 60)
        print(f"Indexed {count} chunks in {mins}m {secs:02d}s.")
        print(f"  Index: {resolve_vector_db_dir(config, output_dir)}")
        print("  Run 'search --vector \"query\"' to search.")


def search_corpus(
    output_dir: Path,
    query: str,
    *,
    channel_filter: str | None = None,
    since_iso: str | None = None,
    limit: int = 20,
) -> dict:
    """Search taxonomy + concepts for matching videos. Returns structured results.

    `since_iso` (YYYY-MM-DD) post-filters matching videos to those with
    `published >= since_iso`. Concept data lives in JSON files, so this is
    a post-rank filter (unlike hybrid_search, which pushes the filter to LanceDB).
    """
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

    # Optional date-window filter (applied post-rank; concepts live in JSON, not a query store)
    if since_iso:
        matching_videos = [v for v in matching_videos if v.get("published", "") >= since_iso]

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

    since_raw = getattr(args, "since", None)
    since_iso = parse_since(since_raw).date().isoformat() if since_raw else None

    # Hybrid search mode (BM25 + vector + RRF)
    if getattr(args, "vector", False):
        hits = hybrid_search(
            output_dir,
            args.query,
            channel_filter=args.channel,
            since_iso=since_iso,
            limit=args.limit,
            config=config,
            expand=not getattr(args, "no_expand", False),
        )
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
            vid_url = f"https://www.youtube.com/watch?v={hit['video_id']}" if hit.get("video_id") else ""
            ts_secs = hit.get("timestamp_seconds", 0)
            if vid_url and ts_secs:
                vid_url += f"&t={ts_secs}"
            print(f"  [{i}] [{hit['channel']}] {hit['published']}  {hit['title']}")
            print(f"      Timestamp: [{hit['timestamp']}]  Relevance: {hit['relevance']:.4f}")
            if vid_url:
                print(f"      Video: {vid_url}")
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
    results = search_corpus(
        output_dir,
        args.query,
        channel_filter=args.channel,
        since_iso=since_iso,
        limit=args.limit,
    )

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
# dedupe subcommand - cleans up title-rotation duplicates
# ---------------------------------------------------------------------------


# Modes we track in meta.json, mapped to the artifact glob patterns that
# constitute "this mode is complete under this prefix". Used when canonical
# lacks a mode the loser has: we move the loser's artifacts over before
# deleting the rest of the loser's siblings.
_MODE_ARTIFACT_PATTERNS: dict[str, tuple[str, ...]] = {
    "scan": ("{prefix}.mindmap.md", "{prefix}.mindmap.*.md"),
    "transcript": ("{prefix}.transcript.md", "{prefix}.transcript.raw.txt"),
    "concepts": ("{prefix}.concepts.json",),
}


def _pick_canonical(metas: list[tuple[Path, dict]]) -> tuple[Path, dict]:
    """Pick canonical by (latest processed, most modes_completed, prefix).

    Reverse-sort so index 0 is canonical. Stable tie-break on alphabetical
    prefix keeps the choice deterministic when timestamps are identical.
    """

    def sort_key(item: tuple[Path, dict]) -> tuple[str, int, str]:
        path, data = item
        return (
            data.get("processed", ""),
            len(data.get("modes_completed", [])),
            path.name,
        )

    ranked = sorted(metas, key=sort_key, reverse=True)
    return ranked[0]


def _merge_alt_titles(
    canonical_data: dict,
    metas: list[tuple[Path, dict]],
    canonical_path: Path,
) -> list[str]:
    """Return the merged alt_titles list, ordered by ascending processed time.

    Starts with canonical's existing alt_titles (may be empty), then appends
    each loser's title in chronological order, skipping the canonical title
    and any duplicates.
    """
    canonical_title = canonical_data.get("title")
    existing = list(canonical_data.get("alt_titles", []))
    loser_titles_in_order = [
        data.get("title")
        for path, data in sorted(metas, key=lambda m: m[1].get("processed", ""))
        if path != canonical_path and data.get("title")
    ]

    merged: list[str] = []
    seen = {canonical_title}
    for title in existing + loser_titles_in_order:
        if title and title not in seen:
            merged.append(title)
            seen.add(title)
    return merged


def _move_missing_mode_artifacts(
    channel_dir: Path,
    canonical_prefix: str,
    loser_prefix: str,
    missing_modes: set[str],
) -> None:
    """Move artifacts for each missing mode from loser_prefix to canonical_prefix.

    Skips if the destination already exists (shouldn't happen when canonical
    lacks the mode, but the guard avoids overwriting unrelated content).
    """
    for mode in missing_modes:
        for pattern in _MODE_ARTIFACT_PATTERNS.get(mode, ()):
            for src in channel_dir.glob(pattern.format(prefix=loser_prefix)):
                suffix = src.name[len(loser_prefix) :]
                dst = channel_dir / f"{canonical_prefix}{suffix}"
                if not dst.exists():
                    src.rename(dst)


def _apply_dedupe_group(
    channel_dir: Path,
    metas: list[tuple[Path, dict]],
) -> None:
    """Apply the dedup to one video_id group: merge alts, move missing mode
    artifacts, write canonical meta, delete all loser siblings."""
    canonical_path, canonical_data = _pick_canonical(metas)
    canonical_prefix = canonical_path.name.removesuffix(".meta.json")

    # Union modes_completed and move any artifact only losers have.
    canonical_modes = set(canonical_data.get("modes_completed", []))
    for loser_path, loser_data in metas:
        if loser_path == canonical_path:
            continue
        loser_prefix = loser_path.name.removesuffix(".meta.json")
        loser_modes = set(loser_data.get("modes_completed", []))
        missing = loser_modes - canonical_modes
        if missing:
            _move_missing_mode_artifacts(channel_dir, canonical_prefix, loser_prefix, missing)
            canonical_modes |= missing

    canonical_data["modes_completed"] = sorted(canonical_modes)
    merged_alts = _merge_alt_titles(canonical_data, metas, canonical_path)
    if merged_alts:
        canonical_data["alt_titles"] = merged_alts

    canonical_path.write_text(json.dumps(canonical_data, indent=2), encoding="utf-8")

    # Sweep every loser prefix's remaining siblings.
    for loser_path, _ in metas:
        if loser_path == canonical_path:
            continue
        loser_prefix = loser_path.name.removesuffix(".meta.json")
        for sibling in channel_dir.glob(f"{loser_prefix}.*"):
            sibling.unlink()

    _invalidate_video_id_cache(channel_dir)


def _scan_duplicate_groups(channel_dir: Path) -> dict[str, list[tuple[Path, dict]]]:
    """Return {video_id: [(meta_path, meta_data), ...]} for groups with >1 entry."""
    groups: dict[str, list[tuple[Path, dict]]] = {}
    if not channel_dir.exists():
        return {}
    for meta_path in channel_dir.glob("*.meta.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        vid = data.get("video_id")
        if vid:
            groups.setdefault(vid, []).append((meta_path, data))
    return {vid: metas for vid, metas in groups.items() if len(metas) > 1}


def cmd_dedupe(args, config):
    """Find and clean up video_id duplicates across channels.

    Dry-run by default; pass --apply to mutate disk. Does NOT auto-rebuild
    taxonomy or the LanceDB index - surface the user-facing next-step
    reminder instead so the operator can decide when to pay that cost.
    """
    require_channels_config(config)
    output_dir = resolve_output_dir(config)
    channel_filter = getattr(args, "channel", None)
    apply = bool(getattr(args, "apply", False))

    channel_names = [channel_filter] if channel_filter else [c["name"] for c in config.get("channels", [])]

    total_groups = 0
    total_excess = 0

    for ch_name in channel_names:
        channel_dir = output_dir / ch_name
        dup_groups = _scan_duplicate_groups(channel_dir)
        if not dup_groups:
            continue

        log.info("[%s] %d duplicate group(s)", ch_name, len(dup_groups))
        total_groups += len(dup_groups)

        for vid, metas in sorted(dup_groups.items()):
            canonical_path, canonical_data = _pick_canonical(metas)
            log.info("  video_id=%s", vid)
            log.info(
                "    canonical: %s  '%s'  processed=%s",
                canonical_path.name,
                canonical_data.get("title", ""),
                canonical_data.get("processed", "")[:19],
            )
            for loser_path, loser_data in metas:
                if loser_path == canonical_path:
                    continue
                log.info(
                    "    loser:     %s  '%s'  processed=%s",
                    loser_path.name,
                    loser_data.get("title", ""),
                    loser_data.get("processed", "")[:19],
                )
                total_excess += 1

            if apply:
                _apply_dedupe_group(channel_dir, metas)

    verb = "cleaned up" if apply else "would clean up"
    if total_groups == 0:
        log.info("No duplicates found.")
        return

    log.info(
        "Summary: %d group(s), %d excess file(s) %s.",
        total_groups,
        total_excess,
        verb,
    )
    if not apply:
        log.info("Re-run with --apply to execute.")
    else:
        log.info("Next steps: run `taxonomy-build` and `index --force` to rebuild derived artifacts.")


# ---------------------------------------------------------------------------
# prune-shorts subcommand
# ---------------------------------------------------------------------------
# Cleans up YouTube Shorts that polluted the corpus before the scan-time
# skip_shorts filter existed. Dry-run by default; --apply mutates disk.
# Mirrors the dedupe contract — manual taxonomy-build + index --force after
# --apply, NOT auto-rebuilt (predictable blast radius).

# Explicit suffix allowlist for deletion. Critical: NOT the whole-prefix glob
# that _apply_dedupe_group uses, because translate_video.py produces siblings
# (.en.srt, .translate-bcs.txt) that share the prefix and must survive a
# Shorts prune. translate-bcs is operationally separate from curate.
PRUNE_SHORTS_DELETION_PATTERNS = (
    "{prefix}.mindmap.md",
    "{prefix}.mindmap.*.md",  # knowledge / light / heavy variants
    "{prefix}.mindmap.raw.txt",  # issue #119 confabulation-guard forensic sidecar
    "{prefix}.transcript.md",
    "{prefix}.transcript.raw.txt",
    "{prefix}.transcript.raw.*.txt",
    "{prefix}.concepts.json",
    "{prefix}.meta.json",
    "{prefix}.meta.corrupt.json",  # issue #124 quarantine copy of an unusable meta
)


def _collect_short_candidates(channel_dir: Path, youtube) -> list[tuple[Path, dict, int]]:
    """Return [(meta_path, meta_data, duration_seconds), ...] for Shorts.

    Walks meta.json files. Metas without video_id are skipped (cannot
    classify safely). For metas with duration_seconds cached (post-Unit-3
    scans), uses that. For metas missing the field (legacy), batches a
    videos.list lookup. is_short() makes the final classification per its
    fail-safe-to-long-form contract.
    """
    if not channel_dir.exists():
        return []

    metas: list[tuple[Path, dict]] = []
    needs_lookup: list[str] = []
    for meta_path in sorted(channel_dir.glob("*.meta.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        vid = data.get("video_id")
        if not vid:
            continue
        metas.append((meta_path, data))
        if data.get("duration_seconds") is None:
            needs_lookup.append(vid)

    fetched_durations: dict[str, str | None] = {}
    if needs_lookup:
        fetched_durations = enrich_with_durations(youtube, needs_lookup)

    candidates: list[tuple[Path, dict, int]] = []
    for meta_path, data in metas:
        vid = data["video_id"]
        cached_seconds = data.get("duration_seconds")
        if cached_seconds is not None:
            duration_iso: str | None = f"PT{int(cached_seconds)}S"
            duration_for_log = int(cached_seconds)
        else:
            duration_iso = fetched_durations.get(vid)
            parsed = _parse_iso8601_duration(duration_iso)
            duration_for_log = parsed if parsed is not None else 0
        if is_short(vid, duration_iso):
            candidates.append((meta_path, data, duration_for_log))
    return candidates


def _apply_prune_shorts(
    channel_dir: Path,
    candidates: list[tuple[Path, dict, int]],
) -> int:
    """Delete artifacts for each candidate Short via PRUNE_SHORTS_DELETION_PATTERNS.

    Sidecar files outside the allowlist (.en.srt, .translate-bcs.txt, etc.)
    are preserved. Returns the total number of files deleted. Calls
    _invalidate_video_id_cache after the loop so subsequent is_processed()
    calls re-glob.
    """
    deleted = 0
    for meta_path, _data, _seconds in candidates:
        prefix = meta_path.name.removesuffix(".meta.json")
        for pattern in PRUNE_SHORTS_DELETION_PATTERNS:
            for path in channel_dir.glob(pattern.format(prefix=prefix)):
                path.unlink()
                deleted += 1
    if candidates:
        _invalidate_video_id_cache(channel_dir)
    return deleted


def _count_artifacts_for_prefix(channel_dir: Path, prefix: str) -> int:
    """Count files matching PRUNE_SHORTS_DELETION_PATTERNS for one prefix."""
    return sum(1 for pattern in PRUNE_SHORTS_DELETION_PATTERNS for _ in channel_dir.glob(pattern.format(prefix=prefix)))


def cmd_prune_shorts(args, config):
    """Find and delete YouTube Shorts artifacts.

    Dry-run by default; pass --apply to mutate disk. Mirrors dedupe — does
    NOT auto-rebuild taxonomy or the LanceDB index. The user runs
    `taxonomy-build` and `index --force` afterward.
    """
    require_channels_config(config)
    output_dir = resolve_output_dir(config)
    channel_filter = getattr(args, "channel", None)
    apply = bool(getattr(args, "apply", False))

    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if not yt_key:
        log.error("YOUTUBE_API_KEY not set. Required to fetch durations for legacy metas.")
        sys.exit(1)
    yt_build = require_youtube()
    youtube = yt_build("youtube", "v3", developerKey=yt_key)

    channel_names = [channel_filter] if channel_filter else [c["name"] for c in config.get("channels", [])]

    total_shorts = 0
    total_artifacts = 0

    for ch_name in channel_names:
        channel_dir = output_dir / ch_name
        candidates = _collect_short_candidates(channel_dir, youtube)
        if not candidates:
            continue

        log.info("[%s] %d Short(s) detected", ch_name, len(candidates))
        ch_artifact_count = 0
        for meta_path, data, seconds in candidates:
            prefix = meta_path.name.removesuffix(".meta.json")
            url = data.get("video_url") or f"https://youtube.com/watch?v={data.get('video_id', '')}"
            artifact_count = _count_artifacts_for_prefix(channel_dir, prefix)
            log.info(
                "  %-60s | %d:%02d | %s | %d artifacts",
                (data.get("title") or "")[:60],
                seconds // 60,
                seconds % 60,
                url,
                artifact_count,
            )
            ch_artifact_count += artifact_count
        log.info("  Channel summary: %d Shorts, %d artifacts", len(candidates), ch_artifact_count)
        total_shorts += len(candidates)
        total_artifacts += ch_artifact_count

        if apply:
            _apply_prune_shorts(channel_dir, candidates)

    if total_shorts == 0:
        log.info("No Shorts found.")
        return

    verb = "deleted" if apply else "would delete"
    log.info("Summary: %d Shorts, %d artifacts %s.", total_shorts, total_artifacts, verb)
    if not apply:
        log.info("Re-run with --apply to execute.")
    else:
        log.info("Next steps: run `taxonomy-build` and `index --force` to rebuild derived artifacts.")


def _format_nugget_excerpt(hit: dict, index: int) -> str:
    """Format one hybrid-search hit as an attributed excerpt for the nugget prompt."""
    vid_url = f"https://www.youtube.com/watch?v={hit['video_id']}" if hit.get("video_id") else ""
    ts_secs = hit.get("timestamp_seconds", 0)
    if vid_url and ts_secs:
        vid_url += f"&t={ts_secs}"
    header = (
        f"### Excerpt {index}\n"
        f"- **Channel:** {hit['channel']}\n"
        f"- **Published:** {hit['published']}\n"
        f"- **Title:** {hit['title']}\n"
        f"- **Timestamp:** [{hit['timestamp']}]\n"
    )
    if vid_url:
        header += f"- **URL:** {vid_url}\n"
    body = hit["text"].strip()
    return f"{header}\n{body}\n"


def build_nugget_prompt(template: str, query: str, hits: list[dict]) -> str:
    """Fill the nugget-brief template with the query and formatted excerpts.

    Pure function — no I/O, no Gemini calls. Separated from cmd_nugget so
    the substitution logic is unit-testable.
    """
    excerpts_text = "\n".join(_format_nugget_excerpt(h, i) for i, h in enumerate(hits, 1))
    return (
        template.replace("{{QUERY}}", query)
        .replace("{{NUM_CHUNKS}}", str(len(hits)))
        .replace("{{EXCERPTS}}", excerpts_text)
    )


def cmd_nugget(args, config):
    """Synthesize a consultant-grade multi-creator nugget brief for a query."""
    output_dir = resolve_output_dir(config)
    since_raw = getattr(args, "since", None)
    since_iso = parse_since(since_raw).date().isoformat() if since_raw else None

    log.info("Retrieving top-%d excerpts for: %s", args.limit, args.query)
    hits = hybrid_search(
        output_dir,
        args.query,
        channel_filter=args.channel,
        since_iso=since_iso,
        limit=args.limit,
        config=config,
        expand=not getattr(args, "no_expand", False),
    )
    if not hits:
        print(f'No results for "{args.query}". Is the index built? Run: video_intel.py index')
        return

    min_rel = getattr(args, "min_relevance", 0.0)
    strong = [h for h in hits if h["relevance"] >= min_rel]
    if not strong:
        print(f'No strong matches for "{args.query}" (best relevance: {hits[0]["relevance"]:.4f}).')
        return

    channels_seen = sorted({h["channel"] for h in strong})
    log.info("Retrieved %d excerpts across %d channels: %s", len(strong), len(channels_seen), ", ".join(channels_seen))

    prompt_template = load_prompt("nugget-brief")
    filled_prompt = build_nugget_prompt(prompt_template, args.query, strong)

    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not gemini_key:
        log.error("Missing GEMINI_API_KEY or GOOGLE_API_KEY environment variable.")
        sys.exit(1)
    client = create_client(gemini_key)
    require_gemini()
    from google.genai import types as genai_types

    model = getattr(args, "model", None) or config.get("model", DEFAULT_MODEL)
    log.info("Synthesizing with %s...", model)

    # Use a direct text-in / text-out Gemini call (markdown output, not JSON).
    config_kwargs = {
        "temperature": 0.3,
        "safety_settings": build_permissive_safety_settings(genai_types),
    }
    contents = genai_types.Content(parts=[genai_types.Part(text=filled_prompt)])
    max_retries = 3
    transport_attempts = 0
    response_text = None
    for attempt in range(max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(**config_kwargs),
            )
            response_text = response.text
            break
        except Exception as e:
            retry = get_retry_delay(
                e,
                attempt,
                max_retries_rate=max_retries,
                max_retries_server=max_retries,
                max_retries_transport=MAX_RETRIES_TRANSPORT,
                transport_attempt=transport_attempts,
            )
            if retry is None:
                raise
            if is_transient_transport_error(e):
                transport_attempts += 1
            kind, wait, _ = retry
            log.warning("%s — retry %d/%d in %.0fs...", kind, attempt + 1, max_retries, wait)
            time.sleep(wait)

    if not response_text:
        log.error("No response from Gemini after %d retries.", max_retries)
        sys.exit(1)

    if getattr(args, "output", None):
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(response_text, encoding="utf-8")
        print(f"Nugget brief saved: {out_path}")
    else:
        print(response_text)


def cmd_repair_metas(args, config):
    """Backfill identity into identity-less transcript metas (issue #66).

    Finds ``.meta.json`` files missing ``video_id`` and reconstructs identity
    from the sibling ``.transcript.md`` header. Dry-run by default; ``--apply``
    writes. Only fills MISSING fields - never overwrites an existing value. This
    heals metas written before the transcript writer stamped full identity, which
    otherwise defeat ``_load_video_id_index`` and get re-transcribed every scan.
    """
    output_dir = resolve_output_dir(config)
    only = getattr(args, "channel", None)
    repaired = 0
    unrepairable = 0
    applied = 0
    for meta_path in sorted(output_dir.glob("*/*.meta.json")):
        channel = meta_path.parent.name
        if only and channel != only:
            continue
        # encoding="utf-8" (not the cp1252 Windows default): a meta with non-ASCII
        # content (Cyrillic/BCS creators) would otherwise raise UnicodeDecodeError -
        # which is NOT an OSError - and abort the whole walk (ce-correctness review).
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            continue
        if meta.get("video_id"):
            continue  # already has identity
        prefix = meta_path.name[: -len(".meta.json")]
        transcript_path = meta_path.parent / f"{prefix}.transcript.md"
        identity = _identity_from_transcript_header(transcript_path) if transcript_path.exists() else None
        if not identity:
            unrepairable += 1
            log.warning("  [%s] %s: no usable .transcript.md header to backfill from", channel, prefix)
            continue
        missing = {k: v for k, v in identity.items() if v and not meta.get(k)}
        log.info("  [%s] %s: backfill %s", channel, prefix, ", ".join(sorted(missing)) or "(nothing missing)")
        if args.apply and missing:
            meta.update(missing)
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            applied += 1
        repaired += 1
    if applied:
        # A backfilled meta now carries video_id; drop the process-global index
        # cache so a later step in this process sees it (mirrors dedupe).
        _invalidate_video_id_cache()
    verb = "Repaired" if args.apply else "Would repair"
    log.info("%s %d identity-less meta(s); %d unrepairable (no usable header).", verb, repaired, unrepairable)
    if not args.apply and repaired:
        log.info("Re-run with --apply to write. After applying, run 'index --force' if idempotency was affected.")


# ---------------------------------------------------------------------------
# briefings subcommand - catch-up briefings for unseen videos (issue #80)
# ---------------------------------------------------------------------------

BRIEFINGS_DIR_NAME = "_briefings"
PROFILE_FILENAME = "profile.yaml"
AUDIENCE_FILENAME = "audience.md"
DEFAULT_LIMIT = 30
PROFILE_TOP_CONCEPTS = 40

# Headline digest (issue #113): peripheral vision over unfollowed channels.
HEADLINES_DIR_NAME = "_headlines"
HEADLINES_SEEN_FILENAME = "seen.json"
HEADLINES_MAX_ITEMS = 10  # global cap per run
HEADLINES_MAX_ZERO_SCORE = 5  # a few recent "Other headlines" (mirrors briefings zero-score rule)
HEADLINES_SEEN_MAX = 500  # bound seen.json so it never grows without limit
HEADLINES_LOOKBACK_DAYS = 14  # "new" window; the seen-set is the real re-surface guard
HEADLINE_DOMAIN_MATCH_WEIGHT = 0.5  # weak signal, mirrors rank_unseen's domain bonus
# A bare YouTube channel id: literal "UC" + 22 url-safe base64 chars.
_UC_CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
_YOUTUBE_HOSTS = frozenset({"youtube.com", "m.youtube.com", "youtu.be", "www.youtube.com"})

_MINDMAP_TIMESTAMP_RE = re.compile(r"\((\d{1,3}):(\d{2})(?::(\d{2}))?\)")


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a markdown doc into (front_matter_dict, body).

    Front matter is a leading YAML block delimited by '---' lines. Returns
    ({}, text) when no well-formed front matter is present and never raises on
    malformed YAML - a hand-edited briefing must not crash the catch-up scan.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    closing = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if closing is None:
        return {}, text
    try:
        data = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, "\n".join(lines[closing + 1 :])


def load_seen_video_ids(briefings_dir: Path) -> set[str]:
    """Union of `video_ids` across every _briefings/**/*.md front matter.

    This is the strict set-difference basis for "unseen": a video that has
    appeared in ANY briefing is considered surfaced. Missing dir -> empty set.
    Recurses into subfolders (e.g. _briefings/sales/) so topic-based
    organization of the briefings dir doesn't silently un-see videos.
    """
    seen: set[str] = set()
    if not briefings_dir.is_dir():
        return seen
    for md in sorted(briefings_dir.rglob("*.md")):
        try:
            front_matter, _ = parse_front_matter(md.read_text(encoding="utf-8"))
        except OSError:
            continue
        ids = front_matter.get("video_ids") or []
        if isinstance(ids, list):
            seen.update(str(v) for v in ids if v)
    return seen


def _artifact_count(record: dict) -> int:
    """How many of a video's optional artifacts (mindmap, concepts) are present."""
    return sum(1 for key in ("mindmap_path", "concepts_path") if record.get(key))


def collect_corpus_videos(output_dir: Path) -> list[dict]:
    """One record per unique video_id that has a meta.json, across all channel dirs.

    Skips dot-dirs and underscore-dirs (e.g. _briefings) so human-note folders
    are never mistaken for channels. Each record carries the catch-up fields
    plus paths to the mindmap/concepts siblings (None when absent). When a
    video_id has duplicate metas (title-rotation, pre-dedupe), the most complete
    record wins so a video is never surfaced twice in one briefing.
    """
    by_id: dict[str, dict] = {}
    if not output_dir.is_dir():
        return []
    channel_dirs = [
        d for d in output_dir.iterdir() if d.is_dir() and not d.name.startswith(".") and not d.name.startswith("_")
    ]
    for channel_dir in sorted(channel_dirs):
        for meta_path in sorted(channel_dir.glob("*.meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            video_id = meta.get("video_id")
            if not video_id:
                continue
            prefix = meta_path.name[: -len(".meta.json")]
            mindmap_path = channel_dir / f"{prefix}.mindmap.md"
            concepts_path = channel_dir / f"{prefix}.concepts.json"
            record = {
                "video_id": video_id,
                "title": meta.get("title", prefix),
                "published": meta.get("published", ""),
                "channel": meta.get("channel", channel_dir.name),
                "url": meta.get("video_url") or f"https://www.youtube.com/watch?v={video_id}",
                "mindmap_path": mindmap_path if mindmap_path.exists() else None,
                "concepts_path": concepts_path if concepts_path.exists() else None,
            }
            # Dedupe by video_id - title-rotation can leave >1 meta per id, and a
            # set-difference against prior briefings won't catch a same-corpus dup.
            # Keep the most complete record so ranking has concepts to work with.
            existing = by_id.get(video_id)
            if existing is None or _artifact_count(record) > _artifact_count(existing):
                by_id[video_id] = record
    return list(by_id.values())


def _parse_iso_date(value: str):
    """Parse a 'YYYY-MM-DD' (or longer ISO) string to a date, else None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def compute_catchup_window(*, since=None, until=None, today=None):
    """Return (lower, upper) date bounds for a catch-up scan, all UTC dates.

    lower = `since` if given, else `date.min` - i.e. **unbounded by default**
    (issue #88). The permanent set-difference on `video_id` (see
    `load_seen_video_ids` / `select_unseen`) is the real "never re-surface"
    guard; a recency floor only hid old-but-never-briefed videos, which is the
    opposite of what a catch-up should do. Pass `--since` to *narrow* back to a
    floor when you want one. upper = `until` if given, else today.
    """
    if today is None:
        today = datetime.now(UTC).date()
    lower = since if since is not None else date.min
    upper = until if until is not None else today
    return lower, upper


def select_unseen(videos: list[dict], seen_ids: set[str], *, lower, upper) -> list[dict]:
    """Videos whose id is in no briefing and whose published date is in [lower, upper].

    Set-difference on video_id is the primary guard (never window-based), so a
    video surfaced once is never re-surfaced. Videos with an unparseable
    `published` are dropped - with the default window now unbounded (issue #88)
    they would otherwise have no year to sort or group under, and every real
    YouTube-sourced corpus video carries a `published` date anyway. All
    comparisons are on UTC dates; `lower` is `date.min` on an unbounded run.
    """
    out: list[dict] = []
    for video in videos:
        if video["video_id"] in seen_ids:
            continue
        published = _parse_iso_date(video.get("published", ""))
        if published is None or published < lower or published > upper:
            continue
        out.append(video)
    return out


def _infer_profile(output_dir: Path, config: dict | None = None, *, today=None) -> dict:
    """Build a starter interest profile from signals the user already produced.

    Single-tier cold-start (issue #80): the scanned channel list plus the
    corpus's most-recurring taxonomy concepts (video_count as weight). No
    questions, no host-agent introspection.
    """
    if today is None:
        today = datetime.now(UTC).date()
    channels = [c.get("name") for c in (config or {}).get("channels", []) if c.get("name")]
    interest_concepts: dict[str, int] = {}
    domains: dict[str, int] = {}
    tax_path = output_dir / "taxonomy.json"
    if tax_path.exists():
        try:
            tax = json.loads(tax_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            tax = {}
        concepts = tax.get("concepts", {}) if isinstance(tax, dict) else {}
        ranked = sorted(
            concepts.items(),
            key=lambda kv: (int(kv[1].get("video_count", 0)), kv[0]),
            reverse=True,
        )[:PROFILE_TOP_CONCEPTS]
        for cid, meta in ranked:
            weight = int(meta.get("video_count", 1)) or 1
            interest_concepts[cid] = weight
            dom = meta.get("domain") or cid.split(".")[0]
            domains[dom] = domains.get(dom, 0) + weight
    return {
        "schema_version": 1,
        "id": f"inferred-{today.isoformat()}",
        "source": "inferred",
        "generated": today.isoformat(),
        "note": (
            "Auto-inferred from your scanned channels + taxonomy. Hand-edit freely - "
            "once this file exists it is never overwritten."
        ),
        "channels": channels,
        "interest_domains": sorted(domains, key=lambda d: domains[d], reverse=True),
        "interest_concepts": interest_concepts,
    }


def _load_usable_profile(briefings_dir: Path) -> dict | None:
    """Return a hand-tuned profile.yaml as a dict, or None if absent/empty/broken.

    "Usable" = the file exists AND parses to a non-empty dict. An empty or
    unreadable profile.yaml counts as no profile (so a fresh inference runs and
    the cold-start warning still fires). Shared by `load_interest_model`, the
    cold-start check in `cmd_briefings`, and `profile show` / `profile init` so
    every surface agrees on what "tuned" means.
    """
    profile_path = briefings_dir / PROFILE_FILENAME
    if not profile_path.exists():
        return None
    try:
        data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError):
        return None
    return data if isinstance(data, dict) and data else None


def _coerce_profile_interests(profile: dict) -> tuple[dict[str, float], set[str]]:
    """Tolerantly parse a (possibly hand-edited) profile into (interest_concepts, domains).

    A hand-edited profile.yaml may carry `interest_concepts` as a list/scalar
    (membership+indexing would crash) with string weights, and `interest_domains`
    as a bare string ("ai" would otherwise become a set of single chars). Coerce
    to a `{concept_id: float}` map (dropping non-numeric weights) and a `set[str]`
    of domains. Shared by `rank_unseen` and `rank_headlines` so the tolerant
    parsing stays in one place (both must accept the same malformed inputs).
    """
    raw_interest = profile.get("interest_concepts")
    interest: dict[str, float] = {}
    if isinstance(raw_interest, dict):
        for cid, weight in raw_interest.items():
            try:
                value = float(weight)
            except (TypeError, ValueError):
                continue
            # NaN/inf poison every comparison downstream (a NaN-scored item sorts
            # into the top slot and then matches neither the >0 nor the ==0
            # bucket, so it vanishes from the digest entirely). Drop them here,
            # at the single coercion point, rather than defending every consumer.
            if not math.isfinite(value):
                log.warning("Ignoring non-finite weight for interest concept %r in profile.yaml.", cid)
                continue
            interest[cid] = value
    raw_domains = profile.get("interest_domains") or []
    if isinstance(raw_domains, str):
        raw_domains = [raw_domains]
    domains = {d for d in raw_domains if isinstance(d, str)} if isinstance(raw_domains, list | set | tuple) else set()
    return interest, domains


# ---------------------------------------------------------------------------
# The compiled interest model - ONE ranking surface for BOTH consumers (#115)
# ---------------------------------------------------------------------------


def _norm_phrase(text: str) -> str:
    """Lowercase and collapse to space-separated alphanumeric tokens for matching."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _humanize_concept_id(concept_id: str) -> str:
    """`ai-agents.mcp-servers` -> `mcp servers` (drop domain prefix, split separators)."""
    tail = concept_id.split(".")[-1] if "." in concept_id else concept_id
    return _norm_phrase(tail)


@dataclass(frozen=True)
class InterestConcept:
    """One weighted interest, with the phrases that recognize it in free text.

    `phrases` are space-padded, normalized forms (label + taxonomy aliases +
    the humanized concept id) so a substring test on a padded title matches
    whole words only.
    """

    concept_id: str
    weight: float
    label: str
    phrases: tuple[str, ...]


@dataclass(frozen=True)
class InterestModel:
    """The compiled reading of a user's profile - the single ranking input.

    `weights` is the concept-evidence view (`rank_unseen`, which reads
    `concepts.json`); `concepts`/`domain_terms` are the text-evidence view
    (`rank_headlines`, which only has a title). Both are derived from the SAME
    profile in one place, so one profile edit moves both surfaces wherever each
    has matching evidence.

    `weights` is exposed read-only: mutating it after compilation would change
    `rank_unseen` while `rank_headlines` kept the weight already copied into
    each `InterestConcept` - drift inside the one model that exists to prevent it.
    """

    weights: Mapping[str, float]
    domains: frozenset[str]
    concepts: tuple[InterestConcept, ...]
    domain_terms: tuple[tuple[str, str], ...]
    profile_id: str
    source: str  # "persisted" (on disk) | "inferred" (ephemeral)
    profile_path: Path | None
    audience_path: Path | None
    raw: dict
    # False when taxonomy.json exists but could not be read. Ranking still works
    # (humanized concept ids), but a surface that spends irreversible state on a
    # ranked render - the headline digest's seen-set - must not do so blind.
    taxonomy_ok: bool = True


def compile_interest_model(
    profile: dict,
    taxonomy: dict | None = None,
    *,
    source: str = "inferred",
    profile_path: Path | None = None,
    audience_path: Path | None = None,
    taxonomy_ok: bool = True,
) -> InterestModel:
    """Compile a (possibly hand-edited) profile + taxonomy into one interest model.

    Tolerant by construction: `_coerce_profile_interests` absorbs the shapes a
    hand-edit produces (list/scalar `interest_concepts`, string weights, a bare
    string domain). Taxonomy `preferred_label`/`aliases` widen a concept's
    recognizable phrases; with no taxonomy each concept still contributes its
    humanized id, so ranking degrades gracefully rather than failing.
    """
    interest, domains = _coerce_profile_interests(profile)
    tax_concepts = taxonomy.get("concepts", {}) if isinstance(taxonomy, dict) else {}
    if not isinstance(tax_concepts, dict):
        tax_concepts = {}

    concepts: list[InterestConcept] = []
    for cid, weight in interest.items():
        entry = tax_concepts.get(cid, {})
        entry = entry if isinstance(entry, dict) else {}
        phrases: set[str] = set()
        label = entry.get("preferred_label")
        if isinstance(label, str) and label.strip():
            phrases.add(label)
        for alias in entry.get("aliases", []) or []:
            if isinstance(alias, str):
                phrases.add(alias)
        phrases.add(_humanize_concept_id(cid))
        padded = tuple(f" {p} " for p in {_norm_phrase(p) for p in phrases} if len(p) >= MIN_ALIAS_LEN)
        if not padded:
            continue
        display = label if isinstance(label, str) and label.strip() else _humanize_concept_id(cid)
        concepts.append(InterestConcept(concept_id=cid, weight=weight, label=display, phrases=padded))

    domain_terms = tuple((f" {d} ", d) for d in (_norm_phrase(x) for x in sorted(domains)) if len(d) >= MIN_ALIAS_LEN)
    return InterestModel(
        weights=MappingProxyType(dict(interest)),
        domains=frozenset(domains),
        concepts=tuple(concepts),
        domain_terms=domain_terms,
        profile_id=str(profile.get("id", "inferred")),
        source=source,
        profile_path=profile_path,
        audience_path=audience_path,
        raw=profile,
        taxonomy_ok=taxonomy_ok,
    )


def _as_interest_model(profile: dict | InterestModel, taxonomy: dict | None = None) -> InterestModel:
    """Accept either an already-compiled model or a raw profile dict.

    Callers that hold a model pass it straight through (the taxonomy is already
    baked in); callers holding a plain dict get it compiled here, so there is
    exactly one interpretation of profile weights in the codebase.

    Passing both a compiled model AND a taxonomy is a caller error, not a silent
    no-op: the taxonomy would be ignored, and the caller would believe alias
    matching was active when it was not.
    """
    if isinstance(profile, InterestModel):
        if taxonomy is not None:
            raise ValueError(
                "taxonomy is ignored when an already-compiled InterestModel is passed; "
                "compile the model with the taxonomy instead (compile_interest_model)."
            )
        return profile
    return compile_interest_model(profile or {}, taxonomy)


def load_interest_model(output_dir: Path, config: dict | None = None, *, today: date | None = None) -> InterestModel:
    """Resolve the user's interest model from disk, inferring one when absent.

    Read-only by design (issue #115): a persisted `_briefings/profile.yaml` wins
    outright; otherwise a profile is inferred in memory and NOT written -
    `profile init` is the only surface that persists one. Both `briefings
    --unseen` and the scan headline digest load through here, so they can never
    disagree about what interests the user.
    """
    briefings_dir = output_dir / BRIEFINGS_DIR_NAME
    existing = _load_usable_profile(briefings_dir)
    profile = existing if existing is not None else _infer_profile(output_dir, config, today=today)
    # A corrupt taxonomy.json (interrupted taxonomy-build, cloud-mount stale read)
    # must not disable ranking - the compiler falls back to humanized concept ids.
    # ValueError covers both JSONDecodeError and UnicodeDecodeError: `briefings`
    # reads taxonomy.json for the first time here, and a half-written file on a
    # cloud mount is as likely to be invalid UTF-8 as invalid JSON.
    taxonomy_ok = True
    try:
        taxonomy = load_taxonomy(output_dir)
    except (ValueError, OSError):
        log.warning("taxonomy.json unreadable; ranking on concept ids only.")
        taxonomy = None
        taxonomy_ok = False
    return compile_interest_model(
        profile,
        taxonomy,
        source="persisted" if existing is not None else "inferred",
        profile_path=briefings_dir / PROFILE_FILENAME,
        audience_path=briefings_dir / AUDIENCE_FILENAME,
        taxonomy_ok=taxonomy_ok,
    )


def rank_unseen(unseen: list[dict], profile: dict | InterestModel) -> list[dict]:
    """Score each unseen video by overlap with the profile's interest concepts.

    score = sum of interest weights for the video's concepts that are interest
    concepts, plus a small domain-affinity bonus. Sorted by (score desc,
    published desc). Videos without concepts.json score 0 and sort last but are
    NOT dropped - a fresh video may not be concept-extracted yet (personalization
    reorders, it never deletes).

    Accepts a compiled `InterestModel` (the shared path) or a raw profile dict
    (compiled here); either way the weights come from one compiler.
    """
    model = _as_interest_model(profile)
    interest, domains = model.weights, model.domains
    scored: list[dict] = []
    for video in unseen:
        score = 0.0
        matched: list[str] = []
        concepts_path = video.get("concepts_path")
        if concepts_path and Path(concepts_path).exists():
            try:
                cdata = json.loads(Path(concepts_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cdata = {}
            for concept in cdata.get("concepts", []):
                if not isinstance(concept, dict):
                    continue
                cid = concept.get("concept_id")
                if cid in interest:
                    score += interest[cid]
                    matched.append(concept.get("preferred_label") or cid)
                elif concept.get("domain") in domains:
                    score += 0.5
                    matched.append(concept.get("preferred_label") or concept.get("domain"))
        scored.append({**video, "score": score, "matched_concepts": matched[:5]})
    scored.sort(key=lambda v: (v["score"], v.get("published", "")), reverse=True)
    return scored


# ---------------------------------------------------------------------------
# Headline digest - peripheral vision over unfollowed channels (issue #113)
# ---------------------------------------------------------------------------


def _is_youtube_channel_source(url: str | None) -> bool:
    """True when `url` is a recognizable YouTube channel URL or a bare UC... id.

    Guards headline eligibility. `get_channel_id()` submits the last URL path
    segment to the YouTube Data API for ANY host, so a mis-flagged non-YouTube
    url (Skool, Vimeo, ...) would still cost an API call. This shape check MUST
    run BEFORE `get_channel_id()` (issue #113 hazard). Host matching is exact
    against a known set so a suffix trick like `notyoutube.com` is rejected.
    """
    if not isinstance(url, str):
        return False
    candidate = url.strip()
    if not candidate:
        return False
    if _UC_CHANNEL_ID_RE.match(candidate):
        return True
    from urllib.parse import urlparse

    try:
        parsed = urlparse(candidate if "//" in candidate else "https://" + candidate)
    except ValueError:
        return False
    host = (parsed.netloc or "").lower().split(":")[0]
    return host in _YOUTUBE_HOSTS


def _channel_scan_enabled(channel: dict) -> bool:
    """True when a channel enters the PRIMARY scan pipeline (full Gemini processing).

    `enabled` defaults True when absent. Only a real boolean True admits a channel;
    a non-boolean `enabled` (e.g. a stray `"headlines"` from a mis-attempted
    tri-state) is treated as disabled rather than truthy-admitted. Without this the
    truthiness gate `c.get("enabled", True)` would silently pull a channel labelled
    with a string into full processing - the exact bug the separate `headline_digest`
    opt-in exists to avoid (issue #113).
    """
    # `.get(..., True)` yields True when the key is absent, so `is True` alone
    # admits absent + boolean-True and rejects False + every non-boolean value.
    return channel.get("enabled", True) is True


def collect_headline_channels(config: dict) -> list[dict]:
    """Channels eligible for the headline digest, per the frozen eligibility rule.

    All must hold: `enabled is False`, `headline_digest is True`, and a recognizably
    YouTube `url`. The YouTube shape check runs HERE, before any `get_channel_id()`
    call, so a mis-flagged non-YouTube source never reaches the API. `enabled: true`
    + `headline_digest: true` is redundant (an enabled channel is already visible) and
    is ignored.
    """
    eligible: list[dict] = []
    for channel in config.get("channels", []):
        if channel.get("enabled", True) is not False:
            continue
        if channel.get("headline_digest") is not True:
            continue
        if not _is_youtube_channel_source(channel.get("url")):
            log.info(
                "Headline digest: skipping non-YouTube source for '%s' (%s).",
                channel.get("name", "?"),
                channel.get("url"),
            )
            continue
        eligible.append(channel)
    return eligible


def rank_headlines(videos: list[dict], profile: dict | InterestModel, taxonomy: dict | None = None) -> list[dict]:
    """Rank metadata-only headline videos by title match against the interest profile.

    Headline videos carry NO concepts.json (no Gemini extraction), so `rank_unseen`
    would score every one zero and collapse to pure recency (issue #113). Instead we
    match normalized title phrases against the profile's interest concepts - using
    taxonomy `preferred_label`/`aliases` where available, else a humanized concept id -
    and the profile's interest domains (a weaker signal). Ties break by recency.

    Returns each video with `score` and `matched_concepts`, sorted (score desc,
    published desc). Accepts a compiled `InterestModel` (the shared path) or a raw
    profile dict plus taxonomy (compiled here), so both surfaces score from the
    same reading of the profile.

    Ranking quality depends on taxonomy quality: with no `taxonomy.json`, each
    interest concept contributes only its humanized concept-id phrase (which rarely
    appears verbatim in a title), so scoring degrades toward pure recency via the
    zero-score "Other headlines" bucket. That degradation is graceful, not a defect.

    One observed phrase is paid for ONCE, at the highest weight among the concepts
    it matched. Taxonomy aliases are shared across concepts (on the live corpus
    "Context Management" is an alias of three separate concepts), so summing per
    concept would let a single generic phrase in a title collect several full
    interest weights - inflating exactly the vaguest headlines, and only on this
    surface (the concept-evidence surface counts a video's own concept ids, which
    cannot collide this way).
    """
    model = _as_interest_model(profile, taxonomy)

    scored: list[dict] = []
    for video in videos:
        title = f" {_norm_phrase(video.get('title', ''))} "
        score = 0.0
        matched: list[str] = []
        hits = [(c, {p for p in c.phrases if p in title}) for c in model.concepts]
        # Heaviest concept claims a contested phrase first, so the cap keeps the
        # strongest interpretation of the evidence rather than an arbitrary one.
        hits = sorted((h for h in hits if h[1]), key=lambda h: (-h[0].weight, h[0].concept_id))
        claimed: set[str] = set()
        for concept, phrases in hits:
            if not phrases - claimed:
                # Every phrase this concept matched was already paid for by a
                # heavier concept: the same words, not independent evidence.
                continue
            claimed |= phrases
            score += concept.weight  # each concept pays at most once
            matched.append(concept.label)
        for padded_domain, domain_label in model.domain_terms:
            if padded_domain in title:
                score += HEADLINE_DOMAIN_MATCH_WEIGHT
                matched.append(domain_label)
        scored.append({**video, "score": score, "matched_concepts": matched[:5]})
    scored.sort(key=lambda v: (v["score"], v.get("published", "")), reverse=True)
    return scored


def load_headlines_seen_ids(output_dir: Path) -> list[str]:
    """Load the ordered seen-set from `_headlines/seen.json` (empty on any failure)."""
    path = output_dir / HEADLINES_DIR_NAME / HEADLINES_SEEN_FILENAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    ids = data.get("seen") if isinstance(data, dict) else data
    if not isinstance(ids, list):
        return []
    # Dedupe preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for vid in ids:
        key = str(vid)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def advance_headlines_seen(output_dir: Path, new_ids: list[str]) -> None:
    """Append `new_ids` to the seen-set and persist, trimmed to the newest HEADLINES_SEEN_MAX.

    Order is preserved (oldest first) so trimming keeps the most-recently-seen ids.
    Callers advance the set ONLY after a real (non-dry-run) render.
    """
    existing = load_headlines_seen_ids(output_dir)
    seen = set(existing)
    for vid in new_ids:
        key = str(vid)
        if key not in seen:
            seen.add(key)
            existing.append(key)
    trimmed = existing[-HEADLINES_SEEN_MAX:]
    headlines_dir = output_dir / HEADLINES_DIR_NAME
    headlines_dir.mkdir(parents=True, exist_ok=True)
    path = headlines_dir / HEADLINES_SEEN_FILENAME
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps({"seen": trimmed}, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _select_headline_items(ranked: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split ranked headlines into (positive interest-matches, a few zero-score recents).

    Positive matches come first up to the global cap; remaining slots are filled with
    the most recent zero-score items (capped), mirroring the briefings rule that
    zero-score items are surfaced under "Other headlines", not dropped.
    """
    positive = [v for v in ranked if v["score"] > 0][:HEADLINES_MAX_ITEMS]
    remaining = HEADLINES_MAX_ITEMS - len(positive)
    # `<= 0`, not `== 0`: a negative weight (a hand-edit saying "I dislike this")
    # must rank an item LAST, never delete it - personalization reorders, it does
    # not filter. With `== 0` a negatively-scored item matched neither bucket, so
    # it was dropped without ever entering the seen-set and re-dropped every run.
    zero = [v for v in ranked if v["score"] <= 0][: min(remaining, HEADLINES_MAX_ZERO_SCORE)] if remaining > 0 else []
    return positive, zero


def render_headline_digest(youtube, config: dict, output_dir: Path, *, dry_run: bool) -> list[dict]:
    """Fetch, rank, and render the headline digest; advance the seen-set after render.

    Metadata-only: uses the cheap uploads-playlist path plus a duration enrich for the
    Shorts filter. Makes NO Gemini calls and writes NO corpus artifacts. Non-fatal by
    construction - the caller runs it last, after wanted work. Returns the rendered
    items (also useful for a future standalone `headlines` command). A `--dry-run`
    renders but does not advance `_headlines/seen.json`.
    """
    headline_channels = collect_headline_channels(config)
    log.info("Headline digest: %d channel(s) considered.", len(headline_channels))
    if not headline_channels:
        return []

    # The SAME compiled interest model `briefings --unseen` ranks with (issue #115),
    # and it never persists a profile: a scan must not create profile.yaml as a
    # side effect. A corrupt taxonomy.json degrades ranking inside the loader
    # rather than aborting the digest via cmd_scan's outer except.
    model = load_interest_model(output_dir, config)
    if not model.taxonomy_ok:
        # Rendering here would burn irreversible seen-state on an unranked list:
        # a corrupt taxonomy.json also empties an inferred profile, so every item
        # scores 0, five recents get marked seen, and after the taxonomy is
        # repaired those videos can never be surfaced ranked. Skipping costs one
        # digest; rendering costs those videos permanently.
        log.warning(
            "Headline digest: taxonomy.json is unreadable, so ranking would be blind. "
            "Skipping the digest this run (nothing marked seen). Re-run `taxonomy-build` to restore it."
        )
        return []
    seen = set(load_headlines_seen_ids(output_dir))
    since_dt = datetime.now(UTC) - timedelta(days=HEADLINES_LOOKBACK_DAYS)

    collected: list[dict] = []
    for channel in headline_channels:
        try:
            channel_id, channel_title = get_channel_id(youtube, channel["url"])
        except HttpError as e:
            # Symmetric with the fetch path below: a quota-exhaustion at channel
            # resolution stops the digest early rather than aborting the scan.
            if e.resp.status == 403 and _is_quota_exceeded(e):
                log.warning("Headline digest: YouTube quota exhausted; stopping digest early.")
                break
            raise
        if not channel_id:
            log.warning("Headline digest: channel not found: %s", channel.get("url"))
            continue
        try:
            videos = fetch_channel_videos(youtube, channel_id, since_dt)
        except HttpError as e:
            if e.resp.status == 403 and _is_quota_exceeded(e):
                log.warning("Headline digest: YouTube quota exhausted; stopping digest early.")
                break
            raise
        for v in videos:
            if v.get("video_id") in seen:
                continue
            v["channel"] = channel["name"]
            v["channel_title"] = channel_title or channel["name"]
            collected.append(v)

    if not collected:
        log.info("Headline digest: no new uploads to surface.")
        return []

    # Shorts filter via duration enrich (no Gemini). Quota-exhaustion is non-fatal:
    # skip the filter entirely rather than fall through to is_short()'s per-video
    # /shorts HEAD probe - that would trade one quota failure for N live network
    # round-trips in a path advertised as metadata-only. Un-filtered items are
    # kept (fail-safe to long-form, same bias as is_short).
    durations: dict[str, str | None] = {}
    quota_degraded = False
    try:
        durations = enrich_with_durations(youtube, [v["video_id"] for v in collected])
    except HttpError as e:
        if e.resp.status == 403 and _is_quota_exceeded(e):
            log.warning("Headline digest: quota exhausted during Shorts check; keeping items unfiltered.")
            quota_degraded = True
        else:
            raise
    if not quota_degraded:
        for v in collected:
            v["duration_iso"] = durations.get(v["video_id"])
        collected = [v for v in collected if not is_short(v["video_id"], v["duration_iso"])]

    ranked = rank_headlines(collected, model)
    positive, zero = _select_headline_items(ranked)
    rendered = positive + zero
    if not rendered:
        log.info("Headline digest: no new uploads to surface.")
        return []

    log.info("")
    log.info("=== Other headlines - new in channels you're not actively following ===")
    for v in positive:
        log.info("  * [%s] %s  (%s)  %s", v["channel"], v.get("title", ""), v.get("published", ""), v.get("url", ""))
        if v.get("matched_concepts"):
            log.info("      matches: %s", ", ".join(v["matched_concepts"]))
    if zero:
        log.info("  -- Other headlines --")
        for v in zero:
            log.info(
                "  . [%s] %s  (%s)  %s", v["channel"], v.get("title", ""), v.get("published", ""), v.get("url", "")
            )

    if not dry_run:
        advance_headlines_seen(output_dir, [v["video_id"] for v in rendered])
    return rendered


def extract_mindmap_links(mindmap_path, url: str, limit: int = 3) -> list[tuple[str, str]]:
    """Best-effort: first `limit` '(M:SS)'/'(H:MM:SS)' timestamps -> deep-links.

    Returns [(label, url&t=Ns), ...]; empty when no mindmap or no timestamps.
    Mirrors the hand-authored viewing-guide style without being load-bearing.
    """
    if not mindmap_path or not Path(mindmap_path).exists():
        return []
    links: list[tuple[str, str]] = []
    try:
        text = Path(mindmap_path).read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        match = _MINDMAP_TIMESTAMP_RE.search(line)
        if not match:
            continue
        first, mm, ss = match.groups()
        if ss is not None:
            seconds = int(first) * 3600 + int(mm) * 60 + int(ss)
            stamp = f"{first}:{mm}:{ss}"
        else:
            seconds = int(first) * 60 + int(mm)
            stamp = f"{first}:{mm}"
        label = re.sub(r"\s+", " ", line[: match.start()].strip(" \t-*•")).strip() or "jump"
        links.append((f"{label} ({stamp})", f"{url}&t={seconds}s"))
        if len(links) >= limit:
            break
    return links


def _format_age(published: date | None, today: date) -> str:
    """Human-readable age badge from a publish date, e.g. "3y", "8mo", "5d".

    Mechanically derived from the date only (issue #88) - deliberately NOT a
    semantic "evergreen" judgment, which the ranking score does not support.
    Returns "" when the date is missing or in the future (nothing to badge).
    """
    if published is None or published > today:
        return ""
    days = (today - published).days
    if days >= 365:
        return f"{days // 365}y"
    if days >= 30:
        return f"{days // 30}mo"
    return f"{days}d"


def _window_label(lower: date, upper: date) -> str:
    """Human-facing window label. Shows "unbounded" rather than the bare
    0001-01-01 sentinel when there's no lower floor (issue #88). Front-matter
    fields keep the machine-stable ISO date; only visible prose uses this."""
    lower_txt = "unbounded" if lower == date.min else lower.isoformat()
    return f"{lower_txt} to {upper.isoformat()}"


def render_unseen_briefing(ranked: list[dict], profile: dict, *, lower, upper, today=None) -> str:
    """Render a catch-up briefing markdown doc with the standard front matter.

    The `video_ids` front-matter list is what makes this briefing count toward
    future coverage (so the next --unseen run won't re-surface these). The
    primary list stays strictly relevance-ranked (issue #88) - old-but-high-
    relevance videos surface near the top, not buried by recency. Temporal
    structure lives in a per-item age badge and a secondary "By year" appendix,
    never as headers inside the primary list (which would fight the sort).
    """
    if today is None:
        today = datetime.now(UTC).date()
    front_matter = {
        "artifact_type": "viewing_guide",
        "schema_version": 1,
        "title": f"Catch-up briefing - unseen videos ({lower.isoformat()} to {upper.isoformat()})",
        "created_at": today.isoformat(),
        "corpus_snapshot_date": today.isoformat(),
        "scan_window": {"start": lower.isoformat(), "end": upper.isoformat()},
        "audience_profile": profile.get("id", "inferred"),
        "generator": {"name": "briefings --unseen", "version": 1},
        "video_ids": [v["video_id"] for v in ranked],
    }
    lines = [
        "---",
        yaml.safe_dump(front_matter, sort_keys=False, allow_unicode=True).rstrip(),
        "---",
        "",
        f"# Catch-up briefing - {len(ranked)} unseen video(s)",
        "",
        f"**Window:** {_window_label(lower, upper)} (UTC)",
        (
            f"**Ranking lens:** inferred profile `{profile.get('id', 'inferred')}` "
            "(top corpus concepts + scanned channels). One reader's lens, not an "
            "objective ranking - run `profile init`, then hand-edit "
            "`_briefings/profile.yaml` to retune."
        ),
        "",
    ]
    if not ranked:
        lines.append("_No unseen videos in this window._")
        return "\n".join(lines) + "\n"
    for video in ranked:
        # Escape link-breaking brackets - YouTube titles are creator-controlled
        # and a title like "free gpt ](evil)" would otherwise corrupt the link.
        safe_title = str(video["title"]).replace("[", "\\[").replace("]", "\\]")
        lines.append(f"## [{safe_title}]({video['url']})")
        meta_line = f"{video['channel']} · {video.get('published', '')}"
        age = _format_age(_parse_iso_date(video.get("published", "")), today)
        if age:
            meta_line += f" · age {age}"
        if video.get("score"):
            meta_line += f" · relevance {video['score']:g}"
        lines.append(meta_line)
        if video.get("matched_concepts"):
            lines.append("")
            lines.append("Why: " + ", ".join(video["matched_concepts"]))
        # Skip deep-links for zero-score entries: those are the least-trustworthy
        # mindmaps (often off-topic livestream captures whose timestamps mismatch
        # the title). Only adorn entries the ranking actually vouched for.
        links = extract_mindmap_links(video.get("mindmap_path"), video["url"]) if video.get("score") else []
        if links:
            lines.append("")
            lines.append(" · ".join(f"[{label}]({u})" for label, u in links))
        lines.append("")

    lines.extend(_render_by_year_appendix(ranked))
    return "\n".join(lines) + "\n"


def _render_by_year_appendix(ranked: list[dict]) -> list[str]:
    """Secondary chronological index of the SAME videos in the primary list.

    Groups the ranked set by publish year (newest year first) for quick
    temporal scanning, without touching the relevance-first primary order
    (issue #88). Same video_ids, same links - just a different lens. Videos
    with an unparseable year (should be none post-`select_unseen`) are skipped.
    """
    by_year: dict[int, list[dict]] = {}
    for video in ranked:
        published = _parse_iso_date(video.get("published", ""))
        if published is None:
            continue
        by_year.setdefault(published.year, []).append(video)
    if not by_year:
        return []
    out = ["---", "", "## By year", ""]
    for year in sorted(by_year, reverse=True):
        out.append(f"### {year}")
        for video in by_year[year]:
            safe_title = str(video["title"]).replace("[", "\\[").replace("]", "\\]")
            out.append(f"- [{safe_title}]({video['url']}) · {video['channel']}")
        out.append("")
    return out


def cmd_briefings(args, config):
    """Generate catch-up briefings for videos not yet surfaced in any briefing."""
    if not getattr(args, "unseen", False):
        log.error("briefings: only --unseen mode is implemented. Pass --unseen.")
        sys.exit(1)

    output_dir = resolve_output_dir(config)
    briefings_dir = output_dir / BRIEFINGS_DIR_NAME
    today = datetime.now(UTC).date()
    since = parse_since(args.since).date() if getattr(args, "since", None) else None
    until = parse_since(args.until).date() if getattr(args, "until", None) else None
    lower, upper = compute_catchup_window(since=since, until=until, today=today)
    if lower > upper:
        # An inverted window matches nothing; without this warning a zero-result
        # run looks identical to "fully caught up". --until takes an absolute date.
        log.warning(
            "briefings --unseen: window start %s is after end %s - no videos can match. "
            "Check --since/--until (--until expects an absolute YYYY-MM-DD).",
            lower.isoformat(),
            upper.isoformat(),
        )

    seen = load_seen_video_ids(briefings_dir)
    videos = collect_corpus_videos(output_dir)
    unseen = select_unseen(videos, seen, lower=lower, upper=upper)
    # One shared, read-only load (issue #115): the same compiled model the scan's
    # headline digest ranks with. `source` doubles as the cold-start signal - an
    # empty/broken profile.yaml resolves to "inferred", so the warning below still
    # fires. Persisting is `profile init`'s job, so every briefings run (dry or
    # not) is side-effect-free about the profile.
    dry_run = getattr(args, "dry_run", False)
    model = load_interest_model(output_dir, config, today=today)
    profile = model.raw
    had_tuned_profile = model.source == "persisted"
    ranked = rank_unseen(unseen, model)
    total_unseen = len(ranked)
    # Cap to a digestible guide (a 589-item dump is an index, not a briefing).
    # --limit 0 means "no cap"; the rest stay unseen for the next run.
    limit = getattr(args, "limit", DEFAULT_LIMIT)
    if limit is None:
        limit = DEFAULT_LIMIT
    if limit > 0:
        ranked = ranked[:limit]

    # Cold-start guard (issue #88): on the first run against a large corpus the
    # profile is freshly inferred (no hand-tuning yet), so it can overweight
    # generic/frequent concepts. That only matters when videos are actually
    # being deferred (total > cap) - then rank order decides what you see now vs
    # later. Warn rather than reimpose a recency floor (a floor hides old-but-
    # important videos, a strictly worse failure).
    if not had_tuned_profile and limit > 0 and total_unseen > limit:
        # Don't tell someone already in --dry-run to "preview with --dry-run".
        next_step = (
            "run `profile init` to persist it, hand-edit _briefings/profile.yaml, then generate."
            if dry_run
            else "run `profile show` to see the lens, `profile init` to persist it, then hand-edit to retune."
        )
        log.warning(
            "briefings --unseen: %d unseen videos but no tuned _briefings/profile.yaml yet; "
            "the top %d are ranked by a freshly-inferred profile. %s",
            total_unseen,
            limit,
            next_step,
        )

    log.info(
        "briefings --unseen: %d corpus videos, %d already surfaced, %d unseen in %s..%s; showing %d",
        len(videos),
        len(seen),
        total_unseen,
        lower.isoformat(),
        upper.isoformat(),
        len(ranked),
    )

    if getattr(args, "dry_run", False):
        capped = f" (top {len(ranked)} of {total_unseen})" if len(ranked) < total_unseen else ""
        print(f"Would surface {len(ranked)} unseen video(s){capped} in {_window_label(lower, upper)}:")
        for video in ranked:
            tag = f"[{video['score']:g}] " if video.get("score") else ""
            print(f"  {video.get('published', ''):<10}  {video['channel']:<18}  {tag}{video['title']}")
        if not ranked:
            print("  (none)")
        return

    if not ranked:
        print(f"No unseen videos in {_window_label(lower, upper)}. Nothing written.")
        return

    content = render_unseen_briefing(ranked, profile, lower=lower, upper=upper, today=today)
    briefings_dir.mkdir(parents=True, exist_ok=True)
    # Never overwrite an earlier same-day briefing: doing so would drop its
    # video_ids from the seen-coverage record and re-surface those videos later.
    base_name = f"{today.isoformat()}-catch-up-unseen"
    out_path = briefings_dir / f"{base_name}.md"
    counter = 2
    while out_path.exists():
        out_path = briefings_dir / f"{base_name}-{counter}.md"
        counter += 1
    out_path.write_text(content, encoding="utf-8")
    print(f"Wrote catch-up briefing: {out_path} ({len(ranked)} videos)")

    # --pdf is purely additive: the Markdown above stays the canonical
    # seen-coverage record (load_seen_video_ids only parses *.md front matter),
    # and the PDF is a shareable render of the SAME ranked set. The link
    # extractor mirrors render_unseen_briefing's zero-score skip exactly so the
    # two artifacts stay in lockstep.
    if getattr(args, "pdf", False):
        try:
            from briefing_pdf import render_unseen_briefing_pdf
        except ImportError:
            log.error(
                'PDF export needs reportlab. Install the optional extra: pip install -e ".[pdf]" '
                "(or pip install reportlab). The Markdown briefing was written regardless."
            )
        else:
            pdf_path = out_path.with_suffix(".pdf")

            def _links(video):
                return extract_mindmap_links(video.get("mindmap_path"), video["url"]) if video.get("score") else []

            render_unseen_briefing_pdf(
                ranked, profile, pdf_path, lower=lower, upper=upper, link_extractor=_links, today=today
            )
            print(f"Wrote catch-up briefing PDF: {pdf_path}")


# ---------------------------------------------------------------------------
# profile subcommand - see and initialize the personalization surface (#115)
# ---------------------------------------------------------------------------

PROFILE_SHOW_TOP = 10


def _profile_show(output_dir: Path, config: dict) -> None:
    """Print the resolved interest model and where it comes from. Writes nothing.

    Zero side effects is the contract: no directory is created, no profile is
    persisted. `profile init` is the only write surface.
    """
    model = load_interest_model(output_dir, config)
    # A file that exists but does not parse to a non-empty mapping is IGNORED for
    # ranking. Reporting that as "not on disk" would hide the one failure this
    # command exists to catch: a hand-edit (the only retune path) that broke.
    profile_on_disk = bool(model.profile_path and model.profile_path.exists())
    if model.source == "persisted":
        profile_state = "persisted"
    elif profile_on_disk:
        profile_state = "IGNORED - file exists but is empty or unparseable"
    else:
        profile_state = "inferred (ephemeral - not on disk)"
    audience_state = "present" if model.audience_path and model.audience_path.exists() else "absent"

    print("Personalization profile")
    print(f"  Corpus         : {output_dir}")
    print(f"  Ranking weights: {model.profile_path}  [{profile_state}]")
    print(f"  Reader context : {model.audience_path}  [{audience_state}]")
    print(f"  Profile id     : {model.profile_id}")
    # A concept whose phrases are all shorter than MIN_ALIAS_LEN scores on the
    # concept surface but is structurally unmatchable in a title, so report both
    # counts rather than implying every interest reaches both surfaces.
    matchable = len(model.concepts)
    interests_line = f"  Interests      : {len(model.weights)} weighted concept(s), {len(model.domains)} domain(s)"
    if matchable < len(model.weights):
        interests_line += f"; {matchable} matchable in headline titles"
    print(interests_line)
    if not model.taxonomy_ok:
        print("  Taxonomy       : UNREADABLE - matching falls back to concept ids (run `taxonomy-build`)")
    for cid, weight in sorted(model.weights.items(), key=lambda kv: (-kv[1], kv[0]))[:PROFILE_SHOW_TOP]:
        label = next((c.label for c in model.concepts if c.concept_id == cid), _humanize_concept_id(cid))
        print(f"      {weight:>6g}  {cid}  ({label})")
    if len(model.weights) > PROFILE_SHOW_TOP:
        print(f"      ... {len(model.weights) - PROFILE_SHOW_TOP} more")
    if model.domains:
        print(f"  Domains        : {', '.join(sorted(model.domains))}")
    print()
    print("Both `briefings --unseen` and the scan's headline digest rank from this one model.")
    if profile_state.startswith("IGNORED"):
        print(
            f"Your {PROFILE_FILENAME} is not being used: it is empty or not valid YAML, so the weights "
            "above were inferred instead. Fix the file (or delete it and re-run `profile init`). "
            "Nothing was written."
        )
    elif model.source != "persisted":
        print("Run `profile init` to persist it, then hand-edit the file to retune. Nothing was written.")


def _profile_init(output_dir: Path, config: dict) -> None:
    """Persist the currently-inferred profile and scaffold the audience notes.

    Never overwrites either file - including a partial or malformed one. A broken
    profile.yaml is still the user's file, and hand-editing is the retune path.
    """
    briefings_dir = output_dir / BRIEFINGS_DIR_NAME
    briefings_dir.mkdir(parents=True, exist_ok=True)

    profile_path = briefings_dir / PROFILE_FILENAME
    if profile_path.exists():
        print(f"Kept existing ranking weights: {profile_path} (never overwritten - edit it to retune)")
        # Still never overwritten, but say so out loud: an unusable file is
        # silently ignored at ranking time, and a neutral "kept" line would let
        # the user believe a broken hand-edit is in force.
        if _load_usable_profile(briefings_dir) is None:
            log.warning(
                "%s is empty or not valid YAML, so ranking ignores it and infers instead. "
                "Fix the file, or delete it and re-run `profile init` for a fresh one.",
                profile_path,
            )
    else:
        profile = _infer_profile(output_dir, config)
        if not profile.get("interest_concepts"):
            # Persisting an empty profile is a one-way trap: it would then be the
            # "persisted" profile forever (never overwritten, cold-start warning
            # suppressed), so every surface would score 0 with nothing to explain
            # why. Almost always this just means the corpus has no taxonomy yet.
            print(
                f"Nothing to persist yet: no interest concepts could be inferred from {output_dir}. "
                "Run `taxonomy-build` first (it needs concept-extracted videos), then `profile init`."
            )
            _profile_init_audience(briefings_dir)
            return
        # Atomic write (the house idiom for every corpus artifact): a torn write
        # here would be permanent, because a malformed profile.yaml is never
        # overwritten by design. Corpora commonly live on cloud-synced mounts.
        tmp_path = profile_path.with_suffix(".yaml.tmp")
        tmp_path.write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True), encoding="utf-8")
        tmp_path.replace(profile_path)
        print(f"Wrote ranking weights: {profile_path} ({len(profile.get('interest_concepts', {}))} concepts)")

    _profile_init_audience(briefings_dir)
    print()
    print("Edit the two files to retune. `profile show` prints the resolved model and these paths.")


def _profile_init_audience(briefings_dir: Path) -> None:
    """Scaffold `audience.md` from the template unless the user already has one.

    Independent of the ranking weights on purpose: the prose profile is useful
    (and hand-editable) even when there is nothing to infer weights from yet.
    """
    audience_path = briefings_dir / AUDIENCE_FILENAME
    if audience_path.exists():
        print(f"Kept existing reader context: {audience_path} (never overwritten)")
        return
    template = SKILL_DIR / "examples" / AUDIENCE_FILENAME
    try:
        tmp_audience = audience_path.with_suffix(".md.tmp")
        tmp_audience.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        tmp_audience.replace(audience_path)
    except OSError:
        log.warning("Could not scaffold %s from the template at %s; author it by hand.", audience_path, template)
    else:
        print(f"Scaffolded reader context: {audience_path} (edit it - it is prose for you, not weights)")


def cmd_profile(args, config):
    """`profile show` (read-only) / `profile init` (persist, never overwrite)."""
    if getattr(args, "profile_action", None) == "init":
        _profile_init(resolve_output_dir(config), config)
    else:
        # create=False: `show` promises zero writes, and the default mkdir would
        # otherwise create the corpus tree while printing "nothing was written".
        _profile_show(resolve_output_dir(config, create=False), config)


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
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Set logging verbosity (default: info)",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=None,
        help=(
            "Gemini model override (default: config.yaml model field, "
            "or gemini-3-flash-preview). "
            "Gemini 3.x Flash: best for video understanding (mindmaps, screen content). "
            "Gemini 2.5 Pro: more reliable structured JSON output, higher token limit "
            "- prefer for transcripts when Flash truncates. "
            "Applies to: scan, mindmap, transcript, concepts."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scan command
    scan_parser = subparsers.add_parser("scan", help="Scan channels for new videos")
    scan_parser.add_argument("--channel", help="Scan only this channel name")
    scan_parser.add_argument("--since", help="Override lookback window (e.g. 14d, 2026-01-01)")
    scan_parser.add_argument("--dry-run", action="store_true", help="Preview without processing")
    scan_parser.add_argument("--force", action="store_true", help="Regenerate mindmaps even if they exist")
    scan_parser.add_argument(
        "--chunk-minutes",
        type=int,
        default=None,
        dest="chunk_minutes",
        help=(
            "Split transcripts longer than this into chunks (issue #128). "
            f"Overrides per-channel and top-level chunk_minutes; default {TRANSCRIPT_CHUNK_MINUTES_DEFAULT}. "
            "Lower it (e.g. 20) for dense conference talks that hit the output cap."
        ),
    )

    # mindmap command
    mm_parser = subparsers.add_parser("mindmap", help="Generate mind map for a specific video")
    mm_source = mm_parser.add_mutually_exclusive_group(required=True)
    mm_source.add_argument("--url", help="YouTube video URL")
    mm_source.add_argument(
        "--file",
        help=(
            "Path to local video file (.mp4/.mov/.mkv/.webm/.avi). Pair with --channel to "
            "route artifacts into output_dir/<channel>/ for gated-video recovery."
        ),
    )
    mm_parser.add_argument("--prompt", help="Prompt name (default: config default_prompt)")
    mm_parser.add_argument(
        "--channel",
        help=(
            "Channel name for output folder. With --url: where artifacts land. "
            "With --file: enables in-place recovery routing (inferred from parent folder "
            "when the file lives under output_dir/<channel>/; explicit override otherwise)."
        ),
    )
    mm_parser.add_argument(
        "--video-id",
        dest="video_id",
        help=(
            "11-char YouTube video ID. With --file + --channel: used to match an existing "
            "canonical scan meta.json (G2 dedup) or to stamp the video_url in a fresh meta."
        ),
    )
    mm_parser.add_argument(
        "--title",
        help="Video title. Auto-detected from YouTube snippet with --url; falls back to filename stem with --file.",
    )
    mm_parser.add_argument(
        "--date",
        help=(
            "Publish date YYYY-MM-DD. Defaults to today with --url; falls back to the file's "
            "mtime with --file when not given."
        ),
    )
    mm_parser.add_argument("--force", action="store_true", help="Regenerate even if mindmap exists")
    mm_parser.add_argument(
        "--media-resolution",
        choices=["low", "high"],
        default="low",
        dest="media_resolution",
        help=(
            "Gemini media resolution for the mindmap-from-video path "
            "(default: low). LOW yields equivalent quality at 3x lower input-token "
            "cost for theme/concept extraction (issue #58 Gate 3); HIGH is for the "
            "rare case where the prompt depends on reading fine on-screen text. "
            "Ignored on the mindmap-from-transcript path (text-only)."
        ),
    )

    # transcript command
    tx_parser = subparsers.add_parser("transcript", help="Transcribe a specific video")
    tx_source = tx_parser.add_mutually_exclusive_group(required=True)
    tx_source.add_argument("--url", help="YouTube video URL")
    tx_source.add_argument(
        "--file",
        help=(
            "Path to local video file (.mp4/.mov/.mkv/.webm/.avi). Pair with --channel to "
            "route artifacts into output_dir/<channel>/ for gated-video recovery."
        ),
    )
    tx_parser.add_argument("--start", help="Segment start time (MM:SS, HH:MM:SS, or raw seconds)")
    tx_parser.add_argument("--end", help="Segment end time (MM:SS, HH:MM:SS, or raw seconds)")
    tx_parser.add_argument(
        "--channel",
        help=(
            "Channel name for output folder. With --url: where artifacts land. "
            "With --file: enables in-place recovery routing (inferred from parent folder "
            "when the file lives under output_dir/<channel>/; explicit override otherwise)."
        ),
    )
    tx_parser.add_argument(
        "--video-id",
        dest="video_id",
        help=(
            "11-char YouTube video ID. With --file + --channel: used to match an existing "
            "canonical scan meta.json (G2 dedup) or to stamp the video_url in a fresh meta."
        ),
    )
    tx_parser.add_argument(
        "--title",
        help="Video title. Auto-detected from YouTube snippet with --url; falls back to filename stem with --file.",
    )
    tx_parser.add_argument(
        "--date",
        help=(
            "Publish date YYYY-MM-DD. Defaults to today with --url; falls back to the file's "
            "mtime with --file when not given."
        ),
    )
    tx_parser.add_argument("--force", action="store_true", help="Regenerate even if transcript exists")
    tx_parser.add_argument(
        "--chunk-minutes",
        type=int,
        default=None,
        dest="chunk_minutes",
        help=(
            f"Chunk size in minutes for auto-splitting long videos via the YouTube URL path. "
            f"Default: per-channel/top-level chunk_minutes from config.yaml, else {TRANSCRIPT_CHUNK_MINUTES_DEFAULT}. "
            "Manual --start/--end disables chunking."
        ),
    )
    tx_parser.add_argument(
        "--media-resolution",
        choices=["low", "high"],
        default="low",
        dest="media_resolution",
        help=(
            "Gemini media resolution for the single-shot transcript path "
            "(default: low). LOW yields equivalent quality at 3x lower input-token "
            "cost for talking-head + slide content (issue #58 Gate 3) and is required "
            "to fit hour-long videos under Gemini's 1M-token cap. The chunked-transcript "
            "path is hardcoded to LOW regardless of this flag."
        ),
    )
    tx_parser.add_argument(
        "--transcript-source",
        choices=["gemini", "yt-captions", "auto"],
        default=None,
        dest="transcript_source",
        help=(
            "Where the transcript text comes from (issue #60). 'gemini' (default): "
            "multimodal transcript. 'yt-captions': build from the YouTube English "
            "caption track only (cheap, speech-only - no SCREEN/diarization). 'auto': "
            "try Gemini, fall back to captions on failure (token-cap, 403, confabulation). "
            "Overrides the per-channel transcript_source config knob."
        ),
    )

    # process command: one-upload full pipeline for local MP4s
    process_parser = subparsers.add_parser(
        "process",
        help="Full pipeline (mindmap + transcript + concepts) on a video. --file uploads a local MP4 once; --url chunks a YouTube URL when long.",
    )
    process_source = process_parser.add_mutually_exclusive_group(required=True)
    process_source.add_argument(
        "--file",
        help="Path to local video file. The channel is inferred from the parent folder (output_dir/<channel>/X.mp4) or passed explicitly via --channel.",
    )
    process_source.add_argument(
        "--url",
        help="YouTube video URL (issue #50). Auto-chunks transcripts on long videos via --chunk-minutes.",
    )
    process_parser.add_argument(
        "--channel",
        help="Channel name (must exist in config.yaml). Overrides parent-folder inference.",
    )
    process_parser.add_argument(
        "--video-id",
        dest="video_id",
        help="11-char YouTube video ID. Used for G2 dedup against existing canonical scan meta.json.",
    )
    process_parser.add_argument(
        "--title",
        help="Video title. Falls back to filename stem (--file) or YouTube snippet (--url).",
    )
    process_parser.add_argument(
        "--date",
        help="Publish date YYYY-MM-DD. Falls back to the file's mtime (--file) or YouTube publishedAt (--url).",
    )
    process_parser.add_argument("--start", help="Segment start time (MM:SS, HH:MM:SS, or raw seconds)")
    process_parser.add_argument("--end", help="Segment end time (MM:SS, HH:MM:SS, or raw seconds)")
    process_parser.add_argument("--force", action="store_true", help="Regenerate all artifacts from scratch")
    process_parser.add_argument("--prompt", help="Mindmap prompt name (overrides config default)")
    process_parser.add_argument(
        "--chunk-minutes",
        type=int,
        default=None,
        dest="chunk_minutes",
        help=(
            f"Chunk size in minutes for the transcript step on long videos. "
            f"Default: per-channel/top-level chunk_minutes from config.yaml, else {TRANSCRIPT_CHUNK_MINUTES_DEFAULT}. "
            "Applies to both --url and --file paths. Auto-triggered when video duration "
            "exceeds the threshold; disabled when manual --start/--end is set on --file."
        ),
    )
    process_parser.add_argument(
        "--media-resolution",
        choices=["low", "high"],
        default="low",
        dest="media_resolution",
        help=(
            "Gemini media resolution for the mindmap-from-video path "
            "(default: low). LOW yields equivalent quality at 3x lower input-token "
            "cost for theme/concept extraction (issue #58 Gate 3); HIGH is for the "
            "rare case where the prompt depends on reading fine on-screen text. "
            "Ignored on the mindmap-from-transcript path (text-only)."
        ),
    )
    process_parser.add_argument(
        "--transcript-source",
        choices=["gemini", "yt-captions", "auto"],
        default=None,
        dest="transcript_source",
        help=(
            "Where the transcript text comes from (issue #60). 'gemini' (default): "
            "multimodal transcript. 'yt-captions': YouTube caption track only "
            "(cheap, speech-only). 'auto': try Gemini, fall back to captions on "
            "failure. Overrides the per-channel transcript_source config knob. "
            "Applies to the --url path; --file uploads are always Gemini multimodal."
        ),
    )

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
    search_parser.add_argument(
        "--since",
        help="Filter to videos published within a window. Accepts 'Nd' (e.g. '30d') or 'YYYY-MM-DD'.",
    )
    search_parser.add_argument(
        "--no-expand",
        action="store_true",
        dest="no_expand",
        help=(
            "Disable Stage-1 taxonomy query expansion (hybrid mode only). "
            "Used for A/B diagnostic comparison against the baseline."
        ),
    )

    # index command
    index_parser = subparsers.add_parser("index", help="Build vector search index from transcripts")
    index_parser.add_argument("--channel", help="Index only this channel")
    index_parser.add_argument("--force", action="store_true", help="Rebuild index from scratch")

    # nugget command
    nugget_parser = subparsers.add_parser(
        "nugget",
        help="Synthesize a consultant-grade nugget brief across creators for a query",
    )
    nugget_parser.add_argument("query", help="Research question to probe across creators")
    nugget_parser.add_argument("--channel", help="Restrict to this channel (default: all)")
    nugget_parser.add_argument(
        "--limit",
        type=int,
        default=15,
        help="Max excerpts feeding the synthesis (default: 15)",
    )
    nugget_parser.add_argument(
        "--since",
        help="Only consider videos published within this window. 'Nd' or 'YYYY-MM-DD'.",
    )
    nugget_parser.add_argument(
        "--min-relevance",
        type=float,
        default=0.0,
        dest="min_relevance",
        help="Minimum relevance score (RRF scale) for inclusion (default: 0.0)",
    )
    nugget_parser.add_argument(
        "--no-expand",
        action="store_true",
        dest="no_expand",
        help="Disable Stage-1 taxonomy query expansion",
    )
    nugget_parser.add_argument(
        "--output",
        help="Write briefing to this file instead of stdout",
    )

    # status command
    subparsers.add_parser("status", help="Show corpus status: output dir, channels, artifact counts")

    # dedupe command
    dedupe_parser = subparsers.add_parser(
        "dedupe",
        help="Find and clean up title-rotation duplicates (same video_id, different slug)",
    )
    dedupe_parser.add_argument("--channel", help="Restrict to this channel (default: all configured channels)")
    dedupe_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually mutate disk. Default is dry-run (report only).",
    )

    # repair-metas command (issue #66)
    repair_parser = subparsers.add_parser(
        "repair-metas",
        help="Backfill missing identity (video_id/url/title/published) into transcript metas from their .transcript.md headers (issue #66).",
    )
    repair_parser.add_argument("--channel", help="Restrict to this channel (default: all channels).")
    repair_parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the backfilled fields. Default is dry-run (report only).",
    )

    # prune-shorts command
    prune_parser = subparsers.add_parser(
        "prune-shorts",
        help="Find and delete YouTube Shorts artifacts (mindmap, transcript, concepts, meta)",
    )
    prune_parser.add_argument(
        "--channel",
        help="Restrict to this channel (default: all configured channels)",
    )
    prune_parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually mutate disk. Default is dry-run (report only).",
    )

    # mark-skip command (issue #42): set skip_modes on a video's meta.json
    mark_skip_parser = subparsers.add_parser(
        "mark-skip",
        help="Mark a video to skip one or more processing modes (writes skip_modes to meta.json)",
    )
    mark_skip_parser.add_argument("--url", required=True, help="YouTube URL of the video to mark")
    mark_skip_parser.add_argument(
        "--mode",
        action="append",
        required=True,
        choices=SKIP_MODES_VALID,
        help="Processing mode to skip. Repeat for multiple modes (e.g. --mode transcript --mode concepts).",
    )
    mark_skip_parser.add_argument(
        "--reason",
        help="Optional human-readable reason persisted as skip_reason in meta.json",
    )

    # briefings command (issue #80): catch-up briefings for unseen videos
    briefings_parser = subparsers.add_parser(
        "briefings",
        help="Generate catch-up briefings for videos not yet surfaced in any _briefings/ guide",
    )
    briefings_parser.add_argument(
        "--unseen",
        action="store_true",
        help="Catch-up mode: surface corpus videos absent from every existing briefing's video_ids",
    )
    briefings_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the unseen set (count + titles) without writing a briefing",
    )
    briefings_parser.add_argument(
        "--since",
        help="Lower bound of the catch-up window ('Nd' or 'YYYY-MM-DD'). The default is "
        "unbounded (every never-briefed video); pass this to NARROW to a recency floor "
        "(e.g. --since 30d for just the last month).",
    )
    briefings_parser.add_argument(
        "--until",
        help="Upper bound of the catch-up window (absolute 'YYYY-MM-DD'). 'Nd' is accepted "
        "but means 'N days ago', so it is rarely what you want for an upper bound.",
    )
    briefings_parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Cap the briefing to the top-N most relevant unseen videos (default {DEFAULT_LIMIT}). "
        "0 = no cap. Uncapped videos stay unseen for the next run.",
    )
    briefings_parser.add_argument(
        "--pdf",
        action="store_true",
        help="Also write a clickable PDF (<date>-catch-up-unseen.pdf) beside the Markdown, "
        'rendered from the same ranked set. Requires the [pdf] extra (pip install -e ".[pdf]"). '
        "The Markdown is always written too - it remains the seen-coverage record.",
    )

    # profile command (issue #115): see / initialize the personalization surface
    profile_parser = subparsers.add_parser(
        "profile",
        help="Show or initialize the personalization profile that ranks briefings and the headline digest",
    )
    # metavar keeps the error on a bare `profile` readable ("required: {show,init}")
    # instead of leaking the argparse dest name.
    profile_actions = profile_parser.add_subparsers(dest="profile_action", required=True, metavar="{show,init}")
    profile_actions.add_parser(
        "show",
        help="Print the resolved interest model, its source, and both file paths (writes nothing)",
    )
    profile_actions.add_parser(
        "init",
        help=f"Persist the inferred _briefings/{PROFILE_FILENAME} and scaffold "
        f"_briefings/{AUDIENCE_FILENAME}. Never overwrites an existing file.",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    configured_level = getattr(logging, args.log_level.upper())
    log.setLevel(configured_level)
    # gemini_common carries the usage_metadata observability logs introduced in
    # PR #32 (feat(process)). Without this, log_usage_metadata's INFO lines are
    # filtered by the WARNING-level root configured above and users never see
    # the cached= / prompt= / total= token counts.
    logging.getLogger("gemini_common").setLevel(configured_level)
    config = load_config()

    if args.command == "scan":
        cmd_scan(args, config)
    elif args.command == "mindmap":
        cmd_mindmap(args, config)
    elif args.command == "transcript":
        cmd_transcript(args, config)
    elif args.command == "process":
        cmd_process(args, config)
    elif args.command == "concepts":
        cmd_concepts(args, config)
    elif args.command == "taxonomy-build":
        cmd_taxonomy_build(args, config)
    elif args.command == "search":
        cmd_search(args, config)
    elif args.command == "index":
        cmd_index(args, config)
    elif args.command == "nugget":
        cmd_nugget(args, config)
    elif args.command == "status":
        cmd_status(args, config)
    elif args.command == "repair-metas":
        cmd_repair_metas(args, config)
    elif args.command == "dedupe":
        cmd_dedupe(args, config)
    elif args.command == "prune-shorts":
        cmd_prune_shorts(args, config)
    elif args.command == "mark-skip":
        cmd_mark_skip(args, config)
    elif args.command == "briefings":
        cmd_briefings(args, config)
    elif args.command == "profile":
        cmd_profile(args, config)


if __name__ == "__main__":
    main()
