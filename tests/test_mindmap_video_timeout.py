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
        assert (meta["video_id"], meta["channel"]) == (VIDEO_ID, "alpha")


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
        assert (meta["video_id"], meta["channel"]) == (VIDEO_ID, "alpha")
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
        assert (meta["video_id"], meta["channel"]) == (VIDEO_ID, "alpha")


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


# ---------------------------------------------------------------------------
# Round 2: bad knob, --file paths, text path, disabled cap
# ---------------------------------------------------------------------------


def _scan_harness(monkeypatch, tmp_path):
    video = {"video_id": VIDEO_ID, "title": "A Talk", "published": "2026-08-12", "url": URL}
    _wire_gemini_boundary(monkeypatch, tmp_path)
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: (url, url))
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: [dict(video)])
    monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)
    return SimpleNamespace(dry_run=False, channel=None, force=False, since=None, model=None)


class TestScanInvalidKnobKeepsTheMindmapCapped:
    def test_string_knob_falls_back_to_the_default_cap_not_the_raw_value(self, tmp_path, monkeypatch, caplog):
        scan_args = _scan_harness(monkeypatch, tmp_path)
        monkeypatch.setattr(vi, "call_gemini", lambda *a, **kw: time.sleep(1.5))
        monkeypatch.setattr(vi, "TRANSCRIPT_TIMEOUT_DEFAULT", 0.2)
        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "alpha",
                    "url": "https://youtube.com/@alpha",
                    "auto_transcript": "none",
                    "mindmap_source": "video",
                    "transcript_timeout_seconds": "600",
                }
            ],
        }

        t0 = time.monotonic()
        with caplog.at_level("ERROR"):
            vi.cmd_scan(scan_args, config)
        elapsed = time.monotonic() - t0

        assert elapsed < WALL_CLOCK_BOUND
        meta, _ = _only_meta(tmp_path)
        # A closure that bound the raw "600" would surface a TypeError/ValueError text here.
        assert "mindmap Gemini call exceeded" in meta["last_error"]
        assert (meta["video_id"], meta["channel"], meta["title"]) == (VIDEO_ID, "alpha", "A Talk")
        errors = [r.message for r in caplog.records if "invalid transcript_timeout_seconds" in r.message]
        assert errors, "the invalid-knob ERROR must be logged"
        assert any("video mindmaps keep the default" in m for m in errors)


# ---------------------------------------------------------------------------
# mindmap --file
# ---------------------------------------------------------------------------


def _file_args(mp4, **overrides):
    return _url_args(url=None, file=str(mp4), **overrides)


def _wire_file_boundary(monkeypatch, tmp_path, uploads):
    _wire_gemini_boundary(monkeypatch, tmp_path)
    monkeypatch.setattr(vi, "upload_local_video", lambda _c, _p: uploads.append(_p) or "files/xyz")
    monkeypatch.setattr(vi, "_local_file_duration_seconds", lambda _p: 600)
    monkeypatch.setattr(vi, "load_taxonomy", lambda _d: {"concepts": {}})
    monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: ("p", "done"))


def _mp4(directory):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "talk.mp4"
    path.write_bytes(b"fake mp4 bytes")
    return path


class TestMindmapFileIsCappedByTheKnob:
    def test_channel_inferred_case(self, tmp_path, monkeypatch):
        uploads: list = []
        _wire_file_boundary(monkeypatch, tmp_path, uploads)
        mp4 = _mp4(tmp_path / "alpha")

        t0 = time.monotonic()
        vi.cmd_mindmap(_file_args(mp4, channel="alpha", video_id=VIDEO_ID), _channel_config())

        assert time.monotonic() - t0 < WALL_CLOCK_BOUND
        meta, _ = _only_meta(tmp_path)
        assert "mindmap Gemini call exceeded" in meta["last_error"]
        assert meta["channel"] == "alpha"
        assert meta["video_id"] == VIDEO_ID
        assert uploads, "the upload must have happened (the cap guards the call after it)"

    def test_standalone_case_uses_the_top_level_knob(self, tmp_path, monkeypatch):
        uploads: list = []
        _wire_file_boundary(monkeypatch, tmp_path, uploads)
        mp4 = _mp4(tmp_path / "loose")
        config = {"transcript_timeout_seconds": 0.2, "channels": [{"name": "alpha", "url": "https://youtube.com/@a"}]}

        t0 = time.monotonic()
        vi.cmd_mindmap(_file_args(mp4, channel=None), config)

        assert time.monotonic() - t0 < WALL_CLOCK_BOUND
        metas = list((tmp_path / "loose").glob("*.meta.json"))
        assert len(metas) == 1
        meta = json.loads(metas[0].read_text(encoding="utf-8"))
        assert "mindmap Gemini call exceeded" in meta["last_error"]
        assert meta["video_id"]

    def test_bad_knob_exits_before_the_upload(self, tmp_path, monkeypatch):
        uploads: list = []
        _wire_file_boundary(monkeypatch, tmp_path, uploads)
        mp4 = _mp4(tmp_path / "alpha")
        config = _channel_config()
        config["transcript_timeout_seconds"] = "600"

        with pytest.raises(SystemExit) as exc:
            vi.cmd_mindmap(_file_args(mp4, channel="alpha"), config)

        assert exc.value.code == 1
        assert uploads == [], "probe before pay: the upload must never happen"


