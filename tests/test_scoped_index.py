"""`index --channel` is incremental, not a whole-index overwrite (issue #183).

Before this, `build_search_index` filtered COLLECTION to one channel and then
ran an unconditional `create_table(..., mode="overwrite")`, so a valid
`--channel X` replaced a ~40-channel index with one channel, printed a chunk
count and exited 0. A mistyped channel name was harmless (the empty-records
guard returned first), which made the failure rare and much more confusing.

Real LanceDB throughout; only the Voyage client - the paid network boundary -
is stubbed. Assertions read the table back through `db.open_table(...)` rather
than trusting the function's return value: a stub-LanceDB suite would agree
with the overwrite by construction, which is the checker-agrees-with-writer
class this repo has been burned by (PR #136).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import video_intel as vi

lancedb = pytest.importorskip("lancedb")


TRANSCRIPT = """# Transcript

**Source URL:** https://www.youtube.com/watch?v={vid}

[00:00] Speaker: {body} one.
[00:30] Speaker: {body} two.
[01:00] Speaker: {body} three.
"""


def _write_video(channel_dir, *, vid: str, slug: str, body: str) -> None:
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"2026-01-01-{slug}.transcript.md").write_text(
        TRANSCRIPT.format(vid=vid, body=body), encoding="utf-8"
    )
    (channel_dir / f"2026-01-01-{slug}.meta.json").write_text(
        json.dumps(
            {
                "video_id": vid,
                "title": slug,
                "published": "2026-01-01",
                "video_url": f"https://www.youtube.com/watch?v={vid}",
                "channel": channel_dir.name,
            }
        ),
        encoding="utf-8",
    )


class _RecordingVoyage:
    """Stands in for voyageai.Client, recording every text it was asked to embed."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.embedded: list[str] = []
        self.calls = 0
        self.fail_on = fail_on

    def embed(self, texts, *_args, **_kwargs):
        self.calls += 1
        texts = list(texts)
        if self.fail_on and any(self.fail_on in t for t in texts):
            raise RuntimeError("voyage exploded mid-embed")
        self.embedded.extend(texts)
        return SimpleNamespace(embeddings=[[float(len(t) % 7), 0.5, 0.25, 0.125] for t in texts])


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """Two-channel corpus plus a local vector_db_dir, with Voyage stubbed."""
    output_dir = tmp_path / "corpus"
    _write_video(output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha content")
    _write_video(output_dir / "alpha", vid="a2", slug="alpha-second", body="alpha extra")
    _write_video(output_dir / "beta", vid="b1", slug="beta-first", body="beta content")

    voyage = _RecordingVoyage()
    monkeypatch.setattr(vi, "require_voyageai", lambda: SimpleNamespace(Client=lambda: voyage))
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")

    db_path = tmp_path / "lancedb"
    config = {"vector_db_dir": str(db_path)}
    return SimpleNamespace(output_dir=output_dir, db_path=db_path, config=config, voyage=voyage)


def _rows(db_path):
    db = lancedb.connect(str(db_path))
    table = db.open_table(vi.LANCEDB_TABLE)
    return table.search().select(["video_id", "channel", "text"]).limit(0).to_arrow().to_pylist()


class TestScopedIndexIsIncremental:
    def test_scoped_index_preserves_other_channels_rows(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        before = _rows(corpus.db_path)
        beta_before = [r for r in before if r["channel"] == "beta"]
        assert beta_before, "fixture must produce beta rows"

        # Change alpha's content, then re-index alpha alone.
        _write_video(corpus.output_dir / "alpha", vid="a1", slug="alpha-first", body="alpha rewritten")

        vi.build_search_index(corpus.output_dir, channel_filter="alpha", config=corpus.config)
        after = _rows(corpus.db_path)

        beta_after = [r for r in after if r["channel"] == "beta"]
        assert len(beta_after) == len(beta_before), "a scoped index must not delete other channels"
        assert {r["video_id"] for r in beta_after} == {"b1"}
        assert any("alpha rewritten" in r["text"] for r in after), "alpha rows must reflect the new content"
        assert not any("alpha content" in r["text"] for r in after), "alpha's stale rows must be replaced"

    def test_scoped_index_reembeds_only_that_channel(self, corpus) -> None:
        """The spend assertion. Without it the fix could quietly re-embed the
        whole corpus and every other test would still pass."""
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        corpus.voyage.embedded.clear()

        vi.build_search_index(corpus.output_dir, channel_filter="alpha", config=corpus.config)

        assert corpus.voyage.embedded, "scoped run embedded nothing"
        assert all("alpha" in t for t in corpus.voyage.embedded), corpus.voyage.embedded
        assert not any("beta" in t for t in corpus.voyage.embedded)

    def test_scoped_index_on_missing_table_refuses_before_any_embedding(self, corpus) -> None:
        """The ORDERING is what catches a guard-after-embed regression - an
        exit-code-only assertion passes either way."""
        with pytest.raises(SystemExit) as exc:
            vi.build_search_index(corpus.output_dir, channel_filter="alpha", config=corpus.config)
        assert exc.value.code == 1
        assert corpus.voyage.calls == 0, "refusal must precede the paid Voyage call"

    def test_force_with_channel_does_not_drop_other_channels(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        vi.build_search_index(corpus.output_dir, channel_filter="alpha", force=True, config=corpus.config)
        after = _rows(corpus.db_path)
        assert {r["channel"] for r in after} == {"alpha", "beta"}

    def test_force_without_channel_still_rebuilds_the_whole_corpus(self, corpus) -> None:
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        corpus.voyage.embedded.clear()
        vi.build_search_index(corpus.output_dir, force=True, config=corpus.config)
        assert {r["channel"] for r in _rows(corpus.db_path)} == {"alpha", "beta"}
        assert any("beta" in t for t in corpus.voyage.embedded), "a full --force must re-embed everything"

    def test_embed_failure_on_scoped_run_leaves_old_rows_intact(self, corpus, monkeypatch) -> None:
        """Delete-after-embed: a mid-embed failure must not have already removed
        the channel's rows."""
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        before = _rows(corpus.db_path)

        exploding = _RecordingVoyage(fail_on="alpha")
        monkeypatch.setattr(vi, "require_voyageai", lambda: SimpleNamespace(Client=lambda: exploding))
        with pytest.raises(RuntimeError):
            vi.build_search_index(corpus.output_dir, channel_filter="alpha", config=corpus.config)

        after = _rows(corpus.db_path)
        assert len(after) == len(before)
        assert {r["channel"] for r in after} == {"alpha", "beta"}

    def test_channel_name_with_apostrophe_deletes_only_its_own_rows(self, corpus) -> None:
        _write_video(corpus.output_dir / "o'brien", vid="o1", slug="obrien-first", body="obrien content")
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        assert {r["channel"] for r in _rows(corpus.db_path)} == {"alpha", "beta", "o'brien"}

        alpha_before = [r for r in _rows(corpus.db_path) if r["channel"] == "alpha"]

        _write_video(corpus.output_dir / "o'brien", vid="o1", slug="obrien-first", body="obrien rewritten")
        vi.build_search_index(corpus.output_dir, channel_filter="o'brien", config=corpus.config)

        after = _rows(corpus.db_path)
        assert {r["channel"] for r in after} == {"alpha", "beta", "o'brien"}
        assert len([r for r in after if r["channel"] == "alpha"]) == len(alpha_before)
        assert any("obrien rewritten" in r["text"] for r in after)
        assert not any("obrien content" in r["text"] for r in after)

    def test_a_mistyped_channel_still_destroys_nothing(self, corpus) -> None:
        """This was already safe (the empty-records guard returns first) and
        must stay safe - it is why the real defect stayed hidden so long."""
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        before = _rows(corpus.db_path)
        assert vi.build_search_index(corpus.output_dir, channel_filter="alfa", config=corpus.config) == 0
        assert len(_rows(corpus.db_path)) == len(before)

    def test_scoped_rows_are_searchable_after_the_incremental_write(self, corpus) -> None:
        """Appended rows are brute-force searched and invisible to FTS until the
        table is compacted, so the scoped path must optimize()."""
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        _write_video(corpus.output_dir / "alpha", vid="a3", slug="alpha-third", body="distinctive newcontent")
        vi.build_search_index(corpus.output_dir, channel_filter="alpha", config=corpus.config)

        db = lancedb.connect(str(corpus.db_path))
        table = db.open_table(vi.LANCEDB_TABLE)
        hits = table.search("newcontent", query_type="fts").limit(10).to_list()
        assert hits, "newly added rows must be reachable by FTS after optimize()"


class TestSchemaGuard:
    def test_index_schema_mismatch_reports_both_directions(self) -> None:
        table = SimpleNamespace(schema=[SimpleNamespace(name=n) for n in ("text", "channel", "vector")])
        assert vi.index_schema_mismatch(table, {"text", "channel"}) is None
        assert "new columns not in the index" in vi.index_schema_mismatch(table, {"text", "channel", "brand_new"})
        assert "index columns the new records do not supply" in vi.index_schema_mismatch(table, {"text"})

    def test_a_stale_schema_refuses_before_embedding(self, corpus, monkeypatch) -> None:
        """LanceDB does NOT reliably reject a mismatched add - a record missing
        a column is silently null-filled - so the check has to be explicit, and
        it has to run before the paid call."""
        vi.build_search_index(corpus.output_dir, config=corpus.config)
        monkeypatch.setattr(vi, "index_schema_mismatch", lambda *_a, **_k: "simulated drift")
        corpus.voyage.embedded.clear()
        with pytest.raises(SystemExit) as exc:
            vi.build_search_index(corpus.output_dir, channel_filter="alpha", config=corpus.config)
        assert exc.value.code == 1
        assert corpus.voyage.embedded == [], "schema refusal must precede the paid Voyage call"


class TestEscapeSqlStringLiteral:
    def test_doubles_single_quotes(self) -> None:
        assert vi.escape_sql_string_literal("o'brien") == "o''brien"
        assert vi.escape_sql_string_literal("plain") == "plain"
        assert vi.escape_sql_string_literal("a'b'c") == "a''b''c"

    def test_hybrid_search_uses_the_same_helper(self) -> None:
        """One helper for every predicate site, so a fix at one cannot leave the
        other broken."""
        import ast
        import inspect

        source = inspect.getsource(vi)
        tree = ast.parse(source)
        raw_interpolations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            text = ast.unparse(node)
            if "channel = " not in text:
                continue
            if "escape_sql_string_literal" not in text:
                raw_interpolations.append(text)
        assert not raw_interpolations, (
            "a channel predicate interpolates a raw value; route it through "
            f"escape_sql_string_literal: {raw_interpolations}"
        )
