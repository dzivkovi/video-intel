"""Issue #249: the mindmap-from-video Gemini call is capped by a wall-clock timeout.

The mindmap-from-video call had no wall-clock cap and was observed blocked for
39 minutes (the httpx read timeout never fired). It is now wrapped in the same
`_run_with_timeout` helper and knob (`transcript_timeout_seconds`) as the
transcript call. Expiry records `last_error` plus identity in meta.json and
returns an `error:` status, so one hung video never stalls the batch. The
text-only `source="transcript"` call is deliberately left uncapped.

Falsification: drop `timeout_seconds=` from any video-source caller and the
matching caller-level test waits out its 6s sleep and fails the wall-clock bound.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
import time
from types import SimpleNamespace

import pytest

import video_intel as vi

VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
_TYPES = SimpleNamespace(MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH"))
HANG_SECONDS = 6
WALL_CLOCK_BOUND = 4


def _video():
    return {"video_id": VIDEO_ID, "url": URL, "title": "A Talk", "published": "2026-08-12"}


def _sleeping_gemini(*_a, **_kw):
    time.sleep(HANG_SECONDS)
    return "# never reached"


def _only_meta(tmp_path):
    metas = list((tmp_path / "alpha").glob("*.meta.json"))
    assert len(metas) == 1, metas
    return json.loads(metas[0].read_text(encoding="utf-8")), metas[0]


# ---------------------------------------------------------------------------
# 1. process_mindmap, direct
# ---------------------------------------------------------------------------


class TestProcessMindmapVideoCallIsCapped:
    def test_hung_call_returns_error_and_records_last_error_with_identity(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "call_gemini", lambda *a, **kw: time.sleep(3))

        t0 = time.monotonic()
        _prefix, status = vi.process_mindmap(
            None,
            _TYPES,
            _video(),
            "PROMPT",
            "stub-model",
            tmp_path,
            "alpha",
            source="video",
            media_resolution="LOW",
            timeout_seconds=0.3,
        )

        assert time.monotonic() - t0 < 2
        assert status.startswith("error:")
        assert "mindmap Gemini call exceeded" in status
        meta, _ = _only_meta(tmp_path)
        assert "mindmap Gemini call exceeded" in meta["last_error"]
        # [core: identity #66]: the error record must still stamp identity.
        assert meta["video_id"] == VIDEO_ID
        assert meta["channel"] == "alpha"
        assert meta["title"] == "A Talk"
        assert not list((tmp_path / "alpha").glob("*.mindmap.md"))

    def test_zero_disables_the_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "call_gemini", lambda *a, **kw: "# Map\n- a")

        _, status = vi.process_mindmap(
            None,
            _TYPES,
            _video(),
            "PROMPT",
            "stub-model",
            tmp_path,
            "alpha",
            source="video",
            media_resolution="LOW",
            timeout_seconds=0,
        )

        assert status == "done"
        assert list((tmp_path / "alpha").glob("*.mindmap.md"))


# ---------------------------------------------------------------------------
# 2. The transcript source stays uncapped (AST guard)
# ---------------------------------------------------------------------------


def _call_name(node: ast.Call) -> str | None:
    return node.func.id if isinstance(node.func, ast.Name) else None


def _process_mindmap_tree() -> ast.FunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(vi.process_mindmap)))
    return tree.body[0]


def _gemini_calls_inside_timeout_wrapper(fn: ast.FunctionDef) -> tuple[set[str], set[str]]:
    """Return (names called anywhere, names called inside a _run_with_timeout call)."""
    everywhere = {n for c in ast.walk(fn) if isinstance(c, ast.Call) and (n := _call_name(c))}
    wrapped: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node) == "_run_with_timeout":
            wrapped |= {n for c in ast.walk(node) if isinstance(c, ast.Call) and (n := _call_name(c))}
    return everywhere, wrapped


class TestTranscriptSourceIsDeliberatelyUncapped:
    def test_walk_finds_both_gemini_calls_and_the_wrapper(self):
        everywhere, _ = _gemini_calls_inside_timeout_wrapper(_process_mindmap_tree())
        # Companion: without this a renamed callee turns the guard below into a tautology.
        assert {"call_gemini", "call_gemini_text", "_run_with_timeout"} <= everywhere

    def test_only_the_video_call_is_wrapped(self):
        _, wrapped = _gemini_calls_inside_timeout_wrapper(_process_mindmap_tree())
        assert "call_gemini" in wrapped
        assert "call_gemini_text" not in wrapped


# ---------------------------------------------------------------------------
# 3. process --url fallback (caller level)
# ---------------------------------------------------------------------------


def _wire_gemini_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, _TYPES))
    monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
    monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _vid: False)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _vid: 600)
    monkeypatch.setattr(vi, "call_gemini", _sleeping_gemini)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)


def _url_args(**overrides):
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
        "topic": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _channel_config(**extra):
    channel = {"name": "alpha", "url": "https://youtube.com/@alpha", **extra}
    return {"transcript_timeout_seconds": 0.2, "channels": [channel]}


class TestProcessUrlFallbackMindmapIsCappedByTheKnob:
    def test_hung_video_fallback_is_cut_off_and_recorded(self, tmp_path, monkeypatch):
        _wire_gemini_boundary(monkeypatch, tmp_path)
        # A failed transcript step makes the resolver pick source="video".
        monkeypatch.setattr(vi, "process_transcript", lambda *a, **kw: ("2026-08-12-a-talk", "error: boom"))
        monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: ("p", "done"))

        t0 = time.monotonic()
        with pytest.raises(SystemExit) as exc:
            vi.cmd_process(_url_args(), _channel_config())
        elapsed = time.monotonic() - t0

        assert elapsed < WALL_CLOCK_BOUND, f"mindmap waited out the hang ({elapsed:.1f}s)"
        # `_cmd_process_url` exits 1 when the mindmap step returns an error status
        # (a hard failure, not EXIT_PARTIAL). A bare raises(SystemExit) would also be
        # satisfied by exit 0, so the code is asserted [core: real-caller].
        assert exc.value.code == 1
        meta, _ = _only_meta(tmp_path)
        assert "mindmap Gemini call exceeded" in meta["last_error"]


# ---------------------------------------------------------------------------
# 4. mindmap --url (caller level)
# ---------------------------------------------------------------------------


class TestMindmapUrlIsCappedByTheKnob:
    def test_hung_video_source_is_cut_off_and_recorded(self, tmp_path, monkeypatch):
        _wire_gemini_boundary(monkeypatch, tmp_path)
        config = _channel_config(mindmap_source="video")

        t0 = time.monotonic()
        result = vi.cmd_mindmap(_url_args(), config)
        elapsed = time.monotonic() - t0

        # cmd_mindmap logs the error status and returns None: no exit, no raise.
        assert result is None
        assert elapsed < WALL_CLOCK_BOUND, f"mindmap waited out the hang ({elapsed:.1f}s)"
        meta, meta_path = _only_meta(tmp_path)
        assert "mindmap Gemini call exceeded" in meta["last_error"]
        assert not list(meta_path.parent.glob("*.mindmap.md"))


# ---------------------------------------------------------------------------
# 5. scan fallback (caller level)
# ---------------------------------------------------------------------------


class TestScanFallbackMindmapIsCappedByTheKnob:
    def test_hung_video_mindmap_does_not_stall_the_scan(self, tmp_path, monkeypatch):
        video = {"video_id": VIDEO_ID, "title": "A Talk", "published": "2026-08-12", "url": URL}
        _wire_gemini_boundary(monkeypatch, tmp_path)
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: (url, url))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: [dict(video)])
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)
        config = {
            "output_dir": str(tmp_path),
            **_channel_config(auto_transcript="none", mindmap_source="video"),
        }
        scan_args = SimpleNamespace(dry_run=False, channel=None, force=False, since=None, model=None)

        t0 = time.monotonic()
        vi.cmd_scan(scan_args, config)
        elapsed = time.monotonic() - t0

        assert elapsed < WALL_CLOCK_BOUND, f"scan waited out the hang ({elapsed:.1f}s)"
        meta, _ = _only_meta(tmp_path)
        assert "mindmap Gemini call exceeded" in meta["last_error"]


# ---------------------------------------------------------------------------
# 6. Every video-source call site passes the budget (AST)
# ---------------------------------------------------------------------------


def _process_mindmap_call_sites() -> list[tuple[str, ast.Call]]:
    tree = ast.parse(inspect.getsource(vi))
    found: list[tuple[str, ast.Call]] = []

    def visit(node, enclosing):
        for child in ast.iter_child_nodes(node):
            name = enclosing
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and enclosing is None:
                name = child.name
            if isinstance(child, ast.Call) and _call_name(child) == "process_mindmap":
                found.append((name or "<module>", child))
            visit(child, name)

    visit(tree, None)
    return found


def _kw(call: ast.Call) -> dict[str, ast.expr]:
    return {k.arg: k.value for k in call.keywords if k.arg}


def _is_video_source(call: ast.Call) -> bool:
    kws = _kw(call)
    src = kws.get("source")
    return (isinstance(src, ast.Constant) and src.value == "video") or "media_uri" in kws


class TestEveryVideoMindmapCallSitePassesTheBudget:
    def test_walk_finds_call_sites(self):
        sites = _process_mindmap_call_sites()
        assert len(sites) >= 8
        assert any(_is_video_source(c) for _, c in sites)
        assert any(not _is_video_source(c) for _, c in sites)

    def test_video_source_calls_pass_timeout_seconds(self):
        video_sites = [(n, c) for n, c in _process_mindmap_call_sites() if _is_video_source(c)]
        missing = [(n, c.lineno) for n, c in video_sites if "timeout_seconds" not in _kw(c)]
        assert not missing, f"video-source process_mindmap calls without timeout_seconds: {missing}"
        assert {n for n, _ in video_sites} == {"cmd_scan", "_cmd_mindmap_impl", "_cmd_process_url", "_cmd_process_impl"}

    def test_transcript_source_calls_do_not_pass_it(self):
        transcript_sites = [(n, c) for n, c in _process_mindmap_call_sites() if not _is_video_source(c)]
        assert transcript_sites
        passing = [(n, c.lineno) for n, c in transcript_sites if "timeout_seconds" in _kw(c)]
        assert not passing, f"transcript-source calls unexpectedly pass timeout_seconds: {passing}"


# ---------------------------------------------------------------------------
# 7. _run_with_timeout label
# ---------------------------------------------------------------------------


class TestRunWithTimeoutLabel:
    def test_custom_label_leads_the_message(self):
        with pytest.raises(vi.TranscriptTimeout) as exc:
            vi._run_with_timeout(lambda: time.sleep(1), 0.1, label="mindmap Gemini call")
        assert str(exc.value).startswith("mindmap Gemini call exceeded")

    def test_default_label_is_the_transcript_call(self):
        with pytest.raises(vi.TranscriptTimeout) as exc:
            vi._run_with_timeout(lambda: time.sleep(1), 0.1)
        assert str(exc.value).startswith("transcript Gemini call exceeded")
