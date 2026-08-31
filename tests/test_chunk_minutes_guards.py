"""Issue #168: `resolve_chunk_minutes` was guarded only in `cmd_scan`'s
per-channel loop. Three single-video call sites had NO exception handling at
all - not merely a narrow `except ValueError` - so a plain string
`chunk_minutes` typo already raw-tracebacked out of `transcript --url`,
`process --url`, and `process --file` today.

Sharpest illustration (from the issue): in `_cmd_process_url` the #135 guard
exits cleanly on a `transcript_source` typo, and a few dozen lines later the
SAME `channel_cfg` used to hit the unguarded `resolve_chunk_minutes` and
raw-traceback on a `chunk_minutes` typo.

Also folds in a silent-coercion hole verified by execution: PyYAML types an
unquoted `yes`/`true` as Python `True`, `bool` subclasses `int`, and
`int(True) == 1` - so `chunk_minutes: yes` used to resolve to a 1-minute
chunk size (roughly 60 Gemini calls on an hour-long video instead of 2),
raising nothing and logging nothing.

Idioms (fixtures, args builders, scan-stubbing pattern) are lifted from
`tests/test_manual_url_transcript_source.py`, which covers the sibling
`resolve_transcript_source` guard (issue #135/#127) call site by call site.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

import video_intel as vi
from video_intel import TRANSCRIPT_CHUNK_MINUTES_DEFAULT, resolve_chunk_minutes, validate_channel_knobs


def _assert_exits_1_no_traceback(fn, *args, **kwargs):
    """Run `fn`, asserting it exits 1 via `sys.exit` rather than letting a
    ValueError/TypeError escape uncaught.

    A bare `pytest.raises(SystemExit)` would already fail the test if a
    ValueError escaped instead (it does not match SystemExit), but this
    makes the failure mode explicit and legible: with the guard removed, this
    helper reports exactly what escaped instead of an opaque pytest traceback
    mismatch.
    """
    try:
        fn(*args, **kwargs)
    except SystemExit as e:
        assert e.code == 1, f"expected exit code 1, got {e.code!r}"
        return
    except Exception as e:  # pragma: no cover - only hit when the guard regresses
        pytest.fail(f"expected a clean SystemExit(1), but {type(e).__name__} escaped uncaught: {e}")
    pytest.fail("expected SystemExit, but the call returned normally")


# ---------------------------------------------------------------------------
# Resolver-level: the boolean-rejection fix, and the existing lenient/
# precedence behavior locked in place unchanged.
# ---------------------------------------------------------------------------


class TestResolveChunkMinutesBooleanRejection:
    def test_true_raises_and_names_the_yaml_trap(self):
        """Pre-fix this silently resolved to 1 (int(True) == 1) - a ~60x
        chunk-count multiplier on an hour-long video with no error, no log."""
        with pytest.raises(ValueError) as exc_info:
            resolve_chunk_minutes({"chunk_minutes": True}, {})

        message = str(exc_info.value)
        assert "True" in message
        assert "yaml" in message.lower() or "boolean" in message.lower()

    def test_false_still_raises(self):
        """Unchanged qualitatively: False must still raise. The exact message
        now goes through the same boolean branch as True (isinstance(True,
        int) is also True, so the bool check must run before int()), which is
        fine - this only pins that it keeps raising, not the literal wording."""
        with pytest.raises(ValueError):
            resolve_chunk_minutes({"chunk_minutes": False}, {})

    def test_bool_checked_before_int_coercion(self):
        """isinstance(True, int) is True - if the bool check ran AFTER the
        int() coercion (or was omitted), True would silently become 1 and no
        ValueError would raise at all. This is the regression this issue
        exists to close."""
        with pytest.raises(ValueError):
            resolve_chunk_minutes({}, {"chunk_minutes": True})

    @pytest.mark.parametrize("source", ["cli_override", "channel", "top_level"])
    def test_boolean_rejected_at_every_precedence_slot(self, source):
        kwargs = {"channel_config": {}, "config": {}}
        if source == "cli_override":
            kwargs["cli_override"] = True
        elif source == "channel":
            kwargs["channel_config"] = {"chunk_minutes": True}
        else:
            kwargs["config"] = {"chunk_minutes": True}

        with pytest.raises(ValueError):
            resolve_chunk_minutes(**kwargs)


class TestResolveChunkMinutesLeniencyUnchanged:
    """Out of scope for #168: do NOT tighten these - someone may rely on them."""

    @pytest.mark.parametrize(("candidate", "expected"), [(30, 30), ("30", 30), (30.7, 30)])
    def test_valid_shapes_still_resolve(self, candidate, expected):
        assert resolve_chunk_minutes({"chunk_minutes": candidate}, {}) == expected

    def test_invalid_string_still_raises_value_error(self):
        with pytest.raises(ValueError):
            resolve_chunk_minutes({"chunk_minutes": "thirty"}, {})

    def test_non_positive_still_raises_value_error(self):
        with pytest.raises(ValueError):
            resolve_chunk_minutes({"chunk_minutes": 0}, {})


