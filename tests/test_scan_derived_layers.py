"""Issue #283: scan ends with the free derived layers consistent; the index stays a printed recipe.

The guard tests drive the REAL `cmd_scan` with only the paid and network
boundaries stubbed (YouTube, the Gemini-backed `process_mindmap` /
`process_concepts` / `_scan_transcribe_one`), and assert on what lands on
disk: `taxonomy.json` carries the concept this scan minted, `topics.json` and
the catalog exist, and no Voyage client is ever built [core: real-caller].
"""

import argparse
import json
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

import atlas_build
import corpus_walk
import video_intel
from video_intel import cmd_scan, scan_status_wrote_artifact


def _args(*, channel=None, dry_run=False):
    return argparse.Namespace(channel=channel, since=None, force=False, dry_run=dry_run, model=None)


def _video(video_id, title):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "published": "2026-10-01",
        "duration_iso": "PT10M",
    }


class _VoyageSpy:
    """Stands in for the `voyageai` package; records every client construction."""

    def __init__(self):
        self.clients = 0
        spy = self

        class Client:
            def __init__(self, *a, **kw):
                spy.clients += 1

        self.module = types.SimpleNamespace(Client=Client)


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    """A tmp corpus, the stubbed boundaries, and the Voyage/index spies."""
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
    monkeypatch.setattr(video_intel, "is_short", lambda *a, **kw: False)
    monkeypatch.setattr(video_intel, "record_alt_title_if_rotated", lambda *a, **kw: False)
    monkeypatch.setattr(video_intel, "backup_config_if_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(video_intel, "render_headline_digest", lambda *_a, **_kw: None)

    voyage = _VoyageSpy()
    monkeypatch.setitem(sys.modules, "voyageai", voyage.module)
    monkeypatch.setattr(video_intel, "require_voyageai", lambda: voyage.module)
    index_calls = []
    monkeypatch.setattr(video_intel, "build_search_index", lambda *a, **kw: index_calls.append(a) or 0)

    videos_by_channel: dict[str, list[dict]] = {}

    monkeypatch.setattr(
        video_intel,
        "fetch_channel_videos",
        lambda _yt, channel_id, *_a, **_kw: [dict(v) for v in videos_by_channel.get(channel_id, [])],
    )
    monkeypatch.setattr(
        video_intel,
        "enrich_with_durations",
        lambda _yt, ids: {v["video_id"]: v["duration_iso"] for vs in videos_by_channel.values() for v in vs},
    )

    def fake_mindmap(*args, **kwargs):
        video, channel = args[2], args[6]
        prefix = kwargs.get("prefix") or video_intel.video_file_prefix(video)
        channel_dir = corpus / channel
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / f"{prefix}.mindmap.md").write_text("# Mind map\n\n## Branch [00:01:00]\n- leaf [00:01:05]\n")
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": video["video_id"],
                    "video_url": video["url"],
                    "title": video["title"],
                    "published": video["published"],
                    "channel": channel,
                    "prompt": "mindmap-knowledge",
                    "modes_completed": ["scan"],
                }
            ),
            encoding="utf-8",
        )
        return prefix, "done"

    def fake_concepts(client, types_, video, mindmap_text, taxonomy, model, output_dir, ch_name, **kw):
        prefix = kw["prefix"]
        body = {
            "video_id": video["video_id"],
            "concepts": [{"concept_id": f"fresh_{video['video_id']}", "preferred_label": f"Fresh {video['video_id']}"}],
        }
        (output_dir / ch_name / f"{prefix}.concepts.json").write_text(json.dumps(body), encoding="utf-8")
        return prefix, "done"

    def fake_transcribe(**kw):
        channel_dir, prefix, video = kw["channel_dir"], kw["prefix"], kw["video"]
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / f"{prefix}.transcript.md").write_text("# Transcript\n", encoding="utf-8")
        meta = channel_dir / f"{prefix}.meta.json"
        if not meta.exists():  # the transcript loop runs first and stamps identity (#66)
            meta.write_text(
                json.dumps({"video_id": video["video_id"], "title": video["title"], "published": video["published"]}),
                encoding="utf-8",
            )
        return prefix, "done"

    monkeypatch.setattr(video_intel, "process_mindmap", fake_mindmap)
    monkeypatch.setattr(video_intel, "process_concepts", fake_concepts)
    monkeypatch.setattr(video_intel, "_scan_transcribe_one", fake_transcribe)

    env = types.SimpleNamespace(corpus=corpus, videos=videos_by_channel, voyage=voyage, index_calls=index_calls)
    return env


