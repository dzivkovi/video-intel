"""Tests for scripts/wiki_concepts.py (issue #148, hardened for PR #150).

Contract under test:
- stability filter excludes single-channel concepts even at high video count;
- generated output has zero dead wikilinks;
- the write surface is confined to the requested --out directory;
- identical inputs produce byte-identical output across two runs;
- index links are checked against an INDEPENDENTLY derived expected path
  (never by calling the writer's own slugify), per the PR #136
  checker-uses-writer-path guardrail;
- write_pages refuses to overwrite a foreign generator's page and prunes
  only its own stale pages (issue #150 FIX1);
- the as_mentioned timestamp fallback requires a whole-token match, not a
  substring (issue #150 FIX3);
- creator-authored text is escaped before landing in a table row or link
  (issue #150 FIX4);
- sibling discovery survives prefixes containing glob metacharacters
  (issue #150 FIX5);
- unreadable/unparseable files are logged and counted, never silent
  (issue #150 FIX6);
- slug collisions are disambiguated, and the MOC basename is reserved
  (issue #150 FIX7);
- --top rejects non-positive values, co-occurrence and channel-membership
  counting dedup by video_id, and generated_from stores only the corpus
  directory name (issue #150 FIX8).
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

import pytest
import wiki_concepts as wc
from wiki_concepts import (
    MOC_BASENAME,
    build_co_occurrence,
    build_mentions,
    build_pages,
    collect_corpus_videos,
    find_timestamp,
    mention_link,
    parse_mindmap_timestamps,
    reset_skip_tracking,
    select_stable_concepts,
    sibling_artifacts,
    skipped_file_count,
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

    def test_minutes_past_99_anchor_at_the_real_time_not_a_substring(self):
        """Issue #195, same class as chunk_transcript's boundary fix.

        The capped `\\d{1,2}` token pattern did not MISS `(100:18)` - findall
        matched the substring `00:18` and anchored the citation at 18 seconds,
        100 minutes early. That is a fabricated timestamp, which invariant 4 of
        this module's contract forbids outright.
        """
        text = "## H\n\n* **S**\n  - a bullet from the third hour (100:18)\n"
        entries = parse_mindmap_timestamps(text)
        assert entries[0].seconds == 6018

    def test_three_part_timestamps_are_unchanged_by_the_widened_token(self):
        text = "## H\n\n* **S**\n  - late bullet (1:40:18)\n"
        entries = parse_mindmap_timestamps(text)
        assert entries[0].seconds == 6018

    @pytest.mark.parametrize("garbage", ["100:180", "100:18:99:22", "1234:5"])
    def test_a_malformed_token_anchors_nothing_rather_than_something_wrong(self, garbage):
        """Codex peer-review case: an unanchored findall would fabricate a
        plausible anchor out of a malformed value ("100:180" -> "100:18").
        The contract is None -> render without &t=, never a wrong anchor."""
        text = f"## H\n\n* **S**\n  - malformed bullet ({garbage})\n"
        entries = parse_mindmap_timestamps(text)
        assert entries[0].seconds is None

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


def _independently_assign_slugs(selected_ids: list[str]) -> dict[str, str]:
    """Second, independently written collision-disambiguation pass (issue
    #150 FIX7): does NOT call `wiki_concepts._assign_slugs`, so a bug in the
    writer's own disambiguation algorithm would surface as a mismatch here
    instead of trivially agreeing with itself - the same spirit as PR #136's
    checker-uses-writer's-path lesson, applied to an algorithm instead of a
    path. Reserves the MOC basename exactly like the writer does."""
    used: set[str] = {MOC_BASENAME}
    slug_for: dict[str, str] = {}
    for cid in selected_ids:
        base = slug = _independently_computed_slug(cid)
        n = 2
        while slug in used:
            slug = f"{base}-{n}"
            n += 1
        used.add(slug)
        slug_for[cid] = slug
    return slug_for


def _build_corpus_with_colliding_concept_ids(tmp_path: Path) -> Path:
    """Two distinct concept_ids that slugify to the same base ('topic.one'
    and 'topic_one' both -> 'topic-one'), each stable across 3 channels, so
    the collision-disambiguation path is actually exercised end-to-end."""
    corpus = tmp_path / "corpus"
    for i, channel in enumerate(["alpha", "beta", "gamma"], start=1):
        _write_video(
            corpus / channel,
            f"2026-0{i}-01-video-a",
            concepts=[_concept("topic.one", "Topic One")],
            meta=_meta(f"vid-a-{channel}", f"Video A {channel}", f"2026-0{i}-01", channel),
        )
        _write_video(
            corpus / channel,
            f"2026-0{i}-02-video-b",
            concepts=[_concept("topic_one", "Topic One Alt")],
            meta=_meta(f"vid-b-{channel}", f"Video B {channel}", f"2026-0{i}-02", channel),
        )
    return corpus


class TestIndexLinksMatchWriterPaths:
    def test_index_links_resolve_to_pages_that_actually_exist_by_independent_slug(self, tmp_path):
        corpus = _build_corpus_with_related_concepts(tmp_path)
        pages = build_pages(corpus, top_n=20)
        index = pages[f"{MOC_BASENAME}.md"]

        records = collect_corpus_videos(corpus)
        mentions = build_mentions(records)
        selected = select_stable_concepts(mentions, top_n=20)
        assert selected, "fixture must produce at least one stable concept"

        expected_slugs = _independently_assign_slugs(selected)
        for cid in selected:
            expected_slug = expected_slugs[cid]
            assert f"[[{expected_slug}]]" in index
            assert f"{expected_slug}.md" in pages

    def test_index_links_resolve_correctly_even_with_a_slug_collision(self, tmp_path):
        corpus = _build_corpus_with_colliding_concept_ids(tmp_path)
        pages = build_pages(corpus, top_n=20)
        index = pages[f"{MOC_BASENAME}.md"]

        records = collect_corpus_videos(corpus)
        mentions = build_mentions(records)
        selected = select_stable_concepts(mentions, top_n=20)
        assert len(selected) >= 2, "fixture must produce a real collision to disambiguate"

        expected_slugs = _independently_assign_slugs(selected)
        seen_slugs: set[str] = set()
        for cid in selected:
            slug = expected_slugs[cid]
            assert slug not in seen_slugs, "collision disambiguation must yield distinct slugs"
            seen_slugs.add(slug)
            assert f"[[{slug}]]" in index
            assert f"{slug}.md" in pages


# ---------------------------------------------------------------------------
# FIX1: namespace collision with wiki_atlas - foreign-generator refusal and
# own-stale-only pruning
# ---------------------------------------------------------------------------


def _frontmatter(generator: str | None) -> str:
    lines = ["---"]
    if generator is not None:
        lines.append(f"generator: {generator}")
    lines.append("---")
    lines.append("")
    lines.append("# Page")
    lines.append("")
    return "\n".join(line for line in lines if line is not None)


class TestNamespaceCollisionGuards:
    def test_refuses_to_overwrite_a_foreign_generators_page(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "some-concept.md").write_text(_frontmatter("wiki_atlas.py"), encoding="utf-8")

        with pytest.raises(SystemExit, match=r"wiki_atlas\.py"):
            write_pages({"some-concept.md": "new content"}, out_dir)

        # nothing was overwritten by the aborted run
        assert "wiki_atlas.py" in (out_dir / "some-concept.md").read_text(encoding="utf-8")

    def test_prunes_only_its_own_stale_pages(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        # own-stale: our generator wrote it, but it is not in this run's set
        (out_dir / "stale-own.md").write_text(_frontmatter(wc.GENERATOR_NAME), encoding="utf-8")
        # foreign: a different generator's page, must survive untouched
        (out_dir / "foreign.md").write_text(_frontmatter("wiki_atlas.py"), encoding="utf-8")
        # current: part of this run's page set, gets (re)written
        current_content = _frontmatter(wc.GENERATOR_NAME)

        write_pages({"current.md": current_content}, out_dir)

        assert not (out_dir / "stale-own.md").exists()
        assert (out_dir / "foreign.md").exists()
        assert (out_dir / "current.md").exists()

    def test_never_touches_a_page_with_no_readable_generator_stamp(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "hand-written.md").write_text("# Just a note\n", encoding="utf-8")

        write_pages({"current.md": _frontmatter(wc.GENERATOR_NAME)}, out_dir)

        assert (out_dir / "hand-written.md").exists()


# ---------------------------------------------------------------------------
# Second review round FIX9: ownership is FAIL CLOSED - overwrite only on an
# exact GENERATOR_NAME match; every other ownership state (absent, empty,
# malformed, foreign, or unreadable frontmatter) refuses instead of falling
# through to a silent overwrite.
# ---------------------------------------------------------------------------


class TestFailClosedOwnership:
    def test_unstamped_human_file_colliding_with_current_page_refuses_and_preserves_bytes(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        original = "# My own hand-written note\n\nDo not touch this, please.\n"
        (out_dir / "some-concept.md").write_text(original, encoding="utf-8")

        with pytest.raises(SystemExit):
            write_pages({"some-concept.md": "generator-produced content"}, out_dir)

        assert (out_dir / "some-concept.md").read_text(encoding="utf-8") == original

    def test_malformed_unterminated_frontmatter_refuses_and_preserves_bytes(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        # Opens a frontmatter block but never closes it - `_frontmatter_generator`
        # cannot find the closing `---` and returns None, the same "no signal"
        # outcome as no frontmatter at all.
        malformed = "---\ngenerator: wiki_concepts.py\n\n# No closing delimiter above\n"
        (out_dir / "some-concept.md").write_text(malformed, encoding="utf-8")

        with pytest.raises(SystemExit):
            write_pages({"some-concept.md": "generator-produced content"}, out_dir)

        assert (out_dir / "some-concept.md").read_text(encoding="utf-8") == malformed

    def test_empty_generator_value_refuses(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        malformed = "---\ngenerator:\n---\n\n# Page\n"
        (out_dir / "some-concept.md").write_text(malformed, encoding="utf-8")

        with pytest.raises(SystemExit):
            write_pages({"some-concept.md": "generator-produced content"}, out_dir)

        assert (out_dir / "some-concept.md").read_text(encoding="utf-8") == malformed

    def test_own_stamped_existing_page_is_overwritten_normally(self, tmp_path):
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        (out_dir / "some-concept.md").write_text(_frontmatter(wc.GENERATOR_NAME), encoding="utf-8")

        write_pages({"some-concept.md": "brand new content"}, out_dir)

        assert (out_dir / "some-concept.md").read_text(encoding="utf-8") == "brand new content"


# ---------------------------------------------------------------------------
# FIX3: as_mentioned fallback requires a whole-token match, not a substring
# ---------------------------------------------------------------------------


class TestAsMentionedWholeTokenFallback:
    def test_short_substring_inside_an_unrelated_word_does_not_match(self):
        text = "## H\n\n* **S**\n  - they said maintain discipline (3:00)\n"
        entries = parse_mindmap_timestamps(text)
        # "AI" is a substring of "maintain" and "said" but must not anchor here
        assert find_timestamp(entries, branch="Nope", as_mentioned="AI") is None

    def test_genuine_whole_token_still_resolves(self):
        text = "## H\n\n* **S**\n  - they discussed context engineering deeply (4:15)\n"
        entries = parse_mindmap_timestamps(text)
        assert find_timestamp(entries, branch="Nope", as_mentioned="context") == 255

    def test_boundary_rejects_a_substring_even_at_minimum_length(self):
        # "test" (4 chars, meets the length floor) is a substring of
        # "contest" but not a whole token there.
        text = "## H\n\n* **S**\n  - the contest result surprised everyone (1:00)\n"
        entries = parse_mindmap_timestamps(text)
        assert find_timestamp(entries, branch="Nope", as_mentioned="test") is None


# ---------------------------------------------------------------------------
# FIX4: markdown escaping for creator-authored text
# ---------------------------------------------------------------------------


class TestMarkdownEscaping:
    def test_title_with_pipe_and_brackets_renders_one_intact_row_and_a_working_link(self, tmp_path):
        corpus = tmp_path / "corpus"
        title = "GPT-5 | Full Breakdown [2026]"
        _write_video(
            corpus / "alpha",
            "2026-01-01-video",
            concepts=[_concept("shared.thing", "Shared Thing")],
            meta=_meta("vid-a", title, "2026-01-01", "alpha"),
            mindmap_text=MINDMAP_A,
        )
        for i, channel in enumerate(["beta", "gamma"], start=2):
            _write_video(
                corpus / channel,
                f"2026-0{i}-01-video",
                concepts=[_concept("shared.thing", "Shared Thing")],
                meta=_meta(f"vid-{channel}", f"Video {channel}", f"2026-0{i}-01", channel),
                mindmap_text=MINDMAP_A,
            )
        pages = build_pages(corpus, top_n=20)
        concept_page = next(content for name, content in pages.items() if name != f"{MOC_BASENAME}.md")

        table_rows = [line for line in concept_page.splitlines() if line.startswith("| 2026-01-01")]
        assert len(table_rows) == 1, "the escaped title must not split into extra table cells/rows"
        row = table_rows[0]
        assert "GPT-5 \\| Full Breakdown \\[2026\\]" in row
        # 4 structural separators (leading/trailing + 2 interior) plus the
        # one escaped pipe from the title - the escaping keeps it a literal
        # character, it does not remove it.
        assert row.count("|") == 5
        assert "](https://www.youtube.com/watch?v=vid-a" in row


# ---------------------------------------------------------------------------
# FIX5: sibling discovery survives glob metacharacters in the prefix
# ---------------------------------------------------------------------------


class TestSiblingDiscoveryWithGlobMetacharacters:
    def test_bracketed_prefix_finds_meta_and_mindmap_siblings(self, tmp_path):
        corpus = tmp_path / "corpus"
        channel_dir = corpus / "alpha"
        channel_dir.mkdir(parents=True)
        prefix = "talk [final]"
        _write_video(
            channel_dir,
            prefix,
            concepts=[_concept("x.y", "X Y")],
            meta=_meta("v1", "T", "2026-01-01", "alpha"),
            mindmap_text=MINDMAP_A,
        )
        cpath = channel_dir / f"{prefix}.concepts.json"
        sibs = sibling_artifacts(cpath)
        assert sibs[".meta.json"] == channel_dir / f"{prefix}.meta.json"
        assert sibs[".mindmap.md"] == channel_dir / f"{prefix}.mindmap.md"


# ---------------------------------------------------------------------------
# FIX6: unreadable/unparseable files are logged and counted
# ---------------------------------------------------------------------------


class TestSkippedFileTracking:
    def test_corrupt_concepts_json_logs_a_warning_and_is_counted(self, tmp_path, caplog):
        reset_skip_tracking()
        channel_dir = tmp_path / "corpus" / "alpha"
        channel_dir.mkdir(parents=True)
        bad = channel_dir / "2026-01-01-broken.concepts.json"
        bad.write_text("{not json", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="wiki_concepts"):
            records = collect_corpus_videos(channel_dir.parent)

        assert records == []
        assert skipped_file_count() == 1
        assert any(str(bad) in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# FIX7: slug collisions and the reserved MOC basename
# ---------------------------------------------------------------------------


class TestSlugCollisionsAndReservedMocBasename:
    def test_colliding_concept_ids_get_disambiguated_slugs_in_rank_order(self):
        slug_for = wc._assign_slugs(["dom.pact", "dom_pact"])
        assert slug_for["dom.pact"] == "dom-pact"
        assert slug_for["dom_pact"] == "dom-pact-2"

    def test_moc_basename_cannot_be_claimed_by_a_concept(self):
        slug_for = wc._assign_slugs(["concept.pages"])
        assert slug_for["concept.pages"] != MOC_BASENAME
        assert slug_for["concept.pages"] == f"{MOC_BASENAME}-2"


# ---------------------------------------------------------------------------
# FIX8: --top validation, video_id dedup, generated_from privacy
# ---------------------------------------------------------------------------


class TestTopValidation:
    def test_top_zero_refuses(self, monkeypatch, tmp_path, capsys):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        monkeypatch.setattr(sys, "argv", ["wiki_concepts.py", "--corpus", str(corpus), "--top", "0"])
        with pytest.raises(SystemExit):
            wc.main()
        assert "--top" in capsys.readouterr().err

    def test_top_negative_refuses(self, monkeypatch, tmp_path, capsys):
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        monkeypatch.setattr(sys, "argv", ["wiki_concepts.py", "--corpus", str(corpus), "--top", "-1"])
        with pytest.raises(SystemExit):
            wc.main()
        assert "--top" in capsys.readouterr().err


class TestDuplicateVideoDedup:
    def test_co_occurrence_counts_a_title_rotation_duplicate_once(self, tmp_path):
        corpus = tmp_path / "corpus"
        # same video_id, two concepts.json files (title-rotation duplicate,
        # pre-`dedupe --apply`), both listing the same concept pair
        _write_video(
            corpus / "alpha",
            "2026-01-01-old-title",
            concepts=[_concept("a.one", "A One"), _concept("a.two", "A Two")],
            meta=_meta("dup-vid", "Old Title", "2026-01-01", "alpha"),
        )
        _write_video(
            corpus / "alpha",
            "2026-02-01-new-title",
            concepts=[_concept("a.one", "A One"), _concept("a.two", "A Two")],
            meta=_meta("dup-vid", "New Title", "2026-02-01", "alpha"),
        )
        records = collect_corpus_videos(corpus)
        co = build_co_occurrence(records, {"a.one", "a.two"})
        assert co["a.one"]["a.two"] == 1  # one real video, not two

    def test_select_stable_concepts_dedups_channel_membership_by_video_id(self):
        # A single video mis-filed under three channel-folder names (the
        # residual state before `dedupe --apply` runs) must not spoof
        # cross-channel stability - it is still ONE video.
        mentions_by_concept = {
            "solo.spoof": [
                _fake_mention("solo.spoof", channel="alpha", video_id="dup-vid"),
                _fake_mention("solo.spoof", channel="beta", video_id="dup-vid"),
                _fake_mention("solo.spoof", channel="gamma", video_id="dup-vid"),
            ],
        }
        assert select_stable_concepts(mentions_by_concept, min_channels=3) == []


class TestGeneratedFromPrivacy:
    def test_generated_from_stores_only_the_corpus_directory_name(self, tmp_path):
        corpus = _build_corpus_with_related_concepts(tmp_path / "my-private-corpus-name")
        pages = build_pages(corpus, top_n=20)
        for content in pages.values():
            assert f"generated_from: {corpus.name}" in content
            assert str(corpus) not in content
            assert str(tmp_path) not in content


# ---------------------------------------------------------------------------
# Second review round FIX10: rendering (concept page + MOC) is
# video_id-normalized, mirroring the dedup `select_stable_concepts` already
# applies. A video mis-filed under two channel-folder names (a
# title-rotation duplicate not yet cleaned by `dedupe --apply`) must be
# counted once, and rendered under exactly one channel heading, everywhere.
# ---------------------------------------------------------------------------


def _build_corpus_with_video_misfiled_under_two_channels(tmp_path: Path) -> Path:
    """One video_id ('dup-vid') present under BOTH 'alpha' and 'zulu'
    channel-folder names (title-rotation residue), plus two more genuinely
    distinct videos in 'beta' and 'gamma' carrying the same concept - so the
    concept clears the 3-channel stability bar via {alpha, beta, gamma}
    (the canonical channel for 'dup-vid' is 'alpha', the lexicographically
    smaller of {alpha, zulu}) while still exercising the duplicate-video
    rendering path."""
    corpus = tmp_path / "corpus"
    _write_video(
        corpus / "alpha",
        "2026-01-01-dup",
        concepts=[_concept("shared.thing", "Shared Thing", branch="Shared Topic")],
        meta=_meta("dup-vid", "Duplicated Video", "2026-01-01", "alpha"),
        mindmap_text=MINDMAP_A,
    )
    _write_video(
        corpus / "zulu",
        "2026-01-02-dup-retitled",
        concepts=[_concept("shared.thing", "Shared Thing", branch="Shared Topic")],
        meta=_meta("dup-vid", "Duplicated Video (retitled)", "2026-01-02", "zulu"),
        mindmap_text=MINDMAP_A,
    )
    for i, channel in enumerate(["beta", "gamma"], start=1):
        _write_video(
            corpus / channel,
            f"2026-0{i}-05-video",
            concepts=[_concept("shared.thing", "Shared Thing", branch="Shared Topic")],
            meta=_meta(f"vid-{channel}", f"Video {channel}", f"2026-0{i}-05", channel),
            mindmap_text=MINDMAP_A,
        )
    return corpus


class TestRenderingIsVideoIdNormalized:
    def test_concept_page_header_and_moc_agree_on_deduped_counts(self, tmp_path):
        corpus = _build_corpus_with_video_misfiled_under_two_channels(tmp_path)
        pages = build_pages(corpus, top_n=20)
        concept_page = next(content for name, content in pages.items() if name != f"{MOC_BASENAME}.md")
        index = pages[f"{MOC_BASENAME}.md"]

        # 3 distinct videos (dup-vid counted once) across 3 distinct
        # channels (alpha, beta, gamma - "zulu" collapses into "alpha").
        assert "## Member videos (3 across 3 channels)" in concept_page
        assert "3 videos across 3 channels" in index

    def test_duplicated_video_appears_under_exactly_one_channel_heading(self, tmp_path):
        corpus = _build_corpus_with_video_misfiled_under_two_channels(tmp_path)
        pages = build_pages(corpus, top_n=20)
        concept_page = next(content for name, content in pages.items() if name != f"{MOC_BASENAME}.md")

        assert concept_page.count("Duplicated Video") == 1
        # canonical channel is "alpha" (lexicographically smaller than "zulu")
        assert "### alpha" in concept_page
        assert "### zulu" not in concept_page

    def test_select_stable_concepts_and_rendering_agree_on_video_count(self, tmp_path):
        corpus = _build_corpus_with_video_misfiled_under_two_channels(tmp_path)
        records = collect_corpus_videos(corpus)
        mentions = build_mentions(records)
        selected = select_stable_concepts(mentions, top_n=20)
        assert selected == ["shared.thing"]

        canonical = wc.canonical_channel_by_video(mentions["shared.thing"])
        assert len(canonical) == 3
        assert canonical["dup-vid"] == "alpha"
