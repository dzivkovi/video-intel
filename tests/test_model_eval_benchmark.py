"""`model_eval.py` as the repeatable benchmark for models and prompts (issue #292).

The issue #219 evaluation reached a sound decision, but its evidence was gathered
by hand: repeat rolls typed into a table, a dropped-task roll noticed by eye, and
timestamp accuracy listed as not measured. These tests pin the machinery that
replaces the hand work: rolls with median and spread, hard fails through the
production parser, the cue-level caption offset (only through
`caption_align.entry_offsets`), the candidate-prompt arm, the one decision-rule
function, and a dry run that calls nothing.

No network of any kind: the Gemini client and the caption fetcher are stubbed at
the boundary, and the manifest is synthetic under `tmp_path` [core: real-caller].
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import caption_align
import model_eval
import pytest
from google.genai import types as genai_types
from youtube_captions import CAPTIONS_FAILURE_ABSENT, CAPTIONS_FAILURE_BLOCKED, CaptionsResult

ROOT = Path(__file__).resolve().parent.parent
SEG = 600
START = 1200
VID = "vid00000001"
URL = f"https://www.youtube.com/watch?v={VID}"
NEW = "gemini-3.8-flash"
OLD = "gemini-3.7-flash"
FX = {"id": "fx1", "url": URL, "start": START, "facet": "synthetic"}


def _mmss(t: int) -> str:
    return f"{t // 60:02d}:{t % 60:02d}"


def _envelope(stamps: list[int], *, speakers: bool = True, screen: bool = True, texts: list[str] | None = None) -> str:
    texts = texts or [f"word{i} filler{i} more{i}" for i in range(len(stamps))]
    env: dict = {
        "transcripts": [{"start": _mmss(s), "voice": 1, "text": t} for s, t in zip(stamps, texts, strict=True)]
    }
    env["screen_content"] = [{"start": _mmss(stamps[0]), "text": "slide"}] if screen else []
    env["speakers"] = [{"voice": 1, "name": "Host"}] if speakers else []
    return json.dumps(env)


def _response(text: str, served: str | None = None, *, prompt: int = 5000) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=None,
        model_version=served,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=100,
            total_token_count=prompt + 100,
        ),
    )


class FakeClient:
    """Fakes ``client.models.generate_content``; returns queued texts in order, then repeats the last.

    Records what each call was SENT - the prompt text, the thinking config and
    the media resolution - so a regression that sends the wrong prompt or drops
    a setting cannot ship green [core: real-caller].
    """

    def __init__(self, *texts: str) -> None:
        self._texts = list(texts)
        self.calls: list[str] = []
        self.sent: list[dict] = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        self.calls.append(model)
        prompt = next(p.text for p in contents.parts if getattr(p, "text", None))
        thinking = config.thinking_config.model_dump(mode="json", exclude_none=True) if config.thinking_config else None
        self.sent.append(
            {"model": model, "prompt": prompt, "thinking": thinking, "media": str(config.media_resolution)}
        )
        text = self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]
        return _response(text, served=model)


# ---------------------------------------------------------------------------
# Synthetic transcript and caption track with KNOWN offsets
# ---------------------------------------------------------------------------

SHIFTS = [1, -2, 3, 0, 5, -1, 2, 4, -3, 6]
# |shifts| sorted: 0 1 1 2 2 3 3 4 5 6 -> median (2 + 3) / 2 = 2.5;
# nearest-rank p90 = the ceil(0.9 * 10) = 9th value = 5.
EXPECTED_MEDIAN = 2.5
EXPECTED_P90 = 5


def _cue_starts() -> list[int]:
    return [START + 10 + 30 * i for i in range(len(SHIFTS))]


def _aligned_transcript(n: int = len(SHIFTS)) -> str:
    stamps = [c + s for c, s in zip(_cue_starts()[:n], SHIFTS[:n], strict=True)]
    texts = [f"alpha{i} beta{i} gamma{i}" for i in range(n)]
    return _envelope(stamps, texts=texts)


def _track(extra: list[caption_align.Cue] | None = None) -> model_eval.CaptionTrack:
    cues = [caption_align.Cue(float(c), 3.0, f"alpha{i} beta{i} gamma{i}") for i, c in enumerate(_cue_starts())]
    return model_eval.CaptionTrack(cues=sorted(cues + (extra or []), key=lambda c: c.start))


def _captions_result() -> CaptionsResult:
    return CaptionsResult(
        snippets=[(float(c), f"alpha{i} beta{i} gamma{i}") for i, c in enumerate(_cue_starts())],
        is_generated=True,
        language="en",
        durations=tuple(3.0 for _ in SHIFTS),
    )


def _score(raw: str, **kw) -> dict:
    return model_eval.score(raw, {"prompt": 1000, "candidates": 100}, NEW, SEG, None, start_secs=START, **kw)


# ---------------------------------------------------------------------------
# Hard fails go through the production parser
# ---------------------------------------------------------------------------


class TestHardFails:
    def test_hard_fails_unparseable_text_is_not_envelope(self):
        row = _score("this is not json")

        assert model_eval.hard_fails(row, FX) == [model_eval.HARD_FAIL_NOT_ENVELOPE]

    def test_hard_fails_dict_without_transcripts_is_not_envelope(self):
        row = _score(json.dumps({"summary": "a different JSON shape"}))

        assert model_eval.hard_fails(row, FX) == [model_eval.HARD_FAIL_NOT_ENVELOPE]

    def test_hard_fails_missing_speakers_is_named(self):
        row = _score(_envelope([START, START + 30], speakers=False))

        assert model_eval.hard_fails(row, FX) == [model_eval.HARD_FAIL_MISSING_SPEAKERS]

    def test_hard_fails_missing_screen_content_is_named(self):
        row = _score(_envelope([START, START + 30], screen=False))

        assert model_eval.hard_fails(row, FX) == [model_eval.HARD_FAIL_MISSING_SCREEN]

    def test_hard_fails_same_payload_passes_when_fixture_expects_neither(self):
        row = _score(_envelope([START, START + 30], speakers=False, screen=False))
        fixture = {**FX, "expects_speakers": False, "expects_screen": False}

        assert model_eval.hard_fails(row, fixture) == []

    def test_hard_fails_task_wrapper_missing_one_task_goes_through_production_normalization(self):
        raw = json.dumps(
            [
                {"task": "transcripts", "output": [{"start": _mmss(START), "voice": 1, "text": "hello there"}]},
                {"task": "speakers", "output": [{"voice": 1, "name": "Host"}]},
            ]
        )

        row = _score(raw)

        assert row["envelope_ok"] is True
        assert model_eval.hard_fails(row, FX) == [model_eval.HARD_FAIL_MISSING_SCREEN]

    def test_hard_fails_list_wrapped_envelope_is_still_the_envelope(self):
        raw = "[" + _envelope([START, START + 30]) + "]"

        row = _score(raw)

        assert row["envelope_shape"] == "list-wrapped"
        assert model_eval.hard_fails(row, FX) == []

    def test_hard_fails_generation_error_is_counted(self):
        row = {"model": NEW, "fixture": "fx1", "error": "TimeoutError: slow", "roll": 2}

        assert model_eval.hard_fails(row, FX) == [model_eval.HARD_FAIL_GENERATION]

    def test_hard_fails_legacy_row_without_envelope_ok_claims_nothing(self):
        legacy = {"model": NEW, "fixture": "fx1", "speakers": 2, "screen_content": 1, "max_gap_s": 30}

        assert model_eval.hard_fails(legacy, FX) == []


# ---------------------------------------------------------------------------
# Caption offsets: cue level, only through caption_align.entry_offsets
# ---------------------------------------------------------------------------


class TestCaptionOffset:
    def test_caption_offset_known_shifts_give_hand_computed_median_and_p90(self):
        row = _score(_aligned_transcript(), caption_track=_track())

        assert row["caption_offset_median_s"] == EXPECTED_MEDIAN
        assert row["caption_offset_p90_s"] == EXPECTED_P90
        assert row["caption_aligned"] == len(SHIFTS)
        assert row["caption_unaligned"] == 0

    def test_caption_offset_too_few_aligned_is_none_not_zero(self):
        row = _score(_aligned_transcript(4), caption_track=_track())

        assert row["caption_offset_median_s"] is None
        assert row["caption_offset_p90_s"] is None
        assert row["caption_aligned"] == 4
        assert "4" in row["caption_reason"]

    def test_caption_offset_unaligned_entries_are_counted_and_excluded(self):
        stamps = [c + s for c, s in zip(_cue_starts(), SHIFTS, strict=True)] + [START + 400]
        texts = [f"alpha{i} beta{i} gamma{i}" for i in range(len(SHIFTS))] + ["zzz yyy xxx"]

        row = _score(_envelope(stamps, texts=texts), caption_track=_track())

        assert row["caption_unaligned"] == 1
        assert row["caption_offset_median_s"] == EXPECTED_MEDIAN

    def test_caption_offset_refused_track_is_none_with_reason(self):
        track = model_eval.CaptionTrack(cues=None, reason="caption request refused (IpBlocked)")

        row = _score(_aligned_transcript(), caption_track=track)

        assert row["caption_offset_median_s"] is None
        assert "refused" in row["caption_reason"]

    def test_caption_offset_without_track_makes_no_claim(self):
        row = _score(_aligned_transcript())

        assert row["caption_offset_median_s"] is None
        assert row["caption_reason"]

    def test_caption_offset_only_cues_inside_the_window_reach_the_aligner(self, monkeypatch):
        seen: list[list[caption_align.Cue]] = []
        real = caption_align.entry_offsets

        def spy(entries, cues, **kw):
            seen.append(list(cues))
            return real(entries, cues, **kw)

        monkeypatch.setattr(caption_align, "entry_offsets", spy)
        outside = [caption_align.Cue(100.0, 2.0, "before window"), caption_align.Cue(1900.0, 2.0, "after window")]

        _score(_aligned_transcript(), caption_track=_track(outside))

        assert seen, "the aligner was never called"
        assert all(START <= c.start < START + SEG for c in seen[0])
        assert len(seen[0]) == len(SHIFTS)

    def test_caption_offset_goes_through_caption_align_entry_offsets(self, monkeypatch):
        calls: list[int] = []

        def spy(entries, cues, **kw):
            calls.append(len(entries))
            return [caption_align.EntryOffset(e[0], e[0] - 1.0, 1.0, 1.0) for e in entries]

        monkeypatch.setattr(caption_align, "entry_offsets", spy)

        row = _score(_aligned_transcript(), caption_track=_track())

        assert calls == [len(SHIFTS)]
        assert row["caption_offset_median_s"] == 1.0


class TestNoAlignmentOfItsOwn:
    """`model_eval.py` must not grow a private aligner [core: one-definition]."""

    ALIGNER_NAMES = frozenset(
        {"align_words_to_cues", "align_entries", "entry_offsets", "normalize_words", "_anchor", "_coverage"}
    )

    @staticmethod
    def _defs_and_imports(path: Path) -> tuple[set[str], set[str], set[str]]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        imports = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                imports |= {a.name for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module:
                imports.add(n.module)
        attrs = {
            f"{n.value.id}.{n.attr}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        }
        return defs, imports, attrs

    def test_model_eval_defines_no_alignment_and_imports_no_difflib(self):
        defs, imports, _ = self._defs_and_imports(ROOT / "scripts" / "model_eval.py")

        assert not (defs & self.ALIGNER_NAMES)
        assert "difflib" not in imports

    def test_the_walk_finds_the_aligner_where_it_lives(self):
        """Non-vacuity: the same walk on caption_align.py finds every name and difflib."""
        defs, imports, _ = self._defs_and_imports(ROOT / "scripts" / "caption_align.py")

        assert defs >= self.ALIGNER_NAMES
        assert "difflib" in imports

    def test_model_eval_reaches_the_aligner_through_the_module(self):
        _, _, attrs = self._defs_and_imports(ROOT / "scripts" / "model_eval.py")

        assert "caption_align.entry_offsets" in attrs


# ---------------------------------------------------------------------------
# Rolls, the cache key, and failed rolls
# ---------------------------------------------------------------------------


@pytest.fixture
def no_override(monkeypatch):
    monkeypatch.setattr(model_eval, "prompt_override", lambda _name: None)


def _candidate_arm(tmp_path: Path, text: str = "A candidate transcript prompt.\n") -> model_eval.PromptArm:
    path = tmp_path / "candidate-transcript.md"
    path.write_bytes(text.encode("utf-8"))
    return model_eval.candidate_prompt_arm(path)


class TestCacheKey:
    def test_cache_key_separates_prompts_models_and_rolls(self, tmp_path, no_override):
        client = FakeClient(_envelope([START, START + 30]))
        bundled = model_eval.bundled_prompt()
        candidate = _candidate_arm(tmp_path)
        scratch = tmp_path / "raw"
        scratch.mkdir()

        cells = [(NEW, bundled, 1), (NEW, candidate, 1), (NEW, bundled, 2), (NEW, bundled, 3), (OLD, bundled, 1)]
        for model, prompt, roll in cells:
            model_eval.run_one(client, genai_types, FX, model, None, SEG, scratch, roll=roll, prompt=prompt)

        assert len(client.calls) == 5
        assert len(list(scratch.glob("*.usage.json"))) == 5

    def test_cache_key_cell_is_replayed_only_under_the_same_key(self, tmp_path, no_override):
        client = FakeClient(_envelope([START, START + 30]))
        bundled = model_eval.bundled_prompt()
        scratch = tmp_path / "raw"
        scratch.mkdir()

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch, roll=2, prompt=bundled)
        row = model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch, roll=2, prompt=bundled)

        assert len(client.calls) == 1
        assert row["cache"] == "hit"

    def test_cache_key_changed_prompt_is_a_miss_and_overwrites(self, tmp_path, no_override):
        client = FakeClient(_envelope([START, START + 30]))
        bundled = model_eval.bundled_prompt()
        changed = model_eval.PromptArm(
            label=model_eval.BUNDLED, text=bundled.text + "\nEdited.", sha256="0" * 64, source=bundled.source
        )
        scratch = tmp_path / "raw"
        scratch.mkdir()

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch, prompt=bundled)
        row = model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch, prompt=changed)

        assert len(client.calls) == 2
        assert row["cache"] == "live"
        stored = json.loads((scratch / f"fx1__{NEW}.usage.json").read_text(encoding="utf-8"))
        assert stored[model_eval.CACHE_KEY_FIELD]["prompt_sha256"] == "0" * 64

    def test_cache_key_thinking_level_is_part_of_the_key(self, tmp_path, no_override):
        client = FakeClient(_envelope([START, START + 30]))
        scratch = tmp_path / "raw"
        scratch.mkdir()

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)
        model_eval.run_one(client, genai_types, FX, NEW, "medium", SEG, scratch)

        assert len(client.calls) == 2

    def test_cache_key_legacy_entry_replays_as_roll_one_of_bundled(self, tmp_path, no_override):
        scratch = tmp_path / "raw"
        scratch.mkdir()
        (scratch / f"fx1__{NEW}.json").write_text(_envelope([START, START + 30]), encoding="utf-8")
        (scratch / f"fx1__{NEW}.usage.json").write_text(json.dumps({"prompt": 10}), encoding="utf-8")
        client = FakeClient("unused")

        row = model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)

        assert client.calls == []
        assert row["cache"] == "legacy"
        assert row["roll"] == 1
        assert row["prompt_label"] == model_eval.BUNDLED

    def test_cache_key_legacy_entry_never_serves_a_candidate_arm(self, tmp_path, no_override):
        scratch = tmp_path / "raw"
        scratch.mkdir()
        (scratch / f"fx1__{NEW}.json").write_text(_envelope([START, START + 30]), encoding="utf-8")
        (scratch / f"fx1__{NEW}.usage.json").write_text(json.dumps({"prompt": 10}), encoding="utf-8")
        client = FakeClient(_envelope([START, START + 30]))

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch, prompt=_candidate_arm(tmp_path))

        assert client.calls == [NEW]

    @pytest.mark.parametrize(("label", "roll"), [("candidate:abcd1234", 1), (model_eval.BUNDLED, 2)])
    def test_cached_cell_serves_a_keyless_entry_only_as_roll_one_of_bundled(self, tmp_path, label, roll):
        (tmp_path / f"fx1__{NEW}.json").write_text(_envelope([START, START + 30]), encoding="utf-8")
        (tmp_path / f"fx1__{NEW}.usage.json").write_text(json.dumps({"prompt": 10}), encoding="utf-8")

        served = model_eval.cached_cell(tmp_path, f"fx1__{NEW}", {"any": "key"}, label, roll)

        assert served is None


class TestFailedRolls:
    def test_failed_roll_is_recorded_with_roll_and_arm(self, tmp_path, no_override, monkeypatch):
        def boom(*a, **kw):
            raise TimeoutError("took too long")

        monkeypatch.setattr(model_eval.v, "call_gemini", boom)
        scratch = tmp_path / "raw"
        scratch.mkdir()

        row = model_eval.run_one(FakeClient("x"), genai_types, FX, NEW, None, SEG, scratch, roll=3)

        assert row["error"].startswith("TimeoutError")
        assert row["roll"] == 3
        assert row["prompt_label"] == model_eval.BUNDLED
        assert list(scratch.iterdir()) == [], "a failed roll must not be cached"


class TestRollStats:
    def test_roll_stats_median_and_spread(self):
        assert model_eval.roll_stats([30, 50, 40]) == (40, 20)

    def test_roll_stats_one_roll_spread_is_none_not_zero(self):
        assert model_eval.roll_stats([30]) == (30, None)

    def test_roll_stats_ignores_rolls_without_a_value(self):
        assert model_eval.roll_stats([None, 30, 50]) == (40, 20)

    def test_roll_stats_no_values(self):
        assert model_eval.roll_stats([None]) == (None, None)


# ---------------------------------------------------------------------------
# The decision rule: one function, per-rule verdicts
# ---------------------------------------------------------------------------


def _r(model: str, roll: int, gap, off, *, fixture: str = "fx1", label: str = "bundled", **kw) -> dict:
    row = {
        "fixture": fixture,
        "model": model,
        "prompt_label": label,
        "roll": roll,
        "envelope_shape": "dict",
        "envelope_ok": True,
        "segments": 20,
        "max_gap_s": gap,
        "caption_offset_median_s": off,
        "caption_offset_p90_s": off,
        "screen_content": 1,
        "speakers": 2,
        "thinking_tok": 0,
        "cost_per_video_hour": 0.3,
        "window_exceeded": False,
        "model_served": model,
        "cache": "live",
    }
    row.update(kw)
    return row


MANIFEST = {"fixtures": [FX, {"id": "fx2", "url": URL, "start": 0, "facet": "second"}]}
INC = (OLD, "bundled")
CAND = (NEW, "bundled")


def _incumbent_rolls(fixture: str = "fx1") -> list[dict]:
    # max_gap 40/50/60 -> median 50, spread 20; offset 1/2/3 -> median 2, spread 2
    return [
        _r(OLD, 1, 40, 1.0, fixture=fixture),
        _r(OLD, 2, 50, 2.0, fixture=fixture),
        _r(OLD, 3, 60, 3.0, fixture=fixture),
    ]


def _verdict(rows: list[dict], incumbent=INC, arm=CAND) -> dict:
    return model_eval.decision_rule(rows, MANIFEST, incumbent)["candidates"][arm]


class TestDecisionRule:
    def test_decision_rule_pass_path(self):
        rows = _incumbent_rolls() + [_r(NEW, 1, 55, 3.0), _r(NEW, 2, 60, 3.5), _r(NEW, 3, 70, 4.0)]

        verdict = _verdict(rows)

        assert {rid: s for rid, (s, _) in verdict["rules"].items()} == dict.fromkeys(model_eval.RULE_IDS, "PASS")
        assert verdict["overall"] == "PASS"

    def test_decision_rule_hard_fail_in_one_roll_fails_r1(self):
        rows = _incumbent_rolls() + [_r(NEW, 1, 50, 2.0), _r(NEW, 2, 50, 2.0, speakers=0), _r(NEW, 3, 50, 2.0)]

        verdict = _verdict(rows)

        status, detail = verdict["rules"]["R1"]
        assert status == "FAIL"
        assert "roll 2" in detail and model_eval.HARD_FAIL_MISSING_SPEAKERS in detail
        assert verdict["overall"] == "FAIL"

    def test_decision_rule_failed_generation_fails_r1(self):
        failed = {"fixture": "fx1", "model": NEW, "prompt_label": "bundled", "roll": 3, "error": "TimeoutError: x"}
        rows = _incumbent_rolls() + [_r(NEW, 1, 50, 2.0), _r(NEW, 2, 50, 2.0), failed]

        status, detail = _verdict(rows)["rules"]["R1"]

        assert status == "FAIL"
        assert "TimeoutError" in detail

    def test_decision_rule_gap_beyond_incumbent_spread_fails_r2(self):
        rows = _incumbent_rolls() + [_r(NEW, 1, 71, 2.0), _r(NEW, 2, 72, 2.0), _r(NEW, 3, 73, 2.0)]

        verdict = _verdict(rows)

        assert verdict["rules"]["R2"][0] == "FAIL"
        assert verdict["rules"]["R3"][0] == "PASS"

    def test_decision_rule_offset_beyond_incumbent_spread_fails_r3(self):
        rows = _incumbent_rolls() + [_r(NEW, 1, 50, 4.5), _r(NEW, 2, 50, 5.0), _r(NEW, 3, 50, 5.5)]

        verdict = _verdict(rows)

        assert verdict["rules"]["R3"][0] == "FAIL"
        assert verdict["rules"]["R2"][0] == "PASS"

    def test_decision_rule_no_cost_figure_fails_r4(self):
        rows = _incumbent_rolls() + [_r(NEW, r, 50, 2.0, cost_per_video_hour=None) for r in (1, 2, 3)]

        verdict = _verdict(rows)

        assert verdict["rules"]["R4"][0] == "FAIL"
        assert verdict["overall"] == "FAIL"

    def test_decision_rule_one_incumbent_roll_is_not_evaluated(self):
        rows = [_r(OLD, 1, 50, 2.0), _r(NEW, 1, 50, 2.0)]

        verdict = _verdict(rows)

        assert verdict["rules"]["R2"][0] == "NOT EVALUATED"
        assert "--rolls" in verdict["rules"]["R2"][1]
        assert verdict["rules"]["R3"][0] == "NOT EVALUATED"
        assert verdict["overall"] == "NOT EVALUATED"

    @pytest.mark.parametrize("side", ["candidate", "incumbent"])
    def test_decision_rule_none_offset_on_either_side_is_not_evaluated(self, side):
        inc = _incumbent_rolls()
        cand = [_r(NEW, r, 50, 2.0) for r in (1, 2, 3)]
        for row in inc if side == "incumbent" else cand:
            row["caption_offset_median_s"] = None

        verdict = _verdict(inc + cand)

        assert verdict["rules"]["R3"][0] == "NOT EVALUATED"
        assert verdict["overall"] != "PASS"

    def test_decision_rule_no_incumbent_rows_for_the_fixture_is_not_evaluated(self):
        rows = _incumbent_rolls("fx1") + [_r(NEW, r, 50, 2.0, fixture="fx2") for r in (1, 2, 3)]

        verdict = _verdict(rows)

        assert verdict["fixtures"]["fx2"]["R2"][0] == "NOT EVALUATED"
        assert verdict["overall"] == "NOT EVALUATED"

    def test_decision_rule_candidate_roll_dropped_for_a_missed_window_is_not_evaluated(self):
        rows = _incumbent_rolls() + [
            _r(NEW, 1, 50, 2.0),
            _r(NEW, 2, 10, 0.5, window_exceeded=True),
            _r(NEW, 3, 50, 2.0),
        ]

        verdict = _verdict(rows)

        assert verdict["rules"]["R2"][0] == "NOT EVALUATED"
        assert verdict["rules"]["R1"][0] == "PASS", "window_exceeded is reported, not folded into hard fails"

    def test_decision_rule_legacy_incumbent_roll_does_not_widen_the_spread(self):
        legacy = _r(OLD, 1, 300, 2.0, cache="legacy")
        rows = [legacy, _r(OLD, 2, 50, 2.0), _r(OLD, 3, 52, 2.0)] + [_r(NEW, r, 100, 2.0) for r in (1, 2, 3)]

        verdict = _verdict(rows)

        assert verdict["rules"]["R2"][0] == "FAIL", "a stale legacy roll must not loosen the tolerance"
        assert "legacy" in verdict["rules"]["R2"][1]

    def test_decision_rule_two_names_served_by_one_model_is_not_evaluated(self):
        inc = [{**row, "model_served": NEW} for row in _incumbent_rolls()]
        rows = inc + [_r(NEW, r, 50, 2.0) for r in (1, 2, 3)]

        verdict = _verdict(rows)

        assert verdict["rules"]["R2"][0] == "NOT EVALUATED"
        assert NEW in verdict["rules"]["R2"][1]

    def test_decision_rule_prompt_arm_against_bundled_arm(self):
        bundled = [_r(NEW, r, g, 2.0) for r, g in ((1, 40), (2, 50), (3, 60))]
        cand = [_r(NEW, r, 45, 2.0, label="candidate:abcd1234") for r in (1, 2, 3)]

        verdict = model_eval.decision_rule(bundled + cand, MANIFEST, (NEW, "bundled"))

        assert list(verdict["candidates"]) == [(NEW, "candidate:abcd1234")]
        assert verdict["candidates"][(NEW, "candidate:abcd1234")]["overall"] == "PASS"

    def test_decision_rule_no_candidate_arm(self):
        verdict = model_eval.decision_rule(_incumbent_rolls(), MANIFEST, INC)

        assert verdict["candidates"] == {}


# ---------------------------------------------------------------------------
# The rendered card
# ---------------------------------------------------------------------------


class TestCard:
    def test_card_roll_table_is_generated_with_medians_and_spread(self):
        rows = _incumbent_rolls() + [_r(NEW, r, 50, 2.0) for r in (1, 2, 3)]

        card = model_eval.render(rows, [NEW, OLD], MANIFEST, OLD)

        assert (
            "| roll |"
            in card.splitlines()[next(i for i, x in enumerate(card.splitlines()) if x.startswith("| fixture"))]
        )
        assert "50 (20)" in card, "median (spread) of the incumbent's max_gap_s"
        assert "## Decision rule" in card
        assert "Overall: **PASS**" in card

    def test_card_one_roll_spread_renders_as_unknown(self):
        card = model_eval.render([_r(OLD, 1, 50, 2.0), _r(NEW, 1, 50, 2.0)], [NEW, OLD], MANIFEST, OLD)

        assert "50 (-)" in card
        assert "NOT EVALUATED" in card

    def test_card_failed_roll_is_shown_and_counted(self):
        failed = {"fixture": "fx1", "model": NEW, "prompt_label": "bundled", "roll": 2, "error": "TimeoutError: x"}
        rows = _incumbent_rolls() + [_r(NEW, 1, 50, 2.0), failed, _r(NEW, 3, 50, 2.0)]

        card = model_eval.render(rows, [NEW, OLD], MANIFEST, OLD)

        assert "**ERROR**" in card
        assert "generation_failed" in card

    def test_card_names_a_differing_served_model_in_the_decision(self):
        rows = _incumbent_rolls() + [{**_r(NEW, r, 50, 2.0), "model_served": "gemini-3.9-flash"} for r in (1, 2, 3)]

        card = model_eval.render(rows, [NEW, OLD], MANIFEST, OLD)

        decision = card.split("## Decision rule", 1)[1]
        assert f"`{NEW}` (served gemini-3.9-flash)" in decision

    def test_card_prompt_run_mean_line_does_not_average_two_prompts(self):
        bundled = [_r(NEW, r, 40, 2.0) for r in (1, 2)]
        cand = [_r(NEW, r, 400, 2.0, label="candidate:abcd1234") for r in (1, 2)]

        card = model_eval.render(bundled + cand, [NEW], MANIFEST, (NEW, "bundled"))

        assert "mean max_gap" not in card
        assert "candidate:abcd1234" in card.split("## Decision rule", 1)[1]

    def test_card_mean_line_reads_bundled_arms_only(self):
        rows = [_r(NEW, 1, 40, 2.0), _r(NEW, 1, 400, 2.0, label="candidate:abcd1234"), _r(OLD, 1, 50, 2.0)]

        card = model_eval.render(rows, [NEW, OLD], MANIFEST, OLD)

        assert "mean max_gap **40.0s** vs 50.0s" in card

    def test_card_no_candidate_arm_says_so(self):
        card = model_eval.render(_incumbent_rolls(), [OLD], MANIFEST, OLD)

        assert "No candidate arm" in card

    def test_card_records_prompt_hash_per_arm(self):
        rows = [_r(NEW, 1, 50, 2.0, prompt_sha256="a" * 64, prompt_source="bundled prompts/transcript.md")]
        rows.append(_r(NEW, 1, 50, 2.0, label="candidate:bbbbbbbb", prompt_sha256="b" * 64, prompt_source="cand.md"))

        card = model_eval.render(rows, [NEW], MANIFEST, (NEW, "bundled"))

        assert "b" * 64 in card and "cand.md" in card
        assert "a" * 64 in card

    def test_card_states_cue_level_offsets(self):
        card = model_eval.render([_r(NEW, 1, 50, 2.0)], [NEW], MANIFEST, None)

        assert "cue level" in card.lower()

    def test_card_old_rows_without_rolls_or_offsets_still_render(self):
        """Rows written before issue #292: no roll, no prompt label, no offsets, no envelope_ok."""
        old = [
            {"fixture": "fx1", "model": m, "envelope_shape": "dict", "segments": 20, "max_gap_s": g,
             "screen_content": 1, "speakers": 2, "thinking_tok": 0, "cost_per_video_hour": 0.3,
             "window_exceeded": None}
            for m, g in ((NEW, 30), (OLD, 40))
        ]  # fmt: skip

        card = model_eval.render(old, [NEW, OLD], MANIFEST, OLD)

        decision = card.split("## Decision rule", 1)[1]
        assert "R1 no hard fail in any roll: PASS" in decision
        assert "R2" in decision and "NOT EVALUATED" in decision
        assert "insufficient data" not in card


