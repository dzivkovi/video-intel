"""Minimal checks for the #261 catalog renderer spike."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_catalog as rc

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


def rendered(tmp_path: Path) -> str:
    atlas_path = tmp_path / "atlas.json"
    atlas_path.write_text(json.dumps(tiny_atlas()), encoding="utf-8")
    assert rc.main(["--atlas", str(atlas_path)]) == 0
    return (tmp_path / "catalog.html").read_text(encoding="utf-8")


def split_data_block(html: str) -> tuple[str, str]:
    start = html.index(OPEN_TAG) + len(OPEN_TAG)
    end = html.index("</script>", start)
    return html[start:end], html[: start - len(OPEN_TAG)] + html[end:]


def test_data_block_is_escaped_and_round_trips(tmp_path):
    html = rendered(tmp_path)
    assert html.count(OPEN_TAG) == 1
    assert rc.SENTINEL not in html
    data, _ = split_data_block(html)
    assert "<" not in data and ">" not in data and "&" not in data
    assert "\\u003c/script\\u003e" in data
    assert json.loads(data)["rows"][0]["title"] == HOSTILE_TITLE


def test_title_cannot_close_the_script_element(tmp_path):
    html = rendered(tmp_path)
    assert "Break </script>" not in html
    assert "<!--<script>" not in html
    # every opening script tag has exactly one matching close
    assert len(re.findall(r"<script\b", html)) == html.count("</script>")


def test_no_external_resources_except_the_thumbnail_host(tmp_path):
    """The page loads no script, style or font from the network.

    Thumbnails are the one deliberate outbound fetch: the page builds
    `https://i.ytimg.com/vi/<id>/mqdefault.jpg` from each row's own watch URL,
    lazily, with no referrer, and the viewer can turn them off. No other
    http(s) string may appear outside the data block.
    """
    html = rendered(tmp_path)
    assert not re.search(r"<script[^>]*\bsrc\s*=", html, re.I)
    _, outside = split_data_block(html)
    assert "http://" not in outside
    assert outside.count("https://") == 1 and THUMB_HOST in outside
    assert not re.search(r'<link[^>]+href="(?!data:)', outside)
    assert 'referrerpolicy: "no-referrer"' in outside and 'loading: "lazy"' in outside
