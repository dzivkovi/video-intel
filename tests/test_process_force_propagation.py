"""Regression tests for issue #173: force propagation on process --url/--file.

`_cmd_process_url`'s Step 3 call to `process_concepts` omitted `force=args.force`,
so a `process --url ... --force` re-run on an already-processed video kept
STALE concepts derived from the pre-force mindmap: `process_concepts`'s own
`concepts_path.exists() and not force` early return fired silently, at exit 0.
That guard is correct and shared with the lazy-fill `concepts --channel` path
and must not change - the fix is threading `force=args.force` through the
`--url` call site, matching the sibling `--file` path.

Investigation for the second asymmetry at the same call site (the `--file`
path wraps `process_concepts` in try/except + `_record_concepts_error`; the
`--url` path did not): `cmd_process` only wraps `_cmd_process_impl` in a
`finally: flush_topic_stamps()` (no `except`), and `main()`'s dispatch calls
`cmd_process(args, config)` inside a bare `try/.../finally` with no `except`
clause at all. So an exception raised inside the `--url` path's
`process_concepts` call propagated uncaught all the way out of `main()` -
no identity stamped, no `concepts_status` recorded (issue #66/#129 both
require a concepts failure to leave a durable trace). The fix adds the same
try/except + `_record_concepts_error` shape the `--file` path already uses.

Test layout:
  * TestForceParityBetweenPaths - parametrized over the three pipeline steps;
    derives what each path (`--url`, `--file`) ACTUALLY passes as `force` for
    a given `args.force` and compares the two, rather than hardcoding a
    per-site expectation. A fourth step added later inherits the check
    automatically (PR #136's checker-uses-the-writer's-contract lesson,
    applied to arguments instead of paths).
  * TestUrlForcePropagatesToRealConceptsStep - end to end against the REAL
    `process_concepts` (only the `call_gemini_text` network seam is stubbed).
    RED against origin/main: `--force` still reports the stale marker
    concept. GREEN after the fix: the marker is gone and fresh content lands,
    with `extracted_from` / `source_prompt` provenance pointing at the
    regenerated mindmap.
  * TestFilePathUnchanged - the sibling `--file` call site keeps passing
    `force=args.force` to `process_concepts`, both with and without --force.
  * TestUrlConceptsExceptionIsRecordedWithIdentity - a concepts exception on
    the `--url` path no longer propagates uncaught; it is recorded via
    `_record_concepts_error` with full identity stamped, matching --file.
"""

import argparse
import json
from unittest.mock import MagicMock

import pytest

