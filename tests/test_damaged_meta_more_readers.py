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


#: The two shapes that crashed before #319, plus invalid JSON (skipped before,
#: now also with the one WARNING). The list is non-empty on purpose: an empty
#: list is falsy, and one reader (`if meta:`) passed it by accident.
DAMAGED_META = {
    "torn": _torn_bytes({"video_id": DAMAGED_VID}),
    "list": json.dumps([{"video_id": DAMAGED_VID}]).encode("utf-8"),
    "invalid_json": b'{"video_id": "dmgvid00002", "title": ',
}
DAMAGED_CONCEPTS = {
    "torn": _torn_bytes({"video_id": DAMAGED_VID, "concepts": [{"concept_id": "c_damaged"}]}),
    "list": json.dumps([{"concept_id": "c_damaged"}]).encode("utf-8"),
    "invalid_json": b'{"video_id": "dmgvid00002", "concepts": [',
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
    warning = _warnings_naming(caplog, damaged)[0]
    assert "missing from taxonomy.json until the file is regenerated" in warning
    assert f"delete it and run `concepts --channel {CHANNEL}`" in warning


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

    # Assert: a not-found exit that names the damaged meta instead of telling the operator to scan.
    assert excinfo.value.code == 1
    assert f"No readable meta.json found for video_id {DAMAGED_VID}" in caplog.text
    assert f"1 damaged meta.json file(s) were skipped: {CHANNEL}/{DAMAGED_PREFIX}.meta.json" in caplog.text
    assert "The target may be one of them" in caplog.text
    assert "Run scan or transcript --url first" not in caplog.text
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


# ---------------------------------------------------------------------------
# G2 with only a damaged canonical: refuse before any upload (issue #277 principle)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["process", "transcript"])
@pytest.mark.parametrize("shape", SHAPES)
def test_file_ingest_when_only_canonical_meta_damaged_exits_1_before_upload(
    process_env, monkeypatch, caplog, shape, command
):
    # Arrange: the channel holds only a damaged meta; the local file is named by the id G2 looks up.
    corpus, calls = process_env
    channel_dir = corpus / CHANNEL
    damaged = _write_damaged_meta(channel_dir, shape)
    mp4 = channel_dir / f"{DAMAGED_VID}.mp4"
    mp4.write_bytes(b"fake mp4 bytes")
    listing_before = sorted(p.name for p in channel_dir.iterdir())
    uploads = []
    monkeypatch.setattr(video_intel, "upload_local_video", lambda _c, p: uploads.append(p) or "files/stub")
    cmd = video_intel.cmd_process if command == "process" else video_intel.cmd_transcript

    # Act
    with caplog.at_level(logging.WARNING), pytest.raises(SystemExit) as excinfo:
        cmd(_process_args(mp4), _channels_config())

    # Assert: exit code 1, no upload, no paid call, nothing new on disk, the damaged file untouched.
    assert excinfo.value.code == 1
    assert uploads == []
    assert calls == []
    assert sorted(p.name for p in channel_dir.iterdir()) == listing_before
    assert damaged.read_bytes() == DAMAGED_META[shape]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert damaged.name in errors[0] and str(channel_dir) in errors[0]
    assert "Repair or delete them, then run again" in errors[0]


@pytest.mark.parametrize("shape", SHAPES)
def test_file_ingest_when_rejected_stem_meta_is_damaged_and_stem_is_an_id_g2_exits_1_before_upload(
    process_env, monkeypatch, caplog, tmp_path, shape
):
    # Arrange: a file outside the corpus with an id-shaped stem, a damaged stem-named meta in the
    # channel folder, and --title/--date given. The adoption guard rejects the meta; G2 then refuses.
    corpus, calls = process_env
    channel_dir = corpus / CHANNEL
    channel_dir.mkdir(parents=True)
    stem_meta = channel_dir / f"{DAMAGED_VID}.meta.json"
    stem_meta.write_bytes(DAMAGED_META[shape])
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    mp4 = downloads / f"{DAMAGED_VID}.mp4"
    mp4.write_bytes(b"fake mp4 bytes")
    uploads = []
    monkeypatch.setattr(video_intel, "upload_local_video", lambda _c, p: uploads.append(p) or "files/stub")
    args = _process_args(mp4)
    args.title, args.date = "Damaged talk", "2026-10-02"

    # Act
    with caplog.at_level(logging.WARNING), pytest.raises(SystemExit) as excinfo:
        video_intel.cmd_process(args, _channels_config())

    # Assert: the guard's discard WARNING, then G2's refusal, exit code 1, no upload, no paid call.
    assert any(
        r.levelno == logging.WARNING and stem_meta.name in r.getMessage() and "discarding it" in r.getMessage()
        for r in caplog.records
    )
    assert excinfo.value.code == 1
    assert uploads == []
    assert calls == []
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and stem_meta.name in errors[0] and "Repair or delete them" in errors[0]


def test_g2_when_only_canonical_meta_unreadable_keeps_skipping_and_does_not_block(tmp_path, unreadable, caplog):
    # Arrange: a failed READ is not damaged content and must not block (it skips, as on origin/main).
    channel_dir = tmp_path / CHANNEL
    _write_healthy(channel_dir)
    meta = channel_dir / f"{HEALTHY_PREFIX}.meta.json"
    unreadable.add(str(meta))

    # Act
    with caplog.at_level(logging.WARNING):
        result = video_intel._find_canonical_meta_by_video_id(channel_dir, HEALTHY_VID)

    # Assert
    assert result == (None, [])
    warnings = _warnings_naming(caplog, meta)
    assert len(warnings) == 1 and "PermissionError" in warnings[0]


# ---------------------------------------------------------------------------
# The recovery a skipped concepts file's WARNING prescribes actually works
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_damaged_concepts_recovery_delete_and_concepts_channel_restores_taxonomy(corpus, monkeypatch, caplog, shape):
    # Arrange: a healthy video (meta + mind map) whose concepts.json is damaged; Gemini stubbed.
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(video_intel, "create_client", lambda *a, **k: object())
    monkeypatch.setattr(video_intel, "load_prompt", lambda n: "PROMPT {{taxonomy}}")
    restored = {"concepts": [{"concept_id": "c_restored", "preferred_label": "Restored"}]}
    monkeypatch.setattr(video_intel, "call_gemini_text", lambda *a, **k: json.dumps(restored))
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir, prompt="mindmap-knowledge")
    damaged = channel_dir / f"{HEALTHY_PREFIX}.concepts.json"
    damaged.write_bytes(DAMAGED_CONCEPTS[shape])
    with caplog.at_level(logging.WARNING):
        video_intel.cmd_taxonomy_build(argparse.Namespace(), {})
    assert "c_restored" not in _taxonomy(corpus)["concepts"]
    assert f"concepts --channel {CHANNEL}" in _warnings_naming(caplog, damaged)[0]

    # Act: the prescribed recovery - delete the file, run concepts for the channel, rebuild.
    damaged.unlink()
    concepts_args = argparse.Namespace(channel=CHANNEL, force=False, model=None, dry_run=False, log_level="info")
    video_intel.cmd_concepts(concepts_args, _channels_config())
    video_intel.cmd_taxonomy_build(argparse.Namespace(), {})

    # Assert
    assert json.loads(damaged.read_text(encoding="utf-8"))["video_id"] == HEALTHY_VID
    assert "c_restored" in _taxonomy(corpus)["concepts"]


