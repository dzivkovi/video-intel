"""Per-step Gemini model overrides, and the Flash version-detection fix.

Two related contracts land here.

**1. Flash detection across dot-versioned ids.** The original check was the
literal substring ``"gemini-3-flash"``, which matches ONLY the hyphenated 3.0
id. Every dot-versioned Flash (3.1, 3.5, 3.6, 3.7) fell through to the Pro
branch and silently ran at ``thinking_level="low"`` instead of ``"minimal"`` -
re-opening the issue #58 Gate 2 vector where thinking tokens eat the output
budget and truncate a chunked transcript with no API error.

**2. gemini-3.7-flash cannot reach zero thinking, and that turned out not to
matter.** Verified against the live API 2026-08-17: ``thinking_level="minimal"``
returns 400 INVALID_ARGUMENT (low/medium/high only, medium default), and
``thinking_budget=0`` is accepted but IGNORED - 911 thinking tokens still billed
on a TEXT prompt. An earlier revision concluded from that "3.7 must never be the
transcript model". A real A/B (``tests/evals/model-cards/``) reversed it: on
VIDEO input thinking came back 0 for every model, and ``minimal`` collapses
minutes of video into one stamped block, wrecking &t= deep-link precision. 3.7
is now the default for all three steps. The ``models:`` block remains as a lever
for the next candidate, not because the steps currently differ.

The end-to-end tests here deliberately assert the model that reaches the Gemini
call, not the value the resolver returned. A resolver test alone would pass
even if a step function forgot to apply the override - the same
agree-by-construction blindness that let three path defects ship green in
PR #136.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel
from video_intel import (
    DEFAULT_MODEL,
    STEP_MODEL_KEYS,
    _effective_model,
    _make_thinking_config_for_transcript,
    process_concepts,
    process_mindmap,
    process_transcript,
    resolve_step_models,
)


@pytest.fixture(autouse=True)
def clear_step_models():
    """_STEP_MODELS is module-level and set once in main(); isolate every test."""
    video_intel._STEP_MODELS.clear()
    yield
    video_intel._STEP_MODELS.clear()


@pytest.fixture
def sample_video():
    return {
        "video_id": "ZZZ999",
        "url": "https://youtu.be/ZZZ999",
        "title": "Sample title",
        "published": "2026-08-17",
    }


@pytest.fixture
def fake_types():
    """Minimal genai types stub: MediaResolution enum + a real ThinkingConfig."""
    return SimpleNamespace(
        MediaResolution=SimpleNamespace(
            MEDIA_RESOLUTION_LOW="MEDIA_RESOLUTION_LOW",
            MEDIA_RESOLUTION_HIGH="MEDIA_RESOLUTION_HIGH",
        ),
        ThinkingConfig=lambda **kw: SimpleNamespace(**kw),
    )


def _args(model=None):
    return argparse.Namespace(model=model)


# ---------------------------------------------------------------------------
# 1. Thinking-level routing
# ---------------------------------------------------------------------------


class TestFlashVersionDetection:
    """Dot-versioned Flash ids must reach the Flash branch, not the Pro branch."""

    @pytest.mark.parametrize(
        "model",
        [
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.1-flash-live-preview",
            "gemini-3.5-flash",
            "gemini-3.6-flash",
        ],
    )
    def test_minimal_capable_flash_gets_minimal(self, model, fake_types):
        cfg = _make_thinking_config_for_transcript(fake_types, model)
        assert getattr(cfg, "thinking_level", None) == "minimal", (
            f"{model} must route to thinking_level='minimal', got {cfg!r}. "
            "Falling through to the Pro branch ('low') re-opens the issue #58 "
            "Gate 2 truncation vector: thinking tokens bill against the same "
            "64k output cap the transcript competes for."
        )

    def test_gemini_3_7_flash_gets_low_not_minimal(self, fake_types):
        """3.7 rejects MINIMAL with a 400; LOW is its floor."""
        cfg = _make_thinking_config_for_transcript(fake_types, "gemini-3.7-flash")
        assert getattr(cfg, "thinking_level", None) == "low", (
            "gemini-3.7-flash must route to 'low'. Sending 'minimal' returns "
            "400 INVALID_ARGUMENT and would hard-fail every chunked transcript."
        )

    @pytest.mark.parametrize("model", ["gemini-3.1-pro-preview", "gemini-3-pro-image"])
    def test_pro_variants_still_get_low(self, model, fake_types):
        cfg = _make_thinking_config_for_transcript(fake_types, model)
        assert getattr(cfg, "thinking_level", None) == "low"

    def test_two_five_branches_unchanged(self, fake_types):
        flash = _make_thinking_config_for_transcript(fake_types, "gemini-2.5-flash")
        pro = _make_thinking_config_for_transcript(fake_types, "gemini-2.5-pro")
        assert getattr(flash, "thinking_budget", None) == 0
        assert getattr(pro, "thinking_budget", None) == 128

    def test_unknown_model_returns_none(self, fake_types):
        """Unknown ids yield None so the SDK default applies and we don't 400."""
        assert _make_thinking_config_for_transcript(fake_types, "gemini-flash-latest") is None


