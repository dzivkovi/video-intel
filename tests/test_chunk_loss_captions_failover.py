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

    def __init__(self, result):
        self.result = result
        self.calls: list[str] = []

    def __call__(self, video_id):
        self.calls.append(video_id)
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
        assert "--transcript-source yt-captions" in caplog.text


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
        assert _timeout_callers() == {"cmd_scan", "_cmd_transcript_impl", "_cmd_process_url"}
