"""Tests for scripts/atlas_build.py, the data layer (issue #260, unit U2).

Contract under test:
- `build_rows` returns one row per walker record with the nineteen R11 keys
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
    "transcript_source",
    "synopsis",
    "deep_links",
    "concept_ids",
    "topics",
    "in_briefings",
    "briefing_count",
    "wiki",
    "aliases",
    "in_search_index",
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
        extra={"transcript_status": "complete", "duration_seconds": 600, "transcript_source": "gemini"},
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
    def test_rows_have_the_nineteen_keys_in_order(self, tmp_path):
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
        # The source comes from meta.json alone, with the transcript still unopened.
        assert _row(result, "aaaaaaaaaa1")["transcript_source"] == "gemini"


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
        # Precedence: a severe flag outranks any status, a status outranks skip_modes.
        (
            "v-severe-partial",
            {"transcript_status": "partial", "transcript_quality_flags": ["blind_gap_severe"]},
            False,
            "severe",
        ),
        (
            "v-severe-trunc",
            {"transcript_status": "truncated_output", "transcript_quality_flags": ["monolithic_severe"]},
            False,
            "severe",
        ),
        ("v-skip-partial", {"skip_modes": ["transcript"], "transcript_status": "partial"}, False, "partial"),
        ("v-legacy-skip", {"skip": True}, False, "skipped"),
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

    def test_a_scalar_flags_field_does_not_abort_the_build(self, tmp_path):
        corpus = tmp_path / "c"
        _video(
            corpus, "chanA", "2026-01-01-a", "a", extra={"transcript_status": "partial", "transcript_quality_flags": 7}
        )
        _video(corpus, "chanA", "2026-01-01-b", "b", extra={"transcript_status": "complete"})
        result = ab.build_rows(corpus)
        assert len(result.rows) == 2
        assert _row(result, "a")["transcript_quality"] == "partial"
        assert _row(result, "a")["transcript_flag_count"] == 0

    def test_flag_count_counts_only_string_entries(self, tmp_path):
        corpus = tmp_path / "c"
        flags = ["timestamp_overrun_outlier_mild", {"x": 1}, 7, "blind_gap_mild", None]
        _video(corpus, "chanA", "2026-01-01-a", "a", extra={"transcript_quality_flags": flags})
        _video(corpus, "chanA", "2026-01-01-b", "b", extra={"transcript_quality_flags": "monolithic_severe"})
        result = ab.build_rows(corpus)
        assert _row(result, "a")["transcript_flag_count"] == 2
        assert _row(result, "b")["transcript_flag_count"] == 0

    def test_transcript_source_is_carried_verbatim(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a", extra={"transcript_source": "youtube_captions"})
        _video(corpus, "chanA", "2026-01-01-b", "b", extra={"transcript_source": "gemini"})
        result = ab.build_rows(corpus)
        assert _row(result, "a")["transcript_source"] == "youtube_captions"
        assert _row(result, "b")["transcript_source"] == "gemini"

    def test_missing_empty_or_non_string_source_is_null(self, tmp_path):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "2026-01-01-a", "a")
        _video(corpus, "chanA", "2026-01-01-b", "b", extra={"transcript_source": ""})
        _video(corpus, "chanA", "2026-01-01-c", "c", extra={"transcript_source": 7})
        _video(corpus, "chanA", "2026-01-01-d", "d", extra={"transcript_source": ["gemini"]})
        result = ab.build_rows(corpus)
        for vid in ("a", "b", "c", "d"):
            row = _row(result, vid)
            assert "transcript_source" in row, vid
            assert row["transcript_source"] is None, vid


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


class TestNonUtf8Briefing:
    def test_build_rows_and_main_complete_over_a_torn_briefing(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _write_json(corpus / "_briefings" / "good.md", "---\nvideo_ids: [bbbbbbbbbb1]\n---\n")
        torn = corpus / "_briefings" / "torn.md"
        torn.write_bytes(b"---\nvideo_ids: [aaaaaaaaaa1]\ntitle: \xd0\n---\n")
        result = ab.build_rows(corpus)
        assert _row(result, "bbbbbbbbbb1")["briefing_count"] == 1
        assert _row(result, "aaaaaaaaaa1")["briefing_count"] == 0
        assert str(torn) in result.skipped_files
        code, _ = _run_main(capsys, corpus)
        assert code == 0
        assert _atlas_path(corpus).is_file()


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


# ---------------------------------------------------------------------------
# U5: writer, CLI, schema, determinism
# ---------------------------------------------------------------------------

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "schemas" / "atlas.schema.json"
HEADER_KEYS = [
    "schema_version",
    "generator",
    "generator_version",
    "generated_from",
    "generated_at",
    "summary",
    "concepts",
    "rows",
]
FIXED_STAMP = "2026-10-28T00:00:00Z"
STDOUT_RE = re.compile(
    r"atlas_build: wrote atlas\.json to (?P<where>\S+) "
    r"\(metas=(?P<metas>\d+) rows=(?P<rows>\d+) duplicates=(?P<duplicates>\d+) "
    r"skipped=(?P<skipped>\d+) no_id=(?P<no_id>\d+) excluded_metas=(?P<excluded>\d+) "
    r"unthreaded=(?P<unthreaded>\d+) \((?P<share>n/a|\d+\.\d%)\) warnings=(?P<warnings>\d+)\)"
)


def _run_main(capsys, corpus, *extra):
    code = ab.main(["--corpus", str(corpus), *extra])
    out = capsys.readouterr().out
    return code, out


def _atlas_path(corpus):
    return corpus / "_atlas" / "atlas.json"


def _parse_line(out):
    lines = out.splitlines()
    assert len(lines) == 1, lines
    match = STDOUT_RE.fullmatch(lines[0])
    assert match, lines[0]
    return match


def _partitions(match):
    n = {k: int(match[k]) for k in ("metas", "rows", "duplicates", "skipped", "no_id")}
    return n["metas"] == n["rows"] + n["duplicates"] + n["skipped"] + n["no_id"]


class TestCli:
    def test_happy_path_writes_file_and_one_counts_only_line(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        code, out = _run_main(capsys, corpus)
        assert code == 0
        assert _atlas_path(corpus).is_file()
        match = _parse_line(out)
        assert match["where"] == "video-intel/_atlas"
        assert (match["metas"], match["rows"], match["duplicates"]) == ("4", "4", "0")
        assert (match["skipped"], match["no_id"], match["excluded"]) == ("0", "0", "0")
        assert (match["unthreaded"], match["share"], match["warnings"]) == ("3", "75.0%", "0")
        assert _partitions(match)
        for leak in ("chanA", "chanB", "chanC", "chanD", "sales", str(tmp_path), "Agents", "Агенти"):
            assert leak not in out

    def test_partition_holds_with_duplicates_and_no_id(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _video(corpus, "chanE", "2026-01-20-dup", "aaaaaaaaaa1", published="2026-01-20")
        _video(corpus, "chanE", "2026-01-21-noid", "x", meta={"title": "no id here"})
        (corpus / "chanE" / "2026-01-22-bad.meta.json").write_text("{not json", encoding="utf-8")
        _, out = _run_main(capsys, corpus)
        match = _parse_line(out)
        assert int(match["metas"]) == 7
        assert (match["duplicates"], match["no_id"], match["skipped"]) == ("1", "1", "1")
        assert _partitions(match)

    def test_empty_corpus_still_prints_and_partitions(self, tmp_path, capsys):
        corpus = tmp_path / "empty"
        corpus.mkdir()
        code, out = _run_main(capsys, corpus)
        assert code == 0
        match = _parse_line(out)
        assert (match["metas"], match["rows"], match["share"]) == ("0", "0", "n/a")
        assert _partitions(match)
        assert json.loads(_atlas_path(corpus).read_bytes())["rows"] == []

    def test_missing_corpus_exits_non_zero(self, tmp_path):
        with pytest.raises(SystemExit) as exc:
            ab.main(["--corpus", str(tmp_path / "nope")])
        assert exc.value.code not in (0, None)
        assert "corpus not found" in str(exc.value.code)

    def test_out_flag_redirects_the_file(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        out_dir = tmp_path / "elsewhere"
        _, out = _run_main(capsys, corpus, "--out", str(out_dir))
        assert (out_dir / ab.ATLAS_FILENAME).is_file()
        assert not _atlas_path(corpus).exists()
        # Outside the corpus only the folder's own name is printed, never a
        # path that would hint at where the corpus or the copy lives.
        assert _parse_line(out)["where"] == "elsewhere"

    def test_out_inside_the_corpus_keeps_the_corpus_relative_form(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _, out = _run_main(capsys, corpus, "--out", str(corpus / "_scratch"))
        assert _parse_line(out)["where"] == "video-intel/_scratch"

    def test_output_file_set_is_only_atlas_json(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        atlas_dir = corpus / "_atlas"
        atlas_dir.mkdir()
        (atlas_dir / "atlas-policy.yaml").write_text("owner: me\n", encoding="utf-8")
        _run_main(capsys, corpus)
        names = sorted(p.name for p in atlas_dir.iterdir())
        assert names == ["atlas-policy.yaml", "atlas.json"]
        for name in names:
            for suffix in (".meta.json", ".concepts.json", ".mindmap.md", ".transcript.md"):
                assert not name.endswith(suffix)


REFUSED_RE = re.compile(
    r"atlas_build: did not write atlas\.json: (?P<n>\d+) channel folder\(s\) could not be listed "
    r"\(metas=\d+ rows=\d+ duplicates=\d+ skipped=\d+ no_id=\d+ excluded_metas=\d+ unlistable=(?P<unlistable>\d+)\)"
)


def _deny_listing(monkeypatch, folder_name):
    """Make `os.scandir` raise PermissionError for one folder, everything else real."""
    real = ab.os.scandir

    def scandir(path="."):
        if pathlib.Path(path).name == folder_name:
            raise PermissionError(13, "Access is denied", str(path))
        return real(path)

    monkeypatch.setattr(ab.os, "scandir", scandir)


class TestUnlistableChannel:
    def test_an_unlistable_channel_keeps_the_previous_catalog_and_exits_2(self, tmp_path, capsys, monkeypatch):
        # Falsified by removing main's early return: the catalog was rewritten
        # without chanB, with a valid-looking partition, and this went red.
        corpus = _happy_corpus(tmp_path)
        _run_main(capsys, corpus, "--generated-at", FIXED_STAMP)
        before = _atlas_path(corpus).read_bytes()
        _deny_listing(monkeypatch, "chanB")
        code, out = _run_main(capsys, corpus, "--generated-at", FIXED_STAMP)
        assert code == 2
        assert _atlas_path(corpus).read_bytes() == before
        assert sorted(p.name for p in (corpus / "_atlas").iterdir()) == ["atlas.json"]
        lines = out.splitlines()
        assert len(lines) == 1, lines
        match = REFUSED_RE.fullmatch(lines[0])
        assert match, lines[0]
        assert (match["n"], match["unlistable"]) == ("1", "1")
        assert "chanB" not in out

    def test_build_rows_reports_the_code_and_the_count(self, tmp_path, monkeypatch):
        corpus = _happy_corpus(tmp_path)
        _deny_listing(monkeypatch, "chanB")
        result = ab.build_rows(corpus)
        assert result.summary["unlistable_channels"] == 1
        assert "channel_unlistable" in result.summary["warnings"]
        assert "bbbbbbbbbb1" not in {r["video_id"] for r in result.rows}

    def test_an_unlistable_excluded_folder_is_counted_apart_and_still_writes(self, tmp_path, capsys, monkeypatch):
        corpus = _happy_corpus(tmp_path)
        (corpus / "_quarantine").mkdir()
        _deny_listing(monkeypatch, "_quarantine")
        code, out = _run_main(capsys, corpus)
        assert code == 0
        doc = json.loads(_atlas_path(corpus).read_bytes())
        assert (doc["summary"]["unlistable_channels"], doc["summary"]["unlistable_excluded"]) == (0, 1)
        assert "excluded_unlistable" in doc["summary"]["warnings"]
        assert _parse_line(out)

    def test_a_healthy_build_reports_zero_unlistable(self, tmp_path):
        summary = ab.build_rows(_happy_corpus(tmp_path)).summary
        assert (summary["unlistable_channels"], summary["unlistable_excluded"]) == (0, 0)


class TestDeterminism:
    def test_two_runs_with_fixed_stamp_are_byte_identical(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _run_main(capsys, corpus, "--generated-at", FIXED_STAMP)
        first = _atlas_path(corpus).read_bytes()
        _run_main(capsys, corpus, "--generated-at", FIXED_STAMP)
        assert _atlas_path(corpus).read_bytes() == first
        assert json.loads(first)["generated_at"] == FIXED_STAMP

    def test_generated_at_string_is_verbatim(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _run_main(capsys, corpus, "--generated-at", "whenever, really")
        assert json.loads(_atlas_path(corpus).read_bytes())["generated_at"] == "whenever, really"

    def test_default_stamp_is_max_processed_in_utc(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _video(corpus, "chanE", "2026-01-02-e1", "eeeeeeeeeee", extra={"processed": "2026-04-01T10:00:00+02:00"})
        _video(corpus, "chanE", "2026-01-03-e2", "eeeeeeeeee2", extra={"processed": "2026-03-01T00:00:00"})
        _run_main(capsys, corpus)
        first = _atlas_path(corpus).read_bytes()
        assert json.loads(first)["generated_at"] == "2026-04-01T08:00:00+00:00"
        _run_main(capsys, corpus)
        assert _atlas_path(corpus).read_bytes() == first

    def test_no_processed_falls_back_to_max_published(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _video(corpus, "chanE", "2026-01-02-e1", "eeeeeeeeeee", extra={"processed": "not a date"})
        _run_main(capsys, corpus)
        assert json.loads(_atlas_path(corpus).read_bytes())["generated_at"] == "2026-03-01"

    def test_neither_gives_null(self, tmp_path, capsys):
        corpus = tmp_path / "c"
        _video(corpus, "chanA", "x1", "aaaaaaaaaa1", published=None)
        _run_main(capsys, corpus)
        assert json.loads(_atlas_path(corpus).read_bytes())["generated_at"] is None

    def test_module_never_reads_the_wall_clock(self):
        # One clock read is allowed: `_file_age_seconds`, which only decides
        # whether a leftover temp is old enough to sweep and never reaches the
        # output. Every other line of the module is scanned.
        import ast

        source = pathlib.Path(ab.__file__).read_text(encoding="utf-8")
        assert "json.dumps" in source  # non-vacuity: the scan reads the real module
        helper = next(
            n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "_file_age_seconds"
        )
        allowed = set(range(helper.lineno, helper.end_lineno + 1))
        lines = source.splitlines()
        clock_lines = [i for i, line in enumerate(lines, 1) if "time.time(" in line]
        assert clock_lines and set(clock_lines) <= allowed, clock_lines
        outside = "\n".join(line for i, line in enumerate(lines, 1) if i not in allowed)
        for call in ("now(", "utcnow(", "today(", "time.time(", "monotonic("):
            assert call not in outside, call


class TestBytes:
    def _bytes(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _run_main(capsys, corpus, "--generated-at", FIXED_STAMP)
        return corpus, _atlas_path(corpus).read_bytes()

    def test_lf_only_and_one_trailing_newline(self, tmp_path, capsys):
        _, data = self._bytes(tmp_path, capsys)
        assert b"\r\n" not in data
        assert data.endswith(b"}\n") and not data.endswith(b"\n\n")

    def test_cyrillic_is_raw_utf8(self, tmp_path, capsys):
        _, data = self._bytes(tmp_path, capsys)
        assert "Агенти и алати".encode() in data
        assert b"\\u04" not in data

    def test_nan_is_rejected_before_the_file_is_touched(self, tmp_path, capsys, monkeypatch):
        corpus, before = self._bytes(tmp_path, capsys)
        real = ab.build_rows

        def with_nan(path, indexed=None):
            result = real(path, indexed)
            result.rows[0]["duration_seconds"] = float("nan")
            return result

        monkeypatch.setattr(ab, "build_rows", with_nan)
        with pytest.raises(ValueError):
            ab.main(["--corpus", str(corpus)])
        assert _atlas_path(corpus).read_bytes() == before
        assert [p.name for p in (corpus / "_atlas").iterdir()] == ["atlas.json"]

    def test_strings_are_nfc_normalized(self):
        doc = {"rows": [], "k": ["Café"]}
        assert "Café".encode() in ab.serialize_atlas(doc)
        assert "Café".encode() not in ab.serialize_atlas(doc)

    def test_unsorted_rows_are_refused_not_resorted(self):
        rows = [{"published": "2026-02-01", "video_id": "b"}, {"published": "2026-01-01", "video_id": "a"}]
        with pytest.raises(ValueError, match="sorted"):
            ab.serialize_atlas({"rows": rows})


class TestKeyOrder:
    def test_header_and_row_keys_in_schema_order(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _run_main(capsys, corpus)
        pairs_seen = []

        def hook(pairs):
            pairs_seen.append([k for k, _ in pairs])
            return dict(pairs)

        doc = json.loads(_atlas_path(corpus).read_bytes(), object_pairs_hook=hook)
        assert pairs_seen[-1] == HEADER_KEYS
        assert list(doc) == HEADER_KEYS
        row_key_lists = [keys for keys in pairs_seen if keys and keys[0] == "video_id"]
        assert len(row_key_lists) == 4
        assert all(keys == ROW_KEYS for keys in row_key_lists)
        assert list(doc["concepts"]) == sorted(doc["concepts"])
        assert list(doc["summary"]["channels"]) == sorted(doc["summary"]["channels"])
        assert doc["generated_from"] == "video-intel"
        assert doc["generator"] == ab.GENERATOR_NAME == "atlas_build.py"
        assert (doc["schema_version"], doc["generator_version"]) == (1, "1")


class TestOwnership:
    def _existing(self, corpus, writer):
        atlas_dir = corpus / "_atlas"
        atlas_dir.mkdir(parents=True)
        target = atlas_dir / "atlas.json"
        writer(target)
        return target

    @pytest.mark.parametrize(
        "body",
        [
            '{"generator": "wiki_concepts.py", "rows": []}',
            '[{"generator": "atlas_build.py"}]',
            '{"rows": []}',
            "{not json",
        ],
        ids=["foreign-generator", "json-list", "missing-key", "not-json"],
    )
    def test_refuses_and_leaves_bytes_untouched(self, tmp_path, monkeypatch, body):
        corpus = _happy_corpus(tmp_path)
        target = self._existing(corpus, lambda p: p.write_text(body, encoding="utf-8"))
        before = target.read_bytes()
        walks = []
        real_build_rows = ab.build_rows

        def spy_build_rows(path):
            walks.append(path)
            return real_build_rows(path)

        monkeypatch.setattr(ab, "build_rows", spy_build_rows)
        with pytest.raises(SystemExit) as exc:
            ab.main(["--corpus", str(corpus)])
        # Refused BEFORE the walk: a foreign file must not cost a full corpus read.
        assert walks == []
        assert exc.value.code not in (0, None)
        assert "refusing to overwrite" in str(exc.value.code)
        assert ab.GENERATOR_NAME in str(exc.value.code)
        assert target.read_bytes() == before
        assert sorted(p.name for p in target.parent.iterdir()) == ["atlas.json"]

    def test_refuses_an_unreadable_target(self, tmp_path):
        corpus = _happy_corpus(tmp_path)
        target = self._existing(corpus, lambda p: p.mkdir())
        with pytest.raises(SystemExit) as exc:
            ab.main(["--corpus", str(corpus)])
        assert exc.value.code not in (0, None)
        assert "refusing to overwrite" in str(exc.value.code)
        assert target.is_dir()

    def test_own_file_is_overwritten(self, tmp_path):
        corpus = _happy_corpus(tmp_path)
        target = self._existing(corpus, lambda p: p.write_text('{"generator": "atlas_build.py"}', encoding="utf-8"))
        assert ab.main(["--corpus", str(corpus)]) == 0
        assert len(json.loads(target.read_bytes())["rows"]) == 4

    def test_own_file_larger_than_the_meta_cap_is_overwritten(self, tmp_path):
        # The live catalog is 6.1 MB, past the 4 MB per-meta default cap: the
        # ownership read must use the atlas cap, or the next rebuild is refused.
        import corpus_walk as cw

        corpus = _happy_corpus(tmp_path)
        filler = [{"video_id": f"v{i:07d}", "pad": "x" * 200} for i in range(cw.DEFAULT_MAX_JSON_BYTES // 200 + 10)]
        body = json.dumps({"generator": ab.GENERATOR_NAME, "rows": filler})
        target = self._existing(corpus, lambda p: p.write_text(body, encoding="utf-8"))
        assert target.stat().st_size > cw.DEFAULT_MAX_JSON_BYTES
        assert ab.main(["--corpus", str(corpus)]) == 0
        assert len(json.loads(target.read_bytes())["rows"]) == 4


class TestAtomicity:
    def _seed(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _run_main(capsys, corpus, "--generated-at", FIXED_STAMP)
        return corpus, _atlas_path(corpus).read_bytes()

    def test_serializer_failure_keeps_previous_file_and_leaves_no_temp(self, tmp_path, capsys, monkeypatch):
        corpus, before = self._seed(tmp_path, capsys)

        def boom(document):
            raise RuntimeError("serializer failed")

        monkeypatch.setattr(ab, "serialize_atlas", boom)
        with pytest.raises(RuntimeError):
            ab.main(["--corpus", str(corpus)])
        assert _atlas_path(corpus).read_bytes() == before
        assert [p.name for p in (corpus / "_atlas").iterdir()] == ["atlas.json"]

    def test_replace_failure_removes_the_temp(self, tmp_path, capsys, monkeypatch):
        corpus, before = self._seed(tmp_path, capsys)
        calls = []

        def fail_replace(src, dst):
            calls.append((pathlib.Path(src), pathlib.Path(dst)))
            raise OSError("replace failed")

        monkeypatch.setattr(ab.os, "replace", fail_replace)
        with pytest.raises(OSError):
            ab.main(["--corpus", str(corpus)])
        assert len(calls) == 1
        src, dst = calls[0]
        assert ab.TEMP_SUFFIX_PATTERN.fullmatch(src.name) and src.parent == dst.parent
        assert _atlas_path(corpus).read_bytes() == before
        assert [p.name for p in (corpus / "_atlas").iterdir()] == ["atlas.json"]

    @staticmethod
    def _age(path, seconds):
        stamp = path.stat().st_mtime - seconds
        ab.os.utime(path, (stamp, stamp))

    def test_leftover_temp_is_swept_and_nothing_else(self, tmp_path, capsys):
        corpus, _ = self._seed(tmp_path, capsys)
        atlas_dir = corpus / "_atlas"
        kept = ["notes.tmp", "atlas-policy.yaml", "atlas.json.tmp", "atlas.json.12x.tmp"]
        for name in ("atlas.json.12345.tmp", *kept):
            (atlas_dir / name).write_text("x", encoding="utf-8")
            self._age(atlas_dir / name, ab.STALE_TEMP_MIN_AGE_SECONDS + 60)
        _run_main(capsys, corpus)
        assert sorted(p.name for p in atlas_dir.iterdir()) == sorted(["atlas.json", *kept])

    def test_a_young_temp_is_left_alone(self, tmp_path, capsys):
        # A temp this fresh may be a concurrent build's live write.
        corpus, _ = self._seed(tmp_path, capsys)
        young = corpus / "_atlas" / "atlas.json.12345.tmp"
        young.write_text("x", encoding="utf-8")
        code, _ = _run_main(capsys, corpus)
        assert code == 0
        assert young.read_text(encoding="utf-8") == "x"

    def test_an_old_temp_is_removed(self, tmp_path, capsys):
        corpus, _ = self._seed(tmp_path, capsys)
        old = corpus / "_atlas" / "atlas.json.12345.tmp"
        old.write_text("x", encoding="utf-8")
        self._age(old, ab.STALE_TEMP_MIN_AGE_SECONDS + 60)
        _run_main(capsys, corpus)
        assert not old.exists()

    def test_an_unlink_that_raises_is_logged_and_the_build_continues(self, tmp_path, capsys, monkeypatch, caplog):
        corpus, _ = self._seed(tmp_path, capsys)
        old = corpus / "_atlas" / "atlas.json.12345.tmp"
        old.write_text("x", encoding="utf-8")
        self._age(old, ab.STALE_TEMP_MIN_AGE_SECONDS + 60)
        real_unlink = pathlib.Path.unlink

        def unlink(self, *a, **k):
            if self.name == old.name:
                raise PermissionError(13, "Access is denied", str(self))
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "unlink", unlink)
        with caplog.at_level(logging.WARNING, logger="video_intel.atlas_build"):
            code, out = _run_main(capsys, corpus)
        assert code == 0
        assert _parse_line(out)
        assert old.exists()
        assert any("atlas.json.12345.tmp" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    def test_an_unlistable_out_dir_is_logged_not_raised(self, tmp_path, monkeypatch, caplog):
        out_dir = tmp_path / "_atlas"
        out_dir.mkdir()
        _deny_listing(monkeypatch, "_atlas")
        with caplog.at_level(logging.WARNING, logger="video_intel.atlas_build"):
            ab.sweep_stale_temps(out_dir)
        assert any("_atlas" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    def test_temp_pattern_shape(self):
        assert ab.TEMP_SUFFIX_PATTERN.fullmatch("atlas.json.12345.tmp")
        for name in ("atlas.json.tmp", "xatlas.json.1.tmp", "atlas.json.1.tmp.bak", "notes.tmp", "atlas.json.12x.tmp"):
            assert not ab.TEMP_SUFFIX_PATTERN.fullmatch(name), name


def _schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


_JSON_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _resolve(prop, schema):
    while "$ref" in prop:
        prop = schema["$defs"][prop["$ref"].rsplit("/", 1)[-1]]
    return prop


def _structural_row_errors(row, row_schema, schema=None):
    """A stdlib check of one row against the schema's own row definition."""
    schema = schema or _schema()
    errors = []
    required = row_schema["required"]
    if list(row) != required:
        errors.append(f"keys {list(row)} != {required}")
    for key, value in row.items():
        prop = row_schema["properties"].get(key)
        if prop is None:
            errors.append(f"extra key {key}")
            continue
        types = _resolve(prop, schema).get("type")
        assert types, f"schema property {key} has no type; the structural check would be vacuous"
        types = [types] if isinstance(types, str) else types
        if not any(_JSON_TYPES[t](value) for t in types):
            errors.append(f"{key}: {value!r} not {types}")
    return errors


