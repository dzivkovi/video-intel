"""Tests for the Stage-1 query expander `expand_query_via_taxonomy()`.

The expander's job is to bridge user vocabulary to creator vocabulary by
looking up every query token/phrase against `taxonomy.json` canonical labels
and aliases, then appending the sibling terms (canonical + other aliases)
for each matched concept onto the end of the query string.

Contract being guarded here (see
`docs/plans/2026-04-20-feat-kb-stage1-query-expansion-plan.md`):

- Empty taxonomy returns the query unchanged.
- Matching is case-insensitive.
- Boundary rule is punctuation-aware: an alias matches if it sits between
  start-of-string/end-of-string or non-word characters (so `C++`, `.NET`,
  `(MCP)`, `k3s` all match, and `pro` does NOT match inside `approach`).
- Aliases shorter than MIN_ALIAS_LEN are ignored.
- MAX_ALIAS_ADDITIONS caps how many sibling terms get appended per query.
- Sibling dedup is case-insensitive.
- Original query stays at the front so its tokens dominate BM25.
"""

from __future__ import annotations

from video_intel import (
    MAX_ALIAS_ADDITIONS,
    MIN_ALIAS_LEN,
    expand_query_via_taxonomy,
)

# ---------------------------------------------------------------------------
# Taxonomy fixtures (inline: the unit under test is a pure function)
# ---------------------------------------------------------------------------


def _taxonomy(*concepts: dict) -> dict:
    """Build a minimal taxonomy dict shaped like scripts/video_intel.load_taxonomy()."""
    return {
        "version": 1,
        "built_from": len(concepts),
        "concepts": {c["concept_id"]: c for c in concepts},
    }


