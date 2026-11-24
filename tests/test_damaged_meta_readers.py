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
    "invalid_json": b'{"video_id": "vid002", "title": ',
}
#: An 11-character id, so the transcript-header parser can recover it.
DAMAGED_VID = "dmgvid00002"
ORIGINAL_RECORD_ALT_TITLE = video_intel.record_alt_title_if_rotated


@pytest.fixture(autouse=True)
def _clear_index_cache():
    video_intel._invalidate_video_id_cache()
    video_intel._DAMAGED_META_WARNED.clear()
    yield
    video_intel._invalidate_video_id_cache()
    video_intel._DAMAGED_META_WARNED.clear()


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
    fetched = [_fetched("vid001", "A rotated title", "2026-10-01")]
    monkeypatch.setattr(video_intel, "fetch_channel_videos", lambda *_a, **_kw: [dict(v) for v in fetched])
    monkeypatch.setattr(video_intel, "enrich_with_durations", lambda _yt, ids: {i: "PT10M" for i in ids})

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
    calls["fetched"] = fetched
    return corpus, calls


def _fetched(video_id, title, published):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "published": published,
        "duration_iso": "PT10M",
    }


def _scan_config():
    return {"channels": [{"name": "chan", "url": "chan", "auto_transcript": "all", "auto_concepts": True}]}


def _scan_args(force=False):
    return argparse.Namespace(channel=None, since=None, force=force, dry_run=False, model=None)


def _transcript_header(video_id: str) -> str:
    return (
        "# Transcript: Damaged talk\n\n"
        f"**Source:** https://www.youtube.com/watch?v={video_id}\n"
        "**Published:** 2026-10-02\n"
    )


@pytest.mark.parametrize("shape", sorted(DAMAGED_SHAPES))
def test_cmd_scan_when_neighbour_meta_damaged_completes_and_processes_healthy(scan_env, caplog, shape):
    # Arrange
    corpus, calls = scan_env
    channel_dir = corpus / "chan"
    damaged = _seed(channel_dir, shape)
    config = {"channels": [{"name": "chan", "url": "chan", "auto_transcript": "all", "auto_concepts": True}]}
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


# ---------------------------------------------------------------------------
# The fetched video's OWN meta is damaged: is_skipped leaves it alone
# ---------------------------------------------------------------------------


def _seed_own_damaged(channel_dir, shape, *, with_transcript=False):
    """The fetched video's own meta is damaged; no mind map, so a paid step would run."""
    channel_dir.mkdir(parents=True, exist_ok=True)
    damaged = channel_dir / f"{DAMAGED_PREFIX}.meta.json"
    damaged.write_bytes(DAMAGED_SHAPES[shape])
    if with_transcript:
        (channel_dir / f"{DAMAGED_PREFIX}.transcript.md").write_text(_transcript_header(DAMAGED_VID), "utf-8")
    return damaged


def _assert_left_alone(channel_dir, damaged, shape, calls, metas_before):
    assert calls["transcribe"] == []
    assert calls["mindmap"] == []
    assert calls["concepts"] == []
    assert damaged.read_bytes() == DAMAGED_SHAPES[shape]
    assert sorted(p.name for p in channel_dir.glob("*.meta.json")) == metas_before
    assert not list(channel_dir.glob("*.corrupt*"))


@pytest.mark.parametrize("force", [False, True], ids=["plain", "force"])
@pytest.mark.parametrize("shape", sorted(DAMAGED_SHAPES))
def test_cmd_scan_when_fetched_video_own_meta_damaged_leaves_it_alone(scan_env, caplog, shape, force):
    # Arrange
    corpus, calls = scan_env
    channel_dir = corpus / "chan"
    damaged = _seed_own_damaged(channel_dir, shape)
    calls["fetched"][:] = [_fetched(DAMAGED_VID, "Damaged talk", "2026-10-02")]
    assert video_intel.video_file_prefix(calls["fetched"][0]) == DAMAGED_PREFIX
    metas_before = sorted(p.name for p in channel_dir.glob("*.meta.json"))

    # Act
    with caplog.at_level(logging.WARNING, logger="video_intel"):
        video_intel.cmd_scan(_scan_args(force=force), _scan_config())

    # Assert
    _assert_left_alone(channel_dir, damaged, shape, calls, metas_before)
    warnings = _warnings_naming(caplog, damaged)
    assert len(warnings) == 1
    assert "until the meta is repaired or deleted" in warnings[0]


def test_is_skipped_when_meta_read_raises_oserror_returns_true(tmp_path, monkeypatch, caplog):
    # Arrange: the file exists but every read of it fails.
    channel_dir = tmp_path / "chan"
    damaged = _seed_own_damaged(channel_dir, "list")
    real_read_bytes = video_intel.Path.read_bytes

    def flaky_read_bytes(self):
        if self.name == damaged.name:
            raise PermissionError("simulated")
        return real_read_bytes(self)

    monkeypatch.setattr(video_intel.Path, "read_bytes", flaky_read_bytes)
    video = {"video_id": DAMAGED_VID, "title": "Damaged talk", "published": "2026-10-02"}

    # Act
    with caplog.at_level(logging.WARNING, logger="video_intel"):
        skipped = video_intel.is_skipped(tmp_path, "chan", video, mode="transcript")
        processed = video_intel.is_processed(tmp_path, "chan", video, "transcript")

    # Assert
    assert skipped is True
    assert processed is False
    warnings = _warnings_naming(caplog, damaged)
    assert len(warnings) == 1 and "PermissionError" in warnings[0]


