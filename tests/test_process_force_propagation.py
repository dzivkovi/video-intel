"""Regression tests for issue #173: force propagation on process --url/--file
and scan's auto_concepts loop.

`_cmd_process_url`'s Step 3 call to `process_concepts` omitted `force=args.force`,
so a `process --url ... --force` re-run on an already-processed video kept
STALE concepts derived from the pre-force mindmap: `process_concepts`'s own
`concepts_path.exists() and not force` early return fired silently, at exit 0.
That guard is correct and shared with the lazy-fill `concepts --channel` path
and must not change - the fix is threading `force=args.force` through the
`--url` call site, matching the sibling `--file` path.

Review round 2 found the SAME defect still live on `scan`'s `auto_concepts`
loop - the path a bulk remediation (issue #172) is most likely to actually
use, since `scan --force` already regenerates every mindmap. That loop has
both halves wrong: a pre-filter (`concepts_path.exists()`, no force check)
and a `process_concepts` call with no `force=` at all. Fixed both, mirroring
`cmd_concepts`'s existing correct gate.

Review round 2 also found the Step 3 preamble (`mindmap_path.read_text()`,
`load_taxonomy()`) sitting OUTSIDE the try/except on both `--url` and
`--file`, so a preamble exception (a Cyrillic/BCS mindmap's
`UnicodeDecodeError`, a cloud-mount `OSError`, a corrupt `taxonomy.json`)
took the exact pre-fix uncaught path the try/except exists to close. Both
lines now live inside the try on both paths.

Investigation for the second asymmetry at the concepts call site (the
`--file` path wraps `process_concepts` in try/except + `_record_concepts_error`;
the `--url` path did not): `cmd_process` only wraps `_cmd_process_impl` in a
`finally: flush_topic_stamps()` (no `except`), and `main()`'s dispatch calls
`cmd_process(args, config)` inside a bare `try/.../finally` with no `except`
clause at all. So an exception raised inside the `--url` path's
`process_concepts` call propagated uncaught all the way out of `main()` -
no identity stamped, no `concepts_status` recorded (issue #66/#129 both
require a concepts failure to leave a durable trace). The fix adds the same
try/except + `_record_concepts_error` shape the `--file` path already uses.

Test layout:
  * TestForceParityBetweenPaths - a REAL `@pytest.mark.parametrize("step", STEPS)`
    over the three pipeline steps (so a fourth step added to STEPS inherits
    the check automatically); derives what each path (`--url`, `--file`)
    ACTUALLY passes as `force` for a given `args.force` and compares the two,
    rather than hardcoding a per-site expectation. Scoped to the
    all-artifacts-present fixture, where the comparison is meaningful (see
    the next two classes for why that scope matters).
  * TestConceptsForceIsUnconditionalAcrossPaths - concepts is the one step
    where BOTH paths pass `force=args.force` literally, with no OR-ing
    against a lazy-fill check. Asserted against both the all-artifacts and
    no-artifacts fixtures, since it holds unconditionally.
  * TestTranscriptMindmapForceDivergesWithoutArtifacts - documents the
    reviewers' catch: `--file` derives `transcript_force`/`mindmap_force` as
    `args.force or needs_X`, where `needs_X` is True whenever the artifact is
    missing regardless of `--force` (lazy-fill); `--url` passes raw
    `args.force`. On a video with no artifacts yet and `force=False`, the two
    paths genuinely (and correctly) disagree.
  * TestScanAutoConceptsForcePropagation - the scan-side half of #173: both
    the candidate pre-filter and the `process_concepts` call in `cmd_scan`'s
    `auto_concepts` loop now honor `args.force`, mirroring `cmd_concepts`.
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
  * TestConceptsPreambleExceptionIsCaught - a Step 3 PREAMBLE exception
    (mindmap read / taxonomy load, not the process_concepts call itself) is
    caught and recorded the same way, on both `--url` and `--file`.
"""

import argparse
import json
from unittest.mock import MagicMock

import pytest

import video_intel
from video_intel import cmd_process

# Pipeline steps in order; drives the real parametrize below so a fourth step
# added here is automatically covered by TestForceParityBetweenPaths.
STEPS = ("transcript", "mindmap", "concepts")

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
    the --url/--file force comparison meaningful rather than accidental. See
    TestTranscriptMindmapForceDivergesWithoutArtifacts for the fixture where
    that arithmetic does NOT collapse to plain args.force.
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