# ---------------------------------------------------------------------------
# OSError keeps skipping at the skip sites (with the WARNING)
# ---------------------------------------------------------------------------


def test_cmd_prune_shorts_when_a_meta_read_fails_skips_it_with_one_warning(corpus, unreadable, monkeypatch, caplog):
    # Arrange: two Shorts by cached duration; one meta cannot be read.
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_youtube", lambda: lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(video_intel, "enrich_with_durations", lambda _yt, ids: {i: None for i in ids})
    monkeypatch.setattr(video_intel, "_is_youtube_short_url", lambda _vid: False)
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir, duration_seconds=30)
    _write_healthy(channel_dir, DAMAGED_PREFIX, video_id=DAMAGED_VID, duration_seconds=30)
    unreadable_meta = channel_dir / f"{DAMAGED_PREFIX}.meta.json"
    unreadable.add(str(unreadable_meta))

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_prune_shorts(argparse.Namespace(channel=None, apply=False), _channels_config())

    # Assert
    assert "Summary: 1 Shorts" in caplog.text
    warnings = _warnings_naming(caplog, unreadable_meta)
    assert len(warnings) == 1 and "PermissionError" in warnings[0]


def test_cmd_process_file_when_a_neighbour_meta_read_fails_g2_still_finds_healthy(process_env, unreadable, caplog):
    # Arrange
    corpus, calls = process_env
    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    _write_healthy(channel_dir, DAMAGED_PREFIX, video_id=DAMAGED_VID)
    unreadable_meta = channel_dir / f"{DAMAGED_PREFIX}.meta.json"
    unreadable.add(str(unreadable_meta))
    mp4 = channel_dir / f"{HEALTHY_VID}.mp4"
    mp4.write_bytes(b"fake mp4 bytes")

    # Act
    with caplog.at_level(logging.WARNING):
        _run_process(_process_args(mp4))

    # Assert
    assert sorted(calls) == [("mindmap", HEALTHY_PREFIX), ("transcript", HEALTHY_PREFIX)]
    warnings = _warnings_naming(caplog, unreadable_meta)
    assert len(warnings) == 1 and "PermissionError" in warnings[0]


