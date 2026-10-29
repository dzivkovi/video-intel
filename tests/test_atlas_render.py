"""The catalog page renderer (`scripts/atlas_render.py`, issue #261).

Every test drives the real `main` or the real `render` over a synthetic atlas;
nothing is stubbed. The template under test is the shipped one.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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
        "deep_links": [
            {
                "label": "First </script> heading",
                "seconds": 245,
                "url": "https://www.youtube.com/watch?v=abcdefghijk&t=245",
            }
        ],
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

    def test_a_title_containing_the_sentinel_renders_verbatim(self, tmp_path):
        atlas = tiny_atlas()
        atlas["rows"][0]["title"] = f"Look: {ar.SENTINEL} inside a title"
        path = tmp_path / "atlas.json"
        path.write_text(json.dumps(atlas), encoding="utf-8")
        html = rendered(path)
        assert html.count(OPEN_TAG) == 1
        data, _ = split_data_block(html)
        assert json.loads(data)["rows"][0]["title"] == atlas["rows"][0]["title"]


class TestVocabulary:
    def test_template_quality_list_matches_the_schema_enum(self):
        """The template hand-lists the transcript_quality values; the schema is their one definition."""
        page = ar.TEMPLATE_PATH.read_text(encoding="utf-8")
        m = re.search(r"var QUALITY = \[([^\]]*)\];", page)
        assert m, "QUALITY list not found in the template"
        template_values = set(re.findall(r'"([a-z_]+)"', m.group(1)))
        schema = json.loads(
            (Path(__file__).resolve().parent.parent / "schemas" / "atlas.schema.json").read_text(encoding="utf-8")
        )
        schema_values = set(schema["$defs"]["row"]["properties"]["transcript_quality"]["enum"])
        assert template_values == schema_values

    def test_template_source_labels_cover_the_known_values(self):
        """Drift tripwire: the sourceLabel map must keep a word for each value the page relies on."""
        page = ar.TEMPLATE_PATH.read_text(encoding="utf-8")
        m = re.search(r"var SOURCE_LABELS = \{([^}]*)\};", page)
        assert m, "SOURCE_LABELS map not found in the template"
        keys = set(re.findall(r'"?([a-z_-]+)"?\s*:', m.group(1)))
        assert {"gemini", "youtube_captions", "none", "unrecorded"} <= keys


class TestEscapeChainParity:
    def test_embed_json_uses_the_same_chain_as_lead_lag_viz(self):
        """Two deliberate text copies (lead_lag_viz sits on the report stack); this pins them to each other."""
        chain = '.replace("&", "\\\\u0026").replace("<", "\\\\u003c").replace(">", "\\\\u003e")'
        render_src = Path(ar.__file__).read_text(encoding="utf-8")
        viz_src = (Path(ar.__file__).parent / "lead_lag_viz.py").read_text(encoding="utf-8")
        assert chain in render_src and chain in viz_src
        hostile = {"t": "</script><!--<script> & ]]>"}
        assert ar.embed_json(hostile) == json.dumps(hostile, ensure_ascii=False, separators=(",", ":")).replace(
            "&", "\\u0026"
        ).replace("<", "\\u003c").replace(">", "\\u003e")


class TestPageScript:
    def test_inline_script_parses_under_node(self, atlas_path, tmp_path):
        """A JavaScript syntax error must not ship green; node --check is the cheapest guard."""
        node = shutil.which("node")
        if not node:
            pytest.skip("node is not installed")
        html = rendered(atlas_path)
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        assert len(scripts) == 2, "expected the theme bootstrap and the page script"
        for k, body in enumerate(scripts):
            js = tmp_path / f"script{k}.js"
            js.write_text(body, encoding="utf-8")
            result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr


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
    def test_out_into_a_missing_folder_creates_it(self, atlas_path, tmp_path):
        target = tmp_path / "new" / "deeper" / "page.html"
        assert ar.main(["--atlas", str(atlas_path), "--out", str(target)]) == 0
        assert target.exists() and not list(target.parent.glob("*.tmp"))

    def test_a_failed_write_exits_2_without_a_traceback_and_keeps_the_old_page(self, atlas_path, capsys, monkeypatch):
        first = rendered(atlas_path)
        out = atlas_path.parent / "catalog.html"

        def refuse(src, dst):
            raise OSError("simulated share lock")

        monkeypatch.setattr(ar.os, "replace", refuse)
        assert ar.main(["--atlas", str(atlas_path)]) == ar.EXIT_REFUSED
        assert "simulated share lock" in capsys.readouterr().err
        assert out.read_text(encoding="utf-8") == first
        assert not list(atlas_path.parent.glob("catalog.html.*.tmp"))

    def test_a_directory_at_the_output_path_is_refused(self, atlas_path, capsys):
        out = atlas_path.parent / "catalog.html"
        out.mkdir()
        assert ar.main(["--atlas", str(atlas_path)]) == ar.EXIT_REFUSED
        assert "cannot be read" in capsys.readouterr().err
        assert out.is_dir()

    def test_a_marker_beyond_the_probe_window_is_refused(self, atlas_path, capsys):
        out = atlas_path.parent / "catalog.html"
        foreign = "<!doctype html>" + ("x" * (ar.OWNERSHIP_PROBE_BYTES + 16)) + ar.GENERATOR_META
        out.write_text(foreign, encoding="utf-8")
        assert ar.main(["--atlas", str(atlas_path)]) == ar.EXIT_REFUSED
        assert out.read_text(encoding="utf-8") == foreign
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_a_foreign_catalog_html_is_refused_and_left_intact(self, atlas_path, capsys):
        out = atlas_path.parent / "catalog.html"
        foreign = "<!doctype html><title>mine</title><p>hand-written</p>\n"
        out.write_text(foreign, encoding="utf-8")
        assert ar.main(["--atlas", str(atlas_path)]) == ar.EXIT_REFUSED
        assert out.read_text(encoding="utf-8") == foreign
        assert "refusing to overwrite" in capsys.readouterr().err
        assert not list(atlas_path.parent.glob("catalog.html.*.tmp"))

    def test_a_page_this_renderer_wrote_is_replaced_with_the_new_content(self, atlas_path):
        first = rendered(atlas_path)
        assert ar.GENERATOR_META in first
        atlas = tiny_atlas()
        atlas["rows"].append(dict(atlas["rows"][0], video_id="lmnopqrstuv", title="Second row"))
        atlas_path.write_text(json.dumps(atlas), encoding="utf-8")
        second = rendered(atlas_path)
        assert second != first
        data, _ = split_data_block(second)
        assert len(json.loads(data)["rows"]) == 2
        assert not list(atlas_path.parent.glob("catalog.html.*.tmp"))

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
