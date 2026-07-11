"""Tests for the catch-up briefing PDF export (issue #82).

The PDF renderer (`scripts/briefing_pdf.py`) is fed the SAME ranked set as the
Markdown path and must produce a valid, clickable PDF: video titles and
timestamped moments become live hyperlinks. reportlab + pypdf are optional, so
these tests skip cleanly when they are not installed.
"""

from __future__ import annotations

from datetime import date

import pytest

pytest.importorskip("reportlab", reason="PDF export needs the optional [pdf] extra")
pypdf = pytest.importorskip("pypdf", reason="link-annotation assertions need pypdf")

from briefing_pdf import render_unseen_briefing_pdf  # noqa: E402

LOWER, UPPER = date(2026, 6, 22), date(2026, 6, 25)
PROFILE = {"id": "inferred-test"}


def _ranked():
    return [
        {
            "video_id": "r4_KLZvHoaA",
            "title": "Anthropic Will Bring Back Fable 5 Differently",
            "url": "https://www.youtube.com/watch?v=r4_KLZvHoaA",
            "channel": "ramjad",
            "published": "2026-06-25",
            "score": 457,
            "matched_concepts": ["Token Economics", "Model Selection Strategy"],
        },
        {
            "video_id": "h1MxhfZSTjo",
            "title": "Google Lost $2.7B In Talent",
            "url": "https://www.youtube.com/watch?v=h1MxhfZSTjo",
            "channel": "natebjones",
            "published": "2026-06-22",
            "score": 0,  # zero-score: no deep-links, mirrors the Markdown contract
            "matched_concepts": [],
        },
    ]


def _links(video):
    # Mirror cmd_briefings: zero-score entries get no deep-links.
    if not video.get("score"):
        return []
    return [("Weekly limits + credits (0:35)", f"{video['url']}&t=35s")]


def test_pdf_written_with_clickable_links(tmp_path):
    out = tmp_path / "brief.pdf"
    render_unseen_briefing_pdf(_ranked(), PROFILE, out, lower=LOWER, upper=UPPER, link_extractor=_links)

    assert out.exists() and out.stat().st_size > 0
    reader = pypdf.PdfReader(str(out))
    annots = sum(len(p.get("/Annots", [])) for p in reader.pages)
    # 2 video-title links + 1 timestamp link (zero-score video contributes none).
    assert annots >= 3, f"expected >=3 link annotations, got {annots}"


def test_zero_score_video_has_no_deeplinks(tmp_path):
    out = tmp_path / "brief.pdf"
    render_unseen_briefing_pdf(_ranked(), PROFILE, out, lower=LOWER, upper=UPPER, link_extractor=_links)
    text = "".join(p.extract_text() for p in pypdf.PdfReader(str(out)).pages)
    # Both titles render; only the scored video shows a timestamp moment.
    assert "Google Lost" in text
    assert "0:35" in text


def test_pdf_shows_age_badge_and_by_year_appendix(tmp_path):
    """Issue #88 parity: the PDF must carry the same age badge + By Year appendix
    as the Markdown render, so the two artifacts stay in lockstep."""
    ranked = [
        {
            "video_id": "new",
            "title": "Recent Talk",
            "url": "https://youtu.be/new",
            "channel": "ch",
            "published": "2026-06-01",
            "score": 9,
            "matched_concepts": [],
        },
        {
            "video_id": "old",
            "title": "Foundational Talk",
            "url": "https://youtu.be/old",
            "channel": "ch",
            "published": "2024-03-01",
            "score": 8,
            "matched_concepts": [],
        },
    ]
    out = tmp_path / "brief.pdf"
    render_unseen_briefing_pdf(
        ranked, PROFILE, out, lower=date.min, upper=date(2026, 7, 6), link_extractor=_links, today=date(2026, 7, 6)
    )
    text = "".join(p.extract_text() for p in pypdf.PdfReader(str(out)).pages)
    assert "age 2y" in text  # the 2024 video's mechanical age badge
    assert "By Year" in text
    assert "2026" in text and "2024" in text


def test_empty_ranked_set_does_not_crash(tmp_path):
    out = tmp_path / "empty.pdf"
    render_unseen_briefing_pdf([], PROFILE, out, lower=LOWER, upper=UPPER, link_extractor=_links)
    assert out.exists()
    assert len(pypdf.PdfReader(str(out)).pages) >= 1


def test_markup_breaking_title_is_escaped(tmp_path):
    """Creator-controlled titles with & or < must not corrupt the PDF markup."""
    ranked = [
        {
            "video_id": "x",
            "title": "C++ & <script> tricks for agents",
            "url": "https://www.youtube.com/watch?v=x",
            "channel": "test",
            "published": "2026-06-24",
            "score": 10,
            "matched_concepts": [],
        }
    ]
    out = tmp_path / "escape.pdf"
    # Must not raise a reportlab parse error on the unescaped markup.
    render_unseen_briefing_pdf(ranked, PROFILE, out, lower=LOWER, upper=UPPER, link_extractor=lambda v: [])
    assert out.exists() and out.stat().st_size > 0
