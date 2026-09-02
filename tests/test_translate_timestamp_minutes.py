"""The translator's bracketed-timestamp minute field is UNBOUNDED (issue #197).

Same defect class as issue #195 one repo over: a `\\d{1,2}` minute field cannot
match a `[100:30]` stamp, and a video-intel transcript renders `MM:SS` verbatim
from whatever the writer produced, so cues past 99:59 carry exactly that shape.
25 such transcripts exist in the corpus today.

The issue was filed on shape alone, with the reviewers explicitly deferring
"whether the affected code paths ever consume video-intel-rendered transcripts".
They do, and executing the pre-fix code showed the consequences were worse than
a mis-parse at all three sites:

* `extract_last_timestamp_seconds` returned **0**, not the last valid stamp,
  because the unmatched line left the running value at the first `[00:00]`.
  Feeding 0 into the video path's `observed_end / duration_seconds` coverage
  ratio is a GUARANTEED false "Gemini may have truncated" warning.
* `detect_overshoot` returned **None** - it stopped detecting overshoot at all,
  a guard silently ceasing to guard.
* `TRANSCRIPT_TIMESTAMP_RE` **rejected** a transcript whose stamps are all past
  99:59 with "does not look like a transcript produced by video_intel.py
  transcript" and `sys.exit(1)` - reachable via a clipped tail transcript
  (`transcript --url --start 1:40:00`).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import translate_video as tv

SOURCE = (Path(__file__).resolve().parent.parent / "scripts" / "translate_video.py").read_text(encoding="utf-8")


class TestMinuteFieldIsUnbounded:
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("[100:30]", 6030),
            ("[125:30]", 7530),
            ("[999:00]", 59940),
            ("[99:30]", 5970),
            ("[00:00]", 0),
        ],
    )
    def test_extract_last_reads_a_two_part_stamp_past_ninety_nine_minutes(self, line, expected):
        assert tv.extract_last_timestamp_seconds(f"[00:00] first\n{line} last\n") == expected

    def test_the_pre_fix_failure_was_zero_not_a_near_miss(self):
        """Pinning WHY this mattered. The unmatched line left `last` at the
        previous match, so a transcript whose only later stamps are past 99:59
        reported the coverage of its first line. On the video path that is
        `0 / duration < 0.95` - a false truncation warning every time."""
        text = "[00:00] intro\n[100:30] middle\n[140:00] end\n"
        assert tv.extract_last_timestamp_seconds(text) == 8400
        assert tv.extract_last_timestamp_seconds(text) != 0

    def test_overshoot_detection_still_fires_past_ninety_nine_minutes(self):
        """A guard that stops guarding is worse than no guard. Pre-fix this
        returned None for any output whose stamps were all past 99:59."""
        text = "[00:00] a\n[125:30] way past the end\n"
        assert tv.detect_overshoot(text, 6000) == (2, 7530)

    def test_overshoot_does_not_false_alarm_inside_tolerance(self):
        assert tv.detect_overshoot("[00:00] a\n[100:30] b\n", 6000) is None

    def test_the_from_transcript_gate_accepts_a_clipped_tail_transcript(self):
        """Every stamp past 99:59 is what a clipped tail looks like
        (`transcript --url --start 1:40:00`). Pre-fix the gate refused it as
        "not a transcript" and exited 1."""
        assert tv.TRANSCRIPT_TIMESTAMP_RE.search("# Title\n\n[100:00] a\n[105:30] b\n")


class TestTheHourFieldStaysBounded:
    """Widening the HOUR field would be a different, worse bug: a legacy
    malformed `[120:05:30]` would parse as 120 hours instead of failing to
    match, and `normalize_timestamp` is what exists to repair that shape."""

    def test_a_legacy_three_part_stamp_with_an_absurd_hour_does_not_parse(self):
        assert tv.extract_last_timestamp_seconds("[120:05:30] b\n") is None

    def test_a_single_digit_hour_still_parses(self):
        assert tv.extract_last_timestamp_seconds("[2:05:30] b\n") == 7530

    def test_a_normal_two_digit_hour_still_parses(self):
        assert tv.extract_last_timestamp_seconds("[02:05:30] b\n") == 7530


class TestPatternsAreDefinedOnce:
    """Issue #195's lesson, applied here: three literals with the identical
    narrow shape are three chances to drift. One definition, three consumers."""

    # Any bracketed literal whose FIRST field is a counted `{N}` / `{N,M}`
    # digit run, or a bare backslash-d backslash-d. The first cut matched only
    # `{1,2}` and the bare form - the two shapes this PR deleted - so a
    # reviewer reintroduced `{1,3}` and the guard passed. `{1,3}` is exactly
    # the shape issue #195 invariant 3 names as still-broken ("just moves the
    # cliff to 16h40m"), so a guard blind to it guards nothing that matters.
    BAD_LITERAL = re.compile(r"\\\[\(?\\d(\{\d(,\d)?\}|\\d)")

    # Three inline literals are deliberately NOT rewired. Each carries a
    # `# timestamp-literal-ok:` marker naming why, so an UNMARKED fourth copy
    # still fails. Exempting by marker rather than by line number means the
    # exemption travels with the code it excuses.
    EXEMPTION_MARKER = "# timestamp-literal-ok:"

    def _offending_lines(self, text: str) -> list[str]:
        text_lines = text.split("\n")
        out = []
        for lineno, line in enumerate(text_lines, 1):
            if line.strip().startswith("#"):
                continue
            if not self.BAD_LITERAL.search(line):
                continue
            window = chr(10).join(text_lines[max(0, lineno - 6) : lineno])
            if self.EXEMPTION_MARKER in window:
                continue
            out.append(f"{lineno}: {line.strip()}")
        return out

    def test_no_site_carries_its_own_bracketed_timestamp_literal(self):
        """Any UNMARKED inline bracketed-timestamp literal is a fourth copy
        waiting to drift out of sync."""
        offenders = self._offending_lines(SOURCE)
        assert not offenders, f"inline bracketed-timestamp literals: {offenders}"

    def test_the_guard_regex_actually_matches_a_known_bad_literal(self):
        """The companion the first cut was missing, and the reason it shipped
        blind: `test_the_walk_is_not_vacuous` proves the CONSTANTS exist, never
        that the SEARCH pattern still matches anything. Feed the guard the
        shapes it must catch - including the `{1,3}` a reviewer smuggled past
        the original - so a regex that stops matching fails HERE instead of
        turning the check above into `assert not []` forever.
        """
        d = chr(92) + "d"
        for bad in (
            'x = re.compile(r"' + chr(92) + "[(" + d + "{1,2}):(" + d + "{2})" + chr(92) + ']")',
            'x = re.compile(r"' + chr(92) + "[(" + d + "{1,3}):(" + d + "{2})" + chr(92) + ']")',
            'x = re.match(r"' + chr(92) + "[" + d + d + ':", line)',
        ):
            assert self._offending_lines(bad), f"the guard no longer catches: {bad}"

    def test_the_exemptions_are_explicit_and_bounded(self):
        """Three marked exemptions today. A diff that marks a fourth has to
        change this number, which is the point - the marker must stay a
        deliberate act, not a way to silence the guard."""
        assert SOURCE.count(self.EXEMPTION_MARKER) == 3, (
            "the set of deliberately-unshared timestamp literals changed; "
            "justify the new one the way the existing three are justified"
        )

    def test_the_walk_is_not_vacuous(self):
        """Companion. If the three consumers were ever hoisted out of this
        module, or the constants renamed, the walk above would compare an empty
        list against itself and pass forever - the tautology class the issue
        #182 field-inventory walk was bitten by."""
        for name in ("TS_HOURS", "TS_MINUTES", "TS_SECONDS", "HHMMSS_BRACKET_RE", "MMSS_BRACKET_RE"):
            assert hasattr(tv, name), f"{name} is gone; the drift guard above is now vacuous"
        assert tv.TS_MINUTES == r"\d+", "the minute field must stay unbounded"
        assert tv.TS_HOURS == r"\d{1,2}", "the hour field must stay bounded"
        # All three consumers must genuinely be built from the constants.
        assert SOURCE.count("HHMMSS_BRACKET_RE") >= 3
        assert SOURCE.count("MMSS_BRACKET_RE") >= 3
        assert "TS_MINUTES}" in SOURCE, "TRANSCRIPT_TIMESTAMP_RE must build from the constant"


