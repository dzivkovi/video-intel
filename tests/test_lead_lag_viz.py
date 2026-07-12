"""Tests for scripts/lead_lag_viz.py (issue #94).

Contract: the page is fully self-contained (no external CDNs) and the ranking
view never presents a small-sample leader without its observed/expected
context (Codex gate constraint from PR #96).
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from scripts.lead_lag_report import Chain, Coverage, CreatorStats, FirstMention, ReportData
from scripts.lead_lag_viz import build_viz_payload, render_html


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


class TestPayload:
    def test_small_sample_and_robust_flags(self):
        payload = build_viz_payload(_data(), min_ranked_concepts=5)
        by_name = {x["creator"]: x for x in payload["leaders"]}
        assert by_name["lucky"]["smallSample"] is True
        assert by_name["lucky"]["robust"] is False
        assert by_name["steady"]["robust"] is True
        assert by_name["big"]["robust"] is True

    def test_most_robust_is_highest_lift_with_enough_expected(self):
        # lucky has the highest lift (6.0) but expected 0.5; steady (1.67) is the
        # highest lift among robust rows with lift > 1 - the Codex constraint.
        payload = build_viz_payload(_data(), min_ranked_concepts=5)
        assert payload["mostRobust"] == "steady"

    def test_leaders_sorted_by_lift_with_naive_rank_attached(self):
        payload = build_viz_payload(_data(), min_ranked_concepts=5)
        lifts = [x["lift"] for x in payload["leaders"]]
        assert lifts == sorted(lifts, reverse=True)
        by_name = {x["creator"]: x for x in payload["leaders"]}
        assert by_name["big"]["naiveRank"] == 1

    def test_chain_marks_sub_threshold_creators_and_links_timestamps(self):
        payload = build_viz_payload(_data(), min_ranked_concepts=5)
        chain = payload["chains"][0]
        leader = chain["mentions"][0]
        assert leader["creator"] == "tiny"
        assert leader["subThreshold"] is True
        assert leader["link"].endswith("t=60")
        # mention without a segment gets no quote and no timestamp param
        no_quote = chain["mentions"][1]
        assert no_quote["quote"] is None
        assert "t=" not in no_quote["link"]

    def test_diagnostics_none_when_fewer_than_two_ranked(self):
        data = _data()
        payload = build_viz_payload(data, min_ranked_concepts=19)  # only big (20) qualifies
        assert payload["diagnostics"]["rhoStart"] is None
        # big's lift is 0.5, below the > 1.0 gate, so no robust leader exists
        assert payload["mostRobust"] is None

    def test_diagnostics_values_computed_over_ranked_set(self):
        # lifts desc [6.0, 1.67, 0.5] vs sizes [10, 40, 100] -> perfect inversion;
        # coverage starts all identical -> tied ranks -> rho undefined -> 0.0
        payload = build_viz_payload(_data(), min_ranked_concepts=5)
        assert payload["diagnostics"]["rhoSize"] == pytest.approx(-1.0)
        assert payload["diagnostics"]["rhoStart"] == pytest.approx(0.0)

    def test_every_leader_lands_in_exactly_one_tier(self):
        payload = build_viz_payload(_data(), min_ranked_concepts=5)
        for x in payload["leaders"]:
            assert x["smallSample"] + x["robust"] <= 1  # disjoint flags

    def test_omitted_ranked_counts_creators_hidden_from_table(self):
        # rankable has 3 creators; min_ranked_concepts=19 keeps only big
        payload = build_viz_payload(_data(), min_ranked_concepts=19)
        assert payload["omittedRanked"] == 2

    def test_kill_rule_derived_from_data_not_hardcoded(self):
        payload = build_viz_payload(_data(), min_ranked_concepts=5)
        kr = payload["killRule"]
        assert kr["creator"] == "big"  # 100 artifacts, largest ranked channel
        assert kr["naiveRank"] == 1
        assert kr["correctedRank"] == 3  # lowest lift of the three ranked
        assert kr["rankedCount"] == 3

    def test_empty_report_data_renders_without_crashing(self):
        data = ReportData(
            coverage={},
            rankable=frozenset(),
            stats={},
            naive={},
            chains=[],
            n_concepts_total=0,
            n_concepts_eligible=0,
            params={"min_adopters": 4, "min_eligible": 3, "min_artifacts": 5, "follow_window_days": 90},
        )
        payload = build_viz_payload(data)
        assert payload["leaders"] == []
        assert payload["killRule"] is None
        html = render_html(payload)
        assert "const DATA =" in html


class TestRenderHtml:
    @pytest.fixture()
    def html(self) -> str:
        return render_html(build_viz_payload(_data(), min_ranked_concepts=5))

    def test_no_external_resources(self, html: str):
        # the only outbound URLs allowed are inside the embedded DATA (YouTube links)
        assert "<script src=" not in html
        assert '<link rel="icon" href="data:,">' in html  # inline favicon, no 404
        assert 'href="http' not in html.split("<script")[0]  # no external stylesheet/font links
        assert 'src="http' not in html
        assert "@import" not in html
        assert "fetch(" not in html

    def test_data_embedded_and_script_safe(self, html: str):
        assert "const DATA =" in html
        # ALL angle brackets in the JSON are unicode-escaped: "<!--" + "<script"
        # would flip the parser into the double-escaped state and blank the page
        start = html.index("const DATA =")
        end = html.index(";", start)
        blob = html[start:end]
        # every angle bracket must be escaped, so the raw characters cannot
        # appear anywhere in the assignment slice (Codex review: the earlier
        # replace()-based form was a tautology)
        assert "<" not in blob
        assert ">" not in blob

    def test_hostile_title_cannot_break_script_context(self):
        data = _data()
        hostile = dataclasses.replace(
            data.chains[0].mentions[0], title="<!--<script>alert(1)</script>", segment_text=None
        )
        data.chains[0] = Chain(data.chains[0].concept_id, (hostile, *data.chains[0].mentions[1:]), data.chains[0].edges)
        html = render_html(build_viz_payload(data, min_ranked_concepts=5))
        start = html.index("const DATA =")
        end = html.index(";", start)
        blob = html[start:end]
        assert "<!--" not in blob
        assert "<script" not in blob

    def test_key_sections_present(self, html: str):
        for marker in ['id="bars"', 'id="tl"', 'id="diag"', 'id="tlist"', 'id="explain"', "<title>"]:
            assert marker in html

    def test_no_em_or_en_dashes(self, html: str):
        assert chr(0x2014) not in html
        assert chr(0x2013) not in html

    def test_ranking_is_tiered_by_evidence(self, html: str):
        # fresh-eye review (Gate 1.5d): the top visual slot must go to the
        # robust tier, so the tier group labels must exist in the renderer
        for marker in ["robust tier", "small sample - leads to verify", "mid tier"]:
            assert marker in html
