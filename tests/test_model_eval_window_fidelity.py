"""A scorecard cell must state whether it measured the window it asked for (#219).

Issue #141 spent weeks on a suspicion that `start_offset` had stopped clipping,
and `model_eval.py` could not answer it either way. That is the gap this closes.

The specific blind spot, reproduced below: `trailing` is computed as
`seg_secs - (stamps[-1] - stamps[0])`, which goes NEGATIVE when the returned
span runs past the requested window - and `max(gaps + [trailing])` then
discards it. On the exact #141 shape (a 600s window returned as 1260s of
re-stamped whole video) the old `max_gap_s` reads a healthy 30s while the
window was exceeded by 630s.

A scorecard that cannot tell a clipped run from an unclipped one cannot defend
its own cost numbers, which are derived from the billed input tokens of a
window it merely assumes it got.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from model_eval import score  # noqa: E402

USAGE = {"prompt": 1, "candidates": 1, "thoughts": 0}


def _mmss(t: int) -> str:
    return f"{t // 60:02d}:{t % 60:02d}"


def _envelope(stamps: list[int]) -> str:
    return json.dumps({"transcripts": [{"start": _mmss(s), "text": "x", "voice": 1} for s in stamps]})


class TestItCatchesTheShapeIssue141Reported:
    def test_a_whole_video_restamped_from_start_is_flagged(self):
        """The #141 shape exactly: a 600s window requested at start=1200, and
        the model returns 1260s of content re-stamped to begin at 1200."""
        stamps = list(range(1200, 1200 + 1260, 30))
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None, start_secs=1200)
        assert row["window_exceeded"] is True

    def test_max_gap_alone_cannot_see_it(self):
        """This is WHY the flag has to exist rather than reusing max_gap_s.

        On the unclipped shape, `trailing` is -630 and `max(gaps + [trailing])`
        silently drops it, so the headline metric reads a healthy 30s. A future
        edit that tries to derive the flag from `max_gap_s` reopens this."""
        stamps = list(range(1200, 1200 + 1260, 30))
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None, start_secs=1200)
        assert row["trailing_gap_s"] < 0, "the overrun should show as a negative trailing gap"
        assert row["max_gap_s"] == 30, "max_gap_s is blind to the overrun, which is the point"
        assert row["window_exceeded"] is True

    def test_a_shifted_first_stamp_is_flagged_even_within_the_span(self):
        """A run that starts in the wrong place is not measuring its facet even
        if its span happens to fit."""
        stamps = list(range(1800, 1800 + 300, 30))
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None, start_secs=1200)
        assert row["window_exceeded"] is True
        assert row["first_stamp_offset_s"] == 600


class TestItDoesNotFalseAlarm:
    def test_a_correctly_clipped_cell_passes(self):
        stamps = list(range(1200, 1200 + 600, 30))
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None, start_secs=1200)
        assert row["window_exceeded"] is False

    def test_a_short_span_inside_the_window_passes(self):
        """A model that simply stops stamping early has a different problem,
        already measured by trailing_gap_s. It is not a window violation."""
        stamps = list(range(1200, 1200 + 200, 30))
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None, start_secs=1200)
        assert row["window_exceeded"] is False

    def test_tolerance_absorbs_a_small_overrun(self):
        """Stamp rounding must not be reported as a clipping failure."""
        stamps = [1200, 1200 + 605]
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None, start_secs=1200)
        assert row["window_exceeded"] is False

    @pytest.mark.parametrize("start", [0, 900, 1200, 3000])
    def test_every_real_fixture_offset_passes_on_a_clean_span(self, start):
        stamps = list(range(start, start + 590, 30))
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None, start_secs=start)
        assert row["window_exceeded"] is False


class TestNotCheckedIsNotTheSameAsFine:
    def test_omitting_start_makes_no_claim(self):
        """`None` means the caller did not say which window was requested, so
        no verdict is recorded. Collapsing this to False would let an unchecked
        cell look like a verified one."""
        stamps = list(range(1200, 1200 + 1260, 30))
        row = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None)
        assert row["window_exceeded"] is None
        assert row["first_stamp_offset_s"] is None

    def test_an_empty_transcript_makes_no_claim(self):
        """Nothing came back, so nothing can be said about its span. That is a
        parse/emptiness failure and other fields already carry it."""
        row = score(json.dumps({"transcripts": []}), USAGE, "gemini-3.7-flash", 600, None, start_secs=1200)
        assert row["window_exceeded"] is None

    def test_the_default_keeps_pre_219_callers_identical(self):
        """The parameter is keyword-with-default so nothing that called score()
        before this change behaves differently."""
        stamps = list(range(0, 600, 30))
        before = score(_envelope(stamps), USAGE, "gemini-3.7-flash", 600, None)
        assert before["window_exceeded"] is None
        assert before["max_gap_s"] == 30


class TestTheCachedRealRunsAllPass:
    """The falsification issue #219's acceptance line demands: run the check
    against the cached 2026-08-18 raw output before trusting it. A check that
    fails on known-good data is worse than no check.

    Skips when the gitignored `_raw` cache is not present in this checkout.
    """

    def test_all_cached_cells_pass(self):
        import glob
        import os

        import yaml

        fixtures = ROOT / "tests" / "evals" / "model_fixtures.yaml"
        raw_dir = ROOT / "tests" / "evals" / "model-cards" / "_raw"
        if not fixtures.exists() or not raw_dir.exists():
            pytest.skip("gitignored fixtures/_raw cache not present in this checkout")

        cfg = yaml.safe_load(fixtures.read_text(encoding="utf-8"))
        fx = {f["id"]: f for f in cfg["fixtures"]}
        seg = cfg["defaults"]["segment_seconds"]

        checked = 0
        for u in sorted(glob.glob(str(raw_dir / "*.usage.json"))):
            fid, _model = os.path.basename(u).replace(".usage.json", "").split("__", 1)
            if fid not in fx:
                continue
            raw = Path(u.replace(".usage.json", ".json")).read_text(encoding="utf-8")
            usage = json.loads(Path(u).read_text(encoding="utf-8"))
            row = score(raw, usage, _model, seg, None, start_secs=fx[fid]["start"])
            assert row["window_exceeded"] is False, (
                f"{fid}/{_model} flagged on known-good cached data - the check false-alarms"
            )
            checked += 1

        assert checked >= 4, f"only {checked} cached cells were checked; the cache looks incomplete"