# ---------------------------------------------------------------------------
# The CLI: arms, the candidate prompt, the dry run, the lazy client
# ---------------------------------------------------------------------------


@pytest.fixture
def harness(tmp_path, monkeypatch, no_override):
    """A synthetic manifest and card folder, plus spies on both paid/network boundaries."""
    manifest = tmp_path / "model_fixtures.yaml"
    manifest.write_text(
        json.dumps(
            {
                "defaults": {"segment_seconds": SEG},
                "fixtures": [FX, {"id": "fx2", "url": URL, "start": 0, "facet": "second"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(model_eval, "FIXTURES", manifest)
    monkeypatch.setattr(model_eval, "CARDS", tmp_path / "cards")
    spies = SimpleNamespace(clients=[], fetches=[], client=FakeClient(_aligned_transcript()))

    def factory(key):
        spies.clients.append(key)
        return spies.client

    def fetcher(video_id, *, reason_sink=None):
        spies.fetches.append(video_id)
        return _captions_result()

    monkeypatch.setattr(model_eval.v, "create_client", factory)
    monkeypatch.setattr(model_eval.v, "fetch_english_captions", fetcher)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return spies


def _estimate(out: str) -> float:
    line = next(x for x in out.splitlines() if x.startswith("Estimated cost"))
    return float(line.split("$", 1)[1].split()[0])


class TestCli:
    @pytest.mark.parametrize("value", ["0", "-1", "x"])
    def test_cli_rolls_below_one_is_rejected(self, value):
        with pytest.raises(SystemExit) as exc:
            model_eval.build_parser().parse_args(["--candidate", NEW, "--rolls", value])

        assert exc.value.code == 2

    def test_cli_rolls_defaults_to_one(self):
        assert model_eval.build_parser().parse_args(["--candidate", NEW]).rolls == 1

    def test_arms_two_models(self, tmp_path, no_override):
        arms, inc = model_eval.resolve_arms(NEW, OLD, model_eval.bundled_prompt(), None)

        assert [(m, p.label) for m, p in arms] == [(NEW, "bundled"), (OLD, "bundled")]
        assert inc == (OLD, "bundled")

    def test_arms_candidate_prompt_adds_a_third_arm(self, tmp_path, no_override):
        cand = _candidate_arm(tmp_path)

        arms, _ = model_eval.resolve_arms(NEW, OLD, model_eval.bundled_prompt(), cand)

        assert [(m, p.label) for m, p in arms][-1] == (NEW, cand.label)
        assert cand.label == f"candidate:{cand.sha256[:8]}"

    def test_arms_incumbent_bundled_is_the_candidate_models_bundled_arm(self, tmp_path, no_override):
        arms, inc = model_eval.resolve_arms(NEW, "bundled", model_eval.bundled_prompt(), _candidate_arm(tmp_path))

        assert inc == (NEW, "bundled")
        assert len(arms) == 2

    def test_arms_default_incumbent_is_default_model(self, no_override):
        arms, inc = model_eval.resolve_arms(NEW, None, model_eval.bundled_prompt(), None)

        assert inc == (model_eval.v.DEFAULT_MODEL, "bundled")
        assert (model_eval.v.DEFAULT_MODEL, "bundled") in [(m, p.label) for m, p in arms]

    def test_candidate_prompt_hash_is_the_sha256_of_the_text_sent(self, tmp_path, no_override):
        import hashlib

        cand = _candidate_arm(tmp_path, "exact bytes\n")

        assert cand.sha256 == hashlib.sha256(b"exact bytes\n").hexdigest()
        assert cand.source == "candidate-transcript.md"

    def test_missing_candidate_prompt_is_refused_before_any_call(self, harness, tmp_path):
        code = model_eval.main(["--candidate", NEW, "--candidate-prompt", str(tmp_path / "nope.md")])

        assert code == 2
        assert harness.clients == [] and harness.fetches == [] and harness.client.calls == []

    def test_dry_run_estimate_scales_with_rolls_and_arms_and_calls_nothing(self, harness, tmp_path, capsys):
        model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--dry-run"])
        one = _estimate(capsys.readouterr().out)
        model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--rolls", "3", "--dry-run"])
        out3 = capsys.readouterr().out
        cand = tmp_path / "c.md"
        cand.write_text("prompt", encoding="utf-8")
        model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--candidate-prompt", str(cand), "--dry-run"])
        with_prompt = _estimate(capsys.readouterr().out)

        assert one > 0
        assert _estimate(out3) == pytest.approx(3 * one, rel=1e-3)
        assert "x 3 roll(s) = 12 calls" in out3
        assert with_prompt == pytest.approx(one * 1.5, rel=1e-3), "the candidate arm costs one more model arm"
        assert harness.clients == [] and harness.fetches == [] and harness.client.calls == []
        assert not (tmp_path / "cards").exists()

    def test_estimate_cost_unknown_model_is_reported(self):
        total, unknown = model_eval.estimate_cost(["not-a-priced-model"], 2, 3, SEG)

        assert total == 0
        assert unknown == ["not-a-priced-model"]

    def test_full_run_fetches_captions_once_per_fixture_and_renders(self, harness, tmp_path):
        code = model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--rolls", "2", "--fixtures", "fx1"])

        assert code == 0
        assert harness.fetches == [VID]
        assert len(harness.client.calls) == 4
        card = (tmp_path / "cards" / f"{NEW}.md").read_text(encoding="utf-8")
        rows = json.loads((tmp_path / "cards" / f"{NEW}.json").read_text(encoding="utf-8"))
        assert sorted((r["model"], r["roll"]) for r in rows) == sorted([(NEW, 1), (NEW, 2), (OLD, 1), (OLD, 2)])
        assert all(r["caption_offset_median_s"] == EXPECTED_MEDIAN for r in rows)
        assert "## Decision rule" in card

    def test_second_run_replays_captions_and_cells_with_no_key_and_no_client(self, harness, tmp_path, monkeypatch):
        argv = ["--candidate", NEW, "--incumbent", OLD, "--rolls", "2", "--fixtures", "fx1"]
        model_eval.main(argv)
        harness.fetches.clear()
        harness.clients.clear()
        monkeypatch.delenv("GEMINI_API_KEY")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        code = model_eval.main(argv)

        assert code == 0
        assert harness.fetches == [], "the caption track is replayed from _raw/"
        assert harness.clients == [], "a full cache replay builds no client"

    def test_refused_caption_fetch_never_fails_the_run(self, harness, tmp_path, monkeypatch):
        def refused(video_id, *, reason_sink=None):
            reason_sink.update({"kind": CAPTIONS_FAILURE_BLOCKED, "exception": "IpBlocked", "message": ""})
            return None

        monkeypatch.setattr(model_eval.v, "fetch_english_captions", refused)

        code = model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--fixtures", "fx1"])

        assert code == 0
        rows = json.loads((tmp_path / "cards" / f"{NEW}.json").read_text(encoding="utf-8"))
        assert all(r["caption_offset_median_s"] is None and "refused" in r["caption_reason"] for r in rows)
        assert not (tmp_path / "cards" / "_raw" / "fx1__captions.json").exists()

    def test_absent_caption_track_is_none_with_reason(self, harness, tmp_path, monkeypatch):
        def absent(video_id, *, reason_sink=None):
            reason_sink.update({"kind": CAPTIONS_FAILURE_ABSENT, "exception": "NoTranscriptFound", "message": ""})
            return None

        monkeypatch.setattr(model_eval.v, "fetch_english_captions", absent)

        model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--fixtures", "fx1"])

        rows = json.loads((tmp_path / "cards" / f"{NEW}.json").read_text(encoding="utf-8"))
        assert all("no English caption track" in r["caption_reason"] for r in rows)

    def test_legacy_cache_replays_offline_with_one_roll_and_none_offsets(self, harness, tmp_path, monkeypatch):
        """The Gate 1b shape: a pre-#292 cache, no key, and both boundaries raising if touched."""
        raw = tmp_path / "cards" / "_raw"
        raw.mkdir(parents=True)
        for model in (NEW, OLD):
            (raw / f"fx1__{model}.json").write_text(_aligned_transcript(), encoding="utf-8")
            (raw / f"fx1__{model}.usage.json").write_text(json.dumps({"prompt": 900, "candidates": 90}), "utf-8")

        def raising(*a, **kw):
            raise RuntimeError("network touched")

        monkeypatch.setattr(model_eval.v, "create_client", raising)
        monkeypatch.setattr(model_eval.v, "fetch_english_captions", raising)
        monkeypatch.delenv("GEMINI_API_KEY")

        code = model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--fixtures", "fx1"])

        assert code == 0
        rows = json.loads((tmp_path / "cards" / f"{NEW}.json").read_text(encoding="utf-8"))
        assert [(r["model"], r["roll"], r["cache"]) for r in rows] == [(NEW, 1, "legacy"), (OLD, 1, "legacy")]
        assert all(r["caption_offset_median_s"] is None for r in rows)
        card = (tmp_path / "cards" / f"{NEW}.md").read_text(encoding="utf-8")
        assert "R2" in card.split("## Decision rule", 1)[1] and "NOT EVALUATED" in card


# ---------------------------------------------------------------------------
# The runbook's commands parse under the real argparse
# ---------------------------------------------------------------------------


class TestRunbook:
    RUNBOOK = ROOT / "docs" / "model-benchmark-runbook.md"

    def _commands(self) -> list[list[str]]:
        out = []
        for line in self.RUNBOOK.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("python scripts/model_eval.py"):
                out.append(shlex.split(line)[2:])
        return out

    def test_runbook_commands_parse(self):
        parser = model_eval.build_parser()

        for argv in self._commands():
            parser.parse_args(argv)

    def test_runbook_command_extraction_is_not_vacuous(self):
        commands = self._commands()

        assert len(commands) >= 3
        assert any("--candidate-prompt" in c for c in commands)
        assert any("--rolls" in c for c in commands)

    def test_runbook_commands_name_a_new_card_and_never_a_tracked_one(self):
        """Without `--out` the prompt recipe would overwrite the default model's tracked scorecard."""
        parser = model_eval.build_parser()

        for argv in self._commands():
            args = parser.parse_args(argv)
            assert args.out, f"runbook command without --out: {argv}"
            assert not (ROOT / args.out).exists(), f"{args.out} would overwrite a tracked card"
            if args.candidate_prompt:
                assert Path(args.out).stem != args.candidate, "a prompt run must not write the model's own card"

    def test_runbook_is_linked_from_testing_and_the_rule_file(self):
        assert "model-benchmark-runbook.md" in (ROOT / "docs" / "testing.md").read_text(encoding="utf-8")
        assert "model-benchmark-runbook.md" in (ROOT / ".claude" / "rules" / "evals.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Review round: excluded rolls, production task counts, the served model, the
# offline replay, the resolved cache key, prompt reading and atomic writes
# ---------------------------------------------------------------------------


class TestExcludedRollsStayExcludedEverywhere:
    """The medians table, the cost rule and the mean line count the rolls the decision rule counts."""

    def test_medians_table_leaves_out_window_missed_and_legacy_rolls(self):
        rows = [
            _r(NEW, 1, 50, 2.0),
            _r(NEW, 2, 52, 2.0),
            _r(NEW, 3, 500, 9.0, window_exceeded=True),
            _r(NEW, 4, 900, 9.0, cache="legacy"),
        ]

        card = model_eval.render(rows, [NEW], MANIFEST, None)

        medians = card.split("## Medians across rolls", 1)[1]
        line = next(x for x in medians.splitlines() if x.startswith(f"| fx1 | {NEW} | bundled | 4 |"))
        assert line == f"| fx1 | {NEW} | bundled | 4 | 0 | 2 | 51 (2) | 2 (0) | 2 (0) | 0.3 |"

    def test_cost_rule_leaves_out_a_window_missed_roll(self):
        rows = _incumbent_rolls() + [
            _r(NEW, 1, 50, 2.0, cost_per_video_hour=0.3),
            _r(NEW, 2, 50, 2.0, cost_per_video_hour=0.9, window_exceeded=True),
        ]

        status, detail = _verdict(rows)["rules"]["R4"]

        assert status == "PASS"
        assert "median $0.3/video-hour" in detail
        assert "1 roll(s) excluded" in detail

    def test_cost_rule_is_not_evaluated_when_every_cost_is_excluded(self):
        rows = _incumbent_rolls() + [_r(NEW, 1, 50, 2.0, cache="legacy")]

        assert _verdict(rows)["rules"]["R4"][0] == "NOT EVALUATED"

    def test_mean_line_leaves_out_legacy_cells(self):
        rows = [_r(NEW, 1, 40, 2.0), _r(NEW, 2, 400, 2.0, cache="legacy"), _r(OLD, 1, 50, 2.0)]

        card = model_eval.render(rows, [NEW, OLD], MANIFEST, OLD)

        assert "mean max_gap **40.0s** vs 50.0s" in card
        assert "1 legacy-cache cell(s) excluded" in card


class TestTaskCountsFollowProduction:
    """`speakers` and `screen_content` count what production would render, not `len()` of anything."""

    @pytest.mark.parametrize(
        ("speakers", "screen"),
        [
            ("Host", "slide text"),
            (["Host"], ["slide"]),
            ([{"name": "Host"}, {"voice": {"id": 1}}], [{"text": "slide"}, {"start": "  ", "text": "x"}]),
        ],
        ids=["string_task_value", "non_dict_entries", "unusable_fields"],
    )
    def test_unrenderable_tasks_count_as_missing(self, speakers, screen):
        raw = json.dumps(
            {
                "transcripts": [{"start": _mmss(START), "voice": 1, "text": "hello"}],
                "speakers": speakers,
                "screen_content": screen,
            }
        )

        row = _score(raw)

        assert (row["speakers"], row["screen_content"]) == (0, 0)
        assert model_eval.hard_fails(row, FX) == [
            model_eval.HARD_FAIL_MISSING_SPEAKERS,
            model_eval.HARD_FAIL_MISSING_SCREEN,
        ]

    def test_usable_entries_still_count(self):
        row = _score(_envelope([START, START + 30]))

        assert (row["speakers"], row["screen_content"]) == (1, 1)


class TestReviewRoundDecisionRule:
    def test_one_none_candidate_roll_is_dropped_and_not_evaluated(self):
        rows = _incumbent_rolls() + [_r(NEW, 1, 50, 2.0), _r(NEW, 2, None, None), _r(NEW, 3, 50, 2.0)]

        verdict = _verdict(rows)

        assert verdict["rules"]["R2"][0] == "NOT EVALUATED"
        assert "1 candidate roll(s)" in verdict["rules"]["R2"][1]
        assert verdict["rules"]["R3"][0] == "NOT EVALUATED"

    @pytest.mark.parametrize("side", ["candidate", "incumbent"])
    def test_model_comparison_with_an_unrecorded_served_model_is_not_evaluated(self, side):
        inc = _incumbent_rolls()
        cand = [_r(NEW, r, 50, 2.0) for r in (1, 2, 3)]
        (cand if side == "candidate" else inc)[1]["model_served"] = None

        verdict = _verdict(inc + cand)

        for rid in ("R2", "R3"):
            assert verdict["rules"][rid][0] == "NOT EVALUATED"
            assert "served model unknown" in verdict["rules"][rid][1]

    def test_prompt_comparison_is_not_affected_by_an_unrecorded_served_model(self):
        bundled = [_r(NEW, r, g, 2.0, model_served=None) for r, g in ((1, 40), (2, 50), (3, 60))]
        cand = [_r(NEW, r, 45, 2.0, label="candidate:abcd1234", model_served=None) for r in (1, 2, 3)]

        verdict = model_eval.decision_rule(bundled + cand, MANIFEST, (NEW, "bundled"))

        assert verdict["candidates"][(NEW, "candidate:abcd1234")]["overall"] == "PASS"

    def test_cost_detail_says_it_is_priced_at_the_requested_rate(self):
        rows = _incumbent_rolls() + [{**_r(NEW, r, 50, 2.0), "model_served": "gemini-3.9-flash"} for r in (1, 2, 3)]

        detail = _verdict(rows)["rules"]["R4"][1]

        assert "priced at the requested model's rate" in detail
        assert "gemini-3.9-flash" in detail


class TestWhatEachCallIsSent:
    def test_candidate_prompt_run_sends_each_arm_its_own_prompt_then_replays(self, harness, tmp_path):
        cand = tmp_path / "cand.md"
        cand.write_bytes(b"CANDIDATE PROMPT TEXT\n")
        argv = ["--candidate", NEW, "--incumbent", OLD, "--candidate-prompt", str(cand), "--rolls", "2"]
        argv += ["--fixtures", "fx1"]

        assert model_eval.main(argv) == 0
        bundled = model_eval.bundled_prompt().text
        sent = [(s["model"], s["prompt"]) for s in harness.client.sent]
        assert sent == [(NEW, bundled)] * 2 + [(OLD, bundled)] * 2 + [(NEW, "CANDIDATE PROMPT TEXT\n")] * 2

        harness.client.sent.clear()
        assert model_eval.main(argv) == 0
        assert harness.client.sent == [], "a second identical run is served from the cache"

    def test_thinking_flag_reaches_the_call(self, harness):
        model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--fixtures", "fx1", "--thinking", "medium"])

        assert harness.client.sent
        assert all(s["thinking"]["thinking_level"].lower() == "medium" for s in harness.client.sent)

    def test_run_one_stamps_the_prompt_hash_and_source_and_sends_that_text(self, tmp_path, no_override):
        cand = _candidate_arm(tmp_path, "My candidate prompt.\n")
        client = FakeClient(_envelope([START, START + 30]))
        scratch = tmp_path / "raw"
        scratch.mkdir()

        row = model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch, prompt=cand)

        assert row["prompt_sha256"] == cand.sha256
        assert row["prompt_source"] == "candidate-transcript.md"
        assert client.sent[0]["prompt"] == "My candidate prompt.\n"

    def test_every_candidate_roll_failed_still_renders_with_r1_fail(self, harness, tmp_path, monkeypatch):
        real = model_eval.v.call_gemini

        def failing_for_the_candidate(client, types, uri, prompt, model, *args, **kwargs):
            if model == NEW:
                raise TimeoutError("slow")
            return real(client, types, uri, prompt, model, *args, **kwargs)

        monkeypatch.setattr(model_eval.v, "call_gemini", failing_for_the_candidate)

        code = model_eval.main(["--candidate", NEW, "--incumbent", OLD, "--rolls", "2", "--fixtures", "fx1"])

        assert code == 0
        rows = json.loads((tmp_path / "cards" / f"{NEW}.json").read_text(encoding="utf-8"))
        assert [r.get("error", "").split(":")[0] for r in rows if r["model"] == NEW] == ["TimeoutError"] * 2
        card = (tmp_path / "cards" / f"{NEW}.md").read_text(encoding="utf-8")
        assert "- R1 no hard fail in any roll: FAIL" in card
        assert "Overall: **FAIL**" in card


class TestCacheKeyIdentityAndSettings:
    @pytest.mark.parametrize("change", ["url", "start", "segment_seconds"])
    def test_a_different_video_or_window_is_a_miss(self, tmp_path, no_override, change):
        client = FakeClient(_envelope([START, START + 30]))
        scratch = tmp_path / "raw"
        scratch.mkdir()
        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)
        fx, seg = dict(FX), SEG
        if change == "url":
            fx["url"] = "https://www.youtube.com/watch?v=vid00000002"
        elif change == "start":
            fx["start"] = START + 60
        else:
            seg = SEG + 60

        model_eval.run_one(client, genai_types, fx, NEW, None, seg, scratch)

        assert len(client.calls) == 2

    def test_media_resolution_change_is_a_miss_and_reaches_the_call(self, tmp_path, no_override, monkeypatch):
        client = FakeClient(_envelope([START, START + 30]))
        scratch = tmp_path / "raw"
        scratch.mkdir()
        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)
        monkeypatch.setattr(model_eval, "MEDIA_RESOLUTION", "MEDIA_RESOLUTION_HIGH")

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)

        assert len(client.calls) == 2
        assert "HIGH" in client.sent[1]["media"]

    def test_output_token_cap_change_is_a_miss(self, tmp_path, no_override, monkeypatch):
        client = FakeClient(_envelope([START, START + 30]))
        scratch = tmp_path / "raw"
        scratch.mkdir()
        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)
        monkeypatch.setattr(model_eval.v, "MAX_OUTPUT_TOKENS", 1024)

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)

        assert len(client.calls) == 2

    def test_the_key_records_the_thinking_config_the_helper_resolved(self, tmp_path, no_override, monkeypatch):
        model = "gemini-2.5-flash"
        client = FakeClient(_envelope([START, START + 30]))
        scratch = tmp_path / "raw"
        scratch.mkdir()
        model_eval.run_one(client, genai_types, FX, model, None, SEG, scratch)
        stored = json.loads((scratch / f"fx1__{model}.usage.json").read_text(encoding="utf-8"))
        monkeypatch.setattr(
            model_eval.v, "_make_thinking_config_for_transcript", lambda t, m: t.ThinkingConfig(thinking_budget=128)
        )

        model_eval.run_one(client, genai_types, FX, model, None, SEG, scratch)

        assert stored[model_eval.CACHE_KEY_FIELD]["thinking"] == {"thinking_budget": 0}
        assert len(client.calls) == 2, "a change in what the helper resolves must not replay a stale response"


