"""Issue #319: seven more readers skip a damaged meta.json (or concepts.json) with one WARNING.

Issue #313 routed the scan's own readers through ``_read_meta_or_skip``. The
readers here still parsed with ``json.loads(path.read_text(...))`` under a
catch that misses ``UnicodeDecodeError`` (a write torn mid-multibyte-character)
and never checked that the top level is an object, so either shape crashed the
command. Each test drives the real command or public entry point over a
synthetic corpus with one healthy video beside one damaged file, with only the
paid and network boundaries stubbed [core: real-caller], and asserts the
command completes, exactly one WARNING names the damaged file, the healthy
video's outcome is correct, and nothing was written next to the damaged file
[core: #124 read-guard].
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
from unittest.mock import MagicMock

import pytest

import video_intel

CHANNEL = "chan"
HEALTHY_VID = "vid00000001"  # 11 characters, so mark-skip's URL parser accepts it
HEALTHY_PREFIX = "2026-10-01-healthy-talk"
OLD_HEALTHY_PREFIX = "2026-09-30-older-healthy-title"
DAMAGED_VID = "dmgvid00002"
DAMAGED_PREFIX = "2026-10-02-damaged-talk"
MINDMAP = "# Mind map\n\n## Branch [00:01:00]\n- leaf [00:01:05]\n"


def _torn_bytes(body: dict) -> bytes:
    """``body`` as JSON with its Cyrillic title cut in the middle of a two-byte character."""
    full = json.dumps({**body, "title": "Привет мир"}, ensure_ascii=False).encode("utf-8")
    cut = full.index("и".encode()) + 1
    return full[:cut]


#: The two shapes that crashed before #319. The list is non-empty on purpose:
#: an empty list is falsy, and one reader (`if meta:`) passed it by accident.
DAMAGED_META = {
    "torn": _torn_bytes({"video_id": DAMAGED_VID}),
    "list": json.dumps([{"video_id": DAMAGED_VID}]).encode("utf-8"),
}
DAMAGED_CONCEPTS = {
    "torn": _torn_bytes({"video_id": DAMAGED_VID, "concepts": [{"concept_id": "c_damaged"}]}),
    "list": json.dumps([{"concept_id": "c_damaged"}]).encode("utf-8"),
}
SHAPES = sorted(DAMAGED_META)


@pytest.fixture(autouse=True)
def _clear_memos():
    video_intel._invalidate_video_id_cache()
    video_intel._DAMAGED_META_WARNED.clear()
    yield
    video_intel._invalidate_video_id_cache()
    video_intel._DAMAGED_META_WARNED.clear()


def _healthy_meta(prefix=HEALTHY_PREFIX, **extra) -> dict:
    meta = {
        "video_id": HEALTHY_VID,
        "video_url": f"https://www.youtube.com/watch?v={HEALTHY_VID}",
        "title": "Healthy talk",
        "published": "2026-10-01",
        "channel": CHANNEL,
    }
    meta.update(extra)
    return meta


def _write_healthy(channel_dir, prefix=HEALTHY_PREFIX, **extra):
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"{prefix}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    (channel_dir / f"{prefix}.meta.json").write_text(json.dumps(_healthy_meta(prefix, **extra)), encoding="utf-8")


def _write_damaged_meta(channel_dir, shape):
    """A damaged meta beside its own mind map, so a delete or a sidecar would show."""
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"{DAMAGED_PREFIX}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    damaged = channel_dir / f"{DAMAGED_PREFIX}.meta.json"
    damaged.write_bytes(DAMAGED_META[shape])
    return damaged


def _damaged_siblings(channel_dir) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(channel_dir.glob(f"{DAMAGED_PREFIX}.*"))}


def _warnings_naming(caplog, path) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and str(path) in r.getMessage()]


def _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before):
    assert len(_warnings_naming(caplog, damaged)) == 1
    assert _damaged_siblings(channel_dir) == before
    assert not list(channel_dir.glob("*.corrupt*"))


def _channels_config():
    return {"channels": [{"name": CHANNEL, "url": f"https://youtube.com/@{CHANNEL}"}]}


@pytest.fixture
def corpus(monkeypatch, tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda *_a, **_kw: root)
    return root


# ---------------------------------------------------------------------------
# dedupe: _scan_duplicate_groups
# ---------------------------------------------------------------------------


def _seed_duplicate_pair(channel_dir):
    _write_healthy(channel_dir, HEALTHY_PREFIX, processed="2026-10-05T00:00:00")
    _write_healthy(channel_dir, OLD_HEALTHY_PREFIX, title="Older title", processed="2026-10-01T00:00:00")


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_dedupe_when_neighbour_meta_damaged_handles_healthy_group_and_keeps_damaged_files(
    corpus, caplog, shape, apply
):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_duplicate_pair(channel_dir)
    damaged = _write_damaged_meta(channel_dir, shape)
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=apply), _channels_config())

    # Assert
    assert "Summary: 1 group(s), 1 excess file(s)" in caplog.text
    assert (channel_dir / f"{HEALTHY_PREFIX}.meta.json").exists()
    assert (channel_dir / f"{OLD_HEALTHY_PREFIX}.meta.json").exists() is (not apply)
    assert (channel_dir / f"{OLD_HEALTHY_PREFIX}.mindmap.md").exists() is (not apply)
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


# ---------------------------------------------------------------------------
# prune-shorts: _collect_short_candidates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_prune_shorts_when_neighbour_meta_damaged_lists_healthy_short_only(corpus, monkeypatch, caplog, shape):
    # Arrange: the healthy video is a Short by its cached duration; YouTube is stubbed.
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_youtube", lambda: lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(video_intel, "enrich_with_durations", lambda _yt, ids: {i: None for i in ids})
    monkeypatch.setattr(video_intel, "_is_youtube_short_url", lambda _vid: False)
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir, duration_seconds=30)
    damaged = _write_damaged_meta(channel_dir, shape)
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_prune_shorts(argparse.Namespace(channel=None, apply=False), _channels_config())

    # Assert
    assert "Summary: 1 Shorts" in caplog.text
    assert HEALTHY_VID in caplog.text
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


# ---------------------------------------------------------------------------
# taxonomy-build: build_taxonomy (concepts read and meta read)
# ---------------------------------------------------------------------------


def _write_concepts(channel_dir, prefix, video_id, concepts):
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = channel_dir / f"{prefix}.concepts.json"
    path.write_text(json.dumps({"video_id": video_id, "concepts": concepts}), encoding="utf-8")
    return path


def _seed_healthy_concepts(channel_dir):
    _write_healthy(channel_dir)
    _write_concepts(channel_dir, HEALTHY_PREFIX, HEALTHY_VID, [{"concept_id": "c1", "preferred_label": "Thing"}])


def _taxonomy(corpus) -> dict:
    return json.loads((corpus / "taxonomy.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_taxonomy_build_when_concepts_file_damaged_skips_it_and_keeps_healthy(corpus, caplog, shape):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    damaged = channel_dir / f"{DAMAGED_PREFIX}.concepts.json"
    damaged.write_bytes(DAMAGED_CONCEPTS[shape])
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.WARNING):
        video_intel.cmd_taxonomy_build(argparse.Namespace(), {})

    # Assert: the skipped file is not counted in built_from.
    taxonomy = _taxonomy(corpus)
    concepts = taxonomy["concepts"]
    assert sorted(concepts) == ["c1"]
    assert taxonomy["built_from"] == 1
    assert concepts["c1"]["first_seen"] == "2026-10-01"
    assert concepts["c1"]["video_count"] == 1
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


def test_cmd_taxonomy_build_when_corpus_healthy_counts_every_concepts_file(corpus):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    _write_concepts(channel_dir, DAMAGED_PREFIX, DAMAGED_VID, [])

    # Act
    video_intel.cmd_taxonomy_build(argparse.Namespace(), {})

    # Assert: a file with zero concepts still contributed, as before the move of the increment.
    assert _taxonomy(corpus)["built_from"] == 2


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_taxonomy_build_when_meta_beside_healthy_concepts_damaged_treats_it_as_missing(corpus, caplog, shape):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    damaged = _write_damaged_meta(channel_dir, shape)
    _write_concepts(channel_dir, DAMAGED_PREFIX, DAMAGED_VID, [{"concept_id": "c2", "preferred_label": "Other"}])
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.WARNING):
        video_intel.cmd_taxonomy_build(argparse.Namespace(), {})

    # Assert: c2 still counts, with no published date, exactly as when the meta is absent.
    concepts = _taxonomy(corpus)["concepts"]
    assert sorted(concepts) == ["c1", "c2"]
    assert concepts["c2"]["first_seen"] is None
    assert concepts["c2"]["video_count"] == 1
    assert concepts["c1"]["first_seen"] == "2026-10-01"
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


def test_cmd_taxonomy_build_when_concepts_is_not_a_list_skips_the_file(corpus, caplog):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    damaged = _write_concepts(channel_dir, DAMAGED_PREFIX, DAMAGED_VID, {"c_damaged": {"preferred_label": "X"}})

    # Act
    with caplog.at_level(logging.WARNING):
        video_intel.cmd_taxonomy_build(argparse.Namespace(), {})

    # Assert
    assert sorted(_taxonomy(corpus)["concepts"]) == ["c1"]
    warnings = _warnings_naming(caplog, damaged)
    assert len(warnings) == 1 and "not a list" in warnings[0]


def test_cmd_taxonomy_build_when_a_concept_entry_is_not_an_object_skips_that_entry(corpus):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    _write_concepts(channel_dir, DAMAGED_PREFIX, DAMAGED_VID, ["not an object", {"concept_id": "c3"}])

    # Act
    video_intel.cmd_taxonomy_build(argparse.Namespace(), {})

    # Assert
    assert sorted(_taxonomy(corpus)["concepts"]) == ["c1", "c3"]


# ---------------------------------------------------------------------------
# The end of a real scan: rebuild_free_derived_layers -> build_taxonomy
# ---------------------------------------------------------------------------


@pytest.fixture
def scan_env(monkeypatch, corpus):
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "require_youtube", lambda: MagicMock())
    monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(video_intel, "get_channel_id", lambda _yt, url: (f"UC-{url}", url))
    monkeypatch.setattr(video_intel, "fetch_preflight_status", lambda *a, **kw: {})
    monkeypatch.setattr(video_intel, "record_alt_title_if_rotated", lambda *a, **kw: False)
    monkeypatch.setattr(video_intel, "backup_config_if_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "render_headline_digest", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "build_search_index", lambda *a, **kw: pytest.fail("index must not be built"))

    new_video = {
        "video_id": "newvid00003",
        "url": "https://www.youtube.com/watch?v=newvid00003",
        "title": "New talk",
        "published": "2026-10-03",
        "duration_iso": "PT10M",
    }
    monkeypatch.setattr(video_intel, "fetch_channel_videos", lambda *_a, **_kw: [dict(new_video)])
    monkeypatch.setattr(video_intel, "enrich_with_durations", lambda _yt, ids: {i: "PT10M" for i in ids})

    def fake_transcribe(**kw):
        channel_dir, prefix, video = kw["channel_dir"], kw["prefix"], kw["video"]
        (channel_dir / f"{prefix}.transcript.md").write_text("# Transcript\n", encoding="utf-8")
        meta = {"video_id": video["video_id"], "title": video["title"], "published": video["published"]}
        (channel_dir / f"{prefix}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        return prefix, "done"

    def fake_mindmap(*args, **kwargs):
        video = args[2]
        prefix = kwargs.get("prefix") or video_intel.video_file_prefix(video)
        (corpus / CHANNEL / f"{prefix}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
        return prefix, "done"

    def fake_concepts(client, types_, video, mindmap_text, taxonomy, model, output_dir, ch_name, **kw):
        body = {"video_id": video["video_id"], "concepts": [{"concept_id": "fresh", "preferred_label": "Fresh"}]}
        (output_dir / ch_name / f"{kw['prefix']}.concepts.json").write_text(json.dumps(body), encoding="utf-8")
        return kw["prefix"], "done"

    monkeypatch.setattr(video_intel, "_scan_transcribe_one", fake_transcribe)
    monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)
    monkeypatch.setattr(video_intel, "process_concepts", fake_concepts)
    return corpus


def _scan_config():
    return {
        "channels": [{"name": CHANNEL, "url": CHANNEL, "auto_transcript": "all", "auto_concepts": True}],
    }


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_scan_when_concepts_file_damaged_rebuilds_taxonomy_at_the_end(scan_env, caplog, shape):
    # Arrange
    corpus = scan_env
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    damaged = channel_dir / f"{DAMAGED_PREFIX}.concepts.json"
    damaged.write_bytes(DAMAGED_CONCEPTS[shape])
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_scan(
            argparse.Namespace(channel=None, since=None, force=False, dry_run=False, model=None), _scan_config()
        )

    # Assert: the scan finished and taxonomy.json carries the healthy and the fresh concepts.
    messages = [r.getMessage() for r in caplog.records]
    assert "Done." in messages
    assert not any("taxonomy.json not rebuilt" in m for m in messages)
    assert sorted(_taxonomy(corpus)["concepts"]) == ["c1", "fresh"]
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


# ---------------------------------------------------------------------------
# mark-skip: the reader side
# ---------------------------------------------------------------------------


def _mark_skip_args(video_id):
    return argparse.Namespace(url=f"https://www.youtube.com/watch?v={video_id}", mode=["transcript"], reason=None)


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_mark_skip_when_neighbour_meta_damaged_marks_healthy_target(corpus, caplog, shape):
    # Arrange
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    damaged = _write_damaged_meta(channel_dir, shape)
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.WARNING):
        video_intel.cmd_mark_skip(_mark_skip_args(HEALTHY_VID), {})

    # Assert
    healthy = json.loads((channel_dir / f"{HEALTHY_PREFIX}.meta.json").read_text(encoding="utf-8"))
    assert healthy["skip_modes"] == ["transcript"]
    assert healthy["video_id"] == HEALTHY_VID
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_mark_skip_when_target_meta_damaged_exits_not_found(corpus, caplog, shape):
    # Arrange
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    damaged = _write_damaged_meta(channel_dir, shape)
    before = _damaged_siblings(channel_dir)
    healthy_before = (channel_dir / f"{HEALTHY_PREFIX}.meta.json").read_bytes()

    # Act
    with caplog.at_level(logging.WARNING), pytest.raises(SystemExit) as excinfo:
        video_intel.cmd_mark_skip(_mark_skip_args(DAMAGED_VID), {})

    # Assert: the existing not-found outcome, never a traceback, and nothing written anywhere.
    assert excinfo.value.code == 1
    assert f"No meta.json found for video_id {DAMAGED_VID}" in caplog.text
    assert (channel_dir / f"{HEALTHY_PREFIX}.meta.json").read_bytes() == healthy_before
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


# ---------------------------------------------------------------------------
# process --file: G2 (_find_canonical_meta_by_video_id) and the sibling read
# ---------------------------------------------------------------------------


@pytest.fixture
def process_env(monkeypatch, corpus):
    """Gemini, the upload and the paid steps stubbed; records each paid call's destination prefix."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(video_intel, "load_taxonomy", lambda _dir: {"concepts": {}})
    monkeypatch.setattr(video_intel, "upload_local_video", lambda _c, _p: "files/stub")
    calls = []

    def fake_mindmap(*args, **kwargs):
        calls.append(("mindmap", kwargs.get("prefix")))
        return kwargs.get("prefix"), "error: stubbed"

    def fake_transcript(*args, **kwargs):
        prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
        calls.append(("transcript", prefix))
        return prefix, "error: stubbed"

    monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)
    monkeypatch.setattr(video_intel, "process_transcript", fake_transcript)
    monkeypatch.setattr(video_intel, "process_concepts", lambda *a, **kw: pytest.fail("concepts must not run"))
    return corpus, calls