class TestSchema:
    def _doc(self, tmp_path, capsys):
        corpus = _happy_corpus(tmp_path)
        _run_main(capsys, corpus)
        return json.loads(_atlas_path(corpus).read_bytes())

    def test_schema_orders_match_the_module(self):
        schema = _schema()
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["required"] == HEADER_KEYS == list(schema["properties"])
        row = schema["$defs"]["row"]
        assert row["required"] == ROW_KEYS == list(ab.ROW_KEYS) == list(row["properties"])
        assert row["additionalProperties"] is False
        for plain in (row["properties"]["title"], row["properties"]["synopsis"]["items"]):
            assert plain["contentMediaType"] == "text/plain"
        assert schema["$defs"]["deep_link"]["properties"]["label"]["contentMediaType"] == "text/plain"

    def test_structural_check_passes_every_written_row(self, tmp_path, capsys):
        doc = self._doc(tmp_path, capsys)
        row_schema = _schema()["$defs"]["row"]
        assert doc["rows"]
        for row in doc["rows"]:
            assert _structural_row_errors(row, row_schema) == []

    def test_structural_check_rejects_an_extra_key_and_a_bad_type(self, tmp_path, capsys):
        row = self._doc(tmp_path, capsys)["rows"][0]
        row_schema = _schema()["$defs"]["row"]
        assert _structural_row_errors({**row, "description": "leak"}, row_schema)
        assert _structural_row_errors({**row, "briefing_count": "3"}, row_schema)

    def test_written_file_validates_under_jsonschema(self, tmp_path, capsys):
        jsonschema = pytest.importorskip("jsonschema")
        doc = self._doc(tmp_path, capsys)
        jsonschema.Draft202012Validator.check_schema(_schema())
        jsonschema.Draft202012Validator(_schema()).validate(doc)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda row: row.pop("video_id"),
            lambda row: row.update(transcript_quality="great"),
            lambda row: row.update(description="leak"),
        ],
        ids=["missing-video-id", "unknown-quality", "extra-key"],
    )
    def test_jsonschema_rejects_a_bad_row(self, tmp_path, capsys, mutate):
        jsonschema = pytest.importorskip("jsonschema")
        doc = self._doc(tmp_path, capsys)
        mutate(doc["rows"][0])
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(_schema()).validate(doc)


