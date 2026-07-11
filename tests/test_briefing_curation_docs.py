"""Sample-driven doc tests for the curated-briefing contract (issue #84, Slice 2).

The curation layer is prose + skill routing, not triage code - but the contract
still needs guarding so the audience-profile template, the documented in-session
workflow, and the committed reference sample cannot silently drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AUDIENCE = REPO / "examples" / "audience.md"
SKILL = REPO / "skills" / "video-intel" / "SKILL.md"
SAMPLE_PDF = REPO / "examples" / "catch-up-briefing-personalized-sample.pdf"


def test_audience_template_exists_with_required_sections():
    """The hand-editable prose profile template must document its four surfaces."""
    text = AUDIENCE.read_text(encoding="utf-8").lower()
    assert "persona" in text
    assert "standing pillars" in text
    assert "signal" in text
    assert "noise" in text
    # It must call out that it is separate from the machine-scored profile.yaml.
    assert "profile.yaml" in text


def test_skill_documents_the_curation_workflow():
    """SKILL.md must route topic briefings to the in-session workflow and keep
    the boundary explicit (assistant curates; scripts supply candidates + render)."""
    text = SKILL.read_text(encoding="utf-8")
    assert "Curated topic briefing" in text
    assert "audience.md" in text
    assert "markdown_pdf.py" in text  # the render step
    assert "search" in text and "--vector" in text  # candidate gathering
    # The boundary: the script must not fabricate the editorial judgment.
    lowered = text.lower()
    assert "does not call an llm during triage" in lowered or "never authors this judgment" in lowered


def test_skill_documents_missing_audience_fallback():
    """A future assistant must not fabricate reader context when audience.md is
    absent - the workflow has to name the fallback explicitly (Codex review)."""
    text = SKILL.read_text(encoding="utf-8").lower()
    assert "does not exist" in text or "if it does not exist" in text
    assert "do not invent" in text or "never manufacture" in text


def test_sample_pdf_shows_the_promised_structure():
    """The committed reference sample must still contain the structural markers
    the workflow promises, so the doc and the example can't drift. `pypdf` is in
    the `dev` extra, so this runs (not silently skips) on a normal dev install."""
    pypdf = pytest.importorskip("pypdf", reason="reading the sample PDF needs pypdf")
    text = " ".join(p.extract_text() for p in pypdf.PdfReader(str(SAMPLE_PDF)).pages).lower()
    for marker in ("watch these", "pillar", "skim or skip"):
        assert marker in text, f"sample PDF is missing the promised structural marker: {marker!r}"
    # Cross-consistency: the SKILL workflow must actually promise the same
    # structural vocabulary the sample demonstrates, so doc and example can't drift.
    skill = SKILL.read_text(encoding="utf-8").lower()
    assert "watch these" in skill
    assert "pillar" in skill
    assert "signal/noise" in skill or "skim or skip" in skill


def test_audience_template_has_no_client_or_pii_leakage():
    """The committed template must stay generic - it ships publicly."""
    text = AUDIENCE.read_text(encoding="utf-8").lower()
    for banned in ("mastercard", "@gmail", "ciklum", "magma inc"):
        assert banned not in text, f"committed audience template leaks '{banned}'"
