"""Tests for scripts/atlas_build.py, the data layer (issue #260, unit U2).

Contract under test:
- `build_rows` returns one row per walker record with the seventeen R11 keys
  in schema order, built by explicit projection: free-text meta fields never
  reach a row;
- the synopsis is the mindmap's `##` headings only, and the generator never
  opens a `.transcript.md` file (transcript presence is a listing fact);
- deep links follow R7 through `timestamp_utils.youtube_deep_link`, and a URL
  is never built from an id (R9);
- `transcript_quality` follows the R8 order exactly;
- reading never writes: the corpus tree is byte-identical after a build;
- untrusted text is stripped of controls and bidi overrides, NFC-normalized,
  capped at 200 characters, with `<` kept literal (KTD9);
- missing or malformed optional inputs degrade to stable warning codes (R5).
"""

from __future__ import annotations

import builtins
import json
import logging
import pathlib
import re

import pytest

import atlas_build as ab
from timestamp_utils import timestamp_tolerance

ROW_KEYS = [
    "video_id",
    "title",
    "channel",
    "channels",
    "published",
    "duration_seconds",
    "url",
    "transcript_quality",
    "transcript_flag_count",
    "synopsis",
    "deep_links",
    "concept_ids",
    "topics",
    "in_briefings",
    "briefing_count",
    "wiki",
    "aliases",
]

SENTINEL = "SENTINEL-TRANSCRIPT-TEXT-7f3a"


