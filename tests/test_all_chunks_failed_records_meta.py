"""Issue #277 (duplicate #256): an all-chunks-failed chunked run must leave a sweepable meta.

When EVERY chunk of a chunked YouTube-URL transcript fails, `_run_chunked_transcript_url`
writes the raw sidecars and returns `error: all chunks failed parsing ...` without
touching meta.json. The shared tail `_finish_chunked_transcript` (the one place all
three URL callers pass through) now records the FINAL error status through
`_record_transcript_error(video=...)`: identity filled only where absent, plus
`last_error`, on the chunked writer's own meta path. A captions replacement under
`auto` already wrote a healthy meta, so nothing more is recorded then.

Falsification: delete the `_record_transcript_error` call in `_finish_chunked_transcript`
and the acceptance tests for all three callers fail; make the identity merge overwrite
instead of fill-only and `TestExistingMetaSurvives` fails.
"""

from __future__ import annotations

import ast
import inspect
import json
from types import SimpleNamespace

import pytest
from youtube_captions import CaptionsResult

import video_intel as vi

VIDEO_ID = "abcdefghijk"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
CONFIG_AUTO = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "auto"}]}
CONFIG_GEMINI = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "transcript_source": "gemini"}]}
_TYPES = SimpleNamespace(MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH"))
_VIDEO = {"video_id": VIDEO_ID, "url": URL, "title": "A Talk", "published": "2026-08-12"}
PREFIX = vi.video_file_prefix(_VIDEO)
ALL_FAILED = "all chunks failed"


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

    def __call__(self, video_id, *, reason_sink=None):
        self.calls.append(video_id)
        if self.result is None and reason_sink is not None:
            reason_sink["kind"] = "absent"
            reason_sink["exception"] = None
        return self.result


def _with_track():
    return _Captions(CaptionsResult([(0.0, "cue one"), (1800.0, "cue two"), (3500.0, "cue three")], True, "en"))


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Real commands; only the Gemini, captions and YouTube boundaries are stubbed. Every chunk hangs."""
    gemini_calls: list[int] = []

    def every_chunk_hangs(client, types, media_uri, prompt, model, **kw):
        gemini_calls.append(kw.get("start_offset") or 0)
        raise vi.TranscriptTimeout("stub hang")

    monkeypatch.setattr(vi, "call_gemini", every_chunk_hangs)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, _TYPES))
    monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _vid: False)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _vid: 3600)
    monkeypatch.setattr(vi, "fetch_english_captions", _Captions(None))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    vi._invalidate_video_id_cache()
    yield SimpleNamespace(root=tmp_path, cdir=tmp_path / "alpha", gemini_calls=gemini_calls)
    vi._invalidate_video_id_cache()


def _meta(cdir):
    return json.loads((cdir / f"{PREFIX}.meta.json").read_text(encoding="utf-8"))


def _assert_failure_recorded(cdir):
    meta_path = cdir / f"{PREFIX}.meta.json"
    assert meta_path.exists(), "an all-chunks-failed run must leave a meta.json"
    meta = _meta(cdir)
    assert meta["video_id"] == VIDEO_ID
    assert meta["channel"] == "alpha"
    assert meta["video_url"] == URL
    assert meta["title"] == "A Talk"
    assert meta["published"] == "2026-08-12"
    assert ALL_FAILED in meta["last_error"]
    assert "chunked transcript" in meta["last_error"]
    # Mirrors the single-shot failure record: no invented status literal, mode not marked complete.
    assert "transcript_status" not in meta
    assert "transcript" not in meta.get("modes_completed", [])
    assert not (cdir / f"{PREFIX}.transcript.md").exists()
    return meta


def _run_process_url(args, config, monkeypatch):
    monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(vi, "process_mindmap", lambda *a, **kw: ("p", "done"))
    monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: ("p", "done"))
    with pytest.raises(SystemExit) as exc:
        vi.cmd_process(args, config)
    return exc.value.code


class TestTranscriptUrlAllChunksFailed:
    """ACCEPTANCE (#256 "Done when"), through the real `cmd_transcript --url` [core: real-caller, identity #66]."""

    def test_cmd_transcript_url_every_chunk_times_out_records_identity_and_last_error(self, wired):
        vi.cmd_transcript(_args(), CONFIG_GEMINI)

        assert len(wired.gemini_calls) == 2
        _assert_failure_recorded(wired.cdir)
        assert list(wired.cdir.glob(f"{PREFIX}.transcript.raw.chunk*.txt"))


class TestScanAllChunksFailed:
    def test_scan_transcribe_one_every_chunk_times_out_records_identity_and_last_error(self, wired):
        wired.cdir.mkdir()

        prefix, status = vi._scan_transcribe_one(
            client=object(),
            types=_TYPES,
            video=dict(_VIDEO),
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=wired.cdir,
            prefix=PREFIX,
            transcript_source="gemini",
            transcript_timeout_seconds=30,
            livestream_captions_first=False,
            duration_seconds=3600,
            chunk_minutes=30,
        )

        assert prefix == PREFIX
        assert status.startswith("error") and ALL_FAILED in status
        _assert_failure_recorded(wired.cdir)


class TestProcessUrlAllChunksFailed:
    def test_cmd_process_url_every_chunk_times_out_records_identity_and_last_error(self, wired, monkeypatch):
        code = _run_process_url(_args(), CONFIG_GEMINI, monkeypatch)

        assert code == vi.EXIT_PARTIAL
        _assert_failure_recorded(wired.cdir)


def _functions_calling(name: str, *, with_keyword: str | None = None) -> set[str]:
    tree = ast.parse(inspect.getsource(vi))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and getattr(call.func, "id", None) == name):
                continue
            if with_keyword is None or any(kw.arg == with_keyword for kw in call.keywords):
                found.add(node.name)
    return found


class TestTheRecordingLivesInTheSharedTail:
    """[core: one-definition] the chunked failure is recorded once, in the tail all three callers share."""

    def test_only_the_shared_tail_records_with_identity(self):
        assert _functions_calling("_record_transcript_error", with_keyword="video") == {"_finish_chunked_transcript"}

    def test_walk_sees_the_existing_single_shot_call_sites(self):
        # Companion: the walk finds callers at all, so the equality above cannot pass vacuously.
        assert {"process_transcript", "_finish_chunked_transcript"} <= _functions_calling("_record_transcript_error")


class TestAutoWithCaptions:
    def test_captions_replace_the_video_and_no_stale_last_error_is_left(self, wired, monkeypatch):
        wired.cdir.mkdir()
        stale = {"video_id": VIDEO_ID, "channel": "alpha", "last_error": "stale earlier failure"}
        (wired.cdir / f"{PREFIX}.meta.json").write_text(json.dumps(stale), encoding="utf-8")
        captions = _with_track()
        monkeypatch.setattr(vi, "fetch_english_captions", captions)

        vi.cmd_transcript(_args(), CONFIG_AUTO)

        meta = _meta(wired.cdir)
        assert len(captions.calls) == 1
        assert meta["transcript_source"] == "youtube_captions"
        assert ALL_FAILED in meta["transcript_failover_reason"]
        assert meta["last_error"] is None
        assert "transcript" in meta["modes_completed"]
        assert meta["video_id"] == VIDEO_ID


class TestAutoWithoutCaptions:
    def test_no_caption_track_records_identity_and_last_error(self, wired, monkeypatch):
        captions = _Captions(None)
        monkeypatch.setattr(vi, "fetch_english_captions", captions)

        vi.cmd_transcript(_args(), CONFIG_AUTO)

        assert len(captions.calls) == 1
        _assert_failure_recorded(wired.cdir)


class TestExistingMetaSurvives:
    def test_forced_rerun_all_chunks_failed_keeps_every_existing_field_and_sets_last_error(self, wired):
        wired.cdir.mkdir()
        existing = {
            "video_id": VIDEO_ID,
            "channel": "alpha",
            "video_url": URL,
            "title": "Original Title",
            "published": "2026-08-12",
            "description": "earlier description",
            "alt_titles": ["Another Title"],
            "skip_modes": ["mindmap"],
            "skip_reason": "operator choice",
            "transcript_status": "complete",
            "transcript_source": "gemini",
            "modes_completed": ["transcript"],
            "processed": "2026-08-13T00:00:00+00:00",
        }
        (wired.cdir / f"{PREFIX}.meta.json").write_text(json.dumps(existing), encoding="utf-8")
        (wired.cdir / f"{PREFIX}.transcript.md").write_text("PRIOR GOOD TRANSCRIPT", encoding="utf-8")

        vi.cmd_transcript(_args(force=True), CONFIG_GEMINI)

        meta = _meta(wired.cdir)
        assert len(wired.gemini_calls) == 2
        for key, value in existing.items():
            assert meta[key] == value, key
        assert ALL_FAILED in meta["last_error"]
        assert (wired.cdir / f"{PREFIX}.transcript.md").read_text(encoding="utf-8") == "PRIOR GOOD TRANSCRIPT"


class TestCorruptMeta:
    def test_torn_meta_is_quarantined_and_the_failure_is_recorded_with_identity(self, wired):
        wired.cdir.mkdir()
        (wired.cdir / f"{PREFIX}.meta.json").write_bytes(b'{"video_id": "abcdefg')

        vi.cmd_transcript(_args(), CONFIG_GEMINI)

        _assert_failure_recorded(wired.cdir)
        sidecars = list(wired.cdir.glob(f"{PREFIX}*{vi.CORRUPT_META_SUFFIX}"))
        assert len(sidecars) == 1
        assert sidecars[0].read_bytes() == b'{"video_id": "abcdefg'


class TestMetaWriteFailureDoesNotRaise:
    def test_oserror_on_the_meta_write_logs_a_warning_and_the_run_ends_as_before(self, wired, monkeypatch, caplog):
        real_write_text = vi.Path.write_text

        def failing_meta_write(self, *args, **kwargs):
            if self.name.endswith(".meta.json"):
                raise OSError("stub cloud-mount hiccup")
            return real_write_text(self, *args, **kwargs)

        monkeypatch.setattr(vi.Path, "write_text", failing_meta_write)
        caplog.set_level("INFO")

        result = vi.cmd_transcript(_args(), CONFIG_GEMINI)

        assert result is None
        assert not (wired.cdir / f"{PREFIX}.meta.json").exists()
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            f"{PREFIX}.meta.json" in m and "stub cloud-mount hiccup" in m and ALL_FAILED in m for m in warnings
        ), warnings
        assert any(f"{PREFIX}: error: all chunks failed" in r.getMessage() for r in caplog.records)
        assert list(wired.cdir.glob(f"{PREFIX}.transcript.raw.chunk*.txt"))


