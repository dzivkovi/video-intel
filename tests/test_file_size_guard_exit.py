"""The `--file` size guard exits 1, and nothing is uploaded first (issue #185).

Issue #185 reported `process --file <oversized>` logging an ERROR and returning
0 - the false-success shape the tri-state exit contract from #129 exists to
prevent. Executed against the real CLI on a 1.5 GB file, both `process --file`
and `transcript --file` already exit 1, and `git log -L` shows the guard has
carried `sys.exit(1)` since it was introduced in PR #32. The reported behaviour
does not reproduce.

What DID exist is the gap that would let it regress unnoticed: the only
coverage was `tests/test_utils.py`'s bare `pytest.raises(SystemExit)`, which
`SystemExit(0)` satisfies just as happily as `SystemExit(1)`. A test that
cannot tell success from failure is not coverage of an exit code.

These tests assert the CODE, cover both commands (only `transcript` had any),
and assert the ORDERING - a guard that fires after the upload would still exit
1 while charging the operator a multi-minute upload for a rejected file, which
is the repo's standing probe-before-you-pay rule.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_intel as vi


def _oversized(monkeypatch, path: Path) -> None:
    """Report a file as over the threshold without writing a gigabyte."""
    path.write_bytes(b"placeholder")
    real_stat = Path.stat

    def fake_stat(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if self == path:
            return SimpleNamespace(
                st_size=vi.LARGE_FILE_THRESHOLD_BYTES + 1,
                st_mtime=st.st_mtime,
            )
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)


def _refuse_upload(monkeypatch) -> dict:
    """Make any upload attempt loudly visible to the assertions."""
    seen = {"uploads": 0}

    def _boom(*_a, **_kw):
        seen["uploads"] += 1
        raise AssertionError("upload attempted for a file the size guard should have rejected")

    monkeypatch.setattr(vi, "upload_local_video", _boom)
    return seen


def _transcript_args(mp4: Path, **over) -> argparse.Namespace:
    base = dict(
        model=None,
        url=None,
        file=mp4,
        channel=None,
        title=None,
        date=None,
        video_id=None,
        force=False,
        start=None,
        end=None,
        prompt=None,
        media_resolution="low",
        chunk_minutes=None,
        transcript_source=None,
        topic=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _process_args(mp4: Path, **over) -> argparse.Namespace:
    return _transcript_args(mp4, **over)


class TestTheSizeGuardExitsOne:
    """`SystemExit` alone is not the contract - a batch driver reads the CODE."""

    def test_transcript_file_exits_1(self, monkeypatch, tmp_path) -> None:
        mp4 = tmp_path / "huge.mp4"
        _oversized(monkeypatch, mp4)
        monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())

        with pytest.raises(SystemExit) as exc:
            vi.cmd_transcript(_transcript_args(mp4), {})
        assert exc.value.code == 1, "an ERROR that exits 0 is the false-success shape #129 forbids"

    def test_process_file_exits_1(self, monkeypatch, tmp_path) -> None:
        """`process --file` had no exit-code coverage at all before #185."""
        mp4 = tmp_path / "huge.mp4"
        _oversized(monkeypatch, mp4)
        monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())

        with pytest.raises(SystemExit) as exc:
            vi.cmd_process(_process_args(mp4), {})
        assert exc.value.code == 1


class TestTheGuardRunsBeforeTheUpload:
    """Probe before you pay: a guard that fires after the upload still exits 1
    while charging a multi-minute upload for a file it was always going to
    reject. The ORDERING is the assertion - the exit code passes either way."""

    def test_transcript_file_uploads_nothing(self, monkeypatch, tmp_path) -> None:
        mp4 = tmp_path / "huge.mp4"
        _oversized(monkeypatch, mp4)
        seen = _refuse_upload(monkeypatch)
        monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())

        with pytest.raises(SystemExit):
            vi.cmd_transcript(_transcript_args(mp4), {})
        assert seen["uploads"] == 0

    def test_process_file_uploads_nothing(self, monkeypatch, tmp_path) -> None:
        mp4 = tmp_path / "huge.mp4"
        _oversized(monkeypatch, mp4)
        seen = _refuse_upload(monkeypatch)
        monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())

        with pytest.raises(SystemExit):
            vi.cmd_process(_process_args(mp4), {})
        assert seen["uploads"] == 0


class TestASegmentStillBypassesTheGuardOnPurpose:
    """`--start`/`--end` is the documented escape hatch: the operator has said
    which slice to send, so the whole-file size stops being the question. The
    bypass must stay - a test that locks the guard's exit code could otherwise
    be 'tightened' into breaking the only way to process a large local file."""

    def test_an_oversized_file_with_a_segment_is_not_rejected_by_the_guard(self, monkeypatch, tmp_path) -> None:
        mp4 = tmp_path / "huge.mp4"
        _oversized(monkeypatch, mp4)
        reached = {"upload": 0}

        def _stop_after_guard(*_a, **_kw):
            reached["upload"] += 1
            raise RuntimeError("reached the upload, so the size guard did not reject this")

        monkeypatch.setattr(vi, "upload_local_video", _stop_after_guard)
        monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())

        with pytest.raises((RuntimeError, SystemExit)):
            vi.cmd_process(_process_args(mp4, start="00:00", end="00:10"), {})
        assert reached["upload"] == 1, "the segment escape hatch must still reach the upload"


class TestTheMissingFileGuardExitsOneToo:
    """The sibling guard a few lines up, same class."""

    def test_transcript_missing_file_exits_1(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())
        with pytest.raises(SystemExit) as exc:
            vi.cmd_transcript(_transcript_args(tmp_path / "nope.mp4"), {})
        assert exc.value.code == 1

    def test_process_missing_file_exits_1(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())
        with pytest.raises(SystemExit) as exc:
            vi.cmd_process(_process_args(tmp_path / "nope.mp4"), {})
        assert exc.value.code == 1
