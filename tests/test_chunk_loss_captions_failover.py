"""Issue #248: a chunk lost to the #74 timeout must route to the captions failover.

A chunked run where one chunk hit the wall-clock timeout (#74) left a `partial`
that the captions failover never filled, because the gate keyed on `error`. Now
a lost-chunks status (`partial (chunks lost: N of M)`) routes to captions under
`transcript_source: auto`: captions replace the whole video and the Gemini
partial is kept as a `.transcript.raw.chunked-partial.txt` sidecar. Under
`gemini` the partial stays and the logged remedy names what actually works.

Falsification: neuter `_finish_chunked_transcript`'s lost-chunks branch (make
`chunked_captions_failover_applies` return False for a lost-chunks status) and
exactly the tests in classes 1 and 6 fail.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
import time
from types import SimpleNamespace
from typing import ClassVar

import pytest
from youtube_captions import CaptionsResult

import video_intel as vi

URL = "https://www.youtube.com/watch?v=abcdefghijk"
CONFIG_AUTO = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "auto"}]}
CONFIG_GEMINI = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "gemini"}]}
_TYPES = SimpleNamespace(MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH"))


def _hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _chunk_payload(start: int, end: int) -> str:
    span = end - start
    marks = [start + int(span * f) for f in (0.02, 0.21, 0.40, 0.59, 0.78, 0.97)]
    return json.dumps(
        {
            "transcripts": [
                {"start": _hms(s), "voice": 1, "text": f"hello from chunk two {i}"} for i, s in enumerate(marks)
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "A"}],
        }
    )


def _args(**overrides):
    base = {
        "url": URL,
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


class _Captions:
    """Recording stand-in for fetch_english_captions."""

    def __init__(self, result, kind="absent"):
        self.result = result
        self.kind = kind  # failure kind reported through reason_sink when result is None
        self.calls: list[str] = []

    def __call__(self, video_id, *, reason_sink=None):
        self.calls.append(video_id)
        if self.result is None and reason_sink is not None:
            reason_sink["kind"] = self.kind
            reason_sink["exception"] = "IpBlocked" if self.kind == "blocked" else None
        return self.result


def _with_track():
    return _Captions(CaptionsResult([(0.0, "cue one"), (1800.0, "cue two"), (3500.0, "cue three")], True, "en"))


@pytest.fixture
def wired(monkeypatch, tmp_path):
    def fake_call_gemini(client, types, media_uri, prompt, model, **kw):
        start = kw.get("start_offset") or 0
        if start == 0:
            raise vi.TranscriptTimeout("stub hang")
        return _chunk_payload(start, kw.get("end_offset") or 3600)

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, _TYPES))
    monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _vid: False)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _vid: 3600)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return tmp_path


def _meta(tmp_path):
    metas = list((tmp_path / "alpha").glob("*.meta.json"))
    assert len(metas) == 1, metas
    meta_path = metas[0]
    prefix = meta_path.name[: -len(".meta.json")]
    return prefix, json.loads(meta_path.read_text(encoding="utf-8")), tmp_path / "alpha"


class TestTranscriptUrlLostChunkUnderAutoFillsWithCaptions:
    def test_captions_replace_the_video_and_partial_is_kept(self, wired, monkeypatch):
        captions = _with_track()
        monkeypatch.setattr(vi, "fetch_english_captions", captions)

        vi.cmd_transcript(_args(), CONFIG_AUTO)

        prefix, meta, cdir = _meta(wired)
        assert len(captions.calls) == 1
        text = (cdir / f"{prefix}.transcript.md").read_text(encoding="utf-8")
        assert "caption" in text.lower()
        assert "FAILED (timeout)" not in text
        assert meta["transcript_source"] == "youtube_captions"
        assert "chunks lost" in meta["transcript_failover_reason"]
        assert "captions replaced the whole video" in meta["transcript_failover_reason"]
        sidecar = cdir / f"{prefix}.transcript.raw.chunked-partial.txt"
        assert sidecar.exists()
        assert "hello from chunk two" in sidecar.read_text(encoding="utf-8")
        assert meta.get("video_id")
        assert meta.get("channel") == "alpha"


class TestTranscriptUrlLostChunkUnderGeminiKeepsPartialAndNamesARunnableRemedy:
    def test_partial_stays_and_remedy_is_runnable(self, wired, monkeypatch, caplog):
        captions = _with_track()
        monkeypatch.setattr(vi, "fetch_english_captions", captions)
        caplog.set_level(logging.WARNING)

        vi.cmd_transcript(_args(), CONFIG_GEMINI)

        prefix, meta, cdir = _meta(wired)
        assert captions.calls == []
        assert "FAILED (timeout)" in (cdir / f"{prefix}.transcript.md").read_text(encoding="utf-8")
        assert meta["transcript_status"] == "partial"
        assert meta["transcript_failed_chunks"] == 1
        assert meta["transcript_chunks"] == 2
        assert "--chunk-minutes 10" in caplog.text
        assert "--transcript-source yt-captions" in caplog.text
        assert "re-run to fill the gap" not in caplog.text
        assert not list(cdir.glob("*.chunked-partial.txt"))


class TestTranscriptUrlLostChunkUnderAutoWithNoCaptionTrack:
    def test_partial_stays_canonical_without_a_sidecar(self, wired, monkeypatch, caplog):
        captions = _Captions(None)
        monkeypatch.setattr(vi, "fetch_english_captions", captions)
        caplog.set_level(logging.WARNING)

        vi.cmd_transcript(_args(), CONFIG_AUTO)

        prefix, meta, cdir = _meta(wired)
        assert len(captions.calls) == 1
        assert "FAILED (timeout)" in (cdir / f"{prefix}.transcript.md").read_text(encoding="utf-8")
        assert meta.get("transcript_source") != "youtube_captions"
        assert meta["transcript_failed_chunks"] == 1
        assert not list(cdir.glob("*.chunked-partial.txt"))
        assert "--chunk-minutes 10" in caplog.text
        # Review round: this run just PROVED the video has no caption track, so
        # recommending `yt-captions` would be a remedy that cannot work.
        assert "--transcript-source yt-captions" not in caplog.text
        assert "no caption track to fill it" in caplog.text


class TestAQualityOnlyPartialNeverTriggersTheFailover:
    """Pins #157 invariant 7: quality flags never trigger the failover."""

    def test_gate_truth_table(self):
        applies = vi.chunked_captions_failover_applies
        assert applies("partial", "auto") is False
        assert applies("partial (quality guard)", "auto") is False
        assert applies("partial (chunks lost: 1 of 2)", "auto") is True
        assert applies("error: boom", "auto") is True
        assert applies("partial (chunks lost: 1 of 2)", "gemini") is False
        assert applies("partial (chunks lost: 1 of 2)", "auto", captions_already_tried=True) is False


