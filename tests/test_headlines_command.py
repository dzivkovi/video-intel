"""Tests for the standalone `headlines` command and description-aware ranking (issue #247).

`headlines` is the scan-tail headline digest with nothing in front of it, and the
ranker now reads the cleaned video description as a second, weaker evidence tier.

Falsification: set HEADLINE_DESCRIPTION_MATCH_FACTOR to 0 and class 2's first test
fails; make cmd_headlines skip render_headline_digest and class 3 fails.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_intel as vi

FACTOR = vi.HEADLINE_DESCRIPTION_MATCH_FACTOR


def _profile():
    return {
        "interest_concepts": {"ai-agents.mcp": 5, "ai.rag": 3},
        "interest_domains": ["ai-agents"],
    }


def _taxonomy():
    return {
        "concepts": {
            "ai-agents.mcp": {
                "preferred_label": "Model Context Protocol",
                "aliases": ["MCP"],
                "domain": "ai-agents",
            },
            "ai.rag": {
                "preferred_label": "Retrieval Augmented Generation",
                "aliases": ["RAG"],
                "domain": "ai",
            },
        }
    }


def _stub_headline_fetch(monkeypatch, videos):
    calls = {"n": 0}

    def _fetch(_yt, _cid, _since):
        calls["n"] += 1
        return [dict(v) for v in videos]

    monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UCabcdefghijklmnopqrstuv", "Peripheral"))
    monkeypatch.setattr("video_intel.fetch_channel_videos", _fetch)
    monkeypatch.setattr("video_intel.enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids, "PT10M"))
    monkeypatch.setattr("video_intel.is_short", lambda _vid, _dur: False)
    return calls


def _headline_config(tmp_path):
    return {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "peripheral", "url": "https://youtube.com/@p", "enabled": False, "headline_digest": True},
        ],
    }


def _score(video, profile=None, taxonomy=None):
    ranked = vi.rank_headlines([video], profile or _profile(), taxonomy or _taxonomy())
    return ranked[0]


class TestCleanHeadlineDescription:
    def test_empty_inputs(self):
        assert vi.clean_headline_description(None) == ""
        assert vi.clean_headline_description("") == ""

    def test_noise_dropped_and_real_sentence_kept(self):
        text = "\n".join(
            [
                "https://example.com/stuff",
                "0:00 Intro",
                "(12:34) The demo",
                "1:02:03 - Q&A",
                "#ai #agents",
                "Use code RAG20 for 20% off",
                "Subscribe for more!",
                "We build an MCP server from scratch",
            ]
        )
        assert vi.clean_headline_description(text) == "we build an mcp server from scratch"

    def test_boilerplate_only_description_is_empty(self):
        text = "Use code RAG20 for 20% off\nSubscribe for more!\nSupport me on Patreon"
        assert vi.clean_headline_description(text) == ""


class TestDescriptionRanksBelowTitleAndOncePerVideo:
    def test_description_match_outranks_newer_zero_and_pays_half(self):
        a = {
            "video_id": "a",
            "title": "Tuesday live",
            "description": "We build an MCP server from scratch",
            "published": "2026-07-01",
        }
        b = {"video_id": "b", "title": "Wednesday live", "published": "2026-07-02"}
        ranked = vi.rank_headlines([b, a], _profile(), _taxonomy())
        assert [v["video_id"] for v in ranked] == ["a", "b"]
        assert ranked[0]["score"] == 5 * FACTOR
        assert ranked[0]["matched_concepts"] == ["Model Context Protocol (description)"]
        assert ranked[1]["score"] == 0

    def test_boilerplate_alone_does_not_lift(self):
        v = {
            "video_id": "x",
            "title": "Tuesday live",
            "description": "Use code MCP20 for 20% off. Subscribe! MCP MCP MCP",
        }
        assert _score(v)["score"] == 0.0

    def test_title_beats_description_and_pays_once(self):
        v = {
            "video_id": "x",
            "title": "MCP explained",
            "description": "More about MCP and the MCP ecosystem",
        }
        got = _score(v)
        assert got["score"] == 5
        assert got["matched_concepts"] == ["Model Context Protocol"]

    def test_repeated_description_mentions_pay_once(self):
        once = _score({"video_id": "x", "title": "Live", "description": "We cover MCP today"})
        many = _score({"video_id": "y", "title": "Live", "description": "We cover " + "MCP " * 10})
        assert once["score"] == many["score"] == 5 * FACTOR

    def test_phrase_claim_spans_both_concepts_in_description(self):
        taxonomy = {
            "concepts": {
                "ai-agents.mcp": {"preferred_label": "Model Context Protocol", "aliases": ["context management"]},
                "ai.other": {"preferred_label": "Other Thing", "aliases": ["context management"]},
            }
        }
        profile = {"interest_concepts": {"ai-agents.mcp": 5, "ai.other": 3}, "interest_domains": []}
        v = {"video_id": "x", "title": "Live", "description": "Notes on context management today"}
        got = _score(v, profile, taxonomy)
        assert got["score"] == 5 * FACTOR
        assert got["matched_concepts"] == ["Model Context Protocol (description)"]

    def test_domain_matched_in_description_pays_reduced_weight(self):
        v = {"video_id": "x", "title": "Live", "description": "A talk about ai agents in production"}
        got = _score(v)
        assert got["score"] == vi.HEADLINE_DOMAIN_MATCH_WEIGHT * FACTOR
        assert got["matched_concepts"][0].endswith("(description)")


def _forbid(_name):
    def _raise(*_a, **_k):
        raise AssertionError("Gemini must never be touched by headlines")

    return _raise


class TestCmdHeadlinesIsTheScanTailWithNothingInFront:
    VIDEOS = (
        {"video_id": "v1", "title": "MCP deep dive", "published": "2026-07-01"},
        {"video_id": "v2", "title": "Weekly show", "description": "Today: RAG pipelines", "published": "2026-07-02"},
    )

    def _setup(self, monkeypatch):
        calls = _stub_headline_fetch(monkeypatch, self.VIDEOS)
        monkeypatch.setattr("video_intel.require_youtube", lambda: lambda *a, **k: object())
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        for name in ("create_client", "call_gemini", "require_gemini"):
            monkeypatch.setattr(f"video_intel.{name}", _forbid(name))
        backups = []
        monkeypatch.setattr("video_intel.backup_config_if_changed", lambda out: backups.append(out))
        return calls, backups

    def test_renders_marks_seen_backs_up_and_dedupes(self, monkeypatch, tmp_path):
        _, backups = self._setup(monkeypatch)
        config = _headline_config(tmp_path)
        args = SimpleNamespace(dry_run=False)

        first = vi.cmd_headlines(args, config)
        assert len(first) == 2
        seen = json.loads((tmp_path / "_headlines" / "seen.json").read_text(encoding="utf-8"))
        assert set(seen["seen"]) == {"v1", "v2"}
        assert backups == [tmp_path]

        assert vi.cmd_headlines(args, config) == []

    def test_dry_run_renders_without_seen_state(self, monkeypatch, tmp_path):
        self._setup(monkeypatch)
        items = vi.cmd_headlines(SimpleNamespace(dry_run=True), _headline_config(tmp_path))
        assert len(items) == 2
        assert not (tmp_path / "_headlines" / "seen.json").exists()

    def test_no_channels_exits_before_any_fetch(self, monkeypatch, tmp_path):
        calls, _ = self._setup(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            vi.cmd_headlines(SimpleNamespace(dry_run=False), {"output_dir": str(tmp_path)})
        assert exc.value.code == 1
        assert calls["n"] == 0

    def test_missing_youtube_key_exits_before_any_fetch(self, monkeypatch, tmp_path):
        calls, _ = self._setup(monkeypatch)
        monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
        with pytest.raises(SystemExit) as exc:
            vi.cmd_headlines(SimpleNamespace(dry_run=False), _headline_config(tmp_path))
        assert exc.value.code == 1
        assert calls["n"] == 0


def _module_tree():
    return ast.parse(Path(vi.__file__).read_text(encoding="utf-8"))


def _enclosing_callers(tree, callee):
    found = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == callee:
                found.add(fn.name)
    return found


class TestHeadlinesParityWithScan:
    def test_render_headline_digest_has_exactly_scan_and_headlines_callers(self):
        callers = _enclosing_callers(_module_tree(), "render_headline_digest")
        assert callers == {"cmd_scan", "cmd_headlines"}

    def test_walk_finds_something(self):
        # companion: the walker sees a known call, so an empty result above cannot pass silently
        assert "cmd_headlines" in _enclosing_callers(_module_tree(), "require_channels_config")


class TestHeadlinesIsRegisteredAndDispatched:
    def test_config_backup_membership(self):
        assert "headlines" in vi.CONFIG_BACKUP_COMMANDS

    def test_main_dispatches(self):
        assert 'args.command == "headlines"' in inspect.getsource(vi.main)

    def test_parser_registers_with_dry_run(self):
        script = Path(vi.__file__).resolve()
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [sys.executable, str(script), "headlines", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert "--dry-run" in proc.stdout
