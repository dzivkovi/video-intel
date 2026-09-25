"""An aired premiere is not a livestream VOD, and the tool can now tell (issue #245).

YouTube attaches ``liveStreamingDetails`` to an aired PREMIERE of an ordinary
upload exactly as to a genuine livestream, so issue #120's classifier routed
premieres captions-first: speech-only transcripts, no SCREEN blocks, status
``complete``, exit 0. Measured on one conference channel: 20 slide-less
transcripts accumulated silently, then a 288-video backfill hit it on video 1.

yt-dlp exposes YouTube's own ``isLiveContent`` verdict as ``live_status``, and it
separated 8/8 premieres (``not_live``) from 9/9 real livestreams (``was_live``)
on the videos that motivated #120. The refinement runs only for videos the Data
API flagged, only when yt-dlp is on PATH, and only a validated ``not_live``
clears the flag - every failure keeps today's captions-first routing.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
from argparse import Namespace
from types import SimpleNamespace

import pytest

import video_intel as vi

# ---------------------------------------------------------------------------
# Probe-level stubs
# ---------------------------------------------------------------------------


def _install_probe_exe(monkeypatch, exe="C:/fake/yt-dlp.exe"):
    """Make the memoized executable lookup report yt-dlp as present."""
    monkeypatch.setattr(vi, "_PREMIERE_PROBE_EXE_MEMO", {"exe": exe})


def _stub_subprocess(monkeypatch, *, stdout="", returncode=0, raises=None, calls=None):
    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(vi.subprocess, "run", fake_run)


class TestProbeLiveStatus:
    def test_absent_executable_spawns_nothing_and_reports_unknown(self, monkeypatch):
        monkeypatch.setattr(vi, "_PREMIERE_PROBE_EXE_MEMO", {})
        monkeypatch.setattr(vi.shutil, "which", lambda _name: None)
        calls: list = []
        _stub_subprocess(monkeypatch, stdout="not_live", calls=calls)

        assert vi.probe_live_status("abcdefghijk") is None
        assert calls == [], "no yt-dlp on PATH must mean no subprocess at all"

    def test_not_live_is_reported_verbatim(self, monkeypatch):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="not_live\n")
        assert vi.probe_live_status("abcdefghijk") == "not_live"

    def test_was_live_is_reported_verbatim(self, monkeypatch):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="was_live\n")
        assert vi.probe_live_status("abcdefghijk") == "was_live"

    def test_the_last_non_empty_line_is_the_verdict(self, monkeypatch):
        """A stray leading line (a notice yt-dlp printed anyway) must not hide the status."""
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="[youtube] abcdefghijk: Downloading webpage\nnot_live\n\n")
        assert vi.probe_live_status("abcdefghijk") == "not_live"

    @pytest.mark.parametrize("stdout", ["", "NA", "garbage", "not_live extra words"])
    def test_unknown_output_is_unknown_not_a_verdict(self, monkeypatch, stdout):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout=stdout)
        assert vi.probe_live_status("abcdefghijk") is None

    def test_non_zero_exit_is_unknown(self, monkeypatch):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="not_live", returncode=1)
        assert vi.probe_live_status("abcdefghijk") is None

    @pytest.mark.parametrize(
        "exc",
        [subprocess.TimeoutExpired(cmd="yt-dlp", timeout=1), OSError("boom"), ValueError("bad arg")],
    )
    def test_a_failed_spawn_is_unknown_never_a_raise(self, monkeypatch, exc):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, raises=exc)
        assert vi.probe_live_status("abcdefghijk") is None

    def test_the_invocation_is_an_argument_list_with_the_safety_flags(self, monkeypatch):
        _install_probe_exe(monkeypatch, exe="C:/fake/yt-dlp.exe")
        calls: list = []
        _stub_subprocess(monkeypatch, stdout="not_live", calls=calls)

        vi.probe_live_status("abcdefghijk")

        (argv, kwargs), *_ = calls
        assert isinstance(argv, list) and argv[0] == "C:/fake/yt-dlp.exe"
        for flag in ("--skip-download", "--no-playlist", "--ignore-config", "--print"):
            assert flag in argv, f"{flag} missing: the probe must never download, walk a playlist, or read user config"
        assert argv[-1] == "https://www.youtube.com/watch?v=abcdefghijk"
        assert kwargs.get("shell") is not True
        assert kwargs.get("timeout") == vi.PREMIERE_PROBE_TIMEOUT_SECONDS


class TestRefineWasLivestream:
    def test_a_regular_upload_never_probes(self, monkeypatch):
        _install_probe_exe(monkeypatch)
        calls: list = []
        _stub_subprocess(monkeypatch, stdout="not_live", calls=calls)

        assert vi.refine_was_livestream("abcdefghijk", False) is False
        assert calls == [], "an unflagged video must pay no network at all"

    def test_not_live_clears_the_flag(self, monkeypatch, caplog):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="not_live")
        with caplog.at_level("INFO", logger="video_intel"):
            assert vi.refine_was_livestream("abcdefghijk", True) is False
        assert any("premiere" in r.getMessage().lower() and "abcdefghijk" in r.getMessage() for r in caplog.records), (
            "clearing the flag must be auditable from the log, per video"
        )

    def test_was_live_keeps_the_flag(self, monkeypatch):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout="was_live")
        assert vi.refine_was_livestream("abcdefghijk", True) is True

    def test_one_video_is_probed_once_per_process(self, monkeypatch):
        """Two consumers can ask about one video; the second ask must not spawn yt-dlp again."""
        _install_probe_exe(monkeypatch)
        calls: list = []
        _stub_subprocess(monkeypatch, stdout="not_live", calls=calls)

        assert vi.refine_was_livestream("abcdefghijk", True) is False
        assert vi.refine_was_livestream("abcdefghijk", True) is False
        assert vi.refine_was_livestream("zzzzzzzzzzz", True) is False

        assert [argv[-1] for argv, _ in calls] == [
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://www.youtube.com/watch?v=zzzzzzzzzzz",
        ]

    @pytest.mark.parametrize("stdout", ["post_live", "is_live", "is_upcoming"])
    def test_other_live_statuses_keep_the_flag(self, monkeypatch, stdout):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, stdout=stdout)
        assert vi.refine_was_livestream("abcdefghijk", True) is True

    def test_an_unclassifiable_video_keeps_the_flag_and_warns(self, monkeypatch, caplog):
        _install_probe_exe(monkeypatch)
        _stub_subprocess(monkeypatch, raises=subprocess.TimeoutExpired(cmd="yt-dlp", timeout=1))
        with caplog.at_level("WARNING", logger="video_intel"):
            assert vi.refine_was_livestream("abcdefghijk", True) is True
        assert any(r.levelname == "WARNING" and "abcdefghijk" in r.getMessage() for r in caplog.records)

    def test_absent_yt_dlp_keeps_the_flag_with_one_notice_per_process(self, monkeypatch, caplog):
        monkeypatch.setattr(vi, "_PREMIERE_PROBE_EXE_MEMO", {})
        monkeypatch.setattr(vi.shutil, "which", lambda _name: None)
        with caplog.at_level("INFO", logger="video_intel"):
            assert vi.refine_was_livestream("aaaaaaaaaaa", True) is True
            assert vi.refine_was_livestream("bbbbbbbbbbb", True) is True
        notices = [r for r in caplog.records if "yt-dlp" in r.getMessage() and "not found" in r.getMessage()]
        assert len(notices) == 1, "the absence notice is once per process, not once per flagged video"
        assert "transcript_source: gemini" in notices[0].getMessage(), "the notice names the opt-out"


# ---------------------------------------------------------------------------
# Caller level: the scan
# ---------------------------------------------------------------------------


def _scan_args(**overrides):
    base = {"dry_run": False, "channel": None, "force": False, "since": None, "model": None}
    base.update(overrides)
    return Namespace(**base)


def _scan_setup(monkeypatch, videos, statuses, transcript_result, *, live_status):
    """Wire the real cmd_scan with the YouTube, Gemini and yt-dlp layers stubbed.

    ``live_status`` is what the yt-dlp probe reports for EVERY probed id (or
    ``None`` for "could not classify"); the probe records which ids it saw.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
    monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos))
    monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids, "PT20M"))
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: statuses.get(vid, {}) for vid in ids})
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

    captured: dict = {"transcript": [], "mindmap": [], "probed": [], "probe_threads": []}

    def fake_probe(video_id):
        captured["probed"].append(video_id)
        captured["probe_threads"].append(threading.current_thread().name)
        return live_status

    _install_probe_exe(monkeypatch)
    monkeypatch.setattr(vi, "probe_live_status", fake_probe)

    def fake_process_transcript(*args, **kwargs):
        video = args[2]
        prefix = args[6]
        captured["transcript"].append((video["video_id"], kwargs.get("livestream_captions_first")))
        return (prefix, transcript_result.get(video["video_id"], "done"))

    def fake_process_mindmap(*args, **kwargs):
        video = args[2]
        captured["mindmap"].append((video["video_id"], kwargs.get("source")))
        return (video["video_id"], "done")

    monkeypatch.setattr(vi, "process_transcript", fake_process_transcript)
    monkeypatch.setattr(vi, "process_mindmap", fake_process_mindmap)
    return captured


