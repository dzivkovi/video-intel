"""`index --incremental` embeds only what the index does not hold (issue #284).

Real LanceDB throughout; only the Voyage client - the paid boundary - is a
recording fake. Every spend claim is asserted on the texts the fake was asked
to embed, and every "untouched" claim by reading the table back, never by
trusting a return value [core: real-caller].
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_intel as vi

lancedb = pytest.importorskip("lancedb")

# Three entries: one chunk per video (chunk_transcript groups five).
TRANSCRIPT = """# Transcript

[00:00] Speaker: {body} one.
[00:30] Speaker: {body} two.
[01:00] Speaker: {body} three.
"""


def _write_video(channel_dir, *, vid: str, slug: str, body: str) -> None:
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"2026-01-01-{slug}.transcript.md").write_text(TRANSCRIPT.format(body=body), encoding="utf-8")
    (channel_dir / f"2026-01-01-{slug}.meta.json").write_text(
        json.dumps({"video_id": vid, "title": slug, "published": "2026-01-01", "channel": channel_dir.name}),
        encoding="utf-8",
    )


class _RecordingVoyage:
    def __init__(self) -> None:
        self.embedded: list[str] = []
        self.clients = 0

    def client(self):
        self.clients += 1
        return self

    def embed(self, texts, *_a, **_kw):
        texts = list(texts)
        self.embedded.extend(texts)
        return SimpleNamespace(embeddings=[[float(len(t) % 7), 0.5, 0.25, 0.125] for t in texts])


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    out = tmp_path / "corpus"
    _write_video(out / "alpha", vid="a1", slug="alpha-first", body="alpha content")
    _write_video(out / "alpha", vid="", slug="alpha-noid", body="alpha nameless")
    _write_video(out / "beta", vid="b1", slug="beta-first", body="beta content")
    voyage = _RecordingVoyage()
    monkeypatch.setattr(vi, "require_voyageai", lambda: SimpleNamespace(Client=voyage.client))
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")
    db_path = tmp_path / "lancedb"
    return SimpleNamespace(output_dir=out, db_path=db_path, config={"vector_db_dir": str(db_path)}, voyage=voyage)


def _rows(db_path, columns=("video_id", "channel", "text", "source_file")):
    table = lancedb.connect(str(db_path)).open_table(vi.LANCEDB_TABLE)
    return sorted(
        table.search().select(list(columns)).limit(0).to_arrow().to_pylist(), key=lambda r: tuple(map(str, r.values()))
    )


def _incremental(corpus, channel=None):
    corpus.voyage.embedded.clear()
    return vi.build_search_index_incremental(corpus.output_dir, channel_filter=channel, config=corpus.config)


class TestIncrementalSelection:
    def test_an_unchanged_corpus_embeds_nothing_and_builds_no_client(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        clients = corpus.voyage.clients
        before = _rows(corpus.db_path)

        assert _incremental(corpus) == 0
        assert corpus.voyage.embedded == []
        assert corpus.voyage.clients == clients
        assert _rows(corpus.db_path) == before

    def test_a_new_video_embeds_only_its_own_chunks(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        before = _rows(corpus.db_path)
        _write_video(corpus.output_dir / "beta", vid="b2", slug="beta-second", body="beta fresh")

        count = _incremental(corpus)

        assert count == 1
        assert corpus.voyage.embedded and all("beta fresh" in t for t in corpus.voyage.embedded)
        after = _rows(corpus.db_path)
        assert [r for r in after if r["video_id"] != "b2"] == before
        assert sum(1 for r in after if r["video_id"] == "b2") == 1

    def test_an_edited_transcript_is_reembedded_and_its_old_rows_are_gone(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        others_before = [r for r in _rows(corpus.db_path) if r["video_id"] != "a1"]
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")

        _incremental(corpus)

        assert corpus.voyage.embedded and all("alpha rewritten" in t for t in corpus.voyage.embedded)
        after = _rows(corpus.db_path)
        a1 = [r["text"] for r in after if r["video_id"] == "a1"]
        assert len(a1) == 1 and all("alpha rewritten" in t for t in a1)
        assert [r for r in after if r["video_id"] != "a1"] == others_before

    def test_a_blank_id_video_is_keyed_by_file_name_not_reembedded_and_replaced_when_edited(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        assert _incremental(corpus) == 0

        _write_video(corpus.output_dir / "alpha", vid="", slug="alpha-noid", body="alpha renamed body")
        _incremental(corpus)

        blank = [r["text"] for r in _rows(corpus.db_path) if r["video_id"] == ""]
        assert len(blank) == 1 and all("alpha renamed body" in t for t in blank)

    def test_channel_scope_walks_one_channel(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "alpha", vid="a3", slug="alpha-third", body="alpha new")
        _write_video(corpus.output_dir / "beta", vid="b3", slug="beta-third", body="beta new")

        _incremental(corpus, channel="alpha")

        ids = {r["video_id"] for r in _rows(corpus.db_path)}
        assert "a3" in ids and "b3" not in ids
        assert all("alpha new" in t for t in corpus.voyage.embedded)

    def test_a_video_id_shared_by_two_channels_keeps_the_other_folders_rows(self, corpus) -> None:
        _write_video(corpus.output_dir / "beta", vid="a1", slug="alpha-first-copy", body="beta copy")
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")

        _incremental(corpus)

        a1 = [(r["channel"], r["text"]) for r in _rows(corpus.db_path) if r["video_id"] == "a1"]
        assert [t for ch, t in a1 if ch == "beta"] and all("beta copy" in t for ch, t in a1 if ch == "beta")
        assert all("alpha rewritten" in t for ch, t in a1 if ch == "alpha")


class TestRobustness:
    def test_the_same_corpus_under_another_path_embeds_nothing_and_duplicates_nothing(self, corpus, tmp_path) -> None:
        """Blank-id videos are keyed by file name, not the absolute path stored in
        `source_file`, so a remount (another drive letter, another machine) must
        not re-embed them or append a second copy."""
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        before = len(_rows(corpus.db_path))
        moved = tmp_path / "remounted"
        corpus.output_dir.rename(moved)
        corpus.output_dir = moved

        assert _incremental(corpus) == 0
        assert len(_rows(corpus.db_path)) == before

    def test_a_failure_between_delete_and_add_is_repaired_by_a_rerun(self, corpus, monkeypatch, caplog) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")
        table_cls = type(lancedb.connect(str(corpus.db_path)).open_table(vi.LANCEDB_TABLE))
        original_add = table_cls.add

        def boom(self, *a, **k):
            raise RuntimeError("add rejected the batch")

        monkeypatch.setattr(table_cls, "add", boom)
        with caplog.at_level(logging.ERROR, logger="video_intel"), pytest.raises(RuntimeError):
            _incremental(corpus)
        assert not [r for r in _rows(corpus.db_path) if r["video_id"] == "a1"], "the delete committed first"
        assert any("index --incremental" in r.getMessage() for r in caplog.records)

        monkeypatch.setattr(table_cls, "add", original_add)
        _incremental(corpus)
        a1 = [r["text"] for r in _rows(corpus.db_path) if r["video_id"] == "a1"]
        assert len(a1) == 1 and "alpha rewritten" in a1[0]


class TestCodexFindings:
    def test_a_failed_compaction_is_finished_by_a_rerun(self, corpus, monkeypatch) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "beta", vid="b2", slug="beta-second", body="beta fresh")
        table_cls = type(lancedb.connect(str(corpus.db_path)).open_table(vi.LANCEDB_TABLE))
        original = table_cls.optimize

        def boom(self, *a, **k):
            raise RuntimeError("compaction failed")

        monkeypatch.setattr(table_cls, "optimize", boom)
        with pytest.raises(RuntimeError):
            _incremental(corpus)
        monkeypatch.setattr(table_cls, "optimize", original)

        def pending():
            table = lancedb.connect(str(corpus.db_path)).open_table(vi.LANCEDB_TABLE)
            return sum(table.index_stats(ix.name).num_unindexed_rows for ix in table.list_indices())

        assert pending() > 0, "fixture must leave rows the FTS index cannot see"
        assert _incremental(corpus) == 0  # nothing to embed...
        assert pending() == 0  # ...but the compaction is finished

    def test_a_changed_transcript_with_no_chunks_keeps_its_rows_even_when_another_video_changes(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        a1_before = [r for r in _rows(corpus.db_path) if r["video_id"] == "a1"]
        (corpus.output_dir / "alpha" / "2026-01-01-alpha-first.transcript.md").write_text(
            "# Transcript\n\nno timestamped entries any more\n", encoding="utf-8"
        )
        _write_video(corpus.output_dir / "beta", vid="b2", slug="beta-second", body="beta fresh")

        _incremental(corpus)

        after = _rows(corpus.db_path)
        assert [r for r in after if r["video_id"] == "a1"] == a1_before
        assert any(r["video_id"] == "b2" for r in after)


class TestCorrectnessFindings:
    def test_a_mistyped_channel_is_an_error_not_already_current(self, corpus, monkeypatch, capsys) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: corpus.output_dir)
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
            vi.cmd_index(argparse.Namespace(channel="alfa", force=False, incremental=True), corpus.config)

        assert exc.value.code == 1
        assert "Index already current" not in capsys.readouterr().out

    def test_a_backfilled_video_id_replaces_its_blank_id_rows(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "alpha", vid="a9", slug="alpha-noid", body="alpha nameless")

        _incremental(corpus)

        rows = [r for r in _rows(corpus.db_path) if "alpha nameless" in r["text"]]
        assert [r["video_id"] for r in rows] == ["a9"], "one copy, under the backfilled id"


def _pending(db_path) -> int:
    table = lancedb.connect(str(db_path)).open_table(vi.LANCEDB_TABLE)
    return sum(table.index_stats(ix.name).num_unindexed_rows for ix in table.list_indices())


class TestTestingFindings:
    """Each case below was a mutation the testing review showed surviving."""

    def test_an_id_video_and_a_blank_id_video_changed_together_each_keep_one_copy(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")
        _write_video(corpus.output_dir / "alpha", vid="", slug="alpha-noid", body="alpha renamed body")

        _incremental(corpus)

        alpha = [r["text"] for r in _rows(corpus.db_path) if r["channel"] == "alpha"]
        assert len(alpha) == 2
        assert any("alpha rewritten" in t for t in alpha) and any("alpha renamed body" in t for t in alpha)

    def test_delete_predicates_are_batched_scoped_and_escaped(self) -> None:
        ids = {f"id{i:04d}" for i in range(450)}
        preds = vi._incremental_delete_predicates({"alpha": (ids, set())})
        assert len(preds) == 3
        assert all(p.startswith("channel = 'alpha' AND (") for p in preds)
        assert {i for i in ids if any(f"'{i}'" in p for p in preds)} == ids

        (pred,) = vi._incremental_delete_predicates({"o'brien": ({"x'y"}, {"C:/O'Brien/a.transcript.md"})})
        assert pred.startswith("channel = 'o''brien' AND (")
        assert "'x''y'" in pred and "'C:/O''Brien/a.transcript.md'" in pred
        assert " OR " in pred

    def test_a_remounted_corpus_with_an_edited_blank_id_video_keeps_one_copy(self, corpus, tmp_path) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        moved = tmp_path / "remounted"
        corpus.output_dir.rename(moved)
        corpus.output_dir = moved
        _write_video(moved / "alpha", vid="", slug="alpha-noid", body="alpha renamed body")

        _incremental(corpus)

        blank = [r["text"] for r in _rows(corpus.db_path) if r["video_id"] == ""]
        assert len(blank) == 1 and "alpha renamed body" in blank[0]

    def test_a_voyage_failure_leaves_the_changed_videos_rows_in_place(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        before = _rows(corpus.db_path)
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")

        def fail(*_a, **_kw):
            raise RuntimeError("voyage down")

        corpus.voyage.embed = fail
        with pytest.raises(RuntimeError):
            _incremental(corpus)
        assert _rows(corpus.db_path) == before

    def test_a_vector_width_change_refuses_before_deleting_anything(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        before = _rows(corpus.db_path)
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")
        corpus.voyage.embed = lambda texts, *_a, **_kw: SimpleNamespace(embeddings=[[0.0] * 8 for _ in texts])

        with pytest.raises(SystemExit) as exc:
            _incremental(corpus)
        assert exc.value.code == 1
        assert _rows(corpus.db_path) == before

    def test_added_rows_are_compacted_into_the_indexes(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "beta", vid="b2", slug="beta-second", body="beta fresh")

        _incremental(corpus)

        assert _pending(corpus.db_path) == 0

    def test_cmd_index_passes_the_channel_through(self, corpus, monkeypatch) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "alpha", vid="a3", slug="alpha-third", body="alpha new")
        _write_video(corpus.output_dir / "beta", vid="b3", slug="beta-third", body="beta new")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: corpus.output_dir)
        corpus.voyage.embedded.clear()

        vi.cmd_index(argparse.Namespace(channel="alpha", force=False, incremental=True), corpus.config)

        assert corpus.voyage.embedded and all("alpha new" in t for t in corpus.voyage.embedded)


def _old_schema_index(corpus) -> None:
    """An index as code before #284 wrote it: no transcript_sha256 column."""
    vi.build_search_index(corpus.output_dir, config=corpus.config)
    db = lancedb.connect(str(corpus.db_path))
    rows = db.open_table(vi.LANCEDB_TABLE).to_arrow().drop(["transcript_sha256"]).to_pylist()
    db.create_table(vi.LANCEDB_TABLE, data=rows, mode="overwrite")


