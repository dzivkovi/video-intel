"""Tests for file-expiry fallback in cmd_process.

Unit 3 from the plan:
- _is_file_expiry_error_status parses the helper's returned "error: ..." string
- Positive cases (file_uri stale): detector returns True
- Negative cases (quota, rate, safety, members-only, transient): detector returns False
- cmd_process wraps mindmap and transcript calls with one re-upload + one retry
"""

import argparse
from unittest.mock import MagicMock

import pytest

from video_intel import _is_file_expiry_error_status, cmd_process


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


def _make_args(*, file=None, channel=None, force=False, **_):
    return argparse.Namespace(
        file=file,
        channel=channel,
        video_id=None,
        title=None,
        date=None,
        start=None,
        end=None,
        force=force,
        model=None,
        prompt=None,
    )


def _config(channel_names=("everyinc",)):
    return {"channels": [{"name": n, "url": f"https://youtube.com/@{n}"} for n in channel_names]}


class TestIsFileExpiryErrorStatusPositives:
    @pytest.mark.parametrize(
        "status",
        [
            "error: APIError: 403 File files/abc123 is in the FAILED state",
            "error: APIError: 404 File files/xyz not found",
            "error: APIError: Resource files/def expired",
            "error: google.api_core.exceptions.FailedPrecondition: File files/hvnbfnr5yht1 is not found.",
        ],
    )
    def test_file_expiry_signature_when_status_matches_returns_true(self, status):
        assert _is_file_expiry_error_status(status) is True


class TestIsFileExpiryErrorStatusNegatives:
    @pytest.mark.parametrize(
        "status",
        [
            "error: APIError: 403 quota exceeded for project X",  # quota (critical negative)
            "error: APIError: 429 rate limit",  # rate limit
            "error: APIError: 403 safety filter blocked output",  # safety
            "error: APIError: 403 permission_denied: members only content",  # YouTube gated
            "error: APIError: 500 internal server error",  # transient
            "error: ValueError: malformed response",  # unrelated parse
            "done",  # non-error status
            "skipped (exists)",  # skip status
            "",  # empty
        ],
    )
    def test_unrelated_failure_when_status_does_not_match_returns_false(self, status):
        assert _is_file_expiry_error_status(status) is False


