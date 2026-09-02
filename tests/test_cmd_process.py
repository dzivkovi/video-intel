"""Tests for cmd_process: one-upload orchestrator for local MP4 pipeline.

Covers Unit 2 and (folded in) Unit 4 scenarios from the plan:
- Upload-once invariant
- Lazy-upload gate on meta.json modes_completed + sidecar check
- Partial-success semantics (artifact persistence under step failure)
- Exit-code contract (0 if mindmap succeeded)
- --force regenerates everything
- Resume scenarios after partial failure
- Integration: file_uri threaded through to process_mindmap / process_transcript
"""

import argparse
import json
from unittest.mock import MagicMock

import pytest

from video_intel import EXIT_PARTIAL, cmd_process


def _write_stub_artifact_if_ok(path, status, content):
    """Write the placeholder artifact a process_* stub claims to have produced.

    Issue #129's exit-code check inspects the filesystem, not just the
    returned status string, so a stub that reports success must also leave a
    real (non-empty) file behind - mirroring what the actual process_transcript
    / process_mindmap / process_concepts helpers write on disk. An error
    status must NOT write anything; that is the failure case under test.
    """
    if not str(status).startswith("error"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _make_args(
    *,
    file=None,
    channel=None,
    video_id=None,
    title=None,
    date=None,
    start=None,
    end=None,
    force=False,
    model=None,
    prompt=None,
):
    """argparse.Namespace matching the process subparser."""
    return argparse.Namespace(
        file=file,
        channel=channel,
        video_id=video_id,
        title=title,
        date=date,
        start=start,
        end=end,
        force=force,
        model=model,
        prompt=prompt,
    )


def _config(channel_names=("everyinc",)):
    return {"channels": [{"name": n, "url": f"https://youtube.com/@{n}"} for n in channel_names]}


@pytest.fixture
def stub_env(monkeypatch, tmp_path):
    """Stub all external dependencies so tests exercise only cmd_process orchestration logic."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
    monkeypatch.setattr("video_intel.resolve_model", lambda _args, _cfg: "stub-model")
    monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
    monkeypatch.setattr("video_intel.load_prompt", lambda _name: f"prompt-for-{_name}")
    monkeypatch.setattr("video_intel.load_taxonomy", lambda _dir: {"concepts": {}})
    return tmp_path


def _prep_mp4(tmp_path, channel="everyinc", name="video.mp4"):
    """Drop an MP4 under output_dir/<channel>/ so infer_channel_from_file_path finds it."""
    output_dir = tmp_path / "video-intel"
    channel_dir = output_dir / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    mp4 = channel_dir / name
    mp4.write_bytes(b"fake mp4 bytes")
    return mp4, channel_dir


class TestCmdProcessHappyPath:
    def test_upload_called_exactly_once_when_all_artifacts_missing(self, stub_env, monkeypatch, tmp_path):
        mp4, channel_dir = _prep_mp4(tmp_path)

        upload_calls = []
        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, p: upload_calls.append(p) or "files/uploaded-once",
        )

        mindmap_calls: list[dict] = []

        def fake_mindmap(*args, **kwargs):
            mindmap_calls.append(kwargs)
            (channel_dir / f"{kwargs.get('prefix') or 'video'}.mindmap.md").write_text(
                "# mindmap content", encoding="utf-8"
            )
            return kwargs.get("prefix") or "video", "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)

        transcript_calls: list[dict] = []

        def fake_transcript(*args, **kwargs):
            transcript_calls.append(kwargs)
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
            status = "done"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.transcript.md", status, "# stub transcript\n")
            return prefix, status

        monkeypatch.setattr("video_intel.process_transcript", fake_transcript)

        concepts_calls: list[dict] = []

        def fake_concepts(*args, **kwargs):
            concepts_calls.append(kwargs)
            prefix = kwargs.get("prefix") or "video"
            status = "done"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.concepts.json", status, '{"concepts": []}')
            return prefix, status

        monkeypatch.setattr("video_intel.process_concepts", fake_concepts)

        # Pin mindmap_source=video: this test's own assertions are about the
        # legacy mindmap-from-video path threading the same file_uri as
        # transcript ("both video-bearing calls"). With a faithful transcript
        # stub now writing a real .transcript.md (issue #129), the default
        # mindmap_source=auto resolver would route mindmap through the
        # cheaper transcript-source path instead (issue #54) - which never
        # receives media_uri at all, since it's a text-only call. Forcing the
        # legacy video path here keeps this test exercising what it actually
        # asserts, without touching the assertions themselves.
        config = _config()
        config["channels"][0]["mindmap_source"] = "video"

        args = _make_args(file=mp4, channel="everyinc")
        cmd_process(args, config)

        assert len(upload_calls) == 1
        assert len(mindmap_calls) == 1
        assert len(transcript_calls) == 1
        assert len(concepts_calls) == 1
        # file_uri threaded through to both video-bearing calls
        assert mindmap_calls[0].get("media_uri") == "files/uploaded-once"
        assert transcript_calls[0].get("media_uri") == "files/uploaded-once"

    def test_concepts_skipped_when_channel_not_configured(self, stub_env, monkeypatch, tmp_path):
        """When resolve_local_file_identity cannot attach to a configured channel, concepts is skipped with a warning; exit stays 0."""
        output_dir = tmp_path / "video-intel"
        output_dir.mkdir()
        loose_dir = tmp_path / "downloads"
        loose_dir.mkdir()
        mp4 = loose_dir / "random.mp4"
        mp4.write_bytes(b"fake")

        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/loose")

        def fake_mindmap(*a, **kw):
            prefix = kw.get("prefix") or "random"
            status = "done"
            _write_stub_artifact_if_ok(loose_dir / f"{prefix}.mindmap.md", status, "# stub mindmap\n")
            return prefix, status

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)

        def fake_transcript(*a, **kw):
            prefix = a[6] if len(a) > 6 else kw.get("prefix") or "random"
            status = "done"
            _write_stub_artifact_if_ok(loose_dir / f"{prefix}.transcript.md", status, "# stub transcript\n")
            return prefix, status

        monkeypatch.setattr("video_intel.process_transcript", fake_transcript)
        concepts_called = []
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: concepts_called.append(kw) or ("random", "done"),
        )

        args = _make_args(file=mp4)  # no --channel, file not under output_dir
        cmd_process(args, _config())

        assert concepts_called == []


class TestCmdProcessLazyUpload:
    def test_upload_skipped_when_all_artifacts_exist_without_force(self, stub_env, monkeypatch, tmp_path):
        """Fast path: nothing to do, zero upload cost."""
        mp4, channel_dir = _prep_mp4(tmp_path)
        # Pre-populate all three artifacts as if a prior run completed them
        prefix = "video"
        (channel_dir / f"{prefix}.mindmap.md").write_text("m", encoding="utf-8")
        (channel_dir / f"{prefix}.transcript.md").write_text("t", encoding="utf-8")
        (channel_dir / f"{prefix}.concepts.json").write_text("{}", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "",
                    "video_url": "",
                    "title": "video",
                    "published": "2026-04-23",
                    "channel": "everyinc",
                    "modes_completed": ["scan", "transcript", "concepts"],
                }
            )
        )

        upload_calls = []
        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, p: upload_calls.append(p) or "files/should-not-be-called",
        )
        monkeypatch.setattr(
            "video_intel.process_mindmap",
            lambda *a, **kw: (kw.get("prefix") or "video", "skipped (exists)"),
        )
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (kw.get("prefix") or "video", "skipped (exists)"),
        )
        concepts_called = []
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: concepts_called.append(kw) or ("video", "skipped (exists)"),
        )

        args = _make_args(file=mp4, channel="everyinc")
        cmd_process(args, _config())

        assert upload_calls == []  # upload-once invariant under all-done state

    def test_upload_happens_when_transcript_missing_after_partial_failure(self, stub_env, monkeypatch, tmp_path):
        """Resume semantics: mindmap.md on disk with modes_completed=['scan'], transcript missing."""
        mp4, channel_dir = _prep_mp4(tmp_path)
        prefix = "video"
        (channel_dir / f"{prefix}.mindmap.md").write_text("m", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "",
                    "video_url": "",
                    "title": "video",
                    "published": "2026-04-23",
                    "channel": "everyinc",
                    "modes_completed": ["scan"],
                }
            )
        )

        upload_calls = []
        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, p: upload_calls.append(p) or "files/resume",
        )
        monkeypatch.setattr(
            "video_intel.process_mindmap",
            lambda *a, **kw: (kw.get("prefix") or "video", "skipped (exists)"),
        )

        def fake_transcript(*a, **kw):
            prefix = a[6] if len(a) > 6 else kw.get("prefix") or "video"
            status = "done"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.transcript.md", status, "# stub transcript\n")
            return prefix, status

        monkeypatch.setattr("video_intel.process_transcript", fake_transcript)

        def fake_concepts(*a, **kw):
            prefix = kw.get("prefix") or "video"
            status = "done"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.concepts.json", status, '{"concepts": []}')
            return prefix, status

        monkeypatch.setattr("video_intel.process_concepts", fake_concepts)

        args = _make_args(file=mp4, channel="everyinc")
        cmd_process(args, _config())

        assert len(upload_calls) == 1

    def test_upload_happens_when_sidecar_raw_txt_present(self, stub_env, monkeypatch, tmp_path):
        """A .transcript.raw.txt sidecar is a 'partial transcript' signal — treat transcript as incomplete.

        Asserts both that the orchestrator uploads AND that it threads force=True to
        process_transcript so the helper does not short-circuit on its own
        `transcript_path.exists()` check (regression: CORR-02 from 2026-04-23 review).
        """
        mp4, channel_dir = _prep_mp4(tmp_path)
        prefix = "video"
        (channel_dir / f"{prefix}.mindmap.md").write_text("m", encoding="utf-8")
        (channel_dir / f"{prefix}.transcript.md").write_text("t", encoding="utf-8")
        (channel_dir / f"{prefix}.transcript.raw.txt").write_text("salvage forensics", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "",
                    "video_url": "",
                    "title": "video",
                    "published": "2026-04-23",
                    "channel": "everyinc",
                    "modes_completed": ["scan", "transcript"],
                }
            )
        )

        upload_calls = []
        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, p: upload_calls.append(p) or "files/resync",
        )
        monkeypatch.setattr(
            "video_intel.process_mindmap",
            lambda *a, **kw: (kw.get("prefix") or "video", "skipped (exists)"),
        )
        transcript_force_values: list[bool] = []
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (
                transcript_force_values.append(kw.get("force"))
                or (a[6] if len(a) > 6 else kw.get("prefix") or "video", "done")
            ),
        )

        def fake_concepts(*a, **kw):
            prefix = kw.get("prefix") or "video"
            status = "done"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.concepts.json", status, '{"concepts": []}')
            return prefix, status

        monkeypatch.setattr("video_intel.process_concepts", fake_concepts)

        args = _make_args(file=mp4, channel="everyinc")
        cmd_process(args, _config())

        assert len(upload_calls) == 1  # sidecar forced regeneration path
        assert transcript_force_values == [True]  # CORR-02: force must be threaded

    def test_force_bypasses_lazy_upload_when_all_artifacts_exist(self, stub_env, monkeypatch, tmp_path):
        mp4, channel_dir = _prep_mp4(tmp_path)
        prefix = "video"
        (channel_dir / f"{prefix}.mindmap.md").write_text("m", encoding="utf-8")
        (channel_dir / f"{prefix}.transcript.md").write_text("t", encoding="utf-8")
        (channel_dir / f"{prefix}.concepts.json").write_text("{}", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps({"modes_completed": ["scan", "transcript", "concepts"]})
        )

        upload_calls = []
        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, p: upload_calls.append(p) or "files/forced",
        )
        mindmap_forces = []
        monkeypatch.setattr(
            "video_intel.process_mindmap",
            lambda *a, **kw: mindmap_forces.append(kw.get("force")) or (kw.get("prefix") or "video", "done"),
        )
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (a[6] if len(a) > 6 else kw.get("prefix") or "video", "done"),
        )
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: (kw.get("prefix") or "video", "done"),
        )

        args = _make_args(file=mp4, channel="everyinc", force=True)
        cmd_process(args, _config())

        assert len(upload_calls) == 1
        assert mindmap_forces == [True]


class TestCmdProcessExitCodeContract:
    def test_mindmap_failure_exits_non_zero_and_skips_concepts(self, stub_env, monkeypatch, tmp_path):
        """After 2026-05-02 inversion: transcript runs FIRST, then mindmap.
        Mindmap failure aborts the run before concepts (preserves the `process` exit-code contract:
        non-zero when the AI's discovery surface fails). Transcript runs regardless because it's
        Step 1 — its presence on disk is the input mindmap-from-transcript would read."""
        mp4, _ = _prep_mp4(tmp_path)

        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/test")
        # Make transcript succeed so we reach the mindmap step
        transcript_called = []
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: transcript_called.append(kw) or ("video", "done"),
        )
        monkeypatch.setattr(
            "video_intel.process_mindmap",
            lambda *a, **kw: (kw.get("prefix") or "video", "error: boom"),
        )
        concepts_called = []
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: concepts_called.append(kw) or ("video", "done"),
        )

        args = _make_args(file=mp4, channel="everyinc")
        with pytest.raises(SystemExit) as exc_info:
            cmd_process(args, _config())

        assert exc_info.value.code != 0
        # Transcript runs FIRST in the new ordering, so it WAS called
        assert len(transcript_called) == 1
        # Concepts must NOT run when mindmap fails
        assert concepts_called == []

    def test_transcript_failure_after_mindmap_exits_partial_and_preserves_mindmap(
        self, stub_env, monkeypatch, tmp_path
    ):
        """Issue #129 flipped this deliberately; it asserted exit 0 before.

        The PRESERVATION half of the partial-success contract is unchanged and is
        asserted here: a transcript failure never rolls back the mindmap. What
        changed is the REPORTING half - the run now exits EXIT_PARTIAL instead of
        disguising an incomplete video as success, because a batch driver reading
        the exit code was the thing that could not see the gap.
        """
        mp4, channel_dir = _prep_mp4(tmp_path)

        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/test")

        def fake_mindmap(*a, **kw):
            prefix = kw.get("prefix") or "video"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.mindmap.md", "done", "# stub mindmap\n")
            return prefix, "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (a[6] if len(a) > 6 else "video", "error: JSON parse failed"),
        )
        concepts_called = []
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: concepts_called.append(kw) or ("video", "done"),
        )

        args = _make_args(file=mp4, channel="everyinc")
        with pytest.raises(SystemExit) as exc_info:
            cmd_process(args, _config())

        assert exc_info.value.code == EXIT_PARTIAL
        # Unchanged: concepts is still skipped after a transcript failure, and
        # the mindmap artifact still survives on disk. Nothing is rolled back.
        assert concepts_called == []
        assert (channel_dir / "video.mindmap.md").exists()

    def test_concepts_failure_exits_partial(self, stub_env, monkeypatch, tmp_path):
        """Issue #129's headline case, inverted from what this test used to assert.

        A concepts step that reports an error and writes no .concepts.json used
        to exit 0. Because is_processed() never looks at concepts, that video was
        never re-queued and simply never reached taxonomy.json or the search
        index - the gap was only findable by walking the filesystem.
        """
        mp4, channel_dir = _prep_mp4(tmp_path)

        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/test")

        def fake_mindmap(*a, **kw):
            prefix = kw.get("prefix") or "video"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.mindmap.md", "done", "# stub mindmap\n")
            return prefix, "done"

        def fake_transcript(*a, **kw):
            prefix = a[6] if len(a) > 6 else "video"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.transcript.md", "done", "# stub\n")
            return prefix, "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)
        monkeypatch.setattr("video_intel.process_transcript", fake_transcript)
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: (kw.get("prefix") or "video", "error: taxonomy broken"),
        )

        args = _make_args(file=mp4, channel="everyinc")
        with pytest.raises(SystemExit) as exc_info:
            cmd_process(args, _config())

        assert exc_info.value.code == EXIT_PARTIAL
        # The steps that DID succeed keep their artifacts.
        assert (channel_dir / "video.mindmap.md").exists()
        assert (channel_dir / "video.transcript.md").exists()


class TestCmdProcessCodeReviewRegressions:
    """Regression tests for P1 findings from 2026-04-23 code review.

    Each test's docstring names the finding ID to keep the connection traceable.
    """

    def test_initial_upload_exception_exits_cleanly(self, stub_env, monkeypatch, tmp_path):
        """REL-1 / ADV-1: initial upload failure must not propagate as uncaught traceback.

        The initial upload_local_video call runs after the identity block is written
        to meta.json with last_error=None. If the upload raises, the orchestrator
        must catch, log the error to meta.json, and sys.exit(1).
        """
        mp4, channel_dir = _prep_mp4(tmp_path)

        def fake_upload(_client, _path):
            raise ConnectionError("network down")

        monkeypatch.setattr("video_intel.upload_local_video", fake_upload)
        # These should never be reached; if they are, the exit behavior is broken.
        monkeypatch.setattr(
            "video_intel.process_mindmap",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("mindmap should not run")),
        )
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("transcript should not run")),
        )

        args = _make_args(file=mp4, channel="everyinc")
        with pytest.raises(SystemExit) as exc_info:
            cmd_process(args, _config())
        assert exc_info.value.code != 0

        # Identity meta was written before the upload attempt; last_error must reflect the failure.
        meta_path = channel_dir / f"{mp4.stem}.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "upload" in (meta.get("last_error") or "").lower()

    def test_transcript_error_parsing_json_prefix_skips_concepts(self, stub_env, monkeypatch, tmp_path):
        """CORR-01: `startswith('error:')` missed `error parsing JSON:` from process_transcript.

        The looser prefix check must catch both 'error: ...' and 'error parsing JSON: ...'
        so concepts never runs against a missing/partial transcript.

        Issue #129: a transcript step that genuinely fails (no artifact, error
        status) now also means the run's requested artifact set is incomplete,
        so cmd_process exits EXIT_PARTIAL instead of returning 0. The transcript
        stub deliberately writes nothing here - that's the failure being tested.
        """
        mp4, channel_dir = _prep_mp4(tmp_path)

        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/test")

        def fake_mindmap(*a, **kw):
            prefix = kw.get("prefix") or "video"
            status = "done"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.mindmap.md", status, "# stub mindmap\n")
            return prefix, status

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (
                a[6] if len(a) > 6 else "video",
                "error parsing JSON: unterminated string",
            ),
        )
        concepts_called: list = []
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: concepts_called.append(kw) or ("video", "done"),
        )

        args = _make_args(file=mp4, channel="everyinc")
        with pytest.raises(SystemExit) as exc_info:
            cmd_process(args, _config())

        assert exc_info.value.code == EXIT_PARTIAL
        assert concepts_called == []  # error-parsing-JSON path must be recognized as failure


class TestCmdProcessFileDerivedPrefix:
    """Issue #186 review: cmd_process --file is the third caller of the changed
    resolver and had no coverage of the derived prefix. Mirrors the transcript
    coverage: artifacts land under {date}-{slug}, and an identical re-run
    lazy-skips without paying a second upload."""

    def _stub_pipeline(self, monkeypatch, channel_dir, upload_calls):
        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, p: upload_calls.append(p) or "files/uploaded-once",
        )

        # Each stub also records its mode the way the real helper does, so the
        # re-run's lazy-skip check sees the same modes_completed state the real
        # pipeline leaves behind (the Gate-1 real run recorded exactly these).
        import video_intel

        def fake_mindmap(*args, **kwargs):
            prefix = kwargs.get("prefix") or "video"
            (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap", encoding="utf-8")
            video_intel.update_meta(channel_dir / f"{prefix}.meta.json", {}, "scan")
            return prefix, "done"

        def fake_transcript(*args, **kwargs):
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix")
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.transcript.md", "done", "# stub transcript\n")
            video_intel.update_meta(channel_dir / f"{prefix}.meta.json", {}, "transcript")
            return prefix, "done"

        def fake_concepts(*args, **kwargs):
            prefix = kwargs.get("prefix") or "video"
            _write_stub_artifact_if_ok(channel_dir / f"{prefix}.concepts.json", "done", '{"concepts": []}')
            video_intel.update_meta(channel_dir / f"{prefix}.meta.json", {}, "concepts")
            return prefix, "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)
        monkeypatch.setattr("video_intel.process_transcript", fake_transcript)
        monkeypatch.setattr("video_intel.process_concepts", fake_concepts)

    def test_artifacts_land_under_the_derived_prefix_and_rerun_lazy_skips(self, stub_env, monkeypatch, tmp_path):
        mp4, channel_dir = _prep_mp4(tmp_path, name="gc720-hybrid-rag.mp4")
        upload_calls: list = []
        self._stub_pipeline(monkeypatch, channel_dir, upload_calls)

        args = _make_args(
            file=mp4, channel="everyinc", video_id="gc720hybrid", title="Hybrid RAG with Neo4j", date="2026-08-31"
        )
        cmd_process(args, _config())

        derived = "2026-08-31-hybrid-rag-with-neo4j"
        assert (channel_dir / f"{derived}.meta.json").exists()
        assert (channel_dir / f"{derived}.transcript.md").exists()
        assert not (channel_dir / "gc720-hybrid-rag.meta.json").exists()
        assert len(upload_calls) == 1

        # Identical re-run: same flags -> same derived prefix -> lazy-skip, no
        # second upload. This is the idempotency the both-flags rule protects.
        cmd_process(
            _make_args(
                file=mp4, channel="everyinc", video_id="gc720hybrid", title="Hybrid RAG with Neo4j", date="2026-08-31"
            ),
            _config(),
        )
        assert len(upload_calls) == 1, "a re-run with identical flags must not re-upload"