class TestAnIndexWithoutHashes:
    def test_incremental_adds_new_videos_and_says_how_to_enable_change_detection(self, corpus, caplog) -> None:
        _old_schema_index(corpus)
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")
        _write_video(corpus.output_dir / "beta", vid="b2", slug="beta-second", body="beta fresh")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            _incremental(corpus)

        assert corpus.voyage.embedded and all("beta fresh" in t for t in corpus.voyage.embedded)
        assert any("index --force" in r.getMessage() and "predates" in r.getMessage() for r in caplog.records)
        assert "transcript_sha256" not in {
            f.name for f in lancedb.connect(str(corpus.db_path)).open_table(vi.LANCEDB_TABLE).schema
        }

    def test_channel_rebuild_still_works_on_an_old_index(self, corpus) -> None:
        _old_schema_index(corpus)
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")

        vi.build_search_index(corpus.output_dir, channel_filter="alpha", config=corpus.config)

        assert any("alpha rewritten" in r["text"] for r in _rows(corpus.db_path))

    def test_a_full_rebuild_turns_change_detection_on(self, corpus) -> None:
        _old_schema_index(corpus)
        vi.build_search_index(corpus.output_dir, force=True, config=corpus.config)
        table = lancedb.connect(str(corpus.db_path)).open_table(vi.LANCEDB_TABLE)
        assert "transcript_sha256" in {f.name for f in table.schema}