def _callers_of_shared_tail() -> set[str]:
    tree = ast.parse(inspect.getsource(vi))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_finish_chunked_transcript"
            for n in ast.walk(node)
        )
    }


class TestEveryChunkedUrlCallSiteUsesTheSharedTail:
    """[core: one-definition] the three YouTube-URL chunked call sites share one tail."""

    SITES: ClassVar[set[str]] = {"_scan_transcribe_one", "_cmd_transcript_impl", "_cmd_process_url"}

    def test_each_site_calls_the_shared_tail(self):
        assert _callers_of_shared_tail() >= self.SITES

    def test_walk_finds_exactly_the_three_sites(self):
        # Companion: the walk finds callers at all, and only these three.
        assert _callers_of_shared_tail() == self.SITES

    def test_local_file_path_does_not_use_it(self):
        assert "_cmd_process_impl" not in _callers_of_shared_tail()


class TestScanTranscribeOneLostChunkUnderAuto:
    def test_scan_unit_fills_with_captions(self, wired, monkeypatch):
        captions = _with_track()
        monkeypatch.setattr(vi, "fetch_english_captions", captions)
        cdir = wired / "alpha"
        video = {"video_id": "abcdefghijk", "url": URL, "title": "A Talk", "published": "2026-08-12"}

        prefix, status = vi._scan_transcribe_one(
            client=object(),
            types=_TYPES,
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=cdir,
            prefix="2026-08-12-a-talk",
            transcript_source="auto",
            transcript_timeout_seconds=30,
            livestream_captions_first=False,
            duration_seconds=3600,
            chunk_minutes=30,
        )

        assert prefix == "2026-08-12-a-talk"
        assert "captions" in status
        meta = json.loads((cdir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert meta["transcript_source"] == "youtube_captions"
        assert len(captions.calls) == 1


def _slow_gemini(client, types, media_uri, prompt, model, **kw):
    # Chunk 1 hangs far past the 0.2s budget; chunk 2 answers at once. If BOTH
    # chunks were lost the run is an error with no meta.json, so one hang keeps
    # the failed-chunk count observable.
    if not kw.get("start_offset"):
        time.sleep(6)
    return _chunk_payload(kw.get("start_offset") or 0, kw.get("end_offset") or 3600)


def _timeout_callers() -> set[str]:
    tree = ast.parse(inspect.getsource(vi))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(n, ast.Call) and getattr(n.func, "id", None) == "resolve_transcript_timeout_seconds"
            for n in ast.walk(node)
        )
    }


