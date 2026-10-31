"""The concepts batch sends the taxonomy through an explicit Gemini cache.

Each concepts call carries the whole taxonomy (~280k tokens). On 2026-10-31
Gemini's implicit cache hit 0 of 25 back-to-back calls, so every call paid full
input price; an explicit cache over the same prefix served 99.7% from cache.

These tests drive the REAL ``cmd_concepts`` / ``cmd_scan`` and the REAL
``process_concepts``; only the Gemini client (the paid boundary) is faked, and
the assertions read what actually reached it [core: real-caller].
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from google.genai import types as genai_types
from test_concepts_accumulation import _make_fake_mindmap, _new_youtube_video, _scan_args, _stub_scan_env

import video_intel
from video_intel import cmd_concepts, cmd_scan

BASE_TAXONOMY = {"version": 1, "concepts": {"ai.base_concept": {"preferred_label": "Base Concept", "aliases": []}}}


class FakeGemini:
    """Records cache and generate calls; video N mints concept ``ai.minted_N``."""

    def __init__(self, *, create_fails=False, cached_call_fails_once=False):
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.calls: list[dict] = []
        self.create_fails = create_fails
        self.cached_call_fails_once = cached_call_fails_once
        self.caches = SimpleNamespace(create=self._create, delete=self._delete)
        self.models = SimpleNamespace(generate_content=self._generate)

    def _create(self, model, config):
        if self.create_fails:
            raise RuntimeError("400 INVALID_ARGUMENT: cached content is too small")
        self.created.append(config.contents[0].parts[0].text)
        return SimpleNamespace(name=f"cachedContents/c{len(self.created)}")

    def _delete(self, name):
        self.deleted.append(name)

    def _generate(self, model, contents, config):
        text = contents.parts[0].text
        cache = config.cached_content
        if cache and self.cached_call_fails_once:
            self.cached_call_fails_once = False
            raise RuntimeError("404 NOT_FOUND: CachedContent not found")
        self.calls.append({"text": text, "cache": cache})
        n = len(self.calls)
        body = {"concepts": [{"concept_id": f"ai.minted_{n}", "preferred_label": f"Minted {n}", "status": "new"}]}
        return SimpleNamespace(text=json.dumps(body), usage_metadata=None)


def _corpus(tmp_path, channel, n):
    channel_dir = tmp_path / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, n + 1):
        (channel_dir / f"video{i}.mindmap.md").write_text(f"# mindmap body {i}\n", encoding="utf-8")
        (channel_dir / f"video{i}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": f"VID{i:08d}",
                    "video_url": f"https://www.youtube.com/watch?v=VID{i:08d}",
                    "title": f"video{i}",
                    "published": f"2026-10-{i:02d}",
                    "channel": channel,
                    "prompt": "mindmap-knowledge",
                }
            ),
            encoding="utf-8",
        )
    return channel_dir


def _run_concepts(tmp_path, monkeypatch, fake, n):
    channel = "aidotengineer"
    channel_dir = _corpus(tmp_path, channel, n)
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), genai_types))
    monkeypatch.setattr(video_intel, "create_client", lambda _key: fake)
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: tmp_path)
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "load_taxonomy", lambda _od: json.loads(json.dumps(BASE_TAXONOMY)))
    monkeypatch.setattr(video_intel, "load_prompt", lambda name: "INSTRUCTIONS\n{{taxonomy}}")
    config = {"channels": [{"name": channel, "url": f"https://youtube.com/@{channel}"}], "output_dir": str(tmp_path)}
    cmd_concepts(argparse.Namespace(channel=None, force=False, dry_run=False, model=None), config)
    return channel_dir


class TestBatchUsesOneExplicitCache:
    def test_taxonomy_goes_to_the_cache_once_and_each_call_sends_only_its_mindmap(self, tmp_path, monkeypatch):
        fake = FakeGemini()
        channel_dir = _run_concepts(tmp_path, monkeypatch, fake, 3)

        assert len(fake.created) == 1
        assert "ai.base_concept" in fake.created[0] and "INSTRUCTIONS" in fake.created[0]
        assert [c["cache"] for c in fake.calls] == ["cachedContents/c1"] * 3
        for i, call in enumerate(fake.calls, 1):
            assert "ai.base_concept" not in call["text"], "the cached taxonomy must not be resent inline"
            assert f"mindmap body {i}" in call["text"]
        assert fake.deleted == ["cachedContents/c1"]
        assert all((channel_dir / f"video{i}.concepts.json").exists() for i in (1, 2, 3))

    def test_concepts_minted_earlier_in_the_run_reach_later_calls(self, tmp_path, monkeypatch):
        """ADR-0010 survives the cache: the snapshot is frozen, so new concepts travel inline."""
        fake = FakeGemini()
        _run_concepts(tmp_path, monkeypatch, fake, 3)

        assert "ai.minted_1" not in fake.calls[0]["text"]
        assert "ai.minted_1" in fake.calls[1]["text"]
        assert "ai.minted_1" in fake.calls[2]["text"] and "ai.minted_2" in fake.calls[2]["text"]

    def test_a_single_video_goes_inline_without_building_a_cache(self, tmp_path, monkeypatch):
        fake = FakeGemini()
        _run_concepts(tmp_path, monkeypatch, fake, 1)

        assert fake.created == []
        assert fake.calls[0]["cache"] is None
        assert "ai.base_concept" in fake.calls[0]["text"]


class TestCacheFailuresDegradeToInline:
    def test_cache_build_failure_sends_every_call_inline_and_tries_to_build_once(self, tmp_path, monkeypatch):
        attempts = {"n": 0}
        fake = FakeGemini(create_fails=True)
        original = fake._create

        def counting_create(model, config):
            attempts["n"] += 1
            return original(model, config)

        fake.caches.create = counting_create
        channel_dir = _run_concepts(tmp_path, monkeypatch, fake, 3)

        assert attempts["n"] == 1
        assert [c["cache"] for c in fake.calls] == [None, None, None]
        assert all("ai.base_concept" in c["text"] for c in fake.calls)
        assert all((channel_dir / f"video{i}.concepts.json").exists() for i in (1, 2, 3))

    def test_a_failed_cached_call_retries_that_video_inline_then_rebuilds(self, tmp_path, monkeypatch):
        fake = FakeGemini(cached_call_fails_once=True)
        channel_dir = _run_concepts(tmp_path, monkeypatch, fake, 3)

        assert fake.calls[0]["cache"] is None and "ai.base_concept" in fake.calls[0]["text"]
        assert [c["cache"] for c in fake.calls[1:]] == ["cachedContents/c2", "cachedContents/c2"]
        assert all((channel_dir / f"video{i}.concepts.json").exists() for i in (1, 2, 3))
        assert fake.deleted == ["cachedContents/c2"]


class TestScanConceptsLoopUsesTheCache:
    def test_scan_auto_concepts_sends_the_taxonomy_through_the_cache(self, tmp_path, monkeypatch):
        channel = "everyinc"
        channel_dir = tmp_path / channel
        videos = [_new_youtube_video(f"VIDS{i:04d}", f"Scan Video {i}") for i in (1, 2)]
        _stub_scan_env(monkeypatch, tmp_path, {f"UC-https://youtube.com/@{channel}": videos})
        fake = FakeGemini()
        monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), genai_types))
        monkeypatch.setattr(video_intel, "create_client", lambda _key: fake)
        monkeypatch.setattr(video_intel, "load_taxonomy", lambda _od: json.loads(json.dumps(BASE_TAXONOMY)))
        monkeypatch.setattr(video_intel, "process_mindmap", _make_fake_mindmap({channel: channel_dir}))
        monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-for-{name}\n{{{{taxonomy}}}}")
        config = {
            "channels": [{"name": channel, "url": f"https://youtube.com/@{channel}", "auto_concepts": True}],
            "output_dir": str(tmp_path),
            "auto_concepts": True,
        }
        cmd_scan(_scan_args(channel=channel), config)

        assert len(fake.created) == 1 and "ai.base_concept" in fake.created[0]
        concept_calls = [c for c in fake.calls if c["cache"]]
        assert len(concept_calls) == 2
        assert all("ai.base_concept" not in c["text"] for c in concept_calls)
        assert fake.deleted == ["cachedContents/c1"]
