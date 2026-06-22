"""Tests for the `briefings --unseen` catch-up command (issue #80).

The catch-up path selects corpus videos that no existing briefing has surfaced
(strict set difference on front-matter `video_ids`), bounds them to a UTC date
window (default 90-day recency floor), ranks them by concept/taxonomy overlap
with an inferred profile, and renders a briefing. Coverage here is the
deterministic core; the LLM-judgment layer is deliberately out of v1 scope.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import yaml


# --------------------------------------------------------------------------
# Corpus fixture helpers
# --------------------------------------------------------------------------
def _write_video(channel_dir, prefix, *, video_id, published, concepts=None, mindmap=None):
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"{prefix}.meta.json").write_text(
        json.dumps(
            {
                "video_id": video_id,
                "channel": channel_dir.name,
                "title": prefix,
                "published": published,
                "video_url": f"https://www.youtube.com/watch?v={video_id}",
            }
        ),
        encoding="utf-8",
    )
    if concepts is not None:
        (channel_dir / f"{prefix}.concepts.json").write_text(
            json.dumps({"video_id": video_id, "concepts": concepts}), encoding="utf-8"
        )
    if mindmap is not None:
        (channel_dir / f"{prefix}.mindmap.md").write_text(mindmap, encoding="utf-8")


def _write_briefing(briefings_dir, name, video_ids):
    briefings_dir.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump({"artifact_type": "viewing_guide", "video_ids": video_ids})
    (briefings_dir / name).write_text(f"---\n{front}---\n\n# guide\n", encoding="utf-8")


# --------------------------------------------------------------------------
# parse_front_matter
# --------------------------------------------------------------------------
def test_parse_front_matter_valid():
    from video_intel import parse_front_matter

    fm, body = parse_front_matter("---\nvideo_ids:\n  - a\n  - b\n---\n\n# Title\ntext")
    assert fm["video_ids"] == ["a", "b"]
    assert "# Title" in body


def test_parse_front_matter_absent_returns_empty():
    from video_intel import parse_front_matter

    fm, body = parse_front_matter("# Just a heading\nno front matter")
    assert fm == {}
    assert body == "# Just a heading\nno front matter"


def test_parse_front_matter_malformed_yaml_does_not_raise():
    from video_intel import parse_front_matter

    fm, _ = parse_front_matter("---\nvideo_ids: [unclosed\n---\nbody")
    assert fm == {}  # malformed -> empty, never raises


# --------------------------------------------------------------------------
# load_seen_video_ids
# --------------------------------------------------------------------------
def test_load_seen_video_ids_unions_across_briefings(tmp_path):
    from video_intel import load_seen_video_ids

    briefings = tmp_path / "_briefings"
    _write_briefing(briefings, "2026-06-10-a.md", ["v1", "v2"])
    _write_briefing(briefings, "2026-06-17-b.md", ["v2", "v3"])

    assert load_seen_video_ids(briefings) == {"v1", "v2", "v3"}


def test_load_seen_video_ids_missing_dir_is_empty(tmp_path):
    from video_intel import load_seen_video_ids

    assert load_seen_video_ids(tmp_path / "_briefings") == set()


def test_load_seen_video_ids_briefing_without_ids(tmp_path):
    from video_intel import load_seen_video_ids

    briefings = tmp_path / "_briefings"
    briefings.mkdir()
    (briefings / "note.md").write_text("# no front matter here\n", encoding="utf-8")
    assert load_seen_video_ids(briefings) == set()


# --------------------------------------------------------------------------
# collect_corpus_videos
# --------------------------------------------------------------------------
def test_collect_corpus_videos_basic(tmp_path):
    from video_intel import collect_corpus_videos

    _write_video(tmp_path / "natebjones", "2026-06-01-x", video_id="v1", published="2026-06-01")
    _write_video(tmp_path / "ramjad", "2026-06-02-y", video_id="v2", published="2026-06-02")

    got = {v["video_id"] for v in collect_corpus_videos(tmp_path)}
    assert got == {"v1", "v2"}


def test_collect_corpus_videos_skips_underscore_and_dot_dirs(tmp_path):
    """_briefings and dot-dirs must never be mistaken for channels (scan-safety)."""
    from video_intel import collect_corpus_videos

    _write_video(tmp_path / "natebjones", "2026-06-01-x", video_id="v1", published="2026-06-01")
    # A stray meta.json inside _briefings must be ignored.
    _write_video(tmp_path / "_briefings", "2026-06-01-note", video_id="vBAD", published="2026-06-01")
    _write_video(tmp_path / ".lancedb", "2026-06-01-cache", video_id="vCACHE", published="2026-06-01")

    got = {v["video_id"] for v in collect_corpus_videos(tmp_path)}
    assert got == {"v1"}


def test_collect_corpus_videos_skips_meta_without_video_id(tmp_path):
    from video_intel import collect_corpus_videos

    ch = tmp_path / "ch"
    ch.mkdir()
    (ch / "broken.meta.json").write_text('{"title": "no id"}', encoding="utf-8")
    assert collect_corpus_videos(tmp_path) == []


# --------------------------------------------------------------------------
# compute_catchup_window + select_unseen
# --------------------------------------------------------------------------
def test_compute_catchup_window_default_90d_floor():
    from video_intel import compute_catchup_window

    today = date(2026, 6, 22)
    lower, upper = compute_catchup_window(today=today)
    assert upper == today
    assert lower == date(2026, 3, 24)  # 90 days before


def test_compute_catchup_window_since_overrides_floor():
    from video_intel import compute_catchup_window

    today = date(2026, 6, 22)
    lower, _upper = compute_catchup_window(since=date(2026, 1, 1), today=today)
    assert lower == date(2026, 1, 1)


def test_select_unseen_drops_seen_ids(tmp_path):
    from video_intel import select_unseen

    videos = [
        {"video_id": "v1", "published": "2026-06-01"},
        {"video_id": "v2", "published": "2026-06-01"},
    ]
    out = select_unseen(videos, {"v1"}, lower=date(2026, 1, 1), upper=date(2026, 12, 31))
    assert [v["video_id"] for v in out] == ["v2"]


def test_select_unseen_applies_date_window():
    from video_intel import select_unseen

    videos = [
        {"video_id": "old", "published": "2026-01-01"},
        {"video_id": "in", "published": "2026-06-01"},
        {"video_id": "future", "published": "2026-12-01"},
    ]
    out = select_unseen(videos, set(), lower=date(2026, 5, 1), upper=date(2026, 7, 1))
    assert [v["video_id"] for v in out] == ["in"]


def test_select_unseen_drops_unparseable_published():
    from video_intel import select_unseen

    videos = [
        {"video_id": "good", "published": "2026-06-01"},
        {"video_id": "bad", "published": "garbage"},
        {"video_id": "empty", "published": ""},
    ]
    out = select_unseen(videos, set(), lower=date(2026, 1, 1), upper=date(2026, 12, 31))
    assert [v["video_id"] for v in out] == ["good"]


# --------------------------------------------------------------------------
# infer_or_load_profile
# --------------------------------------------------------------------------
def test_infer_profile_from_taxonomy_and_persists(tmp_path):
    from video_intel import infer_or_load_profile

    (tmp_path / "taxonomy.json").write_text(
        json.dumps(
            {
                "concepts": {
                    "ai-engineering.agents": {"video_count": 10, "domain": "ai-engineering"},
                    "startup.fundraising": {"video_count": 3, "domain": "startup"},
                }
            }
        ),
        encoding="utf-8",
    )
    config = {"channels": [{"name": "natebjones"}, {"name": "ramjad"}]}

    profile = infer_or_load_profile(tmp_path, config, today=date(2026, 6, 22))

    assert profile["source"] == "inferred"
    assert profile["interest_concepts"]["ai-engineering.agents"] == 10
    assert "natebjones" in profile["channels"]
    # persisted to _briefings/profile.yaml
    assert (tmp_path / "_briefings" / "profile.yaml").exists()


def test_infer_or_load_profile_does_not_overwrite_handedit(tmp_path):
    from video_intel import infer_or_load_profile

    briefings = tmp_path / "_briefings"
    briefings.mkdir()
    (briefings / "profile.yaml").write_text(
        yaml.safe_dump({"source": "hand", "interest_concepts": {"my.custom": 99}}),
        encoding="utf-8",
    )
    profile = infer_or_load_profile(tmp_path, {}, today=date(2026, 6, 22))
    assert profile["source"] == "hand"
    assert profile["interest_concepts"] == {"my.custom": 99}


# --------------------------------------------------------------------------
# rank_unseen
# --------------------------------------------------------------------------
def test_rank_unseen_orders_by_concept_overlap(tmp_path):
    from video_intel import rank_unseen

    ch = tmp_path / "ch"
    _write_video(
        ch,
        "hi",
        video_id="hi",
        published="2026-06-02",
        concepts=[{"concept_id": "ai.agents", "preferred_label": "Agents"}],
    )
    _write_video(
        ch,
        "lo",
        video_id="lo",
        published="2026-06-02",
        concepts=[{"concept_id": "unrelated.thing", "preferred_label": "Thing"}],
    )
    unseen = [
        {"video_id": "lo", "published": "2026-06-02", "concepts_path": ch / "lo.concepts.json"},
        {"video_id": "hi", "published": "2026-06-02", "concepts_path": ch / "hi.concepts.json"},
    ]
    profile = {"interest_concepts": {"ai.agents": 10}, "interest_domains": []}

    ranked = rank_unseen(unseen, profile)
    assert ranked[0]["video_id"] == "hi"
    assert ranked[0]["score"] == 10
    assert "Agents" in ranked[0]["matched_concepts"]


def test_rank_unseen_keeps_videos_without_concepts(tmp_path):
    from video_intel import rank_unseen

    unseen = [{"video_id": "nc", "published": "2026-06-02", "concepts_path": None}]
    ranked = rank_unseen(unseen, {"interest_concepts": {"x": 1}})
    assert len(ranked) == 1
    assert ranked[0]["score"] == 0


# --------------------------------------------------------------------------
# extract_mindmap_links
# --------------------------------------------------------------------------
def test_extract_mindmap_links_parses_minutes_and_hours(tmp_path):
    from video_intel import extract_mindmap_links

    mm = tmp_path / "x.mindmap.md"
    mm.write_text(
        "- Agent loops (3:57)\n- Deep section (1:39:12)\n- third (5:00)\n- fourth (6:00)\n",
        encoding="utf-8",
    )
    links = extract_mindmap_links(mm, "https://youtu.be/x", limit=3)
    assert len(links) == 3
    assert links[0][1].endswith("&t=237s")  # 3:57
    assert links[1][1].endswith("&t=5952s")  # 1:39:12


def test_extract_mindmap_links_empty_when_no_timestamps(tmp_path):
    from video_intel import extract_mindmap_links

    mm = tmp_path / "x.mindmap.md"
    mm.write_text("- no timestamps here\n", encoding="utf-8")
    assert extract_mindmap_links(mm, "https://youtu.be/x") == []
    assert extract_mindmap_links(None, "https://youtu.be/x") == []


# --------------------------------------------------------------------------
# render_unseen_briefing
# --------------------------------------------------------------------------
def test_render_unseen_briefing_front_matter_carries_video_ids():
    from video_intel import parse_front_matter, render_unseen_briefing

    ranked = [
        {
            "video_id": "v1",
            "title": "First",
            "url": "https://youtu.be/v1",
            "channel": "natebjones",
            "published": "2026-06-20",
            "score": 12,
            "matched_concepts": ["Agents"],
            "mindmap_path": None,
        },
    ]
    profile = {"id": "inferred-2026-06-22"}
    md = render_unseen_briefing(
        ranked, profile, lower=date(2026, 3, 24), upper=date(2026, 6, 22), today=date(2026, 6, 22)
    )

    fm, body = parse_front_matter(md)
    assert fm["video_ids"] == ["v1"]
    assert fm["scan_window"] == {"start": "2026-03-24", "end": "2026-06-22"}
    assert fm["artifact_type"] == "viewing_guide"
    assert "First" in body


# --------------------------------------------------------------------------
# cmd_briefings (integration over a tmp corpus)
# --------------------------------------------------------------------------
def test_cmd_briefings_requires_unseen_flag(tmp_path):
    import pytest

    import video_intel as vi

    args = SimpleNamespace(unseen=False, dry_run=False, since=None, until=None)
    with pytest.raises(SystemExit):
        vi.cmd_briefings(args, {"output_dir": str(tmp_path)})


def test_cmd_briefings_dry_run_writes_nothing(tmp_path, capsys):
    import video_intel as vi

    _write_video(tmp_path / "natebjones", "2026-06-20-x", video_id="v1", published="2026-06-20", concepts=[])
    args = SimpleNamespace(unseen=True, dry_run=True, since="3650d", until=None)

    vi.cmd_briefings(args, {"output_dir": str(tmp_path)})

    out = capsys.readouterr().out
    assert "Would surface 1 unseen" in out
    # dry-run must be side-effect-free: no briefing file AND no profile.yaml
    briefings = tmp_path / "_briefings"
    assert not list(briefings.glob("*-catch-up-unseen.md"))
    assert not (briefings / "profile.yaml").exists()


def test_cmd_briefings_writes_briefing_and_excludes_seen(tmp_path):
    import video_intel as vi

    _write_video(tmp_path / "natebjones", "2026-06-20-x", video_id="v1", published="2026-06-20", concepts=[])
    _write_video(tmp_path / "natebjones", "2026-06-19-y", video_id="v2", published="2026-06-19", concepts=[])
    # v2 already surfaced in an existing briefing -> must be excluded
    _write_briefing(tmp_path / "_briefings", "2026-06-18-prev.md", ["v2"])

    args = SimpleNamespace(unseen=True, dry_run=False, since="3650d", until=None)
    vi.cmd_briefings(args, {"output_dir": str(tmp_path)})

    written = list((tmp_path / "_briefings").glob("*-catch-up-unseen.md"))
    assert len(written) == 1
    fm, _ = vi.parse_front_matter(written[0].read_text(encoding="utf-8"))
    assert fm["video_ids"] == ["v1"]  # v2 excluded as already-seen
