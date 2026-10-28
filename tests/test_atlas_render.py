"""The catalog page renderer (`scripts/atlas_render.py`, issue #261).

Every test drives the real `main` or the real `render` over a synthetic atlas;
nothing is stubbed. The template under test is the shipped one.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import atlas_render as ar

OPEN_TAG = '<script type="application/json" id="atlas-data">'
HOSTILE_TITLE = "Break </script><!--<script> out & <b>bold</b> https://evil.example"
THUMB_HOST = "https://i.ytimg.com/vi/"


def tiny_atlas() -> dict:
    row = {
        "video_id": "abcdefghijk",
        "title": HOSTILE_TITLE,
        "channel": "somechannel",
        "channels": ["somechannel"],
        "published": "2026-10-01",
        "duration_seconds": 245,
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "transcript_quality": "complete",
        "transcript_flag_count": 0,
        "synopsis": ["First </script> heading", "Second"],
        "deep_links": [{"label": "First", "seconds": 245, "url": "https://www.youtube.com/watch?v=abcdefghijk&t=245"}],
        "concept_ids": ["dom.c1"],
        "topics": [],
        "in_briefings": False,
        "briefing_count": 0,
        "wiki": {"creator": None, "concepts": []},
        "aliases": [],
    }
    return {
        "schema_version": 1,
        "generator": "atlas_build.py",
        "generator_version": "1",
        "generated_from": "corpus",
        "generated_at": "2026-10-28",
        "summary": {"channels": {"somechannel": 1}, "topics": {}, "unthreaded": 1},
        "concepts": {"dom.c1": "Concept <one>"},
        "rows": [row],
    }


@pytest.fixture
def atlas_path(tmp_path: Path) -> Path:
    path = tmp_path / "atlas.json"
    path.write_text(json.dumps(tiny_atlas()), encoding="utf-8")
    return path


def rendered(atlas_path: Path) -> str:
    assert ar.main(["--atlas", str(atlas_path)]) == 0
    return (atlas_path.parent / "catalog.html").read_text(encoding="utf-8")


def split_data_block(html: str) -> tuple[str, str]:
    start = html.index(OPEN_TAG) + len(OPEN_TAG)
    end = html.index("</script>", start)
    return html[start:end], html[: start - len(OPEN_TAG)] + html[end:]


class TestEscaping:
    def test_data_block_is_escaped_and_round_trips(self, atlas_path):
        html = rendered(atlas_path)
        assert html.count(OPEN_TAG) == 1
        assert ar.SENTINEL not in html
        data, _ = split_data_block(html)
        assert "<" not in data and ">" not in data and "&" not in data
        assert "\\u003c/script\\u003e" in data
        assert json.loads(data)["rows"][0]["title"] == HOSTILE_TITLE

    def test_title_cannot_close_the_script_element(self, atlas_path):
        html = rendered(atlas_path)
        assert "Break </script>" not in html
        assert "<!--<script>" not in html
        assert len(re.findall(r"<script\b", html)) == html.count("</script>")

    def test_corpus_text_never_reaches_inner_html(self):
        """Every value from the data is set through textContent or setAttribute."""
        page = ar.TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "innerHTML" not in page and "insertAdjacentHTML" not in page and "outerHTML" not in page

    def test_the_sentinel_appears_exactly_once_in_the_template(self):
        assert ar.TEMPLATE_PATH.read_text(encoding="utf-8").count(ar.SENTINEL) == 1

    def test_render_refuses_a_template_with_a_doubled_sentinel(self):
        with pytest.raises(ValueError, match="exactly once"):
            ar.render(tiny_atlas(), f"{ar.GENERATOR_META}{ar.SENTINEL}{ar.SENTINEL}")


class TestSelfContained:
    def test_no_external_resources_except_the_thumbnail_host(self, atlas_path):
        """The page loads no script, style or font from the network.

        Thumbnails are the one deliberate outbound fetch: the page builds
        `https://i.ytimg.com/vi/<id>/mqdefault.jpg` from each row's own watch
        URL, lazily, with no referrer, and the viewer can turn them off. No
        other http(s) string may appear outside the data block.
        """
        html = rendered(atlas_path)
        assert not re.search(r"<script[^>]*\bsrc\s*=", html, re.I)
        _, outside = split_data_block(html)
        assert "http://" not in outside
        assert outside.count("https://") == 1 and THUMB_HOST in outside
        assert not re.search(r'<link[^>]+href="(?!data:)', outside)
        assert 'referrerpolicy: "no-referrer"' in outside and 'loading: "lazy"' in outside

    def test_the_thumbnail_id_comes_from_the_watch_url_not_the_row_id(self):
        """`thumbUrl` matches the stored URL's `v=` (or youtu.be path); it never reads `video_id`."""
        page = ar.TEMPLATE_PATH.read_text(encoding="utf-8")
        start = page.index("function thumbUrl(r)")
        body = page[start : page.index("\n  }", start)]
        assert "r.url" in body and "video_id" not in body


class TestOwnership:
    def test_a_foreign_catalog_html_is_refused_and_left_intact(self, atlas_path, capsys):
        out = atlas_path.parent / "catalog.html"
        foreign = "<!doctype html><title>mine</title><p>hand-written</p>\n"
        out.write_text(foreign, encoding="utf-8")
        assert ar.main(["--atlas", str(atlas_path)]) == ar.EXIT_REFUSED
        assert out.read_text(encoding="utf-8") == foreign
        assert "refusing to overwrite" in capsys.readouterr().err
        assert not list(atlas_path.parent.glob("catalog.html.*.tmp"))

    def test_a_page_this_renderer_wrote_is_replaced(self, atlas_path):
        first = rendered(atlas_path)
        assert ar.GENERATOR_META in first
        assert ar.main(["--atlas", str(atlas_path)]) == 0

    def test_out_flag_writes_elsewhere_and_leaves_the_default_alone(self, atlas_path, tmp_path):
        target = tmp_path / "elsewhere" / "page.html"
        target.parent.mkdir()
        assert ar.main(["--atlas", str(atlas_path), "--out", str(target)]) == 0
        assert target.exists() and not (atlas_path.parent / "catalog.html").exists()


class TestInputGuards:
    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda a: a.update(generator="wiki_atlas.py"), "not an atlas.json"),
            (lambda a: a.update(schema_version=2), "schema_version"),
            (lambda a: a.update(rows={}), "no rows list"),
        ],
    )
    def test_a_file_that_is_not_a_current_atlas_is_refused(self, tmp_path, capsys, mutate, message):
        atlas = tiny_atlas()
        mutate(atlas)
        path = tmp_path / "atlas.json"
        path.write_text(json.dumps(atlas), encoding="utf-8")
        assert ar.main(["--atlas", str(path)]) == ar.EXIT_REFUSED
        assert message in capsys.readouterr().err
        assert not (tmp_path / "catalog.html").exists()

    def test_a_missing_atlas_is_refused_with_exit_2(self, tmp_path, capsys):
        assert ar.main(["--atlas", str(tmp_path / "nope.json")]) == ar.EXIT_REFUSED
        assert "nope.json" in capsys.readouterr().err


class TestDeterminism:
    def test_rendering_twice_is_byte_identical(self, atlas_path):
        first = rendered(atlas_path)
        second = rendered(atlas_path)
        assert first == second

    def test_the_page_uses_lf_line_endings(self, atlas_path):
        raw = rendered(atlas_path)
        assert "\r" not in raw