class TestResolveChunkMinutesPrecedence:
    def test_cli_override_wins_over_everything(self):
        assert resolve_chunk_minutes({"chunk_minutes": 10}, {"chunk_minutes": 20}, cli_override=5) == 5

    def test_channel_wins_over_top_level(self):
        assert resolve_chunk_minutes({"chunk_minutes": 10}, {"chunk_minutes": 20}) == 10

    def test_top_level_wins_over_default(self):
        assert resolve_chunk_minutes({}, {"chunk_minutes": 20}) == 20

    def test_default_when_nothing_set(self):
        assert resolve_chunk_minutes({}, {}) == TRANSCRIPT_CHUNK_MINUTES_DEFAULT


# ---------------------------------------------------------------------------
# --dry-run preflight: the boolean rejection must surface through
# validate_channel_knobs (issue #169's preflight, already on this branch)
# without any change to that function - it already reuses the real resolver.
# ---------------------------------------------------------------------------


class TestPreflightSurfacesBooleanChunkMinutes:
    def test_boolean_chunk_minutes_is_flagged_by_the_preflight(self):
        problems = validate_channel_knobs({"chunk_minutes": True}, {})

        assert len(problems) == 1
        knob, message, _consequence = problems[0]
        assert knob == "chunk_minutes"
        assert "True" in message