@pytest.mark.parametrize("which", ["meta", "concepts"])
def test_intel_graph_load_corpus_when_a_read_fails_skips_as_before(corpus, unreadable, caplog, which):
    # Arrange
    duckdb = pytest.importorskip("duckdb")
    import intel_graph

    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    _write_healthy(channel_dir, DAMAGED_PREFIX, video_id=DAMAGED_VID, title="Second talk")
    _write_concepts(channel_dir, DAMAGED_PREFIX, DAMAGED_VID, [{"concept_id": "c2", "as_mentioned": "other"}])
    target = channel_dir / f"{DAMAGED_PREFIX}.{which}.json"
    unreadable.add(str(target))
    con = duckdb.connect(":memory:")

    # Act
    with caplog.at_level(logging.WARNING):
        intel_graph.load_corpus(con, corpus)

    # Assert: an unreadable meta loads the video with its concepts identity; an unreadable concepts file skips it.
    rows = dict(con.execute("SELECT artifact_id, title FROM artifacts").fetchall())
    expected = {HEALTHY_VID: "Healthy talk"}
    if which == "meta":
        expected[DAMAGED_VID] = DAMAGED_PREFIX
    assert rows == expected
    warnings = _warnings_naming(caplog, target)
    assert len(warnings) == 1 and "PermissionError" in warnings[0]


# ---------------------------------------------------------------------------
# intel_graph: the concepts read beside the meta read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_intel_graph_load_corpus_when_concepts_damaged_skips_that_video(corpus, caplog, shape):
    # Arrange
    duckdb = pytest.importorskip("duckdb")
    import intel_graph

    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    _write_healthy(channel_dir, DAMAGED_PREFIX, video_id=DAMAGED_VID)
    damaged = channel_dir / f"{DAMAGED_PREFIX}.concepts.json"
    damaged.write_bytes(DAMAGED_CONCEPTS[shape])
    before = _damaged_siblings(channel_dir)
    con = duckdb.connect(":memory:")

    # Act
    with caplog.at_level(logging.WARNING):
        intel_graph.load_corpus(con, corpus)

    # Assert
    rows = dict(con.execute("SELECT artifact_id, title FROM artifacts").fetchall())
    assert rows == {HEALTHY_VID: "Healthy talk"}
    _assert_one_warning_and_untouched(caplog, damaged, channel_dir, before)


# ---------------------------------------------------------------------------
# A concepts.json with no `concepts` key reads as empty
# ---------------------------------------------------------------------------