class TestDefaultModelHasBoundedThinking:
    """DEFAULT_MODEL must never fall through to Gemini's DYNAMIC thinking default.

    This replaces an earlier assertion that the default had to reach ZERO
    thinking. Measurement retired that invariant: on video input, thinking
    tokens came back 0 for every model tested (tests/evals/model-cards/), so
    zero-vs-bounded was never the live variable - it was an artifact of a
    synthetic TEXT benchmark. Worse, chasing zero meant `minimal`, which the
    A/B showed collapses minutes of video into a single stamped block and
    wrecks &t= deep-link precision.

    The real issue #58 Gate 2 invariant is narrower and still binding: the
    15,013 thinking tokens that truncated Tucker chunk 2 came from the
    UNBOUNDED dynamic default. `minimal` and `low` both satisfy it; `None`
    (SDK default) does not.
    """

    def test_default_model_gets_an_explicit_thinking_config(self, fake_types):
        cfg = _make_thinking_config_for_transcript(fake_types, DEFAULT_MODEL)
        assert cfg is not None, (
            f"DEFAULT_MODEL={DEFAULT_MODEL!r} falls through to the SDK's dynamic "
            "thinking default. That is the issue #58 Gate 2 vector: unbounded "
            "thinking consumed 15,013 tokens and silently truncated ~47 minutes "
            "of a transcript with no API error. Add the model to "
            "_make_thinking_config_for_transcript."
        )

    def test_default_model_thinking_level_is_a_bounded_value(self, fake_types):
        cfg = _make_thinking_config_for_transcript(fake_types, DEFAULT_MODEL)
        level = getattr(cfg, "thinking_level", None)
        budget = getattr(cfg, "thinking_budget", None)
        assert level in {"minimal", "low"} or budget is not None, (
            f"DEFAULT_MODEL={DEFAULT_MODEL!r} resolved to thinking_level={level!r}. "
            "Only the bottom levels are acceptable for mechanical transcription; "
            "'medium'/'high' spend output budget on reasoning the task does not need."
        )

    def test_default_model_is_priced_in_the_eval_harness(self):
        """A default with no pricing row makes every future scorecard blind on cost."""
        import model_eval

        assert DEFAULT_MODEL in model_eval.PRICING, (
            f"DEFAULT_MODEL={DEFAULT_MODEL!r} has no entry in model_eval.PRICING, so "
            "scripts/model_eval.py cannot score it on cost - the dimension that was "
            "missed the first time this default was chosen."
        )


# ---------------------------------------------------------------------------
# 2. Resolver precedence
# ---------------------------------------------------------------------------


class TestResolveStepModels:
    def test_reads_models_block(self):
        cfg = {"model": "base-m", "models": {"mindmap": "mm-m", "concepts": "cc-m"}}
        assert resolve_step_models(_args(), cfg) == {"mindmap": "mm-m", "concepts": "cc-m"}

    def test_absent_block_is_empty(self):
        assert resolve_step_models(_args(), {"model": "base-m"}) == {}

    def test_cli_model_flag_suppresses_the_whole_block(self):
        """--model is a whole-run override; honoring it for 2 of 3 steps would
        make the documented 'force Pro when Flash struggles' recovery partial."""
        cfg = {"model": "base-m", "models": {"mindmap": "mm-m", "transcript": "tx-m"}}
        assert resolve_step_models(_args(model="gemini-2.5-pro"), cfg) == {}

    @pytest.mark.parametrize("bad", ["not-a-mapping", ["a", "b"], 42])
    def test_malformed_block_is_ignored_not_raised(self, bad):
        """A typo in an optional knob must never take down a scan."""
        assert resolve_step_models(_args(), {"models": bad}) == {}

    def test_non_string_step_value_ignored_others_kept(self):
        cfg = {"models": {"mindmap": {"nested": 1}, "concepts": "cc-m"}}
        assert resolve_step_models(_args(), cfg) == {"concepts": "cc-m"}

    def test_blank_string_ignored(self):
        assert resolve_step_models(_args(), {"models": {"mindmap": "   "}}) == {}

    def test_unknown_step_key_ignored(self, caplog):
        cfg = {"models": {"mindmapp": "typo-m", "concepts": "cc-m"}}
        assert resolve_step_models(_args(), cfg) == {"concepts": "cc-m"}

    def test_step_keys_match_the_three_gemini_steps(self):
        assert set(STEP_MODEL_KEYS) == {"transcript", "mindmap", "concepts"}


