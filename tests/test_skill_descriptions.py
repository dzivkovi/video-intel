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
