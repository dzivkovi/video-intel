"""Issue #313: a damaged meta.json is skipped with a WARNING at every reader site, never fatal.

Three damaged shapes used to raise straight out of the reader-side meta reads
and stop whatever asked: a file torn in the middle of a multi-byte character
(``UnicodeDecodeError``, a ``ValueError`` and not an ``OSError``), a top level
that is not an object (``AttributeError``), and a ``video_id`` that is not a
string (``TypeError`` in the index, a non-string id handed to Gemini in the
concepts loops). The readers stay strict: the damaged file is skipped, nothing
is written next to it, and no identity is invented [core: #124 read-guard].

The tests drive the real callers (``is_processed``, ``cmd_scan``,
``cmd_concepts``) with only the paid and network boundaries stubbed, and
assert on persisted content and on calls not made [core: real-caller].
"""

from __future__ import annotations

import argparse
import json
import logging
from unittest.mock import MagicMock

import pytest

import video_intel

HEALTHY_PREFIX = "2026-10-01-healthy-talk"
DAMAGED_PREFIX = "2026-10-02-damaged-talk"
MINDMAP = "# Mind map\n\n## Branch [00:01:00]\n- leaf [00:01:05]\n"


def _torn_bytes() -> bytes:
    """A meta whose Cyrillic title is cut in the middle of a two-byte character."""
    full = json.dumps({"video_id": "vid002", "title": "Привет мир"}, ensure_ascii=False).encode()
    cut = full.index("и".encode()) + 1  # one byte into a two-byte character
    return full[:cut]


DAMAGED_SHAPES = {
    "torn": _torn_bytes(),
    "list": b"[]",
    "non_string_id": json.dumps({"video_id": ["vid002"], "title": "Damaged talk"}).encode("utf-8"),
}


@pytest.fixture(autouse=True)
def _clear_index_cache():
    video_intel._invalidate_video_id_cache()
    yield
    video_intel._invalidate_video_id_cache()


def _healthy_meta(**extra) -> dict:
    meta = {
        "video_id": "vid001",
        "video_url": "https://www.youtube.com/watch?v=vid001",
        "title": "Healthy talk",
        "published": "2026-10-01",
        "channel": "chan",
        "prompt": "mindmap-knowledge",
    }
    meta.update(extra)
    return meta


def _seed(channel_dir, shape, *, healthy_has_concepts=False):
    """One healthy video (mindmap + transcript + meta) and one damaged meta beside a mindmap."""
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"{HEALTHY_PREFIX}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    (channel_dir / f"{HEALTHY_PREFIX}.transcript.md").write_text("# Transcript\n", encoding="utf-8")
    (channel_dir / f"{HEALTHY_PREFIX}.meta.json").write_text(json.dumps(_healthy_meta()), encoding="utf-8")
    if healthy_has_concepts:
        (channel_dir / f"{HEALTHY_PREFIX}.concepts.json").write_text("{}", encoding="utf-8")
    (channel_dir / f"{DAMAGED_PREFIX}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    damaged = channel_dir / f"{DAMAGED_PREFIX}.meta.json"
    damaged.write_bytes(DAMAGED_SHAPES[shape])
    return damaged


def _assert_damaged_untouched(channel_dir, damaged, shape):
    assert damaged.read_bytes() == DAMAGED_SHAPES[shape]
    assert not list(channel_dir.glob("*.meta.corrupt.json"))
    assert not (channel_dir / f"{DAMAGED_PREFIX}.concepts.json").exists()


def _warnings_naming(caplog, path) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and str(path) in r.getMessage()]


# ---------------------------------------------------------------------------
# is_processed, through _load_video_id_index
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(DAMAGED_SHAPES))
def test_is_processed_when_neighbour_meta_damaged_skips_it_and_indexes_healthy(tmp_path, caplog, shape):
    # Arrange: the fetched title is rotated, so only the video_id index can find the healthy artifacts.
    channel_dir = tmp_path / "chan"
    damaged = _seed(channel_dir, shape)
    rotated = {"video_id": "vid001", "title": "A rotated title", "published": "2026-10-01"}
    assert video_intel.video_file_prefix(rotated) != HEALTHY_PREFIX

    # Act
    with caplog.at_level(logging.WARNING, logger="video_intel"):
        processed = video_intel.is_processed(tmp_path, "chan", rotated, "transcript")
        index = video_intel._load_video_id_index(channel_dir)

    # Assert
    assert processed is True
    assert index == {"vid001": HEALTHY_PREFIX}
    assert len(_warnings_naming(caplog, damaged)) == 1
    _assert_damaged_untouched(channel_dir, damaged, shape)


