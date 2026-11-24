"""One failing headline channel never costs the others, and never leaks the API key (issue #311).

The real `render_headline_digest`, `get_channel_id`, `fetch_channel_videos` and
`enrich_with_durations` run against a fake YouTube client; only the client (the
paid boundary) and the /shorts HEAD probe (network) are stubbed.

Falsification: put the bare `raise` back in either per-channel block, or in the
digest's enrich block, and the isolation tests fail; make `describe_http_error` or
`describe_error_for_log` return `str(e)` and the redaction tests fail. Each test
below names what it guards; the falsification table lives in the PR.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel as vi

SECRET = "SECRET123"
API = "https://youtube.googleapis.com/youtube/v3"


def _channel_id(handle: str) -> str:
    return "UC" + (handle * 22)[:22]


def _http_error(status: int, reason: str, path: str, query: str) -> vi.HttpError:
    resp = MagicMock(status=status, reason="Error")
    body = {"error": {"code": status, "message": "boom", "errors": [{"reason": reason, "message": "boom"}]}}
    uri = f"{API}/{path}?{query}&key={SECRET}&alt=json"
    return vi.HttpError(resp, json.dumps(body).encode("utf-8"), uri=uri)


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
    """Three channels `a`, `b`, `c`, one recent upload each (`a1`, `b1`, `c1`).

    `fail` maps a handle to (stage, error) where stage is "channels" or "playlist";
    `enrich_error`, when set, is raised by the duration enrich (`videos().list`).
    """

    def __init__(self, fail: dict | None = None, enrich_error: Exception | None = None):
        self.fail = fail or {}
        self.enrich_error = enrich_error
        self.handles_resolved: list[str] = []
        published = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.published = published

    def _for(self, handle, stage):
        entry = self.fail.get(handle)
        if entry and entry[0] == stage:
            raise entry[1]

    def channels(self):
        def handler(part, forHandle=None, id=None):
            handle = forHandle or id
            self.handles_resolved.append(handle)
            self._for(handle, "channels")
            return {"items": [{"id": _channel_id(handle), "snippet": {"title": handle.upper()}}]}

        return _Resource(handler)

    def playlistItems(self):
        def handler(part, playlistId, maxResults, pageToken):
            handle = playlistId[2]
            self._for(handle, "playlist")
            vid = f"{handle}1"
            return {
                "items": [
                    {
                        "snippet": {"title": f"Talk from {handle}", "publishedAt": self.published},
                        "contentDetails": {"videoId": vid, "videoPublishedAt": self.published},
                    }
                ]
            }

        return _Resource(handler)

    def videos(self):
        def handler(id, part):
            if self.enrich_error is not None:
                raise self.enrich_error
            return {"items": [{"id": v, "contentDetails": {"duration": "PT10M"}} for v in id.split(",")]}

        return _Resource(handler)


def _config(tmp_path):
    return {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": f"chan_{h}", "url": f"https://youtube.com/@{h}", "enabled": False, "headline_digest": True}
            for h in ("a", "b", "c")
        ],
    }


def _seen(tmp_path) -> set[str]:
    path = tmp_path / "_headlines" / "seen.json"
    return set(json.loads(path.read_text(encoding="utf-8"))["seen"])


def _assert_secret_nowhere(caplog, capsys):
    for record in caplog.records:
        assert SECRET not in record.getMessage()
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def _channel_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "chan_b" in r.getMessage()]


def _no_shorts_probe(monkeypatch):
    monkeypatch.setattr("video_intel._is_youtube_short_url", lambda _vid: False)


# ---------------------------------------------------------------------------
# Isolation: one failing channel is skipped, the others render and are marked seen
# ---------------------------------------------------------------------------


def test_render_playlist_not_found_on_middle_channel_renders_others_and_warns_once(
    monkeypatch, tmp_path, caplog, capsys
):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(404, "playlistNotFound", "playlistItems", "part=snippet&playlistId=UUbbb")
    youtube = FakeYouTube(fail={"b": ("playlist", err)})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert {v["video_id"] for v in rendered} == {"a1", "c1"}
    assert _seen(tmp_path) == {"a1", "c1"}
    warnings = _channel_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "https://youtube.com/@b" in message
    assert "404" in message
    assert "no public uploads (playlistNotFound)" in message
    assert "config.yaml" in message
    _assert_secret_nowhere(caplog, capsys)


def test_render_skipped_channel_surfaces_on_next_run_once_fixed(monkeypatch, tmp_path):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(404, "playlistNotFound", "playlistItems", "playlistId=UUbbb")
    config = _config(tmp_path)
    vi.render_headline_digest(FakeYouTube(fail={"b": ("playlist", err)}), config, tmp_path, dry_run=False)

    # Act
    second = vi.render_headline_digest(FakeYouTube(), config, tmp_path, dry_run=False)

    # Assert
    assert [v["video_id"] for v in second] == ["b1"]


def test_render_channel_resolution_error_on_middle_channel_renders_others(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(400, "invalidArgument", "channels", "part=id%2Csnippet&forHandle=b")
    youtube = FakeYouTube(fail={"b": ("channels", err)})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert {v["video_id"] for v in rendered} == {"a1", "c1"}
    assert _seen(tmp_path) == {"a1", "c1"}
    warnings = _channel_warnings(caplog)
    assert len(warnings) == 1
    assert "400" in warnings[0].getMessage()
    assert "invalidArgument" in warnings[0].getMessage()
    _assert_secret_nowhere(caplog, capsys)


def test_render_server_error_on_middle_channel_renders_others(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(500, "backendError", "playlistItems", "playlistId=UUbbb")
    youtube = FakeYouTube(fail={"b": ("playlist", err)})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert {v["video_id"] for v in rendered} == {"a1", "c1"}
    warnings = _channel_warnings(caplog)
    assert len(warnings) == 1
    assert "500" in warnings[0].getMessage()
    assert "backendError" in warnings[0].getMessage()
    _assert_secret_nowhere(caplog, capsys)


def test_render_quota_on_middle_channel_still_stops_the_digest(monkeypatch, tmp_path):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(403, "quotaExceeded", "playlistItems", "playlistId=UUbbb")
    youtube = FakeYouTube(fail={"b": ("playlist", err)})

    # Act
    rendered = vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert: the third channel is never asked for; the first still renders.
    assert "c" not in youtube.handles_resolved
    assert [v["video_id"] for v in rendered] == ["a1"]


def test_render_quota_at_channel_resolution_still_stops_the_digest(monkeypatch, tmp_path):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(403, "quotaExceeded", "channels", "part=id%2Csnippet&forHandle=b")
    youtube = FakeYouTube(fail={"b": ("channels", err)})

    # Act
    rendered = vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert "c" not in youtube.handles_resolved
    assert [v["video_id"] for v in rendered] == ["a1"]


def test_render_rate_limit_429_stops_the_digest(monkeypatch, tmp_path):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(429, "rateLimitExceeded", "playlistItems", "playlistId=UUbbb")
    youtube = FakeYouTube(fail={"b": ("playlist", err)})

    # Act
    rendered = vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert "c" not in youtube.handles_resolved
    assert [v["video_id"] for v in rendered] == ["a1"]


def test_render_transport_error_on_middle_channel_renders_others_without_key(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = ConnectionError(f"timed out requesting {API}/playlistItems?playlistId=UUbbb&key={SECRET}&alt=json")
    youtube = FakeYouTube(fail={"b": ("playlist", err)})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert {v["video_id"] for v in rendered} == {"a1", "c1"}
    warnings = _channel_warnings(caplog)
    assert len(warnings) == 1
    assert "ConnectionError" in warnings[0].getMessage()
    _assert_secret_nowhere(caplog, capsys)


def _all_invalid_key():
    err = _http_error(400, "keyInvalid", "channels", "part=id%2Csnippet&forHandle=x")
    return FakeYouTube(fail={h: ("channels", err) for h in "abc"})


def _error_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


def test_render_all_channels_fail_logs_one_error_and_raises(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    youtube = _all_invalid_key()

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"), pytest.raises(vi.HeadlineDigestAllFailed):
        vi.render_headline_digest(youtube, _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    errors = _error_lines(caplog)
    assert len(errors) == 1
    assert "all 3 headline channels failed" in errors[0]
    assert "keyInvalid" in errors[0] and "YOUTUBE_API_KEY" in errors[0]
    assert "no new uploads" not in caplog.text
    assert not (tmp_path / "_headlines" / "seen.json").exists()
    _assert_secret_nowhere(caplog, capsys)


def _headlines_env(monkeypatch, youtube):
    monkeypatch.setattr("video_intel.require_youtube", lambda: lambda *_a, **_k: youtube)
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr("video_intel.backup_config_if_changed", lambda _out: None)


def test_cmd_headlines_all_channels_fail_exits_1_without_key(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    _headlines_env(monkeypatch, _all_invalid_key())

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"), pytest.raises(SystemExit) as exc:
        vi.cmd_headlines(SimpleNamespace(dry_run=False), _config(tmp_path))

    # Assert
    assert exc.value.code == 1
    assert len(_error_lines(caplog)) == 1
    _assert_secret_nowhere(caplog, capsys)


def test_cmd_scan_all_headline_channels_fail_is_non_fatal_without_key(monkeypatch, tmp_path, caplog, capsys):
    # Arrange: only headline channels, so the primary loop has nothing to fetch.
    youtube = _all_invalid_key()
    monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr("video_intel.require_youtube", lambda: lambda *_a, **_k: youtube)
    monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
    args = argparse.Namespace(channel=None, since=None, dry_run=False, force=False, model=None)

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(args, _config(tmp_path))

    # Assert
    assert "all 3 headline channels failed" in _outer_warning(caplog)
    assert any(r.getMessage() == "Done." for r in caplog.records)
    _assert_secret_nowhere(caplog, capsys)


def test_cmd_headlines_unexpected_exception_exits_1_without_key_or_traceback(monkeypatch, tmp_path, caplog, capsys):
    # Arrange: a transport error from the enrich call, which only catches HttpError.
    err = ConnectionError(f"reset requesting {API}/videos?id=a1&key={SECRET}&alt=json")
    _headlines_env(monkeypatch, FakeYouTube(enrich_error=err))
    _no_shorts_probe(monkeypatch)

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"), pytest.raises(SystemExit) as exc:
        vi.cmd_headlines(SimpleNamespace(dry_run=False), _config(tmp_path))

    # Assert
    assert exc.value.code == 1
    errors = _error_lines(caplog)
    assert len(errors) == 1 and "ConnectionError" in errors[0]
    assert "Traceback" not in caplog.text
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    assert SECRET not in captured.out + captured.err
    assert all(SECRET not in r.getMessage() for r in caplog.records)


def test_match_configured_channel_http_error_logs_no_key(caplog, capsys):
    # Arrange
    err = _http_error(400, "keyInvalid", "channels", "part=id%2Csnippet&forHandle=a")
    youtube = FakeYouTube(fail={"a": ("channels", err)})
    config = {"channels": [{"name": "chan_a", "url": "https://youtube.com/@a"}]}

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.match_configured_channel(youtube, config, "UCzzzzzzzzzzzzzzzzzzzzzz")

    # Assert
    resolve = [r.getMessage() for r in caplog.records if "Could not resolve channel id" in r.getMessage()]
    assert len(resolve) == 1 and "keyInvalid" in resolve[0]
    _assert_secret_nowhere(caplog, capsys)


def _forbid_shorts_probe(monkeypatch):
    def _fail(*_a, **_k):
        pytest.fail("a degraded enrich must skip the Shorts filter, as the quota path does")

    monkeypatch.setattr("video_intel.is_short", _fail)


def _enrich_warnings(caplog):
    return [r for r in caplog.records if "durations could not be fetched" in r.getMessage()]


def test_render_enrich_server_error_degrades_like_quota_without_key(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    _forbid_shorts_probe(monkeypatch)
    quota = _http_error(403, "quotaExceeded", "videos", "id=a1%2Cb1%2Cc1&part=contentDetails")
    quota_rendered = vi.render_headline_digest(
        FakeYouTube(enrich_error=quota), _config(tmp_path / "quota"), tmp_path / "quota", dry_run=True
    )
    err = _http_error(500, "backendError", "videos", "id=a1%2Cb1%2Cc1&part=contentDetails")
    caplog.clear()

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.render_headline_digest(FakeYouTube(enrich_error=err), _config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert {v["video_id"] for v in rendered} == {v["video_id"] for v in quota_rendered} == {"a1", "b1", "c1"}
    assert _seen(tmp_path) == {"a1", "b1", "c1"}
    warnings = _enrich_warnings(caplog)
    assert len(warnings) == 1
    assert "500" in warnings[0].getMessage() and "backendError" in warnings[0].getMessage()
    _assert_secret_nowhere(caplog, capsys)


def test_cmd_headlines_enrich_server_error_renders_without_key(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    _forbid_shorts_probe(monkeypatch)
    err = _http_error(500, "backendError", "videos", "id=a1%2Cb1%2Cc1&part=contentDetails")
    youtube = FakeYouTube(enrich_error=err)
    monkeypatch.setattr("video_intel.require_youtube", lambda: lambda *_a, **_k: youtube)
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr("video_intel.backup_config_if_changed", lambda _out: None)

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.cmd_headlines(SimpleNamespace(dry_run=False), _config(tmp_path))

    # Assert
    assert {v["video_id"] for v in rendered} == {"a1", "b1", "c1"}
    assert len(_enrich_warnings(caplog)) == 1
    _assert_secret_nowhere(caplog, capsys)


# ---------------------------------------------------------------------------
# The standalone command behaves the same
# ---------------------------------------------------------------------------


def test_cmd_headlines_one_failing_channel_exits_normally_with_others_rendered(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    _no_shorts_probe(monkeypatch)
    err = _http_error(404, "playlistNotFound", "playlistItems", "playlistId=UUbbb")
    youtube = FakeYouTube(fail={"b": ("playlist", err)})
    monkeypatch.setattr("video_intel.require_youtube", lambda: lambda *_a, **_k: youtube)
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr("video_intel.backup_config_if_changed", lambda _out: None)

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.cmd_headlines(SimpleNamespace(dry_run=False), _config(tmp_path))

    # Assert
    assert {v["video_id"] for v in rendered} == {"a1", "c1"}
    assert _seen(tmp_path) == {"a1", "c1"}
    assert len(_channel_warnings(caplog)) == 1
    _assert_secret_nowhere(caplog, capsys)


# ---------------------------------------------------------------------------
# cmd_scan's outer handler never logs str(e)
# ---------------------------------------------------------------------------


def _scan_with_failing_digest(monkeypatch, tmp_path, exc):
    monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
    monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
    monkeypatch.setattr("video_intel.get_channel_id", lambda *_a, **_k: (None, None))

    def _raise(*_a, **_k):
        raise exc

    monkeypatch.setattr("video_intel.render_headline_digest", _raise)
    config = {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "regular", "url": "https://youtube.com/@r"},
            {"name": "peripheral", "url": "https://youtube.com/@p", "enabled": False, "headline_digest": True},
        ],
    }
    args = argparse.Namespace(channel=None, since=None, dry_run=False, force=False, model=None)
    vi.cmd_scan(args, config)


def _outer_warning(caplog):
    hits = [r.getMessage() for r in caplog.records if "Headline digest failed (non-fatal)" in r.getMessage()]
    assert len(hits) == 1
    return hits[0]


def test_cmd_scan_outer_handler_http_error_logs_no_key(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    err = _http_error(404, "playlistNotFound", "playlistItems", "playlistId=UUbbb")

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        _scan_with_failing_digest(monkeypatch, tmp_path, err)

    # Assert
    message = _outer_warning(caplog)
    assert "404" in message
    assert "playlistNotFound" in message
    _assert_secret_nowhere(caplog, capsys)


def test_cmd_scan_outer_handler_other_exception_redacts_key(monkeypatch, tmp_path, caplog, capsys):
    # Arrange
    err = RuntimeError(f"failed requesting {API}/videos?part=id&key={SECRET}&alt=json")

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        _scan_with_failing_digest(monkeypatch, tmp_path, err)

    # Assert
    message = _outer_warning(caplog)
    assert "RuntimeError" in message
    assert "part=id" in message
    _assert_secret_nowhere(caplog, capsys)


# ---------------------------------------------------------------------------
# The one helper
# ---------------------------------------------------------------------------


def test_describe_http_error_well_formed_names_status_reason_path_without_query():
    # Arrange
    err = _http_error(404, "playlistNotFound", "playlistItems", "part=snippet&playlistId=UUbbb")

    # Act
    text = vi.describe_http_error(err)

    # Assert
    assert "404" in text
    assert "playlistNotFound" in text
    assert "/youtube/v3/playlistItems" in text
    assert SECRET not in text
    assert "playlistId" not in text  # the whole query string is dropped


def test_describe_http_error_non_json_body_degrades_without_raising():
    err = vi.HttpError(MagicMock(status=502, reason="Bad Gateway"), b"<html>oops</html>", uri=f"{API}/x?key={SECRET}")
    text = vi.describe_http_error(err)
    assert "502" in text
    assert SECRET not in text


def test_describe_http_error_missing_resp_and_uri_degrades_without_raising():
    text = vi.describe_http_error(SimpleNamespace())
    assert isinstance(text, str) and text


def test_redact_api_key_masks_only_the_key_value():
    text = vi.redact_api_key(f"GET {API}/videos?part=id&key={SECRET}&alt=json")
    assert SECRET not in text
    assert "part=id" in text and "alt=json" in text


@pytest.mark.parametrize("spelling", ["KEY", "Key", "kEy"])
def test_redact_api_key_any_case_is_masked(spelling):
    text = vi.redact_api_key(f"GET {API}/videos?part=id&{spelling}={SECRET}&alt=json")
    assert SECRET not in text
