"""General Markdown -> clickable-PDF renderer for curated briefings (issue #84).

`briefing_pdf.py` renders the *structured* `ranked` dicts of a deterministic
`briefings --unseen` run. This module is its free-form sibling: it turns an
arbitrary **curated** Markdown briefing (the kind Claude authors in-session for
a topic - profile, top picks, pillar sections, why-it-matters lines, signal/
noise calls) into the SAME lean, tappable aesthetic: bold, one accent color,
and real hyperlinks (including `&t=<seconds>` deep-links that open YouTube at
the exact moment).

Design mirrors `briefing_pdf.py` deliberately: reportlab is an OPTIONAL
dependency (`pip install -e ".[pdf]"`), imported only when this module is used,
and the same accent (`#c96442`) so every briefing PDF reads as one family.

CLI:  python scripts/markdown_pdf.py INPUT.md OUTPUT.pdf
API:  render_markdown_file_to_pdf(md_path, pdf_path)
      render_markdown_to_pdf(md_text, pdf_path, title=...)

Supported Markdown (briefing subset, not a full CommonMark parser):
  #/##/### headings, --- rules, - and 1. bullets, **bold**, [text](url) links,
  bare https:// URLs, GFM pipe tables (cells keep clickable links; the
  `|---|` separator row marks the shaded header), and a leading `---`-fenced
  YAML front-matter block (stripped - it is machine metadata, not reader
  content).
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

ACCENT = "#c96442"  # matches briefing_pdf.py - the one color that signals "clickable"

_MARGIN_LR_INCH = 0.55  # shared by the doc template and the table width budget

# A GFM table row: the line starts and ends with a pipe. The separator row
# (|---|:---:|) is detected cell-wise so it can mark the header instead of
# rendering as content.
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")

# One pass tokenizes a line into markdown-link / bold / bare-URL / plain runs.
# Plain runs are XML-escaped; URLs are escaped inside href too (reportlab
# unescapes &amp; back to & when it follows the link), matching briefing_pdf._link.
_INLINE = re.compile(
    r"\[(?P<ltext>[^\]]+)\]\((?P<lurl>[^)]+)\)"  # [text](url)
    r"|\*\*(?P<bold>[^*]+)\*\*"  # **bold**
    r"|(?P<url>https?://[^\s)]+)"  # bare url
)


def _esc(text: str) -> str:
    """Escape for reportlab's mini-markup parser (content is creator-controlled)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(url: str, label: str) -> str:
    return f'<a href="{_esc(url)}" color="{ACCENT}"><u>{_esc(label)}</u></a>'


def _inline(text: str) -> str:
    """Convert a single line's inline Markdown to reportlab markup, escaping the rest."""
    parts: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        if m.start() > pos:
            parts.append(_esc(text[pos : m.start()]))
        if m.group("ltext") is not None:
            parts.append(_link(m.group("lurl"), m.group("ltext")))
        elif m.group("bold") is not None:
            # Recurse so a bold-wrapped link (**[Title](url)**, the header shape
            # curated briefings use) stays clickable, not flattened to literal text.
            parts.append(f"<b>{_inline(m.group('bold'))}</b>")
        else:  # bare url
            parts.append(_link(m.group("url"), m.group("url")))
        pos = m.end()
    if pos < len(text):
        parts.append(_esc(text[pos:]))
    return "".join(parts)


