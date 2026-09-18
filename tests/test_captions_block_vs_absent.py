"""A refused caption request is not a video without captions (issue #231).

MEASURED 2026-09-18. `fetch_english_captions` returned usable tracks for
`aX8Y183qDpY` and `NdeOsuoIGuc` at 22:43, and `None` for the same ids at 01:38.
Re-checked at 19:30 the next day - eighteen hours later - all three ids still
raise `IpBlocked`. So the diagnosis in the issue holds (the endpoint is
refusing, the videos have not lost their captions) and its assumed recovery
does not: this is a sustained block, not a transient blip.

Two consequences shaped the fix, and both are pinned below.

1. The library ALREADY distinguishes these cases. `IpBlocked` derives from
   `RequestBlocked`, which derives from `CouldNotRetrieveTranscript`, the same
   base as `NoTranscriptFound`. The pre-fix code caught the base class and
   spoke for every subclass, discarding a distinction that was already made.

2. The issue proposed "one retry after a short sleep". The eighteen-hour
   measurement says no: retrying against an endpoint that is refusing is waste
   at best and extends the block at worst. There is deliberately no retry here,
   and `test_a_refusal_is_not_retried` exists so a future edit cannot add one
   without confronting that evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import youtube_captions as yc  # noqa: E402

import video_intel as vi  # noqa: E402

pytest.importorskip("youtube_transcript_api")
from youtube_transcript_api import _errors as E  # noqa: E402


def _make(klass):
    """Instantiate a library exception without depending on its arity."""
    for args in (("vid",), ("vid", "reason"), ("vid", "reason", "extra")):
        try:
            return klass(*args)
        except TypeError:
            continue
    return klass.__new__(klass)


class TestTheLibraryAlreadyKnewTheDifference:
    """The classification reads the library's own exception classes."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("IpBlocked", yc.CAPTIONS_FAILURE_BLOCKED),
            ("RequestBlocked", yc.CAPTIONS_FAILURE_BLOCKED),
            ("PoTokenRequired", yc.CAPTIONS_FAILURE_BLOCKED),
            ("NoTranscriptFound", yc.CAPTIONS_FAILURE_ABSENT),
            ("TranscriptsDisabled", yc.CAPTIONS_FAILURE_ABSENT),
            ("VideoUnavailable", yc.CAPTIONS_FAILURE_VIDEO),
            ("AgeRestricted", yc.CAPTIONS_FAILURE_VIDEO),
        ],
    )
    def test_each_real_library_exception_classifies(self, name, expected):
        klass = getattr(E, name, None)
        if klass is None:
            pytest.skip(f"{name} not in this youtube-transcript-api version")
        assert yc._exception_kind(_make(klass)) == expected

    def test_classification_walks_the_mro_not_a_name_list(self):
        """`IpBlocked` is classified through its `RequestBlocked` base, so a
        subclass a future release adds is covered without editing a list.

        Falsified by switching `_exception_kind` to compare only
        `type(exc).__name__`: this test then fails for the synthetic subclass
        while every parametrized case above still passes."""

        class FutureBlockVariant(E.RequestBlocked):
            pass

        assert yc._exception_kind(_make(FutureBlockVariant)) == yc.CAPTIONS_FAILURE_BLOCKED

    def test_an_unknown_failure_is_other_not_absent(self):
        """Defaulting an unrecognised failure to "absent" would reintroduce the
        bug for every case the classifier has not met yet."""

        class Unrecognised(E.CouldNotRetrieveTranscript):
            pass

        assert yc._exception_kind(_make(Unrecognised)) == yc.CAPTIONS_FAILURE_OTHER


class TestTheSinkIsOptionalSoTheOtherConsumerIsUntouched:
    """`translate_video.py` shares this module and is operationally separate."""

    def test_without_a_sink_a_block_still_returns_none(self, monkeypatch):
        monkeypatch.setattr(yc, "_exception_kind", yc._exception_kind)
        called = {}

        class FakeApi:
            def list(self, video_id):
                called["hit"] = True
                raise _make(E.IpBlocked)

        monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda *a, **k: FakeApi())
        assert yc.fetch_english_captions("vid") is None
        assert called["hit"] is True

    def test_translate_video_calls_without_a_sink(self):
        """Source-level: the separate script must not have been wired into the
        new keyword as a side effect of this ticket."""
        src = (SCRIPTS / "translate_video.py").read_text(encoding="utf-8")
        assert "reason_sink" not in src, (
            "translate_video.py is operationally separate; it must not inherit "
            "issue #231's sink without its own decision and smoke test"
        )


