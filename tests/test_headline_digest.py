"""Tests for the headline digest - peripheral vision over unfollowed channels (issue #113).

A channel with `enabled: false` is invisible to `scan`. The headline digest gives
the user a "flip the newspaper for catchy headlines" experience over channels they
do not actively follow, rendered as a trailing section of a full `scan` run.

Frozen design (Claude + Codex cross-model pass). The invariants under test:
  - Eligibility: `enabled is false` AND `headline_digest is true` AND a recognizably
    YouTube source, validated BEFORE `get_channel_id()` (a mis-flagged non-YouTube
    url must never hit the API).
  - Tri-state guard: a stray non-boolean `enabled` never enters the primary loop
    (the truthiness-gate bug the separate opt-in key exists to avoid).
  - Ranking: metadata-only videos are ranked by title/profile match (`rank_headlines`),
    NOT by concepts (`rank_unseen` would score every item zero).
  - Profile is loaded with `persist=False` (a scan never creates/overwrites profile.yaml).
  - Seen-state lives in `_headlines/seen.json`, advanced only after render; `--dry-run`
    does not advance it.
  - Placement: `scan --channel X` emits no digest; full `scan` does.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock

import pytest

import video_intel as vi

# ---------------------------------------------------------------------------
# YouTube-source validation (runs BEFORE get_channel_id)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/@somecreator",
        "https://www.youtube.com/@somecreator",
        "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
        "UCabcdefghijklmnopqrstuv",  # bare channel id
        "http://m.youtube.com/@creator",
    ],
)
def test_youtube_source_accepts_youtube_shapes(url):
    assert vi._is_youtube_channel_source(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/user123",
        "https://www.skool.com/some-community",
        "https://notyoutube.com/@creator",  # host suffix trick must not pass
        "",
        None,
        "just-a-handle",
        "UCtooshort",  # not a valid UC channel-id length
    ],
)
def test_youtube_source_rejects_non_youtube(url):
    assert vi._is_youtube_channel_source(url) is False


# ---------------------------------------------------------------------------
# Eligibility collector
# ---------------------------------------------------------------------------


def _cfg(*channels):
    return {"channels": list(channels)}


def test_collect_requires_all_conditions():
    eligible = {"name": "peripheral", "url": "https://youtube.com/@p", "enabled": False, "headline_digest": True}
    config = _cfg(
        eligible,
        {"name": "enabled_true", "url": "https://youtube.com/@e", "enabled": True, "headline_digest": True},
        {"name": "no_flag", "url": "https://youtube.com/@n", "enabled": False},
        {"name": "non_youtube", "url": "https://vimeo.com/x", "enabled": False, "headline_digest": True},
        {"name": "default_enabled", "url": "https://youtube.com/@d", "headline_digest": True},
    )
    got = [c["name"] for c in vi.collect_headline_channels(config)]
    assert got == ["peripheral"]


def test_collect_never_calls_get_channel_id_for_non_youtube(monkeypatch):
    """The YouTube shape check must gate BEFORE any API resolution."""

    def _fail(*_a, **_k):
        pytest.fail("get_channel_id must not be reached for a non-YouTube headline channel")

    monkeypatch.setattr("video_intel.get_channel_id", _fail)
    config = _cfg({"name": "bad", "url": "https://vimeo.com/x", "enabled": False, "headline_digest": True})
    # Collector performs the shape check itself; a non-YouTube url is dropped and
    # never handed to get_channel_id.
    assert vi.collect_headline_channels(config) == []


# ---------------------------------------------------------------------------
# Tri-state guard: a stray non-boolean `enabled` must not enter the primary loop
# ---------------------------------------------------------------------------


def _stub_scan_environment(monkeypatch, tmp_path):
    monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
    monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)


def test_non_boolean_enabled_stays_out_of_primary_loop(monkeypatch, tmp_path):
    """`enabled: "headlines"` is truthy - a naive gate would pull it into full
    Gemini processing. The strict boolean gate keeps it out of the primary loop."""
    _stub_scan_environment(monkeypatch, tmp_path)

    def _fail_get_channel_id(*_a, **_k):
        pytest.fail("a channel with a non-boolean enabled must not reach the primary scan loop")

    monkeypatch.setattr("video_intel.get_channel_id", _fail_get_channel_id)
    monkeypatch.setattr("video_intel.render_headline_digest", lambda *a, **k: [])

    config = {
        "output_dir": str(tmp_path),
        "channels": [{"name": "stray", "url": "https://youtube.com/@s", "enabled": "headlines"}],
    }
    args = argparse.Namespace(channel=None, since=None, dry_run=True, force=False, model=None)
    vi.cmd_scan(args, config)


def test_channel_scan_enabled_helper():
    assert vi._channel_scan_enabled({"name": "a"}) is True  # absent -> default True
    assert vi._channel_scan_enabled({"enabled": True}) is True
    assert vi._channel_scan_enabled({"enabled": False}) is False
    assert vi._channel_scan_enabled({"enabled": "headlines"}) is False  # non-bool -> out
    assert vi._channel_scan_enabled({"enabled": 1}) is False  # non-bool -> out


# ---------------------------------------------------------------------------
# Ranking: title/profile match, not all-zero; positives sort before zero recents
# ---------------------------------------------------------------------------


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


def test_rank_headlines_scores_by_title_match():
    videos = [
        {"video_id": "mcp", "title": "Building with MCP servers", "published": "2026-07-01"},
        {"video_id": "rag", "title": "A deep dive into RAG pipelines", "published": "2026-07-02"},
        {"video_id": "cat", "title": "My cat did something cute", "published": "2026-07-10"},
    ]
    ranked = vi.rank_headlines(videos, _profile(), _taxonomy())
    # Not all-zero: the metadata-only titles matched profile phrases.
    assert any(v["score"] > 0 for v in ranked)
    # Highest weight first, then next, then the zero-score recent last.
    assert [v["video_id"] for v in ranked] == ["mcp", "rag", "cat"]
    assert ranked[-1]["score"] == 0


def test_rank_headlines_positive_before_zero_score_recents():
    """A high-relevance OLD video sorts above a zero-score NEW one."""
    videos = [
        {"video_id": "new_noise", "title": "Random unrelated vlog", "published": "2026-07-20"},
        {"video_id": "old_signal", "title": "MCP explained", "published": "2026-01-01"},
    ]
    ranked = vi.rank_headlines(videos, _profile(), _taxonomy())
    assert [v["video_id"] for v in ranked] == ["old_signal", "new_noise"]


def test_rank_headlines_humanizes_concept_id_without_taxonomy():
    profile = {"interest_concepts": {"ai-agents.mcp-servers": 4}, "interest_domains": []}
    videos = [{"video_id": "x", "title": "Using MCP servers in production", "published": "2026-07-01"}]
    ranked = vi.rank_headlines(videos, profile, taxonomy=None)
    assert ranked[0]["score"] == 4


# ---------------------------------------------------------------------------
# Seen state + profile side-effect + placement (through render_headline_digest)
# ---------------------------------------------------------------------------


def _stub_headline_fetch(monkeypatch, videos):
    monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UCabcdefghijklmnopqrstuv", "Peripheral"))
    monkeypatch.setattr("video_intel.fetch_channel_videos", lambda _yt, _cid, _since: [dict(v) for v in videos])
    monkeypatch.setattr(
        "video_intel.enrich_with_durations",
        lambda _yt, ids: dict.fromkeys(ids, "PT10M"),  # all long-form
    )
    monkeypatch.setattr("video_intel.is_short", lambda _vid, _dur: False)


def _headline_config(tmp_path):
    return {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "peripheral", "url": "https://youtube.com/@p", "enabled": False, "headline_digest": True},
        ],
    }


def test_render_advances_seen_and_does_not_resurface(monkeypatch, tmp_path):
    videos = [
        {"video_id": "v1", "title": "MCP deep dive", "published": "2026-07-01"},
        {"video_id": "v2", "title": "RAG patterns", "published": "2026-07-02"},
    ]
    _stub_headline_fetch(monkeypatch, videos)
    config = _headline_config(tmp_path)

    first = vi.render_headline_digest(MagicMock(), config, tmp_path, dry_run=False)
    assert {v["video_id"] for v in first} == {"v1", "v2"}

    seen_path = tmp_path / "_headlines" / "seen.json"
    assert seen_path.exists()

    second = vi.render_headline_digest(MagicMock(), config, tmp_path, dry_run=False)
    assert second == []  # nothing re-surfaces once seen


def test_dry_run_does_not_advance_seen(monkeypatch, tmp_path):
    videos = [{"video_id": "v1", "title": "MCP deep dive", "published": "2026-07-01"}]
    _stub_headline_fetch(monkeypatch, videos)
    config = _headline_config(tmp_path)

    vi.render_headline_digest(MagicMock(), config, tmp_path, dry_run=True)
    assert not (tmp_path / "_headlines" / "seen.json").exists()

    # A subsequent real run still surfaces the item (dry-run left it unseen).
    surfaced = vi.render_headline_digest(MagicMock(), config, tmp_path, dry_run=False)
    assert [v["video_id"] for v in surfaced] == ["v1"]


def test_render_does_not_create_or_modify_profile(monkeypatch, tmp_path):
    videos = [{"video_id": "v1", "title": "MCP deep dive", "published": "2026-07-01"}]
    _stub_headline_fetch(monkeypatch, videos)
    config = _headline_config(tmp_path)

    vi.render_headline_digest(MagicMock(), config, tmp_path, dry_run=False)
    assert not (tmp_path / "_briefings" / "profile.yaml").exists()


def test_render_caps_global_item_count(monkeypatch, tmp_path):
    videos = [
        {"video_id": f"v{i}", "title": f"Random topic {i}", "published": f"2026-07-{i:02d}"} for i in range(1, 21)
    ]
    _stub_headline_fetch(monkeypatch, videos)
    config = _headline_config(tmp_path)
    top = vi.render_headline_digest(MagicMock(), config, tmp_path, dry_run=True)
    assert len(top) <= vi.HEADLINES_MAX_ITEMS


# ---------------------------------------------------------------------------
# Placement inside cmd_scan
# ---------------------------------------------------------------------------


def test_full_scan_invokes_headline_digest(monkeypatch, tmp_path):
    _stub_scan_environment(monkeypatch, tmp_path)
    monkeypatch.setattr("video_intel.get_channel_id", lambda *_a, **_k: (None, None))
    calls = []
    monkeypatch.setattr(
        "video_intel.render_headline_digest",
        lambda *a, **k: calls.append(k.get("dry_run")) or [],
    )
    config = {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "regular", "url": "https://youtube.com/@r"},
            {"name": "peripheral", "url": "https://youtube.com/@p", "enabled": False, "headline_digest": True},
        ],
    }
    args = argparse.Namespace(channel=None, since=None, dry_run=False, force=False, model=None)
    vi.cmd_scan(args, config)
    assert calls == [False], "full scan must invoke the headline digest exactly once"


def test_focused_scan_skips_headline_digest(monkeypatch, tmp_path):
    _stub_scan_environment(monkeypatch, tmp_path)
    monkeypatch.setattr("video_intel.get_channel_id", lambda *_a, **_k: (None, None))

    def _fail(*_a, **_k):
        pytest.fail("scan --channel X must not emit the peripheral headline digest")

    monkeypatch.setattr("video_intel.render_headline_digest", _fail)
    config = {
        "output_dir": str(tmp_path),
        "channels": [
            {"name": "regular", "url": "https://youtube.com/@r"},
            {"name": "peripheral", "url": "https://youtube.com/@p", "enabled": False, "headline_digest": True},
        ],
    }
    args = argparse.Namespace(channel="regular", since=None, dry_run=False, force=False, model=None)
    vi.cmd_scan(args, config)


def test_seen_state_is_bounded(monkeypatch, tmp_path):
    """seen.json is a bounded set - it never grows without limit."""
    ids = [f"vid{i}" for i in range(vi.HEADLINES_SEEN_MAX + 50)]
    vi.advance_headlines_seen(tmp_path, ids)
    stored = json.loads((tmp_path / "_headlines" / "seen.json").read_text(encoding="utf-8"))
    kept = stored["seen"] if isinstance(stored, dict) else stored
    assert len(kept) <= vi.HEADLINES_SEEN_MAX
    # The most recent ids are the ones retained.
    assert ids[-1] in kept
    assert ids[0] not in kept


# ---------------------------------------------------------------------------
# Global cap, Shorts filter, quota degradation, malformed inputs (review hardening)
# ---------------------------------------------------------------------------


def _quota_http_error():
    resp = MagicMock(status=403)
    content = b'{"error": {"errors": [{"reason": "quotaExceeded"}], "code": 403, "message": "quota exceeded"}}'
    return vi.HttpError(resp=resp, content=content)


def _non_quota_http_error():
    resp = MagicMock(status=403)
    content = b'{"error": {"errors": [{"reason": "forbidden"}], "code": 403, "message": "forbidden"}}'
    return vi.HttpError(resp=resp, content=content)


def test_positive_matches_cap_at_max_items(monkeypatch, tmp_path):
    """The POSITIVE-match slice honors HEADLINES_MAX_ITEMS, not just the zero sub-cap.

    A prior version tested the cap with all-zero-score titles, which only exercised
    HEADLINES_MAX_ZERO_SCORE. Here every title matches the profile, so a broken
    positive-path slice would flood the digest past the global cap.
    """
    # Profile whose humanized concept ids appear verbatim in every title.
    profile = {"interest_concepts": {f"ai.topic{i}": 10 for i in range(30)}, "interest_domains": []}
    videos = [
        {"video_id": f"v{i}", "title": f"All about topic{i % 30} and mcp", "published": f"2026-07-{(i % 28) + 1:02d}"}
        for i in range(25)
    ]
    tax = {
        "concepts": {f"ai.topic{i}": {"preferred_label": f"topic{i}", "aliases": [], "domain": "ai"} for i in range(30)}
    }
    ranked = vi.rank_headlines(videos, profile, tax)
    assert all(v["score"] > 0 for v in ranked), "test setup: every title should score positive"
    positive, zero = vi._select_headline_items(ranked)
    assert len(positive) == vi.HEADLINES_MAX_ITEMS
    assert zero == []  # no slots left for zero-score once positives fill the cap
    assert len(positive) + len(zero) <= vi.HEADLINES_MAX_ITEMS


def test_render_drops_shorts(monkeypatch, tmp_path):
    """A video classified as a Short must not appear in the rendered digest."""
    videos = [
        {"video_id": "long1", "title": "A real long-form talk", "published": "2026-07-01"},
        {"video_id": "short1", "title": "A quick short", "published": "2026-07-02"},
    ]
    monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UCabcdefghijklmnopqrstuv", "Peripheral"))
    monkeypatch.setattr("video_intel.fetch_channel_videos", lambda _yt, _cid, _since: [dict(v) for v in videos])
    monkeypatch.setattr("video_intel.enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids, "PT10M"))
    monkeypatch.setattr("video_intel.is_short", lambda vid, _dur: vid == "short1")
    rendered = vi.render_headline_digest(MagicMock(), _headline_config(tmp_path), tmp_path, dry_run=True)
    ids = {v["video_id"] for v in rendered}
    assert "long1" in ids
    assert "short1" not in ids


def test_fetch_quota_error_stops_digest_gracefully(monkeypatch, tmp_path):
    monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UCabcdefghijklmnopqrstuv", "Peripheral"))

    def _raise_quota(*_a, **_k):
        raise _quota_http_error()

    monkeypatch.setattr("video_intel.fetch_channel_videos", _raise_quota)
    # Must not propagate - the digest degrades to empty, the scan continues.
    out = vi.render_headline_digest(MagicMock(), _headline_config(tmp_path), tmp_path, dry_run=False)
    assert out == []


def test_channel_resolution_quota_error_stops_digest(monkeypatch, tmp_path):
    def _raise_quota(*_a, **_k):
        raise _quota_http_error()

    monkeypatch.setattr("video_intel.get_channel_id", _raise_quota)
    out = vi.render_headline_digest(MagicMock(), _headline_config(tmp_path), tmp_path, dry_run=False)
    assert out == []


def test_enrich_quota_error_keeps_items_unfiltered(monkeypatch, tmp_path):
    """When the Shorts-check enrich quota-fails, items are kept without per-video HEAD probes."""
    videos = [{"video_id": "v1", "title": "Long talk", "published": "2026-07-01"}]
    monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UCabcdefghijklmnopqrstuv", "Peripheral"))
    monkeypatch.setattr("video_intel.fetch_channel_videos", lambda _yt, _cid, _since: [dict(v) for v in videos])

    def _raise_quota(*_a, **_k):
        raise _quota_http_error()

    monkeypatch.setattr("video_intel.enrich_with_durations", _raise_quota)

    def _fail_is_short(*_a, **_k):
        pytest.fail("is_short must not run when durations are quota-unavailable (avoids N network HEADs)")

    monkeypatch.setattr("video_intel.is_short", _fail_is_short)
    out = vi.render_headline_digest(MagicMock(), _headline_config(tmp_path), tmp_path, dry_run=True)
    assert [v["video_id"] for v in out] == ["v1"]


def test_non_quota_http_error_propagates(monkeypatch, tmp_path):
    """A non-quota 403 is a real error and must NOT be swallowed as quota degradation."""
    monkeypatch.setattr("video_intel.get_channel_id", lambda _yt, _url: ("UCabcdefghijklmnopqrstuv", "Peripheral"))

    def _raise(*_a, **_k):
        raise _non_quota_http_error()

    monkeypatch.setattr("video_intel.fetch_channel_videos", _raise)
    with pytest.raises(vi.HttpError):
        vi.render_headline_digest(MagicMock(), _headline_config(tmp_path), tmp_path, dry_run=False)


def test_malformed_seen_json_degrades_to_resurface(monkeypatch, tmp_path):
    """A corrupt seen.json loses dedup for that run but must not crash."""
    headlines_dir = tmp_path / "_headlines"
    headlines_dir.mkdir(parents=True)
    (headlines_dir / "seen.json").write_text("{not valid json", encoding="utf-8")
    assert vi.load_headlines_seen_ids(tmp_path) == []
    videos = [{"video_id": "v1", "title": "Long talk", "published": "2026-07-01"}]
    _stub_headline_fetch(monkeypatch, videos)
    out = vi.render_headline_digest(MagicMock(), _headline_config(tmp_path), tmp_path, dry_run=True)
    assert [v["video_id"] for v in out] == ["v1"]


def test_rank_headlines_empty_profile_all_zero(monkeypatch):
    """rank_headlines on an empty profile scores zero without crashing (coercion path)."""
    videos = [{"video_id": "v1", "title": "Anything at all", "published": "2026-07-01"}]
    ranked = vi.rank_headlines(videos, {}, None)
    assert ranked[0]["score"] == 0
    assert ranked[0]["matched_concepts"] == []


def test_render_returns_empty_when_no_eligible_channels(monkeypatch, tmp_path):
    """Zero eligible channels short-circuits before any profile/taxonomy load."""

    def _fail_profile(*_a, **_k):
        pytest.fail("the interest model must not be loaded when there are no eligible channels")

    # Guard the loader the digest actually calls (issue #115). Pointed at
    # infer_or_load_profile it would pass vacuously, guarding nothing.
    monkeypatch.setattr("video_intel.load_interest_model", _fail_profile)
    config = {"output_dir": str(tmp_path), "channels": [{"name": "regular", "url": "https://youtube.com/@r"}]}
    assert vi.render_headline_digest(MagicMock(), config, tmp_path, dry_run=True) == []
