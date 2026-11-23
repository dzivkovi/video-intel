"""The suite-wide Shorts probe pin (conftest `no_shorts_probe`, issue #279)."""

from __future__ import annotations

import socket

import httpx

import video_intel as vi


def test_is_short_unstubbed_makes_no_network_call(monkeypatch):
    attempts: list[str] = []

    def _record_and_refuse(*args, **_kwargs):
        attempts.append(repr(args[:2]))
        raise OSError("network is off in tests")

    monkeypatch.setattr(socket, "getaddrinfo", _record_and_refuse)
    monkeypatch.setattr(socket, "create_connection", _record_and_refuse)

    result = vi.is_short("pin279-unstubbed", "PT5M")

    assert result is False
    assert attempts == []


def test_explicit_head_stub_overrides_the_pin(monkeypatch):
    monkeypatch.setattr(vi.httpx, "head", lambda _url, **_kwargs: httpx.Response(200))

    assert vi._is_youtube_short_url("pin279-explicit") is True
