"""Channels are followed by id, so a re-registered handle cannot redirect a scan (issue #312).

The real `cmd_scan`, `render_headline_digest` and `match_configured_channel`
run, together with the real `get_channel_id` and `fetch_channel_videos`,
against a fake YouTube client. Only the client and the Gemini-backed writers
are stubbed [core: real-caller]. The scan tests assert on the uploads playlist
that was actually fetched and on the writers that were NOT called, never on an
exit code.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel as vi

UC_A = "UC" + "A" * 22
UC_B = "UC" + "B" * 22
UC_C = "UC" + "C" * 22
HANDLE_URL = "https://www.youtube.com/@creator"


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
    """`handles` maps a handle to a channel id (or to an exception to raise).

    Each channel id has one recent upload whose video id names its owner, so a
    stranger's video is recognizable wherever it lands.
    """

    def __init__(self, handles: dict):
        self.handles = handles
        self.channel_lookups: list[dict] = []
        self.playlists_fetched: list[str] = []
        self.published = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def video_of(channel_id: str) -> str:
        return f"vid_{channel_id[2]}"

    def channels(self):
        def handler(part, forHandle=None, id=None):
            self.channel_lookups.append({"part": part, "forHandle": forHandle, "id": id})
            key = forHandle or id
            answer = self.handles.get(key)
            if isinstance(answer, Exception):
                raise answer
            if answer is None:
                return {"items": []}
            return {"items": [{"id": answer, "snippet": {"title": f"Title {answer[2]}"}}]}

        return _Resource(handler)

    def playlistItems(self):
        def handler(part, playlistId, maxResults, pageToken):
            self.playlists_fetched.append(playlistId)
            vid = self.video_of("UC" + playlistId[2:])
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
            return {"items": [{"id": v, "contentDetails": {"duration": "PT10M"}} for v in id.split(",")]}

        return _Resource(handler)


def _uploads(channel_id: str) -> str:
    return "UU" + channel_id[2:]


def _ids_path(corpus):
    return corpus / "_channels" / "ids.json"


def _seed_ids(corpus, entries: dict) -> None:
    path = _ids_path(corpus)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries), encoding="utf-8")


def _learned(channel_id: str, title: str = "Known") -> dict:
    return {"channel_id": channel_id, "title": title, "first_seen": "2026-01-01"}


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# cmd_scan harness
# ---------------------------------------------------------------------------


@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    env = SimpleNamespace(corpus=corpus, youtube=None, written=[])
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *_a, **_k: env.youtube)
    monkeypatch.setattr(vi, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _cfg: corpus)
    monkeypatch.setattr(vi, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda *a, **kw: {})
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

    def fake_concepts(client, types_, video, *a, **kw):
        env.written.append(("concepts", video["video_id"]))
        return kw.get("prefix"), "error: stub"

    monkeypatch.setattr(vi, "process_mindmap", fake_mindmap)
    monkeypatch.setattr(vi, "_scan_transcribe_one", fake_transcribe)
    monkeypatch.setattr(vi, "process_concepts", fake_concepts)
    return env


def _scan_args(*, dry_run=False):
    return argparse.Namespace(channel=None, since=None, force=False, dry_run=dry_run, model=None)


def _scan_config(**extra) -> dict:
    return {"channels": [{"name": "chan", "url": HANDLE_URL, **extra}]}


def _written_ids(env) -> set[str]:
    return {vid for _, vid in env.written}


def test_cmd_scan_learned_id_when_handle_resolves_elsewhere_fetches_known_id_and_warns_once(scan_env, caplog):
    # Arrange: the handle was learned as UC_A; it now belongs to UC_B, which has uploads.
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A, "Original Creator")})
    scan_env.youtube = FakeYouTube({"creator": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    assert FakeYouTube.video_of(UC_B) not in _written_ids(scan_env)
    assert FakeYouTube.video_of(UC_A) in _written_ids(scan_env)
    mismatch = [w for w in _warnings(caplog) if UC_B in w]
    assert len(mismatch) == 1
    for needle in ("chan", HANDLE_URL, UC_A, "Original Creator", UC_B, "Title B", "channel_id", "ids.json"):
        assert needle in mismatch[0]


def test_cmd_scan_explicit_channel_id_skips_handle_lookup_and_state_file(scan_env, caplog):
    # Arrange: no state file; the config pins UC_A while the handle points at UC_B.
    scan_env.youtube = FakeYouTube({"creator": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config(channel_id=UC_A))

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    assert scan_env.youtube.channel_lookups == []
    assert FakeYouTube.video_of(UC_B) not in _written_ids(scan_env)
    assert not _ids_path(scan_env.corpus).exists()


def test_cmd_scan_first_run_records_id_and_second_run_with_same_id_is_silent(scan_env, caplog):
    # Arrange
    scan_env.youtube = FakeYouTube({"creator": UC_A})

    # Act: run 1 learns the id.
    vi.cmd_scan(_scan_args(), _scan_config())
    learned = json.loads(_ids_path(scan_env.corpus).read_text(encoding="utf-8"))
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert learned["chan"]["channel_id"] == UC_A
    assert learned["chan"]["title"] == "Title A"
    assert learned["chan"]["first_seen"] == datetime.now(UTC).strftime("%Y-%m-%d")
    assert not [w for w in _warnings(caplog) if UC_A in w]


def test_cmd_scan_dry_run_writes_no_state_file(scan_env):
    # Arrange
    scan_env.youtube = FakeYouTube({"creator": UC_A})

    # Act
    vi.cmd_scan(_scan_args(dry_run=True), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    assert not _ids_path(scan_env.corpus).exists()


def test_cmd_scan_dry_run_still_warns_on_mismatch_and_leaves_state_file_untouched(scan_env, caplog):
    # Arrange
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A)})
    before = _ids_path(scan_env.corpus).read_bytes()
    scan_env.youtube = FakeYouTube({"creator": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(dry_run=True), _scan_config())

    # Assert
    assert len([w for w in _warnings(caplog) if UC_B in w]) == 1
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    assert _ids_path(scan_env.corpus).read_bytes() == before


@pytest.mark.parametrize(
    "answer",
    [None, vi.HttpError(MagicMock(status=404, reason="Not Found"), b"{}", uri="https://example.invalid/channels")],
    ids=["no-items", "http-404"],
)
def test_cmd_scan_handle_gone_with_known_id_fetches_known_id_and_warns_once(scan_env, caplog, answer):
    # Arrange
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A)})
    scan_env.youtube = FakeYouTube({"creator": answer})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    gone = [w for w in _warnings(caplog) if "no longer resolves" in w]
    assert len(gone) == 1
    assert "chan" in gone[0] and UC_A in gone[0]


def test_cmd_scan_existing_learned_entry_is_never_rewritten(scan_env):
    # Arrange: a mismatch run must not "correct" the learned entry.
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A, "Original Creator")})
    before = _ids_path(scan_env.corpus).read_bytes()
    scan_env.youtube = FakeYouTube({"creator": UC_B})

    # Act
    vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert _ids_path(scan_env.corpus).read_bytes() == before


def test_cmd_scan_new_channel_is_added_beside_existing_entries(scan_env):
    # Arrange
    _seed_ids(scan_env.corpus, {"other": _learned(UC_C, "Other")})
    scan_env.youtube = FakeYouTube({"creator": UC_A})

    # Act
    vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    data = json.loads(_ids_path(scan_env.corpus).read_text(encoding="utf-8"))
    assert data["other"] == _learned(UC_C, "Other")
    assert data["chan"]["channel_id"] == UC_A


@pytest.mark.parametrize("content", ["{not json", "[1, 2, 3]", "\xff\xfe garbage"], ids=["json", "list", "bytes"])
def test_cmd_scan_corrupt_state_file_warns_once_behaves_as_today_and_is_left_byte_identical(scan_env, caplog, content):
    # Arrange: two channels, so a per-channel warning would show up twice.
    path = _ids_path(scan_env.corpus)
    path.parent.mkdir(parents=True)
    path.write_bytes(content.encode("latin-1"))
    before = path.read_bytes()
    scan_env.youtube = FakeYouTube({"creator": UC_A, "second": UC_C})
    config = {"channels": [{"name": "chan", "url": HANDLE_URL}, {"name": "two", "url": "https://youtube.com/@second"}]}

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), config)

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A), _uploads(UC_C)]
    assert len([w for w in _warnings(caplog) if "ids.json" in w]) == 1
    assert path.read_bytes() == before


def test_cmd_scan_quota_stop_at_lookup_with_known_id_propagates(scan_env):
    # Arrange
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A)})
    body = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}}).encode("utf-8")
    quota = vi.HttpError(MagicMock(status=403, reason="Forbidden"), body, uri="https://example.invalid/channels")
    scan_env.youtube = FakeYouTube({"creator": quota})

    # Act / Assert
    with pytest.raises(vi.HttpError):
        vi.cmd_scan(_scan_args(), _scan_config())
    assert scan_env.youtube.playlists_fetched == []


# ---------------------------------------------------------------------------
# render_headline_digest
# ---------------------------------------------------------------------------


def _digest_config(tmp_path) -> dict:
    return {
        "output_dir": str(tmp_path),
        "channels": [{"name": "chan", "url": HANDLE_URL, "enabled": False, "headline_digest": True}],
    }


def test_render_headline_digest_mismatched_handle_renders_known_channel_not_stranger(monkeypatch, tmp_path, caplog):
    # Arrange
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _vid: False)
    _seed_ids(tmp_path, {"chan": _learned(UC_A, "Original Creator")})
    youtube = FakeYouTube({"creator": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        rendered = vi.render_headline_digest(youtube, _digest_config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert [v["video_id"] for v in rendered] == [FakeYouTube.video_of(UC_A)]
    assert youtube.playlists_fetched == [_uploads(UC_A)]
    assert len([w for w in _warnings(caplog) if UC_B in w]) == 1


def test_render_headline_digest_known_id_after_failed_lookup_is_not_a_failed_channel(monkeypatch, tmp_path):
    # Arrange: the only channel's lookup fails; with a known id it must still render, not raise all-failed.
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _vid: False)
    _seed_ids(tmp_path, {"chan": _learned(UC_A)})
    error = vi.HttpError(MagicMock(status=500, reason="Error"), b"{}", uri="https://example.invalid/channels")
    youtube = FakeYouTube({"creator": error})

    # Act
    rendered = vi.render_headline_digest(youtube, _digest_config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert [v["video_id"] for v in rendered] == [FakeYouTube.video_of(UC_A)]


def test_render_headline_digest_first_run_records_id_and_dry_run_does_not(monkeypatch, tmp_path):
    # Arrange
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _vid: False)
    youtube = FakeYouTube({"creator": UC_A})

    # Act
    vi.render_headline_digest(youtube, _digest_config(tmp_path), tmp_path, dry_run=True)
    after_dry_run = _ids_path(tmp_path).exists()
    vi.render_headline_digest(youtube, _digest_config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert after_dry_run is False
    assert json.loads(_ids_path(tmp_path).read_text(encoding="utf-8"))["chan"]["channel_id"] == UC_A


# ---------------------------------------------------------------------------
# match_configured_channel (manual --url routing)
# ---------------------------------------------------------------------------


def _matcher_config() -> dict:
    return {
        "channels": [
            {"name": "first", "url": "https://youtube.com/@first"},
            {"name": "second", "url": "https://youtube.com/@second"},
        ]
    }


def test_match_configured_channel_learned_ids_make_no_channel_lookup(tmp_path):
    # Arrange
    _seed_ids(tmp_path, {"first": _learned(UC_A), "second": _learned(UC_B)})
    youtube = FakeYouTube({"first": UC_C, "second": UC_C})

    # Act
    name = vi.match_configured_channel(youtube, _matcher_config(), UC_B, output_dir=tmp_path)

    # Assert
    assert name == "second"
    assert youtube.channel_lookups == []


def test_match_configured_channel_unknown_id_resolves_live_and_is_recorded(tmp_path):
    # Arrange
    _seed_ids(tmp_path, {"first": _learned(UC_A)})
    youtube = FakeYouTube({"second": UC_B})

    # Act
    name = vi.match_configured_channel(youtube, _matcher_config(), UC_B, output_dir=tmp_path)

    # Assert
    assert name == "second"
    assert [c["forHandle"] for c in youtube.channel_lookups] == ["second"]
    assert json.loads(_ids_path(tmp_path).read_text(encoding="utf-8"))["second"]["channel_id"] == UC_B


def test_match_configured_channel_explicit_channel_id_needs_no_lookup(tmp_path):
    # Arrange
    config = {"channels": [{"name": "first", "url": "https://youtube.com/@first", "channel_id": UC_A}]}
    youtube = FakeYouTube({"first": UC_C})

    # Act
    name = vi.match_configured_channel(youtube, config, UC_A, output_dir=tmp_path)

    # Assert
    assert name == "first"
    assert youtube.channel_lookups == []
    assert not _ids_path(tmp_path).exists()


class _SnippetYouTube:
    """Answers the manual commands' one `videos().list` snippet lookup."""

    def __init__(self, channel_id: str):
        self._channel_id = channel_id

    def videos(self):
        return self

    def list(self, **_kw):
        return self

    def execute(self):
        snippet = {"title": "A Talk", "publishedAt": "2026-01-01T00:00:00Z", "channelId": self._channel_id}
        return {"items": [{"snippet": snippet | {"channelTitle": "Someone Else"}}]}