_FLAGGED = {"live_broadcast_content": "none", "privacy_status": "public", "was_livestream": True}
_PLAIN = {"live_broadcast_content": "none", "privacy_status": "public", "was_livestream": False}


class TestCmdScanPremiereRefinement:
    def _config(self, tmp_path):
        return {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch", "auto_transcript": "all"}],
        }

    def test_a_premiere_routes_gemini_first_and_keeps_its_video_mindmap_fallback(self, tmp_path, monkeypatch):
        """Both #120 consumers must see the refined flag, not just the transcript router."""
        videos = [{"video_id": "prem1", "title": "Premiered talk", "published": "2026-06-13"}]
        captured = _scan_setup(
            monkeypatch, videos, {"prem1": _FLAGGED}, {"prem1": "error: 400 INVALID_ARGUMENT"}, live_status="not_live"
        )

        vi.cmd_scan(_scan_args(), self._config(tmp_path))

        # Both consumers asked (the transcript router, then the mindmap
        # suppression on the failed transcript); the memo keeps it to ONE probe.
        assert captured["probed"] == ["prem1"]
        assert captured["transcript"] == [("prem1", False)], "a premiere is an ordinary upload: Gemini first"
        assert captured["mindmap"] == [("prem1", "video")], (
            "the #120 mindmap suppression must lift too - a premiere's URI is fetchable"
        )

    def test_a_real_livestream_is_unchanged(self, tmp_path, monkeypatch):
        videos = [{"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"}]
        captured = _scan_setup(
            monkeypatch, videos, {"vod1": _FLAGGED}, {"vod1": "error: 400 INVALID_ARGUMENT"}, live_status="was_live"
        )

        vi.cmd_scan(_scan_args(), self._config(tmp_path))

        assert captured["transcript"] == [("vod1", True)]
        assert captured["mindmap"] == [], "no mindmap-from-video call may be spent on a broken livestream URI"

    def test_an_unclassifiable_flagged_video_keeps_captions_first(self, tmp_path, monkeypatch):
        """Fail-safe direction: an absent signal never becomes a positive premiere verdict."""
        videos = [{"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"}]
        captured = _scan_setup(monkeypatch, videos, {"vod1": _FLAGGED}, {}, live_status=None)

        vi.cmd_scan(_scan_args(), self._config(tmp_path))

        assert captured["transcript"] == [("vod1", True)]

    def test_regular_uploads_are_never_probed(self, tmp_path, monkeypatch):
        videos = [
            {"video_id": "reg1", "title": "Regular", "published": "2026-06-13"},
            {"video_id": "prem1", "title": "Premiered", "published": "2026-06-13"},
        ]
        captured = _scan_setup(monkeypatch, videos, {"reg1": _PLAIN, "prem1": _FLAGGED}, {}, live_status="not_live")

        vi.cmd_scan(_scan_args(), self._config(tmp_path))

        assert captured["probed"] == ["prem1"], "only Data-API-flagged videos pay for a probe"
        # Sorted because the transcript stage runs under a thread pool; list
        # equality (not dict) so a duplicate dispatch for one id cannot hide.
        assert sorted(captured["transcript"]) == [("prem1", False), ("reg1", False)]

    def test_explicit_channel_gemini_still_wins_without_needing_the_probe(self, tmp_path, monkeypatch):
        """The #120 escape hatch is untouched: an explicit gemini is Gemini-first, and it never pays a probe."""
        videos = [{"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"}]
        captured = _scan_setup(monkeypatch, videos, {"vod1": _FLAGGED}, {}, live_status="was_live")
        config = self._config(tmp_path)
        config["channels"][0]["transcript_source"] = "gemini"

        vi.cmd_scan(_scan_args(), config)

        assert captured["transcript"] == [("vod1", False)]
        assert captured["probed"] == [], "a probe cannot change an explicit gemini's routing, so it is never spent"

    def test_an_already_processed_flagged_video_is_not_probed(self, tmp_path, monkeypatch):
        """The scan re-lists a processed video every run; the probe must not be paid every run."""
        videos = [{"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"}]
        captured = _scan_setup(monkeypatch, videos, {"vod1": _FLAGGED}, {}, live_status="not_live")
        ch_dir = tmp_path / "ch"
        ch_dir.mkdir()
        prefix = vi.video_file_prefix(videos[0])
        (ch_dir / f"{prefix}.transcript.md").write_text("# done\n", encoding="utf-8")
        (ch_dir / f"{prefix}.mindmap.md").write_text("# done\n", encoding="utf-8")
        (ch_dir / f"{prefix}.meta.json").write_text(
            '{"video_id": "vod1", "modes_completed": ["transcript", "mindmap"]}', encoding="utf-8"
        )

        vi.cmd_scan(_scan_args(), self._config(tmp_path))

        assert captured["transcript"] == [] and captured["mindmap"] == []
        assert captured["probed"] == [], "nothing was going to be routed, so nothing may be probed"

    def test_dry_run_never_probes(self, tmp_path, monkeypatch):
        videos = [{"video_id": "vod1", "title": "Live VOD", "published": "2026-06-13"}]
        captured = _scan_setup(monkeypatch, videos, {"vod1": _FLAGGED}, {}, live_status="not_live")

        vi.cmd_scan(_scan_args(dry_run=True), self._config(tmp_path))

        assert captured["transcript"] == [] and captured["mindmap"] == []
        assert captured["probed"] == [], "a preview spends no network on classification"

    def test_the_probe_runs_in_the_worker_not_the_submit_loop(self, tmp_path, monkeypatch):
        """An argument evaluated at executor.submit() time runs on the main thread, serially,
        before any job starts - 5 s per flagged video ahead of the whole stage (Codex peer pass)."""
        videos = [{"video_id": "prem1", "title": "Premiered talk", "published": "2026-06-13"}]
        captured = _scan_setup(monkeypatch, videos, {"prem1": _FLAGGED}, {}, live_status="not_live")

        vi.cmd_scan(_scan_args(), self._config(tmp_path))

        assert captured["probed"] == ["prem1"]
        assert captured["probe_threads"] and all(name != "MainThread" for name in captured["probe_threads"])

    def test_a_flagged_video_routed_to_captions_by_duration_is_not_probed(self, tmp_path, monkeypatch):
        """captions_over_duration_seconds (#227) already decided yt-captions; the flag cannot change that."""
        videos = [{"video_id": "vod1", "title": "Long VOD", "published": "2026-06-13"}]
        captured = _scan_setup(monkeypatch, videos, {"vod1": _FLAGGED}, {}, live_status="not_live")
        config = self._config(tmp_path)
        config["channels"][0]["captions_over_duration_seconds"] = 600  # the stubbed duration is PT20M

        vi.cmd_scan(_scan_args(), config)

        assert captured["transcript"] == [("vod1", False)]
        assert captured["probed"] == [], "process_transcript takes the yt-captions branch before it reads the flag"