class TestCachedReplayTouchesNoNetwork:
    ARGV = ("--candidate", NEW, "--incumbent", OLD)

    @staticmethod
    def _forget_captions(tmp_path: Path) -> None:
        for path in (tmp_path / "cards" / "_raw").glob("*__captions.json"):
            path.unlink()

    def test_fully_cached_replay_fetches_no_captions(self, harness, tmp_path):
        model_eval.main(list(self.ARGV))
        self._forget_captions(tmp_path)
        harness.fetches.clear()

        code = model_eval.main(list(self.ARGV))

        assert code == 0
        assert harness.fetches == []
        rows = json.loads((tmp_path / "cards" / f"{NEW}.json").read_text(encoding="utf-8"))
        assert all("--fetch-captions" in r["caption_reason"] for r in rows)

    def test_one_live_cell_fetches_once_per_fixture(self, harness, tmp_path):
        model_eval.main(list(self.ARGV))
        self._forget_captions(tmp_path)
        for suffix in (".json", ".usage.json"):
            (tmp_path / "cards" / "_raw" / f"fx1__{NEW}{suffix}").unlink()
        harness.fetches.clear()

        model_eval.main(list(self.ARGV))

        assert harness.fetches == [VID, VID]

    def test_fetch_captions_flag_fetches_on_a_cached_replay(self, harness, tmp_path):
        model_eval.main(list(self.ARGV))
        self._forget_captions(tmp_path)
        harness.fetches.clear()

        model_eval.main([*self.ARGV, "--fetch-captions"])

        assert harness.fetches == [VID, VID]


