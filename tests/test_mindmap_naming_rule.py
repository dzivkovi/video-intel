"""The mindmap prompt must keep naming load-bearing (#228).

Measured 2026-09-18 over 15 videos spanning five content types. The scoring
harness and the full scorecard live in `tests/evals/`; this file pins the two
properties of the PROMPT that the measurement identified, so a later edit
cannot quietly undo them.

Two things worth knowing before changing this prompt:

1. **The EXAMPLE is as strong a signal as the instructions.** Every bullet in
   the original example was a generic description with no proper noun, and the
   model copied that register. Fixing the example alone moved recall (v4);
   fixing the instruction alone moved it more (v5); both together were the
   only variant that improved recall AND held every guard (v1, shipped).
2. **More naming pressure trades away numbers.** A variant that added a
   wrong-versus-right table scored marginally higher on recall and collapsed
   quantitative detail from 60 to 37 - the exact Goodhart failure the guard
   vector exists to catch. Do not add naming emphasis without re-running the
   sweep.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
TRANSCRIPT_PROMPT = PROMPTS / "mindmap-from-transcript.md"
SCORECARD = Path(__file__).resolve().parent / "evals" / "mindmap_v1_scorecard.json"

_BULLET = re.compile(r"^  - (.+)$", re.M)


class TestNamingIsLoadBearing:
    def test_the_prompt_requires_the_name_verbatim(self):
        text = TRANSCRIPT_PROMPT.read_text(encoding="utf-8")
        assert "MUST carry that name verbatim" in text, (
            "the naming rule was softened; measured recall depends on it being mandatory"
        )

    def test_it_says_a_description_is_not_a_substitute(self):
        """The observed failure mode is exactly this substitution:
        `tt-ali/archify` became 'Polished interactive diagrams generated from
        raw codebase'."""
        text = TRANSCRIPT_PROMPT.read_text(encoding="utf-8")
        assert "not a substitute for its name" in text

    def test_the_rule_lives_in_the_bullets_section_not_only_labeling(self):
        """`Preserve proper nouns as-is` was already in Labeling, which governs
        branch and sub-category HEADERS. The loss happens in bullets."""
        text = TRANSCRIPT_PROMPT.read_text(encoding="utf-8")
        bullets_start = text.index("## Bullets")
        next_section = text.index("## ", bullets_start + 3)
        assert "MUST carry that name verbatim" in text[bullets_start:next_section], (
            "the naming rule moved out of the Bullets section"
        )


class TestTheExampleTeachesNaming:
    """A few-shot example outweighs an instruction it contradicts."""

    def test_the_example_bullets_carry_proper_nouns(self):
        text = TRANSCRIPT_PROMPT.read_text(encoding="utf-8")
        example = text[text.index("## Example") :]
        bullets = _BULLET.findall(example)
        assert bullets, "the example has no bullets to learn from"
        named = [b for b in bullets if re.search(r"\b(LanceDB|Cohere Rerank|voyage-3)\b", b)]
        assert len(named) >= 3, (
            "the example went back to describing things without naming them; "
            f"named bullets: {len(named)} of {len(bullets)}"
        )

    def test_the_example_still_shows_quantitative_detail(self):
        """Naming must be IN ADDITION to substance. An example that names
        everything and quantifies nothing teaches the regression that variant
        v2 produced."""
        text = TRANSCRIPT_PROMPT.read_text(encoding="utf-8")
        example = text[text.index("## Example") :]
        assert re.search(r"\d+%", example), "the example lost its quantitative bullet"


class TestTheScorecardIsRecordedAndHonest:
    def test_the_measured_improvement_is_recorded(self):
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        assert card["variant_mean_recall"] > card["control_mean_recall"]
        assert card["guards_hold"] is True

    def test_the_worst_variant_roll_beats_the_best_control_roll(self):
        """A robustness property, NOT a significance claim.

        With 2 control and 3 variant rolls, the exact permutation test gives
        p = 0.10 one-sided (verified independently of the reviewer who raised
        it). The honest wording is "directionally suggestive, not
        established", and `statistics` in the scorecard says so. What this
        test pins is the weaker, checkable thing: the separation is not an
        artifact of picking the best roll on each side."""
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        assert card["variant_worst_roll"] > max(card["control_rolls"])

    def test_the_acceptance_target_is_not_claimed_as_met(self):
        """Issue #228 asks for >=90% recall. This ground truth counts every
        thrice-repeated proper noun in an hour-long interview, which a mindmap
        legitimately will not all carry, so 90% is not reachable against it and
        the shipped prompt does not claim it."""
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        assert card["variant_mean_recall"] < 0.9

    def test_the_model_era_claim_is_recorded_as_RETRACTED(self):
        """This test used to assert the opposite, and that is the point of it.

        It originally pinned "the mindmaps already on disk score higher than
        today's model produces" as the sweep's biggest finding. A free
        corpus-wide rescore refuted it: 0.393 (n=941) preview vs 0.359 (n=930)
        for 3.7, paired within channel +0.017 with preview ahead in only 21 of
        34. The 0.415-vs-0.274 gap was a sampling artifact - the 15-video
        sample was preview-dominated, and the re-run arm fed preview-era
        TRANSCRIPTS into 3.7, a pairing production never runs.

        `historical_on_disk_recall` stays in the scorecard because it is what
        was measured on that sample; what must never come back is the reading
        of it. A future edit that deletes the retraction should fail here.
        """
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        retraction = card.get("historical_on_disk_retraction") or {}
        assert retraction.get("retracted") is True, (
            "the model-era retraction was removed; the raw number is a sampling "
            "artifact and must not be republished as a finding"
        )
        rescore = retraction.get("corpus_wide_rescore") or {}
        assert rescore.get("paired_within_channel_gap") is not None, (
            "the corpus-wide rescore that refuted the claim was removed"
        )

    def test_the_sample_gap_is_not_presented_as_bigger_than_the_prompt_fix(self):
        """The raw sample numbers still show a large gap - that is exactly why
        the retraction has to travel with them. Anyone reading the scorecard
        arithmetic alone reaches the wrong conclusion, so this test asserts the
        pairing rather than the arithmetic."""
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        if "historical_on_disk_recall" in card:
            assert "historical_on_disk_retraction" in card, (
                "historical_on_disk_recall must never appear without its retraction"
            )

    def test_the_scorecard_refuses_to_overstate_the_statistics(self):
        """The claim is the deliverable here, so an over-claim is the defect.
        A future edit that drops the caveat should fail."""
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        stats = card.get("statistics") or {}
        assert stats.get("one_sided_exact_permutation_p") is not None, "the p-value was removed"
        assert stats["one_sided_exact_permutation_p"] >= 0.05, (
            "if this became significant, the wording below should be upgraded deliberately"
        )
        assert "not established" in stats.get("note", "").lower()

    def test_the_control_mean_matches_its_own_rolls(self):
        """It did not: 0.2653 and 0.2833 average to 0.2743, and 0.2745 was
        recorded. Small, but nothing was recomputing it."""
        card = json.loads(SCORECARD.read_text(encoding="utf-8"))
        rolls = card["control_rolls"]
        assert abs(card["control_mean_recall"] - sum(rolls) / len(rolls)) < 1e-6
