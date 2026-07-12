"""Tests for scripts/wiki_atlas.py (issue #105).

Contract (the non-dull checklist, enforced at generation):
- wikilinks only where a validated lead-lag relationship exists AND the target
  page exists - sub-threshold chain members render as plain text;
- every leader evidence block carries a timestamped citation;
- small-sample dossiers carry the same honesty language as the report/viz;
- frontmatter stamps on every page so Obsidian Bases can facet-browse.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from scripts.lead_lag_report import Chain, Coverage, CreatorStats, FirstMention, ReportData
from scripts.lead_lag_viz import ROBUST_EXPECTED, SMALL_SAMPLE_EXPECTED
from scripts.wiki_atlas import PROSE_INLINE, PROSE_TODO, build_atlas, slugify, tier_label, write_atlas


def d(iso: str) -> dt.date:
    return dt.date.fromisoformat(iso)


def _mention(creator: str, date: str, **kwargs: object) -> FirstMention:
    defaults: dict[str, object] = {
        "concept_id": "dom.pact",
        "artifact_id": f"{creator}-vid",
        "title": f"{creator} on pact",
        "url": f"https://www.youtube.com/watch?v={creator}-vid",
        "start_seconds": 60,
        "segment_text": "the pact pattern changes everything",
        "as_mentioned": "pact pattern",
    }
    defaults.update(kwargs)
    return FirstMention(source_id=creator, first_date=d(date), **defaults)  # type: ignore[arg-type]


def _data() -> ReportData:
    coverage = {
        "big": Coverage("big", d("2026-01-01"), d("2026-06-30"), 100),
        "steady": Coverage("steady", d("2026-01-01"), d("2026-06-30"), 40),
        "lucky": Coverage("lucky", d("2026-01-01"), d("2026-06-30"), 10),
        "tiny": Coverage("tiny", d("2026-01-01"), d("2026-06-30"), 3),
    }
    stats = {
        "big": CreatorStats("big", firsts=4.0, expected=8.0, eligible_concepts=20),
        "steady": CreatorStats("steady", firsts=10.0, expected=6.0, eligible_concepts=18),
        "lucky": CreatorStats("lucky", firsts=3.0, expected=0.5, eligible_concepts=6),
    }
    chain = Chain(
        concept_id="dom.pact",
        mentions=(
            _mention("tiny", "2026-02-01"),
            _mention("steady", "2026-02-10", segment_text=None, start_seconds=None),
            _mention("big", "2026-03-01"),
        ),
        edges=(("tiny", "steady", 9), ("steady", "big", 19)),
    )
    return ReportData(
        coverage=coverage,
        rankable=frozenset({"big", "steady", "lucky"}),
        stats=stats,
        naive={"big": 12.0, "steady": 9.0, "lucky": 3.0},
        chains=[chain],
        n_concepts_total=50,
        n_concepts_eligible=1,
        params={"min_adopters": 4, "min_eligible": 3, "min_artifacts": 5, "follow_window_days": 90},
    )


class TestPageSet:
    def test_emits_dossier_per_ranked_creator_plus_concepts_index_log(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        assert set(pages) == {
            "creators/big.md",
            "creators/steady.md",
            "creators/lucky.md",
            "concepts/dom-pact.md",
            "index.md",
            "log.md",
        }

    def test_prose_slots_present_in_skeleton(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        assert PROSE_TODO in pages["index.md"]
        assert PROSE_TODO in pages["creators/big.md"]
        assert PROSE_TODO in pages["concepts/dom-pact.md"]
        assert PROSE_INLINE in pages["index.md"]

    def test_write_atlas_round_trip(self, tmp_path):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        write_atlas(pages, tmp_path)
        assert (tmp_path / "creators" / "big.md").read_text(encoding="utf-8") == pages["creators/big.md"]
        assert (tmp_path / "index.md").exists()


class TestNonDullChecklist:
    def test_subthreshold_chain_member_is_plain_text_not_wikilink(self):
        # "tiny" leads the chain but has no dossier: linking it would create a
        # dead wikilink, so it must render as plain text everywhere.
        pages = build_atlas(_data(), min_ranked_concepts=5)
        assert "[[tiny]]" not in pages["concepts/dom-pact.md"]
        assert "| tiny |" in pages["concepts/dom-pact.md"]
        assert "[[tiny]]" not in pages["creators/steady.md"]

    def test_ranked_chain_member_is_wikilinked(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        assert "[[steady]]" in pages["concepts/dom-pact.md"]
        assert "[[big]]" in pages["concepts/dom-pact.md"]

    def test_leader_evidence_has_timestamped_citation(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        page = pages["concepts/dom-pact.md"]
        assert "&t=60" in page
        assert "2026-02-01" in page
        assert '"' in page  # a quoted segment, not a bare link

    def test_small_sample_dossier_carries_honesty_language(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        assert "Small sample" in pages["creators/lucky.md"]
        assert "not a verdict" in pages["creators/lucky.md"]
        assert "Small sample" not in pages["creators/steady.md"]

    def test_frontmatter_stamps(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        assert pages["creators/big.md"].startswith("---\ntype: creator\ncreator: big\n")
        concept = pages["concepts/dom-pact.md"]
        assert concept.startswith("---\ntype: concept\nconcept: dom.pact\n")
        assert "first_covered: 2026-02-01" in concept

    def test_index_lists_creators_in_corrected_order_with_context_slot(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        index = pages["index.md"]
        # lift order: lucky 6.0, steady 1.67, big 0.5
        assert index.index("[[lucky]]") < index.index("[[steady]]") < index.index("[[big]]")
        assert index.count(PROSE_INLINE) == 4  # 3 creators + 1 concept


class TestReviewRegressions:
    """Locks the PR #106 review findings (in-family + Codex convergence)."""

    def test_tied_same_day_co_leader_renders_as_lead_not_follow(self):
        data = _data()
        tied = Chain(
            concept_id="dom.tied",
            mentions=(
                _mention("steady", "2026-04-01", concept_id="dom.tied"),
                _mention("big", "2026-04-01", concept_id="dom.tied"),
                _mention("lucky", "2026-05-01", concept_id="dom.tied"),
            ),
            edges=(("big", "lucky", 30),),
        )
        data.chains.append(tied)
        pages = build_atlas(data, min_ranked_concepts=5)
        big = pages["creators/big.md"]
        assert "Leads on (1 concepts)" in big
        assert "0 days behind" not in big

    def test_follow_beyond_window_is_stated_but_not_wikilinked(self):
        data = _data()
        late = Chain(
            concept_id="dom.late",
            mentions=(
                _mention("steady", "2026-01-05", concept_id="dom.late"),
                _mention("big", "2026-06-24", concept_id="dom.late"),  # 170d > 90d window
            ),
            edges=(),
        )
        data.chains.append(late)
        pages = build_atlas(data, min_ranked_concepts=5)
        big = pages["creators/big.md"]
        assert "170 days behind steady" in big
        assert "170 days behind [[steady]]" not in big

    def test_ranked_creator_without_naive_firsts_renders_unranked_not_zero(self):
        data = _data()
        data.naive.pop("lucky")
        pages = build_atlas(data, min_ranked_concepts=5)
        assert "unranked (no naive firsts)" in pages["creators/lucky.md"]
        assert "| #0 |" not in pages["creators/lucky.md"]

    def test_log_inventory_includes_itself(self):
        pages = build_atlas(_data(), min_ranked_concepts=5)
        assert f"- Pages: {len(pages)}" in pages["log.md"]
        assert "log.md" in pages["log.md"]

    def test_write_atlas_rejects_path_escape(self, tmp_path):
        import pytest

        with pytest.raises(ValueError, match="escapes wiki dir"):
            write_atlas({"../evil.md": "x"}, tmp_path)


class TestHelpers:
    def test_colliding_concept_slugs_get_distinct_pages(self):
        data = _data()
        second = dataclasses.replace(
            data.chains[0],
            concept_id="dom_pact",
            mentions=tuple(dataclasses.replace(m, concept_id="dom_pact") for m in data.chains[0].mentions),
        )
        data.chains.append(second)
        pages = build_atlas(data, min_ranked_concepts=5)
        assert "concepts/dom-pact.md" in pages
        assert "concepts/dom-pact-2.md" in pages

    def test_slugify_safe_for_filesystem_and_wikilinks(self):
        assert slugify("dom.pact") == "dom-pact"
        assert slugify("C++ / .NET (MCP)") == "c-net-mcp"
        assert slugify("...") == "concept"

    def test_tier_labels_match_viz_thresholds(self):
        assert tier_label(ROBUST_EXPECTED) == "robust"
        assert tier_label(SMALL_SAMPLE_EXPECTED - 0.1) == "small sample"
        assert tier_label((SMALL_SAMPLE_EXPECTED + ROBUST_EXPECTED) / 2) == "mid"