class TestCandidatePromptReadLikeLoadPrompt:
    def test_lf_and_crlf_copies_hash_and_send_the_same_text(self, tmp_path):
        lf = tmp_path / "lf.md"
        crlf = tmp_path / "crlf.md"
        lf.write_bytes(b"line one\nline two\n")
        crlf.write_bytes(b"line one\r\nline two\r\n")

        a, b = model_eval.candidate_prompt_arm(lf), model_eval.candidate_prompt_arm(crlf)

        assert a.text == b.text == "line one\nline two\n"
        assert a.sha256 == b.sha256 == hashlib.sha256(b"line one\nline two\n").hexdigest()

    def test_non_utf8_candidate_prompt_is_refused_before_any_call(self, harness, tmp_path):
        bad = tmp_path / "bad.md"
        bad.write_bytes(b"\xff\xfe\xfa not utf-8")

        code = model_eval.main(["--candidate", NEW, "--candidate-prompt", str(bad)])

        assert code == 2
        assert harness.clients == [] and harness.fetches == [] and harness.client.calls == []

    def test_estimate_cost_scales_with_fixtures(self):
        one, _ = model_eval.estimate_cost([NEW], 1, 1, SEG)
        two, _ = model_eval.estimate_cost([NEW], 2, 1, SEG)

        assert one > 0
        assert two == pytest.approx(2 * one)