def _process_args(mp4, video_id=None):
    return argparse.Namespace(
        file=mp4,
        url=None,
        channel=CHANNEL,
        video_id=video_id,
        title=None,
        date=None,
        start=None,
        end=None,
        force=False,
        model=None,
        prompt=None,
    )


def _run_process(args):
    # The stubbed mind map reports an error, so process exits non-zero by design.
    with contextlib.suppress(SystemExit):
        video_intel.cmd_process(args, _channels_config())


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_process_file_when_neighbour_meta_damaged_g2_finds_healthy_canonical(process_env, caplog, shape):
    # Arrange: an id-named local file whose canonical scan meta lives under another prefix.
    corpus, calls = process_env
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    damaged = _write_damaged_meta(channel_dir, shape)
    mp4 = channel_dir / f"{HEALTHY_VID}.mp4"
    mp4.write_bytes(b"fake mp4 bytes")
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.WARNING):
        _run_process(_process_args(mp4))

    # Assert: the paid call was aimed at the healthy canonical prefix.
    assert sorted(calls) == [("mindmap", HEALTHY_PREFIX), ("transcript", HEALTHY_PREFIX)]
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


@pytest.mark.parametrize("shape", SHAPES)
def test_cmd_process_file_when_sibling_meta_damaged_continues_to_g2(process_env, caplog, shape):
    # Arrange: the local file sits beside its own damaged meta; --video-id names the healthy canonical.
    corpus, calls = process_env
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    damaged = _write_damaged_meta(channel_dir, shape)
    mp4 = channel_dir / f"{DAMAGED_PREFIX}.mp4"
    mp4.write_bytes(b"fake mp4 bytes")
    before = _damaged_siblings(channel_dir)

    # Act
    with caplog.at_level(logging.WARNING):
        _run_process(_process_args(mp4, video_id=HEALTHY_VID))

    # Assert: resolution skipped the damaged sibling and reached the G2 canonical.
    assert sorted(calls) == [("mindmap", HEALTHY_PREFIX), ("transcript", HEALTHY_PREFIX)]
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


