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
  bare https:// URLs, and a leading `---`-fenced YAML front-matter block
  (stripped - it is machine metadata, not reader content).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ACCENT = "#c96442"  # matches briefing_pdf.py - the one color that signals "clickable"

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

    story = []
    for raw in md_text.split("\n"):
        line = raw.rstrip()
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
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=letter,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        leftMargin=0.55 * inch,
        rightMargin=0.55 * inch,
        title=title or Path(str(pdf_path)).stem,
        author="video-intel",
    )
    doc.build(story)


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