def test_cmd_taxonomy_build_when_concepts_key_missing_reads_empty_counts_file_no_warning(corpus, caplog):
    # Arrange
    channel_dir = corpus / CHANNEL
    _seed_healthy_concepts(channel_dir)
    no_key = channel_dir / f"{DAMAGED_PREFIX}.concepts.json"
    no_key.write_text(json.dumps({"video_id": DAMAGED_VID}), encoding="utf-8")

    # Act
    with caplog.at_level(logging.WARNING):
        video_intel.cmd_taxonomy_build(argparse.Namespace(), {})

    # Assert
    taxonomy = _taxonomy(corpus)
    assert sorted(taxonomy["concepts"]) == ["c1"]
    assert taxonomy["built_from"] == 2
    assert _warnings_naming(caplog, no_key) == []


# ---------------------------------------------------------------------------
# Ownership: an apply path never reaches a dot-extended neighbour's files (issue #319)
# ---------------------------------------------------------------------------

#: Who sits beside the healthy video: nobody, a damaged meta, or a healthy meta of another video.
NEIGHBOURS = ["damaged", "healthy"]


def _write_meta(channel_dir, prefix, **fields):
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = channel_dir / f"{prefix}.meta.json"
    path.write_text(json.dumps(fields), encoding="utf-8")
    return path


def _write_neighbour(channel_dir, prefix, kind, **healthy_fields):
    """A neighbour meta whose prefix dot-extends another video's; returns its files' bytes."""
    if kind == "damaged":
        (channel_dir / f"{prefix}.meta.json").write_bytes(DAMAGED_META["torn"])
    else:
        _write_meta(channel_dir, prefix, video_id="nbrvid00009", title="Neighbour", **healthy_fields)
    return channel_dir


def _snapshot(channel_dir, prefix):
    """Bytes of every file whose name starts with ``prefix + "."``."""
    return {p.name: p.read_bytes() for p in sorted(channel_dir.glob(f"{prefix}.*"))}


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
@pytest.mark.parametrize("neighbour", NEIGHBOURS)
def test_cmd_dedupe_when_loser_prefix_is_dot_extended_by_neighbour_keeps_neighbour_files(
    corpus, caplog, neighbour, apply
):
    # Arrange: duplicates `talk` (loser) and `winner` (canonical), and `talk.notes` with its own artifacts.
    channel_dir = corpus / CHANNEL
    _write_meta(channel_dir, "talk", video_id=HEALTHY_VID, title="Old title", processed="2026-10-01T00:00:00")
    (channel_dir / "talk.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    _write_meta(channel_dir, "winner", video_id=HEALTHY_VID, title="New title", processed="2026-10-05T00:00:00")
    (channel_dir / "winner.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    _write_neighbour(channel_dir, "talk.notes", neighbour)
    (channel_dir / "talk.notes.transcript.md").write_text("# Transcript\n", encoding="utf-8")
    (channel_dir / "talk.notes.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    neighbour_before = _snapshot(channel_dir, "talk.notes")

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=apply), _channels_config())

    # Assert: the group was handled; the neighbour's meta and artifacts are byte-identical.
    assert "Summary: 1 group(s), 1 excess file(s)" in caplog.text
    assert (channel_dir / "talk.meta.json").exists() is (not apply)
    assert (channel_dir / "talk.mindmap.md").exists() is (not apply)
    assert _snapshot(channel_dir, "talk.notes") == neighbour_before


@pytest.mark.parametrize("neighbour", NEIGHBOURS)
def test_cmd_dedupe_apply_when_moving_a_mode_never_moves_a_neighbours_file(corpus, neighbour):
    # Arrange: canonical `winner` lacks the mind map; loser `talk` has it, and `talk.mindmap.notes`
    # owns `talk.mindmap.notes.transcript.md`, which the `{prefix}.mindmap.*.md` pattern also matches.
    channel_dir = corpus / CHANNEL
    _write_meta(
        channel_dir,
        "talk",
        video_id=HEALTHY_VID,
        title="Old",
        processed="2026-10-01T00:00:00",
        modes_completed=["scan"],
    )
    (channel_dir / "talk.mindmap.md").write_text("# loser mind map\n", encoding="utf-8")
    _write_meta(channel_dir, "winner", video_id=HEALTHY_VID, title="New", processed="2026-10-05T00:00:00")
    _write_neighbour(channel_dir, "talk.mindmap.notes", neighbour)
    (channel_dir / "talk.mindmap.notes.transcript.md").write_text("# neighbour transcript\n", encoding="utf-8")
    neighbour_before = _snapshot(channel_dir, "talk.mindmap.notes")

    # Act
    video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), _channels_config())

    # Assert: the loser's mind map moved; the neighbour's file did not.
    assert (channel_dir / "winner.mindmap.md").read_text(encoding="utf-8") == "# loser mind map\n"
    assert not (channel_dir / "winner.mindmap.notes.transcript.md").exists()
    assert _snapshot(channel_dir, "talk.mindmap.notes") == neighbour_before