# ---------------------------------------------------------------------------
# intel_graph.load_corpus: the meta read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_intel_graph_load_corpus_when_meta_damaged_loads_with_concepts_identity(corpus, caplog, shape):
    # Arrange
    duckdb = pytest.importorskip("duckdb")
    import intel_graph

    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    damaged = _write_damaged_meta(channel_dir, shape)
    _write_concepts(
        channel_dir, DAMAGED_PREFIX, DAMAGED_VID, [{"concept_id": "c2", "as_mentioned": "other", "confidence": 1.0}]
    )
    before = _damaged_siblings(channel_dir)
    con = duckdb.connect(":memory:")

    # Act
    with caplog.at_level(logging.WARNING):
        intel_graph.load_corpus(con, corpus)

    # Assert: both videos loaded; the damaged one keeps its concepts identity and falls back to its prefix as title.
    rows = dict(con.execute("SELECT artifact_id, title FROM artifacts").fetchall())
    assert rows == {HEALTHY_VID: "Healthy talk", DAMAGED_VID: DAMAGED_PREFIX}
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


# ---------------------------------------------------------------------------
# A failed READ (OSError) keeps each site's origin/main outcome [core: #124 read-guard]
# ---------------------------------------------------------------------------


@pytest.fixture
def unreadable(monkeypatch):
    """Make every read of the registered paths raise PermissionError; other files read normally."""
    paths: set[str] = set()
    real_read_bytes = video_intel.Path.read_bytes

    def flaky_read_bytes(self):
        if str(self) in paths:
            raise PermissionError(f"simulated: {self.name}")
        return real_read_bytes(self)

    monkeypatch.setattr(video_intel.Path, "read_bytes", flaky_read_bytes)
    return paths


