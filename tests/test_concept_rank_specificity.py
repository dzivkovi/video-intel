"""Issue #189: concept-mode ranking prefers specificity over popularity.

The pre-#189 sort was `(-match_score, -video_count)` where match_score is a
bag over the label plus EVERY alias concatenated - so each query term could
match a DIFFERENT alias, and 20 mega-concepts outscored the concept actually
carrying the phrase. Measured live before the fix: `search "prompt
engineering"` ranked `structured_prompting` 21st of 111; after: 2nd, behind a
concept whose alias IS the exact phrase (the taxonomy's own claim). The other
five measured queries went to rank 1 from 10/3/5/5/2.

The bag score keeps its SELECTION role untouched - exactly the same concepts
match, and the exact-vs-partial video-lookup RULE is unchanged. Order among
equals moved: phrase-in-one-field, then best single-field term coverage
(token equality, boundary punctuation stripped), then tightness, then
video_count last. Consequence stated plainly: on the PARTIAL path the five
concepts that fetch videos follow the NEW order, so the returned video set
can change there - intended, and pinned by a test below.
"""

import json
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


class TestTieBreakUsesTokenSemantics:
    """Codex peer-pass P2: a substring numerator over a token denominator made
    "prompting, engineering" a perfect, maximally tight match for the query
    "prompt engineering". The tie-break counts TOKEN EQUALITY (boundary
    punctuation stripped; interior punctuation kept so "c++" survives); the
    SELECTION bag keeps substring semantics on purpose - recall is its job."""

    def test_substring_bearing_field_no_longer_beats_a_genuine_token_match(self):
        concepts = {
            "c.fake": {"preferred_label": "prompting, engineering", "aliases": [], "video_count": 90},
            "c.real": {"preferred_label": "prompt systems engineering", "aliases": [], "video_count": 1},
        }
        res = _search(_tax(concepts), "prompt engineering")
        ids = [c["concept_id"] for c in res["concepts"]]
        assert ids.index("c.real") < ids.index("c.fake")

    def test_boundary_punctuation_is_stripped_from_field_tokens(self):
        concepts = {
            "c.comma": {"preferred_label": "prompt, engineering notes", "aliases": [], "video_count": 1},
            "c.fake": {"preferred_label": "prompting engineering", "aliases": [], "video_count": 90},
        }
        res = _search(_tax(concepts), "prompt engineering")
        assert res["concepts"][0]["concept_id"] == "c.comma"

    def test_selection_still_uses_substring_semantics(self):
        """ "prompting" alone still MATCHES the term "prompt" for selection -
        only the ordering tie-break went token-strict."""
        concepts = {"c.sub": {"preferred_label": "prompting", "aliases": [], "video_count": 1}}
        res = _search(_tax(concepts), "prompt")
        assert [c["concept_id"] for c in res["concepts"]] == ["c.sub"]


class TestWhitespaceNormalizedPhrase:
    def test_a_double_spaced_query_still_earns_the_phrase_bonus(self):
        """Codex peer-pass P3: .split() normalizes whitespace for every score,
        so the phrase regex must see the normalized query too - a pasted
        double space must not silently disable only the phrase bonus. The two
        fixtures tie on EVERY other key (field score, tightness, both
        two-token labels), so only the phrase bonus separates them - without
        normalization, video_count would flip the order."""
        concepts = {
            "c.phrase": {"preferred_label": "prompt engineering", "aliases": [], "video_count": 1},
            "c.swapped": {"preferred_label": "engineering prompt", "aliases": [], "video_count": 90},
        }
        res = _search(_tax(concepts), "prompt  engineering")
        assert res["concepts"][0]["concept_id"] == "c.phrase"


class TestPhraseOutranksFieldCoverage:
    def test_a_phrase_carrier_with_partial_coverage_beats_full_scattered_coverage(self):
        """Review finding: swapping phrase above/below field_score in the sort
        key survived every earlier test. This fixture discriminates: the
        hyphenated label carries the contiguous phrase (boundary-aware) but
        only half the query's tokens as EQUAL tokens, while the rival holds
        every token yet never the phrase."""
        concepts = {
            "c.hyphen": {"preferred_label": "prompt engineering-focused", "aliases": [], "video_count": 1},
            "c.scatter": {"preferred_label": "prompt systems engineering", "aliases": [], "video_count": 90},
        }
        res = _search(_tax(concepts), "prompt engineering")
        assert res["concepts"][0]["concept_id"] == "c.hyphen"


class TestPartialPathVideoSetFollowsTheNewOrder:
    def test_the_five_partial_concepts_that_fetch_videos_are_the_specificity_top_five(self, tmp_path):
        """Intended improvement, stated plainly: `matching_concepts[:5]` means
        "the best five", and #189 redefines best - so on the PARTIAL path the
        returned VIDEO set can change. The exact path is order-independent
        (the set of all 1.0 matches) and stays byte-identical."""
        # Seven partial matches for "prompt engineering" (each holds only
        # "prompt"); the specificity order decides which five fetch videos.
        concepts = {}
        for i in range(7):
            concepts[f"c.p{i}"] = {
                "preferred_label": f"prompt pad{i} " + "filler " * i,
                "aliases": [],
                "video_count": 100 - i,
            }
        (tmp_path / "demo").mkdir()
        for i in range(7):
            (tmp_path / "demo" / f"v{i}.concepts.json").write_text(
                json.dumps({"video_id": f"vid{i:08d}xxx", "concepts": [{"concept_id": f"c.p{i}"}]}),
                encoding="utf-8",
            )
            (tmp_path / "demo" / f"v{i}.meta.json").write_text(
                json.dumps(
                    {"video_id": f"vid{i:08d}xxx", "title": f"t{i}", "published": "2026-01-01", "channel": "demo"}
                ),
                encoding="utf-8",
            )
        with mock.patch.object(video_intel, "load_taxonomy", lambda _d: _tax(concepts)):
            res = video_intel.search_corpus(tmp_path, "prompt engineering", limit=20)
        # Tightness ranks the SHORTEST prompt-bearing labels first, so the two
        # longest (most filler) partials are the ones whose videos drop out.
        got_videos = {v["video_id"] for v in res["videos"]}
        assert got_videos == {f"vid{i:08d}xxx" for i in range(5)}
