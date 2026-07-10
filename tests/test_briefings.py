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


def test_load_seen_video_ids_recurses_into_topic_subfolders(tmp_path):
    """Topic subfolders (e.g. _briefings/sales/) must not un-see their videos."""
    from video_intel import load_seen_video_ids

    briefings = tmp_path / "_briefings"
    _write_briefing(briefings, "2026-06-10-a.md", ["v1"])
    _write_briefing(briefings / "sales", "2026-07-03-sales-catchup.md", ["v2", "v3"])

    assert load_seen_video_ids(briefings) == {"v1", "v2", "v3"}


def test_load_seen_video_ids_recurses_into_dot_and_underscore_subfolders(tmp_path):
    """Unlike collect_corpus_videos's channel-dir skip, dot/underscore-prefixed
    subfolders *inside* _briefings/ are not special-cased here - they still
    count as seen. The two functions have deliberately opposite policies:
    collect_corpus_videos skips _briefings entirely (so it's never mistaken
    for a channel), while load_seen_video_ids recurses into everything under
    it (so no organizing scheme inside _briefings can un-see a video)."""
    from video_intel import load_seen_video_ids

    briefings = tmp_path / "_briefings"
    _write_briefing(briefings / ".archive", "2026-05-01-old.md", ["v4"])
    _write_briefing(briefings / "_drafts", "2026-05-02-draft.md", ["v5"])

    assert load_seen_video_ids(briefings) == {"v4", "v5"}


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
def test_compute_catchup_window_default_recency_floor():
    from video_intel import compute_catchup_window

    today = date(2026, 6, 22)
    lower, upper = compute_catchup_window(today=today)
    assert upper == today
    assert lower == date(2026, 5, 23)  # default 30-day floor


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


# --------------------------------------------------------------------------
# Regression tests for ce-code-review fixes
# --------------------------------------------------------------------------
def test_cmd_briefings_roundtrip_marks_its_own_videos_seen(tmp_path):
    """Core contract: a generated briefing's video_ids count as seen next run."""
    import video_intel as vi

    _write_video(tmp_path / "natebjones", "2026-06-20-x", video_id="v1", published="2026-06-20", concepts=[])
    args = SimpleNamespace(unseen=True, dry_run=False, since="3650d", until=None)

    vi.cmd_briefings(args, {"output_dir": str(tmp_path)})  # run 1 surfaces v1
    seen_after = vi.load_seen_video_ids(tmp_path / "_briefings")
    assert "v1" in seen_after  # the written briefing made v1 seen

    vi.cmd_briefings(args, {"output_dir": str(tmp_path)})  # run 2: v1 now seen
    files = sorted((tmp_path / "_briefings").glob("*-catch-up-unseen.md"))
    # run 2 has nothing unseen -> writes nothing -> still exactly one briefing
    assert len(files) == 1


def test_cmd_briefings_does_not_overwrite_same_day(tmp_path):
    """A second same-day run that still has unseen videos must not clobber the first."""
    import video_intel as vi

    _write_video(tmp_path / "natebjones", "2026-06-20-x", video_id="v1", published="2026-06-20", concepts=[])
    args = SimpleNamespace(unseen=True, dry_run=False, since="3650d", until=None)
    vi.cmd_briefings(args, {"output_dir": str(tmp_path)})  # writes file 1 (v1)

    # New unseen video arrives; a second same-day run should add a suffixed file.
    _write_video(tmp_path / "natebjones", "2026-06-21-z", video_id="v2", published="2026-06-21", concepts=[])
    vi.cmd_briefings(args, {"output_dir": str(tmp_path)})

    files = sorted((tmp_path / "_briefings").glob("*-catch-up-unseen*.md"))
    assert len(files) == 2  # first briefing preserved, second suffixed


def test_rank_unseen_tolerates_list_interest_concepts():
    """A hand-edited profile with interest_concepts as a list must not crash."""
    from video_intel import rank_unseen

    unseen = [{"video_id": "v", "published": "2026-06-01", "concepts_path": None}]
    # interest_concepts as a list (a natural hand-edit mistake) used to TypeError.
    ranked = rank_unseen(unseen, {"interest_concepts": ["ai.agents"], "interest_domains": []})
    assert ranked[0]["score"] == 0


def test_rank_unseen_domain_bonus(tmp_path):
    from video_intel import rank_unseen

    ch = tmp_path / "ch"
    _write_video(
        ch,
        "d",
        video_id="d",
        published="2026-06-02",
        concepts=[{"concept_id": "startup.x", "preferred_label": "X thing", "domain": "startup"}],
    )
    unseen = [{"video_id": "d", "published": "2026-06-02", "concepts_path": ch / "d.concepts.json"}]
    # concept not in interest_concepts, but its domain is in interest_domains
    profile = {"interest_concepts": {"ai.agents": 5}, "interest_domains": ["startup"]}
    ranked = rank_unseen(unseen, profile)
    assert ranked[0]["score"] == 0.5
    assert ranked[0]["matched_concepts"]  # domain match is now explained


def test_compute_catchup_window_until_overrides_upper():
    from video_intel import compute_catchup_window

    _lower, upper = compute_catchup_window(since=date(2026, 1, 1), until=date(2026, 6, 10), today=date(2026, 6, 22))
    assert upper == date(2026, 6, 10)


def test_select_unseen_inverted_window_is_empty():
    from video_intel import select_unseen

    videos = [{"video_id": "v", "published": "2026-05-15"}]
    out = select_unseen(videos, set(), lower=date(2026, 6, 1), upper=date(2026, 1, 1))
    assert out == []


