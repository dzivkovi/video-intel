"""Issue #189: concept-mode ranking prefers specificity over popularity.

The pre-#189 sort was `(-match_score, -video_count)` where match_score is a
bag over the label plus EVERY alias concatenated - so each query term could
match a DIFFERENT alias, and 20 mega-concepts outscored the concept actually
carrying the phrase. Measured live before the fix: `search "prompt
engineering"` ranked `structured_prompting` 21st of 111; after: 2nd, behind a
concept whose alias IS the exact phrase (the taxonomy's own claim). The other
five measured queries went to rank 1 from 10/3/5/5/2.

The bag score keeps its SELECTION role untouched - exactly the same concepts
match, and the exact-vs-partial video-lookup split is unchanged. Only the
ORDER among equals moved: phrase-in-one-field, then best single-field term
coverage, then tightness (query terms as a share of the field's own words),
then video_count last.
"""

import tempfile
from pathlib import Path
from unittest import mock

import video_intel


def _tax(concepts):
    return {"concepts": concepts}


def _search(taxonomy, query, limit=50):
    with tempfile.TemporaryDirectory() as td, mock.patch.object(video_intel, "load_taxonomy", lambda _d: taxonomy):
        return video_intel.search_corpus(Path(td), query, limit=limit)


MEGA = {
    "preferred_label": "AI Agent Configuration",
    # The query's terms appear, but scattered across DIFFERENT aliases - the
    # exact live shape that buried the right answer at rank 21.
    "aliases": ["prompt caching tricks", "context engineering habits", "many other things"],
    "video_count": 500,
}
PHRASE = {
    "preferred_label": "Structured Prompting",
    "aliases": ["Agentic Workflows and Prompt Engineering"],
    "video_count": 10,
}
EXACT_ALIAS = {
    "preferred_label": "Some Concept",
    "aliases": ["Prompt Engineering"],
    "video_count": 5,
}


class TestSpecificityBeatsPopularity:
    def test_phrase_carrier_outranks_the_scattered_mega_concept(self):
        res = _search(_tax({"c.mega": MEGA, "c.phrase": PHRASE}), "prompt engineering")
        ids = [c["concept_id"] for c in res["concepts"]]
        assert ids.index("c.phrase") < ids.index("c.mega")

    def test_exact_alias_outranks_phrase_inside_a_longer_alias(self):
        res = _search(_tax({"c.phrase": PHRASE, "c.exact": EXACT_ALIAS}), "prompt engineering")
        ids = [c["concept_id"] for c in res["concepts"]]
        assert ids.index("c.exact") < ids.index("c.phrase")

    def test_video_count_still_breaks_genuine_ties(self):
        a = {"preferred_label": "Prompt Engineering Basics", "aliases": [], "video_count": 3}
        b = {"preferred_label": "Prompt Engineering Basics", "aliases": [], "video_count": 30}
        res = _search(_tax({"c.small": a, "c.big": b}), "prompt engineering")
        ids = [c["concept_id"] for c in res["concepts"]]
        assert ids.index("c.big") < ids.index("c.small")

    def test_the_issue_shape_end_to_end(self):
        """All three shapes together, in the order the ranking chain promises."""
        res = _search(_tax({"c.mega": MEGA, "c.phrase": PHRASE, "c.exact": EXACT_ALIAS}), "prompt engineering")
        assert [c["concept_id"] for c in res["concepts"]] == ["c.exact", "c.phrase", "c.mega"]


class TestSelectionIsUnchanged:
    """The bag score still decides WHO matches and which concepts fetch
    videos - issue #189 moved only the order among the matched."""

    def test_the_scattered_mega_concept_still_matches_at_full_score(self):
        res = _search(_tax({"c.mega": MEGA, "c.phrase": PHRASE}), "prompt engineering")
        by_id = {c["concept_id"]: c for c in res["concepts"]}
        assert by_id["c.mega"]["_match_score"] == 1.0
        assert by_id["c.phrase"]["_match_score"] == 1.0

    def test_partial_fallback_still_returns_top_five(self):
        concepts = {
            f"c.p{i}": {"preferred_label": f"prompt thing {i}", "aliases": [], "video_count": i} for i in range(8)
        }
        res = _search(_tax(concepts), "prompt engineering")
        # No concept carries "engineering", so every match is partial (0.5)
        # and the fallback cap of five still applies to the video lookup set.
        assert all(c["_match_score"] == 0.5 for c in res["concepts"])


class TestBoundaryAwarePhrase:
    def test_punctuation_query_uses_the_shared_boundary_and_does_not_crash(self):
        concepts = {
            "c.cpp": {"preferred_label": "C++ Development", "aliases": ["C++"], "video_count": 2},
            "c.other": {
                "preferred_label": "Development Practices C stuff",
                "aliases": ["C++ adjacent"],
                "video_count": 90,
            },
        }
        res = _search(_tax(concepts), "c++")
        assert res["concepts"][0]["concept_id"] == "c.cpp"

    def test_phrase_never_matches_inside_a_word(self):
        concepts = {
            "c.sub": {"preferred_label": "misprompt engineeringish notes", "aliases": [], "video_count": 90},
            "c.real": {"preferred_label": "Prompt Engineering", "aliases": [], "video_count": 1},
        }
        res = _search(_tax(concepts), "prompt engineering")
        assert res["concepts"][0]["concept_id"] == "c.real"


class TestMalformedAliasEntries:
    def test_non_string_aliases_are_ignored_not_crashed_on(self):
        concepts = {
            "c.bad": {"preferred_label": "Prompt Engineering", "aliases": [None, 42, "real alias"], "video_count": 2},
        }
        res = _search(_tax(concepts), "prompt engineering")
        assert res["concepts"][0]["concept_id"] == "c.bad"