@pytest.mark.parametrize("neighbour", NEIGHBOURS)
def test_cmd_dedupe_apply_when_neighbour_file_matches_canonical_pattern_still_rescues_loser_mode(corpus, neighbour):
    # Arrange: canonical `talk` claims the mind map but has none on disk; `talk.mindmap.notes`'s
    # `talk.mindmap.notes.transcript.md` must not count as the canonical's mind map, or the loser's
    # real mind map is swept instead of moved.
    channel_dir = corpus / CHANNEL
    _write_meta(
        channel_dir,
        "talk",
        video_id=HEALTHY_VID,
        title="New",
        processed="2026-10-05T00:00:00",
        modes_completed=["scan"],
    )
    _write_meta(
        channel_dir,
        "winner",
        video_id=HEALTHY_VID,
        title="Old",
        processed="2026-10-01T00:00:00",
        modes_completed=["scan"],
    )
    (channel_dir / "winner.mindmap.md").write_text("# the only mind map\n", encoding="utf-8")
    _write_neighbour(channel_dir, "talk.mindmap.notes", neighbour)
    (channel_dir / "talk.mindmap.notes.transcript.md").write_text("# neighbour transcript\n", encoding="utf-8")
    neighbour_before = _snapshot(channel_dir, "talk.mindmap.notes")

    # Act
    video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), _channels_config())

    # Assert
    assert (channel_dir / "talk.mindmap.md").read_text(encoding="utf-8") == "# the only mind map\n"
    assert _snapshot(channel_dir, "talk.mindmap.notes") == neighbour_before


def test_cmd_dedupe_apply_when_no_overlap_behaves_as_before(corpus, caplog):
    # Arrange: the control - no dot-extended neighbour.
    channel_dir = corpus / CHANNEL
    _write_meta(
        channel_dir,
        "talk",
        video_id=HEALTHY_VID,
        title="Old",
        processed="2026-10-01T00:00:00",
        modes_completed=["scan"],
    )
    (channel_dir / "talk.mindmap.md").write_text("# loser mind map\n", encoding="utf-8")
    (channel_dir / "talk.en.srt").write_text("1\n", encoding="utf-8")  # no meta of its own: belongs to `talk`
    _write_meta(channel_dir, "winner", video_id=HEALTHY_VID, title="New", processed="2026-10-05T00:00:00")

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), _channels_config())

    # Assert
    assert "Summary: 1 group(s), 1 excess file(s) cleaned up." in caplog.text
    assert sorted(p.name for p in channel_dir.iterdir()) == ["winner.meta.json", "winner.mindmap.md"]


@pytest.fixture
def prune_env(monkeypatch, corpus):
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_youtube", lambda: lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(video_intel, "enrich_with_durations", lambda _yt, ids: {i: None for i in ids})
    monkeypatch.setattr(video_intel, "_is_youtube_short_url", lambda _vid: False)
    return corpus


def _seed_short(channel_dir):
    _write_meta(channel_dir, "talk", video_id=HEALTHY_VID, title="Short", duration_seconds=30)
    (channel_dir / "talk.mindmap.md").write_text(MINDMAP, encoding="utf-8")