def _seed_taxonomy(corpus):
    path = corpus / "taxonomy.json"
    path.write_text(json.dumps({"version": 1, "concepts": {"old_concept": {"preferred_label": "Old"}}}), "utf-8")
    return path.read_bytes()


def _make_index(index_dir, video_ids):
    """A real one-table LanceDB index holding one chunk per id."""
    lancedb = pytest.importorskip("lancedb")
    db = lancedb.connect(str(index_dir))
    db.create_table(
        corpus_walk.SEARCH_INDEX_TABLE,
        data=[{"video_id": vid, "channel": "c", "text": "t", "vector": [0.1, 0.2]} for vid in video_ids],
    )


# ---------------------------------------------------------------------------
# The guard: the rebuild happens, and the index is never built
# ---------------------------------------------------------------------------


class TestScanRebuildsTheFreeLayers:
    def test_taxonomy_topics_and_catalog_are_rebuilt_and_no_voyage_client_is_built(self, scan_env):
        before = _seed_taxonomy(scan_env.corpus)
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "First talk")]
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_concepts": True}]}

        cmd_scan(_args(), config)

        taxonomy_bytes = (scan_env.corpus / "taxonomy.json").read_bytes()
        assert taxonomy_bytes != before
        assert "fresh_aaaaaaaaaa1" in json.loads(taxonomy_bytes)["concepts"]
        assert (scan_env.corpus / video_intel.TOPICS_FILENAME).is_file()
        atlas = json.loads((scan_env.corpus / "_atlas" / "atlas.json").read_text(encoding="utf-8"))
        assert [r["video_id"] for r in atlas["rows"]] == ["aaaaaaaaaa1"]
        assert atlas["rows"][0]["concept_ids"] == ["fresh_aaaaaaaaaa1"]
        assert (scan_env.corpus / "_atlas" / "catalog.html").is_file()
        assert scan_env.voyage.clients == 0
        assert scan_env.index_calls == []

    def test_a_scan_that_wrote_nothing_rebuilds_nothing(self, scan_env):
        before = _seed_taxonomy(scan_env.corpus)
        video = _video("aaaaaaaaaa1", "First talk")
        scan_env.videos["UC-alpha"] = [video]
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_concepts": True}]}
        cmd_scan(_args(), config)  # first run writes everything
        (scan_env.corpus / "taxonomy.json").write_bytes(before)
        for derived in (scan_env.corpus / "_atlas" / "atlas.json", scan_env.corpus / "_atlas" / "catalog.html"):
            derived.unlink()

        cmd_scan(_args(), config)  # second run: every artifact exists, nothing is written

        assert (scan_env.corpus / "taxonomy.json").read_bytes() == before
        assert not (scan_env.corpus / "_atlas" / "atlas.json").exists()

    def test_dry_run_rebuilds_nothing(self, scan_env):
        before = _seed_taxonomy(scan_env.corpus)
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "First talk")]
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_concepts": True}]}

        cmd_scan(_args(dry_run=True), config)

        assert (scan_env.corpus / "taxonomy.json").read_bytes() == before
        assert not (scan_env.corpus / "_atlas").exists()
        assert not (scan_env.corpus / video_intel.TOPICS_FILENAME).exists()

    def test_a_failed_step_is_logged_and_the_later_steps_still_run(self, scan_env, monkeypatch, caplog):
        def broken(_output_dir):
            raise ValueError("corrupt concepts file")

        monkeypatch.setattr(video_intel, "build_taxonomy", broken)
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "First talk")]
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_concepts": True}]}

        with caplog.at_level(logging.INFO, logger="video_intel"):
            cmd_scan(_args(), config)

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("taxonomy.json not rebuilt (corrupt concepts file)" in w and "taxonomy-build" in w for w in warnings)
        assert (scan_env.corpus / "_atlas" / "atlas.json").is_file()
        assert caplog.records[-1].getMessage() == "Done."

    def test_a_refused_catalog_does_not_end_the_scan(self, scan_env, caplog):
        foreign = scan_env.corpus / "_atlas" / "atlas.json"
        foreign.parent.mkdir()
        foreign.write_text('{"generator": "someone-else"}', encoding="utf-8")
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "First talk")]
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_concepts": True}]}

        with caplog.at_level(logging.INFO, logger="video_intel"):
            cmd_scan(_args(), config)

        assert foreign.read_text(encoding="utf-8") == '{"generator": "someone-else"}'
        assert any("catalog not rebuilt" in r.getMessage() for r in caplog.records)
        assert caplog.records[-1].getMessage() == "Done."

    # Each loop's hook has to mark the channel on its own: a scan where only that
    # loop wrote anything still rebuilds (the testing review deleted each hook
    # in turn and these cases are what fail).

    def test_a_transcript_only_scan_rebuilds(self, scan_env):
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "First talk")]
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_transcript": "all", "mindmap_source": "none"}]}

        cmd_scan(_args(), config)

        atlas = json.loads((scan_env.corpus / "_atlas" / "atlas.json").read_text(encoding="utf-8"))
        assert [r["video_id"] for r in atlas["rows"]] == ["aaaaaaaaaa1"]
        assert not list((scan_env.corpus / "alpha").glob("*.mindmap.md"))

    def test_a_concepts_backfill_with_no_new_video_rebuilds_the_taxonomy(self, scan_env):
        channel_dir = scan_env.corpus / "alpha"
        channel_dir.mkdir()
        (channel_dir / "2026-09-01-old-talk.mindmap.md").write_text("# Mind map\n\n## Branch [00:01:00]\n", "utf-8")
        (channel_dir / "2026-09-01-old-talk.meta.json").write_text(
            json.dumps({"video_id": "oooooooooo1", "title": "Old talk", "published": "2026-09-01"}), encoding="utf-8"
        )
        _seed_taxonomy(scan_env.corpus)
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_concepts": True}]}

        cmd_scan(_args(), config)  # YouTube returns nothing; only the concepts loop writes

        assert "fresh_oooooooooo1" in json.loads((scan_env.corpus / "taxonomy.json").read_text("utf-8"))["concepts"]

    def test_a_concepts_write_whose_meta_update_failed_still_rebuilds(self, scan_env, monkeypatch):
        """process_concepts writes concepts.json BEFORE its meta update, so an
        `error` status can follow a real write; the taxonomy must still follow."""
        channel_dir = scan_env.corpus / "alpha"
        channel_dir.mkdir()
        (channel_dir / "2026-09-01-old-talk.mindmap.md").write_text("# Mind map\n\n## Branch [00:01:00]\n", "utf-8")
        (channel_dir / "2026-09-01-old-talk.meta.json").write_text(
            json.dumps({"video_id": "oooooooooo1", "title": "Old talk", "published": "2026-09-01"}), encoding="utf-8"
        )
        written = video_intel.process_concepts

        def write_then_fail(*args, **kw):
            written(*args, **kw)
            return kw["prefix"], "error: meta update failed"

        monkeypatch.setattr(video_intel, "process_concepts", write_then_fail)
        config = {"channels": [{"name": "alpha", "url": "alpha", "auto_concepts": True}]}

        cmd_scan(_args(), config)

        assert "fresh_oooooooooo1" in json.loads((scan_env.corpus / "taxonomy.json").read_text("utf-8"))["concepts"]

    def test_a_title_rotation_alone_rebuilds(self, scan_env, monkeypatch):
        video = _video("aaaaaaaaaa1", "First talk")
        scan_env.videos["UC-alpha"] = [video]
        config = {"channels": [{"name": "alpha", "url": "alpha"}]}
        cmd_scan(_args(), config)  # first run writes the mind map
        (scan_env.corpus / "_atlas" / "atlas.json").unlink()
        monkeypatch.setattr(video_intel, "record_alt_title_if_rotated", lambda *a, **kw: True)

        cmd_scan(_args(), config)  # nothing new to process; only the rotation was recorded

        assert (scan_env.corpus / "_atlas" / "atlas.json").is_file()


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------