# ---------------------------------------------------------------------------
# Docs and skill parity (U4): every documented flag is a real flag
# ---------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parent.parent
#: The two places a reader is told how to run the catalog build.
DOCS_WITH_INVOCATIONS = ("docs/atlas.md", "skills/video-intel/SKILL.md")
#: One invocation: the script name, then tokens up to a backtick, a table pipe
#: or the end of the line (a code span or a fenced block both end there).
_INVOCATION_RE = re.compile(r"python\s+scripts/atlas_build\.py((?:[ \t]+[^\s`|]+)*)")


def _documented_invocations(text: str) -> list[list[str]]:
    """Every `atlas_build.py` invocation in `text`, as its argument tokens."""
    return [m.group(1).split() for m in _INVOCATION_RE.finditer(text)]


def _parser_flags() -> set[str]:
    return {opt for action in ab.build_parser()._actions for opt in action.option_strings}


class TestDocumentedFlagsExist:
    """A doc that tells the reader to pass a flag `build_parser()` lacks sends
    them straight into an argparse error. The case list comes from the DOCS,
    so a typo'd or retired flag in either file fails here by name."""

    @pytest.mark.parametrize("doc", DOCS_WITH_INVOCATIONS)
    def test_every_documented_flag_is_a_real_flag(self, doc):
        known = _parser_flags()
        invocations = _documented_invocations((REPO / doc).read_text(encoding="utf-8"))
        unknown = sorted({tok.split("=", 1)[0] for args in invocations for tok in args if tok.startswith("--")} - known)
        assert not unknown, f"{doc} documents atlas_build.py flags the parser does not have: {unknown}"

    @pytest.mark.parametrize("doc", DOCS_WITH_INVOCATIONS)
    def test_each_doc_has_at_least_one_invocation_with_corpus(self, doc):
        """Non-vacuity: an extractor that stops matching would turn the test
        above into `assert not set()` forever."""
        invocations = _documented_invocations((REPO / doc).read_text(encoding="utf-8"))
        assert invocations, f"no atlas_build.py invocation found in {doc}"
        assert any("--corpus" in args for args in invocations), f"no invocation in {doc} passes --corpus"

    def test_the_extractor_sees_a_bad_flag(self):
        """Positive control on synthetic text, independent of the real docs."""
        found = _documented_invocations("| `python scripts/atlas_build.py --corpus <dir> --bogus` | x --nope |")
        assert found == [["--corpus", "<dir>", "--bogus"]]
        assert "--bogus" not in _parser_flags()
        assert {"--corpus", "--out", "--generated-at"} <= _parser_flags()
