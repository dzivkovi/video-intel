"""Tests for YouTube Shorts classification, scan-time filter, and the
prune-shorts subcommand.

Covers all 5 implementation units of
docs/plans/2026-04-24-002-feat-skip-shorts-and-prune-plan.md. Single test
file per the codebase precedent in tests/test_video_id_dedup.py (helpers
plus cmd integration in one place).
"""

import json
from pathlib import Path

import httpx
import pytest

import video_intel as vi

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """Reset module-level caches between tests so monkeypatched returns are
    not masked by prior call results."""
    vi._is_youtube_short_url.cache_clear()
    vi._invalidate_video_id_cache()
    yield
    vi._is_youtube_short_url.cache_clear()
    vi._invalidate_video_id_cache()


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Zero out the retry sleep so transient-error tests run instantly."""
    monkeypatch.setattr(vi, "_SHORT_URL_RETRY_DELAY", 0)


def _write_meta(channel_dir: Path, prefix: str, data: dict) -> Path:
    """Write a meta.json sidecar and return its path."""
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = channel_dir / f"{prefix}.meta.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _touch(path: Path, content: str = "x") -> None:
    """Create a file with the given content, ensuring parent dirs exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit 1: _parse_iso8601_duration
# ---------------------------------------------------------------------------


class TestParseIsoDuration:
    def test_parses_seconds_only(self):
        assert vi._parse_iso8601_duration("PT47S") == 47

    def test_parses_minutes_only(self):
        assert vi._parse_iso8601_duration("PT12M") == 720

    def test_parses_hours_minutes_seconds(self):
        assert vi._parse_iso8601_duration("PT1H4M3S") == 3843

    def test_parses_minute_seconds(self):
        assert vi._parse_iso8601_duration("PT1M30S") == 90

    def test_parses_one_hour(self):
        assert vi._parse_iso8601_duration("PT1H") == 3600

    def test_returns_none_for_garbage(self):
        assert vi._parse_iso8601_duration("garbage") is None

    def test_returns_none_for_empty_string(self):
        assert vi._parse_iso8601_duration("") is None

    def test_returns_none_for_none(self):
        assert vi._parse_iso8601_duration(None) is None


# ---------------------------------------------------------------------------
# Unit 1: _is_youtube_short_url (HEAD redirect check with bounded retry)
# ---------------------------------------------------------------------------


