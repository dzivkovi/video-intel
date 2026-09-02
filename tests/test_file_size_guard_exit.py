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

Two things a reviewer caught about the FIRST version of this file, both of
which made tests pass for the wrong reason, and both of which are the same
class of defect as the one being tested:

* `_cmd_process_impl` wraps `upload_local_video` in a broad
  `except Exception: ... sys.exit(1)`. So on the process side an exit-code-only
  assertion passes even if the size guard is DELETED - execution simply falls
  through to an upload that fails and exits 1 anyway. Every exit-code test here
  therefore also asserts that the upload was never reached; the two assertions
  together are what pin the guard as the cause.
* Both commands `sys.exit(1)` on a missing `GEMINI_API_KEY` BEFORE the size
  guard runs, and `_cmd_process_impl` calls `resolve_output_dir` before it too
  (which mkdirs `~/video-intel` for an empty config). Without stubbing both,
  these tests would pass vacuously in any environment without the key, and
  would write to the real home directory. `_isolated` handles both.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_intel as vi


def _isolated(monkeypatch, tmp_path) -> None:
    """Reach the guard under test, and touch nothing outside tmp_path.

    Both commands exit 1 on a missing GEMINI_API_KEY before the size guard, and
    `_cmd_process_impl` resolves (and creates) the output dir before it. Either
    would make these tests pass for a reason unrelated to the guard.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: tmp_path / "corpus")
    monkeypatch.setattr(vi, "require_gemini", lambda: (SimpleNamespace(), None))
    monkeypatch.setattr(vi, "create_client", lambda *a, **k: SimpleNamespace())
    # cmd_transcript loads its prompt before the size guard; a missing prompt
    # file exits 1 there and would satisfy both the exit-code and the
    # zero-upload assertion without the guard ever running.
    monkeypatch.setattr(vi, "load_prompt", lambda _name: "stub prompt")


def _oversized(monkeypatch, path: Path) -> None:
    """Report a file as over the threshold without writing a gigabyte."""
    path.write_bytes(b"placeholder")
    real_stat = Path.stat

    def fake_stat(self, *a, **kw):
        st = real_stat(self, *a, **kw)
        if self == path:
            return SimpleNamespace(st_size=vi.LARGE_FILE_THRESHOLD_BYTES + 1, st_mtime=st.st_mtime)
        return st

    monkeypatch.setattr(Path, "stat", fake_stat)


def _watch_upload(monkeypatch) -> dict:
    """Record any upload attempt without raising, so the ORDER is observable."""
    seen = {"uploads": 0}

    def _record(*_a, **_kw):
        seen["uploads"] += 1
        raise RuntimeError("upload reached")

    monkeypatch.setattr(vi, "upload_local_video", _record)
    return seen


def _args(mp4: Path, **over) -> argparse.Namespace:
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


class TestTheSizeGuardExitsOneAndIsTheReason:
    """`SystemExit` alone is not the contract - a batch driver reads the CODE.

    And on the process side the code alone is not enough either: an unrelated
    downstream failure also exits 1, so each test pairs the exit code with
    proof that the upload was never reached.
    """

    def test_transcript_file_exits_1_without_uploading(self, monkeypatch, tmp_path) -> None:
        mp4 = tmp_path / "huge.mp4"
        _isolated(monkeypatch, tmp_path)
        _oversized(monkeypatch, mp4)
        seen = _watch_upload(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            vi.cmd_transcript(_args(mp4), {})
        assert exc.value.code == 1, "an ERROR that exits 0 is the false-success shape #129 forbids"
        assert seen["uploads"] == 0, "the size guard, not a downstream failure, must be the cause"

    def test_process_file_exits_1_without_uploading(self, monkeypatch, tmp_path) -> None:
        """`process --file` had no exit-code coverage at all before #185, and
        its broad `except Exception -> sys.exit(1)` around the upload means the
        exit code by itself proves nothing."""
        mp4 = tmp_path / "huge.mp4"
        _isolated(monkeypatch, tmp_path)
        _oversized(monkeypatch, mp4)
        seen = _watch_upload(monkeypatch)

        with pytest.raises(SystemExit) as exc:
            vi.cmd_process(_args(mp4), {})
        assert exc.value.code == 1
        assert seen["uploads"] == 0, "the size guard, not the upload handler, must be the cause"


#: `has_segment` is `start is not None OR end is not None` - EITHER endpoint
#: counts. Testing only the both-supplied case would leave `or` -> `and` green
#: while breaking every valid `--start`-only and `--end`-only invocation.
SEGMENTS = [
    pytest.param({"start": "00:00", "end": "00:10"}, id="both"),
    pytest.param({"start": "00:05"}, id="start-only"),
    pytest.param({"end": "00:10"}, id="end-only"),
]


class TestTheSegmentBypassIsDeliberate:
    """`--start`/`--end` is the documented escape hatch: the operator has said
    which slice to send, so the whole-file size stops being the question. The
    bypass must stay for BOTH commands - a test that locks the guard's exit
    code could otherwise be 'tightened' into breaking the only way to process a
    large local file."""

    @pytest.mark.parametrize("segment", SEGMENTS)
    def test_process_reaches_the_upload_with_a_segment(self, monkeypatch, tmp_path, segment) -> None:
        mp4 = tmp_path / "huge.mp4"
        _isolated(monkeypatch, tmp_path)
        _oversized(monkeypatch, mp4)
        seen = _watch_upload(monkeypatch)

        with pytest.raises((RuntimeError, SystemExit)):
            vi.cmd_process(_args(mp4, **segment), {})
        assert seen["uploads"] == 1, "the segment escape hatch must still reach the upload"

    @pytest.mark.parametrize("segment", SEGMENTS)
    def test_transcript_reaches_the_upload_with_a_segment(self, monkeypatch, tmp_path, segment) -> None:
        """The two guards are separate code with separate messages, so covering
        one proves nothing about the other."""
        mp4 = tmp_path / "huge.mp4"
        _isolated(monkeypatch, tmp_path)
        _oversized(monkeypatch, mp4)
        seen = _watch_upload(monkeypatch)

        with pytest.raises((RuntimeError, SystemExit)):
            vi.cmd_transcript(_args(mp4, **segment), {})
        assert seen["uploads"] == 1


class TestTheMissingFileGuardExitsOneToo:
    """The sibling guard a few lines up, same class."""

    def test_transcript_missing_file_exits_1(self, monkeypatch, tmp_path) -> None:
        _isolated(monkeypatch, tmp_path)
        seen = _watch_upload(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            vi.cmd_transcript(_args(tmp_path / "nope.mp4"), {})
        assert exc.value.code == 1
        assert seen["uploads"] == 0

    def test_process_missing_file_exits_1(self, monkeypatch, tmp_path) -> None:
        _isolated(monkeypatch, tmp_path)
        seen = _watch_upload(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            vi.cmd_process(_args(tmp_path / "nope.mp4"), {})
        assert exc.value.code == 1
        assert seen["uploads"] == 0


class TestTheseTestsReachTheGuardTheyClaimToTest:
    """Guard the guards: if an earlier check starts short-circuiting these
    paths, every test above would pass vacuously. This one fails instead."""

    @pytest.mark.parametrize("command", ["cmd_process", "cmd_transcript"])
    def test_a_normal_sized_file_is_not_rejected_by_the_size_guard(self, monkeypatch, tmp_path, command) -> None:
        """Both commands need their own canary: their pre-guard preambles are
        different code (cmd_transcript loads a prompt, cmd_process resolves the
        output dir), so one certifies nothing about the other."""
        mp4 = tmp_path / "fine.mp4"
        mp4.write_bytes(b"small enough")
        _isolated(monkeypatch, tmp_path)
        seen = _watch_upload(monkeypatch)

        with pytest.raises((RuntimeError, SystemExit)):
            getattr(vi, command)(_args(mp4), {})
        assert seen["uploads"] == 1, (
            "an under-threshold file must reach the upload; if it does not, the tests above "
            "are passing on an earlier check rather than on the size guard"
        )
