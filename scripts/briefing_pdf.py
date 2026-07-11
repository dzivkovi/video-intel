"""Self-contained PDF renderer for catch-up briefings (issue #82).

A clickable, one-page-friendly PDF rendered from the SAME ranked set the
Markdown briefing uses (``render_unseen_briefing`` in ``video_intel.py``).
The design is deliberately lean - the only adornments are the three that
create a tappable call-to-action: **bold**, an accent color, and a real
hyperlink. No cover page, no recap section, no tables, no "featured" slot:
the top-ranked item simply renders first.

reportlab is an OPTIONAL dependency (``pip install -e ".[pdf]"``). Import
this module lazily so the Markdown path stays dependency-light; callers
catch ImportError and print an actionable install hint.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate

ACCENT = "#c96442"  # the one color that signals "clickable"
_INK = HexColor("#1a1a1a")
_BODY = HexColor("#333333")
_GRAY = HexColor("#777777")
_RULE = HexColor("#e2e2e2")

_TITLE = ParagraphStyle("bp_title", fontName="Helvetica-Bold", fontSize=19, leading=24, textColor=_INK, spaceAfter=3)
_SUB = ParagraphStyle("bp_sub", fontName="Helvetica", fontSize=10, leading=14, textColor=_GRAY, spaceAfter=14)
_VTITLE = ParagraphStyle(
    "bp_vtitle", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=_INK, spaceBefore=11, spaceAfter=1
)
_META = ParagraphStyle("bp_meta", fontName="Helvetica", fontSize=9, leading=12, textColor=_GRAY, spaceAfter=3)
_WHY = ParagraphStyle("bp_why", fontName="Helvetica", fontSize=11, leading=15.5, textColor=_BODY, spaceAfter=4)
_JUMP = ParagraphStyle("bp_jump", fontName="Helvetica", fontSize=10.5, leading=16, textColor=_BODY, spaceAfter=2)
_YEAR = ParagraphStyle(
    "bp_year", fontName="Helvetica-Bold", fontSize=11, leading=15, textColor=_GRAY, spaceBefore=10, spaceAfter=3
)


def _esc(text: object) -> str:
    """Escape text for reportlab's mini-markup parser (titles are creator-controlled)."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# Age formatting is intentionally re-implemented here rather than imported from
# video_intel (this module stays import-free of the main script, like _esc/_link
# above). It must stay behaviourally identical to video_intel._format_age.
def _parse_date(value: object) -> date | None:
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except (TypeError, ValueError):
        return None


def _age(published: date | None, today: date) -> str:
    """y/mo/d age badge; "" for missing or future dates. Mirror of _format_age."""
    if published is None or published > today:
        return ""
    days = (today - published).days
    if days >= 365:
        return f"{days // 365}y"
    if days >= 30:
        return f"{days // 30}mo"
    return f"{days}d"


def _link(url: str, label: str) -> str:
    """Bold + accent-colored hyperlink - the tappable call-to-action."""
    return f'<a href="{_esc(url)}" color="{ACCENT}"><b>{_esc(label)}</b></a>'


def render_unseen_briefing_pdf(
    ranked: Sequence[dict],
    profile: dict,
    out_path,
    *,
    lower,
    upper,
    link_extractor: Callable[[dict], list[tuple[str, str]]] | None = None,
    today=None,
) -> None:
    """Write a clickable catch-up-briefing PDF to ``out_path``.

    ``ranked`` items use the same keys as ``render_unseen_briefing``:
    ``video_id, title, url, channel, published, score, matched_concepts``.
    ``link_extractor(video) -> [(label, url), ...]`` supplies the timestamped
    deep-links (the caller passes the same mindmap-link logic the Markdown
    path uses, so the two renderers stay in lockstep without this module
    importing from ``video_intel``).
    """
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=letter,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.6 * inch,
        title=f"Catch-up briefing {lower.isoformat()} to {upper.isoformat()}",
        author="video-intel",
    )
    # "unbounded" rather than the bare 0001-01-01 sentinel (issue #88); mirrors
    # video_intel._window_label. The PDF title (machine-ish) keeps ISO dates.
    lower_txt = "unbounded" if lower == date.min else lower.isoformat()
    story = [
        Paragraph("Catch-Up Briefing: Unseen Videos", _TITLE),
        Paragraph(
            f"{lower_txt} to {upper.isoformat()} &middot; {len(ranked)} video(s) "
            "&middot; tap any orange link to jump to that moment",
            _SUB,
        ),
    ]
    if not ranked:
        story.append(Paragraph("No unseen videos in this window.", _WHY))
        doc.build(story)
        return

    today_date = today or datetime.now(UTC).date()
    for video in ranked:
        story.append(Paragraph(_link(video["url"], video["title"]), _VTITLE))
        meta = f"{_esc(video['channel'])} &middot; {_esc(video.get('published', ''))}"
        age = _age(_parse_date(video.get("published", "")), today_date)
        if age:
            meta += f" &middot; age {age}"
        if video.get("score"):
            meta += f" &middot; relevance {video['score']:g}"
        story.append(Paragraph(meta, _META))
        if video.get("matched_concepts"):
            story.append(Paragraph("Why: " + _esc(", ".join(video["matched_concepts"])), _WHY))
        links = link_extractor(video) if link_extractor else []
        if links:
            story.append(Paragraph("  &middot;  ".join(_link(u, label) for label, u in links), _JUMP))
        story.append(HRFlowable(width="100%", thickness=0.4, color=_RULE, spaceBefore=7, spaceAfter=1))

    # Secondary "By year" appendix - same videos as above, grouped chronologically
    # (newest year first) for a temporal lens, without reordering the primary
    # relevance-ranked list. Mirrors render_unseen_briefing's appendix.
    by_year: dict[int, list[dict]] = {}
    for video in ranked:
        published = _parse_date(video.get("published", ""))
        if published is not None:
            by_year.setdefault(published.year, []).append(video)
    if by_year:
        story.append(Paragraph("By Year", _SUB))
        for year in sorted(by_year, reverse=True):
            story.append(Paragraph(str(year), _YEAR))
            for video in by_year[year]:
                story.append(
                    Paragraph(f"{_link(video['url'], video['title'])} &middot; {_esc(video['channel'])}", _JUMP)
                )

    doc.build(story)