def _concept(concept_id: str, label: str, aliases: list[str]) -> dict:
    return {
        "concept_id": concept_id,
        "preferred_label": label,
        "aliases": aliases,
        "video_count": 1,
        "domain": "",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_empty_taxonomy_returns_query_unchanged():
    expanded, matches = expand_query_via_taxonomy("reliable agents", {"concepts": {}})
    assert expanded == "reliable agents"
    assert matches == []


def test_missing_concepts_key_is_treated_as_empty():
    expanded, matches = expand_query_via_taxonomy("reliable agents", {})
    assert expanded == "reliable agents"
    assert matches == []


def test_canonical_label_match_appends_aliases():
    tax = _taxonomy(_concept("mcp", "Model Context Protocol", ["MCP", "mcp-server"]))
    expanded, matches = expand_query_via_taxonomy("what is Model Context Protocol", tax)

    assert expanded.startswith("what is Model Context Protocol"), "original query must prefix expansion"
    # Canonical itself is the matched term; siblings are the aliases
    assert "MCP" in expanded
    assert "mcp-server" in expanded
    assert len(matches) == 1
    rec = matches[0]
    assert rec["concept_id"] == "mcp"
    assert rec["matched_term"].lower() == "model context protocol"
    assert set(rec["added"]) == {"MCP", "mcp-server"}


def test_alias_match_adds_canonical_plus_other_aliases():
    tax = _taxonomy(_concept("mcp", "Model Context Protocol", ["MCP", "mcp-server"]))
    expanded, matches = expand_query_via_taxonomy("what is MCP good for", tax)

    assert expanded.startswith("what is MCP good for")
    assert "Model Context Protocol" in expanded
    assert "mcp-server" in expanded
    # Keep expanded in use — guards against regressions where wiring drops it
    assert len(expanded) > len("what is MCP good for")
    # "MCP" is the matched term — it should NOT also be added as a sibling of itself
    added = matches[0]["added"]
    assert "MCP" not in added
    assert set(added) == {"Model Context Protocol", "mcp-server"}


def test_case_insensitive_alias_match():
    tax = _taxonomy(_concept("mcp", "Model Context Protocol", ["MCP"]))
    # Lowercase query, uppercase alias — the plan calls this out explicitly
    expanded, matches = expand_query_via_taxonomy("what is mcp", tax)
    assert "Model Context Protocol" in expanded
    assert len(matches) == 1


def test_punctuation_heavy_alias_cpp():
    tax = _taxonomy(_concept("lang.cpp", "C plus plus", ["C++"]))
    expanded, matches = expand_query_via_taxonomy("what about C++ vs Rust", tax)
    assert "C plus plus" in expanded
    assert matches[0]["matched_term"] == "C++"


def test_punctuation_heavy_alias_dotnet():
    tax = _taxonomy(_concept("lang.dotnet", "DotNet", [".NET"]))
    expanded, matches = expand_query_via_taxonomy("we use .NET here", tax)
    assert "DotNet" in expanded
    assert matches[0]["matched_term"] == ".NET"
    # Worth asserting: ".NET" alone (as a prefix-punct alias) won't match
    # under stdlib \b — the punctuation-aware boundary is doing the work.
    assert expanded != "we use .NET here"


def test_punctuation_heavy_alias_parens_mcp():
    # Use a query that doesn't also contain the canonical label, so we can
    # prove the parens-bearing alias matches on its own merits.
    tax = _taxonomy(_concept("mcp", "Model Context Protocol", ["(MCP)"]))
    expanded, matches = expand_query_via_taxonomy("just (MCP) basics", tax)
    assert "Model Context Protocol" in expanded
    assert len(matches) == 1
    assert matches[0]["matched_term"] == "(MCP)"


def test_punctuation_heavy_alias_k3s():
    # Use only an alias form so the matched_term assertion is unambiguous
    # (canonical-first scan would otherwise match "K3s" as the canonical).
    tax = _taxonomy(_concept("kube.k3s", "Lightweight Kubernetes", ["k3s"]))
    _, matches = expand_query_via_taxonomy("k8s/k3s setup", tax)
    # Boundary check: "/" is a non-word char, so "k3s" matches here even
    # though it is adjacent to "k8s".
    assert len(matches) == 1
    assert matches[0]["matched_term"] == "k3s"
    assert "Lightweight Kubernetes" in matches[0]["added"]


def test_boundary_rejects_substring_inside_word():
    # Classic false-friend: alias "pro" should NOT match inside "approach"
    tax = _taxonomy(_concept("brand.pro", "Pro Tier", ["pro"]))
    expanded, matches = expand_query_via_taxonomy("what is the right approach", tax)
    assert expanded == "what is the right approach"
    assert matches == []


def test_boundary_rejects_prefix_word():
    # "pro" embedded at the start of "programming" must not match
    tax = _taxonomy(_concept("brand.pro", "Pro Tier", ["pro"]))
    _, matches = expand_query_via_taxonomy("programming lessons", tax)
    assert matches == []


def test_multi_concept_dedup_case_insensitive():
    # Two concepts sharing a sibling phrase in different capitalization.
    # c1's canonical ("Shared Phrase") and c2's alias ("shared phrase") are
    # the same string modulo case — only one should land in siblings.
    tax = _taxonomy(
        _concept("c1", "Shared Phrase", ["alpha-only"]),
        _concept("c2", "Canonical Two", ["trigger2", "shared phrase"]),
    )
    _, matches = expand_query_via_taxonomy("alpha-only and trigger2", tax)

    # All sibling additions across both matches, lowercased:
    all_added_lower = [a.lower() for m in matches for a in m["added"]]
    assert len(all_added_lower) == len(set(all_added_lower)), (
        f"siblings were not deduped case-insensitively: {all_added_lower}"
    )
    # And the shared phrase appears exactly once across both concepts
    assert all_added_lower.count("shared phrase") == 1


def test_max_alias_additions_cap_is_honored():
    # Build a concept with far more aliases than the cap allows
    big_alias_list = [f"alias_{i}" for i in range(MAX_ALIAS_ADDITIONS + 5)]
    tax = _taxonomy(_concept("c1", "Canonical", big_alias_list))
    _, matches = expand_query_via_taxonomy("Canonical thing", tax)

    # Count siblings actually appended: must be <= the cap
    total_added = sum(len(m["added"]) for m in matches)
    assert total_added <= MAX_ALIAS_ADDITIONS, f"expected <= {MAX_ALIAS_ADDITIONS} siblings, got {total_added}"


def test_min_alias_len_excludes_single_char_aliases():
    # MIN_ALIAS_LEN = 2 by default — a single-char alias "X" must be skipped,
    # even if it would match as a token in the query.
    assert MIN_ALIAS_LEN >= 2  # guard against someone loosening the floor
    tax = _taxonomy(_concept("c1", "Experimental Brand", ["X", "xbrand"]))
    expanded, matches = expand_query_via_taxonomy("what is X about", tax)
    # "X" should not trigger a match; "xbrand" isn't in the query so no match
    assert matches == []
    assert expanded == "what is X about"


def test_original_query_is_prefix_of_expanded():
    tax = _taxonomy(_concept("mcp", "Model Context Protocol", ["MCP"]))
    original = "what is MCP"
    expanded, _ = expand_query_via_taxonomy(original, tax)
    assert expanded.startswith(original), "positional dominance matters for BM25 — original query must be at the front"