# ---------------------------------------------------------------------------
# process --file
# ---------------------------------------------------------------------------


class TestProcessFileIsCappedByTheKnob:
    def test_video_mindmap_is_capped_and_exit_is_one(self, tmp_path, monkeypatch):
        uploads: list = []
        _wire_file_boundary(monkeypatch, tmp_path, uploads)
        monkeypatch.setattr(vi, "process_transcript", lambda *a, **kw: ("talk", "error: boom"))
        mp4 = _mp4(tmp_path / "alpha")

        t0 = time.monotonic()
        with pytest.raises(SystemExit) as exc:
            vi.cmd_process(_file_args(mp4, channel="alpha"), _channel_config())

        assert time.monotonic() - t0 < WALL_CLOCK_BOUND
        # A mindmap error is a hard failure (1), never EXIT_PARTIAL (3).
        assert exc.value.code == 1
        meta, _ = _only_meta(tmp_path)
        assert "mindmap Gemini call exceeded" in meta["last_error"]
        assert meta["channel"] == "alpha"

    def test_single_shot_transcript_step_is_capped_too(self, tmp_path, monkeypatch, caplog):
        uploads: list = []
        _wire_file_boundary(monkeypatch, tmp_path, uploads)
        # Real process_transcript, real mindmap: both hit the 6s sleeping call_gemini.
        mp4 = _mp4(tmp_path / "alpha")

        t0 = time.monotonic()
        with caplog.at_level("INFO"), pytest.raises(SystemExit):
            vi.cmd_process(_file_args(mp4, channel="alpha"), _channel_config())
        elapsed = time.monotonic() - t0

        # Uncapped, either step alone would wait out HANG_SECONDS.
        assert elapsed < WALL_CLOCK_BOUND, f"a step waited out the hang ({elapsed:.1f}s)"
        # The mindmap error later overwrites meta.last_error, so the transcript
        # step's own status is read from its log line.
        assert any("transcript Gemini call exceeded" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# mindmap --url, bad knob versus the text path
# ---------------------------------------------------------------------------


class TestMindmapUrlBadKnobDoesNotBlockTheTextPath:
    def test_transcript_source_still_writes_the_mindmap(self, tmp_path, monkeypatch):
        _wire_gemini_boundary(monkeypatch, tmp_path)
        cdir = tmp_path / "alpha"
        cdir.mkdir()
        (cdir / "2026-08-12-a-talk.transcript.md").write_text("hello transcript", encoding="utf-8")
        monkeypatch.setattr(vi, "call_gemini_text", lambda *a, **kw: "# Map\n- a")
        config = _channel_config(mindmap_source="transcript", transcript_timeout_seconds="600s")

        vi.cmd_mindmap(_url_args(), config)

        assert list(cdir.glob("*.mindmap.md")), "the text-source mindmap must be written"

    def test_video_source_exits_before_any_lookup(self, tmp_path, monkeypatch):
        _wire_gemini_boundary(monkeypatch, tmp_path)
        lookups: list = []
        monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda vid: lookups.append(vid) or 600)
        config = _channel_config(mindmap_source="video", transcript_timeout_seconds="600s")

        with pytest.raises(SystemExit) as exc:
            vi.cmd_mindmap(_url_args(), config)

        assert exc.value.code == 1
        assert lookups == [], "probe before pay: no YouTube lookup for an unusable knob"


# ---------------------------------------------------------------------------
# Disabled cap
# ---------------------------------------------------------------------------


class TestZeroKnobDisablesTheCapAtTheCaller:
    def test_mindmap_url_completes_when_the_knob_is_zero(self, tmp_path, monkeypatch):
        _wire_gemini_boundary(monkeypatch, tmp_path)
        monkeypatch.setattr(vi, "call_gemini", lambda *a, **kw: time.sleep(0.5) or "# Map\n- a")
        config = _channel_config(mindmap_source="video", transcript_timeout_seconds=0)

        vi.cmd_mindmap(_url_args(), config)

        assert list((tmp_path / "alpha").glob("*.mindmap.md"))