def strip_front_matter(text: str) -> str:
    """Drop a leading `---`-fenced YAML block (machine metadata, not content)."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_flowable(lines: list[str], cell_style, head_style):
    """Turn buffered pipe-table lines into a reportlab Table with clickable cells."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    rows: list[list[str]] = []
    has_header = False
    for i, line in enumerate(lines):
        cells = _split_table_row(line)
        if cells and all(_TABLE_SEP_CELL.match(c) for c in cells):
            if i == 1:
                has_header = True
            continue
        rows.append(cells)
    if not rows:
        return None

    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    # Column widths proportional to the longest cell text, floored so a short
    # column (a date, a count) never collapses to nothing.
    frame_width = letter[0] - 2 * _MARGIN_LR_INCH * inch
    weights = [max(max(len(row[c]) for row in rows), 8) for c in range(ncols)]
    floored = [max(w / sum(weights), 0.12) for w in weights]
    col_widths = [frame_width * w / sum(floored) for w in floored]

    data = [
        [Paragraph(_inline(text), head_style if (has_header and r == 0) else cell_style) for text in row]
        for r, row in enumerate(rows)
    ]
    table = Table(data, colWidths=col_widths, repeatRows=1 if has_header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#c8c8c8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if has_header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")))
    table.setStyle(TableStyle(style))
    return table


def _build_story(md_text: str):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import HRFlowable, Paragraph, Spacer

    base = getSampleStyleSheet()
    accent = colors.HexColor(ACCENT)
    body = ParagraphStyle("md_body", parent=base["Normal"], fontSize=11, leading=15, spaceAfter=8)
    bullet = ParagraphStyle("md_bullet", parent=body, leftIndent=14, spaceAfter=7)
    h1 = ParagraphStyle("md_h1", parent=base["Title"], fontSize=18, leading=22, spaceBefore=6, spaceAfter=12)
    h2 = ParagraphStyle(
        "md_h2", parent=base["Heading2"], fontSize=14, leading=18, spaceBefore=14, spaceAfter=8, textColor=accent
    )
    h3 = ParagraphStyle("md_h3", parent=base["Heading3"], fontSize=12, leading=16, spaceBefore=10, spaceAfter=5)
    meta = ParagraphStyle("md_meta", parent=body, fontSize=9.5, textColor=colors.HexColor("#555555"), spaceAfter=6)
    cell = ParagraphStyle("md_cell", parent=body, fontSize=9.5, leading=13, spaceAfter=0)
    cell_head = ParagraphStyle("md_cell_head", parent=cell, fontName="Helvetica-Bold")

    story = []
    table_buf: list[str] = []

    def flush_table() -> None:
        if not table_buf:
            return
        table = _table_flowable(table_buf, cell, cell_head)
        table_buf.clear()
        if table is not None:
            story.append(Spacer(1, 4))
            story.append(table)
            story.append(Spacer(1, 8))

    for raw in md_text.split("\n"):
        line = raw.rstrip()
        if _TABLE_ROW.match(line):
            table_buf.append(line)
            continue
        flush_table()
        if not line.strip():
            continue
        if line.startswith("### "):
            story.append(Paragraph(_inline(line[4:]), h3))
        elif line.startswith("## "):
            story.append(Paragraph(_inline(line[3:]), h2))
        elif line.startswith("# "):
            story.append(Paragraph(_inline(line[2:]), h1))
        elif line.strip() == "---":
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", color=colors.HexColor("#cccccc"), thickness=0.6))
            story.append(Spacer(1, 8))
        elif re.match(r"^\d+\.\s", line) or line.startswith("- "):
            story.append(Paragraph(_inline(re.sub(r"^(\d+\.\s|-\s)", "", line)), bullet))
        elif line.startswith("**") and line.rstrip().endswith("**") and line.count("**") == 2:
            story.append(Paragraph(_inline(line), meta))
        else:
            story.append(Paragraph(_inline(line), body))
    flush_table()
    return story


def render_markdown_to_pdf(md_text: str, pdf_path, *, title: str | None = None) -> None:
    """Render curated Markdown text to a clickable PDF at ``pdf_path``.

    Raises ImportError (with the same install hint as briefing_pdf) if the
    optional ``[pdf]`` extra is missing.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate

    story = _build_story(strip_front_matter(md_text))
    # Build to a temp sibling, then replace: a PDF viewer or a cloud-sync scan
    # holding the target open on Windows would otherwise fail the whole render
    # (observed 2026-08-02 overwriting an existing briefing PDF on the mount).
    tmp_path = Path(str(pdf_path) + ".tmp")
    doc = SimpleDocTemplate(
        str(tmp_path),
        pagesize=letter,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=_MARGIN_LR_INCH * inch,
        rightMargin=_MARGIN_LR_INCH * inch,
        title=title or Path(str(pdf_path)).stem,
        author="video-intel",
    )
    doc.build(story)
    for delay in (0.0, 0.5, 1.0, 2.0):
        time.sleep(delay)
        try:
            os.replace(tmp_path, pdf_path)
            return
        except PermissionError:
            continue
    tmp_path.unlink(missing_ok=True)
    raise PermissionError(f"Cannot overwrite {pdf_path}: the file is locked, likely open in a PDF viewer.")


def render_markdown_file_to_pdf(md_path, pdf_path) -> None:
    """Render a Markdown file to a clickable PDF beside it (or at ``pdf_path``)."""
    text = Path(md_path).read_text(encoding="utf-8")
    render_markdown_to_pdf(text, pdf_path, title=Path(str(md_path)).stem)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python scripts/markdown_pdf.py INPUT.md OUTPUT.pdf", file=sys.stderr)
        return 2
    try:
        render_markdown_file_to_pdf(argv[0], argv[1])
    except ImportError:
        print(
            'PDF export needs reportlab. Install the optional extra: pip install -e ".[pdf]" '
            "(or pip install reportlab).",
            file=sys.stderr,
        )
        return 1
    print(f"Wrote {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
