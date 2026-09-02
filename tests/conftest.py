"""Pytest configuration - adds scripts/ to import path and hosts shared fixtures."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# Import after sys.path is set so the scripts package is resolvable.
from video_intel import LANCEDB_TABLE


class _CapturingBuilder:
    """Fake LanceDB search builder that records .where() / .text() calls."""

    def __init__(self) -> None:
        self.where_clauses: list[str] = []
        self.text_calls: list[str] = []
        self.limit_calls: list[int] = []

    def vector(self, _vec):
        return self

    def text(self, q):
        self.text_calls.append(q)
        return self

    def limit(self, n):
        self.limit_calls.append(n)
        return self

    def where(self, clause):
        self.where_clauses.append(clause)
        return self

    def to_pandas(self):
        return pd.DataFrame()


class _FakeTable:
    def __init__(self, builder) -> None:
        self._builder = builder

    def search(self, **_kwargs):
        return self._builder


class _FakeDB:
    def __init__(self, table) -> None:
        self._table = table

    def list_tables(self):
        return SimpleNamespace(tables=[LANCEDB_TABLE])

    def open_table(self, _name):
        return self._table


class _CapturingVoyage:
    """Fake voyageai.Client that records embed() inputs for assertions."""

    def __init__(self) -> None:
        self.embed_calls: list[list[str]] = []

    def embed(self, texts, *_args, **_kwargs):
        captured = list(texts) if isinstance(texts, list | tuple) else [texts]
        self.embed_calls.append(captured)
        return SimpleNamespace(embeddings=[[0.0] * 1024])


@pytest.fixture
def fake_lancedb(monkeypatch):
    """Wire up a capturing LanceDB stack and return the builder for assertions.

    The builder exposes `.where_clauses` (list[str]) and `.text_calls`
    (list[str]). A `.voyage` attribute is attached so expansion-aware tests
    can inspect `.embed_calls`.
    """
    import video_intel as vi

    builder = _CapturingBuilder()
    table = _FakeTable(builder)
    db = _FakeDB(table)
    voyage = _CapturingVoyage()

    monkeypatch.setattr(vi, "require_lancedb", lambda: SimpleNamespace(connect=lambda *_a, **_kw: db))
    monkeypatch.setattr(
        vi,
        "require_voyageai",
        lambda: SimpleNamespace(Client=lambda: voyage),
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")
    builder.voyage = voyage  # type: ignore[attr-defined]
    return builder