class TestCmdProcessFileExpiryFallback:
    @pytest.fixture
    def stub_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setattr("video_intel.resolve_model", lambda _args, _cfg: "stub-model")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: f"prompt-for-{_name}")
        monkeypatch.setattr("video_intel.load_taxonomy", lambda _dir: {"concepts": {}})
        return tmp_path

    def _prep_mp4(self, tmp_path):
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "everyinc"
        channel_dir.mkdir(parents=True, exist_ok=True)
        mp4 = channel_dir / "video.mp4"
        mp4.write_bytes(b"fake mp4 bytes")
        return mp4, channel_dir

    def test_mindmap_file_expiry_re_uploads_once_and_retries_succeeds(self, stub_env, monkeypatch):
        mp4, channel_dir = self._prep_mp4(stub_env)

        upload_count = {"n": 0}

        def fake_upload(_client, _path):
            upload_count["n"] += 1
            return f"files/upload-{upload_count['n']}"

        monkeypatch.setattr("video_intel.upload_local_video", fake_upload)

        call_count = {"mindmap": 0}

        def fake_mindmap(*args, **kwargs):
            call_count["mindmap"] += 1
            if call_count["mindmap"] == 1:
                return (
                    kwargs.get("prefix") or "video",
                    "error: APIError: 403 File files/upload-1 is in the FAILED state",
                )
            # Second attempt: succeed, and write the artifact
            (channel_dir / f"{kwargs.get('prefix') or 'video'}.mindmap.md").write_text("m", encoding="utf-8")
            return kwargs.get("prefix") or "video", "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)

        def fake_transcript(*a, **kw):
            prefix = a[6] if len(a) > 6 else "video"
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

        # Pin mindmap_source=video: this test exercises the file-expiry
        # re-upload+retry wrapper, which only wraps the legacy
        # mindmap-from-video call (mindmap-from-transcript needs no file_uri
        # and never re-uploads). With a faithful transcript stub now writing
        # a real .transcript.md (issue #129), the default mindmap_source=auto
        # resolver would otherwise route mindmap through the transcript-source
        # path and bypass the retry wrapper entirely, defeating the test.
        config = _config()
        config["channels"][0]["mindmap_source"] = "video"

        cmd_process(_make_args(file=mp4, channel="everyinc"), config)

        assert upload_count["n"] == 2  # one initial, one re-upload
        assert call_count["mindmap"] == 2  # one failure, one retry

    def test_transcript_file_expiry_re_uploads_once_and_retries(self, stub_env, monkeypatch):
        mp4, channel_dir = self._prep_mp4(stub_env)

        upload_count = {"n": 0}

        def fake_upload(_client, _path):
            upload_count["n"] += 1
            return f"files/upload-{upload_count['n']}"

        monkeypatch.setattr("video_intel.upload_local_video", fake_upload)

        mindmap_call = {"n": 0}

        def fake_mindmap(*args, **kwargs):
            mindmap_call["n"] += 1
            (channel_dir / f"{kwargs.get('prefix') or 'video'}.mindmap.md").write_text("m", encoding="utf-8")
            return kwargs.get("prefix") or "video", "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)

        transcript_call = {"n": 0}

        def fake_transcript(*args, **kwargs):
            transcript_call["n"] += 1
            prefix = args[6] if len(args) > 6 else kwargs.get("prefix") or "video"
            if transcript_call["n"] == 1:
                status = "error: APIError: 404 File files/upload-1 not found"
            else:
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

        cmd_process(_make_args(file=mp4, channel="everyinc"), _config())

        assert upload_count["n"] == 2
        assert transcript_call["n"] == 2
        assert mindmap_call["n"] == 1  # mindmap still only called once

    def test_unrelated_error_does_not_trigger_re_upload(self, stub_env, monkeypatch):
        """Quota-exceeded 403 must NOT false-positive as file-expiry.

        After 2026-05-02 inversion: transcript runs FIRST, so it IS called once
        (and succeeds in this test). Mindmap then fails with the quota 403.
        Single upload, no re-upload, abort before concepts.
        """
        mp4, _ = self._prep_mp4(stub_env)

        upload_count = {"n": 0}
        monkeypatch.setattr(
            "video_intel.upload_local_video",
            lambda _c, _p: upload_count.__setitem__("n", upload_count["n"] + 1) or "files/upload-1",
        )

        # Transcript succeeds (runs first in new ordering)
        transcript_called = []
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: transcript_called.append(kw) or ("video", "done"),
        )
        # Mindmap fails with a quota error (NOT a file-expiry error)
        monkeypatch.setattr(
            "video_intel.process_mindmap",
            lambda *a, **kw: (
                kw.get("prefix") or "video",
                "error: APIError: 403 quota exceeded for project X",
            ),
        )
        concepts_called = []
        monkeypatch.setattr(
            "video_intel.process_concepts",
            lambda *a, **kw: concepts_called.append(kw) or ("video", "done"),
        )

        with pytest.raises(SystemExit) as exc_info:
            cmd_process(_make_args(file=mp4, channel="everyinc"), _config())

        assert exc_info.value.code != 0
        assert upload_count["n"] == 1  # no re-upload on quota error (the contract under test)
        assert len(transcript_called) == 1  # transcript runs first in new ordering
        assert concepts_called == []  # aborted after mindmap failure

    def test_re_upload_failure_does_not_loop(self, stub_env, monkeypatch):
        """If re-upload itself fails, cmd_process must not enter a second retry loop."""
        mp4, _ = self._prep_mp4(stub_env)

        upload_count = {"n": 0}

        def fake_upload(_client, _path):
            upload_count["n"] += 1
            if upload_count["n"] == 2:
                raise ConnectionError("network down")
            return "files/upload-1"

        monkeypatch.setattr("video_intel.upload_local_video", fake_upload)

        mindmap_call = {"n": 0}

        def fake_mindmap(*args, **kwargs):
            mindmap_call["n"] += 1
            return (
                kwargs.get("prefix") or "video",
                "error: APIError: 403 File files/upload-1 is in the FAILED state",
            )

        monkeypatch.setattr("video_intel.process_mindmap", fake_mindmap)
        monkeypatch.setattr("video_intel.process_transcript", lambda *a, **kw: ("video", "done"))
        monkeypatch.setattr("video_intel.process_concepts", lambda *a, **kw: ("video", "done"))

        with pytest.raises(SystemExit) as exc_info:
            cmd_process(_make_args(file=mp4, channel="everyinc"), _config())

        assert exc_info.value.code != 0
        assert upload_count["n"] == 2  # one initial, one failed re-upload, then stop
        assert mindmap_call["n"] == 1  # no third attempt
