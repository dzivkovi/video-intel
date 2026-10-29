"""Export a public subset of the corpus catalog behind a default-deny policy.

    python scripts/atlas_export.py --atlas <output_dir>/_atlas/atlas.json --policy <policy.yaml> [--out <dir>] [--deny-terms <file>] [--no-verify-public]

Reads the private `atlas.json` written by `atlas_build.py` and a small YAML
policy, keeps only the rows the policy names (an explicit channel allowlist, a
date floor, canonical YouTube watch URLs), confirms with the YouTube Data API
that every kept video is PUBLIC (an unlisted video's link is a disclosure the
creator did not make), projects each kept row onto an explicit PUBLIC field
list, prunes the side tables to what the kept rows reference, and writes a
sibling bundle, `<out>/atlas.json` plus `<out>/catalog.html`, that never holds
an unfiltered byte. The default `--out` is `_atlas-export/` next to the private
`_atlas/`; writing into the private folder itself is refused.

Default deny, four times over: a channel not in the allowlist is absent, a
video the API does not report as public is absent, a field not in
`PUBLIC_ROW_KEYS` is absent, a concept label or channel count the kept rows do
not reference is absent. `--deny-terms` adds a byte scan over the serialized
subset (one term per line, the owner's private term list) that aborts before
any write when a term appears. Standard library only at import; PyYAML loads
inside `load_policy`, like `corpus_walk.parse_front_matter`; the API call uses
`urllib` inside `fetch_privacy_status`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

import atlas_render

GENERATOR_NAME = "atlas_export.py"
GENERATOR_VERSION = "2"
SCHEMA_VERSION = 1
SOURCE_GENERATOR = "atlas_build.py"
EXPORT_DIRNAME = "_atlas-export"
ATLAS_FILENAME = "atlas.json"
MAX_ATLAS_BYTES = 256 * 1024 * 1024
EXIT_REFUSED = 2
MIN_DENY_TERM_CHARS = 5
YOUTUBE_API_KEY_ENV = "YOUTUBE_API_KEY"
VIDEOS_STATUS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
STATUS_BATCH = 50
STATUS_TIMEOUT_SECONDS = 30

#: Everything a public row may carry. A key not listed here never leaves the machine,
#: whatever the private row holds (topics, briefing membership, wiki keys, aliases,
#: the folders a duplicate lived in).
PUBLIC_ROW_KEYS = (
    "video_id",
    "title",
    "channel",
    "published",
    "duration_seconds",
    "url",
    "transcript_quality",
    "transcript_flag_count",
    "transcript_source",
    "synopsis",
    "deep_links",
    "concept_ids",
)
#: The policy's whole vocabulary; an unknown key is a refusal, not a warning.
POLICY_KEYS = frozenset({"channels", "since", "exclude_video_ids"})
_DATE_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])")
#: The id shape `timestamp_utils.is_youtube_deep_linkable` checks; kept local so this
#: script stays a two-import module, and pinned to that rule by its tests.
_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_CANONICAL_URL = "https://www.youtube.com/watch?v="


class PolicyError(ValueError):
    """A policy file that cannot be trusted to mean what it says."""


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _strict_yaml_load(text: str) -> object:
    """PyYAML safe load that rejects duplicate mapping keys and non-string keys."""
    import yaml

    # A SafeLoader subclass: it constructs only plain data, never tagged Python objects.
    # The override below only adds the duplicate-key and string-key refusals.
    class StrictLoader(yaml.SafeLoader):
        pass

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise PolicyError(f"policy keys must be strings, got {key!r}")
            if key in seen:
                raise PolicyError(f"duplicate policy key: {key!r}")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)
    try:
        return yaml.load(text, Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy is not valid YAML: {exc}") from exc


def _string_list(value: object, key: str, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PolicyError(f"policy `{key}` must be a non-empty list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"policy `{key}` entries must be non-empty strings, got {item!r}")
        out.append(item.strip())
    if len(set(out)) != len(out):
        raise PolicyError(f"policy `{key}` repeats an entry")
    return out


def load_policy(path: Path) -> dict:
    """Parse and validate the policy. Every check is a refusal (PolicyError), never a default."""
    data = _strict_yaml_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PolicyError("policy must be a mapping")
    unknown = sorted(set(data) - POLICY_KEYS)
    if unknown:
        raise PolicyError(f"policy has unknown keys: {unknown}; allowed: {sorted(POLICY_KEYS)}")
    policy = {"channels": _string_list(data.get("channels"), "channels"), "since": None, "exclude_video_ids": []}
    since = data.get("since")
    if since is not None:
        since_text = since.isoformat() if hasattr(since, "isoformat") else since
        if not isinstance(since_text, str) or not _DATE_RE.fullmatch(since_text):
            raise PolicyError(f"policy `since` must be a YYYY-MM-DD date, got {since!r}")
        try:
            dt.date.fromisoformat(since_text)
        except ValueError as exc:
            raise PolicyError(f"policy `since` is not a calendar date: {since_text}") from exc
        policy["since"] = since_text
    if data.get("exclude_video_ids") is not None:
        ids = _string_list(data["exclude_video_ids"], "exclude_video_ids", allow_empty=True)
        bad = [i for i in ids if not _VIDEO_ID_RE.fullmatch(i)]
        if bad:
            raise PolicyError(f"policy `exclude_video_ids` holds non-id entries: {bad}")
        policy["exclude_video_ids"] = ids
    return policy


def policy_fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def canonical_url(row: dict) -> str | None:
    """The row's URL when it is exactly the canonical watch URL of its own id, else None."""
    vid, url = row.get("video_id"), row.get("url")
    if not isinstance(vid, str) or not _VIDEO_ID_RE.fullmatch(vid) or not isinstance(url, str):
        return None
    return url if url == f"{_CANONICAL_URL}{vid}" else None