def _recipe(caplog):
    lines = [r.getMessage() for r in caplog.records]
    start = next(i for i, m in enumerate(lines) if m.startswith("Search index not rebuilt by scan"))
    return lines[start : start + 10]


class TestIndexRecipe:
    def _config(self, index_dir):
        return {
            "vector_db_dir": str(index_dir),
            "channels": [
                {"name": "alpha", "url": "alpha", "auto_transcript": "all", "mindmap_source": "auto"},
                {"name": "beta", "url": "beta"},
            ],
        }

    def test_only_channels_whose_transcripts_changed_are_listed(self, scan_env, tmp_path, caplog):
        index_dir = tmp_path / "index"
        _make_index(index_dir, ["zzzzzzzzzz9"])
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "Alpha talk")]
        scan_env.videos["UC-beta"] = [_video("bbbbbbbbbb2", "Beta talk")]

        with caplog.at_level(logging.INFO, logger="video_intel"):
            cmd_scan(_args(), self._config(index_dir))

        recipe = _recipe(caplog)
        assert "  python scripts/video_intel.py index --incremental" in recipe
        assert recipe[0].endswith("Transcripts changed in: alpha")  # beta wrote only a mind map
        assert not any(line == "  python scripts/video_intel.py index" for line in recipe)
        assert any("atlas_build.py --corpus" in line and str(index_dir) in line for line in recipe)
        assert scan_env.voyage.clients == 0
        atlas = json.loads((scan_env.corpus / "_atlas" / "atlas.json").read_text(encoding="utf-8"))
        assert {r["video_id"]: r["in_search_index"] for r in atlas["rows"]} == {
            "aaaaaaaaaa1": False,
            "bbbbbbbbbb2": False,
        }

    def test_a_confirmed_absent_index_prescribes_the_full_build(self, scan_env, tmp_path, caplog):
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "Alpha talk")]

        with caplog.at_level(logging.INFO, logger="video_intel"):
            cmd_scan(_args(), self._config(tmp_path / "no-index-here"))

        recipe = _recipe(caplog)
        assert "  python scripts/video_intel.py index" in recipe
        assert not any("--channel" in line for line in recipe if "video_intel.py index" in line)
        note = recipe[recipe.index("  python scripts/video_intel.py index") + 1]
        assert "index --force is running" in note  # a running full rebuild looks the same as no index

    def test_an_unknown_index_state_keeps_the_incremental_line(self, scan_env, tmp_path, monkeypatch, caplog):
        index_dir = tmp_path / "index"
        index_dir.mkdir()
        monkeypatch.setattr(
            atlas_build, "read_search_index", lambda _d: atlas_build.IndexRead(atlas_build.INDEX_UNKNOWN)
        )
        scan_env.videos["UC-alpha"] = [_video("aaaaaaaaaa1", "Alpha talk")]

        with caplog.at_level(logging.INFO, logger="video_intel"):
            cmd_scan(_args(), self._config(index_dir))

        recipe = _recipe(caplog)
        assert "  python scripts/video_intel.py index --incremental" in recipe
        assert "  python scripts/video_intel.py index" not in recipe

    def test_a_mindmap_only_scan_prints_no_recipe(self, scan_env, caplog):
        scan_env.videos["UC-beta"] = [_video("bbbbbbbbbb2", "Beta talk")]

        with caplog.at_level(logging.INFO, logger="video_intel"):
            cmd_scan(_args(), {"channels": [{"name": "beta", "url": "beta"}]})

        assert not any(r.getMessage().startswith("Search index not rebuilt") for r in caplog.records)
        assert (scan_env.corpus / "_atlas" / "atlas.json").is_file()


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "wrote"),
    [
        ("done", True),
        ("done (captions)", True),
        ("partial (chunks lost: 1 of 3)", True),
        ("partial (quality guard)", True),
        ("skipped (exists)", False),
        ("skipped (mindmap_source=none)", False),
        (video_intel.LIVESTREAM_MINDMAP_SKIP_STATUS, False),
        ("error: 403 PERMISSION_DENIED", False),
    ],
)
def test_scan_status_wrote_artifact(status, wrote):
    assert scan_status_wrote_artifact(status) is wrote