# ---------------------------------------------------------------------------
# Caller level: the manual --url commands
# ---------------------------------------------------------------------------


def _preflight_flagged(monkeypatch):
    """The Data API says 'livestream' for every id; the probe decides what that means."""
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-key")
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: object())
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: dict(_FLAGGED) for vid in ids})


def _recording_probe(monkeypatch, live_status):
    """Stub the probe and return the list of ids it was asked about."""
    probed: list[str] = []

    def fake_probe(video_id):
        probed.append(video_id)
        return live_status

    monkeypatch.setattr(vi, "probe_live_status", fake_probe)
    return probed


def _transcript_args(**overrides):
    base = {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "file": None,
        "channel": "alpha",
        "title": "A Talk",
        "date": "2026-08-12",
        "start": None,
        "end": None,
        "force": False,
        "transcript_source": None,
        "media_resolution": "low",
        "chunk_minutes": None,
        "prompt": None,
        "model": None,
        "video_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _RecordingTranscript:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return ("2026-08-12-a-talk", "done")


@pytest.fixture
def transcript_wired(monkeypatch, tmp_path):
    recorder = _RecordingTranscript()
    monkeypatch.setattr(vi, "process_transcript", recorder)
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _vid: 600)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    _preflight_flagged(monkeypatch)
    _install_probe_exe(monkeypatch)
    return recorder


