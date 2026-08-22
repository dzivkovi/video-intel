"""Tests for scripts/wiki_concepts.py (issue #148).

Contract under test:
- stability filter excludes single-channel concepts even at high video count;
- generated output has zero dead wikilinks;
- the write surface is confined to the requested --out directory;
- identical inputs produce byte-identical output across two runs;
- index links are checked against an INDEPENDENTLY derived expected path
  (never by calling the writer's own slugify), per the PR #136
  checker-uses-writer-path guardrail.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from wiki_concepts import (
    build_co_occurrence,
    build_mentions,
    build_pages,
    collect_corpus_videos,
    find_timestamp,
    mention_link,
    parse_mindmap_timestamps,
    select_stable_concepts,
    sibling_artifacts,
    write_pages,
)

# ---------------------------------------------------------------------------
# Fixture helpers - build a synthetic corpus tree on disk
# ---------------------------------------------------------------------------


def _write_video(
    channel_dir: Path,
    prefix: str,
    *,
    concepts: list[dict],
    meta: dict | None = None,
    mindmap_text: str | None = None,
) -> None:
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"{prefix}.concepts.json").write_text(
        json.dumps({"video_id": (meta or {}).get("video_id", prefix), "concepts": concepts}),
        encoding="utf-8",
    )
    if meta is not None:
        (channel_dir / f"{prefix}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if mindmap_text is not None:
        (channel_dir / f"{prefix}.mindmap.md").write_text(mindmap_text, encoding="utf-8")


def _concept(cid: str, label: str, as_mentioned: str = "", branch: str = "") -> dict:
    return {
        "concept_id": cid,
        "preferred_label": label,
        "as_mentioned": as_mentioned or label,
        "branch": branch or label,
        "confidence": 0.9,
        "status": "matched",
        "domain": cid.split(".")[0],
    }


def _meta(video_id: str, title: str, published: str, channel: str) -> dict:
    return {
        "video_id": video_id,
        "title": title,
        "published": published,
        "channel": channel,
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
    }


MINDMAP_A = """<!-- video: https://www.youtube.com/watch?v=vid-a -->
<!-- title: Video A -->
<!-- published: 2026-01-01 -->

## Shared Topic

* **Shared Topic**
  - Something said here (1:05)

## Other Topic

* **Other Topic**
  - Something else (2:30)