def test_the_index_names_have_one_definition():
    assert video_intel.LANCEDB_DIR is corpus_walk.SEARCH_INDEX_DIRNAME
    assert video_intel.LANCEDB_TABLE is corpus_walk.SEARCH_INDEX_TABLE
    assert atlas_build.SEARCH_INDEX_TABLE is corpus_walk.SEARCH_INDEX_TABLE


class TestReadSearchIndex:
    def test_a_missing_folder_is_absent(self, tmp_path):
        assert atlas_build.read_search_index(tmp_path / "nope") == atlas_build.IndexRead(atlas_build.INDEX_ABSENT)

    def test_a_folder_without_the_table_is_absent(self, tmp_path):
        pytest.importorskip("lancedb")
        assert atlas_build.read_search_index(tmp_path) == atlas_build.IndexRead(atlas_build.INDEX_ABSENT)

    def test_a_real_table_yields_its_video_ids(self, tmp_path):
        _make_index(tmp_path, ["aaaaaaaaaa1", "aaaaaaaaaa1", "bbbbbbbbbb2", ""])
        read = atlas_build.read_search_index(tmp_path)
        assert read.state == atlas_build.INDEX_READ
        assert read.video_ids == frozenset({"aaaaaaaaaa1", "bbbbbbbbbb2"})

    def test_an_index_that_will_not_open_is_unknown_not_absent(self, tmp_path, monkeypatch):
        def connect(_path):
            raise OSError("locked")

        monkeypatch.setitem(sys.modules, "lancedb", types.SimpleNamespace(connect=connect))
        assert atlas_build.read_search_index(tmp_path).state == atlas_build.INDEX_UNKNOWN

    def test_a_short_projection_is_unknown_never_a_partial_set(self, tmp_path, monkeypatch):
        arrow = MagicMock(num_rows=3)
        table = MagicMock()
        table.search.return_value.select.return_value.limit.return_value.to_arrow.return_value = arrow
        table.count_rows.return_value = 5
        db = MagicMock()
        db.list_tables.return_value.tables = [corpus_walk.SEARCH_INDEX_TABLE]
        db.open_table.return_value = table
        monkeypatch.setitem(sys.modules, "lancedb", types.SimpleNamespace(connect=lambda _p: db))
        assert atlas_build.read_search_index(tmp_path) == atlas_build.IndexRead(atlas_build.INDEX_UNKNOWN)

    def test_a_missing_package_is_unknown_not_absent(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "lancedb", None)
        assert atlas_build.read_search_index(tmp_path).state == atlas_build.INDEX_UNKNOWN

    def test_rows_carry_membership_or_null(self, tmp_path):
        corpus = tmp_path / "corpus"
        ch = corpus / "alpha"
        ch.mkdir(parents=True)
        for vid in ("aaaaaaaaaa1", "bbbbbbbbbb2"):
            (ch / f"2026-10-01-{vid}.meta.json").write_text(
                json.dumps({"video_id": vid, "title": vid, "published": "2026-10-01"}), encoding="utf-8"
            )
        with_index = atlas_build.build_rows(corpus, frozenset({"aaaaaaaaaa1"}))
        assert {r["video_id"]: r["in_search_index"] for r in with_index.rows} == {
            "aaaaaaaaaa1": True,
            "bbbbbbbbbb2": False,
        }
        without = atlas_build.build_rows(corpus)
        assert {r["in_search_index"] for r in without.rows} == {None}


@pytest.mark.parametrize(
    ("name", "printed"),
    [
        ("alpha", "alpha"),
        ("my-channel_2", "my-channel_2"),
        ("two words", '"two words"'),
        ("o'brien", '"o\'brien"'),
    ],
)
def test_printed_channel_names_are_pasteable(name, printed):
    assert video_intel._shell_arg(name) == printed