_CONFIG_PLAIN = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha"}]}


class TestManualTranscriptUrl:
    def test_a_premiere_is_gemini_first(self, transcript_wired, monkeypatch):
        monkeypatch.setattr(vi, "probe_live_status", lambda _vid: "not_live")

        vi.cmd_transcript(_transcript_args(), _CONFIG_PLAIN)

        assert transcript_wired.calls[0]["livestream_captions_first"] is False

    def test_a_real_livestream_stays_captions_first(self, transcript_wired, monkeypatch):
        monkeypatch.setattr(vi, "probe_live_status", lambda _vid: "was_live")

        vi.cmd_transcript(_transcript_args(), _CONFIG_PLAIN)

        assert transcript_wired.calls[0]["livestream_captions_first"] is True

    def test_explicit_cli_gemini_never_probes(self, transcript_wired, monkeypatch):
        probed = _recording_probe(monkeypatch, "was_live")

        vi.cmd_transcript(_transcript_args(transcript_source="gemini"), _CONFIG_PLAIN)

        assert transcript_wired.calls[0]["livestream_captions_first"] is False
        assert probed == []

    def test_explicit_channel_gemini_never_probes(self, transcript_wired, monkeypatch):
        probed = _recording_probe(monkeypatch, "was_live")
        config = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "gemini"}]}

        vi.cmd_transcript(_transcript_args(), config)

        assert transcript_wired.calls[0]["livestream_captions_first"] is False
        assert probed == []