def _public_deep_links(links: object, url: str) -> list[dict]:
    """Only links that are the row's own canonical URL plus a `t=` seconds parameter."""
    out = []
    for dl in links if isinstance(links, list) else []:
        if not isinstance(dl, dict):
            continue
        lu, secs, label = dl.get("url"), dl.get("seconds"), dl.get("label")
        if (
            isinstance(lu, str)
            and isinstance(secs, int)
            and not isinstance(secs, bool)
            and isinstance(label, str)
            and lu == f"{url}&t={secs}"
        ):
            out.append({"label": label, "seconds": secs, "url": lu})
    return out


def select_rows(rows: list[dict], policy: dict) -> tuple[list[dict], dict[str, int]]:
    """Apply the policy as one projection; return (public rows, drop counts by reason)."""
    allowed = set(policy["channels"])
    excluded = set(policy["exclude_video_ids"])
    since = policy["since"]
    kept: list[dict] = []
    dropped = {"channel": 0, "date": 0, "url": 0, "excluded": 0}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("channel"), str) or row["channel"] not in allowed:
            dropped["channel"] += 1
            continue
        url = canonical_url(row)
        if url is None:
            dropped["url"] += 1
            continue
        if row["video_id"] in excluded:
            dropped["excluded"] += 1
            continue
        published = row.get("published")
        if since is not None and (not isinstance(published, str) or published < since):
            dropped["date"] += 1
            continue
        public = {key: row.get(key) for key in PUBLIC_ROW_KEYS}
        public["deep_links"] = _public_deep_links(row.get("deep_links"), url)
        public["synopsis"] = [s for s in (row.get("synopsis") or []) if isinstance(s, str)]
        public["concept_ids"] = sorted({c for c in (row.get("concept_ids") or []) if isinstance(c, str)})
        kept.append(public)
    return kept, dropped


def unmatched_channels(policy: dict, rows: list[dict]) -> list[str]:
    """Policy channels no kept row carries: a typo in the allowlist must not export silently."""
    present = {row["channel"] for row in rows}
    return sorted(set(policy["channels"]) - present)


# ---------------------------------------------------------------------------
# Visibility gate
# ---------------------------------------------------------------------------