#: (neighbour prefix, the Short's own files the ownership rule leaves to `talk`). With the neighbour
#: exactly `talk.mindmap` (the reproduced shape) the name `talk.mindmap.md` starts with
#: `talk.mindmap.`, so the rule gives it to the neighbour: it is kept and not counted.
PRUNE_OVERLAPS = [
    ("talk.mindmap", ["talk.meta.json"]),
    ("talk.mindmap.notes", ["talk.meta.json", "talk.mindmap.md"]),
]


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
@pytest.mark.parametrize(("nbr_prefix", "own_files"), PRUNE_OVERLAPS, ids=["talk.mindmap", "talk.mindmap.notes"])
@pytest.mark.parametrize("neighbour", NEIGHBOURS)
def test_cmd_prune_shorts_when_short_prefix_is_dot_extended_by_neighbour_keeps_neighbour_files(
    prune_env, caplog, neighbour, nbr_prefix, own_files, apply
):
    # Arrange: the Short `talk`, and a neighbour (a long video, or damaged) owning
    # `<nbr>.transcript.md`, which the `{prefix}.mindmap.*.md` pattern also matches.
    channel_dir = prune_env / CHANNEL
    _seed_short(channel_dir)
    _write_neighbour(channel_dir, nbr_prefix, neighbour, duration_seconds=600)
    (channel_dir / f"{nbr_prefix}.transcript.md").write_text("# neighbour transcript\n", encoding="utf-8")
    neighbour_before = _snapshot(channel_dir, nbr_prefix)

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_prune_shorts(argparse.Namespace(channel=None, apply=apply), _channels_config())

    # Assert: the count names only the files the rule gives to `talk`, and only they are deleted.
    assert f"Summary: 1 Shorts, {len(own_files)} artifacts" in caplog.text
    for name in own_files:
        assert (channel_dir / name).exists() is (not apply)
    assert _snapshot(channel_dir, nbr_prefix) == neighbour_before


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
def test_cmd_prune_shorts_when_no_overlap_behaves_as_before(prune_env, caplog, apply):
    # Arrange: the control - the Short plus a translate-bcs sibling, which is not on the allowlist.
    channel_dir = prune_env / CHANNEL
    _seed_short(channel_dir)
    (channel_dir / "talk.transcript.md").write_text("# Transcript\n", encoding="utf-8")
    (channel_dir / "talk.en.srt").write_text("1\n", encoding="utf-8")

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_prune_shorts(argparse.Namespace(channel=None, apply=apply), _channels_config())

    # Assert
    assert "Summary: 1 Shorts, 3 artifacts" in caplog.text
    remaining = sorted(p.name for p in channel_dir.iterdir())
    expected = ["talk.en.srt"] if apply else ["talk.en.srt", "talk.meta.json", "talk.mindmap.md", "talk.transcript.md"]
    assert remaining == expected


# ---------------------------------------------------------------------------
# intel_graph: a non-object entry in a concepts list is skipped
# ---------------------------------------------------------------------------


def test_intel_graph_load_corpus_when_concepts_list_holds_null_loads_the_valid_entry(corpus):
    # Arrange
    duckdb = pytest.importorskip("duckdb")
    import intel_graph

    channel_dir = corpus / CHANNEL
    _write_healthy(channel_dir)
    _write_concepts(
        channel_dir,
        HEALTHY_PREFIX,
        HEALTHY_VID,
        [None, {"concept_id": "c1", "preferred_label": "Thing", "as_mentioned": "thing", "confidence": 1.0}],
    )
    con = duckdb.connect(":memory:")

    # Act
    intel_graph.load_corpus(con, corpus)

    # Assert
    rows = con.execute("SELECT artifact_id, concept_id FROM has_concept").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(HEALTHY_VID, "c1")]


# ---------------------------------------------------------------------------
# _owned_glob: the prefix is literal, and names compare under the platform's case rule
# ---------------------------------------------------------------------------


def _filesystem_is_case_insensitive(path) -> bool:
    probe = path / "CaseProbe.txt"
    probe.write_text("x", encoding="utf-8")
    try:
        return (path / "caseprobe.txt").exists()
    finally:
        probe.unlink()


