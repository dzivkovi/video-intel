"""Issue #314: a successful scan step after a title rotation writes under the existing prefix.

``is_processed`` and ``is_skipped`` already find a rotated video through the
video-id index (``resolve_video_prefix``), but the scan's transcript and mind-map
loops computed their prefix from the CURRENT title, so a step that ran after a
rotation wrote a second ``meta.json`` under the new title's prefix. Every scan
writer now takes its prefix from ``resolve_video_prefix``, the same value the
filters use [core: writer's-path] [core: one-definition].

The tests drive the real ``cmd_scan`` twice (one process per scan in production,
so the index cache is dropped between runs). Only YouTube and the Gemini calls
are stubbed, so the real transcript, mind-map and concepts writers choose their
own paths [core: real-caller]. Expected paths are derived from the OLD title
independently of anything the scan passes around.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel

VID = "vid001"
OLD_TITLE = "The original title"
NEW_TITLE = "A rotated title"
PUBLISHED = "2026-10-01"
OLD_PREFIX = video_intel.video_file_prefix({"title": OLD_TITLE, "published": PUBLISHED})
NEW_PREFIX = video_intel.video_file_prefix({"title": NEW_TITLE, "published": PUBLISHED})
MINDMAP_MD = "# Mind map\n\n## Branch [00:01:00]\n- leaf [00:01:05]\n"
TRANSCRIPT_JSON = json.dumps(
    {
        "transcripts": [{"start": f"00:{m:02d}:00", "voice": 1, "text": f"line {m}"} for m in range(5)],
        "screen_content": [],
        "speakers": [{"voice": 1, "name": "Host"}],
    }
)
CONCEPTS_JSON = json.dumps({"concepts": [{"concept_id": "c1", "preferred_label": "Thing", "status": "new"}]})


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    """Real cmd_scan and real writers; YouTube and the Gemini calls are stubbed."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "require_youtube", lambda: MagicMock())
    monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: corpus)
    monkeypatch.setattr(video_intel, "get_channel_id", lambda _yt, url: (f"UC-{url}", url))
    monkeypatch.setattr(video_intel, "fetch_preflight_status", lambda *a, **kw: {})
    monkeypatch.setattr(video_intel, "enrich_with_durations", lambda _yt, ids: {i: "PT5M" for i in ids})
    monkeypatch.setattr(video_intel, "backup_config_if_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "render_headline_digest", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "rebuild_free_derived_layers", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "build_search_index", lambda *a, **kw: pytest.fail("index must not be built"))
    monkeypatch.setattr(video_intel, "_make_thinking_config_for_transcript", lambda _types, _model: None)

    fetched: list[dict] = []
    monkeypatch.setattr(video_intel, "fetch_channel_videos", lambda *_a, **_kw: [dict(v) for v in fetched])

    calls: dict[str, int] = {"transcript": 0, "mindmap_video": 0, "mindmap_text": 0, "concepts": 0}

    def fake_call_gemini(client, types, media_uri, prompt, model, **kw):
        if kw.get("response_json"):
            calls["transcript"] += 1
            return TRANSCRIPT_JSON
        calls["mindmap_video"] += 1
        return MINDMAP_MD

    def fake_call_gemini_text(client, types, text, model, **kw):
        if "# TRANSCRIPT TO PROCESS" in text:
            calls["mindmap_text"] += 1
            return MINDMAP_MD
        calls["concepts"] += 1
        return CONCEPTS_JSON

    monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(video_intel, "call_gemini_text", fake_call_gemini_text)
    video_intel._invalidate_video_id_cache()
    yield corpus / "chan", fetched, calls
    video_intel._invalidate_video_id_cache()


def _video(title: str) -> dict:
    return {
        "video_id": VID,
        "url": f"https://www.youtube.com/watch?v={VID}",
        "title": title,
        "published": PUBLISHED,
        "duration_iso": "PT5M",
    }


def _scan(fetched: list[dict], title: str, *, force: bool = False, **channel_knobs) -> None:
    """One scan, as its own process would see it: the index cache starts empty."""
    video_intel._invalidate_video_id_cache()
    fetched[:] = [_video(title)]
    config = {"channels": [{"name": "chan", "url": "chan", **channel_knobs}]}
    args = argparse.Namespace(channel=None, since=None, force=force, dry_run=False, model=None)
    video_intel.cmd_scan(args, config)


def _metas_for_vid(channel_dir) -> list[str]:
    return sorted(
        p.name
        for p in channel_dir.glob("*.meta.json")
        if json.loads(p.read_text(encoding="utf-8")).get("video_id") == VID
    )


def _names(channel_dir) -> list[str]:
    return sorted(p.name for p in channel_dir.iterdir())


def _assert_single_meta_under_old_prefix(channel_dir) -> dict:
    assert OLD_PREFIX != NEW_PREFIX
    assert _metas_for_vid(channel_dir) == [f"{OLD_PREFIX}.meta.json"]
    assert [n for n in _names(channel_dir) if n.startswith(NEW_PREFIX)] == []
    assert video_intel._scan_duplicate_groups(channel_dir) == {}
    return json.loads((channel_dir / f"{OLD_PREFIX}.meta.json").read_text(encoding="utf-8"))


def test_cmd_scan_rotated_title_when_transcript_step_succeeds_writes_under_existing_prefix(scan_env):
    # Arrange: run 1 writes a mind map and meta under the old title, no transcript.
    channel_dir, fetched, calls = scan_env
    _scan(fetched, OLD_TITLE)
    assert _names(channel_dir) == [f"{OLD_PREFIX}.meta.json", f"{OLD_PREFIX}.mindmap.md"]

    # Act: run 2 sees the rotated title with the transcript step on.
    _scan(fetched, NEW_TITLE, auto_transcript="all")

    # Assert
    assert calls["transcript"] == 1
    assert (channel_dir / f"{OLD_PREFIX}.transcript.md").exists()
    meta = _assert_single_meta_under_old_prefix(channel_dir)
    assert NEW_TITLE in meta["alt_titles"]
    assert "transcript" in meta["modes_completed"]


def test_cmd_scan_rotated_title_over_captions_threshold_keeps_captions_source(scan_env, monkeypatch):
    # Arrange: the per-video source is keyed by the same resolved prefix the transcript worker reads.
    channel_dir, fetched, calls = scan_env
    _scan(fetched, OLD_TITLE)
    captions = SimpleNamespace(
        snippets=[(float(s), f"cue {s}") for s in range(0, 300, 5)], is_generated=True, language="en"
    )
    monkeypatch.setattr(video_intel, "fetch_english_captions", lambda _vid, **_kw: captions)

    # Act
    _scan(fetched, NEW_TITLE, auto_transcript="all", captions_over_duration_seconds=60)

    # Assert: no Gemini transcript call, and the caption transcript sits next to the existing meta.
    assert calls["transcript"] == 0
    assert (channel_dir / f"{OLD_PREFIX}.transcript.md").exists()
    _assert_single_meta_under_old_prefix(channel_dir)


def test_cmd_scan_rotated_title_when_mindmap_step_succeeds_writes_under_existing_prefix(scan_env):
    # Arrange: run 1 writes a transcript and meta under the old title, no mind map.
    channel_dir, fetched, calls = scan_env
    _scan(fetched, OLD_TITLE, auto_transcript="all", auto_mindmap="none")
    assert _names(channel_dir) == [f"{OLD_PREFIX}.meta.json", f"{OLD_PREFIX}.transcript.md"]

    # Act
    _scan(fetched, NEW_TITLE, auto_transcript="all")

    # Assert: the mind map was built from the existing transcript and landed next to it.
    assert calls["transcript"] == 1
    assert calls["mindmap_text"] == 1
    assert calls["mindmap_video"] == 0
    assert list(channel_dir.glob(f"{OLD_PREFIX}.mindmap*.md")) == [channel_dir / f"{OLD_PREFIX}.mindmap.md"]
    meta = _assert_single_meta_under_old_prefix(channel_dir)
    assert NEW_TITLE in meta["alt_titles"]


def test_cmd_scan_new_video_lands_under_title_prefix_and_rescan_writes_nothing(scan_env):
    # Arrange
    channel_dir, fetched, calls = scan_env

    # Act
    _scan(fetched, OLD_TITLE, auto_transcript="all")
    after_first = _names(channel_dir)
    calls_after_first = dict(calls)
    _scan(fetched, OLD_TITLE, auto_transcript="all")

    # Assert
    assert after_first == [
        f"{OLD_PREFIX}.meta.json",
        f"{OLD_PREFIX}.mindmap.md",
        f"{OLD_PREFIX}.transcript.md",
    ]
    assert calls_after_first == {"transcript": 1, "mindmap_video": 0, "mindmap_text": 1, "concepts": 0}
    assert _names(channel_dir) == after_first
    assert calls == calls_after_first


def test_cmd_scan_force_with_rotated_title_re_extracts_concepts_of_existing_prefix(scan_env):
    # Arrange: run 1 writes mind map, concepts and meta under the old title.
    channel_dir, fetched, calls = scan_env
    _scan(fetched, OLD_TITLE, auto_concepts=True)
    concepts_path = channel_dir / f"{OLD_PREFIX}.concepts.json"
    assert concepts_path.exists()
    assert calls["concepts"] == 1
    concepts_path.write_text(json.dumps({"video_id": VID, "concepts": []}), encoding="utf-8")

    # Act: --force regenerates the mind map, and the concepts loop is bounded to what it touched.
    _scan(fetched, NEW_TITLE, force=True, auto_concepts=True)

    # Assert
    assert calls["mindmap_video"] == 2
    assert calls["concepts"] == 2
    assert json.loads(concepts_path.read_text(encoding="utf-8"))["concepts"][0]["concept_id"] == "c1"
    _assert_single_meta_under_old_prefix(channel_dir)


def test_cmd_scan_force_rotated_title_mindmap_source_none_re_extracts_concepts_of_existing_prefix(scan_env):
    # Arrange: mind map and concepts from before the channel turned its mind-map step off.
    channel_dir, fetched, calls = scan_env
    _scan(fetched, OLD_TITLE, auto_concepts=True)
    concepts_path = channel_dir / f"{OLD_PREFIX}.concepts.json"
    concepts_path.write_text(json.dumps({"video_id": VID, "concepts": []}), encoding="utf-8")

    # Act
    _scan(fetched, NEW_TITLE, force=True, auto_concepts=True, auto_transcript="all", mindmap_source="none")

    # Assert
    assert calls["mindmap_video"] == 1
    assert calls["concepts"] == 2
    assert json.loads(concepts_path.read_text(encoding="utf-8"))["concepts"][0]["concept_id"] == "c1"
    assert (channel_dir / f"{OLD_PREFIX}.transcript.md").exists()
    _assert_single_meta_under_old_prefix(channel_dir)