def test_infer_or_load_profile_preserves_existing_without_interest_concepts(tmp_path):
    """An existing profile.yaml with content but no interest_concepts is not overwritten."""
    from video_intel import infer_or_load_profile

    briefings = tmp_path / "_briefings"
    briefings.mkdir()
    (briefings / "profile.yaml").write_text(yaml.safe_dump({"source": "hand", "channels": ["x"]}), encoding="utf-8")
    profile = infer_or_load_profile(tmp_path, {}, today=date(2026, 6, 22))
    assert profile == {"source": "hand", "channels": ["x"]}  # preserved verbatim


def test_render_unseen_briefing_includes_mindmap_links(tmp_path):
    from video_intel import render_unseen_briefing

    mm = tmp_path / "v.mindmap.md"
    mm.write_text("- Agent loops (3:57)\n", encoding="utf-8")
    ranked = [
        {
            "video_id": "v",
            "title": "Title",
            "url": "https://www.youtube.com/watch?v=v",
            "channel": "ch",
            "published": "2026-06-20",
            "score": 5,
            "matched_concepts": ["Agents"],
            "mindmap_path": mm,
        }
    ]
    md = render_unseen_briefing(ranked, {"id": "p"}, lower=date(2026, 3, 1), upper=date(2026, 6, 22))
    assert "&t=237s" in md  # 3:57 deep-link wired through render


def test_render_unseen_briefing_escapes_bracket_titles():
    from video_intel import render_unseen_briefing

    ranked = [
        {
            "video_id": "v",
            "title": "free gpt ](evil) [x",
            "url": "https://www.youtube.com/watch?v=v",
            "channel": "ch",
            "published": "2026-06-20",
            "score": 0,
            "matched_concepts": [],
            "mindmap_path": None,
        }
    ]
    md = render_unseen_briefing(ranked, {"id": "p"}, lower=date(2026, 3, 1), upper=date(2026, 6, 22))
    # The link-breaking ] is backslash-escaped, so the H2 link target stays the
    # real video URL rather than being hijacked to "(evil)".
    assert "\\](evil)" in md
    assert "## [free gpt \\](evil) \\[x](https://www.youtube.com/watch?v=v)" in md


# --------------------------------------------------------------------------
# Regression tests for Codex (cross-model) review findings
# --------------------------------------------------------------------------
def test_collect_corpus_videos_dedupes_by_video_id(tmp_path):
    """Title-rotation can leave 2 metas for one video_id; collect emits one record."""
    from video_intel import collect_corpus_videos

    ch = tmp_path / "natebjones"
    # Same video_id under two rotated slugs; the second has concepts (more complete).
    _write_video(ch, "2026-06-01-old-title", video_id="vDUP", published="2026-06-01")
    _write_video(
        ch,
        "2026-06-01-new-title",
        video_id="vDUP",
        published="2026-06-01",
        concepts=[{"concept_id": "x"}],
    )
    got = collect_corpus_videos(tmp_path)
    dup_records = [v for v in got if v["video_id"] == "vDUP"]
    assert len(dup_records) == 1  # never surfaced twice
    assert dup_records[0]["concepts_path"] is not None  # most-complete record won


def test_rank_unseen_tolerates_scalar_domains_and_string_weights():
    """A hand-edited profile with a bare-string interest_domains or string weight must not crash or mis-split."""
    from video_intel import rank_unseen

    unseen = [{"video_id": "v", "published": "2026-06-01", "concepts_path": None}]
    # interest_domains as a bare string would become set("ai") = {'a','i'} without the guard;
    # a string weight would crash score += "high".
    profile = {"interest_concepts": {"ai.agents": "high"}, "interest_domains": "ai-engineering"}
    ranked = rank_unseen(unseen, profile)
    assert ranked[0]["score"] == 0  # non-numeric weight dropped, no crash


# --------------------------------------------------------------------------
# Regression tests for dog-food validation findings (--limit, scoreless links)
# --------------------------------------------------------------------------
def test_cmd_briefings_respects_limit(tmp_path):
    """--limit caps the briefing (and its video_ids) to the top-N; the rest stay unseen."""
    import video_intel as vi

    for i in range(5):
        _write_video(
            tmp_path / "natebjones", f"2026-06-2{i}-v{i}", video_id=f"v{i}", published=f"2026-06-2{i}", concepts=[]
        )
    args = SimpleNamespace(unseen=True, dry_run=False, since="3650d", until=None, limit=2)
    vi.cmd_briefings(args, {"output_dir": str(tmp_path)})

    written = list((tmp_path / "_briefings").glob("*-catch-up-unseen.md"))
    fm, _ = vi.parse_front_matter(written[0].read_text(encoding="utf-8"))
    assert len(fm["video_ids"]) == 2  # capped to top-2; other 3 remain unseen next run


def test_render_suppresses_links_for_scoreless_entries(tmp_path):
    """A zero-score entry must not carry (possibly mismatched) mindmap deep-links."""
    from video_intel import render_unseen_briefing

    mm = tmp_path / "v.mindmap.md"
    mm.write_text("- Off-topic diagram (3:57)\n", encoding="utf-8")
    ranked = [
        {
            "video_id": "v",
            "title": "Scoreless",
            "url": "https://www.youtube.com/watch?v=v",
            "channel": "ch",
            "published": "2026-06-20",
            "score": 0,
            "matched_concepts": [],
            "mindmap_path": mm,
        }
    ]
    md = render_unseen_briefing(ranked, {"id": "p"}, lower=date(2026, 3, 1), upper=date(2026, 6, 22))
    assert "&t=" not in md  # no deep-links on an unvouched entry
