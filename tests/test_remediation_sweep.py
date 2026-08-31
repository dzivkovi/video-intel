"""Tests for scripts/remediation_sweep.py (issue #172 worklist builder).

remediation_sweep re-derives its severe/mild buckets by calling
assess_transcript_artifact - the SAME function the transcript writers use -
against on-disk .transcript.md files. It never scores quality itself. These
tests exercise the sweep's OWN plumbing: the dialogue-line parser that feeds
the assessor, the duration-floor extractor, the bucket classifier, and the
corpus walker. They deliberately do not re-test assess_transcript_artifact's
own severity thresholds (that is tests/test_transcript_quality_guard.py's
job) - a test here that could pass with the sweep's hunk reverted is not
testing the sweep.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import remediation_sweep as sweep  # noqa: E402

# ---------------------------------------------------------------------------
# 1. parse_dialogue_entries
# ---------------------------------------------------------------------------


class TestParseDialogueEntries:
    def test_bracketed_mmss_at_column_zero_is_counted(self):
        text = '[00:12] Speaker (host): "hello there"\n'
        entries = sweep.parse_dialogue_entries(text)
        assert entries == [{"start": "00:12"}]

    def test_bracketed_hhmmss_at_column_zero_is_counted(self):
        text = '[01:02:03] Speaker (guest): "later in the video"\n'
        entries = sweep.parse_dialogue_entries(text)
        assert entries == [{"start": "01:02:03"}]

    def test_indented_screen_content_line_is_not_counted(self):
        """Load-bearing: screen content is not dialogue. A collapsed
        transcript can still carry richly timestamped screen-overlay lines,
        and counting those would mask the exact monolithic collapse this
        sweep exists to find - so an indented SCREEN line must contribute
        zero dialogue entries even though it starts with a bracketed
        timestamp of its own."""
        text = '  SCREEN [00:00-00:51] [text_overlay]: "Slide: Welcome"\n'
        entries = sweep.parse_dialogue_entries(text)
        assert entries == []

    def test_on_screen_text_continuation_line_is_not_counted(self):
        text = "On-screen text: Welcome to the talk\n"
        entries = sweep.parse_dialogue_entries(text)
        assert entries == []

    def test_order_is_preserved_not_sorted(self):
        # Out-of-order on purpose - the assessor's backward-jump detection
        # depends on emission order surviving the parser untouched.
        text = "[00:30] second line\n[00:05] first line\n[00:10] third line\n"
        entries = sweep.parse_dialogue_entries(text)
        assert [e["start"] for e in entries] == ["00:30", "00:05", "00:10"]

    def test_one_dialogue_line_amid_twenty_screen_lines_yields_exactly_one(self):
        lines = ['[00:03] Speaker (host): "only real line"']
        lines += [f'  SCREEN [{i:02d}:00-{i:02d}:30] [text_overlay]: "slide {i}"' for i in range(20)]
        text = "\n".join(lines) + "\n"
        entries = sweep.parse_dialogue_entries(text)
        assert len(entries) == 1
        assert entries[0]["start"] == "00:03"


# ---------------------------------------------------------------------------
# 2. last_timestamp_seconds
# ---------------------------------------------------------------------------


class TestLastTimestampSeconds:
    def test_returns_max_timestamp_anywhere_in_text(self):
        text = "[00:05] line one\n[00:03] line two\n[00:09] line three\n"
        assert sweep.last_timestamp_seconds(text) == 9

    def test_range_second_half_wins(self):
        # A SCREEN range like [00:00-12:38] - the end of the range must be
        # the candidate, not the start.
        text = '  SCREEN [00:00-12:38] [text_overlay]: "slide"\n[00:05] dialogue\n'
        assert sweep.last_timestamp_seconds(text) == 12 * 60 + 38

    def test_none_when_no_timestamps_present(self):
        text = "just prose, no brackets, no times at all\n"
        assert sweep.last_timestamp_seconds(text) is None

    def test_unaffected_by_garbage_brackets(self):
        text = "[not a timestamp] some text [also garbage: 99]\n[00:07] real one\n"
        assert sweep.last_timestamp_seconds(text) == 7


# ---------------------------------------------------------------------------
# 3. classify
# ---------------------------------------------------------------------------


def _row(**overrides) -> dict:
    base = {
        "channel": "demo",
        "prefix": "2026-01-01-some-video",
        "video_id": "abc123",
        "title": "Some Video",
        "url": "https://www.youtube.com/watch?v=abc123",
        "model": "gemini-3.7-flash",
        "transcript_status": "ok",
        "duration_seconds": None,
        "duration_source": None,
        "floor_seconds": None,
        "entries": [],
        "bytes": 0,
    }
    base.update(overrides)
    return base


def _entries(seconds_list):
    return [{"start": f"{s // 60:02d}:{s % 60:02d}"} for s in seconds_list]


def _dense_healthy_entries(duration):
    """Enough evenly-spaced entries to clear both the monolithic and gap
    checks - a stand-in for a genuinely healthy transcript."""
    count = max(4, duration // 60)
    return _entries([int(duration * i / (count - 1)) for i in range(count)])


class TestClassify:
    def test_truncated_status_buckets_as_truncated_regardless_of_flags(self):
        row = _row(
            transcript_status=sweep.vi.TRANSCRIPT_STATUS_TRUNCATED,
            duration_seconds=1800,
            entries=_dense_healthy_entries(1800),
        )
        out = sweep.classify(row)
        assert out["bucket"] == "truncated"

    def test_monolithic_severe_flag_buckets_as_monolithic_severe(self):
        # One entry over a >5min window: the assessor's own monolithic gate.
        row = _row(duration_seconds=1800, entries=_entries([10]))
        out = sweep.classify(row)
        assert out["bucket"] == "monolithic_severe"
        assert "monolithic_severe" in out["severe"]

    def test_no_duration_and_no_floor_is_unassessable_never_clean(self):
        """Issue #172's central rule: without a duration, the assessor's
        gap/density/monolithic checks are all gated off, so a genuine 1-entry
        collapse returns severe: [] and must NOT read as clean."""
        row = _row(duration_seconds=None, floor_seconds=None, entries=_entries([10]))
        out = sweep.classify(row)
        assert out["bucket"] == "unassessable"
        assert out["bucket"] != "clean"
        assert out["severe"] == []

    def test_no_duration_but_usable_floor_is_assessed_via_timestamp_floor(self):
        # Floor is well over 300s so the monolithic gate is live, and a
        # single entry against that floor is a genuine collapse.
        row = _row(duration_seconds=None, floor_seconds=1800, entries=_entries([10]))
        out = sweep.classify(row)
        assert out["bucket"] == "monolithic_severe"
        assert out["duration_source"] == "timestamp_floor"
        assert out["assessed_duration_seconds"] == 1800

    def test_healthy_row_buckets_as_clean(self):
        row = _row(duration_seconds=1800, entries=_dense_healthy_entries(1800))
        out = sweep.classify(row)
        assert out["bucket"] == "clean"
        assert out["severe"] == []
        assert out["mild"] == []

    def test_mild_only_row_buckets_as_mild(self):
        # A single backward jump of 90s (>= 60s mild, < 600s severe) amid an
        # otherwise dense, healthy transcript.
        base = [int(1800 * i / 11) for i in range(12)]
        base[6], base[7] = base[7], base[7] - 90  # introduce a 90s backward step
        row = _row(duration_seconds=1800, entries=_entries(base))
        out = sweep.classify(row)
        assert out["bucket"] == "mild"
        assert out["severe"] == []
        assert out["mild"]


# ---------------------------------------------------------------------------
# 4. collect_rows
# ---------------------------------------------------------------------------


def _write_meta(path: Path, **fields):
    path.write_text(json.dumps(fields), encoding="utf-8")


class TestCollectRows:
    def test_skips_underscore_and_dot_prefixed_dirs(self, tmp_path):
        for name in ("_briefings", ".lancedb", "realchannel"):
            d = tmp_path / name
            d.mkdir()
            (d / "2026-01-01-vid.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        rows = sweep.collect_rows(tmp_path)
        assert {r["channel"] for r in rows} == {"realchannel"}

    def test_pairs_transcript_with_its_meta(self, tmp_path):
        ch = tmp_path / "demo"
        ch.mkdir()
        (ch / "2026-01-01-vid.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        _write_meta(
            ch / "2026-01-01-vid.meta.json",
            video_id="vid123",
            title="Real Title",
            video_url="https://www.youtube.com/watch?v=vid123",
            model="gemini-3.7-flash",
            transcript_status="ok",
            duration_seconds=1200,
        )
        rows = sweep.collect_rows(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert row["video_id"] == "vid123"
        assert row["title"] == "Real Title"
        assert row["duration_seconds"] == 1200
        assert row["duration_source"] == "meta"

    def test_tolerates_missing_meta(self, tmp_path):
        ch = tmp_path / "demo"
        ch.mkdir()
        (ch / "2026-01-01-vid.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        rows = sweep.collect_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["video_id"] is None
        assert rows[0]["duration_seconds"] is None
        assert rows[0]["title"] == "2026-01-01-vid"

    def test_tolerates_malformed_meta_returns_row_not_raises(self, tmp_path):
        ch = tmp_path / "demo"
        ch.mkdir()
        (ch / "2026-01-01-vid.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        (ch / "2026-01-01-vid.meta.json").write_text("{not valid json", encoding="utf-8")
        rows = sweep.collect_rows(tmp_path)
        assert len(rows) == 1
        assert rows[0]["video_id"] is None

    def test_duration_source_meta_only_when_positive_int(self, tmp_path):
        ch = tmp_path / "demo"
        ch.mkdir()
        for stem, duration in (("a", 0), ("b", -5), ("c", "1200"), ("d", None)):
            (ch / f"2026-01-01-{stem}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
            _write_meta(ch / f"2026-01-01-{stem}.meta.json", duration_seconds=duration)
        rows = {r["prefix"]: r for r in sweep.collect_rows(tmp_path)}
        for stem in ("a", "b", "c", "d"):
            row = rows[f"2026-01-01-{stem}"]
            assert row["duration_seconds"] is None, stem
            assert row["duration_source"] is None, stem

    def test_only_channel_filters_to_one_channel(self, tmp_path):
        for name in ("demo1", "demo2"):
            ch = tmp_path / name
            ch.mkdir()
            (ch / "2026-01-01-vid.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        rows = sweep.collect_rows(tmp_path, only_channel="demo1")
        assert {r["channel"] for r in rows} == {"demo1"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