def _usage(prompt):
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=5000,
            total_token_count=prompt + 5000,
        )
    )


class TestAllChunksConfabulated:
    def test_every_chunk_prompt_zero_is_recorded_too(self, wired, monkeypatch):
        def every_chunk_confabulates(client, types, media_uri, prompt, model, **kw):
            kw["on_response"](_usage(0))
            return json.dumps({"transcripts": [], "screen_content": [], "speakers": []})

        monkeypatch.setattr(vi, "call_gemini", every_chunk_confabulates)

        vi.cmd_transcript(_args(), CONFIG_GEMINI)

        meta = _meta(wired.cdir)
        assert meta["video_id"] == VIDEO_ID
        assert meta["channel"] == "alpha"
        assert "all chunks confabulated" in meta["last_error"]
        assert "transcript_status" not in meta


class _NetDown(ConnectionError):
    pass


def _captions_raise(video_id, *, reason_sink=None):
    raise _NetDown("network unreachable")


class TestCaptionsCallRaisesUnderAuto:
    """The Gemini failure is recorded before the captions exception propagates, unchanged."""

    def test_transcript_url_records_then_propagates(self, wired, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", _captions_raise)

        with pytest.raises(_NetDown):
            vi.cmd_transcript(_args(), CONFIG_AUTO)

        _assert_failure_recorded(wired.cdir)

    def test_process_url_records_and_exits_as_before(self, wired, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", _captions_raise)

        code = _run_process_url(_args(), CONFIG_AUTO, monkeypatch)

        assert code == vi.EXIT_PARTIAL
        _assert_failure_recorded(wired.cdir)


class TestReadFailureOnExistingMetaSkipsTheWrite:
    def test_read_oserror_leaves_the_meta_bytes_untouched_and_warns(self, tmp_path, monkeypatch, caplog):
        cdir = tmp_path / "alpha"
        cdir.mkdir()
        meta_path = cdir / "p.meta.json"
        healthy = {
            "video_id": VIDEO_ID,
            "channel": "alpha",
            "title": "Original",
            "alt_titles": ["Other"],
            "skip_modes": ["mindmap"],
            "skip_reason": "operator choice",
            "modes_completed": ["transcript"],
        }
        meta_path.write_text(json.dumps(healthy), encoding="utf-8")
        before = meta_path.read_bytes()
        real_read_bytes = vi.Path.read_bytes

        def flaky_meta_read(self):
            if self.name.endswith(".meta.json"):
                raise OSError("stub transient read failure")
            return real_read_bytes(self)

        monkeypatch.setattr(vi.Path, "read_bytes", flaky_meta_read)
        caplog.set_level("WARNING")

        vi._record_transcript_error(meta_path, "chunked transcript failed: boom", video=dict(_VIDEO), channel_dir=cdir)

        monkeypatch.setattr(vi.Path, "read_bytes", real_read_bytes)
        assert meta_path.read_bytes() == before
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("p.meta.json" in m and "stub transient read failure" in m and "boom" in m for m in warnings), (
            warnings
        )


# ---------------------------------------------------------------------------
# Title rotation [core: writer's-path]: the caller hands the tail the NEW title's
# prefix while an older meta still claims the video_id. The checker below derives
# the expected meta from the OLD title on its own, never from the prefix a caller
# hands in, and counts every meta in the folder that claims the id.
# ---------------------------------------------------------------------------

OLD_TITLE = "Zeta Talk"
NEW_TITLE = "Alpha Talk"


def _titled(title):
    return {**_VIDEO, "title": title, "duration_iso": "PT60M"}


OLD_PREFIX = vi.video_file_prefix(_titled(OLD_TITLE))
OLD_SEED = {
    "video_id": VIDEO_ID,
    "video_url": URL,
    "channel": "alpha",
    "title": OLD_TITLE,
    "published": "2026-08-12",
    "processed": "2026-08-13T00:00:00+00:00",
    "modes_completed": ["scan"],
    "topics": ["sales"],
}


def _seed_old(cdir):
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{OLD_PREFIX}.meta.json").write_text(json.dumps(OLD_SEED), encoding="utf-8")
    (cdir / f"{OLD_PREFIX}.mindmap.md").write_text("# mm\n", encoding="utf-8")


def _metas_claiming_the_id(cdir):
    out = []
    for p in sorted(cdir.glob("*.meta.json")):
        if json.loads(p.read_text(encoding="utf-8")).get("video_id") == VIDEO_ID:
            out.append(p.name)
    return out


def _assert_one_meta_on_the_old_prefix(cdir):
    assert _metas_claiming_the_id(cdir) == [f"{OLD_PREFIX}.meta.json"]
    meta = json.loads((cdir / f"{OLD_PREFIX}.meta.json").read_text(encoding="utf-8"))
    for key, value in OLD_SEED.items():
        assert meta[key] == value, key
    assert ALL_FAILED in meta["last_error"]
    return meta


@pytest.fixture
def scan_wired(wired, monkeypatch):
    """The real `cmd_scan`; YouTube discovery, Gemini, captions and the mindmap writer are stubbed."""
    from unittest.mock import MagicMock

    feed: list[dict] = []
    mindmap_calls: list[str] = []

    def fake_mindmap(*args, **kwargs):
        video = args[2]
        prefix = kwargs.get("prefix") or vi.video_file_prefix(video)
        mindmap_calls.append(prefix)
        (wired.cdir / f"{prefix}.mindmap.md").write_text("# mm\n", encoding="utf-8")
        vi.update_meta(wired.cdir / f"{prefix}.meta.json", vi._transcript_identity_fields(video, wired.cdir), "scan")
        return prefix, "done"

    monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), _TYPES))
    monkeypatch.setattr(vi, "require_youtube", lambda: MagicMock())
    monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(vi, "get_channel_id", lambda _yt, url: (f"UC-{url}", url))
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda *_a, **_kw: [dict(v) for v in feed])
    monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids, "PT60M"))
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda *_a, **_kw: {})
    monkeypatch.setattr(vi, "is_short", lambda *_a, **_kw: False)
    monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(vi, "render_headline_digest", lambda *_a, **_kw: None)
    monkeypatch.setattr(vi, "rebuild_free_derived_layers", lambda *_a, **_kw: None)
    monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    wired.feed = feed
    wired.mindmap_calls = mindmap_calls
    return wired