class TestTheSinkRecordsWhy:
    def _blocked_api(self, monkeypatch, exc):
        class FakeApi:
            def list(self, video_id):
                raise exc

        monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda *a, **k: FakeApi())

    def test_a_block_is_recorded_as_a_refusal(self, monkeypatch):
        self._blocked_api(monkeypatch, _make(E.IpBlocked))
        sink: dict = {}
        assert yc.fetch_english_captions("vid", reason_sink=sink) is None
        assert sink["kind"] == yc.CAPTIONS_FAILURE_BLOCKED
        assert sink["exception"] == "IpBlocked"
        assert yc.captions_failure_is_refusal(sink["kind"]) is True

    def test_a_genuine_absence_is_not_a_refusal(self, monkeypatch):
        self._blocked_api(monkeypatch, _make(E.NoTranscriptFound))
        sink: dict = {}
        assert yc.fetch_english_captions("vid", reason_sink=sink) is None
        assert sink["kind"] == yc.CAPTIONS_FAILURE_ABSENT
        assert yc.captions_failure_is_refusal(sink["kind"]) is False

    def test_a_block_logs_at_warning_with_the_recovery(self, monkeypatch, caplog):
        """An info line is the wrong level for something that fails every video
        in the scan, and a message with no recovery trains the operator to
        ignore it."""
        self._blocked_api(monkeypatch, _make(E.IpBlocked))
        with caplog.at_level("INFO", logger="youtube_captions"):
            yc.fetch_english_captions("vid", reason_sink={})
        blocked = [r for r in caplog.records if r.levelname == "WARNING"]
        assert blocked, "a refusal must not be reported at INFO alongside real absences"
        assert "REFUSED" in blocked[0].getMessage()
        assert yc.CAPTIONS_BLOCK_RECOVERY in blocked[0].getMessage()

    def test_the_recovery_does_not_promise_that_waiting_works(self):
        """The issue assumed "re-run later and it picks up". Measured: the block
        persisted at least 18 hours. A remedy that may not work must not be
        stated as one that will - it teaches distrust of every such message."""
        assert "18 hours" in yc.CAPTIONS_BLOCK_RECOVERY
        assert "gemini" in yc.CAPTIONS_BLOCK_RECOVERY.lower()


class TestTheCallerSaysWhichHappened:
    """Caller-level: drives the REAL `process_transcript`, only the library
    stubbed. A unit test of the classifier would pass with the caller still
    printing the old message - the repo has been bitten by exactly that."""

    def _drive(self, monkeypatch, tmp_path, exc):
        class FakeApi:
            def list(self, video_id):
                raise exc

        monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda *a, **k: FakeApi())
        recorded = {}
        monkeypatch.setattr(vi, "_record_transcript_error", lambda path, msg, **kw: recorded.update(msg=msg))
        video = {
            "video_id": "vid123",
            "title": "T",
            "published": "2026-09-18T00:00:00Z",
            "url": "https://www.youtube.com/watch?v=vid123",
        }
        result = vi.process_transcript(
            None,
            None,
            video,
            "prompt-text",
            "model",
            tmp_path,
            "2026-09-18-t",
            transcript_source="yt-captions",
        )
        return result, recorded

    def test_a_refusal_does_not_claim_the_video_has_no_captions(self, monkeypatch, tmp_path):
        result, recorded = self._drive(monkeypatch, tmp_path, _make(E.IpBlocked))
        _prefix, status = result
        assert "refused" in status, f"status still hides the refusal: {status!r}"
        assert "no captions available" not in status
        assert "refused" in recorded["msg"]
        assert yc.CAPTIONS_BLOCK_RECOVERY in recorded["msg"], (
            "the persisted meta must carry the recovery, not just the log line"
        )

    def test_a_genuine_absence_keeps_the_original_message(self, monkeypatch, tmp_path):
        """The pre-#231 wording is CORRECT for a video that really has no
        English track, so it must survive unchanged."""
        result, recorded = self._drive(monkeypatch, tmp_path, _make(E.NoTranscriptFound))
        _prefix, status = result
        assert status == "error: no captions available (yt-captions)"
        assert "no English captions available" in recorded["msg"]

    def test_a_refusal_is_not_retried(self, monkeypatch, tmp_path):
        """The issue proposed one retry after a short sleep. The eighteen-hour
        measurement says a retry against a refusing endpoint is waste. This
        fails if a future edit adds one."""
        calls = {"n": 0}

        class FakeApi:
            def list(self, video_id):
                calls["n"] += 1
                raise _make(E.IpBlocked)

        monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda *a, **k: FakeApi())
        monkeypatch.setattr(vi, "_record_transcript_error", lambda *a, **k: None)
        vi.process_transcript(
            None,
            None,
            {
                "video_id": "vid123",
                "title": "T",
                "published": "2026-09-18T00:00:00Z",
                "url": "https://www.youtube.com/watch?v=vid123",
            },
            "prompt-text",
            "model",
            tmp_path,
            "2026-09-18-t",
            transcript_source="yt-captions",
        )
        assert calls["n"] == 1, f"a refusal was retried {calls['n']} times"


class TestTheSinkIsPerCallNotShared:
    def test_two_videos_do_not_inherit_each_others_reason(self, monkeypatch):
        """The captions path runs under max_parallel threads. A sink hoisted
        out of the per-video branch would let video B report video A's failure -
        the same mistake the per-chunk usage_capture guardrail prevents."""
        outcomes = iter([_make(E.IpBlocked), _make(E.NoTranscriptFound)])

        class FakeApi:
            def list(self, video_id):
                raise next(outcomes)

        monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", lambda *a, **k: FakeApi())
        first: dict = {}
        second: dict = {}
        yc.fetch_english_captions("a", reason_sink=first)
        yc.fetch_english_captions("b", reason_sink=second)
        assert first["kind"] == yc.CAPTIONS_FAILURE_BLOCKED
        assert second["kind"] == yc.CAPTIONS_FAILURE_ABSENT

    def test_the_caller_builds_the_sink_inside_the_branch(self):
        """Source-level companion: a sink created at function scope would be
        shared across every video the function handles."""
        src = (SCRIPTS / "video_intel.py").read_text(encoding="utf-8")
        marker = 'if transcript_source == "yt-captions":'
        idx = src.index(marker)
        branch = src[idx : idx + 900]
        assert "captions_reason: dict = {}" in branch, (
            "the reason sink must be constructed inside the yt-captions branch"
        )