# ---------------------------------------------------------------------------
# Caller-level: drive the REAL commands, network stubbed, per issue #168.
# Idioms lifted from tests/test_manual_url_transcript_source.py.
# ---------------------------------------------------------------------------


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
    """Captures whether/what process_transcript was reached with."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return ("2026-08-12-a-talk", "done")


@pytest.fixture
def wired_transcript(monkeypatch, tmp_path):
    """Neutralize everything cmd_transcript touches except the guards."""
    recorder = _RecordingTranscript()
    monkeypatch.setattr(vi, "process_transcript", recorder)
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _vid: False)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _vid: 600)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    return recorder


CONFIG_CHUNK_TYPO = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "chunk_minutes": "thirty"}]}
CONFIG_VALID_SOURCE_BAD_CHUNK = {
    "channels": [
        {
            "name": "alpha",
            "url": "https://youtube.com/@alpha",
            "transcript_source": "gemini",
            "chunk_minutes": "thirty",
        }
    ]
}


class TestTranscriptUrlChunkMinutesGuard:
    """The `_cmd_transcript_impl` call site (transcript --url)."""

    def test_typo_exits_1_cleanly_no_traceback(self, wired_transcript, caplog):
        with caplog.at_level("ERROR"):
            _assert_exits_1_no_traceback(vi.cmd_transcript, _transcript_args(), CONFIG_CHUNK_TYPO)

        assert not wired_transcript.calls, "process_transcript must never be reached on a bad chunk_minutes"
        messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert any("chunk_minutes" in m for m in messages), (
            f"expected an actionable chunk_minutes error, got: {messages}"
        )

    def test_valid_chunk_minutes_is_unaffected(self, wired_transcript):
        """The guard must not change behavior for a value that was always
        valid - proven by reaching process_transcript at all."""
        config = {"channels": [{"name": "alpha", "url": "u", "chunk_minutes": 15}]}

        vi.cmd_transcript(_transcript_args(), config)

        assert wired_transcript.calls, "process_transcript should have been reached with a valid chunk_minutes"


class TestProcessUrlChunkMinutesGuard:
    """The `_cmd_process_url` call site - the issue's sharpest illustration:
    a VALID transcript_source paired with a typo'd chunk_minutes on the SAME
    channel_cfg must still exit 1 cleanly, not traceback a few lines later."""

    @staticmethod
    def _args(**overrides):
        base = {
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
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

    @pytest.fixture
    def stubbed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "load_prompt", lambda name: f"prompt-for-{name}")
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
        return tmp_path

    def test_typo_exits_1_cleanly_no_traceback(self, stubbed, caplog):
        with caplog.at_level("ERROR"):
            _assert_exits_1_no_traceback(vi._cmd_process_url, self._args(), CONFIG_CHUNK_TYPO)

        messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert any("chunk_minutes" in m for m in messages), (
            f"expected an actionable chunk_minutes error, got: {messages}"
        )

    def test_valid_transcript_source_with_typo_chunk_minutes_still_exits_1(self, stubbed, caplog):
        """The issue's headline case: the #135 guard passes cleanly on a
        valid transcript_source, and a few dozen lines later the unguarded
        resolve_chunk_minutes used to raw-traceback on the SAME channel_cfg."""
        with caplog.at_level("ERROR"):
            _assert_exits_1_no_traceback(vi._cmd_process_url, self._args(), CONFIG_VALID_SOURCE_BAD_CHUNK)

    def test_valid_chunk_minutes_is_unaffected(self, stubbed, monkeypatch):
        """Reaching past the guard with a valid value must not regress -
        proven by observing the value the resolver actually returned. Raises
        from inside the spy itself (rather than a downstream stub) so the
        test stays scoped to exactly this guard: resolve_chunk_minutes runs
        inside its OWN try/except that only catches (ValueError, TypeError),
        so a RuntimeError raised here escapes immediately, before the
        (separate, broader) try block that wraps the rest of Step 1."""
        seen: dict = {}
        real_resolver = vi.resolve_chunk_minutes

        def spy(channel_cfg, config, cli_override=None):
            seen["value"] = real_resolver(channel_cfg, config, cli_override)
            raise RuntimeError("stop-after-guard")

        monkeypatch.setattr(vi, "resolve_chunk_minutes", spy)

        config = {"channels": [{"name": "alpha", "url": "u", "chunk_minutes": 15}]}
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            vi._cmd_process_url(self._args(), config)

        assert seen["value"] == 15


class TestProcessFileChunkMinutesGuard:
    """The `_cmd_process_impl` --file call site."""

    @staticmethod
    def _args(file_path, **overrides):
        base = {
            "file": str(file_path),
            "channel": "alpha",
            "video_id": None,
            "title": None,
            "date": None,
            "start": None,
            "end": None,
            "force": False,
            "prompt": None,
            "model": None,
            "media_resolution": "low",
            "chunk_minutes": None,
            "transcript_source": None,
            "topic": None,
        }
        base.update(overrides)
        return SimpleNamespace(**base)

    @pytest.fixture
    def stubbed(self, monkeypatch, tmp_path):
        mp4 = tmp_path / "talk.mp4"
        mp4.write_bytes(b"\x00" * 32)
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c: tmp_path)
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(vi, "load_prompt", lambda name: f"prompt-for-{name}")
        monkeypatch.setattr(vi, "upload_local_video", lambda _c, _p: "files/xyz")
        monkeypatch.setattr(
            vi,
            "resolve_local_file_identity",
            lambda *a, **kw: {
                "video_id": "vid",
                "url": "https://www.youtube.com/watch?v=vid",
                "title": "T",
                "published": "2026-08-12",
                "published_source": "flag",
                "channel": "alpha",
                "channel_dir": tmp_path / "alpha",
                "prefix": "2026-08-12-t",
                "meta_path": tmp_path / "alpha" / "2026-08-12-t.meta.json",
            },
        )
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
        return mp4

    def test_guard_runs_before_the_gemini_upload_not_after(self, stubbed, monkeypatch, caplog):
        """Probe before you pay.

        `upload_local_video` sits ABOVE the original position of this guard, so
        merely adding a try/except would have improved the error message while
        still charging the operator a full multi-minute MP4 upload for a
        one-character config typo. Verified against the real CLI: pre-fix,
        `process --file` with `chunk_minutes: thirty` logged
        "Uploading video: dummy.mp4" and created a Gemini file server-side
        before failing. Assert the ORDERING, not just the exit code - an exit-code
        -only assertion passes either way.
        """
        mp4 = stubbed
        uploads: list[object] = []

        def _record_upload(_client, path):
            uploads.append(path)
            return "files/xyz"

        monkeypatch.setattr(vi, "upload_local_video", _record_upload)
        config = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "chunk_minutes": "thirty"}]}

        with caplog.at_level("ERROR"):
            _assert_exits_1_no_traceback(vi.cmd_process, self._args(mp4), config)

        assert uploads == [], (
            "the chunk_minutes guard must reject the config typo BEFORE upload_local_video is called; "
            f"got {len(uploads)} upload(s)"
        )

    def test_typo_exits_1_cleanly_no_traceback(self, stubbed, caplog):
        mp4 = stubbed
        config = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha", "chunk_minutes": "thirty"}]}

        with caplog.at_level("ERROR"):
            _assert_exits_1_no_traceback(vi.cmd_process, self._args(mp4), config)

        messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert any("chunk_minutes" in m for m in messages), (
            f"expected an actionable chunk_minutes error, got: {messages}"
        )

    def test_valid_chunk_minutes_is_unaffected(self, stubbed, monkeypatch):
        """Raises from inside the spy itself, same reasoning as the
        `_cmd_process_url` sibling test: this guard's try/except only
        catches (ValueError, TypeError), so a RuntimeError raised here
        escapes immediately and proves nothing downstream is needed to
        observe the resolved value."""
        mp4 = stubbed
        seen: dict = {}
        real_resolver = vi.resolve_chunk_minutes

        def spy(channel_cfg, config, cli_override=None):
            seen["value"] = real_resolver(channel_cfg, config, cli_override)
            raise RuntimeError("stop-after-guard")

        monkeypatch.setattr(vi, "resolve_chunk_minutes", spy)

        config = {"channels": [{"name": "alpha", "url": "u", "chunk_minutes": 15}]}
        with pytest.raises(RuntimeError, match="stop-after-guard"):
            vi.cmd_process(self._args(mp4), config)

        assert seen["value"] == 15


# ---------------------------------------------------------------------------
# Scan-level: widened catch + errors.append so a chunk_minutes typo shows up
# in the end-of-scan failure summary instead of silently dropping a channel.
# ---------------------------------------------------------------------------


def _scan_args(**overrides):
    base = {"dry_run": False, "channel": None, "force": False, "since": None, "model": None}
    base.update(overrides)
    return SimpleNamespace(**base)


class TestScanChunkMinutesTypoSkipsOnlyThatChannel:
    """The per-channel loop guard - mirrors
    TestScanChannelConfigTypoSkipsOnlyThatChannel in
    test_manual_url_transcript_source.py, but for chunk_minutes.

    The typo channel is listed FIRST so "continues to the next channel" is
    actually exercised.
    """

    good_video: ClassVar[dict] = {
        "video_id": "good1",
        "title": "Good video",
        "published": "2026-04-15",
        "url": "https://www.youtube.com/watch?v=good1",
    }
    typo_video: ClassVar[dict] = {
        "video_id": "typo1",
        "title": "Typo channel video",
        "published": "2026-04-15",
        "url": "https://www.youtube.com/watch?v=typo1",
    }

    def _run_scan(self, monkeypatch, caplog, tmp_path, *, bad_chunk_minutes):
        videos_by_channel_url = {
            "https://example.com/typo": [self.typo_video],
            "https://example.com/good": [self.good_video],
        }

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: (url, url))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos_by_channel_url.get(cid, [])))
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

        transcripts_seen: list[str] = []
        mindmaps_seen: list[str] = []

        def fake_transcript(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            transcripts_seen.append(video["video_id"])
            return (video.get("video_id", "prefix"), "done")

        def fake_mindmap(*args, **kwargs):
            video = args[2] if len(args) > 2 else kwargs.get("video")
            mindmaps_seen.append(video["video_id"])
            return (video.get("video_id", "prefix"), "done")

        monkeypatch.setattr(vi, "process_transcript", fake_transcript)
        monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)

        config = {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "typo",
                    "url": "https://example.com/typo",
                    "auto_transcript": "all",
                    "chunk_minutes": bad_chunk_minutes,
                },
                {"name": "good", "url": "https://example.com/good", "auto_transcript": "all"},
            ],
        }

        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(), config)

        return transcripts_seen, mindmaps_seen

    def test_typo_channel_skipped_healthy_channel_still_processed(self, tmp_path, monkeypatch, caplog):
        transcripts_seen, mindmaps_seen = self._run_scan(monkeypatch, caplog, tmp_path, bad_chunk_minutes="thirty")

        assert "good1" in transcripts_seen, "the healthy channel's transcript must still run"
        assert "typo1" not in transcripts_seen, "the typo channel's transcript step must be skipped"
        assert "good1" in mindmaps_seen, "the healthy channel's mindmap must still run"
        assert "typo1" not in mindmaps_seen, "the typo channel's mindmap step must also be skipped"

        error_messages = [r.message for r in caplog.records if r.levelname == "ERROR" and "chunk_minutes" in r.message]
        # Issue #169's preflight (validate_channel_knobs) logs this once, in
        # addition to this existing skip site's own log line.
        assert len(error_messages) == 2, f"expected preflight + skip-site error lines, got: {error_messages}"
        assert all("typo" in m for m in error_messages), "every error must name the offending channel"
        assert any("entire channel" in m for m in error_messages), (
            "the skip-site message must say the WHOLE channel is skipped, not just transcripts"
        )

    def test_typo_channel_appears_in_the_end_of_scan_failure_summary(self, tmp_path, monkeypatch, caplog):
        """Item 2 from the #135 review, reapplied here: a `continue` with no
        `errors.append` leaves the scan reporting `Done.` and exiting 0 with
        a channel silently dropped. This site did not have `errors.append`
        before this fix."""
        self._run_scan(monkeypatch, caplog, tmp_path, bad_chunk_minutes="thirty")

        summary_lines = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("FAILED" in line for line in summary_lines), (
            f"expected a '--- N FAILED ---' summary line, got: {summary_lines}"
        )
        assert any("typo" in line and "chunk_minutes" in line.lower() for line in summary_lines), (
            f"expected the typo channel named in the failure summary, got: {summary_lines}"
        )

    def test_boolean_chunk_minutes_is_skipped_with_the_boolean_message(self, tmp_path, monkeypatch, caplog):
        """`chunk_minutes: yes` must not silently run 1-minute chunks - it
        must be skipped with the boolean/YAML-trap message, same as any
        other chunk_minutes typo."""
        transcripts_seen, _mindmaps_seen = self._run_scan(monkeypatch, caplog, tmp_path, bad_chunk_minutes=True)

        assert "typo1" not in transcripts_seen, "a boolean chunk_minutes must not silently resolve to 1-minute chunks"
        assert "good1" in transcripts_seen

        error_messages = [r.message for r in caplog.records if r.levelname == "ERROR" and "chunk_minutes" in r.message]
        assert any("True" in m for m in error_messages), f"expected the boolean value named, got: {error_messages}"