def _scan_config(source, **channel_extra):
    channel = {"name": "alpha", "url": "alpha", "auto_transcript": "all", "transcript_source": source}
    return {"channels": [{**channel, **channel_extra}]}


def _scan(source="gemini", **channel_extra):
    args = SimpleNamespace(channel=None, since=None, force=False, dry_run=False, model=None)
    vi.cmd_scan(args, _scan_config(source, **channel_extra))


# `last_error` is one field shared across stages: a later successful mindmap
# write clears it. These two tests turn the mindmap stage off so the assertion
# sees what the transcript stage recorded.
_TRANSCRIPT_STAGE_ONLY = {"mindmap_source": "none"}


class TestRealScanAllChunksFailed:
    def test_cmd_scan_new_video_every_chunk_fails_records_identity_and_last_error(self, scan_wired):
        scan_wired.feed.append(_titled("A Talk"))

        _scan(**_TRANSCRIPT_STAGE_ONLY)

        assert len(scan_wired.gemini_calls) == 2
        _assert_failure_recorded(scan_wired.cdir)

    def test_cmd_scan_captions_raise_under_auto_records_and_the_scan_continues(self, scan_wired, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", _captions_raise)
        scan_wired.feed.append(_titled("A Talk"))

        _scan("auto", **_TRANSCRIPT_STAGE_ONLY)

        _assert_failure_recorded(scan_wired.cdir)


class TestTitleRotationKeepsOneMetaPerVideo:
    def test_precondition_the_new_title_sorts_first(self):
        # Without this ordering the first-wins index would hide a second meta.
        assert vi.video_file_prefix(_titled(NEW_TITLE)) < OLD_PREFIX

    def test_two_real_scans_record_on_the_existing_meta_and_never_remake_the_mindmap(self, scan_wired):
        _seed_old(scan_wired.cdir)
        scan_wired.feed.append(_titled(NEW_TITLE))

        _scan()
        _assert_one_meta_on_the_old_prefix(scan_wired.cdir)
        _scan()

        meta = _assert_one_meta_on_the_old_prefix(scan_wired.cdir)
        assert len(scan_wired.gemini_calls) == 4
        assert scan_wired.mindmap_calls == []
        assert meta["alt_titles"] == [NEW_TITLE]
        vi._invalidate_video_id_cache()
        assert vi._load_video_id_index(scan_wired.cdir) == {VIDEO_ID: OLD_PREFIX}

    def test_real_transcript_url_records_on_the_existing_meta(self, wired):
        _seed_old(wired.cdir)

        vi.cmd_transcript(_args(title=NEW_TITLE), CONFIG_GEMINI)

        assert len(wired.gemini_calls) == 2
        _assert_one_meta_on_the_old_prefix(wired.cdir)


class TestRecordTranscriptErrorContract:
    def test_without_video_and_no_meta_writes_nothing(self, tmp_path):
        meta_path = tmp_path / "x.meta.json"

        vi._record_transcript_error(meta_path, "boom")

        assert not meta_path.exists()

    def test_without_video_on_existing_meta_sets_only_last_error(self, tmp_path):
        meta_path = tmp_path / "x.meta.json"
        meta_path.write_text(json.dumps({"title": "T"}), encoding="utf-8")

        vi._record_transcript_error(meta_path, "boom")

        assert json.loads(meta_path.read_text(encoding="utf-8")) == {"title": "T", "last_error": "boom"}

    def test_with_video_and_no_meta_creates_it_with_identity(self, tmp_path):
        cdir = tmp_path / "alpha"
        meta_path = cdir / "x.meta.json"

        vi._record_transcript_error(meta_path, "boom", video=dict(_VIDEO), channel_dir=cdir)

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta == {
            "video_url": URL,
            "video_id": VIDEO_ID,
            "channel": "alpha",
            "title": "A Talk",
            "published": "2026-08-12",
            "last_error": "boom",
        }


class TestRequeueUnchanged:
    """The recorded meta must not make the video look processed: the next scan retries it, as before."""

    def test_recorded_failure_is_still_not_processed_for_any_mode(self, wired):
        vi.cmd_transcript(_args(), CONFIG_GEMINI)
        _assert_failure_recorded(wired.cdir)
        vi._invalidate_video_id_cache()

        # The primary (video_id index) path is the one exercised, not the slug fallback.
        assert vi._load_video_id_index(wired.cdir) == {VIDEO_ID: PREFIX}
        for mode in sorted(vi._MODE_ARTIFACT_MODES):
            assert vi.is_processed(wired.root, "alpha", dict(_VIDEO), mode) is False, mode
            assert vi.is_processed(wired.root, "alpha", dict(_VIDEO), mode, any_variant=True) is False, mode
        assert vi.is_skipped(wired.root, "alpha", dict(_VIDEO), mode="transcript") is False

    def test_companion_the_probe_does_see_a_transcript(self, wired):
        vi.cmd_transcript(_args(), CONFIG_GEMINI)
        vi._invalidate_video_id_cache()
        (wired.cdir / f"{PREFIX}.transcript.md").write_text("real transcript", encoding="utf-8")

        assert vi.is_processed(wired.root, "alpha", dict(_VIDEO), "transcript") is True