class TestIsYoutubeShortUrl:
    def _patch_head(self, monkeypatch, responses):
        """Install a fake httpx.head that yields the given responses sequentially.

        responses: list of (status_code,) tuples, or callables that raise.
        """
        calls = {"n": 0}

        def fake_head(url, **kwargs):
            i = calls["n"]
            calls["n"] += 1
            entry = responses[min(i, len(responses) - 1)]
            if callable(entry):
                entry()
            return httpx.Response(entry)

        monkeypatch.setattr(vi.httpx, "head", fake_head)
        return calls

    def test_returns_true_when_status_200(self, monkeypatch):
        self._patch_head(monkeypatch, [200])
        assert vi._is_youtube_short_url("abc") is True

    def test_returns_false_when_status_303(self, monkeypatch):
        # YouTube empirically returns 303 (not 302) for non-Shorts.
        self._patch_head(monkeypatch, [303])
        assert vi._is_youtube_short_url("abc") is False

    def test_returns_false_when_status_302(self, monkeypatch):
        # Generic non-200 still classifies as long-form.
        self._patch_head(monkeypatch, [302])
        assert vi._is_youtube_short_url("abc") is False

    def test_returns_true_after_retry_when_first_call_returns_503(self, monkeypatch):
        calls = self._patch_head(monkeypatch, [503, 200])
        assert vi._is_youtube_short_url("abc") is True
        assert calls["n"] == 2  # one retry consumed

    def test_returns_false_after_retry_exhausted_on_persistent_503(self, monkeypatch):
        calls = self._patch_head(monkeypatch, [503, 503])
        assert vi._is_youtube_short_url("abc") is False
        assert calls["n"] == 2  # one retry, then give up

    def test_returns_false_on_connection_error(self, monkeypatch):
        def raise_connect_error():
            raise httpx.ConnectError("simulated connection failure")

        self._patch_head(monkeypatch, [raise_connect_error, raise_connect_error])
        assert vi._is_youtube_short_url("abc") is False

    def test_returns_true_after_retry_on_initial_timeout(self, monkeypatch):
        def raise_timeout():
            raise httpx.ConnectTimeout("simulated timeout")

        responses = [raise_timeout, 200]
        calls = self._patch_head(monkeypatch, responses)
        assert vi._is_youtube_short_url("abc") is True
        assert calls["n"] == 2

    def test_caches_result_per_video_id(self, monkeypatch):
        calls = self._patch_head(monkeypatch, [200, 303])
        assert vi._is_youtube_short_url("abc") is True
        assert vi._is_youtube_short_url("abc") is True  # cached
        assert calls["n"] == 1

    def test_integration_against_mock_transport_returns_200(self, monkeypatch):
        """Validate the real httpx call shape via MockTransport — the only
        test that exercises the full request/response round-trip rather
        than monkeypatching httpx.head."""

        def handler(request):
            assert request.method == "HEAD"
            assert "youtube.com/shorts/" in str(request.url)
            return httpx.Response(200)

        # Wrap httpx.head to use MockTransport for this test only.
        transport = httpx.MockTransport(handler)

        def fake_head(url, **kwargs):
            with httpx.Client(transport=transport) as client:
                return client.request("HEAD", url, **kwargs)

        monkeypatch.setattr(vi.httpx, "head", fake_head)
        assert vi._is_youtube_short_url("abc") is True

    def test_integration_against_mock_transport_returns_303_with_location(self, monkeypatch):
        """Empirically YouTube returns 303 with Location header for non-Shorts."""

        def handler(request):
            return httpx.Response(303, headers={"Location": "https://www.youtube.com/watch?v=abc"})

        transport = httpx.MockTransport(handler)

        def fake_head(url, **kwargs):
            with httpx.Client(transport=transport) as client:
                return client.request("HEAD", url, **kwargs)

        monkeypatch.setattr(vi.httpx, "head", fake_head)
        assert vi._is_youtube_short_url("xyz") is False


# ---------------------------------------------------------------------------
# Unit 1: is_short (combines duration + url check)
# ---------------------------------------------------------------------------


class TestIsShort:
    def test_short_when_duration_under_60s(self, monkeypatch):
        # url check should not be invoked for sub-60s durations
        def boom(_video_id):
            raise AssertionError("redirect check should not fire under 60s")

        monkeypatch.setattr(vi, "_is_youtube_short_url", boom)
        assert vi.is_short("abc", "PT47S") is True

    def test_long_when_duration_over_60s_and_url_says_long(self, monkeypatch):
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _v: False)
        assert vi.is_short("abc", "PT12M30S") is False

    def test_short_when_raised_cap_duration_and_url_says_short(self, monkeypatch):
        # 90-second video that YouTube classifies as a Short via /shorts/ URL
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _v: True)
        assert vi.is_short("abc", "PT1M30S") is True

    def test_long_when_raised_cap_duration_and_url_says_long(self, monkeypatch):
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _v: False)
        assert vi.is_short("abc", "PT1M30S") is False

    def test_falls_back_to_url_check_when_duration_is_none(self, monkeypatch):
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _v: True)
        assert vi.is_short("abc", None) is True

    def test_falls_back_to_url_check_when_duration_unparseable(self, monkeypatch):
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _v: True)
        assert vi.is_short("abc", "BOGUS") is True

    def test_returns_long_when_url_check_raises(self, monkeypatch):
        # D8 fail-safe: any classification ambiguity defaults to long-form
        def boom(_v):
            raise httpx.HTTPError("simulated")

        monkeypatch.setattr(vi, "_is_youtube_short_url", boom)
        assert vi.is_short("abc", "PT5M") is False

    def test_returns_long_when_video_id_missing(self, monkeypatch):
        # Empty video_id shouldn't trigger a URL fetch
        def boom(_v):
            raise AssertionError("redirect check should not fire for empty video_id")

        monkeypatch.setattr(vi, "_is_youtube_short_url", boom)
        assert vi.is_short("", "PT5M") is False
        assert vi.is_short(None, "PT5M") is False