_URL_PREFIX = video_intel.video_file_prefix({"published": "2026-04-15", "title": "Test Video"})


def _run_url_and_capture(tmp_path, monkeypatch, force, *, with_artifacts):
    """Drive cmd_process --url, capturing the `force` kwarg each of the three
    process_* stubs receives. `with_artifacts` controls only the PRE-RUN
    state (whether transcript/mindmap/concepts already exist on disk before
    the call) - the stubs themselves always write a real artifact on success,
    matching what the production helpers do, so Step 2/3 always have
    something to read regardless of the starting state."""
    with monkeypatch.context() as m:
        _stub_url_env(m, tmp_path)
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True, exist_ok=True)
        prefix = _URL_PREFIX
        if with_artifacts:
            (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
            (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
            (channel_dir / f"{prefix}.concepts.json").write_text('{"concepts": []}', encoding="utf-8")

        captured: dict = {}

        def fake_transcript(*args, **kwargs):
            captured["transcript"] = kwargs.get("force")
            (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
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


def _run_file_and_capture(tmp_path, monkeypatch, force, *, with_artifacts):
    """Drive cmd_process --file, capturing the `force` kwarg each of the
    three process_* stubs receives. Same with_artifacts contract as
    _run_url_and_capture."""
    with monkeypatch.context() as m:
        _stub_file_env(m, tmp_path)
        if with_artifacts:
            mp4, channel_dir = _prep_fully_processed_file(tmp_path)
        else:
            mp4, channel_dir = _prep_mp4(tmp_path)
        m.setattr(video_intel, "upload_local_video", lambda *_a, **_kw: "files/stub")

        captured: dict = {}

        def fake_transcript(*args, **kwargs):
            captured["transcript"] = kwargs.get("force")
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
            (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
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
# 1. Both paths agree on force propagation, per step, derived independently.
#    Scoped to the all-artifacts-present fixture - see the next two classes
#    for the concepts-is-unconditional / transcript-mindmap-diverges split.
# ---------------------------------------------------------------------------


class TestForceParityBetweenPaths:
    """PR #136's checker-uses-the-writer's-contract lesson, applied to
    arguments instead of paths: never hardcode 'the --url site should pass
    force=X' - derive what each site ACTUALLY passes and compare the two.
    Real parametrize over STEPS, so a fourth pipeline step added there is
    covered automatically without a new test method."""

    @pytest.mark.parametrize("step", STEPS)
    @pytest.mark.parametrize("force", [True, False])
    def test_step_agrees_when_all_artifacts_present(self, tmp_path, monkeypatch, step, force):
        url_captured = _run_url_and_capture(tmp_path / "url", monkeypatch, force, with_artifacts=True)
        file_captured = _run_file_and_capture(tmp_path / "file", monkeypatch, force, with_artifacts=True)
        assert url_captured.get(step) == file_captured.get(step) == force


# ---------------------------------------------------------------------------
# 1b. Concepts is the unconditional invariant: force=args.force on BOTH
#     paths, no OR-ing against a lazy-fill check, so it holds regardless of
#     whether any artifact already exists.
# ---------------------------------------------------------------------------


class TestConceptsForceIsUnconditionalAcrossPaths:
    """Unlike transcript/mindmap on --file (which OR the requested force
    against a lazy-fill `needs_X` check - see the class below), both
    call sites pass `force=args.force` to process_concepts literally. This is
    the row callers should treat as load-bearing; the transcript/mindmap
    parity above only holds under the all-artifacts-present fixture."""

    @pytest.mark.parametrize("force", [True, False])
    def test_with_all_artifacts_present(self, tmp_path, monkeypatch, force):
        url_captured = _run_url_and_capture(tmp_path / "url", monkeypatch, force, with_artifacts=True)
        file_captured = _run_file_and_capture(tmp_path / "file", monkeypatch, force, with_artifacts=True)
        assert url_captured.get("concepts") == file_captured.get("concepts") == force

    @pytest.mark.parametrize("force", [True, False])
    def test_with_no_artifacts_present(self, tmp_path, monkeypatch, force):
        url_captured = _run_url_and_capture(tmp_path / "url", monkeypatch, force, with_artifacts=False)
        file_captured = _run_file_and_capture(tmp_path / "file", monkeypatch, force, with_artifacts=False)
        assert url_captured.get("concepts") == file_captured.get("concepts") == force


# ---------------------------------------------------------------------------
# 1c. The documented, deliberate difference: --file's lazy-fill forces
#     transcript/mindmap when nothing exists yet, --url does not.
# ---------------------------------------------------------------------------


class TestTranscriptMindmapForceDivergesWithoutArtifacts:
    """--file derives transcript_force/mindmap_force = args.force or needs_X,
    where needs_X is True whenever the artifact is missing regardless of
    --force (lazy-fill: a brand-new video must get its first pass even
    without --force). --url passes raw args.force with no OR-ing. On a video
    with NO artifacts yet and force=False, the two paths genuinely (and
    correctly) disagree - pinned here so nobody "fixes" this into false
    parity. With force=True the two coincidentally agree (both True
    independently), which is why these tests pin force=False specifically."""

    def test_transcript_force_diverges_when_no_artifacts_exist_and_force_is_false(self, tmp_path, monkeypatch):
        url_captured = _run_url_and_capture(tmp_path / "url", monkeypatch, False, with_artifacts=False)
        file_captured = _run_file_and_capture(tmp_path / "file", monkeypatch, False, with_artifacts=False)
        assert url_captured.get("transcript") is False, "raw args.force=False must reach --url's process_transcript"
        assert file_captured.get("transcript") is True, "lazy-fill forces the missing step regardless of --force"
        assert url_captured.get("transcript") != file_captured.get("transcript")

    def test_mindmap_force_diverges_when_no_artifacts_exist_and_force_is_false(self, tmp_path, monkeypatch):
        url_captured = _run_url_and_capture(tmp_path / "url", monkeypatch, False, with_artifacts=False)
        file_captured = _run_file_and_capture(tmp_path / "file", monkeypatch, False, with_artifacts=False)
        assert url_captured.get("mindmap") is False, "raw args.force=False must reach --url's process_mindmap"
        assert file_captured.get("mindmap") is True, "lazy-fill forces the missing step regardless of --force"
        assert url_captured.get("mindmap") != file_captured.get("mindmap")


# ---------------------------------------------------------------------------
# 2. scan's auto_concepts loop: the same two-half defect, fixed the same way.
# ---------------------------------------------------------------------------


class TestScanAutoConceptsForcePropagation:
    """Issue #173 (scan half): cmd_scan's auto_concepts loop had the same two
    halves wrong that cmd_concepts already gates correctly (~:7774/:7824 for
    the pre-fix line numbers) - a candidate pre-filter
    (`concepts_path.exists() and not args.force`) and the process_concepts
    call itself (`force=args.force`). Pre-fix, `scan --force` already
    regenerated every mindmap but silently left every existing concepts.json
    untouched: the pre-filter excluded every candidate with a concepts.json
    regardless of --force, and even a candidate that slipped through never
    received force=args.force on the call. This is the path a bulk
    remediation (issue #172) is most likely to actually use, since it is a
    single `scan --force` rather than N individual `process --url --force`
    invocations."""

    @staticmethod
    def _scan_args(*, force, channel=None):
        return argparse.Namespace(channel=channel, since=None, force=force, dry_run=False, model=None)

    @staticmethod
    def _prep_scan_channel(tmp_path, *, channel="everyinc", prefix="2026-04-10-canonical-talk"):
        channel_dir = tmp_path / channel
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
        (channel_dir / f"{prefix}.concepts.json").write_text(
            json.dumps({"concepts": [{"id": "old-scan-concept"}]}), encoding="utf-8"
        )
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "scanVIDEOID1",
                    "title": "Canonical Talk",
                    "published": "2026-04-10",
                    "channel": channel,
                }
            ),
            encoding="utf-8",
        )
        return channel_dir, prefix

    @staticmethod
    def _stub_scan_env(monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(video_intel, "require_youtube", lambda: MagicMock())
        monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr(video_intel, "get_channel_id", lambda *a, **kw: ("UCfake", "Every"))
        monkeypatch.setattr(video_intel, "fetch_channel_videos", lambda *a, **kw: [])  # no new YouTube videos
        monkeypatch.setattr(video_intel, "load_taxonomy", lambda _od: {"version": 1, "concepts": {}})

    def _run_scan_and_capture(self, tmp_path, monkeypatch, *, force):
        self._stub_scan_env(monkeypatch, tmp_path)
        _channel_dir, prefix = self._prep_scan_channel(tmp_path)

        captured: dict = {"called": False, "force": None}

        def fake_process_concepts(*args, **kwargs):
            captured["called"] = True
            captured["force"] = kwargs.get("force")
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_concepts", fake_process_concepts)

        config = {
            "channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc", "auto_concepts": True}],
            "output_dir": str(tmp_path),
            "auto_concepts": True,
        }
        video_intel.cmd_scan(self._scan_args(force=force, channel="everyinc"), config)
        return captured

    def test_prefilter_admits_existing_concepts_candidate_under_force(self, tmp_path, monkeypatch):
        """Pre-filter half. RED against pre-fix: the filter excluded every
        candidate with an existing concepts.json regardless of --force, so
        process_concepts was never even called."""
        captured = self._run_scan_and_capture(tmp_path, monkeypatch, force=True)
        assert captured["called"] is True

    def test_prefilter_still_excludes_existing_concepts_without_force(self, tmp_path, monkeypatch):
        """Not the always-on-force regression: without --force, an existing
        concepts.json is still skipped by the pre-filter."""
        captured = self._run_scan_and_capture(tmp_path, monkeypatch, force=False)
        assert captured["called"] is False

    def test_process_concepts_call_receives_force(self, tmp_path, monkeypatch):
        """Call-site half. RED against pre-fix: even when the pre-filter
        admits a candidate, the process_concepts call omitted force=
        entirely."""
        captured = self._run_scan_and_capture(tmp_path, monkeypatch, force=True)
        assert captured["force"] is True