def test_is_processed_when_meta_has_no_video_id_keeps_it_unindexed_without_warning(tmp_path, caplog):
    channel_dir = tmp_path / "chan"
    channel_dir.mkdir()
    no_id = channel_dir / "2026-10-03-no-id.meta.json"
    no_id.write_text(json.dumps({"title": "No id"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="video_intel"):
        index = video_intel._load_video_id_index(channel_dir)

    assert index == {}
    assert _warnings_naming(caplog, no_id) == []


# ---------------------------------------------------------------------------
# cmd_scan: the index on the prevention path and the auto_concepts candidate loop
# ---------------------------------------------------------------------------


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "require_youtube", lambda: MagicMock())
    monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: corpus)
    monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(video_intel, "get_channel_id", lambda _yt, url: (f"UC-{url}", url))
    monkeypatch.setattr(video_intel, "fetch_preflight_status", lambda *a, **kw: {})
    monkeypatch.setattr(video_intel, "record_alt_title_if_rotated", lambda *a, **kw: False)
    monkeypatch.setattr(video_intel, "backup_config_if_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "render_headline_digest", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "rebuild_free_derived_layers", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "build_search_index", lambda *a, **kw: pytest.fail("index must not be built"))

    # The fetched video is the healthy one under a rotated title: only the index recognizes it.
    fetched = {
        "video_id": "vid001",
        "url": "https://www.youtube.com/watch?v=vid001",
        "title": "A rotated title",
        "published": "2026-10-01",
        "duration_iso": "PT10M",
    }
    monkeypatch.setattr(video_intel, "fetch_channel_videos", lambda *_a, **_kw: [dict(fetched)])
    monkeypatch.setattr(video_intel, "enrich_with_durations", lambda _yt, ids: {"vid001": "PT10M"})

    calls = {"mindmap": [], "transcribe": [], "concepts": []}

    def fake_mindmap(*args, **kwargs):
        calls["mindmap"].append(args[2]["video_id"])
        return "unused", "done"

    def fake_transcribe(**kw):
        calls["transcribe"].append(kw["video"]["video_id"])
        return kw["prefix"], "done"

    def fake_concepts(client, types_, video, mindmap_text, taxonomy, model, output_dir, ch_name, **kw):
        calls["concepts"].append((video["video_id"], kw["prefix"]))
        body = {"video_id": video["video_id"], "concepts": [{"concept_id": "c1", "preferred_label": "Thing"}]}
        (output_dir / ch_name / f"{kw['prefix']}.concepts.json").write_text(json.dumps(body), encoding="utf-8")
        return kw["prefix"], "done"

    monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)
    monkeypatch.setattr(video_intel, "_scan_transcribe_one", fake_transcribe)
    monkeypatch.setattr(video_intel, "process_concepts", fake_concepts)
    return corpus, calls


@pytest.mark.parametrize("shape", sorted(DAMAGED_SHAPES))
def test_cmd_scan_when_neighbour_meta_damaged_completes_and_processes_healthy(scan_env, caplog, shape):
    # Arrange
    corpus, calls = scan_env
    channel_dir = corpus / "chan"
    damaged = _seed(channel_dir, shape)
    config = {"channels": [{"name": "chan", "url": "chan", "auto_transcript": True, "auto_concepts": True}]}
    args = argparse.Namespace(channel=None, since=None, force=False, dry_run=False, model=None)

    # Act
    with caplog.at_level(logging.WARNING, logger="video_intel"):
        video_intel.cmd_scan(args, config)

    # Assert: the healthy video was recognized as processed (no paid re-run) and got its concepts.
    assert calls["transcribe"] == []
    assert calls["mindmap"] == []
    assert calls["concepts"] == [("vid001", HEALTHY_PREFIX)]
    assert json.loads((channel_dir / f"{HEALTHY_PREFIX}.concepts.json").read_text(encoding="utf-8"))["video_id"] == (
        "vid001"
    )
    assert _warnings_naming(caplog, damaged)
    _assert_damaged_untouched(channel_dir, damaged, shape)


# ---------------------------------------------------------------------------
# cmd_concepts: the collection loop
# ---------------------------------------------------------------------------


@pytest.fixture
def concepts_env(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda cfg, **kw: corpus)
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(video_intel, "create_client", lambda *a, **k: object())
    monkeypatch.setattr(video_intel, "load_taxonomy", lambda *a, **k: {"concepts": {}})
    monkeypatch.setattr(video_intel, "load_prompt", lambda n: "PROMPT {{taxonomy}}")
    gemini_calls = []

    def fake_gemini(client, types, text, model, **k):
        gemini_calls.append(text)
        return json.dumps({"concepts": [{"concept_id": "c1", "preferred_label": "Thing"}]})

    monkeypatch.setattr(video_intel, "call_gemini_text", fake_gemini)
    return corpus, gemini_calls


@pytest.mark.parametrize("shape", sorted(DAMAGED_SHAPES))
def test_cmd_concepts_when_neighbour_meta_damaged_skips_it_and_extracts_healthy(concepts_env, caplog, shape):
    # Arrange
    corpus, gemini_calls = concepts_env
    channel_dir = corpus / "chan"
    damaged = _seed(channel_dir, shape)
    config = {"output_dir": str(corpus), "channels": [{"name": "chan", "url": "https://youtube.com/@chan"}]}
    args = argparse.Namespace(channel="chan", force=False, model=None, dry_run=False, log_level="info")

    # Act
    with caplog.at_level(logging.WARNING, logger="video_intel"):
        video_intel.cmd_concepts(args, config)

    # Assert: exactly one paid call, for the healthy video, and its concepts landed on its own stem.
    assert len(gemini_calls) == 1
    healthy = json.loads((channel_dir / f"{HEALTHY_PREFIX}.concepts.json").read_text(encoding="utf-8"))
    assert healthy["video_id"] == "vid001"
    assert len(_warnings_naming(caplog, damaged)) == 1
    _assert_damaged_untouched(channel_dir, damaged, shape)