# ---------------------------------------------------------------------------
# Unit 2: enrich_with_durations (batched videos.list lookup)
# ---------------------------------------------------------------------------


class _FakeYoutube:
    """Minimal fake mirroring youtube.videos().list(id=..., part=...).execute()."""

    def __init__(self, response_items: list[dict]):
        self._items = response_items
        self.calls: list[str] = []  # list of comma-joined id strings per call

    def videos(self):
        return self

    def list(self, *, id: str, part: str):
        self.calls.append(id)
        self._next_part = part
        self._next_ids = id.split(",")
        return self

    def execute(self):
        items = [item for item in self._items if item["id"] in self._next_ids]
        return {"items": items}


class TestEnrichWithDurations:
    def test_returns_dict_for_three_video_ids(self):
        items = [
            {"id": "abc", "contentDetails": {"duration": "PT47S"}},
            {"id": "def", "contentDetails": {"duration": "PT5M"}},
            {"id": "ghi", "contentDetails": {"duration": "PT1H"}},
        ]
        yt = _FakeYoutube(items)
        result = vi.enrich_with_durations(yt, ["abc", "def", "ghi"])
        assert result == {"abc": "PT47S", "def": "PT5M", "ghi": "PT1H"}
        assert len(yt.calls) == 1

    def test_batches_into_groups_of_fifty(self):
        ids = [f"v{i:03d}" for i in range(51)]
        items = [{"id": vid, "contentDetails": {"duration": "PT5M"}} for vid in ids]
        yt = _FakeYoutube(items)
        result = vi.enrich_with_durations(yt, ids)
        assert len(result) == 51
        assert all(result[vid] == "PT5M" for vid in ids)
        assert len(yt.calls) == 2
        # First call should have 50 ids, second should have 1
        assert len(yt.calls[0].split(",")) == 50
        assert len(yt.calls[1].split(",")) == 1

    def test_exactly_fifty_ids_one_call(self):
        ids = [f"v{i:03d}" for i in range(50)]
        items = [{"id": vid, "contentDetails": {"duration": "PT5M"}} for vid in ids]
        yt = _FakeYoutube(items)
        result = vi.enrich_with_durations(yt, ids)
        assert len(result) == 50
        assert len(yt.calls) == 1

    def test_empty_input_returns_empty_dict_no_api_call(self):
        yt = _FakeYoutube([])
        result = vi.enrich_with_durations(yt, [])
        assert result == {}
        assert yt.calls == []

    def test_video_id_not_in_response_maps_to_none(self):
        # Only "abc" is in the response; "missing" requested but not returned
        # (deleted, members-only, or other access-denied case)
        items = [{"id": "abc", "contentDetails": {"duration": "PT47S"}}]
        yt = _FakeYoutube(items)
        result = vi.enrich_with_durations(yt, ["abc", "missing"])
        assert result == {"abc": "PT47S", "missing": None}

    def test_response_item_missing_content_details_maps_to_none(self):
        items = [{"id": "abc"}]  # No contentDetails key at all
        yt = _FakeYoutube(items)
        result = vi.enrich_with_durations(yt, ["abc"])
        assert result == {"abc": None}

    def test_response_item_missing_duration_maps_to_none(self):
        items = [{"id": "abc", "contentDetails": {}}]  # contentDetails present but no duration
        yt = _FakeYoutube(items)
        result = vi.enrich_with_durations(yt, ["abc"])
        assert result == {"abc": None}


# ---------------------------------------------------------------------------
# Unit 3: cmd_scan integration with skip_shorts filter
# ---------------------------------------------------------------------------