class TestManualUrlPathsHonorTheTimeoutKnob:
    """`transcript --url` and `process --url` must read `transcript_timeout_seconds`.

    Falsification: drop the `transcript_timeout_seconds=` kwarg from
    `_cmd_transcript_impl`'s chunked call and the wall-clock test fails or runs
    out at 3s-plus.
    """

    def test_resolver_precedence_and_rejections(self):
        resolve = vi.resolve_transcript_timeout_seconds
        assert resolve({"transcript_timeout_seconds": 5}, {"transcript_timeout_seconds": 9}) == 5
        assert resolve({}, {"transcript_timeout_seconds": 9}) == 9
        assert resolve({}, {}) == vi.TRANSCRIPT_TIMEOUT_DEFAULT
        assert resolve({"transcript_timeout_seconds": 0}, {}) == 0
        assert resolve({"transcript_timeout_seconds": -1}, {}) == -1
        assert resolve({"transcript_timeout_seconds": 2.5}, {}) == 2.5
        for bad in (True, "600", [600]):
            with pytest.raises(ValueError):
                resolve({"transcript_timeout_seconds": bad}, {})

    def test_transcript_url_chunked_path_passes_the_configured_budget(self, wired, monkeypatch):
        monkeypatch.setattr(vi, "call_gemini", _slow_gemini)
        config = {"transcript_timeout_seconds": 0.2, **CONFIG_GEMINI}

        t0 = time.monotonic()
        vi.cmd_transcript(_args(), config)

        assert time.monotonic() - t0 < 4
        _, meta, _ = _meta(wired)
        assert meta["transcript_failed_chunks"] == 1

    def test_process_url_chunked_path_passes_the_configured_budget(self, wired, monkeypatch):
        monkeypatch.setattr(vi, "call_gemini", _slow_gemini)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: ("p", "done"))
        monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: ("p", "done"))
        config = {"transcript_timeout_seconds": 0.2, **CONFIG_GEMINI}

        t0 = time.monotonic()
        try:
            vi.cmd_process(_args(), config)
        except SystemExit as e:
            assert e.code in (0, 3)

        assert time.monotonic() - t0 < 4
        _, meta, _ = _meta(wired)
        assert meta["transcript_failed_chunks"] == 1

    def test_scan_uses_the_same_resolver(self):
        # [core: one-definition]; the set equality is the companion that proves the walk finds callers.
        # Issue #249 added the two mindmap-from-video resolvers (`mindmap --file`
        # and `process --file`); the three transcript sites are unchanged.
        assert _timeout_callers() == {
            "cmd_scan",
            "_cmd_transcript_impl",
            "_cmd_process_url",
            "_cmd_mindmap_impl",
            "_cmd_process_impl",
        }