# ---------------------------------------------------------------------------
# Rotated title + damaged meta: the id comes from the healthy transcript header
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(DAMAGED_SHAPES))
def test_cmd_scan_when_rotated_title_and_damaged_meta_with_transcript_recognizes_it(
    scan_env, monkeypatch, caplog, shape
):
    # Arrange: the real record_alt_title_if_rotated, so its read of the indexed damaged meta is exercised.
    monkeypatch.setattr(video_intel, "record_alt_title_if_rotated", ORIGINAL_RECORD_ALT_TITLE)
    corpus, calls = scan_env
    channel_dir = corpus / "chan"
    damaged = _seed_own_damaged(channel_dir, shape, with_transcript=True)
    (channel_dir / f"{DAMAGED_PREFIX}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    calls["fetched"][:] = [_fetched(DAMAGED_VID, "A rotated damaged title", "2026-10-02")]
    assert video_intel.video_file_prefix(calls["fetched"][0]) != DAMAGED_PREFIX
    metas_before = sorted(p.name for p in channel_dir.glob("*.meta.json"))

    # Act
    with caplog.at_level(logging.WARNING, logger="video_intel"):
        video_intel.cmd_scan(_scan_args(), _scan_config())

    # Assert
    _assert_left_alone(channel_dir, damaged, shape, calls, metas_before)
    warnings = _warnings_naming(caplog, damaged)
    assert len(warnings) == 1
    assert f"indexed as {DAMAGED_VID} from the sibling transcript header" in warnings[0]


def test_index_when_damaged_meta_transcript_is_not_youtube_leaves_it_unindexed(tmp_path, caplog):
    channel_dir = tmp_path / "chan"
    channel_dir.mkdir()
    damaged = channel_dir / f"{DAMAGED_PREFIX}.meta.json"
    damaged.write_bytes(DAMAGED_SHAPES["torn"])
    (channel_dir / f"{DAMAGED_PREFIX}.transcript.md").write_text(
        "# Transcript: Talk\n\n**Source:** https://vimeo.com/12345\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="video_intel"):
        index = video_intel._load_video_id_index(channel_dir)

    assert index == {}
    warnings = _warnings_naming(caplog, damaged)
    assert len(warnings) == 1 and "not indexed" in warnings[0]
    assert damaged.read_bytes() == DAMAGED_SHAPES["torn"]


@pytest.mark.parametrize("shape", ["torn", "list"])
def test_cmd_scan_when_rotated_title_and_damaged_meta_without_transcript_processes_as_new(scan_env, caplog, shape):
    """Pinned residual, accepted: nothing healthy carries the id, so the rotated title reads as a new video."""
    corpus, calls = scan_env
    channel_dir = corpus / "chan"
    damaged = _seed_own_damaged(channel_dir, shape)
    (channel_dir / f"{DAMAGED_PREFIX}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    calls["fetched"][:] = [_fetched(DAMAGED_VID, "A rotated damaged title", "2026-10-02")]

    with caplog.at_level(logging.WARNING, logger="video_intel"):
        video_intel.cmd_scan(_scan_args(), _scan_config())

    assert calls["transcribe"] == [DAMAGED_VID]
    assert calls["mindmap"] == [DAMAGED_VID]
    assert damaged.read_bytes() == DAMAGED_SHAPES[shape]
    assert "not indexed" in _warnings_naming(caplog, damaged)[0]


# ---------------------------------------------------------------------------
# Warning volume: once per file per process
# ---------------------------------------------------------------------------


def test_index_rebuilt_after_title_rotation_write_warns_once_per_damaged_file(tmp_path, caplog):
    # Arrange: a healthy video whose title rotates (a real write that drops the index) beside a damaged meta.
    channel_dir = tmp_path / "chan"
    damaged = _seed(channel_dir, "torn")
    rotated = {"video_id": "vid001", "title": "A rotated title", "published": "2026-10-01"}

    # Act
    with caplog.at_level(logging.WARNING, logger="video_intel"):
        wrote = ORIGINAL_RECORD_ALT_TITLE(tmp_path, "chan", rotated)
        processed = video_intel.is_processed(tmp_path, "chan", rotated, "transcript")
        video_intel._invalidate_video_id_cache()
        video_intel._load_video_id_index(channel_dir)

    # Assert
    assert wrote is True
    assert processed is True
    healthy = json.loads((channel_dir / f"{HEALTHY_PREFIX}.meta.json").read_text(encoding="utf-8"))
    assert healthy["alt_titles"] == ["A rotated title"]
    assert len(_warnings_naming(caplog, damaged)) == 1
