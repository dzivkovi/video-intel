"""Integration tests: Stage-1 query expansion reaches .text() AND vo.embed().

Unit tests in tests/test_query_expansion.py prove the expander returns the
right string. These tests prove the wiring in `hybrid_search()` actually
hands that expanded string to both the BM25 FTS path (`.text()`) and the
Voyage embedding path (`vo.embed()`), because expanding BM25-only is half
the win — see the "Why expand both BM25 text and embedding input" section
of the plan.
"""

from __future__ import annotations

import json
from types import SimpleNamespace


def _write_taxonomy(tmp_path, *concepts: dict) -> None:
    """Drop a minimal taxonomy.json shaped like load_taxonomy()'s output."""
    data = {
        "version": 1,
        "built_from": len(concepts),
        "concepts": {c["concept_id"]: c for c in concepts},
    }
    (tmp_path / "taxonomy.json").write_text(json.dumps(data), encoding="utf-8")


def _mcp_concept() -> dict:
    return {
        "concept_id": "mcp",
        "preferred_label": "Model Context Protocol",
        "aliases": ["MCP", "mcp-server"],
        "video_count": 1,
        "domain": "",
    }


# ---------------------------------------------------------------------------
# hybrid_search wiring
# ---------------------------------------------------------------------------


def test_hybrid_search_default_expands_both_text_and_embed(fake_lancedb, tmp_path):
    from video_intel import hybrid_search

    _write_taxonomy(tmp_path, _mcp_concept())
    hybrid_search(tmp_path, "what is MCP", config={})

    # BM25 text: should receive the expanded string (original + siblings)
    assert fake_lancedb.text_calls, "hybrid_search never called .text()"
    text_arg = fake_lancedb.text_calls[-1]
    assert text_arg.startswith("what is MCP"), f"original query must prefix the expanded text arg; got {text_arg!r}"
    assert "Model Context Protocol" in text_arg
    assert "mcp-server" in text_arg

    # Embedding input: same expanded string, not the original
    assert fake_lancedb.voyage.embed_calls, "hybrid_search never called vo.embed()"
    embed_input = fake_lancedb.voyage.embed_calls[-1][0]
    assert embed_input == text_arg, (
        f"BM25 text and embed input should be identical under expand=True; got text={text_arg!r} embed={embed_input!r}"
    )


def test_hybrid_search_expand_false_uses_original_query(fake_lancedb, tmp_path):
    from video_intel import hybrid_search

    _write_taxonomy(tmp_path, _mcp_concept())
    hybrid_search(tmp_path, "what is MCP", config={}, expand=False)

    assert fake_lancedb.text_calls[-1] == "what is MCP"
    assert fake_lancedb.voyage.embed_calls[-1] == ["what is MCP"]


def test_hybrid_search_empty_taxonomy_no_op(fake_lancedb, tmp_path):
    """Expansion enabled but taxonomy.json missing — query is unchanged."""
    from video_intel import hybrid_search

    hybrid_search(tmp_path, "reliable agents", config={})

    assert fake_lancedb.text_calls[-1] == "reliable agents"
    assert fake_lancedb.voyage.embed_calls[-1] == ["reliable agents"]


def test_hybrid_search_return_diagnostics_shape(fake_lancedb, tmp_path):
    from video_intel import hybrid_search

    _write_taxonomy(tmp_path, _mcp_concept())
    result = hybrid_search(tmp_path, "what is MCP", config={}, return_diagnostics=True)

    assert isinstance(result, tuple) and len(result) == 2
    hits, diag = result
    assert isinstance(hits, list)
    assert diag["expand_enabled"] is True
    assert diag["original_query"] == "what is MCP"
    assert "Model Context Protocol" in diag["expanded_query"]
    assert len(diag["matches"]) == 1
    assert diag["matches"][0]["concept_id"] == "mcp"


def test_hybrid_search_diagnostics_when_expand_disabled(fake_lancedb, tmp_path):
    from video_intel import hybrid_search

    _write_taxonomy(tmp_path, _mcp_concept())
    _, diag = hybrid_search(
        tmp_path,
        "what is MCP",
        config={},
        expand=False,
        return_diagnostics=True,
    )
    assert diag["expand_enabled"] is False
    assert diag["expanded_query"] == diag["original_query"] == "what is MCP"
    assert diag["matches"] == []


# ---------------------------------------------------------------------------
# cmd_search --vector / --no-expand CLI threading
# ---------------------------------------------------------------------------


def test_cmd_search_default_passes_expand_true(monkeypatch, tmp_path):
    """cmd_search without --no-expand should call hybrid_search(expand=True)."""
    import video_intel as vi

    captured = {"expand": "SENTINEL"}

    def fake_hybrid(_output_dir, _query, **kwargs):
        captured["expand"] = kwargs.get("expand", "MISSING")
        return []

    monkeypatch.setattr(vi, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)

    args = SimpleNamespace(
        query="MCP",
        channel=None,
        limit=None,
        vector=True,
        preview=False,
        min_relevance=0.0,
        since=None,
        no_expand=False,
    )
    vi.cmd_search(args, config={})
    assert captured["expand"] is True


def test_cmd_search_no_expand_flag_disables_expansion(monkeypatch, tmp_path):
    """cmd_search with --no-expand should call hybrid_search(expand=False)."""
    import video_intel as vi

    captured = {"expand": "SENTINEL"}

    def fake_hybrid(_output_dir, _query, **kwargs):
        captured["expand"] = kwargs.get("expand", "MISSING")
        return []

    monkeypatch.setattr(vi, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)

    args = SimpleNamespace(
        query="MCP",
        channel=None,
        limit=None,
        vector=True,
        preview=False,
        min_relevance=0.0,
        since=None,
        no_expand=True,
    )
    vi.cmd_search(args, config={})
    assert captured["expand"] is False


def test_cmd_search_no_expand_attr_missing_defaults_to_expand_true(monkeypatch, tmp_path):
    """If args lacks `no_expand` entirely, the getattr shim makes expand default to True.

    Guards the backward-compat path for any remaining direct-call tests that
    build a SimpleNamespace without the new attribute.
    """
    import video_intel as vi

    captured = {"expand": "SENTINEL"}

    def fake_hybrid(_output_dir, _query, **kwargs):
        captured["expand"] = kwargs.get("expand", "MISSING")
        return []

    monkeypatch.setattr(vi, "hybrid_search", fake_hybrid)
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)

    args = SimpleNamespace(
        query="MCP",
        channel=None,
        limit=None,
        vector=True,
        preview=False,
        min_relevance=0.0,
        since=None,
        # no_expand deliberately absent
    )
    vi.cmd_search(args, config={})
    assert captured["expand"] is True