def yt(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _video(
    corpus,
    channel,
    prefix,
    video_id,
    *,
    title=None,
    published="2026-01-01",
    url="default",
    mindmap=None,
    concepts=None,
    transcript=None,
    extra=None,
    meta=None,
):
    """Write one synthetic video's artifacts under `corpus/channel`."""
    channel_dir = corpus / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    if meta is None:
        meta = {"video_id": video_id, "title": title if title is not None else prefix, "channel": channel}
        if published is not None:
            meta["published"] = published
        if url == "default":
            meta["video_url"] = yt(video_id)
        elif url is not None:
            meta["video_url"] = url
        meta.update(extra or {})
    (channel_dir / f"{prefix}.meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if mindmap is not None:
        (channel_dir / f"{prefix}.mindmap.md").write_text(mindmap, encoding="utf-8")
    if concepts is not None:
        body = concepts if isinstance(concepts, str) else json.dumps(concepts)
        (channel_dir / f"{prefix}.concepts.json").write_text(body, encoding="utf-8")
    if transcript is not None:
        (channel_dir / f"{prefix}.transcript.md").write_text(transcript, encoding="utf-8")


def _concepts(*ids, as_mentioned="pact"):
    return {"concepts": [{"concept_id": cid, "as_mentioned": as_mentioned, "branch": "A"} for cid in ids]}


def _write_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")


def _happy_corpus(root: pathlib.Path) -> pathlib.Path:
    corpus = root / "video-intel"
    _video(
        corpus,
        "chanA",
        "2026-02-01-a1",
        "aaaaaaaaaa1",
        title="Agents / tools, part 1",
        published="2026-02-01",
        mindmap=f"# Root\n\n## Branch One\n\n* **Sub**\n  - first point {SENTINEL} (1:05)\n\n## Branch Two\n  - x (2:00)\n",
        concepts=_concepts("dom.pact", "dom.loop", as_mentioned=SENTINEL),
        transcript=f"[00:00] {SENTINEL}\n",
        extra={"transcript_status": "complete", "duration_seconds": 600},
    )
    _video(
        corpus,
        "chanB",
        "2026-01-15-b1",
        "bbbbbbbbbb1",
        title="Агенти и алати",
        published="2026-01-15",
        concepts=_concepts("dom.pact"),
    )
    _video(corpus, "chanC", "2026-03-01-c1", "ccccccccccc", published="2026-03-01")
    _video(corpus, "chanD", "2026-01-15-d1", "aaaaaaaaaa0", published="2026-01-15")
    _write_json(
        corpus / "taxonomy.json",
        {
            "concepts": {
                "dom.pact": {"preferred_label": "Pact Pattern"},
                "dom.loop": {"preferred_label": "Agent Loop"},
                "dom.unused": {"preferred_label": "Never Referenced"},
            }
        },
    )
    _write_json(
        corpus / "topics.json",
        {"topics": {"sales": {"videos": [{"video_id": "aaaaaaaaaa1"}]}}},
    )
    return corpus


def _walk_values(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk_values(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_values(v)
    else:
        yield value


def _row(result, video_id):
    return next(r for r in result.rows if r["video_id"] == video_id)


class TestHappyPath:
    def test_rows_have_the_seventeen_keys_in_order(self, tmp_path):
        result = ab.build_rows(_happy_corpus(tmp_path))
        assert len(result.rows) == 4
        for row in result.rows:
            assert list(row) == ROW_KEYS

    def test_rows_sorted_by_published_then_video_id(self, tmp_path):
        result = ab.build_rows(_happy_corpus(tmp_path))
        assert [r["video_id"] for r in result.rows] == ["aaaaaaaaaa0", "bbbbbbbbbb1", "aaaaaaaaaa1", "ccccccccccc"]

    def test_titles_keep_cyrillic_and_slash(self, tmp_path):
        result = ab.build_rows(_happy_corpus(tmp_path))
        assert _row(result, "bbbbbbbbbb1")["title"] == "Агенти и алати"
        assert _row(result, "aaaaaaaaaa1")["title"] == "Agents / tools, part 1"

    def test_concepts_lookup_holds_only_referenced_ids(self, tmp_path):
        result = ab.build_rows(_happy_corpus(tmp_path))
        assert result.concepts == {"dom.loop": "Agent Loop", "dom.pact": "Pact Pattern"}
        assert _row(result, "aaaaaaaaaa1")["concept_ids"] == ["dom.pact", "dom.loop"]

    def test_summary_counts_and_unthreaded_share(self, tmp_path):
        summary = ab.build_rows(_happy_corpus(tmp_path)).summary
        assert summary["channels"] == {"chanA": 1, "chanB": 1, "chanC": 1, "chanD": 1}
        assert summary["topics"] == {"sales": 1}
        assert summary["unthreaded"] == 3
        assert summary["unthreaded_share"] == 0.75
        assert (summary["metas"], summary["rows"], summary["duplicates"]) == (4, 4, 0)
        assert (summary["skipped"], summary["no_id"], summary["excluded_metas"]) == (0, 0, 0)
        assert summary["warnings"] == []
        assert _row(ab.build_rows(_happy_corpus(tmp_path / "again")), "aaaaaaaaaa1")["topics"] == ["sales"]

    def test_max_published_and_processed(self, tmp_path):
        corpus = _happy_corpus(tmp_path)
        _video(
            corpus,
            "chanE",
            "2026-01-02-e1",
            "eeeeeeeeeee",
            published="2026-01-02",
            extra={"processed": "2026-04-01T10:00:00+02:00"},
        )
        _video(corpus, "chanE", "2026-01-03-e2", "eeeeeeeeee2", extra={"processed": "2026-03-01T00:00:00"})
        _video(corpus, "chanE", "2026-01-04-e3", "eeeeeeeeee3", extra={"processed": "not a date"})
        result = ab.build_rows(corpus)
        assert result.max_published == "2026-03-01"
        assert result.max_processed == "2026-04-01T08:00:00+00:00"

    def test_empty_corpus_has_null_share(self, tmp_path):
        result = ab.build_rows(tmp_path)
        assert result.rows == []
        assert result.summary["unthreaded_share"] is None
        assert result.max_processed is None and result.max_published is None


class TestSynopsisAndTranscript:
    def test_synopsis_is_exactly_the_branch_headings_in_order(self, tmp_path):
        result = ab.build_rows(_happy_corpus(tmp_path))
        assert _row(result, "aaaaaaaaaa1")["synopsis"] == ["Branch One", "Branch Two"]
        assert _row(result, "bbbbbbbbbb1")["synopsis"] == []

    def test_transcript_sentinel_absent_from_every_value(self, tmp_path):
        result = ab.build_rows(_happy_corpus(tmp_path))
        for row in result.rows:
            for value in _walk_values(row):
                assert SENTINEL not in str(value)
        assert SENTINEL not in json.dumps(result.concepts, ensure_ascii=False)
        assert SENTINEL not in json.dumps(result.summary, ensure_ascii=False)

    def test_transcript_file_is_never_opened(self, tmp_path, monkeypatch):
        corpus = _happy_corpus(tmp_path)
        touched: list[str] = []
        real_read_text = pathlib.Path.read_text
        real_read_bytes = pathlib.Path.read_bytes
        real_path_open = pathlib.Path.open
        real_open = builtins.open

        def spy_read_text(self, *a, **k):
            touched.append(str(self))
            return real_read_text(self, *a, **k)

        def spy_read_bytes(self, *a, **k):
            touched.append(str(self))
            return real_read_bytes(self, *a, **k)

        def spy_path_open(self, *a, **k):
            touched.append(str(self))
            return real_path_open(self, *a, **k)

        def spy_open(file, *a, **k):
            touched.append(str(file))
            return real_open(file, *a, **k)

        monkeypatch.setattr(pathlib.Path, "read_text", spy_read_text)
        monkeypatch.setattr(pathlib.Path, "read_bytes", spy_read_bytes)
        monkeypatch.setattr(pathlib.Path, "open", spy_path_open)
        monkeypatch.setattr(builtins, "open", spy_open)
        result = ab.build_rows(corpus)
        # Companion: the spy sees real reads, so an empty `touched` cannot pass.
        assert any(p.endswith(".mindmap.md") for p in touched)
        assert any(p.endswith(".meta.json") for p in touched)
        assert not [p for p in touched if p.endswith(".transcript.md")]
        # Presence is still known from the listing.
        assert _row(result, "aaaaaaaaaa1")["transcript_quality"] == "complete"


class TestReadingNeverWrites:
    def test_corpus_tree_is_byte_identical_after_build(self, tmp_path):
        corpus = _happy_corpus(tmp_path)
        (corpus / "chanA" / "2026-05-01-bad.meta.json").write_bytes(b"\xff\xfe not json")
        _write_json(corpus / "chanB" / "2026-01-15-b1.concepts.json", {"concepts": "nope"})
        _write_json(corpus / "_briefings" / "b.md", "---\nvideo_ids: [bbbbbbbbbb1]\n---\nbody\n")
        _write_json(corpus / "_wiki" / "creators" / "chanA.md", "---\ntype: creator\ncreator: chanA\n---\n")

        def snapshot():
            return {
                str(p.relative_to(corpus)): (p.read_bytes() if p.is_file() else None) for p in sorted(corpus.rglob("*"))
            }

        before = snapshot()
        ab.build_rows(corpus)
        assert snapshot() == before


class TestDeepLinks:
    VID = "dQw4w9WgXcQ"

    def _build(self, tmp_path, mindmap, *, url="default", duration=None):
        corpus = tmp_path / "c"
        extra = {} if duration is None else {"duration_seconds": duration}
        _video(corpus, "chanA", "2026-01-01-v", self.VID, mindmap=mindmap, url=url, extra=extra)
        return ab.build_rows(corpus).rows[0]

    def test_one_leaf_per_branch_distinct_seconds(self, tmp_path):
        mindmap = "## A\n  - one (4:05)\n  - two (4:05)\n  - three (9:38)\n## B\n  - four (3:15)\n"
        row = self._build(tmp_path, mindmap)
        assert row["deep_links"] == [
            {"label": "A", "seconds": 245, "url": f"{yt(self.VID)}&t=245"},
            {"label": "B", "seconds": 195, "url": f"{yt(self.VID)}&t=195"},
        ]

    def test_seconds_already_used_by_an_earlier_branch_are_skipped(self, tmp_path):
        mindmap = "## A\n  - one (4:05)\n## B\n  - same second (4:05)\n  - later (5:00)\n"
        row = self._build(tmp_path, mindmap)
        assert [(d["label"], d["seconds"]) for d in row["deep_links"]] == [("A", 245), ("B", 300)]

    def test_leaf_past_duration_plus_tolerance_uses_next_leaf(self, tmp_path):
        duration = 600
        limit = duration + timestamp_tolerance(duration)
        late = f"{limit // 60}:{limit % 60 + 1:02d}"
        mindmap = f"## A\n  - late ({late})\n  - ok (5:00)\n"
        row = self._build(tmp_path, mindmap, duration=duration)
        assert row["deep_links"] == [{"label": "A", "seconds": 300, "url": f"{yt(self.VID)}&t=300"}]

    def test_leaf_exactly_at_the_limit_is_kept(self, tmp_path):
        duration = 600
        limit = duration + timestamp_tolerance(duration)
        mindmap = f"## A\n  - edge ({limit // 60}:{limit % 60:02d})\n"
        row = self._build(tmp_path, mindmap, duration=duration)
        assert [d["seconds"] for d in row["deep_links"]] == [limit]

    def test_no_stamps_gives_empty(self, tmp_path):
        assert self._build(tmp_path, "## A\n  - no stamp here\n")["deep_links"] == []

    def test_non_youtube_url_gives_empty(self, tmp_path):
        row = self._build(tmp_path, "## A\n  - one (4:05)\n", url="https://fathom.video/share/x")
        assert row["deep_links"] == []
        assert row["url"] == "https://fathom.video/share/x"

    def test_cap_of_three_on_a_four_branch_mindmap(self, tmp_path):
        mindmap = "## A\n  - a (0:10)\n## B\n  - b (0:20)\n## C\n  - c (0:30)\n## D\n  - d (0:40)\n"
        row = self._build(tmp_path, mindmap)
        assert [d["label"] for d in row["deep_links"]] == ["A", "B", "C"]


class TestUrlRule:
    def _url_row(self, tmp_path, video_id, url):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-v", video_id, url=url, mindmap="## A\n  - a (0:10)\n")
        return ab.build_rows(corpus).rows[0]

    def test_youtube_url_kept_verbatim(self, tmp_path):
        row = self._url_row(tmp_path, "dQw4w9WgXcQ", yt("dQw4w9WgXcQ"))
        assert row["url"] == yt("dQw4w9WgXcQ")
        assert row["deep_links"]

    def test_goldcast_id_url_kept_without_deep_links(self, tmp_path):
        url = "https://www.youtube.com/watch?v=goldcast-ab12cd34"
        row = self._url_row(tmp_path, "goldcast-ab12cd34", url)
        assert row["url"] == url
        assert row["deep_links"] == []

    @pytest.mark.parametrize("url", ["http://x", None, "https://", "not a url", 42])
    def test_non_https_or_absent_gives_null(self, tmp_path, url):
        row = self._url_row(tmp_path, "dQw4w9WgXcQ", url)
        assert row["url"] is None
        assert row["deep_links"] == []


class TestTranscriptQuality:
    CASES = (
        (
            "v-severe",
            {"transcript_status": "complete", "transcript_quality_flags": ["monolithic_severe"]},
            False,
            "severe",
        ),
        ("v-trunc", {"transcript_status": "truncated_output"}, False, "truncated"),
        ("v-partial", {"transcript_status": "partial"}, False, "partial"),
        ("v-ok", {"transcript_status": "ok"}, False, "complete"),
        ("v-complete", {"transcript_status": "complete"}, False, "complete"),
        ("v-skipped", {"skip_modes": ["transcript"], "skip_reason": "SKIPREASON-CANARY"}, False, "skipped"),
        ("v-unknown", {"transcript_status": "weird-literal"}, False, "unknown"),
        ("v-sibling", {}, True, "unknown"),
        ("v-none", {}, False, "none"),
    )

    def test_quality_mapping_in_order(self, tmp_path):
        corpus = tmp_path / "c"
        for vid, extra, has_transcript, _ in self.CASES:
            _video(corpus, "chanA", f"2026-01-01-{vid}", vid, extra=extra, transcript="t\n" if has_transcript else None)
        result = ab.build_rows(corpus)
        for vid, _, _, expected in self.CASES:
            assert _row(result, vid)["transcript_quality"] == expected, vid
        dumped = json.dumps(result.rows)
        assert "skip_reason" not in dumped
        assert "SKIPREASON-CANARY" not in dumped

    def test_flag_count_counts_only_string_entries(self, tmp_path):
        corpus = tmp_path / "c"
        flags = ["timestamp_overrun_outlier_mild", {"x": 1}, 7, "blind_gap_mild", None]
        _video(corpus, "chanA", "2026-01-01-a", "a", extra={"transcript_quality_flags": flags})
        _video(corpus, "chanA", "2026-01-01-b", "b", extra={"transcript_quality_flags": "monolithic_severe"})
        result = ab.build_rows(corpus)
        assert _row(result, "a")["transcript_flag_count"] == 2
        assert _row(result, "b")["transcript_flag_count"] == 0


class TestWikiBriefingsAndPaths:
    def test_wiki_keys_from_page_frontmatter(self, tmp_path):
        corpus = tmp_path / "My Drive" / "video-intel"
        _video(corpus, "chanA", "2026-01-01-a", "aaaaaaaaaaa", concepts=_concepts("x.y", "x.z", "x.none"))
        _video(corpus, "chanB", "2026-01-01-b", "bbbbbbbbbbb", concepts=_concepts("x.y"), title="a/b/c")
        _write_json(corpus / "_wiki" / "creators" / "chanA.md", "---\ntype: creator\ncreator: chanA\n---\n# chanA\n")
        _write_json(
            corpus / "_wiki" / "concept-pages" / "x-y.md",
            "---\ngenerator: wiki_concepts.py\nconcept_id: x.y\n---\n# x.y\n",
        )
        _write_json(corpus / "_wiki" / "concepts" / "x-z.md", "---\ntype: concept\nconcept: x.z\n---\n# x.z\n")
        _write_json(corpus / "_briefings" / "sales" / "b1.md", "---\nvideo_ids: [aaaaaaaaaaa]\n---\n")
        _write_json(corpus / "_briefings" / "b2.md", "---\nvideo_ids: [aaaaaaaaaaa, aaaaaaaaaaa]\n---\n")
        result = ab.build_rows(corpus)
        a, b = _row(result, "aaaaaaaaaaa"), _row(result, "bbbbbbbbbbb")
        assert a["wiki"] == {"creator": "chanA", "concepts": ["x.y", "x.z"]}
        assert b["wiki"] == {"creator": None, "concepts": ["x.y"]}
        assert (a["in_briefings"], a["briefing_count"]) == (True, 2)
        assert (b["in_briefings"], b["briefing_count"]) == (False, 0)
        assert b["title"] == "a/b/c"
        drive = re.compile(r"\b[A-Za-z]:[\\/]")
        for row in result.rows:
            for value in _walk_values(row):
                text = str(value)
                assert str(corpus) not in text
                assert str(tmp_path) not in text
                assert "My Drive" not in text
                assert not drive.search(text)


class TestFreeTextMetaFields:
    def test_canaries_never_reach_rows(self, tmp_path):
        fields = [
            "description",
            "last_error",
            "prompt",
            "model",
            "note",
            "skip_reason",
            "meta_cleanup_note",
            "transcript_warning",
            "transcript_failover_reason",
        ]
        extra = {name: f"CANARY-{name}" for name in fields}
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "aaaaaaaaaaa", extra=extra)
        dumped = json.dumps(ab.build_rows(corpus).rows)
        assert "CANARY-" not in dumped


class TestUntrustedText:
    def test_heading_keeps_lt_drops_bidi_and_is_capped(self, tmp_path):
        long_heading = "L" * 300
        mindmap = f"## Intro <script>x</script> ‮evil\n  - a (0:10)\n## {long_heading}\n  - b (0:20)\n"
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "dQw4w9WgXcQ", mindmap=mindmap, title="T⁦itle\x07 [x](y)")
        row = ab.build_rows(corpus).rows[0]
        assert row["synopsis"][0] == "Intro <script>x</script> evil"
        assert row["synopsis"][1] == "L" * 200
        assert row["deep_links"][0]["label"] == "Intro <script>x</script> evil"
        assert row["title"] == "Title [x](y)"

    def test_title_is_nfc_normalized(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", title="Café")
        assert ab.build_rows(corpus).rows[0]["title"] == "Café"

    def test_missing_title_falls_back_to_prefix_with_warning(self, tmp_path, caplog):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", meta={"video_id": "a", "title": 5})
        with caplog.at_level(logging.WARNING):
            row = ab.build_rows(corpus).rows[0]
        assert row["title"] == "2026-01-01-a"
        assert "title" in caplog.text


class TestTypeHardening:
    def test_int_and_null_published_build_and_sort_first(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "zzz", published="2026-01-01")
        _video(corpus, "chanA", "2026-01-01-b", "bbb", published=20260101)
        _video(corpus, "chanA", "2026-01-01-c", "aaa", published=None)
        _video(corpus, "chanA", "2026-01-01-d", "ddd", published="2026-13-45T00:00")
        result = ab.build_rows(corpus)
        assert [(r["video_id"], r["published"]) for r in result.rows] == [
            ("aaa", ""),
            ("bbb", ""),
            ("ddd", ""),
            ("zzz", "2026-01-01"),
        ]

    def test_published_datetime_is_cut_to_date(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", published="2026-01-01T12:00:00Z")
        assert ab.build_rows(corpus).rows[0]["published"] == "2026-01-01"

    @pytest.mark.parametrize(
        ("value", "expected"), [(True, None), (False, None), ("600", None), (6.5, None), (600, 600)]
    )
    def test_duration_is_int_or_null(self, tmp_path, value, expected):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", extra={"duration_seconds": value})
        assert ab.build_rows(corpus).rows[0]["duration_seconds"] == expected

    @pytest.mark.parametrize(
        "concepts",
        [
            {"concepts": "nope"},
            {"concepts": ["x.y", 3]},
            {"concepts": [{"concept_id": 7}]},
            {"no_concepts_key": True},
        ],
    )
    def test_malformed_concepts_give_empty_list_and_code(self, tmp_path, concepts):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", concepts=concepts)
        _video(corpus, "chanA", "2026-01-01-b", "b", concepts={"concepts": 5})
        result = ab.build_rows(corpus)
        assert _row(result, "a")["concept_ids"] == []
        assert result.summary["warnings"].count("concepts_malformed") == 1

    def test_mixed_concepts_keep_good_ids(self, tmp_path):
        corpus = tmp_path / "c"
        mixed = {"concepts": [{"concept_id": "x.y"}, "junk", {"concept_id": 3}]}
        _video(corpus, "chanA", "2026-01-01-a", "a", concepts=mixed)
        result = ab.build_rows(corpus)
        assert _row(result, "a")["concept_ids"] == ["x.y"]
        assert "concepts_malformed" in result.summary["warnings"]

    def test_absent_concepts_file_is_silent(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a")
        result = ab.build_rows(corpus)
        assert _row(result, "a")["concept_ids"] == []
        assert "concepts_malformed" not in result.summary["warnings"]

    def test_meta_only_video_yields_a_row(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", meta={"video_id": "a"})
        row = ab.build_rows(corpus).rows[0]
        assert list(row) == ROW_KEYS
        assert row["synopsis"] == [] and row["deep_links"] == [] and row["url"] is None

    def test_unreadable_mindmap_is_skipped_and_warned(self, tmp_path, caplog):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a")
        (corpus / "chanA" / "2026-01-01-a.mindmap.md").write_bytes(b"## A\n\xff\xfe\n")
        with caplog.at_level(logging.WARNING):
            result = ab.build_rows(corpus)
        assert result.rows[0]["synopsis"] == []
        assert any(p.endswith(".mindmap.md") for p in result.skipped_files)
        assert "mindmap" in caplog.text


class TestMissingInputs:
    def test_no_taxonomy_labels_fall_back_to_ids(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", concepts=_concepts("x.y"))
        result = ab.build_rows(corpus)
        assert result.concepts == {"x.y": "x.y"}
        assert "taxonomy_missing" in result.summary["warnings"]
        assert "topics_missing" in result.summary["warnings"]

    def test_malformed_taxonomy(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", concepts=_concepts("x.y"))
        _write_json(corpus / "taxonomy.json", "[1, 2]")
        result = ab.build_rows(corpus)
        assert result.concepts == {"x.y": "x.y"}
        assert "taxonomy_malformed" in result.summary["warnings"]

    def test_malformed_topics_gives_empty_topics(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a")
        _write_json(corpus / "topics.json", "{not json")
        result = ab.build_rows(corpus)
        assert result.rows[0]["topics"] == []
        assert "topics_malformed" in result.summary["warnings"]
        assert "topics_missing" not in result.summary["warnings"]
        assert result.summary["unthreaded"] == 1

    def test_topics_wrong_shape_is_malformed(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a")
        _write_json(corpus / "topics.json", {"topics": ["sales"]})
        result = ab.build_rows(corpus)
        assert result.rows[0]["topics"] == []
        assert "topics_malformed" in result.summary["warnings"]

    def test_topics_sorted_slugs(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a")
        _write_json(
            corpus / "topics.json",
            {"topics": {"zeta": {"videos": [{"video_id": "a"}]}, "alpha": {"videos": [{"video_id": "a"}]}}},
        )
        result = ab.build_rows(corpus)
        assert result.rows[0]["topics"] == ["alpha", "zeta"]
        assert result.summary["topics"] == {"alpha": 1, "zeta": 1}


class TestWalkerFields:
    def test_channel_and_aliases_come_from_the_walker(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", title="New title", mindmap="## A\n", concepts=_concepts("x"))
        _video(corpus, "chanB", "2026-01-01-a-old", "a", title="Old title")
        _video(corpus, "chanC", "2026-01-01-c", "c", meta={"video_id": "c", "channel": 5})
        result = ab.build_rows(corpus)
        a = _row(result, "a")
        assert (a["channel"], a["channels"], a["aliases"]) == ("chanA", ["chanA", "chanB"], ["Old title"])
        assert _row(result, "c")["channel"] == "chanC"
        assert result.summary["duplicates"] == 1
