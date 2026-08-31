"""Tests for scripts/remediation_run.py (issue #172 wave driver).

remediation_run re-runs `process --url ... --force` on the sweep's worklist,
in waves, guarded by a consecutive-failure abort, a no-improvement abort, and
resumable state. These tests exercise the guards at the level that matters:
the number of subprocess.run calls made before an abort, and the contents of
the state file left behind - never a stub agreeing with itself.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import remediation_run as rr  # noqa: E402

import video_intel as vi  # noqa: E402

# ---------------------------------------------------------------------------
# 1. select_wave
# ---------------------------------------------------------------------------


def _wave_row(prefix, bucket, briefed=None):
    return {
        "channel": "demo",
        "prefix": prefix,
        "video_id": prefix,
        "title": prefix,
        "url": f"https://www.youtube.com/watch?v={prefix}",
        "bucket": bucket,
        "briefed": briefed,
        "dialogue_entries": 1,
    }


class TestSelectWave:
    def _rows(self):
        return [
            _wave_row("truncated1", "truncated"),
            _wave_row("mono-briefed", "monolithic_severe", briefed=True),
            _wave_row("mono-unbriefed", "monolithic_severe", briefed=False),
            _wave_row("gap-briefed", "blind_gap_severe", briefed=True),
            _wave_row("skip-me", "unassessable"),
        ]

    def test_wave_1_is_the_truncated_bucket(self):
        rows = self._rows()
        out = rr.select_wave(rows, "1")
        assert {r["prefix"] for r in out} == {"truncated1"}

    def test_wave_2_is_severe_and_briefed(self):
        rows = self._rows()
        out = rr.select_wave(rows, "2")
        assert {r["prefix"] for r in out} == {"mono-briefed", "gap-briefed"}

    def test_wave_3_is_severe_and_not_briefed(self):
        rows = self._rows()
        out = rr.select_wave(rows, "3")
        assert {r["prefix"] for r in out} == {"mono-unbriefed"}

    def test_waves_2_and_3_are_disjoint_and_cover_every_severe_row(self):
        rows = self._rows()
        w2 = {r["prefix"] for r in rr.select_wave(rows, "2")}
        w3 = {r["prefix"] for r in rr.select_wave(rows, "3")}
        severe_prefixes = {r["prefix"] for r in rows if r["bucket"] not in ("truncated", "unassessable")}
        assert w2.isdisjoint(w3)
        assert w2 | w3 == severe_prefixes

    def test_unassessable_excluded_from_wave_2_wave_3_and_all(self):
        rows = self._rows()
        for wave in ("2", "3", "all"):
            out = rr.select_wave(rows, wave)
            assert "skip-me" not in {r["prefix"] for r in out}, wave


# ---------------------------------------------------------------------------
# 2. backup_existing
# ---------------------------------------------------------------------------


class TestBackupExisting:
    def _channel_dir(self, tmp_path, channel="demo"):
        d = tmp_path / channel
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_copies_all_four_sibling_artifacts(self, tmp_path):
        channel_dir = self._channel_dir(tmp_path)
        contents = {
            ".transcript.md": b"transcript bytes",
            ".mindmap.md": b"mindmap bytes",
            ".concepts.json": b'{"concepts": []}',
            ".meta.json": b'{"video_id": "v1"}',
        }
        for suffix, data in contents.items():
            (channel_dir / f"vid{suffix}").write_bytes(data)

        rr.backup_existing(tmp_path, {"channel": "demo", "prefix": "vid"})

        dest_dir = tmp_path / "_reports" / "remediation-backup" / "demo"
        for suffix, data in contents.items():
            dst = dest_dir / f"vid{suffix}"
            assert dst.exists(), suffix
            assert dst.read_bytes() == data, suffix

    def test_never_overwrites_an_existing_backup(self, tmp_path):
        channel_dir = self._channel_dir(tmp_path)
        src = channel_dir / "vid.transcript.md"
        src.write_bytes(b"ORIGINAL COLLAPSED CONTENT")
        row = {"channel": "demo", "prefix": "vid"}

        rr.backup_existing(tmp_path, row)
        dst = tmp_path / "_reports" / "remediation-backup" / "demo" / "vid.transcript.md"
        assert dst.read_bytes() == b"ORIGINAL COLLAPSED CONTENT"

        # Simulate a remediation re-run overwriting the live source in place.
        src.write_bytes(b"SECOND RE-RUN CONTENT")
        rr.backup_existing(tmp_path, row)

        # A re-run of a re-run must never clobber the true original.
        assert dst.read_bytes() == b"ORIGINAL COLLAPSED CONTENT"

    def test_tolerates_missing_siblings(self, tmp_path):
        channel_dir = self._channel_dir(tmp_path)
        (channel_dir / "vid.transcript.md").write_bytes(b"only transcript exists")
        row = {"channel": "demo", "prefix": "vid"}

        result = rr.backup_existing(tmp_path, row)

        dest_dir = tmp_path / "_reports" / "remediation-backup" / "demo"
        assert (dest_dir / "vid.transcript.md").exists()
        assert not (dest_dir / "vid.mindmap.md").exists()
        assert not (dest_dir / "vid.concepts.json").exists()
        assert not (dest_dir / "vid.meta.json").exists()
        assert result is not None

    def test_tolerates_unwritable_destination_without_raising(self, tmp_path, monkeypatch):
        channel_dir = self._channel_dir(tmp_path)
        (channel_dir / "vid.transcript.md").write_bytes(b"content")
        row = {"channel": "demo", "prefix": "vid"}

        original_write_bytes = Path.write_bytes

        def failing_write_bytes(self, data):
            if self.name == "vid.transcript.md" and "remediation-backup" in str(self):
                raise OSError("disk full (simulated)")
            return original_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", failing_write_bytes)

        # Must not raise.
        rr.backup_existing(tmp_path, row)

        dest = tmp_path / "_reports" / "remediation-backup" / "demo" / "vid.transcript.md"
        assert not dest.exists()


# ---------------------------------------------------------------------------
# 3. Abort guards, at main() level
# ---------------------------------------------------------------------------


def _write_worklist(reports_dir: Path, rows: list[dict], stamp: str = "2026-08-31") -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{stamp}-remediation-sweep.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _prep_run_corpus(tmp_path, n, channel="demo"):
    """n candidate rows, each with a transcript.md on disk so reassess's
    existence check passes; the assessor itself is stubbed by the caller."""
    output_dir = tmp_path / "corpus"
    channel_dir = output_dir / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(1, n + 1):
        prefix = f"vid{i}"
        (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] placeholder\n", encoding="utf-8")
        rows.append(
            {
                "channel": channel,
                "prefix": prefix,
                "video_id": None,
                "title": f"Video {i}",
                "url": f"https://www.youtube.com/watch?v={prefix}",
                "bucket": "monolithic_severe",
                "dialogue_entries": 1,
            }
        )
    return output_dir, rows


def _run_main(monkeypatch, output_dir, argv_tail):
    monkeypatch.setattr(rr.vi, "load_config", lambda: {})
    monkeypatch.setattr(rr.vi, "resolve_output_dir", lambda _cfg: output_dir)
    monkeypatch.setattr(sys, "argv", ["remediation_run.py", *argv_tail])
    return rr.main()


class TestConsecutiveFailureAbort:
    def test_stops_after_n_hard_failures_and_state_records_only_attempted(self, tmp_path, monkeypatch):
        output_dir, rows = _prep_run_corpus(tmp_path, 5)
        _write_worklist(output_dir / "_reports", rows)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=rr.EXIT_HARD_FAIL, stdout="boom\n")

        monkeypatch.setattr(rr.subprocess, "run", fake_run)
        monkeypatch.setattr(
            rr.vi, "assess_transcript_artifact", lambda *_a, **_kw: {"severe": [], "dialogue_entries": 1}
        )

        _run_main(
            monkeypatch,
            output_dir,
            ["--wave", "all", "--max-consecutive-failures", "2", "--max-consecutive-no-improvement", "99"],
        )

        assert len(calls) == 2, "must stop after exactly 2 hard failures, not run all 5"

        state_path = output_dir / "_reports" / "remediation-state-waveall.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(state) == 2
        assert set(state.keys()) == {"vid1", "vid2"}


class TestNoImprovementAbort:
    def test_fires_even_when_every_exit_code_is_zero(self, tmp_path, monkeypatch):
        """The guard's whole purpose: a green exit code on a transcript that
        re-assesses as still severe must still trip the abort."""
        output_dir, rows = _prep_run_corpus(tmp_path, 5)
        _write_worklist(output_dir / "_reports", rows)

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stdout="all good\n")

        monkeypatch.setattr(rr.subprocess, "run", fake_run)
        # Re-assessment still reports the same collapse: entries unchanged,
        # still severe - a "successful" re-run that did not actually help.
        monkeypatch.setattr(
            rr.vi,
            "assess_transcript_artifact",
            lambda *_a, **_kw: {"severe": ["monolithic_severe"], "dialogue_entries": 1},
        )

        _run_main(
            monkeypatch,
            output_dir,
            ["--wave", "all", "--max-consecutive-failures", "99", "--max-consecutive-no-improvement", "2"],
        )

        assert len(calls) == 2, "every subprocess call exited 0, yet the abort must still fire"

        state_path = output_dir / "_reports" / "remediation-state-waveall.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(state) == 2
        for entry in state.values():
            assert entry["exit"] == 0


class TestSuccessResetsBothCounters:
    def test_interleaved_success_prevents_abort_at_threshold(self, tmp_path, monkeypatch):
        output_dir, rows = _prep_run_corpus(tmp_path, 5)
        _write_worklist(output_dir / "_reports", rows)

        # fail, fail, success, fail, fail - never 3 in a row.
        rc_script = [rr.EXIT_HARD_FAIL, rr.EXIT_HARD_FAIL, 0, rr.EXIT_HARD_FAIL, rr.EXIT_HARD_FAIL]
        assess_script = [
            {"severe": [], "dialogue_entries": 1},
            {"severe": [], "dialogue_entries": 1},
            {"severe": [], "dialogue_entries": 10},  # the success: 10 > before(1)
            {"severe": [], "dialogue_entries": 1},
            {"severe": [], "dialogue_entries": 1},
        ]
        calls = []

        def fake_run(cmd, **kwargs):
            i = len(calls)
            calls.append(cmd)
            return SimpleNamespace(returncode=rc_script[i], stdout="tail\n")

        assess_calls = []

        def fake_assess(*_a, **_kw):
            i = len(assess_calls)
            assess_calls.append(1)
            return assess_script[i]

        monkeypatch.setattr(rr.subprocess, "run", fake_run)
        monkeypatch.setattr(rr.vi, "assess_transcript_artifact", fake_assess)

        _run_main(
            monkeypatch,
            output_dir,
            ["--wave", "all", "--max-consecutive-failures", "3", "--max-consecutive-no-improvement", "99"],
        )

        assert len(calls) == 5, "the mid-run success must reset the failure streak, so all 5 must run"

        state_path = output_dir / "_reports" / "remediation-state-waveall.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert len(state) == 5


# ---------------------------------------------------------------------------
# 4. reassess
# ---------------------------------------------------------------------------


class TestReassess:
    def test_finds_transcript_under_a_rotated_prefix_not_a_stale_cache(self, tmp_path):
        """Title rotation: the pre-run row still names the OLD prefix, but a
        --force re-run wrote the new artifacts under a NEW prefix. reassess
        must re-derive the prefix from _load_video_id_index rather than
        trusting the stale row['prefix'] - and the index cache must be
        invalidated first, or a prior read (from the sweep, or an earlier
        cache hit in this same process) would still point at the old file."""
        output_dir = tmp_path / "corpus"
        channel_dir = output_dir / "demo"
        channel_dir.mkdir(parents=True, exist_ok=True)

        # Pre-rotation state: OLD-PREFIX owns video_id vid42.
        (channel_dir / "OLD-PREFIX.meta.json").write_text(json.dumps({"video_id": "vid42"}), encoding="utf-8")
        (channel_dir / "OLD-PREFIX.transcript.md").write_text("[00:00] stale collapsed line\n", encoding="utf-8")

        # Populate the cache with the stale mapping BEFORE the rotation, the
        # way an earlier sweep pass in the same process would have. If
        # reassess forgot to invalidate, it would still resolve to OLD-PREFIX.
        vi._invalidate_video_id_cache(channel_dir)
        stale_index = vi._load_video_id_index(channel_dir)
        assert stale_index["vid42"] == "OLD-PREFIX"

        # The rotation: --force re-run landed under a new title-derived slug,
        # and the old artifacts are gone.
        (channel_dir / "OLD-PREFIX.meta.json").unlink()
        (channel_dir / "OLD-PREFIX.transcript.md").unlink()
        (channel_dir / "NEW-PREFIX.meta.json").write_text(json.dumps({"video_id": "vid42"}), encoding="utf-8")
        (channel_dir / "NEW-PREFIX.transcript.md").write_text(
            "[00:01] line one\n[00:05] line two\n[00:09] line three\n", encoding="utf-8"
        )

        row = {"channel": "demo", "prefix": "OLD-PREFIX", "video_id": "vid42"}
        result = rr.reassess(output_dir, row)

        assert result["prefix"] == "NEW-PREFIX"
        assert result["entries"] == 3
        assert result["ok"] is True

    def test_missing_transcript_returns_ok_false_without_raising(self, tmp_path):
        output_dir = tmp_path / "corpus"
        (output_dir / "demo").mkdir(parents=True, exist_ok=True)
        row = {"channel": "demo", "prefix": "ghost", "video_id": None}

        result = rr.reassess(output_dir, row)

        assert result == {"ok": False, "reason": "no transcript on disk", "entries": 0, "severe": ["missing"]}


# ---------------------------------------------------------------------------
# 5. latest_sweep
# ---------------------------------------------------------------------------


class TestLatestSweep:
    def test_picks_the_lexicographically_newest(self, tmp_path):
        reports_dir = tmp_path / "_reports"
        reports_dir.mkdir()
        for stamp in ("2026-08-05", "2026-08-31", "2026-08-20"):
            (reports_dir / f"{stamp}-remediation-sweep.json").write_text("[]", encoding="utf-8")

        result = rr.latest_sweep(reports_dir)

        assert result.name == "2026-08-31-remediation-sweep.json"

    def test_raises_system_exit_with_actionable_message_when_none_found(self, tmp_path):
        reports_dir = tmp_path / "_reports"
        reports_dir.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            rr.latest_sweep(reports_dir)

        message = str(exc_info.value)
        assert "remediation_sweep.py" in message
        assert str(reports_dir) in message

    def test_raises_when_reports_dir_does_not_exist_at_all(self, tmp_path):
        reports_dir = tmp_path / "_reports_never_created"

        with pytest.raises(SystemExit):
            rr.latest_sweep(reports_dir)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