# ---------------------------------------------------------------------------
# 3. The actual --url regression, end to end against the real process_concepts.
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
# 4. --file path: unchanged, still threads force=args.force to concepts.
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
# 5. A concepts exception on --url is recorded with identity, not propagated.
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


# ---------------------------------------------------------------------------
# 6. A Step 3 PREAMBLE exception (mindmap read / taxonomy load) is caught
#    too, on both --url and --file - not just the process_concepts() call.
# ---------------------------------------------------------------------------


class TestConceptsPreambleExceptionIsCaught:
    """Issue #173 review round 2: `mindmap_path.read_text()` and
    `load_taxonomy()` now live INSIDE the try, not staged above it. A
    preamble exception must hit the same _record_concepts_error + EXIT_PARTIAL
    path as a process_concepts() exception."""

    def test_url_mindmap_read_exception_is_recorded_not_propagated(self, tmp_path, monkeypatch):
        """A mindmap.md that cannot be decoded as UTF-8 (e.g. a torn write on
        a Cyrillic/BCS title) raises UnicodeDecodeError from read_text()."""
        _stub_url_env(monkeypatch, tmp_path)
        prefix = video_intel.video_file_prefix({"published": "2026-04-15", "title": "Test Video"})
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] hi\n", encoding="utf-8")
        (channel_dir / f"{prefix}.mindmap.md").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
        meta_path = channel_dir / f"{prefix}.meta.json"
        meta_path.write_text(json.dumps({"modes_completed": ["scan", "transcript", "mindmap"]}), encoding="utf-8")

        monkeypatch.setattr(video_intel, "process_transcript", lambda *a, **kw: (prefix, "skipped (exists)"))
        monkeypatch.setattr(video_intel, "process_mindmap", lambda *a, **kw: (prefix, "skipped (exists)"))

        with pytest.raises(SystemExit) as exc_info:
            video_intel.cmd_process(
                _url_args("https://www.youtube.com/watch?v=AAAAAAAAAAA", title="Test Video", force=True), _config()
            )
        assert exc_info.value.code == video_intel.EXIT_PARTIAL

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("video_id") == "AAAAAAAAAAA"
        assert meta.get("channel") == "demo"
        assert meta.get("concepts_status", "").startswith("error")
        assert "codec" in meta.get("last_error", "").lower() or "decode" in meta.get("last_error", "").lower()

    def test_file_taxonomy_load_exception_is_recorded_not_propagated(self, tmp_path, monkeypatch):
        """A corrupt taxonomy.json (or a cloud-mount read failure) raises
        from load_taxonomy(), the second preamble line on the --file path."""
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

        def raising_taxonomy(_dir):
            raise OSError("simulated cloud-mount read failure on taxonomy.json")

        monkeypatch.setattr(video_intel, "load_taxonomy", raising_taxonomy)

        with pytest.raises(SystemExit) as exc_info:
            cmd_process(_file_args(mp4, channel="everyinc", force=True), _config(channel_names=("everyinc",)))
        assert exc_info.value.code == video_intel.EXIT_PARTIAL

        meta_path = channel_dir / "video.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("concepts_status", "").startswith("error")
        assert "simulated cloud-mount read failure" in meta.get("last_error", "")