class TestDetectOvershootMatchesItsOwnDocstring:
    """Its docstring claimed tolerance of "the same [HH:MM:SS] and [MM:SS]
    formats that extract_last_timestamp_seconds accepts". That was FALSE: it
    used `\\d\\d` (exactly two digits) where the other accepts `\\d{1,2}`, so a
    single-digit hour matched one and not the other. Now genuinely parity."""

    # `[00:20]` is the case that exposed the first cut. Its assertion was
    # `(via_extract is not None) == (via_overshoot is not None or via_extract == 0)`,
    # where the `or ... == 0` clause papered over detect_overshoot returning
    # None simply because nothing OVERSHOT. Run against `[00:20] x` it FAILED
    # on healthy code - a latent false alarm - and `[00:00]` passed either
    # way, so the parametrization was partly vacuous too. Recognition is now
    # asked directly. `detect_overshoot` computes `cutoff = last_input +
    # tolerance`, so the probe passes BOTH a -1 input end and an explicit
    # `tolerance_seconds=0`: a cutoff of -1 means every recognized stamp
    # overshoots, so None can only mean "not recognized". The obvious `-1`
    # alone leaves the default 30s tolerance in place and `[00:20]` does not
    # clear it - this test caught that on its own first run.
    @pytest.mark.parametrize(
        "line",
        ["[2:05:30] x", "[02:05:30] x", "[125:30] x", "[99:30] x", "[00:00] x", "[00:20] x"],
    )
    def test_both_helpers_agree_on_what_is_a_timestamp(self, line):
        recognized_by_extract = tv.extract_last_timestamp_seconds(line + "\n") is not None
        recognized_by_overshoot = tv.detect_overshoot(line + "\n", -1, tolerance_seconds=0) is not None
        assert recognized_by_extract == recognized_by_overshoot, (
            f"the two helpers disagree about whether {line!r} carries a timestamp"
        )


class TestOperationalSeparation:
    """The translator must not grow a video-intel dependency as a side effect
    of a shared-shape fix (the standing #129(4) precedent). The permitted
    shared imports are `timestamp_utils` (stdlib-only by contract),
    `gemini_common` and `youtube_captions`; `video_intel` is not one of them.

    Known limit: an AST NAME walk cannot see `importlib.import_module(...)`.
    Verified - swapping the import for that form leaves this suite green. The
    guard is a drift catcher, not a sandbox; do not read it as airtight."""

    def test_no_new_video_intel_import(self):
        """Checked with the AST, not a substring scan: the module legitimately
        MENTIONS video_intel in prose ("from video_intel.py transcript" is help
        text for the --from-transcript flag), and a naive `"from video_intel"
        not in SOURCE` fails on that. What matters is an import STATEMENT."""
        tree = ast.parse(SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "video_intel" not in imported, f"translator imported video_intel: {sorted(imported)}"

    def test_the_ast_walk_sees_the_real_imports(self):
        """Companion: an AST walk that found nothing would pass the test above
        vacuously. `timestamp_utils` is the one shared import the firebreak
        permits, so its presence proves the walk works."""
        tree = ast.parse(SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "timestamp_utils" in imported, "the walk found no imports at all"
