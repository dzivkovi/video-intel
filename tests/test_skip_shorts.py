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