"""


def _build_stable_corpus(tmp_path: Path) -> Path:
    """Corpus where 'shared.thing' spans 3 channels (stable) and 'solo.pet'
    lives only in one channel across many videos (high count, unstable)."""
    corpus = tmp_path / "corpus"

    for i, channel in enumerate(["alpha", "beta", "gamma"], start=1):
        _write_video(
            corpus / channel,
            f"2026-0{i}-01-video",
            concepts=[_concept("shared.thing", "Shared Thing", branch="Shared Topic")],
            meta=_meta(f"vid-{channel}", f"Video {channel}", f"2026-0{i}-01", channel),
            mindmap_text=MINDMAP_A,
        )

    for i in range(1, 6):
        _write_video(
            corpus / "onlyone",
            f"2026-05-{i:02d}-pet-video",
            concepts=[_concept("solo.pet", "Solo Pet")],
            meta=_meta(f"pet-{i}", f"Pet video {i}", f"2026-05-{i:02d}", "onlyone"),
        )

    return corpus


# ---------------------------------------------------------------------------
# Stability filter
# ---------------------------------------------------------------------------


class TestStabilityFilter:
    def test_single_channel_high_count_is_excluded_even_over_multi_channel_low_count(self):
        mentions_by_concept = {
            "solo.pet": [_fake_mention("solo.pet", channel="onlyone", video_id=f"pet-{i}") for i in range(10)],
            "shared.thing": [
                _fake_mention("shared.thing", channel=ch, video_id=f"vid-{ch}") for ch in ("alpha", "beta", "gamma")
            ],
        }
        selected = select_stable_concepts(mentions_by_concept, top_n=20, min_channels=3)
        assert "solo.pet" not in selected
        assert "shared.thing" in selected

    def test_end_to_end_join_from_disk_respects_the_same_filter(self, tmp_path):
        corpus = _build_stable_corpus(tmp_path)
        records = collect_corpus_videos(corpus)
        mentions = build_mentions(records)
        selected = select_stable_concepts(mentions, top_n=20)
        assert selected == ["shared.thing"]

    def test_exactly_three_channels_is_the_minimum_that_passes(self):
        mentions_by_concept = {
            "edge.case": [_fake_mention("edge.case", channel=ch, video_id=ch) for ch in ("a", "b", "c")],
        }
        assert select_stable_concepts(mentions_by_concept, min_channels=3) == ["edge.case"]

    def test_two_channels_fails_the_minimum(self):
        mentions_by_concept = {
            "edge.case": [_fake_mention("edge.case", channel=ch, video_id=ch) for ch in ("a", "b")],
        }
        assert select_stable_concepts(mentions_by_concept, min_channels=3) == []

    def test_top_n_caps_the_selection_after_stability_filtering(self):
        mentions_by_concept = {}
        for n in range(5):
            cid = f"concept.{n}"
            mentions_by_concept[cid] = [
                _fake_mention(cid, channel=ch, video_id=f"{cid}-{ch}") for ch in ("a", "b", "c")
            ]
        selected = select_stable_concepts(mentions_by_concept, top_n=2)
        assert len(selected) == 2


def _fake_mention(cid: str, *, channel: str, video_id: str):
    from wiki_concepts import Mention, VideoRecord

    video = VideoRecord(
        channel=channel,
        video_id=video_id,
        title=f"title-{video_id}",
        published="2026-01-01",
        video_url=f"https://www.youtube.com/watch?v={video_id}",
        prefix=video_id,
        mindmap_path=None,
    )
    return Mention(video=video, concept_id=cid, preferred_label=cid, as_mentioned="", branch="")


# ---------------------------------------------------------------------------
# Corpus walk / sibling-artifact discovery
# ---------------------------------------------------------------------------


class TestCorpusWalk:
    def test_skips_dot_and_underscore_prefixed_dirs(self, tmp_path):
        corpus = tmp_path / "corpus"
        _write_video(
            corpus / "_briefings",
            "2026-01-01-not-a-channel",
            concepts=[_concept("x.y", "X Y")],
        )
        _write_video(
            corpus / ".hidden",
            "2026-01-01-also-not-a-channel",
            concepts=[_concept("x.y", "X Y")],
        )
        records = collect_corpus_videos(corpus)
        assert records == []

    def test_missing_meta_falls_back_to_concepts_json_video_id_and_prefix_title(self, tmp_path):
        corpus = tmp_path / "corpus"
        _write_video(
            corpus / "alpha",
            "2026-01-01-no-meta",
            concepts=[_concept("x.y", "X Y")],
            meta=None,
        )
        records = collect_corpus_videos(corpus)
        assert len(records) == 1
        video, _concepts = records[0]
        assert video.video_id == "2026-01-01-no-meta"
        assert video.title == "2026-01-01-no-meta"
        assert video.video_url == "https://www.youtube.com/watch?v=2026-01-01-no-meta"

    def test_sibling_artifacts_found_by_globbing_not_by_reconstructing_the_prefix(self, tmp_path):
        # A prefix containing a dot (an edge case a naive rsplit('.', 1) on
        # the concepts.json filename would mis-split) must still resolve its
        # siblings correctly, because sibling discovery globs the real
        # directory rather than reconstructing the prefix from parts.
        corpus = tmp_path / "corpus"
        channel_dir = corpus / "alpha"
        channel_dir.mkdir(parents=True)
        prefix = "2026.01.01-weird-prefix"
        _write_video(
            channel_dir, prefix, concepts=[_concept("x.y", "X Y")], meta=_meta("v1", "T", "2026-01-01", "alpha")
        )
        cpath = channel_dir / f"{prefix}.concepts.json"
        sibs = sibling_artifacts(cpath)
        assert ".meta.json" in sibs
        assert sibs[".meta.json"] == channel_dir / f"{prefix}.meta.json"

    def test_empty_concepts_list_is_skipped(self, tmp_path):
        corpus = tmp_path / "corpus"
        _write_video(corpus / "alpha", "2026-01-01-empty", concepts=[])
        assert collect_corpus_videos(corpus) == []

    def test_corrupt_concepts_json_is_skipped_not_raised(self, tmp_path):
        corpus = tmp_path / "corpus" / "alpha"
        corpus.mkdir(parents=True)
        (corpus / "2026-01-01-broken.concepts.json").write_text("{not json", encoding="utf-8")
        assert collect_corpus_videos(corpus.parent) == []


# ---------------------------------------------------------------------------
# Mindmap timestamp parsing
# ---------------------------------------------------------------------------


class TestMindmapTimestampParsing:
    def test_parses_heading_subheading_and_first_timestamp(self):
        entries = parse_mindmap_timestamps(MINDMAP_A)
        shared = [e for e in entries if e.subheading == "Shared Topic"]
        assert shared and shared[0].seconds == 65  # 1:05

    def test_multiple_comma_separated_timestamps_takes_the_first(self):
        text = "## H\n\n* **S**\n  - bullet text (02:13, 05:35)\n"
        entries = parse_mindmap_timestamps(text)
        assert entries[0].seconds == 133  # 02:13

    def test_bullet_without_timestamp_yields_none(self):
        text = "## H\n\n* **S**\n  - a bullet with no time\n"
        entries = parse_mindmap_timestamps(text)
        assert entries[0].seconds is None

    def test_find_timestamp_matches_by_subheading_branch(self):
        entries = parse_mindmap_timestamps(MINDMAP_A)
        assert find_timestamp(entries, branch="Shared Topic", as_mentioned="") == 65

    def test_find_timestamp_falls_back_to_as_mentioned_substring(self):
        entries = parse_mindmap_timestamps(MINDMAP_A)
        seconds = find_timestamp(entries, branch="Nonexistent Branch", as_mentioned="something else")
        assert seconds == 150  # 2:30

    def test_find_timestamp_returns_none_when_nothing_matches(self):
        entries = parse_mindmap_timestamps(MINDMAP_A)
        assert find_timestamp(entries, branch="Nope", as_mentioned="also nope") is None

    def test_mention_link_falls_back_to_plain_url_without_fabricating_a_time(self):
        from wiki_concepts import Mention, VideoRecord

        video = VideoRecord(
            channel="alpha",
            video_id="vid1",
            title="T",
            published="2026-01-01",
            video_url="https://www.youtube.com/watch?v=vid1",
            prefix="p",
            mindmap_path=None,
        )
        m = Mention(video=video, concept_id="x.y", preferred_label="X Y", as_mentioned="nope", branch="nope")
        assert mention_link(m, {}) == "https://www.youtube.com/watch?v=vid1"

    def test_mention_link_adds_t_param_when_timestamp_is_found(self, tmp_path):
        from wiki_concepts import Mention, VideoRecord

        mm_path = tmp_path / "v.mindmap.md"
        mm_path.write_text(MINDMAP_A, encoding="utf-8")
        video = VideoRecord(
            channel="alpha",
            video_id="vid1",
            title="T",
            published="2026-01-01",
            video_url="https://www.youtube.com/watch?v=vid1",
            prefix="p",
            mindmap_path=mm_path,
        )
        m = Mention(
            video=video, concept_id="x.y", preferred_label="X Y", as_mentioned="Shared Topic", branch="Shared Topic"
        )
        assert mention_link(m, {}) == "https://www.youtube.com/watch?v=vid1&t=65"


# ---------------------------------------------------------------------------
# Co-occurrence scoping
# ---------------------------------------------------------------------------


class TestCoOccurrence:
    def test_only_selected_concepts_appear_as_co_occurrence_keys_or_values(self, tmp_path):
        corpus = tmp_path / "corpus"
        _write_video(
            corpus / "alpha",
            "2026-01-01-v",
            concepts=[_concept("kept.one", "Kept One"), _concept("excluded.two", "Excluded Two")],
        )
        records = collect_corpus_videos(corpus)
        co = build_co_occurrence(records, {"kept.one"})
        assert "kept.one" in co
        assert "excluded.two" not in co
        assert "excluded.two" not in co["kept.one"]


# ---------------------------------------------------------------------------
# Generated-output invariants: dead links, write surface, determinism
# ---------------------------------------------------------------------------


def _build_corpus_with_related_concepts(tmp_path: Path) -> Path:
    """Two stable concepts that co-occur in every video, across 3 channels
    each, so 'Related concepts' cross-links have something real to point at."""
    corpus = tmp_path / "corpus"
    for i, channel in enumerate(["alpha", "beta", "gamma"], start=1):
        _write_video(
            corpus / channel,
            f"2026-0{i}-01-video",
            concepts=[
                _concept("topic.one", "Topic One", branch="Shared Topic"),
                _concept("topic.two", "Topic Two", branch="Other Topic"),
            ],
            meta=_meta(f"vid-{channel}", f"Video {channel}", f"2026-0{i}-01", channel),
            mindmap_text=MINDMAP_A,
        )
    return corpus


_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)\]\]")


class TestZeroDeadWikilinks:
    def test_every_wikilink_target_exists_in_the_generated_page_set(self, tmp_path):
        corpus = _build_corpus_with_related_concepts(tmp_path)
        pages = build_pages(corpus, top_n=20)
        assert any("[[" in content for content in pages.values()), "fixture should exercise at least one wikilink"
        for name, content in pages.items():
            for target in _WIKILINK_RE.findall(content):
                assert f"{target}.md" in pages, f"{name} links to missing page [[{target}]]"


class TestWriteSurfaceConfinement:
    def test_write_pages_only_creates_files_under_out_dir(self, tmp_path):
        corpus = _build_corpus_with_related_concepts(tmp_path)
        before = sorted(str(p) for p in corpus.rglob("*") if p.is_file())

        out_dir = tmp_path / "elsewhere" / "concepts_out"
        pages = build_pages(corpus, top_n=20)
        write_pages(pages, out_dir)

        after = sorted(str(p) for p in corpus.rglob("*") if p.is_file())
        assert before == after, "the corpus must never be mutated by the generator"

        written = sorted(str(p) for p in out_dir.rglob("*") if p.is_file())
        assert all(str(out_dir) in p for p in written)
        assert len(written) == len(pages)

    def test_write_pages_refuses_a_path_that_escapes_out_dir(self, tmp_path):
        out_dir = tmp_path / "out"
        with pytest.raises(ValueError, match="escapes"):
            write_pages({"../escape.md": "x"}, out_dir)


class TestDeterminism:
    def test_two_runs_over_unchanged_inputs_are_byte_identical(self, tmp_path):
        corpus = _build_corpus_with_related_concepts(tmp_path)
        first = build_pages(corpus, top_n=20)
        second = build_pages(corpus, top_n=20)
        assert first == second

    def test_two_writes_to_separate_dirs_produce_byte_identical_files(self, tmp_path):
        corpus = _build_corpus_with_related_concepts(tmp_path)
        pages = build_pages(corpus, top_n=20)
        out1, out2 = tmp_path / "out1", tmp_path / "out2"
        write_pages(pages, out1)
        write_pages(pages, out2)
        for rel in pages:
            assert (out1 / rel).read_bytes() == (out2 / rel).read_bytes()


def _independently_computed_slug(concept_id: str) -> str:
    """Deliberately re-implemented here (not imported from wiki_concepts or
    wiki_atlas) so this test cannot pass merely because the writer and the
    checker share one helper - the PR #136 checker-uses-writer-path lesson."""
    slug = re.sub(r"[^a-z0-9]+", "-", concept_id.lower()).strip("-")
    return slug or "concept"


class TestIndexLinksMatchWriterPaths:
    def test_index_links_resolve_to_pages_that_actually_exist_by_independent_slug(self, tmp_path):
        corpus = _build_corpus_with_related_concepts(tmp_path)
        pages = build_pages(corpus, top_n=20)
        index = pages["index.md"]

        records = collect_corpus_videos(corpus)
        mentions = build_mentions(records)
        selected = select_stable_concepts(mentions, top_n=20)
        assert selected, "fixture must produce at least one stable concept"

        for cid in selected:
            expected_slug = _independently_computed_slug(cid)
            assert f"[[{expected_slug}]]" in index
            assert f"{expected_slug}.md" in pages