class TestRefusals:
    def test_no_index_is_refused_before_any_embedding(self, corpus) -> None:
        with pytest.raises(SystemExit) as exc:
            _incremental(corpus)
        assert exc.value.code == 1
        assert corpus.voyage.embedded == [] and corpus.voyage.clients == 0

    def test_a_short_read_of_the_index_is_refused_before_any_embedding(self, corpus, monkeypatch) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "beta", vid="b2", slug="beta-second", body="beta fresh")
        table_cls = type(lancedb.connect(str(corpus.db_path)).open_table(vi.LANCEDB_TABLE))
        monkeypatch.setattr(table_cls, "count_rows", lambda self, *a, **k: 10_000)

        with pytest.raises(SystemExit) as exc:
            _incremental(corpus)
        assert exc.value.code == 1
        assert corpus.voyage.embedded == []

    def test_incremental_and_force_cannot_be_combined(self) -> None:
        script = Path(vi.__file__).resolve()
        proc = subprocess.run(
            [sys.executable, str(script), "index", "--incremental", "--force"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 2
        assert "not allowed with argument" in proc.stderr


def test_cmd_index_routes_incremental(corpus, monkeypatch) -> None:
    vi.build_search_index(corpus.output_dir, config=corpus.config)
    _write_video(corpus.output_dir / "beta", vid="b2", slug="beta-second", body="beta fresh")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: corpus.output_dir)
    corpus.voyage.embedded.clear()

    vi.cmd_index(argparse.Namespace(channel=None, force=False, incremental=True), corpus.config)

    assert corpus.voyage.embedded and all("beta fresh" in t for t in corpus.voyage.embedded)


def test_cmd_index_says_an_unchanged_index_is_current(corpus, monkeypatch, capsys) -> None:
    vi.build_search_index(corpus.output_dir, config=corpus.config)
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: corpus.output_dir)
    capsys.readouterr()

    vi.cmd_index(argparse.Namespace(channel=None, force=False, incremental=True), corpus.config)

    out = capsys.readouterr().out
    assert "Index already current" in out and "No transcripts found" not in out
