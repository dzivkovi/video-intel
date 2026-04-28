"""Tests for timestamp_utils.py — pure functions, no API calls."""

from timestamp_utils import (
    normalize_mm_ss_zero_timestamp,
    normalize_timestamp,
    should_reinterpret_part_as_mm_ss_zero,
    timestamp_tolerance,
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
