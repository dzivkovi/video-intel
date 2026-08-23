"""Tests for timestamp_utils.py - pure functions, no API calls."""

import pathlib
import subprocess
import sys
import textwrap

import pytest

from timestamp_utils import (
    normalize_mm_ss_zero_timestamp,
    normalize_timestamp,
    parse_time_to_seconds,
    should_reinterpret_part_as_mm_ss_zero,
    timestamp_tolerance,
    timestamped_url,
)


class TestNormalizeTimestamp:
    def test_normal_timestamp_unchanged(self):
        assert normalize_timestamp("[00:05:30] hello") == "[00:05:30] hello"

    def test_boundary_23_unchanged(self):
        assert normalize_timestamp("[23:59:59] text") == "[23:59:59] text"

    def test_120_minutes_converts_to_2_hours(self):
        assert normalize_timestamp("[120:05:30] text") == "[02:05:30] text"

    def test_90_minutes_converts_with_carry(self):
        assert normalize_timestamp("[90:15:42] text") == "[01:45:42] text"

    def test_60_minutes_converts_to_1_hour(self):
        assert normalize_timestamp("[60:00:00] text") == "[01:00:00] text"

    def test_no_timestamp_passthrough(self):
        assert normalize_timestamp("no timestamp line") == "no timestamp line"

    def test_minutes_carry_over_60(self):
        # 75 min + 50 MM = 1h15m + 50m = 2h05m
        assert normalize_timestamp("[75:50:10] text") == "[02:05:10] text"

    def test_empty_line_passthrough(self):
        assert normalize_timestamp("") == ""

    def test_tucker_chunk3_100_08_57_normalizes_to_1_48_57(self):
        # Real-input regression from issue #58: Tucker/Sachs chunk 3 produced
        # [100:08:57] meaning 100 minutes (= 1h40min) into the video, 8 sec 57.
        # 100 // 60 = 1 hour, 100 % 60 = 40 minutes, +8 MM = 48 minutes.
        assert normalize_timestamp("[100:08:57] Tucker Carlson (Host)") == "[01:48:57] Tucker Carlson (Host)"

    def test_tucker_chunk3_100_00_00_normalizes_to_1_40_00(self):
        # Chunk 3 start: 100 min total = 1h40m exactly.
        assert normalize_timestamp("[100:00:00] x") == "[01:40:00] x"

    def test_tucker_chunk3_100_22_35_normalizes_to_2_02_35(self):
        # Near chunk 3 end: 100 min + 22 MM = 1h40m + 22m = 2h02m, 35 sec.
        assert normalize_timestamp("[100:22:35] end") == "[02:02:35] end"


class TestNormalizeMmSsZeroTimestamp:
    def test_drops_trailing_zero_seconds_field(self):
        # [05:30:00] in MM:SS:00 mode is really 05:30; drop the trailing :00.
        assert normalize_mm_ss_zero_timestamp("[05:30:00] hello") == "[05:30] hello"

    def test_no_match_passthrough(self):
        # Real HH:MM:SS where SS != 0 — leave alone.
        assert normalize_mm_ss_zero_timestamp("[01:30:42] text") == "[01:30:42] text"

    def test_no_timestamp_passthrough(self):
        assert normalize_mm_ss_zero_timestamp("plain text") == "plain text"


class TestShouldReinterpretPartAsMmSsZero:
    def test_returns_true_when_alt_explains_more(self):
        # Three normalized [HH:MM:00] timestamps where standard interpretation
        # places them way past the chunk end (1h, 2h, 3h vs 10-min chunk) but
        # the alt interpretation (treating HH:MM as plain MM:SS) fits cleanly.
        lines = ["[01:00:00] a", "[02:00:00] b", "[03:00:00] c"]
        assert should_reinterpret_part_as_mm_ss_zero(lines, offset_seconds=0, chunk_duration_seconds=600) is True

    def test_returns_false_when_standard_explains_more(self):
        # Three timestamps that fit cleanly as HH:MM:SS (1h, 1h05m, 1h10m)
        # within a 7200s chunk + offset 0.
        lines = ["[01:00:00] a", "[01:05:00] b", "[01:10:00] c"]
        assert should_reinterpret_part_as_mm_ss_zero(lines, offset_seconds=0, chunk_duration_seconds=7200) is False

    def test_returns_false_when_too_few_candidates(self):
        # Only 2 SS=0 timestamps; need 3+ candidates to flip mode.
        lines = ["[45:00:00] a", "[50:00:00] b"]
        assert should_reinterpret_part_as_mm_ss_zero(lines, offset_seconds=0, chunk_duration_seconds=3600) is False


class TestTimestampTolerance:
    def test_50_minute_chunk_returns_300_seconds(self):
        # 3000s // 10 = 300, capped at 300.
        assert timestamp_tolerance(3000) == 300

    def test_short_chunk_floors_at_30_seconds(self):
        # 60s // 10 = 6, floored to 30.
        assert timestamp_tolerance(60) == 30

    def test_long_chunk_caps_at_300_seconds(self):
        # 10000s // 10 = 1000, capped to 300.
        assert timestamp_tolerance(10000) == 300