class TestEffectiveModel:
    def test_falls_back_to_caller_model_when_unset(self):
        assert _effective_model("caller-m", "mindmap") == "caller-m"

    def test_override_wins_when_set(self):
        video_intel._STEP_MODELS["mindmap"] = "mm-m"
        assert _effective_model("caller-m", "mindmap") == "mm-m"

    def test_override_is_per_step_not_global(self):
        video_intel._STEP_MODELS["mindmap"] = "mm-m"
        assert _effective_model("caller-m", "transcript") == "caller-m"


# ---------------------------------------------------------------------------
# 3. The override must reach the actual Gemini call
# ---------------------------------------------------------------------------


class TestOverrideReachesTheGeminiCall:
    """Assert the model on the wire, not the resolver's return value.

    A resolver-only suite passes even when a step function never applies the
    override - the caller and the checker would agree by construction while
    production silently billed the wrong model.
    """

    def test_mindmap_uses_the_mindmap_model(self, sample_video, fake_types, tmp_path, monkeypatch):
        video_intel._STEP_MODELS.update({"mindmap": "mm-m", "transcript": "tx-m"})
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["model"] = model
            return "## Topic\n\n* bullet (0:00)\n"

        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="MINDMAP-PROMPT",
            model="caller-m",
            output_dir=tmp_path,
            channel_name="demo",
            source="video",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
        )

        assert captured["model"] == "mm-m", (
            f"process_mindmap sent {captured.get('model')!r}; expected the "
            "models.mindmap override 'mm-m'. The override is applied inside the "
            "step function precisely so no call site can miss it."
        )

    def test_transcript_uses_the_transcript_model(self, sample_video, fake_types, tmp_path, monkeypatch):
        video_intel._STEP_MODELS.update({"mindmap": "mm-m", "transcript": "tx-m"})
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["model"] = model
            raise RuntimeError("stub error to exit early")

        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        _, status = process_transcript(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="TRANSCRIPT-PROMPT",
            model="caller-m",
            channel_dir=tmp_path / "demo",
            prefix="2026-08-17-sample",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
        )

        assert status.startswith("error")
        assert captured["model"] == "tx-m", (
            f"process_transcript sent {captured.get('model')!r}; expected 'tx-m'. "
            "Sending the mindmap model here would put transcription on a model "
            "that cannot disable thinking (issue #58 Gate 2)."
        )

    def test_concepts_uses_the_concepts_model(self, sample_video, fake_types, tmp_path, monkeypatch):
        video_intel._STEP_MODELS.update({"concepts": "cc-m"})
        captured = {}

        def fake_call_gemini_text(client, types, text_content, model, **kw):
            captured["model"] = model
            return '{"concepts": []}'

        monkeypatch.setattr(video_intel, "call_gemini_text", fake_call_gemini_text)

        process_concepts(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            mindmap_text="## Topic\n\n* bullet (0:00)\n",
            taxonomy={"concepts": []},
            model="caller-m",
            output_dir=tmp_path,
            channel_name="demo",
        )

        assert captured["model"] == "cc-m"

    def test_no_overrides_leaves_caller_model_untouched(self, sample_video, fake_types, tmp_path, monkeypatch):
        """Back-compat: an empty map must be a strict no-op on every step."""
        captured = {}

        def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
            captured["model"] = model
            return "## Topic\n\n* bullet (0:00)\n"

        monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)

        process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="MINDMAP-PROMPT",
            model="caller-m",
            output_dir=tmp_path,
            channel_name="demo",
            source="video",
            media_uri="https://generativelanguage.googleapis.com/v1beta/files/abc",
        )

        assert captured["model"] == "caller-m"
