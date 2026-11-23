"""The suite-wide Shorts probe pin (conftest `no_shorts_probe`, issue #279)."""

from __future__ import annotations

import socket

import httpx

import video_intel as vi

SHORTS_URL = "https://www.youtube.com/shorts/pin279"


def test_is_short_unstubbed_makes_no_network_call(monkeypatch):
    attempts: list[str] = []

    def _record_and_refuse(*args, **_kwargs):
        attempts.append(repr(args[:2]))
        raise OSError("network is off in tests")

    monkeypatch.setattr(socket, "getaddrinfo", _record_and_refuse)
    monkeypatch.setattr(socket, "create_connection", _record_and_refuse)

    via_is_short = vi.is_short("pin279-unstubbed", "PT5M")
    # Called directly too: is_short swallows any exception from the probe, so a
    # pin that crashed would otherwise read as a clean "not a Short".
    via_probe = vi._is_youtube_short_url("pin279-unstubbed-direct")

    assert via_is_short is False
    assert via_probe is False
    assert attempts == []


def test_the_pin_answers_like_youtube_does_for_long_form():
    assert vi.httpx.head(SHORTS_URL).status_code == 303
    assert vi.httpx.head(SHORTS_URL, follow_redirects=True).status_code == 200


def test_explicit_head_stub_overrides_the_pin(monkeypatch):
    assert vi.httpx.head(SHORTS_URL).status_code == 303
    monkeypatch.setattr(vi.httpx, "head", lambda _url, **_kwargs: httpx.Response(200))

    assert vi._is_youtube_short_url("pin279-explicit") is True


def test_the_pin_clears_the_probe_cache_between_lifecycles(shorts_probe_pin):
    """The probe memoises per id for the life of the process. Two pin lifecycles
    run here back to back on one id: the first caches the pinned answer, the
    second stubs its own. Without the pin's cache clears the second would be
    handed the first one's cached False. One test, so the proof does not depend
    on test order or on which worker runs what."""
    video_id = "pin279-shared-id"

    with shorts_probe_pin():
        first = vi._is_youtube_short_url(video_id)

    with shorts_probe_pin():
        vi.httpx.head = lambda _url, **_kwargs: httpx.Response(200)
        second = vi._is_youtube_short_url(video_id)

    assert first is False
    assert second is True
    assert vi.httpx.head(SHORTS_URL).status_code == 303
