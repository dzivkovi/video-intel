"""Gate: a model cannot become the default without measured evidence on disk.

This is the layer that removes the willpower dependency. A rule in
`specs/agent-rules.md` says a model swap must be A/B'd against the incumbent;
a rule that depends on someone remembering is exactly what failed here - the
gap was written down in a status note and shipped anyway. So the default is
gated on an artifact existing, not on anyone's intent.

What it does NOT do: judge whether the numbers are good. That is a human call,
and pretending a threshold could make it automatic would be false confidence.
It only guarantees the evidence exists, names the model, covers more than one
failure mode, and carries a cost figure - the four things that were missing the
first time this default was chosen.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from video_intel import DEFAULT_MODEL, SKILL_DIR

CARDS = Path(__file__).resolve().parent / "evals" / "model-cards"
_EVALS = Path(__file__).resolve().parent / "evals"
# The real fixture set is gitignored (it names videos from the maintainer's
# private corpus). On a fresh checkout only the template exists, and the
# structural rules below apply to it just as well - so resolve to whichever
# is present rather than failing CI on the absence of a private file.
FIXTURES = _EVALS / "model_fixtures.yaml"
if not FIXTURES.exists():
    FIXTURES = _EVALS / "model_fixtures.yaml.example"


def _card(model: str) -> Path:
    return CARDS / f"{model}.json"


class TestDefaultModelHasAScorecard:
    def test_scorecard_exists(self):
        card = _card(DEFAULT_MODEL)
        assert card.exists(), (
            f"DEFAULT_MODEL={DEFAULT_MODEL!r} has no scorecard at {card}.\n"
            "Run:  python scripts/model_eval.py --candidate "
            f"{DEFAULT_MODEL} --incumbent <the model it replaces>\n"
            "A model default may not be changed on a spec sheet, a vendor "
            "benchmark, or a synthetic microbenchmark. See specs/agent-rules.md sec.6."
        )

    def test_scorecard_actually_scores_the_default_model(self):
        rows = json.loads(_card(DEFAULT_MODEL).read_text(encoding="utf-8"))
        scored = [r for r in rows if r.get("model") == DEFAULT_MODEL and not r.get("error")]
        assert scored, (
            f"The scorecard for {DEFAULT_MODEL!r} contains no successful rows for that "
            "model - it is a stub or every run errored."
        )

    def test_more_than_one_failure_mode_was_probed(self):
        """One fixture is a demo, not an evaluation."""
        rows = json.loads(_card(DEFAULT_MODEL).read_text(encoding="utf-8"))
        fixtures = {r["fixture"] for r in rows if r.get("model") == DEFAULT_MODEL and not r.get("error")}
        assert len(fixtures) >= 2, (
            f"{DEFAULT_MODEL!r} was scored on {sorted(fixtures)} only. Single-axis, "
            "single-fixture evidence is what produced the wrong default the first time."
        )

    def test_cost_was_measured_not_just_quality(self):
        """Price was the dimension skipped entirely on the first attempt."""
        rows = json.loads(_card(DEFAULT_MODEL).read_text(encoding="utf-8"))
        costed = [
            r
            for r in rows
            if r.get("model") == DEFAULT_MODEL and not r.get("error") and r.get("cost_per_video_hour") is not None
        ]
        assert costed, (
            f"No row for {DEFAULT_MODEL!r} carries cost_per_video_hour. Add the model to "
            "scripts/model_eval.py PRICING; a scorecard blind on cost repeats the "
            "original mistake, where the chosen model turned out to be the most "
            "expensive in the lineup."
        )

    def test_the_deep_link_metric_is_present(self):
        """max_gap_s is the metric the corpus's `&t=` links depend on."""
        rows = json.loads(_card(DEFAULT_MODEL).read_text(encoding="utf-8"))
        gaps = [r.get("max_gap_s") for r in rows if r.get("model") == DEFAULT_MODEL and not r.get("error")]
        assert gaps and all(g is not None for g in gaps), (
            "Scorecard rows are missing max_gap_s, the worst-case deep-link error. "
            "That is the dimension that decided this default; a scorecard without it "
            "cannot justify a model choice."
        )


class TestConfigModelIsAlsoGated:
    """The shipped config must not name an unmeasured model either."""

    def test_config_yaml_model_has_a_scorecard(self):
        cfg_path = SKILL_DIR / "config.yaml"
        if not cfg_path.exists():
            pytest.skip("no plugin-local config.yaml (fresh checkout)")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        model = cfg.get("model")
        if not model:
            pytest.skip("config.yaml does not pin a model; DEFAULT_MODEL applies")
        assert _card(model).exists(), (
            f"config.yaml pins model={model!r}, which has no scorecard at {_card(model)}. "
            f"Run scripts/model_eval.py --candidate {model} --incumbent <previous>."
        )

    def test_per_step_overrides_are_gated_too(self):
        """A per-step override is still a production model choice."""
        cfg_path = SKILL_DIR / "config.yaml"
        if not cfg_path.exists():
            pytest.skip("no plugin-local config.yaml (fresh checkout)")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        overrides = cfg.get("models") or {}
        missing = sorted(
            {m for m in overrides.values() if isinstance(m, str) and m.strip() and not _card(m.strip()).exists()}
        )
        assert not missing, (
            f"config.yaml `models:` names unmeasured model(s): {missing}. A per-step "
            "override ships to production the same as the base model."
        )


class TestFixtureSetStaysHonest:
    def test_every_fixture_declares_the_facet_it_probes(self):
        manifest = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
        for fx in manifest["fixtures"]:
            assert (fx.get("facet") or "").strip(), (
                f"Fixture {fx['id']!r} declares no facet. A fixture without a stated "
                "failure mode is a sample, not a test - and the point of the set is "
                "that it forces multiple facets to be considered."
            )

    def test_fixture_ids_are_unique(self):
        manifest = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
        ids = [f["id"] for f in manifest["fixtures"]]
        assert len(ids) == len(set(ids)), f"duplicate fixture ids: {ids}"