def test_cmd_dedupe_apply_when_loser_prefix_has_glob_characters_sweeps_only_its_own_files(corpus):
    # Arrange: loser `talk[1]` and canonical `winner`, beside another video `talk1` that an
    # unescaped `talk[1].*` would match.
    channel_dir = corpus / CHANNEL
    _write_meta(channel_dir, "talk[1]", video_id=HEALTHY_VID, title="Old", processed="2026-10-01T00:00:00")
    (channel_dir / "talk[1].mindmap.md").write_text(MINDMAP, encoding="utf-8")
    _write_meta(channel_dir, "winner", video_id=HEALTHY_VID, title="New", processed="2026-10-05T00:00:00")
    _write_meta(channel_dir, "talk1", video_id="othvid00007", title="Other video")
    (channel_dir / "talk1.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    other_before = _snapshot(channel_dir, "talk1")

    # Act
    video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), _channels_config())

    # Assert: the loser's meta and mind map are gone; the other video is untouched.
    assert not (channel_dir / "talk[1].meta.json").exists()
    assert not (channel_dir / "talk[1].mindmap.md").exists()
    assert _snapshot(channel_dir, "talk1") == other_before


@pytest.mark.parametrize("apply", [False, True], ids=["dry_run", "apply"])
def test_cmd_prune_shorts_when_short_prefix_has_glob_characters_touches_only_its_own_files(prune_env, caplog, apply):
    # Arrange: the Short `clip[ab]`, beside a long video `clipa` that an unescaped `clip[ab].*` would match.
    channel_dir = prune_env / CHANNEL
    _write_meta(channel_dir, "clip[ab]", video_id=HEALTHY_VID, title="Short", duration_seconds=30)
    (channel_dir / "clip[ab].mindmap.md").write_text(MINDMAP, encoding="utf-8")
    _write_meta(channel_dir, "clipa", video_id="othvid00007", title="Long", duration_seconds=600)
    (channel_dir / "clipa.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    (channel_dir / "clipa.transcript.md").write_text("# Transcript\n", encoding="utf-8")  # so a wrong count shows
    other_before = _snapshot(channel_dir, "clipa")

    # Act
    with caplog.at_level(logging.INFO):
        video_intel.cmd_prune_shorts(argparse.Namespace(channel=None, apply=apply), _channels_config())

    # Assert
    assert "Summary: 1 Shorts, 2 artifacts" in caplog.text
    assert (channel_dir / "clip[ab].meta.json").exists() is (not apply)
    assert (channel_dir / "clip[ab].mindmap.md").exists() is (not apply)
    assert _snapshot(channel_dir, "clipa") == other_before


def test_cmd_dedupe_apply_when_neighbour_files_differ_in_case_keeps_them(corpus):
    # Arrange: `talk.notes.meta.json` owns `TALK.NOTES.transcript.md` on a case-insensitive filesystem.
    channel_dir = corpus / CHANNEL
    channel_dir.mkdir(parents=True)
    if not _filesystem_is_case_insensitive(channel_dir):
        pytest.skip("the filesystem is case-sensitive, so the mixed-case name is a different file")
    _write_meta(channel_dir, "talk", video_id=HEALTHY_VID, title="Old", processed="2026-10-01T00:00:00")
    (channel_dir / "talk.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    _write_meta(channel_dir, "winner", video_id=HEALTHY_VID, title="New", processed="2026-10-05T00:00:00")
    _write_meta(channel_dir, "talk.notes", video_id="nbrvid00009", title="Neighbour")
    neighbour_file = channel_dir / "TALK.NOTES.transcript.md"
    neighbour_file.write_text("# neighbour transcript\n", encoding="utf-8")

    # Act
    video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), _channels_config())

    # Assert
    assert not (channel_dir / "talk.meta.json").exists()
    assert neighbour_file.read_text(encoding="utf-8") == "# neighbour transcript\n"
    assert (channel_dir / "talk.notes.meta.json").exists()