LOST = "partial (chunks lost: 1 of 2)"
_VIDEO = {"video_id": "abcdefghijk", "url": URL, "title": "A Talk", "published": "2026-08-12"}
_PREFIX = "2026-08-12-a-talk"


def _finish(cdir, status, *, force=False):
    return vi._finish_chunked_transcript(
        status,
        video=_VIDEO,
        channel_dir=cdir,
        prefix=_PREFIX,
        transcript_source="auto",
        captions_already_tried=False,
        force=force,
        duration_seconds=3600,
        chunk_minutes=30,
    )


def _seed(tmp_path, transcript_text):
    cdir = tmp_path / "alpha"
    cdir.mkdir()
    (cdir / f"{_PREFIX}.transcript.md").write_text(transcript_text, encoding="utf-8")
    meta = {"video_id": "abcdefghijk", "channel": "alpha", "video_url": URL, "title": "A Talk"}
    (cdir / f"{_PREFIX}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return cdir


class TestProcessUrlLostChunkUnderAutoFillsWithCaptions:
    def test_process_url_failover_is_driven_under_auto(self, wired, monkeypatch):
        captions = _with_track()
        monkeypatch.setattr(vi, "fetch_english_captions", captions)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: ("p", "done"))
        monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: ("p", "done"))
        try:
            vi.cmd_process(_args(), CONFIG_AUTO)
        except SystemExit as e:
            assert e.code in (0, 3)

        prefix, meta, cdir = _meta(wired)
        assert len(captions.calls) == 1
        assert meta["transcript_source"] == "youtube_captions"
        assert (cdir / f"{prefix}.transcript.raw.chunked-partial.txt").exists()