class TestManualProcessUrl:
    """The second manual door: process --url resolves the flag through the same helper."""

    def _wire(self, monkeypatch, tmp_path, *, live_status):
        recorder = _RecordingTranscript()
        monkeypatch.setattr(vi, "process_transcript", recorder)
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
        monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _vid: 600)
        monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: ("2026-08-12-a-talk", "done"))
        monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: ("2026-08-12-a-talk", "done"))
        monkeypatch.setattr(vi, "load_taxonomy", lambda _d: {"concepts": {}})
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        _preflight_flagged(monkeypatch)
        _install_probe_exe(monkeypatch)
        self.probed = _recording_probe(monkeypatch, live_status)
        return recorder

    def _args(self, **overrides):
        return _transcript_args(topic=None, captions_over_duration=None, **overrides)

    def test_explicit_cli_gemini_never_probes(self, monkeypatch, tmp_path):
        recorder = self._wire(monkeypatch, tmp_path, live_status="was_live")
        with contextlib.suppress(SystemExit):
            vi.cmd_process(self._args(transcript_source="gemini"), _CONFIG_PLAIN)
        assert recorder.calls and recorder.calls[0]["livestream_captions_first"] is False
        assert self.probed == []

    def test_a_premiere_is_gemini_first(self, monkeypatch, tmp_path):
        recorder = self._wire(monkeypatch, tmp_path, live_status="not_live")
        # The stubbed steps leave no artifacts, so the tri-state exit (#129)
        # reports partial; the routing decision under test happened before it.
        with contextlib.suppress(SystemExit):
            vi.cmd_process(self._args(), _CONFIG_PLAIN)
        assert recorder.calls and recorder.calls[0]["livestream_captions_first"] is False

    def test_a_real_livestream_stays_captions_first(self, monkeypatch, tmp_path):
        recorder = self._wire(monkeypatch, tmp_path, live_status="was_live")
        with contextlib.suppress(SystemExit):
            vi.cmd_process(self._args(), _CONFIG_PLAIN)
        assert recorder.calls and recorder.calls[0]["livestream_captions_first"] is True