class TestP90AtSmallCounts:
    @pytest.mark.parametrize(("n", "median", "p90"), [(5, 2, 5), (7, 2, 5)])
    def test_nearest_rank_p90_by_hand(self, n, median, p90):
        """n=5: |shifts| 0 1 2 3 5, rank ceil(4.5)=5 -> 5 (interpolation would give 4.2).
        n=7: 0 1 1 2 2 3 5, rank ceil(6.3)=7 -> 5 (interpolation would give 3.8)."""
        row = _score(_aligned_transcript(n), caption_track=_track())

        assert row["caption_offset_median_s"] == median
        assert row["caption_offset_p90_s"] == p90


class TestCacheWritesAreAtomic:
    @staticmethod
    def _crash_on_usage(monkeypatch):
        real = os.replace

        def crash(src, dst):
            if str(dst).endswith(".usage.json"):
                raise OSError("simulated crash")
            return real(src, dst)

        monkeypatch.setattr(model_eval.os, "replace", crash)
        return real

    def test_crash_before_the_usage_file_leaves_a_miss(self, tmp_path, no_override, monkeypatch):
        scratch = tmp_path / "raw"
        scratch.mkdir()
        real = self._crash_on_usage(monkeypatch)
        with pytest.raises(OSError):
            model_eval.run_one(FakeClient(_envelope([START, START + 30])), genai_types, FX, NEW, None, SEG, scratch)
        monkeypatch.setattr(model_eval.os, "replace", real)
        client = FakeClient(_envelope([START, START + 30]))

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)

        assert client.calls == [NEW]

    def test_a_crash_replacing_an_entry_never_serves_the_new_raw_under_the_old_key(
        self, tmp_path, no_override, monkeypatch
    ):
        scratch = tmp_path / "raw"
        scratch.mkdir()
        bundled = model_eval.bundled_prompt()
        changed = model_eval.PromptArm(model_eval.BUNDLED, "another prompt", "0" * 64, "other")
        model_eval.run_one(FakeClient(_envelope([START, START + 30])), genai_types, FX, NEW, None, SEG, scratch)
        real = self._crash_on_usage(monkeypatch)
        with pytest.raises(OSError):
            other = FakeClient(_envelope([START + 300, START + 330]))
            model_eval.run_one(other, genai_types, FX, NEW, None, SEG, scratch, prompt=changed)
        monkeypatch.setattr(model_eval.os, "replace", real)
        client = FakeClient(_envelope([START, START + 30]))

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch, prompt=bundled)

        assert client.calls == [NEW]

    def test_a_raw_file_that_does_not_match_its_usage_hash_is_a_miss(self, tmp_path, no_override):
        scratch = tmp_path / "raw"
        scratch.mkdir()
        model_eval.run_one(FakeClient(_envelope([START, START + 30])), genai_types, FX, NEW, None, SEG, scratch)
        (scratch / f"fx1__{NEW}.json").write_text(_envelope([START + 300]), encoding="utf-8")
        client = FakeClient(_envelope([START, START + 30]))

        model_eval.run_one(client, genai_types, FX, NEW, None, SEG, scratch)

        assert client.calls == [NEW]
