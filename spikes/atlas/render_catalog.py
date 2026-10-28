"""Render a corpus catalog (atlas.json) into one self-contained HTML page.

Issue #261 bake-off candidate, not the chosen renderer. Standard library only.

    python spikes/atlas/render_catalog.py --atlas <output_dir>/_atlas/atlas.json [--out <file>]

The page embeds the whole catalog in a <script type="application/json"> block and
loads nothing from the network, so it opens from disk with file://.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

TEMPLATE_PATH = Path(__file__).resolve().with_name("catalog.html")
SENTINEL = "__ATLAS_DATA__"


def embed_json(atlas: dict) -> str:
    """JSON text that is safe inside a script element.

    Every `<`, `>` and `&` is unicode-escaped, not just `</`: a title holding
    `<!--` plus `<script` would otherwise switch the HTML tokenizer into the
    double-escaped script state and swallow the page's real closing tag (the
    same rule as scripts/lead_lag_viz.py::render_html).
    """
    text = json.dumps(atlas, ensure_ascii=False, separators=(",", ":"))
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def render(atlas: dict, template: str) -> str:
    if template.count(SENTINEL) != 1:
        raise ValueError(f"template must contain {SENTINEL} exactly once")
    head, tail = template.split(SENTINEL)
    # Concatenation, not str.replace on the result, so corpus text can never be re-scanned
    # for the sentinel.
    return head + embed_json(atlas) + tail


def load_atlas(path: Path) -> dict:
    atlas = json.loads(path.read_bytes().decode("utf-8"))
    if not isinstance(atlas, dict) or atlas.get("generator") != "atlas_build.py":
        raise ValueError(f"{path} is not an atlas.json written by atlas_build.py")
    if atlas.get("schema_version") != 1:
        raise ValueError(f"{path} has schema_version {atlas.get('schema_version')!r}, expected 1")
    if not isinstance(atlas.get("rows"), list):
        raise ValueError(f"{path} has no rows list")
    return atlas


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render atlas.json into a self-contained catalog.html.")
    parser.add_argument("--atlas", required=True, type=Path, help="Path to atlas.json.")
    parser.add_argument("--out", type=Path, help="Output HTML file. Default: catalog.html next to the atlas.")
    parser.add_argument("--template", type=Path, default=TEMPLATE_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    started = time.perf_counter()
    try:
        atlas = load_atlas(args.atlas)
        template = args.template.read_text(encoding="utf-8")
        html = render(atlas, template)
    except (OSError, ValueError) as exc:
        print(f"render_catalog: {exc}", file=sys.stderr)
        return 2
    out = args.out or args.atlas.with_name("catalog.html")
    write_atomic(out, html)
    elapsed = time.perf_counter() - started
    size = len(html.encode("utf-8"))
    print(f"render_catalog: wrote {out.name} ({size:,} bytes, {len(atlas['rows']):,} rows) in {elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