def _fetch_status_batch(url: str) -> dict:
    """One GET against the videos endpoint; separated so tests can replace the network."""
    with urllib.request.urlopen(url, timeout=STATUS_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_privacy_status(video_ids: list[str], api_key: str) -> dict[str, str]:
    """`{video_id: privacyStatus}` for every id the API returns; an id it omits is absent."""
    statuses: dict[str, str] = {}
    for start in range(0, len(video_ids), STATUS_BATCH):
        batch = video_ids[start : start + STATUS_BATCH]
        query = urllib.parse.urlencode(
            {"part": "status", "id": ",".join(batch), "key": api_key, "maxResults": STATUS_BATCH}
        )
        data = _fetch_status_batch(f"{VIDEOS_STATUS_ENDPOINT}?{query}")
        for item in data.get("items", []) if isinstance(data, dict) else []:
            vid = item.get("id") if isinstance(item, dict) else None
            status = (item.get("status") or {}).get("privacyStatus") if isinstance(item, dict) else None
            if isinstance(vid, str) and isinstance(status, str):
                statuses[vid] = status
    return statuses


def apply_visibility(rows: list[dict], statuses: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Keep rows whose id the API reports as `public`; return (kept, dropped ids)."""
    kept, dropped = [], []
    for row in rows:
        if statuses.get(row["video_id"]) == "public":
            kept.append(row)
        else:
            dropped.append(row["video_id"])
    return kept, dropped


# ---------------------------------------------------------------------------
# Subset document
# ---------------------------------------------------------------------------


def build_subset(atlas: dict, policy: dict, fingerprint: str, rows: list[dict]) -> dict:
    """The public document around already-selected, already-verified `rows`."""
    referenced = {c for row in rows for c in row["concept_ids"]}
    concepts_in = atlas.get("concepts") if isinstance(atlas.get("concepts"), dict) else {}
    concepts = {
        cid: label for cid, label in sorted(concepts_in.items()) if cid in referenced and isinstance(label, str)
    }
    channels: dict[str, int] = {}
    for row in rows:
        channels[row["channel"]] = channels.get(row["channel"], 0) + 1
    # Provenance is derived from the kept rows alone: the private corpus name and its
    # build stamp never leave the machine. `generated_at` is the newest kept publish date.
    newest = max((row["published"] for row in rows if isinstance(row.get("published"), str)), default=None)
    return {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "generated_from": "public subset",
        "generated_at": newest,
        "export": {"policy_sha256_16": fingerprint, "channels": sorted(policy["channels"]), "since": policy["since"]},
        "summary": {"channels": dict(sorted(channels.items())), "topics": {}, "unthreaded": 0, "rows": len(rows)},
        "concepts": concepts,
        "rows": rows,
    }


def serialize(subset: dict) -> bytes:
    return (json.dumps(subset, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Deny-term scan
# ---------------------------------------------------------------------------


def _fold(text: str) -> str:
    """One comparison form for terms and payload: NFC, then full case folding."""
    return unicodedata.normalize("NFC", text).casefold()


def load_deny_terms(path: Path) -> tuple[list[str], int]:
    """(folded terms, count of non-comment lines ignored for being under the length floor)."""
    terms, ignored = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        if len(term) < MIN_DENY_TERM_CHARS:
            ignored += 1
            continue
        terms.append(_fold(term))
    return terms, ignored


def deny_term_hits(payload: bytes, terms: list[str]) -> list[str]:
    """Terms found anywhere in `payload`: substring match after NFC and case folding on both sides."""
    text = _fold(payload.decode("utf-8"))
    return [t for t in terms if t in text]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def check_output_dir(atlas_path: Path, out_dir: Path) -> None:
    """Refuse the private folder itself and any atlas.json or catalog.html another tool owns."""
    if out_dir.resolve() == atlas_path.resolve().parent:
        raise SystemExit(f"refusing to export into the private catalog folder {out_dir}; pass --out elsewhere")
    existing = out_dir / ATLAS_FILENAME
    if existing.exists():
        try:
            head = json.loads(existing.read_bytes()[:MAX_ATLAS_BYTES].decode("utf-8"))
            owner = head.get("generator") if isinstance(head, dict) else None
        except (ValueError, OSError):
            owner = None
        if owner != GENERATOR_NAME:
            raise SystemExit(
                f"refusing to overwrite {existing}: not written by {GENERATOR_NAME} (generator {owner!r}). Move it aside."
            )
    atlas_render.check_ownership(out_dir / atlas_render.CATALOG_FILENAME)


def write_bundle(out_dir: Path, payload: bytes) -> tuple[Path, Path]:
    """Stage atlas.json and catalog.html beside the destination, then swap both in.

    The page is rendered from the staged JSON before either destination is touched,
    so a failed render leaves whatever was there before (a previous bundle, or
    nothing) exactly as it was; the two swaps happen last, in sequence.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ATLAS_FILENAME
    page = out_dir / atlas_render.CATALOG_FILENAME
    pid = os.getpid()
    tmp_json = out_dir / f"{ATLAS_FILENAME}.{pid}.tmp"
    tmp_page = out_dir / f"{atlas_render.CATALOG_FILENAME}.{pid}.tmp"
    try:
        tmp_json.write_bytes(payload)
        if atlas_render.main(["--atlas", str(tmp_json), "--out", str(tmp_page)]) != 0:
            raise SystemExit("rendering the subset failed; nothing in the output folder was changed")
        os.replace(tmp_json, target)
        os.replace(tmp_page, page)
    finally:
        tmp_json.unlink(missing_ok=True)
        tmp_page.unlink(missing_ok=True)
    return target, page


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a public subset of _atlas/atlas.json behind a policy.")
    parser.add_argument("--atlas", required=True, type=Path, help="The private atlas.json written by atlas_build.py.")
    parser.add_argument(
        "--policy", required=True, type=Path, help="YAML policy: channels (required), since, exclude_video_ids."
    )
    parser.add_argument(
        "--out", type=Path, help=f"Bundle folder. Default: {EXPORT_DIRNAME}/ next to the private _atlas/."
    )
    parser.add_argument(
        "--deny-terms",
        required=True,
        type=Path,
        help="One term per line (an empty file means you have none); the export aborts if any appears in the subset.",
    )
    parser.add_argument(
        "--no-verify-public",
        action="store_true",
        help=f"Skip the YouTube visibility check (needs {YOUTUBE_API_KEY_ENV}); unlisted videos then leak.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        atlas = atlas_render.load_atlas(args.atlas)
        if atlas.get("generator") != SOURCE_GENERATOR:
            raise ValueError(f"{args.atlas} is not a private atlas written by {SOURCE_GENERATOR}")
        policy = load_policy(args.policy)
        fingerprint = policy_fingerprint(args.policy)
        terms, ignored = load_deny_terms(args.deny_terms)
    except (OSError, ValueError) as exc:  # PolicyError is a ValueError
        print(f"{GENERATOR_NAME}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if ignored:
        print(
            f"{GENERATOR_NAME}: {ignored} deny term(s) under {MIN_DENY_TERM_CHARS} characters ignored", file=sys.stderr
        )
    api_key = os.environ.get(YOUTUBE_API_KEY_ENV, "")
    if not args.no_verify_public and not api_key:
        print(
            f"{GENERATOR_NAME}: refusing to export without the visibility check: set {YOUTUBE_API_KEY_ENV}, "
            "or pass --no-verify-public to accept that unlisted videos would be published",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    out_dir = args.out or args.atlas.resolve().parent.parent / EXPORT_DIRNAME
    rows, dropped = select_rows(atlas.get("rows") or [], policy)
    dropped_visibility: list[str] = []
    if args.no_verify_public:
        print(f"{GENERATOR_NAME}: WARNING visibility check skipped; unlisted videos are not filtered", file=sys.stderr)
    else:
        try:
            statuses = fetch_privacy_status([row["video_id"] for row in rows], api_key)
        except (OSError, ValueError) as exc:
            print(f"{GENERATOR_NAME}: visibility check failed, nothing written: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        rows, dropped_visibility = apply_visibility(rows, statuses)
        for vid in dropped_visibility:
            print(f"{GENERATOR_NAME}: not public, dropped: {vid}", file=sys.stderr)
    # After every drop: a listed channel with nothing left to export is a policy mistake, not an empty page.
    missing = unmatched_channels(policy, rows)
    if missing:
        print(
            f"{GENERATOR_NAME}: refusing to export: policy channels with no exportable video: {missing}",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    subset = build_subset(atlas, policy, fingerprint, rows)
    payload = serialize(subset)
    hits = deny_term_hits(payload, terms)
    if hits:
        print(
            f"{GENERATOR_NAME}: refusing to export: {len(hits)} deny term(s) found in the subset; nothing written",
            file=sys.stderr,
        )
        for term in hits:
            print(f"{GENERATOR_NAME}: deny term present: {term}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        check_output_dir(args.atlas, out_dir)
        target, page = write_bundle(out_dir, payload)
    except (SystemExit, OSError) as exc:
        print(f"{GENERATOR_NAME}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(
        f"{GENERATOR_NAME}: wrote {target.name} and {page.name} to {out_dir.name} "
        f"(rows={len(rows)} channels={len(subset['summary']['channels'])} concepts={len(subset['concepts'])} "
        f"dropped_channel={dropped['channel']} dropped_url={dropped['url']} dropped_excluded={dropped['excluded']} "
        f"dropped_date={dropped['date']} dropped_visibility={len(dropped_visibility)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
