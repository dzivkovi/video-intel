"""A YouTube error during a scan never prints the API key and never ends the scan for a later channel (issue #320).

The real `cmd_scan`, `fetch_channel_videos`, `enrich_with_durations` and
`fetch_preflight_status` run against a fake YouTube client; only the client and
the Gemini-backed writers are stubbed [core: real-caller]. The fake raises a real
`googleapiclient.errors.HttpError` whose request URI carries `key=SECRET`, the way
the real client reports every request it built with `developerKey`.

Falsification (each one fails a named test here): restore `except HttpError ...
raise` at either site (that site's 500 and transport cases), swap
`_is_youtube_quota_stop` back to the 403-only test (the 429 cases), log `str(e)`
in one WARNING (the secret assertions), remove the wrapper's catch (the fetch
quota-stop case), drop one `errors.append` (that site's FAILED-summary
assertion), remove the googleapiclient log filter from `main()` (the subprocess
case).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httplib2
import pytest

import video_intel as vi

SECRET = "SECRET_KEY_VALUE"
API = "https://youtube.googleapis.com/youtube/v3"
UC_A = "UC" + "A" * 22
UC_B = "UC" + "B" * 22
ENRICH_PART = "contentDetails"
PREFLIGHT_PART = "snippet,status,liveStreamingDetails"
SHORTS_STEP = "classifying Shorts"
PREFLIGHT_STEP = "pre-flight metadata check"


def _http_error(status: int, reason: str | None, path: str = "videos") -> vi.HttpError:
    body = {"error": {"code": status, "message": "boom", "errors": [{"reason": reason, "message": "boom"}]}}
    if reason is None:
        body = {"error": {"code": status, "message": "boom"}}
    uri = f"{API}/{path}?id=vid_A&part=x&key={SECRET}&alt=json"
    return vi.HttpError(httplib2.Response({"status": status}), json.dumps(body).encode("utf-8"), uri=uri)


def _transport_error() -> OSError:
    return OSError(f"connection reset while requesting {API}/videos?id=vid_A&key={SECRET}")


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Resource:
    def __init__(self, handler):
        self._handler = handler

    def list(self, **kwargs):
        return _Request(lambda: self._handler(**kwargs))


class FakeYouTube:
    """Each channel id has one recent upload, `vid_<third char of the id>`.

    `fail` maps (video id, videos.list part) to an exception; `dead` maps an
    uploads playlist id to an exception.
    """

    def __init__(self):
        self.fail: dict[tuple[str, str], Exception] = {}
        self.dead: dict[str, Exception] = {}
        self.playlists_fetched: list[str] = []
        self.videos_calls: list[tuple[str, str]] = []
        self.published = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def channels(self):
        def handler(**_kwargs):
            raise AssertionError("an explicit channel_id makes no handle lookup")

        return _Resource(handler)

    def playlistItems(self):
        def handler(part, playlistId, maxResults, pageToken):
            self.playlists_fetched.append(playlistId)
            if playlistId in self.dead:
                raise self.dead[playlistId]
            vid = f"vid_{playlistId[2]}"
            return {
                "items": [
                    {
                        "snippet": {"title": f"Talk {vid}", "publishedAt": self.published},
                        "contentDetails": {"videoId": vid, "videoPublishedAt": self.published},
                    }
                ]
            }

        return _Resource(handler)

    def videos(self):
        def handler(id, part):
            self.videos_calls.append((id, part))
            for vid in id.split(","):
                if (vid, part) in self.fail:
                    raise self.fail[(vid, part)]
            if part == ENRICH_PART:
                return {"items": [{"id": v, "contentDetails": {"duration": "PT10M"}} for v in id.split(",")]}
            return {
                "items": [
                    {"id": v, "snippet": {"liveBroadcastContent": "none"}, "status": {"privacyStatus": "public"}}
                    for v in id.split(",")
                ]
            }

        return _Resource(handler)


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    env = SimpleNamespace(corpus=corpus, youtube=FakeYouTube(), written=[])
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", SECRET)
    monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *_a, **_k: env.youtube)
    monkeypatch.setattr(vi, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: corpus)
    monkeypatch.setattr(vi, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(vi, "record_alt_title_if_rotated", lambda *a, **kw: False)
    monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_kw: None)
    monkeypatch.setattr(vi, "render_headline_digest", lambda *_a, **_kw: None)
    monkeypatch.setattr(vi, "rebuild_free_derived_layers", lambda *_a, **_kw: None)
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _vid: False)

    def fake_mindmap(*args, **kwargs):
        video = args[2]
        env.written.append(("mindmap", video["video_id"]))
        return kwargs.get("prefix") or vi.video_file_prefix(video), "error: stub"

    def fake_transcribe(**kw):
        env.written.append(("transcript", kw["video"]["video_id"]))
        return kw["prefix"], "error: stub"

    monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)
    monkeypatch.setattr(vi, "_scan_transcribe_one", fake_transcribe)
    monkeypatch.setattr(vi, "process_concepts", lambda *a, **kw: (kw.get("prefix"), "error: stub"))
    return env


def _args(*, dry_run=False):
    return argparse.Namespace(channel=None, since=None, force=False, dry_run=dry_run, model=None)


def _config() -> dict:
    return {
        "channels": [
            {"name": "chan_a", "url": "https://youtube.com/@a", "channel_id": UC_A},
            {"name": "chan_b", "url": "https://youtube.com/@b", "channel_id": UC_B},
        ]
    }


def _run(env, caplog, *, dry_run=False):
    with caplog.at_level(logging.DEBUG), caplog.at_level(logging.DEBUG, logger="video_intel"):
        return vi.cmd_scan(_args(dry_run=dry_run), _config())


def _assert_secret_nowhere(caplog, capsys):
    assert SECRET not in caplog.text
    for record in caplog.records:
        assert SECRET not in record.getMessage()
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def _step_warnings(caplog, step: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and step in r.getMessage()]


def _failed_lines(caplog, channel: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage().startswith(f"  [{channel}] {channel}: error:")
    ]


def _has_failed_block(caplog) -> bool:
    return any("FAILED ---" in r.getMessage() for r in caplog.records)


def _messages(caplog, level) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


SITES = [
    pytest.param(ENRICH_PART, SHORTS_STEP, id="shorts-enrich"),
    pytest.param(PREFLIGHT_PART, PREFLIGHT_STEP, id="preflight"),
]


@pytest.mark.parametrize(("part", "step"), SITES)
@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: _http_error(500, "backendError"), id="http-500"),
        pytest.param(lambda: _http_error(403, "forbidden"), id="http-403-non-quota"),
        pytest.param(_transport_error, id="transport"),
    ],
)
def test_cmd_scan_non_quota_error_at_site_warns_once_records_failure_and_reaches_next_channel(
    scan_env, caplog, capsys, part, step, error_factory
):
    # Arrange
    scan_env.youtube.fail[("vid_A", part)] = error_factory()

    # Act
    result = _run(scan_env, caplog)

    # Assert: the scan returns normally through the failure summary, channel B is processed.
    assert result is None
    assert scan_env.youtube.playlists_fetched == ["UU" + UC_A[2:], "UU" + UC_B[2:]]
    assert "vid_B" in {vid for _, vid in scan_env.written}
    assert "vid_A" not in {vid for _, vid in scan_env.written}
    warnings = _step_warnings(caplog, step)
    assert len(warnings) == 1
    assert "chan_a" in warnings[0]
    assert len(_failed_lines(caplog, "chan_a")) == 1
    assert not _failed_lines(caplog, "chan_b")
    assert "Done." in _messages(caplog, logging.INFO)
    _assert_secret_nowhere(caplog, capsys)


@pytest.mark.parametrize(
    ("part", "quota_message"),
    [
        pytest.param(ENRICH_PART, "YouTube quota exhausted while classifying Shorts", id="shorts-enrich"),
        pytest.param(PREFLIGHT_PART, "YouTube quota exhausted during pre-flight metadata check", id="preflight"),
    ],
)
def test_cmd_scan_429_quota_reason_at_site_keeps_the_quota_outcome(scan_env, caplog, capsys, part, quota_message):
    # Arrange
    scan_env.youtube.fail[("vid_A", part)] = _http_error(429, "rateLimitExceeded")

    # Act
    _run(scan_env, caplog)

    # Assert: the fixed quota ERROR, the next channel runs, nothing in the FAILED summary.
    assert any(quota_message in m and "chan_a" in m for m in _messages(caplog, logging.ERROR))
    assert "vid_B" in {vid for _, vid in scan_env.written}
    assert not _failed_lines(caplog, "chan_a")
    assert not _step_warnings(caplog, SHORTS_STEP) and not _step_warnings(caplog, PREFLIGHT_STEP)
    _assert_secret_nowhere(caplog, capsys)


def test_cmd_scan_dry_run_error_at_enrich_warns_without_failed_summary_and_previews_next_channel(
    scan_env, caplog, capsys
):
    # Arrange
    scan_env.youtube.fail[("vid_A", ENRICH_PART)] = _http_error(500, "backendError")

    # Act
    _run(scan_env, caplog, dry_run=True)

    # Assert
    assert len(_step_warnings(caplog, SHORTS_STEP)) == 1
    assert not _has_failed_block(caplog)
    assert ("vid_B", ENRICH_PART) in scan_env.youtube.videos_calls
    assert any("Talk vid_B" in m for m in _messages(caplog, logging.INFO))
    assert scan_env.written == []
    _assert_secret_nowhere(caplog, capsys)


def test_cmd_scan_quota_stop_at_uploads_fetch_exits_1_with_one_error_and_no_raw_http_error(scan_env, caplog, capsys):
    # Arrange
    scan_env.youtube.dead["UU" + UC_A[2:]] = _http_error(403, "quotaExceeded", path="playlistItems")

    # Act
    with pytest.raises(SystemExit) as exc:
        _run(scan_env, caplog)

    # Assert
    assert exc.value.code == 1
    errors = _messages(caplog, logging.ERROR)
    assert len(errors) == 1
    assert "quotaExceeded" in errors[0]
    assert "HttpError" not in caplog.text
    assert scan_env.youtube.playlists_fetched == ["UU" + UC_A[2:]]
    _assert_secret_nowhere(caplog, capsys)


def test_cmd_scan_wrapper_lets_a_non_http_error_propagate_unchanged(scan_env, monkeypatch):
    # Arrange
    boom = RuntimeError("not a YouTube error")

    def explode(*_a, **_kw):
        raise boom

    monkeypatch.setattr(vi, "resolve_channel_identity", explode)

    # Act
    with pytest.raises(RuntimeError) as exc:
        vi.cmd_scan(_args(), _config())

    # Assert
    assert exc.value is boom


def test_main_masks_the_key_in_googleapiclient_log_records(tmp_path):
    # Arrange: the real main() configures logging; the dispatched command is replaced by
    # one that logs the way googleapiclient.http logs a malformed error body.
    script = textwrap.dedent(
        f"""
        import logging, sys
        sys.path.insert(0, {str(Path(vi.__file__).parent)!r})
        import video_intel as vi

        def fake_status(args, config):
            logging.getLogger("googleapiclient.http").warning(
                "Invalid JSON content from response: %s", "<html>{API}/videos?key={SECRET}&alt=json</html>"
            )
            logging.getLogger("other.lib").warning("untouched key=VISIBLE_OTHER")

        vi.load_config = lambda *a, **kw: {{"output_dir": {str(tmp_path)!r}}}
        vi.cmd_status = fake_status
        sys.argv = ["video_intel.py", "status"]
        vi.main()
        """
    )
    env = {k: v for k, v in os.environ.items() if k not in ("YOUTUBE_API_KEY", "GEMINI_API_KEY")}

    # Act
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, env=env, check=False
    )

    # Assert
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Invalid JSON content from response" in output
    assert "key=REDACTED" in output
    assert SECRET not in output
    assert "key=VISIBLE_OTHER" in output
