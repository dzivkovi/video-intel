"""Tests for the `search --since` flag.

The hybrid search path must accept a `since_iso` kwarg and push it through
to LanceDB as a pre-rank WHERE clause on the `published` column. Without
pre-rank filtering, top-10 results don't guarantee coverage of a date window
(the issue #10 observation: ramjad 2026-03-20..2026-04-19 had 8 videos but
only 3 surfaced in a single hybrid query).

Concept search also respects `since_iso`: it is a post-rank filter there
since concepts live in json, not in a query-plan-aware store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from video_intel import LANCEDB_TABLE


class _CapturingBuilder:
    """Fake LanceDB search builder that records .where() calls."""

    def __init__(self):
        self.where_clauses: list[str] = []

    def vector(self, _vec):
        return self

    def text(self, _q):
        return self

    def limit(self, _n):
        return self

    def where(self, clause):
        self.where_clauses.append(clause)
        return self

    def to_pandas(self):
        return pd.DataFrame()


class _FakeTable:
    def __init__(self, builder):
        self._builder = builder

    def search(self, **_kwargs):
        return self._builder


class _FakeDB:
    def __init__(self, table):
        self._table = table

    def list_tables(self):
        return SimpleNamespace(tables=[LANCEDB_TABLE])

    def open_table(self, _name):
        return self._table


@pytest.fixture
def fake_lancedb(monkeypatch):
    """Wire up a capturing LanceDB stack and return the builder for assertions."""
    import video_intel as vi

    builder = _CapturingBuilder()
    table = _FakeTable(builder)
    db = _FakeDB(table)

    monkeypatch.setattr(vi, "require_lancedb", lambda: SimpleNamespace(connect=lambda *_a, **_kw: db))
    monkeypatch.setattr(
        vi,
        "require_voyageai",
        lambda: SimpleNamespace(
            Client=lambda: SimpleNamespace(embed=lambda *_a, **_kw: SimpleNamespace(embeddings=[[0.0] * 1024]))
        ),
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")
    return builder


def test_hybrid_search_without_since_applies_no_date_filter(fake_lancedb, tmp_path):
    from video_intel import hybrid_search

    hybrid_search(tmp_path, "tips", config={})

    joined = " ".join(fake_lancedb.where_clauses)
    assert "published" not in joined, "No --since flag means no published filter should be pushed to LanceDB."


def test_hybrid_search_with_since_iso_adds_published_filter(fake_lancedb, tmp_path):
    from video_intel import hybrid_search

    hybrid_search(tmp_path, "tips", since_iso="2026-03-20", config={})

    joined = " ".join(fake_lancedb.where_clauses)
    assert "published >= '2026-03-20'" in joined, (
        f"Expected published filter in WHERE clauses; got: {fake_lancedb.where_clauses}"
    )


def test_hybrid_search_combines_channel_and_since_with_and(fake_lancedb, tmp_path):
    from video_intel import hybrid_search

    hybrid_search(
        tmp_path,
        "tips",
        channel_filter="ramjad",
        since_iso="2026-03-20",
        config={},
    )

    # Both filters must appear in a single combined clause, AND-joined.
    # Calling .where() twice in LanceDB replaces, so we combine locally first.
    assert len(fake_lancedb.where_clauses) == 1, f"Expected one combined WHERE, got {fake_lancedb.where_clauses}"
    clause = fake_lancedb.where_clauses[0]
    assert "channel = 'ramjad'" in clause
    assert "published >= '2026-03-20'" in clause
    assert " AND " in clause


def test_cmd_search_parses_30d_and_passes_iso_to_hybrid(monkeypatch, tmp_path):
    """--since 30d is parsed once in cmd_search and handed to hybrid_search as YYYY-MM-DD."""
    import video_intel as vi

    captured: dict[str, str | None] = {"since_iso": "SENTINEL"}

    def fake_hybrid(_output_dir, _query, *, channel_filter=None, since_iso=None, limit=10, config=None):
        captured["since_iso"] = since_iso
        return []

    monkeypatch.setattr(vi, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)

    args = SimpleNamespace(
        query="tips",
        channel=None,
        limit=None,
        vector=True,
        preview=False,
        min_relevance=0.0,
        since="30d",
    )

    vi.cmd_search(args, config={})

    # The resolved ISO should fall within the last 30 days window — validate shape + freshness.
    expected = (datetime.now(UTC) - timedelta(days=30)).date().isoformat()
    assert captured["since_iso"] == expected, f"Expected {expected}, got {captured['since_iso']}"


def test_cmd_search_absolute_date_passes_through(monkeypatch, tmp_path):
    """--since 2026-03-20 should be forwarded verbatim as the ISO string."""
    import video_intel as vi

    captured: dict[str, str | None] = {"since_iso": None}

    def fake_hybrid(_output_dir, _query, *, channel_filter=None, since_iso=None, limit=10, config=None):
        captured["since_iso"] = since_iso
        return []

    monkeypatch.setattr(vi, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)

    args = SimpleNamespace(
        query="tips",
        channel=None,
        limit=None,
        vector=True,
        preview=False,
        min_relevance=0.0,
        since="2026-03-20",
    )

    vi.cmd_search(args, config={})
    assert captured["since_iso"] == "2026-03-20"


def test_search_corpus_since_filter_drops_older_videos(tmp_path):
    """Concept-mode search post-filters matching videos by published date."""
    from video_intel import search_corpus

    # Minimal taxonomy + one channel with two videos (one old, one new)
    (tmp_path / "taxonomy.json").write_text(
        '{"concepts": {"tips": {"preferred_label": "tips", "aliases": [], "video_count": 2, "domain": ""}}}',
        encoding="utf-8",
    )
    ch = tmp_path / "creator"
    ch.mkdir()

    def _write_video(prefix: str, published: str, video_id: str):
        (ch / f"{prefix}.concepts.json").write_text(
            f'{{"video_id": "{video_id}", "concepts": [{{"concept_id": "tips"}}]}}',
            encoding="utf-8",
        )
        (ch / f"{prefix}.meta.json").write_text(
            f'{{"title": "{prefix}", "published": "{published}", "video_id": "{video_id}"}}',
            encoding="utf-8",
        )
        (ch / f"{prefix}.transcript.md").write_text("x", encoding="utf-8")

    _write_video("2026-01-01-old", "2026-01-01", "vid_old")
    _write_video("2026-04-15-new", "2026-04-15", "vid_new")

    # Without filter: both videos returned
    all_results = search_corpus(tmp_path, "tips")
    assert {v["video_id"] for v in all_results["videos"]} == {"vid_old", "vid_new"}

    # With since_iso: only the new one
    filtered = search_corpus(tmp_path, "tips", since_iso="2026-04-01")
    assert {v["video_id"] for v in filtered["videos"]} == {"vid_new"}
