"""Mutual-exclusion invariant between the two video-intel skills' trigger phrases.

The split (video-intel-search read-only vs video-intel curate) relies on
Claude Code's skill selector routing each user phrase to exactly one skill.
The description frontmatter of each SKILL.md is the authoritative routing
surface. This test asserts they are disjoint on canonical trigger phrases
so drift from trigger edits is caught before merge.

Test matrix mirrors the 15-row routing verification in the plan
(docs/plans/2026-04-23-001-feat-search-skill-portability-plan.md), locked
as a forcing function for future SKILL.md edits.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
VIDEO_INTEL_PY = REPO_ROOT / "scripts" / "video_intel.py"
SEARCH_SKILL = REPO_ROOT / "skills" / "video-intel-search" / "SKILL.md"
CURATE_SKILL = REPO_ROOT / "skills" / "video-intel" / "SKILL.md"


def _load_description(path: Path) -> str:
    """Parse SKILL.md YAML frontmatter; return the description string."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} does not start with YAML frontmatter"
    _, rest = text.split("---\n", 1)
    frontmatter_yaml, _ = rest.split("\n---\n", 1)
    data = yaml.safe_load(frontmatter_yaml)
    return data["description"]


def _load_body(path: Path) -> str:
    """Return SKILL.md body (post-frontmatter) — mirrors _load_description split."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path} does not start with YAML frontmatter"
    _, rest = text.split("---\n", 1)
    _, body = rest.split("\n---\n", 1)
    return body


# Phrases that belong to video-intel-search (read-only query intent).
# Matching is case-insensitive substring.
SEARCH_TRIGGERS = [
    "find videos about",
    "search my videos",
    "what videos cover",
    "what did",  # "what did [creator] say about"
    "nugget brief",
    "consultant brief",
    "what do creators say",
    "synthesize insights",
    "corpus status",
    "summarize this video",
    "is this worth watching",
    # Verification / fact-check triggers (added 2026-04-25, KD3/KD4):
    "verify whether",
    "fact-check",
    "really say",
    "quote real",
    "find the source",
    # Personalization read-side (issue #117): "why am I seeing this" is a query
    # about the corpus, asked from anywhere - `profile show` writes nothing.
    "why am i seeing",
    "ranking my briefings",
    "show my interest profile",
    "where is my profile",
]

# Phrases that belong to video-intel (ingest/curate intent).
# Matching is case-insensitive substring.
CURATE_TRIGGERS = [
    "scan channel",
    "transcribe this video",
    "backfill",
    "catch up on",
    "dedupe my corpus",
    "find duplicate videos",
    "process this local",
    "run the full pipeline",
    "prune shorts",
    "remove shorts",
    "delete youtube shorts",
    # Personalization write-side (issue #117): `profile init` persists files, so
    # it stays with curate even though its read-side sibling moved to search.
    "set up my profile",
    "persist my profile",
]


@pytest.fixture(scope="module")
def descriptions():
    return {
        "search": _load_description(SEARCH_SKILL).lower(),
        "curate": _load_description(CURATE_SKILL).lower(),
    }


class TestSearchTriggersInSearchOnly:
    """Each search-intent phrase appears in video-intel-search, NOT video-intel."""

    @pytest.mark.parametrize("phrase", SEARCH_TRIGGERS)
    def test_phrase_in_search_skill(self, descriptions, phrase):
        assert phrase.lower() in descriptions["search"], (
            f"search trigger '{phrase}' missing from video-intel-search/SKILL.md description"
        )

    @pytest.mark.parametrize("phrase", SEARCH_TRIGGERS)
    def test_phrase_not_in_curate_skill(self, descriptions, phrase):
        assert phrase.lower() not in descriptions["curate"], (
            f"search trigger '{phrase}' leaked into video-intel/SKILL.md description - "
            "this will cause dual-routing. Remove from curate skill."
        )


class TestCurateTriggersInCurateOnly:
    """Each curate-intent phrase appears in video-intel, NOT video-intel-search."""

    @pytest.mark.parametrize("phrase", CURATE_TRIGGERS)
    def test_phrase_in_curate_skill(self, descriptions, phrase):
        assert phrase.lower() in descriptions["curate"], (
            f"curate trigger '{phrase}' missing from video-intel/SKILL.md description"
        )

    @pytest.mark.parametrize("phrase", CURATE_TRIGGERS)
    def test_phrase_not_in_search_skill(self, descriptions, phrase):
        assert phrase.lower() not in descriptions["search"], (
            f"curate trigger '{phrase}' leaked into video-intel-search/SKILL.md description - "
            "this will cause dual-routing. Remove from search skill."
        )


class TestSkillMetadataSanity:
    def test_search_skill_exists(self):
        assert SEARCH_SKILL.exists(), f"{SEARCH_SKILL} must exist after Unit 3"

    def test_search_skill_name_matches_dir(self):
        path = SEARCH_SKILL
        text = path.read_text(encoding="utf-8")
        _, rest = text.split("---\n", 1)
        frontmatter_yaml, _ = rest.split("\n---\n", 1)
        data = yaml.safe_load(frontmatter_yaml)
        assert data["name"] == "video-intel-search"

    def test_curate_skill_name_is_video_intel(self):
        path = CURATE_SKILL
        text = path.read_text(encoding="utf-8")
        _, rest = text.split("---\n", 1)
        frontmatter_yaml, _ = rest.split("\n---\n", 1)
        data = yaml.safe_load(frontmatter_yaml)
        assert data["name"] == "video-intel"

    def test_quoted_profile_states_match_what_the_code_emits(self):
        """The search skill tells the assistant to read two `profile show` states
        verbatim. Those strings live in `_profile_show`, so prose and code can
        drift silently - a skill telling Claude to look for a string the code
        stopped printing is a doc that quietly stops working."""
        skill = _load_body(SEARCH_SKILL)
        code = VIDEO_INTEL_PY.read_text(encoding="utf-8")
        for state in ("inferred (ephemeral - not on disk)", "IGNORED - file exists but is empty or unparseable"):
            assert state in skill, f"video-intel-search body no longer names the {state!r} state"
            assert state in code, (
                f"video-intel-search quotes the state {state!r} but scripts/video_intel.py no longer emits it"
            )

    def test_curate_body_bounces_profile_show_to_search_skill(self):
        """Mirrors the KD6 bounce pattern: the pointer lives in the curate BODY,
        never its description. A bounce sentence in the description would inject
        read-side vocabulary into the write-side routing surface - the exact
        collision the description mutex exists to prevent."""
        body = _load_body(CURATE_SKILL)
        rows = [line for line in body.splitlines() if "profile show" in line]
        assert rows, "curate body must still document `profile show`"
        assert any("video-intel-search" in row for row in rows), (
            "curate body's `profile show` row no longer names video-intel-search - "
            "a user in the plugin repo loses the pointer to the portable read-only path"
        )

    def test_curate_description_carries_no_read_side_profile_vocabulary(self):
        """Belt-and-braces on the near-miss found in review: the curate
        description said "what is ranking your briefings", one pronoun away from
        the search trigger "ranking my briefings"."""
        description = _load_description(CURATE_SKILL).lower()
        for phrase in ("ranking your briefings", "ranking my briefings", "why am i seeing"):
            assert phrase not in description, (
                f"read-side phrase '{phrase}' leaked into the curate description; "
                "bounce text belongs in the body (see test_curate_skill_routes_verify_intent_to_search_skill)"
            )

    def test_curate_skill_routes_verify_intent_to_search_skill(self):
        """KD6: curate-skill 'Wrong skill' row bounces verification queries.

        AND-style assertion: 'verify quote' AND 'fact-check' AND
        'video-intel-search' must all appear in the body so a silent
        half-rollback (someone removing one phrase while leaving the other)
        breaks the test. Body-only check; the description-substring mutex
        already verifies these phrases are NOT in the curate description.
        """
        body = _load_body(CURATE_SKILL).lower()
        assert "verify quote" in body, (
            "curate skill body missing 'verify quote' bounce phrase - "
            "verification queries will not route to video-intel-search"
        )
        assert "fact-check" in body, (
            "curate skill body missing 'fact-check' bounce phrase - "
            "fact-check queries will not route to video-intel-search"
        )
        assert "video-intel-search" in body, (
            "curate skill body missing 'video-intel-search' pointer - "
            "the bounce row no longer names the destination skill"
        )

    def test_anti_grep_callout_present_in_search_skill_body(self):
        """KD5: anti-grep callout in video-intel-search body names the why.

        Guards the specific regression most likely to bite — someone "tightening"
        the body and accidentally removing the callout. Three substring assertions
        on a single, named callout lock the lesson's "why" (vocabulary mismatch)
        along with the "what" (no grep).
        """
        body = _load_body(SEARCH_SKILL).lower()
        assert "do not" in body, "anti-grep callout missing 'Do not' phrasing in video-intel-search body"
        assert "`grep`" in body, "anti-grep callout missing literal `grep` in backticks in video-intel-search body"
        assert "vocabulary" in body, (
            "anti-grep callout missing 'vocabulary' (the why-it-fails reason) in video-intel-search body"
        )


class TestPersonalizationRoutingSplit:
    """`profile show` (read-only) is reachable from the search skill; `profile
    init` (writes) stays curate-only. Issue #117.

    The split is by WRITE SCOPE, not by topic: both commands concern the same
    two files, so a reader who assumes "personalization lives in one skill"
    would move one of them and break the read-only guarantee.
    """

    def test_search_body_offers_profile_show(self):
        body = _load_body(SEARCH_SKILL)
        assert "profile show" in body, (
            "video-intel-search body must offer `profile show` - it is the read-only "
            "answer to 'why am I seeing this' and needs no channels: (issue #117)"
        )

    def test_search_body_does_not_offer_profile_init_as_a_command(self):
        """The read-only skill must never hand the user a writing command.

        Semantic, not format-coupled (review finding): any line that mentions
        `profile init` is allowed ONLY if it also routes the user onward to the
        curate skill. A line that pairs `profile init` with a runnable invocation
        (`video_intel.py`) and does NOT name the destination is the regression -
        whether it is a fenced block, a table row, or prose.
        """
        body = _load_body(SEARCH_SKILL)
        offending = [
            line
            for line in body.splitlines()
            if "profile init" in line
            and "video_intel.py" in line
            and "video-intel" not in line.replace("video-intel-search", "")  # the curate skill name
        ]
        assert not offending, (
            f"video-intel-search presents a runnable `profile init` without routing to curate: {offending}"
        )

    def test_search_body_routes_profile_init_to_curate(self):
        """Naming the destination is what makes the split navigable rather than
        a dead end for a user who asked to set the profile up."""
        body = _load_body(SEARCH_SKILL)
        init_rows = [line for line in body.splitlines() if "profile init" in line]
        assert init_rows, "video-intel-search body must mention `profile init` to route it onward"
        assert any("video-intel" in row and "curate" in row.lower() for row in init_rows), (
            f"`profile init` is mentioned but not routed to the curate skill: {init_rows}"
        )