@pytest.mark.parametrize("command", ["transcript", "mindmap", "process"])
def test_manual_url_commands_route_by_learned_id_without_a_channel_lookup(command, monkeypatch, tmp_path):
    """Each real `--url` caller hands the corpus to the matcher, so a learned id is used and no lookup is made."""
    # Arrange
    _seed_ids(tmp_path, {"first": _learned(UC_A), "second": _learned(UC_B)})
    seen: list[str] = []

    def no_lookup(*_a, **_kw):
        pytest.fail("a channel with a learned id must not be looked up")

    def record(*a, **kw):
        seen.extend(str(x) for x in a)
        seen.extend(str(v) for v in kw.values())
        return ("2026-01-01-a-talk", "done")

    monkeypatch.setattr(vi, "get_channel_id", no_lookup)
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: _SnippetYouTube(UC_B))
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "create_client", lambda *_a, **_kw: object())
    monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
    monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_kw: tmp_path)
    monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _v: False)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _v: 600)
    monkeypatch.setattr(vi, "require_channels_config", lambda _c: None)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake-yt-key")
    for name in ("process_transcript", "process_mindmap", "process_concepts"):
        monkeypatch.setattr(vi, name, record)
    args = SimpleNamespace(
        url="https://www.youtube.com/watch?v=abcdefghijk",
        file=None,
        channel=None,
        title=None,
        date=None,
        start=None,
        end=None,
        force=False,
        prompt=None,
        model=None,
        video_id=None,
        media_resolution="low",
        chunk_minutes=None,
        transcript_source=None,
        topic=None,
    )
    fn = {"transcript": vi.cmd_transcript, "mindmap": vi.cmd_mindmap, "process": vi.cmd_process}[command]

    # Act
    with contextlib.suppress(SystemExit):
        fn(args, _matcher_config())

    # Assert
    assert seen, f"{command} never reached a processing step"
    assert any(v == "second" or v.endswith("second") for v in seen), seen
    assert not any("someone" in v.lower() for v in seen), seen