import video_intel
from video_intel import cmd_process

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _url_args(url, **overrides):
    base = {
        "url": url,
        "file": None,
        "channel": "demo",
        "video_id": None,
        "title": "Test Video",
        "date": "2026-04-15",
        "start": None,
        "end": None,
        "force": False,
        "model": None,
        "prompt": None,
        "chunk_minutes": 50,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _file_args(file, **overrides):
    base = {
        "file": file,
        "url": None,
        "channel": None,
        "video_id": None,
        "title": None,
        "date": None,
        "start": None,
        "end": None,
        "force": False,
        "model": None,
        "prompt": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _stub_url_env(monkeypatch, tmp_path, *, duration=1800):
    """Stub external dependencies for cmd_process --url tests. Mirrors
    test_mindmap_from_transcript.py's _stub_url_env."""
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "create_client", lambda *_a, **_kw: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: tmp_path)
    monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(video_intel, "load_taxonomy", lambda *_a, **_kw: {"concepts": {}})
    monkeypatch.setattr(video_intel, "_lookup_video_duration_seconds", lambda *_a, **_kw: duration)
    monkeypatch.setattr(video_intel, "_lookup_was_livestream", lambda *_a, **_kw: False)


def _stub_file_env(monkeypatch, tmp_path):
    """Stub external dependencies for cmd_process --file tests. Mirrors
    test_cmd_process.py's stub_env fixture."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda _args, _cfg: "stub-model")
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
    monkeypatch.setattr(video_intel, "load_prompt", lambda _name: f"prompt-for-{_name}")
    monkeypatch.setattr(video_intel, "load_taxonomy", lambda _dir: {"concepts": {}})


def _prep_mp4(tmp_path, channel="everyinc", name="video.mp4"):
    output_dir = tmp_path / "video-intel"
    channel_dir = output_dir / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    mp4 = channel_dir / name
    mp4.write_bytes(b"fake mp4 bytes")
    return mp4, channel_dir


def _prep_fully_processed_file(tmp_path, channel="everyinc", name="video.mp4", prefix="video"):
    """A local file with all three artifacts + meta already on disk, as if a
    prior `process --file` run completed cleanly. With every artifact
    present and every mode in modes_completed, needs_mindmap/needs_transcript
    on the --file path collapse to `args.force` alone (see the arithmetic in
    _cmd_process_impl: `needs_X = not skip_X and (args.force or "<mode>" not
    in modes_done or not X_path.exists())`), so this fixture is what makes
    the --url/--file force comparison meaningful rather than accidental.
    """
    mp4, channel_dir = _prep_mp4(tmp_path, channel=channel, name=name)
    (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
    (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
    (channel_dir / f"{prefix}.concepts.json").write_text(
        json.dumps({"concepts": [{"id": "old-concept"}]}), encoding="utf-8"
    )
    (channel_dir / f"{prefix}.meta.json").write_text(
        json.dumps(
            {
                "video_id": "",
                "video_url": "",
                "title": prefix,
                "published": "2026-04-23",
                "channel": channel,
                "modes_completed": ["scan", "transcript", "concepts"],
            }
        ),
        encoding="utf-8",
    )
    return mp4, channel_dir


def _write_stub_artifact_if_ok(path, status, content):
    if not str(status).startswith("error"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _config(channel_names=("demo",)):
    return {
        "output_dir": "unused",
        "channels": [{"name": n, "url": f"https://example.com/{n}"} for n in channel_names],
    }


# ---------------------------------------------------------------------------
# 1. Both paths agree on force propagation, per step, derived independently.
# ---------------------------------------------------------------------------


class TestForceParityBetweenPaths:
    """PR #136's checker-uses-the-writer's-contract lesson, applied to
    arguments instead of paths: never hardcode 'the --url site should pass
    force=X' - derive what each site ACTUALLY passes and compare the two."""

    @pytest.mark.parametrize("force", [True, False])
    def test_transcript_step_agrees(self, tmp_path, monkeypatch, force):
        url_force, file_force = self._capture_step_force(tmp_path, monkeypatch, "transcript", force)
        assert url_force == file_force == force

    @pytest.mark.parametrize("force", [True, False])
    def test_mindmap_step_agrees(self, tmp_path, monkeypatch, force):
        url_force, file_force = self._capture_step_force(tmp_path, monkeypatch, "mindmap", force)
        assert url_force == file_force == force

    @pytest.mark.parametrize("force", [True, False])
    def test_concepts_step_agrees(self, tmp_path, monkeypatch, force):
        url_force, file_force = self._capture_step_force(tmp_path, monkeypatch, "concepts", force)
        assert url_force == file_force == force

    @staticmethod
    def _capture_step_force(tmp_path, monkeypatch, step, force):
        url_dir = tmp_path / "url"
        file_dir = tmp_path / "file"
        url_dir.mkdir()
        file_dir.mkdir()
        url_force_seen = TestForceParityBetweenPaths._run_url_and_capture(url_dir, monkeypatch, force)
        file_force_seen = TestForceParityBetweenPaths._run_file_and_capture(file_dir, monkeypatch, force)
        return url_force_seen.get(step), file_force_seen.get(step)

    @staticmethod
    def _run_url_and_capture(tmp_path, monkeypatch, force):
        with monkeypatch.context() as m:
            _stub_url_env(m, tmp_path)
            channel_dir = tmp_path / "demo"
            channel_dir.mkdir(parents=True, exist_ok=True)
            prefix = video_intel.video_file_prefix({"published": "2026-04-15", "title": "Test Video"})
            (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
            (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
            (channel_dir / f"{prefix}.concepts.json").write_text('{"concepts": []}', encoding="utf-8")

            captured: dict = {}

            def fake_transcript(*args, **kwargs):
                captured["transcript"] = kwargs.get("force")
                return prefix, "done"

            def fake_mindmap(*args, **kwargs):
                captured["mindmap"] = kwargs.get("force")
                p = kwargs.get("prefix") or prefix
                (channel_dir / f"{p}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
                return p, "done"

            def fake_concepts(*args, **kwargs):
                captured["concepts"] = kwargs.get("force")
                p = kwargs.get("prefix") or prefix
                _write_stub_artifact_if_ok(channel_dir / f"{p}.concepts.json", "done", '{"concepts": []}')
                return p, "done"

            m.setattr(video_intel, "process_transcript", fake_transcript)
            m.setattr(video_intel, "process_mindmap", fake_mindmap)
            m.setattr(video_intel, "process_concepts", fake_concepts)

            cmd_process(
                _url_args("https://www.youtube.com/watch?v=AAAAAAAAAAA", title="Test Video", force=force),
                _config(),
            )
            return captured

    @staticmethod
    def _run_file_and_capture(tmp_path, monkeypatch, force):
        with monkeypatch.context() as m:
            _stub_file_env(m, tmp_path)
            mp4, channel_dir = _prep_fully_processed_file(tmp_path)
            m.setattr(video_intel, "upload_local_video", lambda *_a, **_kw: "files/stub")

            captured: dict = {}

            def fake_transcript(*args, **kwargs):
                captured["transcript"] = kwargs.get("force")
                prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
                return prefix, "done"

            def fake_mindmap(*args, **kwargs):
                captured["mindmap"] = kwargs.get("force")
                prefix = kwargs.get("prefix") or "video"
                (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
                return prefix, "done"

            def fake_concepts(*args, **kwargs):
                captured["concepts"] = kwargs.get("force")
                prefix = kwargs.get("prefix") or "video"
                _write_stub_artifact_if_ok(channel_dir / f"{prefix}.concepts.json", "done", '{"concepts": []}')
                return prefix, "done"

            m.setattr(video_intel, "process_transcript", fake_transcript)
            m.setattr(video_intel, "process_mindmap", fake_mindmap)
            m.setattr(video_intel, "process_concepts", fake_concepts)

            cmd_process(_file_args(mp4, channel="everyinc", force=force), _config(channel_names=("everyinc",)))
            return captured


# ---------------------------------------------------------------------------
# 2. The actual regression, end to end against the real process_concepts.
# ---------------------------------------------------------------------------


class TestUrlForcePropagatesToRealConceptsStep:
    """Only `call_gemini_text` (the network seam) is stubbed - process_concepts
    itself runs for real, so this exercises its own `concepts_path.exists()
    and not force` early return exactly as production code would.

    RED against origin/main: test_force_regenerates_stale_concepts fails
    because `--force` still reported "skipped (exists)" and the marker
    concept survived.
    """

    @staticmethod
    def _run(tmp_path, monkeypatch, *, force):
        _stub_url_env(monkeypatch, tmp_path)
        prefix = video_intel.video_file_prefix({"published": "2026-04-15", "title": "Test Video"})
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\nregenerated content\n", encoding="utf-8")
        stale_concepts = {
            "video_id": "AAAAAAAAAAA",
            "extracted_from": f"{prefix}.mindmap.md",
            "source_prompt": "mindmap-from-transcript",
            "concepts": [{"id": "MARKER-STALE-CONCEPT", "label": "Stale Marker", "status": "existing"}],
        }
        (channel_dir / f"{prefix}.concepts.json").write_text(json.dumps(stale_concepts), encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "AAAAAAAAAAA",
                    "video_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA",
                    "title": "Test Video",
                    "published": "2026-04-15",
                    "channel": "demo",
                    "modes_completed": ["scan", "transcript", "mindmap", "concepts"],
                }
            ),
            encoding="utf-8",
        )

        monkeypatch.setattr(video_intel, "process_transcript", lambda *a, **kw: (prefix, "skipped (exists)"))
        monkeypatch.setattr(video_intel, "process_mindmap", lambda *a, **kw: (prefix, "skipped (exists)"))

        fresh_raw = json.dumps(
            {"concepts": [{"id": "fresh-concept", "label": "Fresh Regenerated Concept", "status": "new"}]}
        )
        monkeypatch.setattr(video_intel, "call_gemini_text", lambda *a, **kw: fresh_raw)

        video_intel.cmd_process(
            _url_args("https://www.youtube.com/watch?v=AAAAAAAAAAA", title="Test Video", force=force), _config()
        )
        return json.loads((channel_dir / f"{prefix}.concepts.json").read_text(encoding="utf-8"))

    def test_force_regenerates_stale_concepts(self, tmp_path, monkeypatch):
        result = self._run(tmp_path, monkeypatch, force=True)
        ids = {c.get("id") for c in result["concepts"]}
        assert "MARKER-STALE-CONCEPT" not in ids, "stale marker concept must be gone after --force"
        assert "fresh-concept" in ids
        assert result["extracted_from"] == "2026-04-15-test-video.mindmap.md"
        assert result["source_prompt"] == "mindmap-from-transcript"

    def test_without_force_still_skips(self, tmp_path, monkeypatch):
        """Not the always-on-force regression: without --force, an already
        processed video's concepts stay untouched."""
        result = self._run(tmp_path, monkeypatch, force=False)
        ids = {c.get("id") for c in result["concepts"]}
        assert ids == {"MARKER-STALE-CONCEPT"}


# ---------------------------------------------------------------------------
# 3. --file path: unchanged, still threads force=args.force to concepts.
# ---------------------------------------------------------------------------


class TestFilePathUnchanged:
    @pytest.mark.parametrize("force", [True, False])
    def test_concepts_force_matches_args_force(self, tmp_path, monkeypatch, force):
        _stub_file_env(monkeypatch, tmp_path)
        mp4, channel_dir = _prep_fully_processed_file(tmp_path)
        monkeypatch.setattr(video_intel, "upload_local_video", lambda *_a, **_kw: "files/stub")
        monkeypatch.setattr(
            video_intel,
            "process_transcript",
            lambda *a, **kw: (a[6] if len(a) > 6 else kw.get("prefix") or "video", "done"),
        )
        monkeypatch.setattr(
            video_intel,
            "process_mindmap",
            lambda *a, **kw: (kw.get("prefix") or "video", "done"),
        )
        concepts_forces = []

        def fake_concepts(*args, **kwargs):
            concepts_forces.append(kwargs.get("force"))
            prefix = kwargs.get("prefix") or "video"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.concepts.json", "done", '{"concepts": []}')
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_concepts", fake_concepts)

        cmd_process(_file_args(mp4, channel="everyinc", force=force), _config(channel_names=("everyinc",)))

        assert concepts_forces == [force]


# ---------------------------------------------------------------------------
# 4. A concepts exception on --url is recorded with identity, not propagated.
# ---------------------------------------------------------------------------


class TestUrlConceptsExceptionIsRecordedWithIdentity:
    def test_concepts_exception_is_recorded_not_propagated(self, tmp_path, monkeypatch):
        _stub_url_env(monkeypatch, tmp_path)
        prefix = video_intel.video_file_prefix({"published": "2026-04-15", "title": "Test Video"})
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
        meta_path = channel_dir / f"{prefix}.meta.json"
        meta_path.write_text(json.dumps({"modes_completed": ["scan", "transcript", "mindmap"]}), encoding="utf-8")

        monkeypatch.setattr(video_intel, "process_transcript", lambda *a, **kw: (prefix, "skipped (exists)"))
        monkeypatch.setattr(video_intel, "process_mindmap", lambda *a, **kw: (prefix, "skipped (exists)"))

        def raising_concepts(*args, **kwargs):
            raise RuntimeError("simulated concepts backend failure")

        monkeypatch.setattr(video_intel, "process_concepts", raising_concepts)

        with pytest.raises(SystemExit) as exc_info:
            video_intel.cmd_process(
                _url_args("https://www.youtube.com/watch?v=AAAAAAAAAAA", title="Test Video", force=True), _config()
            )
        assert exc_info.value.code == video_intel.EXIT_PARTIAL

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("video_id") == "AAAAAAAAAAA"
        assert meta.get("channel") == "demo"
        assert meta.get("title") == "Test Video"
        assert meta.get("concepts_status", "").startswith("error")
        assert "simulated concepts backend failure" in meta.get("last_error", "")
