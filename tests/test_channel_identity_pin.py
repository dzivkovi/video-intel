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
        self.dead: dict[str, Exception] = {}
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
            if playlistId in self.dead:
                raise self.dead[playlistId]
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


def _learned(channel_id: str, title: str = "Known", url: str | None = None) -> dict:
    entry = {"channel_id": channel_id, "title": title, "first_seen": "2026-01-01"}
    if url is not None:
        entry["url"] = url
    return entry


@pytest.fixture(autouse=True)
def _reset_warn_once():
    vi._CHANNEL_IDS_WARNED.clear()
    yield
    vi._CHANNEL_IDS_WARNED.clear()


def _messages(caplog, level) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


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
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())
    learn_lines = [m for m in _messages(caplog, logging.INFO) if "Learned channel id" in m]
    caplog.clear()
    learned = json.loads(_ids_path(scan_env.corpus).read_text(encoding="utf-8"))
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert learned["chan"]["channel_id"] == UC_A
    assert learned["chan"]["title"] == "Title A"
    assert learned["chan"]["first_seen"] == datetime.now(UTC).strftime("%Y-%m-%d")
    assert not [w for w in _warnings(caplog) if UC_A in w]
    assert learned["chan"]["url"] == HANDLE_URL
    assert len(learn_lines) == 1
    assert "chan" in learn_lines[0] and UC_A in learn_lines[0] and "Title A" in learn_lines[0]


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


def test_cmd_scan_handle_returns_no_channel_with_known_id_fetches_known_id_and_warns_with_remedy(scan_env, caplog):
    # Arrange
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A)})
    scan_env.youtube = FakeYouTube({"creator": None})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    gone = [w for w in _warnings(caplog) if "no longer resolves" in w]
    assert len(gone) == 1
    assert "chan" in gone[0] and UC_A in gone[0] and "channel_id" in gone[0]


@pytest.mark.parametrize(
    "error",
    [
        vi.HttpError(MagicMock(status=500, reason="Error"), b"{}", uri="https://example.invalid/channels"),
        TimeoutError("read timed out"),
    ],
    ids=["http-500", "timeout"],
)
def test_cmd_scan_lookup_raises_with_known_id_fetches_known_id_and_says_could_not_check(scan_env, caplog, error):
    # Arrange
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A)})
    scan_env.youtube = FakeYouTube({"creator": error})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    checked = [w for w in _warnings(caplog) if "could not be checked" in w]
    assert len(checked) == 1
    assert UC_A in checked[0]
    assert "changed owner" not in checked[0] and "ids.json" not in checked[0]
    assert not [w for w in _warnings(caplog) if "no longer resolves" in w]


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


def _http(status: int, reason: str | None = None) -> vi.HttpError:
    body = {"error": {"errors": [{"reason": reason}]}} if reason else {}
    return vi.HttpError(
        MagicMock(status=status, reason="Error"), json.dumps(body).encode("utf-8"), uri="https://example.invalid/x"
    )


def test_cmd_scan_dead_known_id_warns_records_error_and_next_channel_is_processed(scan_env, caplog):
    # Arrange: the known channel was deleted (its uploads playlist is gone); a healthy channel follows.
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A)})
    scan_env.youtube = FakeYouTube({"creator": None, "second": UC_C})
    scan_env.youtube.dead[_uploads(UC_A)] = _http(404, "playlistNotFound")
    config = {"channels": [{"name": "chan", "url": HANDLE_URL}, {"name": "two", "url": "https://youtube.com/@second"}]}

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), config)

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A), _uploads(UC_C)]
    assert FakeYouTube.video_of(UC_C) in _written_ids(scan_env)
    fetch = [w for w in _warnings(caplog) if "Could not fetch videos" in w]
    assert len(fetch) == 1
    assert "chan" in fetch[0] and "playlistNotFound" in fetch[0] and "config.yaml" in fetch[0]
    assert any(w.startswith("  [chan] chan: error: HTTP 404") for w in _warnings(caplog))
    assert any(r.getMessage() == "Done." for r in caplog.records)


def test_cmd_scan_quota_stop_at_fetch_still_propagates(scan_env):
    # Arrange
    scan_env.youtube = FakeYouTube({"creator": UC_A})
    scan_env.youtube.dead[_uploads(UC_A)] = _http(403, "quotaExceeded")

    # Act / Assert
    with pytest.raises(vi.HttpError):
        vi.cmd_scan(_scan_args(), _scan_config())


def _conflict_warnings(caplog) -> list[str]:
    return [w for w in _warnings(caplog) if "was learned before under" in w]