class TestParseTimeToSeconds:
    # Mirrors tests/test_utils.py::TestParseTimeToSeconds - this module now
    # owns the function; video_intel.py re-exports it (issue #152). Keeping
    # both test suites is deliberate: test_utils.py proves the re-export
    # still works, this class proves the implementation itself is correct
    # in its new home.
    def test_mm_ss_returns_seconds(self):
        assert parse_time_to_seconds("05:30") == 330

    def test_hh_mm_ss_returns_seconds(self):
        assert parse_time_to_seconds("01:15:45") == 4545

    def test_raw_seconds_string(self):
        assert parse_time_to_seconds("330") == 330

    def test_zero_value(self):
        assert parse_time_to_seconds("0") == 0
        assert parse_time_to_seconds("00:00") == 0

    def test_leading_zeros_in_components(self):
        assert parse_time_to_seconds("00:05:00") == 300

    def test_whitespace_tolerated(self):
        assert parse_time_to_seconds(" 05:30 ") == 330

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_time_to_seconds("")

    def test_non_numeric_raises(self):
        with pytest.raises(ValueError):
            parse_time_to_seconds("not-a-time")

    def test_too_many_colons_raises(self):
        with pytest.raises(ValueError):
            parse_time_to_seconds("1:2:3:4")


class TestTimestampedUrl:
    # Mirrors tests/test_intel_graph.py::TestTimestampedUrl - intel_graph.py
    # re-exports this function (issue #152). Keeping both test suites is
    # deliberate for the same reason as TestParseTimeToSeconds above.
    def test_appends_t_param_when_query_string_exists(self):
        assert timestamped_url("https://x.com/watch?v=1", 30) == "https://x.com/watch?v=1&t=30"

    def test_appends_t_param_when_no_query_string(self):
        assert timestamped_url("https://x.com/v", 30) == "https://x.com/v?t=30"

    def test_none_url_returns_empty_string(self):
        assert timestamped_url(None, 30) == ""


class TestReExportsStayReExports:
    """The two re-exports survive only INCIDENTALLY, which is the hazard.

    `video_intel.parse_time_to_seconds` and `intel_graph.timestamped_url` are
    plain imports kept alive by each module's own internal call sites. If a
    future refactor drops those uses, ruff F401 fires and the obvious fix
    (delete the import) silently breaks a documented compatibility contract
    that `tests/test_utils.py` and `tests/test_intel_graph.py` depend on.

    Asserting IDENTITY, not just presence, is the point: a re-implementation
    copied back into either old home would satisfy both of those suites while
    reintroducing exactly the duplication issue #152 removed.
    """

    def test_video_intel_re_exports_the_same_object(self):
        import video_intel

        assert video_intel.parse_time_to_seconds is parse_time_to_seconds

    def test_intel_graph_re_exports_the_same_object(self):
        import intel_graph

        assert intel_graph.timestamped_url is timestamped_url


class TestTimestampUtilsStaysStdlibOnly:
    """CLAUDE.md states in bold that this module must never gain a dependency
    beyond the standard library, because that is what lets the five standalone
    analytics scripts import without the curate stack. Prose asserting an
    invariant is not the invariant, so enforce it: import the module in a
    subprocess with every non-stdlib top-level package blocked."""

    def test_import_succeeds_with_all_third_party_packages_blocked(self):
        child_script = textwrap.dedent("""
            import sys, importlib.abc

            class OnlyStdlib(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    root = name.split('.')[0]
                    # The module under test itself is obviously not stdlib.
                    if root == 'timestamp_utils':
                        return None
                    if root in sys.builtin_module_names or root in sys.stdlib_module_names:
                        return None
                    raise ImportError('non-stdlib import blocked: ' + name)

            sys.meta_path.insert(0, OnlyStdlib())
            import timestamp_utils
            print('OK')
            """)
        result = subprocess.run(
            [sys.executable, "-c", child_script],
            cwd=str(pathlib.Path(__file__).resolve().parent.parent / "scripts"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            "timestamp_utils gained a non-stdlib dependency. That breaks the import-weight "
            "firebreak documented in CLAUDE.md: the five standalone analytics scripts import "
            f"it precisely because it is stdlib-only.\n{result.stderr}"
        )
        assert "OK" in result.stdout


class TestStandaloneScriptImportIsolation:
    """Issue #152 acceptance criterion 2: each standalone analytics script must
    import without google-api-python-client (or the other heavy curate-stack
    deps) installed, and must not pull video_intel into sys.modules as a side
    effect. A subprocess with a fresh module table is the only reliable way
    to prove this - reusing the parent pytest process would see modules
    already imported by other test files and give a false pass."""

    # All five, not just wiki_concepts. Importing wiki_concepts transitively
    # covers wiki_atlas, lead_lag_report, and lead_lag_viz, but NOTHING in that
    # chain reaches burst_report - so a revert of burst_report's import back to
    # `from intel_graph import timestamped_url` (the exact regression this
    # guards) would have passed the whole suite undetected.
    @pytest.mark.parametrize(
        "module",
        ["wiki_concepts", "wiki_atlas", "lead_lag_report", "lead_lag_viz", "burst_report"],
    )
    def test_import_succeeds_with_heavy_deps_blocked(self, module):
        child_script = textwrap.dedent(f"""
            import sys, importlib.abc
            BLOCK = {{'googleapiclient', 'google_auth_oauthlib', 'google', 'lancedb', 'voyageai'}}

            class Blocker(importlib.abc.MetaPathFinder):
                def find_spec(self, name, path=None, target=None):
                    if name.split('.')[0] in BLOCK:
                        raise ImportError('blocked: ' + name)
                    return None

            sys.meta_path.insert(0, Blocker())
            import {module}
            print('OK')
            print('video_intel pulled:', 'video_intel' in sys.modules)
            print('intel_graph pulled:', 'intel_graph' in sys.modules)
            """)
        result = subprocess.run(
            [sys.executable, "-c", child_script],
            # Absolute, derived from this test file. A relative "scripts" would
            # silently bind the test to pytest being invoked from the repo root.
            cwd=str(pathlib.Path(__file__).resolve().parent.parent / "scripts"),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout
        assert "video_intel pulled: False" in result.stdout
        assert "intel_graph pulled: False" in result.stdout
