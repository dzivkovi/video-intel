"""Export a public subset of the corpus catalog behind a default-deny policy.

    python scripts/atlas_export.py --atlas <output_dir>/_atlas/atlas.json --policy <policy.yaml> [--out <dir>] [--deny-terms <file>]

Reads the private `atlas.json` written by `atlas_build.py` and a small YAML
policy, keeps only the rows the policy names (an explicit channel allowlist, a
date floor, public YouTube watch URLs), projects each kept row onto an explicit
PUBLIC field list, prunes the side tables to what the kept rows reference, and
writes a sibling bundle, `<out>/atlas.json` plus `<out>/catalog.html`, that
never holds an unfiltered byte. The default `--out` is `_atlas-export/` next to
the private `_atlas/`; writing into the private folder itself is refused.

Default deny, three times over: a channel not in the allowlist is absent, a
field not in `PUBLIC_ROW_KEYS` is absent, a concept label or channel count the
kept rows do not reference is absent. `--deny-terms` adds a byte scan over the
serialized subset (one term per line, the owner's private term list) that
aborts before any write when a term appears. Standard library only at import;
PyYAML loads inside `load_policy`, like `corpus_walk.parse_front_matter`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import atlas_render

GENERATOR_NAME = "atlas_export.py"
GENERATOR_VERSION = "1"
SCHEMA_VERSION = 1
SOURCE_GENERATOR = "atlas_build.py"
EXPORT_DIRNAME = "_atlas-export"
ATLAS_FILENAME = "atlas.json"
MAX_ATLAS_BYTES = 256 * 1024 * 1024
EXIT_REFUSED = 2
MIN_DENY_TERM_CHARS = 5

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
    "synopsis",
    "deep_links",
    "concept_ids",
)
#: The policy's whole vocabulary; an unknown key is a refusal, not a warning.
POLICY_KEYS = frozenset({"channels", "since", "exclude_video_ids"})
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
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


def _string_list(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not value:
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
        if not isinstance(since_text, str) or not _DATE_RE.match(since_text):
            raise PolicyError(f"policy `since` must be a YYYY-MM-DD date, got {since!r}")
        policy["since"] = since_text
    if data.get("exclude_video_ids") is not None:
        ids = _string_list(data["exclude_video_ids"], "exclude_video_ids")
        bad = [i for i in ids if not _VIDEO_ID_RE.match(i)]
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
    if not isinstance(vid, str) or not _VIDEO_ID_RE.match(vid) or not isinstance(url, str):
        return None
    return url if url == f"{_CANONICAL_URL}{vid}" else None


def _public_deep_links(links: object, url: str) -> list[dict]:
    """Only links that are the row's own canonical URL plus a `t=` seconds parameter."""
    out = []
    for dl in links if isinstance(links, list) else []:
        if not isinstance(dl, dict):
            continue
        lu, secs, label = dl.get("url"), dl.get("seconds"), dl.get("label")
        if isinstance(lu, str) and isinstance(secs, int) and isinstance(label, str) and lu == f"{url}&t={secs}":
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
        if not isinstance(row, dict) or row.get("channel") not in allowed:
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


def build_subset(atlas: dict, policy: dict, fingerprint: str) -> tuple[dict, dict[str, int]]:
    rows, dropped = select_rows(atlas.get("rows") or [], policy)
    referenced = {c for row in rows for c in row["concept_ids"]}
    concepts_in = atlas.get("concepts") if isinstance(atlas.get("concepts"), dict) else {}
    concepts = {
        cid: label for cid, label in sorted(concepts_in.items()) if cid in referenced and isinstance(label, str)
    }
    channels: dict[str, int] = {}
    for row in rows:
        channels[row["channel"]] = channels.get(row["channel"], 0) + 1
    subset = {
        "schema_version": SCHEMA_VERSION,
        "generator": GENERATOR_NAME,
        "generator_version": GENERATOR_VERSION,
        "generated_from": atlas.get("generated_from"),
        "generated_at": atlas.get("generated_at"),
        "export": {
            "policy_sha256_16": fingerprint,
            "channels": sorted(policy["channels"]),
            "since": policy["since"],
            "source_rows": len(atlas.get("rows") or []),
        },
        "summary": {"channels": dict(sorted(channels.items())), "topics": {}, "unthreaded": 0, "rows": len(rows)},
        "concepts": concepts,
        "rows": rows,
    }
    return subset, dropped


def serialize(subset: dict) -> bytes:
    return (json.dumps(subset, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Deny-term scan
# ---------------------------------------------------------------------------


def load_deny_terms(path: Path) -> list[str]:
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        term = line.strip()
        if term and not term.startswith("#") and len(term) >= MIN_DENY_TERM_CHARS:
            terms.append(term.lower())
    return terms


def deny_term_hits(payload: bytes, terms: list[str]) -> list[str]:
    """Terms found anywhere in `payload`, case-insensitive substring match over the text."""
    text = payload.decode("utf-8").lower()
    return [t for t in terms if t in text]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def check_output_dir(atlas_path: Path, out_dir: Path) -> None:
    """Refuse the private folder itself and any atlas.json another generator owns."""
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


def write_bundle(out_dir: Path, payload: bytes) -> tuple[Path, Path]:
    """Write atlas.json atomically, then render catalog.html; a failed render removes the bundle."""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / ATLAS_FILENAME
    tmp = out_dir / f"{ATLAS_FILENAME}.{os.getpid()}.tmp"
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    page = out_dir / atlas_render.CATALOG_FILENAME
    if atlas_render.main(["--atlas", str(target)]) != 0:
        target.unlink(missing_ok=True)
        page.unlink(missing_ok=True)
        raise SystemExit("rendering the subset failed; the half-written bundle was removed")
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
        "--deny-terms", type=Path, help="One term per line; the export aborts if any appears in the subset."
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
        terms = load_deny_terms(args.deny_terms) if args.deny_terms else []
    except (OSError, ValueError) as exc:  # PolicyError is a ValueError
        print(f"{GENERATOR_NAME}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    out_dir = args.out or args.atlas.resolve().parent.parent / EXPORT_DIRNAME
    subset, dropped = build_subset(atlas, policy, fingerprint)
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
    rows = len(subset["rows"])
    print(
        f"{GENERATOR_NAME}: wrote {target.name} and {page.name} to {out_dir.name} "
        f"(rows={rows} of {subset['export']['source_rows']} channels={len(subset['summary']['channels'])} "
        f"concepts={len(subset['concepts'])} dropped_channel={dropped['channel']} dropped_url={dropped['url']} "
        f"dropped_excluded={dropped['excluded']} dropped_date={dropped['date']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