def test_cmd_scan_renamed_entry_whose_handle_moved_learns_live_id_and_warns_once(scan_env, caplog):
    # Arrange: the entry was learned as "old"; the config now calls it "chan"; the handle resolves elsewhere.
    _seed_ids(scan_env.corpus, {"old": _learned(UC_A, "Original Creator", url=HANDLE_URL)})
    old_entry = json.loads(_ids_path(scan_env.corpus).read_text(encoding="utf-8"))["old"]
    scan_env.youtube = FakeYouTube({"creator": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert: a rename starts a new pin; the warning is the signal and names the old pin.
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_B)]
    data = json.loads(_ids_path(scan_env.corpus).read_text(encoding="utf-8"))
    assert data["old"] == old_entry
    assert data["chan"]["channel_id"] == UC_B
    conflict = _conflict_warnings(caplog)
    assert len(conflict) == 1
    for needle in ("chan", HANDLE_URL, "'old'", UC_A, "Original Creator", UC_B, "Title B", f"channel_id: {UC_A}"):
        assert needle in conflict[0]


def test_cmd_scan_renamed_entry_same_channel_learns_under_new_name_without_warning(scan_env, caplog):
    # Arrange
    _seed_ids(scan_env.corpus, {"old": _learned(UC_A, url=HANDLE_URL)})
    scan_env.youtube = FakeYouTube({"creator": UC_A})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    data = json.loads(_ids_path(scan_env.corpus).read_text(encoding="utf-8"))
    assert data["chan"]["channel_id"] == UC_A
    assert data["old"] == _learned(UC_A, url=HANDLE_URL)
    assert _conflict_warnings(caplog) == []


def test_cmd_scan_alias_entries_at_old_url_never_assign_their_id_to_the_new_owner(scan_env, caplog):
    # Arrange: two stale entries for creator A at the old url, A's current entry at a new url,
    # and a new entry B at the old url, which B now owns.
    seeded = {
        "a_alias1": _learned(UC_A, "Creator A", url=HANDLE_URL),
        "a_alias2": _learned(UC_A, "Creator A", url=HANDLE_URL),
        "creator_a": _learned(UC_A, "Creator A", url="https://www.youtube.com/@a_new"),
    }
    _seed_ids(scan_env.corpus, seeded)
    scan_env.youtube = FakeYouTube({"creator": UC_B, "a_new": UC_A})
    config = {
        "channels": [
            {"name": "creator_a", "url": "https://www.youtube.com/@a_new"},
            {"name": "creator_b", "url": HANDLE_URL},
        ]
    }

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), config)

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A), _uploads(UC_B)]
    assert FakeYouTube.video_of(UC_B) in _written_ids(scan_env)
    data = json.loads(_ids_path(scan_env.corpus).read_text(encoding="utf-8"))
    assert {k: data[k] for k in seeded} == seeded
    assert data["creator_b"]["channel_id"] == UC_B
    conflict = _conflict_warnings(caplog)
    assert len(conflict) == 1
    assert "creator_b" in conflict[0] and "'a_alias1'" in conflict[0] and "'a_alias2'" in conflict[0]


def test_cmd_scan_renamed_entry_on_dry_run_warns_and_writes_nothing(scan_env, caplog):
    # Arrange
    _seed_ids(scan_env.corpus, {"old": _learned(UC_A, url=HANDLE_URL)})
    before = _ids_path(scan_env.corpus).read_bytes()
    scan_env.youtube = FakeYouTube({"creator": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(dry_run=True), _scan_config())

    # Assert
    assert len(_conflict_warnings(caplog)) == 1
    assert _ids_path(scan_env.corpus).read_bytes() == before


def test_cmd_scan_shared_name_skips_state_file_resolves_live_and_warns_once(scan_env, caplog):
    # Arrange: two entries named "chan"; a learned id under that name must not be applied to either.
    _seed_ids(scan_env.corpus, {"chan": _learned(UC_A)})
    before = _ids_path(scan_env.corpus).read_bytes()
    scan_env.youtube = FakeYouTube({"creator": UC_B, "second": UC_C})
    config = {"channels": [{"name": "chan", "url": HANDLE_URL}, {"name": "chan", "url": "https://youtube.com/@second"}]}

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), config)

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_B), _uploads(UC_C)]
    shared = [w for w in _warnings(caplog) if "share this name" in w]
    assert len(shared) == 1
    assert _ids_path(scan_env.corpus).read_bytes() == before


@pytest.mark.parametrize("url", [UC_A, f"https://www.youtube.com/channel/{UC_A}", f"youtube.com/channel/{UC_A}/videos"])
def test_cmd_scan_url_naming_a_channel_id_makes_no_lookup_and_no_state_access(scan_env, url):
    # Arrange
    scan_env.youtube = FakeYouTube({})

    # Act
    vi.cmd_scan(_scan_args(), {"channels": [{"name": "chan", "url": url}]})

    # Assert
    assert scan_env.youtube.channel_lookups == []
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    assert not _ids_path(scan_env.corpus).exists()


@pytest.mark.parametrize(
    "entry",
    [{"title": "x"}, UC_A, {"channel_id": "UCshort", "title": "x"}, {"channel_id": 123}],
    ids=["no-id", "bare-string", "short-id", "int-id"],
)
def test_cmd_scan_unusable_learned_entry_warns_resolves_live_and_is_left_byte_identical(scan_env, caplog, entry):
    # Arrange
    _seed_ids(scan_env.corpus, {"chan": entry})
    before = _ids_path(scan_env.corpus).read_bytes()
    scan_env.youtube = FakeYouTube({"creator": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_B)]
    assert len([w for w in _warnings(caplog) if "no valid channel_id" in w]) == 1
    assert _ids_path(scan_env.corpus).read_bytes() == before