@pytest.mark.parametrize("which", ["concepts", "meta"])
def test_cmd_taxonomy_build_when_a_read_fails_raises_and_writes_no_taxonomy(corpus, unreadable, which):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    suffix = ".concepts.json" if which == "concepts" else ".meta.json"
    unreadable.add(str(channel_dir / f"{HEALTHY_PREFIX}{suffix}"))

    # Act / Assert
    with pytest.raises(PermissionError):
        video_intel.cmd_taxonomy_build(argparse.Namespace(), {})
    assert not (corpus / "taxonomy.json").exists()


def test_cmd_scan_when_a_concepts_read_fails_logs_not_rebuilt_and_reaches_done(scan_env, unreadable, caplog):
    # Arrange
    corpus = scan_env
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    unreadable.add(str(channel_dir / f"{HEALTHY_PREFIX}.concepts.json"))

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_scan(
            argparse.Namespace(channel=None, since=None, force=False, dry_run=False, model=None), _scan_config()
        )

    # Assert
    messages = [r.getMessage() for r in caplog.records]
    assert "Done." in messages
    assert any("taxonomy.json not rebuilt" in m for m in messages)
    assert not (corpus / "taxonomy.json").exists()


def test_cmd_process_file_when_sibling_read_fails_raises_before_upload(process_env, monkeypatch):
    # Arrange: a healthy sibling meta whose FIRST read fails (momentarily unreadable), later reads succeed.
    # Skipping it would resolve identity from the fallback and go on to pay for the upload.
    corpus, calls = process_env
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    mp4 = channel_dir / f"{HEALTHY_PREFIX}.mp4"
    mp4.write_bytes(b"fake mp4 bytes")
    sibling = str(channel_dir / f"{HEALTHY_PREFIX}.meta.json")
    real_read_bytes = video_intel.Path.read_bytes
    failures = []

    def read_bytes_failing_once(self):
        if str(self) == sibling and not failures:
            failures.append(self)
            raise PermissionError("simulated: momentarily unreadable")
        return real_read_bytes(self)

    monkeypatch.setattr(video_intel.Path, "read_bytes", read_bytes_failing_once)
    uploads = []
    monkeypatch.setattr(video_intel, "upload_local_video", lambda _c, p: uploads.append(p) or "files/stub")

    # Act / Assert
    with pytest.raises(PermissionError):
        video_intel.cmd_process(_process_args(mp4), _channels_config())
    assert len(failures) == 1
    assert uploads == []
    assert calls == []


