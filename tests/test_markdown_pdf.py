"""Tests for the general Markdown -> clickable-PDF renderer (issue #84, Slice 1).

Companion to test_briefings_pdf.py. reportlab + pypdf are optional, so these
skip cleanly when the [pdf] extra is not installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("reportlab", reason="PDF export needs the optional [pdf] extra")
pypdf = pytest.importorskip("pypdf", reason="link-annotation assertions need pypdf")

from markdown_pdf import (  # noqa: E402
    _inline,
    render_markdown_file_to_pdf,
    render_markdown_to_pdf,
    strip_front_matter,
)

SAMPLE = """---
artifact_type: viewing_guide
video_ids:
  - abc
---

# Topic Briefing

**Saved:** 2026-07-10

## Top picks

- [Great Talk (12:34)](https://www.youtube.com/watch?v=abc&t=754) - watch this
- Plain bullet with a bare link https://example.com/x

### 2024

Some **bold** prose and a [second link](https://youtu.be/def).
"""


def test_strip_front_matter_removes_leading_yaml():
    assert strip_front_matter(SAMPLE).startswith("# Topic Briefing")
    # No front matter -> unchanged.
    assert strip_front_matter("# No FM\nbody") == "# No FM\nbody"


def test_inline_markdown_link_becomes_anchor_with_escaped_ampersand():
    out = _inline("[Great Talk (12:34)](https://www.youtube.com/watch?v=abc&t=754)")
    assert 'href="https://www.youtube.com/watch?v=abc&amp;t=754"' in out
    assert "Great Talk (12:34)" in out


def test_inline_bare_url_and_bold():
    assert '<a href="https://example.com/x"' in _inline("see https://example.com/x now")
    assert "<b>bold</b>" in _inline("some **bold** text")


def test_inline_bold_wrapped_link_stays_clickable():
    """Regression: `**[Title](url)**` (the curated-briefing header shape) must
    render as a bold AND clickable link, not flattened to literal bold text."""
    out = _inline("**[Sam - Must Haves](https://youtu.be/aIy85)** (2026-04-15)")
    assert '<b><a href="https://youtu.be/aIy85"' in out
    assert "Sam - Must Haves" in out
    assert "[Sam" not in out  # the literal bracket must be gone (link was parsed)


def test_inline_escapes_plain_text_specials():
    # A stray & / < in prose must be escaped so reportlab does not choke.
    out = _inline("A & B < C, no link here")
    assert "&amp;" in out and "&lt;" in out
    assert "<a " not in out  # nothing linkified


def test_render_writes_pdf_with_clickable_links(tmp_path):
    out = tmp_path / "brief.pdf"
    render_markdown_to_pdf(SAMPLE, out)
    assert out.exists() and out.stat().st_size > 0
    reader = pypdf.PdfReader(str(out))
    annots = sum(len(p.get("/Annots", [])) for p in reader.pages)
    # 3 links: the timestamp deep-link, the bare url, and the second link.
    assert annots >= 3, f"expected >=3 link annotations, got {annots}"
    text = "".join(p.extract_text() for p in reader.pages)
    assert "Topic Briefing" in text
    # Front matter must NOT render into the body.
    assert "artifact_type" not in text


def test_render_file_roundtrip(tmp_path):
    md = tmp_path / "in.md"
    md.write_text(SAMPLE, encoding="utf-8")
    out = tmp_path / "in.pdf"
    render_markdown_file_to_pdf(md, out)
    assert out.exists() and out.stat().st_size > 0


def test_markup_breaking_title_does_not_crash(tmp_path):
    out = tmp_path / "x.pdf"
    render_markdown_to_pdf("# C++ & <script> in a heading\n\nbody & more", out)
    assert out.exists() and out.stat().st_size > 0
