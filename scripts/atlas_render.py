"""Render the corpus catalog (`_atlas/atlas.json`) into one self-contained HTML page.

    python scripts/atlas_render.py --atlas <output_dir>/_atlas/atlas.json [--out <file>]

The page (`catalog.html` next to the atlas by default) embeds the whole catalog in
one `<script type="application/json">` block and loads no script, style or font
from the network, so it opens from disk with `file://` and keeps working when
copied anywhere. The one outbound request the page makes is the YouTube
thumbnail image per card, built in the page from each row's own stored watch
URL, lazily, with no referrer, and switchable off with a button.

Standard library only, under the same import firebreak as `atlas_build.py`
(issue #152): nothing here imports the curate stack. The renderer reads the
atlas file and the template beside this script, never the corpus.

Ownership follows `atlas_build.check_ownership` and `wiki_concepts.write_pages`:
an existing output file is replaced only when it carries this renderer's
`<meta name="generator">` marker; anything else is refused before any write.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

GENERATOR_NAME = "atlas_render.py"
ATLAS_GENERATOR_NAME = "atlas_build.py"
ATLAS_SCHEMA_VERSION = 1
CATALOG_FILENAME = "catalog.html"
TEMPLATE_PATH = Path(__file__).resolve().with_name("atlas_catalog_template.html")
#: The template holds this once; the data block is joined in by splitting on it.
SENTINEL = "__ATLAS_DATA__"
#: The ownership marker a page written by this renderer carries in its <head>.
GENERATOR_META = f'<meta name="generator" content="{GENERATOR_NAME}">'
#: How much of an existing output file is read to look for the marker.
OWNERSHIP_PROBE_BYTES = 16 * 1024
EXIT_REFUSED = 2


def embed_json(atlas: dict) -> str:
    """JSON text that is safe inside a script element.

    Every `<`, `>` and `&` is unicode-escaped, not just `</`: a title holding
    `<!--` plus `<script` would otherwise switch the HTML tokenizer into the
    double-escaped script state and swallow the page's real closing tag (the
    same rule as `lead_lag_viz.render_html`). JSON parses the escapes back to
    the original characters, so the page shows titles verbatim.

    The three-replace chain is deliberately the same text as in `lead_lag_viz`
    rather than an import: that script sits on the DuckDB report stack and this
    one must stay stdlib-only. `tests/test_atlas_render.py::TestEscapeChainParity`
    pins the two copies to each other.
    """
    text = json.dumps(atlas, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def render(atlas: dict, template: str) -> str:
    """Join the escaped atlas into the template at its single sentinel."""
    if template.count(SENTINEL) != 1:
        raise ValueError(f"template must contain {SENTINEL} exactly once")
    if GENERATOR_META not in template:
        raise ValueError(f"template must carry the ownership marker {GENERATOR_META}")
    head, tail = template.split(SENTINEL)
    # Concatenation, never str.replace on the result, so corpus text is never
    # re-scanned for the sentinel.
    return head + embed_json(atlas) + tail


def load_atlas(path: Path) -> dict:
    """Parse `path` and require it to be an atlas.json written by atlas_build.py."""
    atlas = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(atlas, dict) or atlas.get("generator") != ATLAS_GENERATOR_NAME:
        raise ValueError(f"{path} is not an atlas.json written by {ATLAS_GENERATOR_NAME}")
    if atlas.get("schema_version") != ATLAS_SCHEMA_VERSION:
        raise ValueError(f"{path} has schema_version {atlas.get('schema_version')!r}, expected {ATLAS_SCHEMA_VERSION}")
    if not isinstance(atlas.get("rows"), list):
        raise ValueError(f"{path} has no rows list")
    return atlas


def check_ownership(path: Path) -> None:
    """Refuse (SystemExit) unless `path` is absent or carries this renderer's marker.

    Fail-closed: an unreadable file and a file without the marker are both
    refused, never overwritten. Only the first `OWNERSHIP_PROBE_BYTES` are
    read; the marker sits in the template's <head>, well inside that.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(OWNERSHIP_PROBE_BYTES)
    except FileNotFoundError:
        return  # absent: nothing to own
    except OSError as exc:
        raise SystemExit(
            f"refusing to overwrite {path}: it cannot be read ({exc}). Move it aside, or pass --out."
        ) from exc
    if GENERATOR_META.encode("utf-8") in head:
        return
    raise SystemExit(
        f"refusing to overwrite {path}: it was not written by {GENERATOR_NAME} (no generator marker in its head). "
        "Move the file aside, or point --out at a file of your own."
    )


def write_atomic(path: Path, text: str) -> None:
    """Write `text` through `<name>.<pid>.tmp` and one `os.replace`; the temp never survives."""
    check_ownership(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render _atlas/atlas.json into one self-contained catalog.html.")
    parser.add_argument("--atlas", required=True, type=Path, help="Path to atlas.json written by atlas_build.py.")
    parser.add_argument("--out", type=Path, help=f"Output HTML file. Default: {CATALOG_FILENAME} next to the atlas.")
    parser.add_argument("--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = time.perf_counter()
    try:
        atlas = load_atlas(args.atlas)
        template = args.template.read_text(encoding="utf-8")
        html = render(atlas, template)
    except (OSError, ValueError) as exc:
        print(f"{GENERATOR_NAME}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    out = args.out or args.atlas.with_name(CATALOG_FILENAME)
    try:
        write_atomic(out, html)
    except (SystemExit, OSError) as exc:
        # A refused ownership check (SystemExit) and a failed write (a read-only folder, a refused
        # os.replace) report the same way: one stderr line, exit 2, never a traceback.
        print(f"{GENERATOR_NAME}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    elapsed = time.perf_counter() - started
    size = len(html.encode("utf-8"))
    print(f"{GENERATOR_NAME}: wrote {out.name} ({size:,} bytes, {len(atlas['rows']):,} rows) in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