def test_cmd_mark_skip_when_a_meta_read_fails_raises_and_writes_nothing(corpus, unreadable):
    # Arrange
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    _write_healthy(channel_dir, DAMAGED_PREFIX, video_id=DAMAGED_VID)
    unreadable.add(str(channel_dir / f"{DAMAGED_PREFIX}.meta.json"))
    healthy_before = (channel_dir / f"{HEALTHY_PREFIX}.meta.json").read_bytes()

    # Act / Assert
    with pytest.raises(PermissionError):
        video_intel.cmd_mark_skip(_mark_skip_args(HEALTHY_VID), {})
    assert (channel_dir / f"{HEALTHY_PREFIX}.meta.json").read_bytes() == healthy_before
    assert not list(channel_dir.glob("*.corrupt*"))


def test_cmd_dedupe_when_a_meta_read_fails_skips_it_with_one_warning(corpus, unreadable, caplog):
    # Arrange: a third copy of the healthy video that cannot be read joins no group.
    channel_dir = corpus / CHANNEL
    _seed_duplicate_pair(channel_dir)
    _write_healthy(channel_dir, DAMAGED_PREFIX, processed="2026-10-09T00:00:00")
    unreadable_meta = channel_dir / f"{DAMAGED_PREFIX}.meta.json"
    unreadable.add(str(unreadable_meta))

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), _channels_config())

    # Assert: the readable pair was handled, the unreadable copy and its mind map survive.
    assert "Summary: 1 group(s), 1 excess file(s)" in caplog.text
    assert not (channel_dir / f"{OLD_HEALTHY_PREFIX}.meta.json").exists()
    assert unreadable_meta.exists() and (channel_dir / f"{DAMAGED_PREFIX}.mindmap.md").exists()
    warnings = _warnings_naming(caplog, unreadable_meta)
    assert len(warnings) == 1 and "PermissionError" in warnings[0]
