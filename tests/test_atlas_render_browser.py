"""Headless-browser smoke over the rendered catalog page (`scripts/atlas_render.py`).

The pytest text checks in `tests/test_atlas_render.py` cannot execute the page script.
This module loads the rendered synthetic page in headless Chromium through Playwright
and asserts the behaviours the review found only a browser can prove. Skipped when
Playwright or its Chromium build is absent, never failed for that reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import atlas_render as ar

playwright = pytest.importorskip("playwright.sync_api")

HOSTILE_TITLE = "Break </script><!--<script> out & <b>bold</b>"
ROWS = 60  # more than the Cards page (50), fewer than the Compact page (200)


def smoke_atlas() -> dict:
    rows = []
    for i in range(ROWS):
        vid = f"vid{i:08d}"
        rows.append(
            {
                "video_id": vid,
                "title": HOSTILE_TITLE if i == 0 else f"Video number {i}",
                "channel": "alpha" if i % 2 else "beta",
                "channels": ["alpha" if i % 2 else "beta"],
                "published": "2026-12-31" if i == 0 else f"2026-{(i % 12) + 1:02d}-15",
                "duration_seconds": 600 + i,
                "url": f"https://www.youtube.com/watch?v={vid}",
                "transcript_quality": "complete",
                "transcript_flag_count": 0,
                "synopsis": ["Same label", "Same label", "Other"],
                "deep_links": [
                    {"label": "Same label", "seconds": 10, "url": f"https://www.youtube.com/watch?v={vid}&t=10"},
                    {"label": "Same label", "seconds": 20, "url": f"https://www.youtube.com/watch?v={vid}&t=20"},
                ],
                "concept_ids": ["dom.c1"],
                "topics": ["sales"] if i % 3 == 0 else [],
                "in_briefings": False,
                "briefing_count": 0,
                "wiki": {"creator": None, "concepts": []},
                "aliases": [],
            }
        )
    rows.sort(key=lambda r: (r["published"], r["video_id"]))
    return {
        "schema_version": 1,
        "generator": "atlas_build.py",
        "generator_version": "1",
        "generated_from": "corpus",
        "generated_at": "2026-10-28",
        "summary": {
            "channels": {"alpha": ROWS // 2, "beta": ROWS // 2},
            "topics": {"sales": ROWS // 3},
            "unthreaded": 0,
        },
        "concepts": {"dom.c1": "Concept one"},
        "rows": rows,
    }


@pytest.fixture(scope="module")
def page_url(tmp_path_factory) -> str:
    folder = tmp_path_factory.mktemp("catalog")
    atlas_path = folder / "atlas.json"
    atlas_path.write_text(json.dumps(smoke_atlas()), encoding="utf-8")
    assert ar.main(["--atlas", str(atlas_path)]) == 0
    return (folder / "catalog.html").resolve().as_uri()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"headless Chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser, page_url):
    pg = browser.new_page(viewport={"width": 1280, "height": 900})
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(page_url)
    pg.wait_for_selector("#active-title:not(:empty)", timeout=15000)
    pg.errors = errors  # type: ignore[attr-defined]
    yield pg
    assert pg.errors == [], pg.errors  # type: ignore[attr-defined]
    pg.close()


def test_hostile_title_lands_as_text_and_the_page_runs(page):
    assert page.text_content("#active-title").startswith(f"{ROWS} of {ROWS} videos")
    titles = page.eval_on_selector_all("#cards .card h3", "els => els.map(e => e.textContent)")
    assert HOSTILE_TITLE in titles
    assert page.evaluate("document.querySelectorAll('script[type=\"application/json\"]').length") == 1


def test_two_headings_with_one_label_keep_their_own_deep_links(page):
    hrefs = page.eval_on_selector_all("#cards .card:first-child .syn a", "els => els.map(e => e.getAttribute('href'))")
    assert [h.rsplit("&t=", 1)[1] for h in hrefs] == ["10", "20"]


def test_a_facet_click_in_compact_keeps_the_compact_page_size(page):
    page.click("#view-compact")
    page.wait_for_selector("#toc tbody tr")
    assert page.evaluate("document.querySelectorAll('#toc tbody tr').length") == ROWS
    page.click("#facets button.fv[data-dim='ql']:has-text('complete')")  # every fixture row is complete
    page.wait_for_timeout(100)
    assert page.evaluate("document.querySelectorAll('#toc tbody tr').length") == ROWS
    assert "n=" not in page.evaluate("location.hash")


def test_facets_no_row_can_answer_are_hidden_and_the_finding_says_only_what_it_can(page):
    """No fixture row is in a briefing or has a wiki key, so those facets and clauses are absent."""
    dims = page.eval_on_selector_all("#facets details.facet", "els => els.map(e => e.getAttribute('data-dim'))")
    assert "tp" in dims and "br" not in dims and "wk" not in dims
    finding = page.text_content("#finding")
    assert "briefing" not in finding and "in no topic" in finding and "transcript" in finding


def test_the_hash_restores_a_view_and_a_pending_debounce_cannot_undo_it(page, page_url):
    page.goto(page_url + "#q=number&ch=alpha&v=compact")
    page.wait_for_selector("#toc tbody tr")
    assert "channel alpha" in page.text_content("#active-title")
    assert page.input_value("#q") == "number"
    # Type (arming the 160 ms debounce), then navigate immediately: the stale timer must not re-commit.
    page.fill("#q", "number 1")
    page.evaluate("location.hash = '#ch=beta'")
    page.wait_for_timeout(400)
    assert page.evaluate("location.hash") == "#ch=beta"
    assert page.input_value("#q") == ""
    assert "channel beta" in page.text_content("#active-title")