def _scan_args(**overrides):
    """Build a Namespace mirroring the cmd_scan argparse contract."""
    from argparse import Namespace

    base = {
        "dry_run": False,
        "channel": None,
        "force": False,
        "since": None,
        "model": None,
    }
    base.update(overrides)
    return Namespace(**base)


def _scan_setup(monkeypatch, videos, durations=None, raise_on_enrich=None):
    """Common cmd_scan mocking. Returns a dict that test bodies can read to
    inspect which videos hit process_mindmap.

    durations: dict[video_id, duration_iso] — what enrich_with_durations returns.
        Defaults to all-None (treated as long-form by is_short fallback).
    raise_on_enrich: optional Exception instance to raise from enrich.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
    monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos))

    durations = durations or {}

    def fake_enrich(_yt, video_ids):
        if raise_on_enrich is not None:
            raise raise_on_enrich
        return dict.fromkeys(video_ids) | {k: v for k, v in durations.items() if k in video_ids}

    monkeypatch.setattr(vi, "enrich_with_durations", fake_enrich)
    # Default URL check returns False (long-form) so cmd_scan tests don't hit
    # real YouTube. Tests that want a "short via URL" decision pass durations
    # under 60s so the duration short-circuit fires before the URL check.
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

    captured = {"processed": []}

    def fake_process_mindmap(*args, **kwargs):
        # Real signature is (client, types, video, prompt_text, model, output_dir,
        # channel_name, *, prompt_name=, force=, ...). video is positional arg [2].
        video = args[2] if len(args) > 2 else kwargs.get("video")
        captured["processed"].append(video)
        return (video.get("video_id", "prefix"), "done")

    monkeypatch.setattr(vi, "process_mindmap", fake_process_mindmap)
    return captured


class TestCmdScanSkipShorts:
    def test_default_filters_shorts_before_process_mindmap(self, tmp_path, monkeypatch):
        """Default behavior (no skip_shorts in config) drops Shorts."""
        videos = [
            {"video_id": "short1", "title": "30s Short", "published": "2026-04-15"},
            {"video_id": "long1", "title": "10min Long-form", "published": "2026-04-15"},
        ]
        durations = {"short1": "PT30S", "long1": "PT10M"}
        captured = _scan_setup(monkeypatch, videos, durations=durations)

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch", "url": "https://example.com/ch"}]}
        vi.cmd_scan(_scan_args(), config)

        ids = [v["video_id"] for v in captured["processed"]]
        assert "short1" not in ids, "Short must be filtered out by default"
        assert "long1" in ids, "Long-form must still process"

    def test_skip_shorts_false_keeps_shorts(self, tmp_path, monkeypatch):
        videos = [
            {"video_id": "short1", "title": "30s Short", "published": "2026-04-15"},
            {"video_id": "long1", "title": "10min Long-form", "published": "2026-04-15"},
        ]
        durations = {"short1": "PT30S", "long1": "PT10M"}
        captured = _scan_setup(monkeypatch, videos, durations=durations)

        config = {
            "output_dir": str(tmp_path),
            "channels": [{"name": "ch", "url": "https://example.com/ch", "skip_shorts": False}],
        }
        vi.cmd_scan(_scan_args(), config)

        ids = [v["video_id"] for v in captured["processed"]]
        assert "short1" in ids, "Short must be kept when skip_shorts=false"
        assert "long1" in ids

    def test_quota_exceeded_skips_channel_continues_to_next(self, tmp_path, monkeypatch):
        videos = [{"video_id": "v1", "title": "a", "published": "2026-04-15"}]
        captured = _scan_setup(
            monkeypatch,
            videos,
            raise_on_enrich=RuntimeError("<HttpError 403 ... reason: quotaExceeded ...>"),
        )

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch", "url": "https://example.com/ch"}]}
        vi.cmd_scan(_scan_args(), config)

        # No video reached process_mindmap because the channel was aborted
        assert captured["processed"] == []

    def test_empty_video_list_skips_enrich_call(self, tmp_path, monkeypatch):
        enrich_calls = {"n": 0}

        def counting_enrich(_yt, video_ids):
            enrich_calls["n"] += 1
            return dict.fromkeys(video_ids)

        # Empty fetch → cmd_scan early-returns or doesn't call enrich
        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: [])
        monkeypatch.setattr(vi, "enrich_with_durations", counting_enrich)

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch", "url": "https://example.com/ch"}]}
        vi.cmd_scan(_scan_args(), config)

        assert enrich_calls["n"] == 0, "enrich_with_durations must not be called for empty video lists"

    def test_meta_json_records_duration_seconds_after_scan(self, tmp_path, monkeypatch):
        """Forward-fix: meta.json should persist duration_seconds so future
        prune-shorts runs avoid re-fetching from YouTube."""
        videos = [{"video_id": "long1", "title": "10min Long-form", "published": "2026-04-15"}]
        durations = {"long1": "PT10M"}
        _scan_setup(monkeypatch, videos, durations=durations)

        # Replace the fake process_mindmap with one that actually writes meta.json
        def real_process_mindmap(*args, **kwargs):
            # Signature: (client, types, video, prompt_text, model, output_dir, channel_name, *, ...)
            video = args[2] if len(args) > 2 else kwargs["video"]
            model = args[4] if len(args) > 4 else kwargs.get("model", "test-model")
            output_dir = args[5] if len(args) > 5 else kwargs["output_dir"]
            channel_name = args[6] if len(args) > 6 else kwargs["channel_name"]

            from datetime import UTC, datetime

            channel_dir = output_dir / channel_name
            channel_dir.mkdir(parents=True, exist_ok=True)
            prefix = f"{video['published']}-{video['title'].replace(' ', '-').lower()}"
            meta_fields = {
                "video_url": f"https://www.youtube.com/watch?v={video['video_id']}",
                "video_id": video["video_id"],
                "channel": channel_name,
                "title": video["title"],
                "published": video["published"],
                "processed": datetime.now(UTC).isoformat(),
                "model": model,
            }
            duration_seconds = vi._parse_iso8601_duration(video.get("duration_iso"))
            if duration_seconds is not None:
                meta_fields["duration_seconds"] = duration_seconds
            meta_path = channel_dir / f"{prefix}.meta.json"
            vi.update_meta(meta_path, meta_fields, "scan")
            return (prefix, "done")

        monkeypatch.setattr(vi, "process_mindmap", real_process_mindmap)

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch", "url": "https://example.com/ch"}]}
        vi.cmd_scan(_scan_args(), config)

        meta_files = list((tmp_path / "ch").glob("*.meta.json"))
        assert len(meta_files) == 1
        meta = json.loads(meta_files[0].read_text(encoding="utf-8"))
        assert meta.get("duration_seconds") == 600, f"duration_seconds missing: {meta}"


# ---------------------------------------------------------------------------
# Unit 4: cmd_prune_shorts subcommand
# ---------------------------------------------------------------------------


def _prune_args(**overrides):
    """Build a Namespace mirroring the cmd_prune_shorts argparse contract."""
    from argparse import Namespace

    base = {"channel": None, "apply": False}
    base.update(overrides)
    return Namespace(**base)


def _prune_setup(monkeypatch):
    """Common cmd_prune_shorts mocking. Stubs out the YouTube client lookup
    so legacy-meta enrichment paths can run without network."""
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    # No legacy metas in most tests → enrich never fires; install a stub that
    # returns all-None just in case (would treat unknown durations as long-form).
    monkeypatch.setattr(
        vi,
        "enrich_with_durations",
        lambda _yt, video_ids: dict.fromkeys(video_ids),
    )


def _make_short_artifacts(channel_dir: Path, prefix: str, video_id: str, duration_seconds: int = 30):
    """Create the four canonical Shorts artifacts plus a meta.json with cached
    duration_seconds so cmd_prune_shorts classifies via the duration short-circuit
    (no network calls required)."""
    _write_meta(
        channel_dir,
        prefix,
        {
            "video_id": video_id,
            "title": f"Short {video_id}",
            "published": "2026-04-15",
            "duration_seconds": duration_seconds,
        },
    )
    _touch(channel_dir / f"{prefix}.mindmap.md")
    _touch(channel_dir / f"{prefix}.transcript.md")
    _touch(channel_dir / f"{prefix}.concepts.json")


class TestPruneShorts:
    def test_dry_run_lists_short_does_not_delete(self, tmp_path, monkeypatch):
        _prune_setup(monkeypatch)
        ch = tmp_path / "ch1"
        _make_short_artifacts(ch, "2026-04-15-short1", "short1")

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1"), config)

        # All 4 artifacts must still exist (dry-run preview-only)
        assert (ch / "2026-04-15-short1.meta.json").exists()
        assert (ch / "2026-04-15-short1.mindmap.md").exists()
        assert (ch / "2026-04-15-short1.transcript.md").exists()
        assert (ch / "2026-04-15-short1.concepts.json").exists()

    def test_apply_deletes_four_artifacts(self, tmp_path, monkeypatch):
        _prune_setup(monkeypatch)
        ch = tmp_path / "ch1"
        _make_short_artifacts(ch, "2026-04-15-short1", "short1")

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        assert not (ch / "2026-04-15-short1.meta.json").exists()
        assert not (ch / "2026-04-15-short1.mindmap.md").exists()
        assert not (ch / "2026-04-15-short1.transcript.md").exists()
        assert not (ch / "2026-04-15-short1.concepts.json").exists()

    def test_apply_preserves_translate_bcs_sidecars(self, tmp_path, monkeypatch):
        """CRITICAL regression: translate_video.py produces .en.srt and
        .translate-bcs.txt siblings sharing the prefix. They MUST survive
        prune-shorts --apply because translate-bcs is operationally separate
        from curate. Locks the suffix-allowlist contract in place."""
        _prune_setup(monkeypatch)
        ch = tmp_path / "ch1"
        _make_short_artifacts(ch, "2026-04-15-short1", "short1")
        # Sidecars from the translate-bcs workflow:
        _touch(ch / "2026-04-15-short1.en.srt", "1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        _touch(ch / "2026-04-15-short1.translate-bcs.txt", "Bosnian translation")
        _touch(ch / "2026-04-15-short1.unrelated.txt", "unrelated future feature")

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        # Targets gone:
        assert not (ch / "2026-04-15-short1.meta.json").exists()
        assert not (ch / "2026-04-15-short1.mindmap.md").exists()
        # Sidecars survive:
        assert (ch / "2026-04-15-short1.en.srt").exists(), "translate-bcs SRT must survive"
        assert (ch / "2026-04-15-short1.translate-bcs.txt").exists(), "BCS translation must survive"
        assert (ch / "2026-04-15-short1.unrelated.txt").exists(), "unknown sidecars must survive"

    def test_apply_deletes_mindmap_variants(self, tmp_path, monkeypatch):
        """Knowledge / light / heavy mindmap variants share the prefix
        (e.g., {prefix}.mindmap.knowledge.md). All variants delete."""
        _prune_setup(monkeypatch)
        ch = tmp_path / "ch1"
        _make_short_artifacts(ch, "2026-04-15-short1", "short1")
        _touch(ch / "2026-04-15-short1.mindmap.knowledge.md")
        _touch(ch / "2026-04-15-short1.mindmap.light.md")

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        assert not (ch / "2026-04-15-short1.mindmap.knowledge.md").exists()
        assert not (ch / "2026-04-15-short1.mindmap.light.md").exists()

    def test_apply_deletes_transcript_raw_forensic_sidecars(self, tmp_path, monkeypatch):
        """{prefix}.transcript.raw.txt (and bounded-retry .raw.2.txt) are
        salvage-path forensic sidecars from the curate side — they DO get
        cleaned up by prune-shorts."""
        _prune_setup(monkeypatch)
        ch = tmp_path / "ch1"
        _make_short_artifacts(ch, "2026-04-15-short1", "short1")
        _touch(ch / "2026-04-15-short1.transcript.raw.txt")
        _touch(ch / "2026-04-15-short1.transcript.raw.2.txt")

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        assert not (ch / "2026-04-15-short1.transcript.raw.txt").exists()
        assert not (ch / "2026-04-15-short1.transcript.raw.2.txt").exists()

    def test_meta_without_video_id_skipped(self, tmp_path, monkeypatch):
        _prune_setup(monkeypatch)
        ch = tmp_path / "ch1"
        _write_meta(ch, "2026-04-15-orphan", {"title": "no video_id", "duration_seconds": 30})
        _touch(ch / "2026-04-15-orphan.mindmap.md")

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        # Cannot classify without video_id → leave artifacts alone
        assert (ch / "2026-04-15-orphan.meta.json").exists()
        assert (ch / "2026-04-15-orphan.mindmap.md").exists()

    def test_long_form_video_not_deleted(self, tmp_path, monkeypatch):
        _prune_setup(monkeypatch)
        # also stub _is_youtube_short_url so this test is offline-clean
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)
        ch = tmp_path / "ch1"
        _write_meta(
            ch,
            "2026-04-15-long",
            {
                "video_id": "long1",
                "title": "10 min long-form",
                "published": "2026-04-15",
                "duration_seconds": 600,
            },
        )
        _touch(ch / "2026-04-15-long.mindmap.md")
        _touch(ch / "2026-04-15-long.meta.json")  # already created by _write_meta but explicit

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        assert (ch / "2026-04-15-long.meta.json").exists(), "long-form must not be deleted"
        assert (ch / "2026-04-15-long.mindmap.md").exists()

    def test_legacy_meta_without_duration_seconds_triggers_enrich(self, tmp_path, monkeypatch):
        _prune_setup(monkeypatch)
        # Stub out the network-bound URL check too
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)

        enrich_calls = {"video_ids": []}

        def counting_enrich(_yt, video_ids):
            enrich_calls["video_ids"].extend(video_ids)
            return {video_ids[0]: "PT30S"}  # treat as short

        monkeypatch.setattr(vi, "enrich_with_durations", counting_enrich)

        ch = tmp_path / "ch1"
        # No duration_seconds field → triggers enrich call
        _write_meta(
            ch,
            "2026-04-15-legacy",
            {"video_id": "legacy1", "title": "Legacy short", "published": "2026-04-15"},
        )
        _touch(ch / "2026-04-15-legacy.mindmap.md")

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        assert enrich_calls["video_ids"] == ["legacy1"], "Missing duration_seconds must trigger enrich"
        assert not (ch / "2026-04-15-legacy.meta.json").exists(), "30s legacy short should be deleted"

    def test_empty_channel_directory_no_error(self, tmp_path, monkeypatch):
        _prune_setup(monkeypatch)
        # Channel dir doesn't exist on disk
        config = {"output_dir": str(tmp_path), "channels": [{"name": "nonexistent"}]}
        vi.cmd_prune_shorts(_prune_args(channel="nonexistent"), config)
        # No assertion needed — passing without exception is the test

    def test_apply_invalidates_video_id_cache(self, tmp_path, monkeypatch):
        _prune_setup(monkeypatch)
        ch = tmp_path / "ch1"
        _make_short_artifacts(ch, "2026-04-15-short1", "short1")

        # Prime the cache
        vi._load_video_id_index(ch)
        assert str(ch) in vi._VIDEO_ID_CACHE

        config = {"output_dir": str(tmp_path), "channels": [{"name": "ch1"}]}
        vi.cmd_prune_shorts(_prune_args(channel="ch1", apply=True), config)

        assert str(ch) not in vi._VIDEO_ID_CACHE, "cache must be invalidated after --apply"