@pytest.mark.parametrize("bad", ["UCshort", 123], ids=["short", "int"])
def test_cmd_scan_malformed_explicit_channel_id_makes_no_lookup_no_fetch_and_one_error(scan_env, caplog, bad):
    # Arrange
    scan_env.youtube = FakeYouTube({"creator": UC_A})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config(channel_id=bad))

    # Assert
    assert scan_env.youtube.channel_lookups == []
    assert scan_env.youtube.playlists_fetched == []
    errors = [m for m in _messages(caplog, logging.ERROR) if "channel_id" in m]
    assert len(errors) == 1
    assert repr(bad) in errors[0]


def test_cmd_scan_unreadable_state_file_warns_once_resolves_live_and_writes_nothing(scan_env, caplog):
    # Arrange: a directory where the file should be makes the read raise OSError.
    _ids_path(scan_env.corpus).mkdir(parents=True)
    scan_env.youtube = FakeYouTube({"creator": UC_A})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    assert len([w for w in _warnings(caplog) if "Channel id state" in w and "is unreadable" in w]) == 1
    assert not [w for w in _warnings(caplog) if "Could not record" in w]
    assert _ids_path(scan_env.corpus).is_dir()


def test_cmd_scan_failed_state_write_warns_continues_and_leaves_no_partial_file(scan_env, caplog, monkeypatch):
    # Arrange
    real_replace = vi.os.replace

    def failing_replace(src, dst):
        if str(dst).endswith("ids.json"):
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(vi.os, "replace", failing_replace)
    scan_env.youtube = FakeYouTube({"creator": UC_A})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config())

    # Assert
    assert scan_env.youtube.playlists_fetched == [_uploads(UC_A)]
    assert len([w for w in _warnings(caplog) if "Could not record channel id" in w]) == 1
    assert not _ids_path(scan_env.corpus).exists()
    assert list(_ids_path(scan_env.corpus).parent.iterdir()) == []
    assert any(r.getMessage() == "Done." for r in caplog.records)


def test_cmd_scan_log_line_falls_back_to_url_when_no_title(scan_env, caplog):
    # Arrange: an explicit pin makes no lookup, so there is no title.
    scan_env.youtube = FakeYouTube({})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.cmd_scan(_scan_args(), _scan_config(channel_id=UC_A))

    # Assert
    assert f"[chan] {HANDLE_URL}" in _messages(caplog, logging.INFO)


def test_record_learned_channel_id_never_rewrites_an_existing_entry(tmp_path):
    # Arrange
    _seed_ids(tmp_path, {"chan": _learned(UC_A)})
    before = _ids_path(tmp_path).read_bytes()

    # Act
    vi._record_learned_channel_id(tmp_path, "chan", UC_B, "Stranger", HANDLE_URL)

    # Assert
    assert _ids_path(tmp_path).read_bytes() == before


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


def test_render_headline_digest_first_run_records_id_and_dry_run_does_not(monkeypatch, tmp_path, caplog):
    # Arrange
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _vid: False)
    youtube = FakeYouTube({"creator": UC_A})

    # Act
    vi.render_headline_digest(youtube, _digest_config(tmp_path), tmp_path, dry_run=True)
    after_dry_run = _ids_path(tmp_path).exists()
    with caplog.at_level(logging.INFO, logger="video_intel"):
        vi.render_headline_digest(youtube, _digest_config(tmp_path), tmp_path, dry_run=False)

    # Assert
    assert after_dry_run is False
    assert len([m for m in _messages(caplog, logging.INFO) if "Learned channel id" in m and UC_A in m]) == 1
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


def test_match_configured_channel_unknown_id_resolves_live_and_is_recorded(tmp_path, caplog):
    # Arrange
    _seed_ids(tmp_path, {"first": _learned(UC_A)})
    youtube = FakeYouTube({"second": UC_B})

    # Act
    with caplog.at_level(logging.INFO, logger="video_intel"):
        name = vi.match_configured_channel(youtube, _matcher_config(), UC_B, output_dir=tmp_path)

    # Assert
    assert name == "second"
    assert [c["forHandle"] for c in youtube.channel_lookups] == ["second"]
    assert len([m for m in _messages(caplog, logging.INFO) if "Learned channel id" in m and UC_B in m]) == 1
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


def test_match_configured_channel_entries_sharing_a_url_but_pinned_differently_do_not_share_a_cache_slot():
    # Arrange: the first entry is pinned to UC_A; the second (same url, no pin) resolves live to UC_C.
    url = "https://youtube.com/@shared"
    config = {"channels": [{"name": "pinned", "url": url, "channel_id": UC_A}, {"name": "live", "url": url}]}
    youtube = FakeYouTube({"shared": UC_C})

    # Act
    name = vi.match_configured_channel(youtube, config, UC_C)

    # Assert
    assert name == "live"


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