class TestForceIsOnlyForcedForLostChunks:
    def test_error_status_never_clobbers_a_prior_artifact(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", _with_track())
        cdir = _seed(tmp_path, "PRIOR GOOD TRANSCRIPT")

        _finish(cdir, "error: all chunks failed parsing", force=False)

        assert (cdir / f"{_PREFIX}.transcript.md").read_text(encoding="utf-8") == "PRIOR GOOD TRANSCRIPT"
        assert not list(cdir.glob("*chunked-partial*"))

    def test_lost_chunks_status_replaces_this_run_own_partial(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", _with_track())
        cdir = _seed(tmp_path, "THIS RUN PARTIAL")

        _finish(cdir, LOST, force=False)

        assert "THIS RUN PARTIAL" not in (cdir / f"{_PREFIX}.transcript.md").read_text(encoding="utf-8")
        sidecar = cdir / f"{_PREFIX}.transcript.raw.chunked-partial.txt"
        assert sidecar.read_text(encoding="utf-8") == "THIS RUN PARTIAL"


class TestRemedyLinesAreRunnable:
    def test_small_chunks_and_captions_available(self, caplog):
        caplog.set_level(logging.WARNING)
        vi._log_lost_chunks_remedy(_VIDEO, "alpha", 5, "partial (chunks lost: 1 of 3)")
        assert "--chunk-minutes" not in caplog.text
        assert "--transcript-source yt-captions" in caplog.text

    def test_nothing_left_to_run(self, caplog):
        caplog.set_level(logging.WARNING)
        vi._log_lost_chunks_remedy(_VIDEO, "alpha", 5, "partial (chunks lost: 1 of 3)", captions_available=False)
        assert "--chunk-minutes" not in caplog.text
        assert "--transcript-source yt-captions" not in caplog.text
        assert "what remains" in caplog.text

    def test_large_chunks_suggest_ten(self, caplog):
        caplog.set_level(logging.WARNING)
        vi._log_lost_chunks_remedy(_VIDEO, "alpha", 30, "partial (chunks lost: 1 of 3)")
        assert "--chunk-minutes 10" in caplog.text


class TestRefusedCaptionsKeepTheCaptionsRemedy:
    def test_refusal_keeps_partial_and_the_captions_line(self, wired, monkeypatch, caplog):
        monkeypatch.setattr(vi, "fetch_english_captions", _Captions(None, kind="blocked"))
        caplog.set_level(logging.WARNING)

        vi.cmd_transcript(_args(), CONFIG_AUTO)

        prefix, _meta_json, cdir = _meta(wired)
        assert "FAILED (timeout)" in (cdir / f"{prefix}.transcript.md").read_text(encoding="utf-8")
        assert "REFUSED" in caplog.text
        assert "--transcript-source yt-captions" in caplog.text
        assert not list(cdir.glob("*chunked-partial*"))


class TestSidecarNeverClobbersAnEarlierOne:
    def _setup(self, tmp_path):
        cdir = _seed(tmp_path, "B")
        earlier = cdir / f"{_PREFIX}.transcript.raw.chunked-partial.txt"
        earlier.write_text("A", encoding="utf-8")
        return cdir, earlier

    def test_no_track_leaves_only_the_earlier_sidecar(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", _Captions(None))
        cdir, earlier = self._setup(tmp_path)

        _finish(cdir, LOST)

        assert earlier.read_text(encoding="utf-8") == "A"
        assert list(cdir.glob("*chunked-partial*")) == [earlier]

    def test_track_keeps_both_partials(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", _with_track())
        cdir, earlier = self._setup(tmp_path)

        _finish(cdir, LOST)

        assert earlier.read_text(encoding="utf-8") == "A"
        others = [p for p in cdir.glob("*chunked-partial*.txt") if p != earlier]
        assert len(others) == 1
        assert others[0].read_text(encoding="utf-8") == "B"


class TestScanTailIsInsideThePerVideoNet:
    def test_a_raising_tail_keeps_the_gemini_status(self, wired, monkeypatch):
        def boom(*_a, **_kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(vi, "_finish_chunked_transcript", boom)

        _prefix, status = vi._scan_transcribe_one(
            client=object(),
            types=_TYPES,
            video=_VIDEO,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=wired / "alpha",
            prefix=_PREFIX,
            transcript_source="auto",
            transcript_timeout_seconds=30,
            livestream_captions_first=False,
            duration_seconds=3600,
            chunk_minutes=30,
        )

        assert status.startswith("partial (chunks lost")


class TestScanSurvivesABadTimeoutKnob:
    def test_bad_knob_on_one_channel_does_not_abort_the_scan(self, tmp_path, monkeypatch):
        bad = {**_VIDEO, "video_id": "bad1", "url": "https://www.youtube.com/watch?v=bad1"}
        good = {**_VIDEO, "video_id": "good1", "url": "https://www.youtube.com/watch?v=good1"}
        videos = {"https://example.com/a": [bad], "https://example.com/b": [good]}
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: (url, url))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos.get(cid, [])))
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)
        seen: list[str] = []

        def fake_transcript(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs["video"]
            seen.append(video["video_id"])
            return video["video_id"], "done"

        monkeypatch.setattr(vi, "process_transcript", fake_transcript)
        monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: ("p", "done"))
        bad_channel = {"name": "a", "url": "https://example.com/a", "auto_transcript": "all"}
        bad_channel["transcript_timeout_seconds"] = "600"
        good_channel = {"name": "b", "url": "https://example.com/b", "auto_transcript": "all"}
        config = {"output_dir": str(tmp_path), "channels": [bad_channel, good_channel]}

        vi.cmd_scan(SimpleNamespace(dry_run=False, channel=None, force=False, since=None, model=None), config)

        assert "good1" in seen
