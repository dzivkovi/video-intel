"""Tests for scripts/corpus_walk.py (issue #260, unit U1).

Contract under test:
- every moved reader primitive is the SAME object in `video_intel` (and in
  `wiki_concepts` for `frontmatter_value`), so the move cannot leave a
  private copy behind to drift [core: one-definition];
- `read_json_best_effort` never writes or quarantines, skips and counts an
  existing-but-unusable file (torn UTF-8, a JSON list, a directory, over the
  size cap), and stays silent about an absent one [core: #124 read-guard];
- `parse_front_matter` imports PyYAML lazily, so importing the module keeps
  the standalone scripts off the yaml dependency;
- `briefing_video_counts` counts per briefing file, and `load_seen_video_ids`
  is exactly its key set.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest
import wiki_concepts as wc
import yaml

import corpus_walk as cw
import video_intel as vi

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "scripts"


# ---------------------------------------------------------------------------
# Identity: the move left one definition, re-exported
# ---------------------------------------------------------------------------

# (name bound in video_intel, name in corpus_walk)
_VIDEO_INTEL_REEXPORTS = [
    ("usable_video_id", "usable_video_id"),
    ("read_json_best_effort", "read_json_best_effort"),
    ("is_channel_dir", "is_channel_dir"),
    ("META_SUFFIX", "META_SUFFIX"),
    ("CONCEPTS_SUFFIX", "CONCEPTS_SUFFIX"),
    ("MINDMAP_SUFFIX", "MINDMAP_SUFFIX"),
    ("TRANSCRIPT_SUFFIX", "TRANSCRIPT_SUFFIX"),
    ("QUALITY_FLAG_MONOLITHIC_SEVERE", "QUALITY_FLAG_MONOLITHIC_SEVERE"),
    ("QUALITY_FLAG_BLIND_GAP_SEVERE", "QUALITY_FLAG_BLIND_GAP_SEVERE"),
    ("QUALITY_FLAG_BACKWARD_JUMP_SEVERE", "QUALITY_FLAG_BACKWARD_JUMP_SEVERE"),
    ("QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE", "QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE"),
    ("_SEVERE_QUALITY_FLAGS", "SEVERE_QUALITY_FLAGS"),
    ("transcript_quality_flags_are_severe", "transcript_quality_flags_are_severe"),
    ("_HEALTHY_TRANSCRIPT_STATUSES", "_HEALTHY_TRANSCRIPT_STATUSES"),
    ("TRANSCRIPT_STATUS_TRUNCATED", "TRANSCRIPT_STATUS_TRUNCATED"),
    ("is_skipped_meta", "is_skipped_meta"),
    ("parse_front_matter", "parse_front_matter"),
    ("briefing_video_counts", "briefing_video_counts"),
    ("load_seen_video_ids", "load_seen_video_ids"),
]


class TestMovedNamesAreOneObject:
    @pytest.mark.parametrize(("vi_name", "cw_name"), _VIDEO_INTEL_REEXPORTS)
    def test_video_intel_binds_the_corpus_walk_object(self, vi_name, cw_name):
        assert getattr(vi, vi_name) is getattr(cw, cw_name)

    def test_severe_flag_set_is_the_four_moved_constants(self):
        assert (
            frozenset(
                {
                    cw.QUALITY_FLAG_MONOLITHIC_SEVERE,
                    cw.QUALITY_FLAG_BLIND_GAP_SEVERE,
                    cw.QUALITY_FLAG_BACKWARD_JUMP_SEVERE,
                    cw.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE,
                }
            )
            == cw.SEVERE_QUALITY_FLAGS
        )

    def test_wiki_concepts_binds_the_corpus_walk_frontmatter_reader(self):
        assert wc.frontmatter_value is cw.frontmatter_value

    def test_no_private_copy_of_a_moved_function_is_defined_in_video_intel(self):
        # Identity alone passes if video_intel re-binds the name AFTER a local
        # def; this walk catches the local def itself.
        import ast

        tree = ast.parse((SCRIPTS_DIR / "video_intel.py").read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        moved = {
            "usable_video_id",
            "read_json_best_effort",
            "is_channel_dir",
            "transcript_quality_flags_are_severe",
            "is_skipped_meta",
            "parse_front_matter",
            "briefing_video_counts",
            "load_seen_video_ids",
        }
        assert defined & moved == set()
        # Companion: the walk sees top-level defs at all.
        assert "collect_corpus_videos" in defined


# ---------------------------------------------------------------------------
# read_json_best_effort
# ---------------------------------------------------------------------------


def _warnings(caplog):
    return [r for r in caplog.records if r.name == "video_intel.corpus_walk" and r.levelno == logging.WARNING]


class TestReadJsonBestEffort:
    def test_a_valid_object_is_returned(self, tmp_path):
        path = tmp_path / "a.meta.json"
        path.write_text(json.dumps({"video_id": "abc"}), encoding="utf-8")
        skipped: list[str] = []
        assert cw.read_json_best_effort(path, skipped) == {"video_id": "abc"}
        assert skipped == []

    def test_a_meta_torn_mid_character_is_skipped_counted_logged_and_never_quarantined(self, tmp_path, caplog):
        path = tmp_path / "torn.meta.json"
        raw = json.dumps({"video_id": "abc", "title": "Привет мир"}, ensure_ascii=False).encode("utf-8")
        cut = raw.index("П".encode()) + 1  # inside the two-byte sequence
        path.write_bytes(raw[:cut])
        skipped: list[str] = []
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            assert cw.read_json_best_effort(path, skipped) is None
        assert skipped == [str(path)]
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()
        # Reading never writes: no sidecar, the torn bytes untouched.
        assert sorted(p.name for p in tmp_path.iterdir()) == ["torn.meta.json"]
        assert path.read_bytes() == raw[:cut]

    def test_a_json_list_top_level_is_skipped_and_counted(self, tmp_path, caplog):
        path = tmp_path / "list.meta.json"
        path.write_text(json.dumps([{"video_id": "abc"}]), encoding="utf-8")
        skipped: list[str] = []
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            assert cw.read_json_best_effort(path, skipped) is None
        assert skipped == [str(path)]
        assert len(_warnings(caplog)) == 1

    @pytest.mark.parametrize("payload", ["42", '"hello"', "null", "true"])
    def test_a_scalar_top_level_is_skipped(self, tmp_path, payload):
        path = tmp_path / "scalar.meta.json"
        path.write_text(payload, encoding="utf-8")
        skipped: list[str] = []
        assert cw.read_json_best_effort(path, skipped) is None
        assert skipped == [str(path)]

    def test_an_absent_file_is_neither_counted_nor_logged(self, tmp_path, caplog):
        skipped: list[str] = []
        with caplog.at_level(logging.DEBUG, logger="video_intel.corpus_walk"):
            assert cw.read_json_best_effort(tmp_path / "missing.json", skipped) is None
        assert skipped == []
        assert [r for r in caplog.records if r.name == "video_intel.corpus_walk"] == []

    def test_a_directory_named_like_a_meta_is_skipped_as_an_os_error(self, tmp_path, caplog):
        path = tmp_path / "x.meta.json"
        path.mkdir()
        skipped: list[str] = []
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            assert cw.read_json_best_effort(path, skipped) is None
        assert skipped == [str(path)]
        assert len(_warnings(caplog)) == 1

    def test_a_file_over_the_size_cap_is_skipped(self, tmp_path):
        path = tmp_path / "big.meta.json"
        path.write_text(json.dumps({"video_id": "abc", "pad": "x" * 100}), encoding="utf-8")
        skipped: list[str] = []
        assert cw.read_json_best_effort(path, skipped, max_bytes=50) is None
        assert skipped == [str(path)]
        # The same file under a generous cap reads fine.
        assert cw.read_json_best_effort(path, [], max_bytes=10_000)["video_id"] == "abc"

    def test_each_bad_file_is_appended_once(self, tmp_path):
        bad = tmp_path / "bad.meta.json"
        bad.write_text("{not json", encoding="utf-8")
        skipped: list[str] = []
        cw.read_json_best_effort(bad, skipped)
        cw.read_json_best_effort(tmp_path / "missing.json", skipped)
        assert skipped == [str(bad)]


class TestReadTextBestEffort:
    def test_a_utf8_file_is_returned(self, tmp_path):
        path = tmp_path / "a.mindmap.md"
        path.write_bytes("## Привет\n".encode())
        skipped: list[str] = []
        assert cw.read_text_best_effort(path, skipped, max_bytes=1024) == "## Привет\n"
        assert skipped == []

    def test_an_absent_file_is_neither_counted_nor_logged(self, tmp_path, caplog):
        skipped: list[str] = []
        with caplog.at_level(logging.DEBUG, logger="video_intel.corpus_walk"):
            assert cw.read_text_best_effort(tmp_path / "missing.md", skipped, max_bytes=1024) is None
        assert skipped == []
        assert [r for r in caplog.records if r.name == "video_intel.corpus_walk"] == []

    def test_a_file_torn_mid_character_is_skipped_once_and_never_rewritten(self, tmp_path, caplog):
        path = tmp_path / "torn.mindmap.md"
        raw = "## Привет".encode()
        cut = raw.index("П".encode()) + 1  # inside the two-byte sequence
        path.write_bytes(raw[:cut])
        skipped: list[str] = []
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            assert cw.read_text_best_effort(path, skipped, max_bytes=1024) is None
        assert skipped == [str(path)]
        warnings = _warnings(caplog)
        assert len(warnings) == 1
        assert str(path) in warnings[0].getMessage()
        assert sorted(p.name for p in tmp_path.iterdir()) == ["torn.mindmap.md"]
        assert path.read_bytes() == raw[:cut]

    def test_errors_replace_reads_a_torn_file_instead_of_skipping_it(self, tmp_path):
        path = tmp_path / "torn.md"
        path.write_bytes(b"ok \xd0")
        skipped: list[str] = []
        assert cw.read_text_best_effort(path, skipped, max_bytes=1024, errors="replace") == "ok \ufffd"
        assert skipped == []

    def test_a_file_over_the_size_cap_is_skipped(self, tmp_path, caplog):
        path = tmp_path / "big.mindmap.md"
        path.write_text("x" * 100, encoding="utf-8")
        skipped: list[str] = []
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            assert cw.read_text_best_effort(path, skipped, max_bytes=50) is None
        assert skipped == [str(path)]
        assert len(_warnings(caplog)) == 1
        # The cap is inclusive: exactly max_bytes reads fine.
        assert cw.read_text_best_effort(path, [], max_bytes=100) == "x" * 100


# ---------------------------------------------------------------------------
# Small predicates
# ---------------------------------------------------------------------------


class TestIsChannelDir:
    def test_plain_directory_is_a_channel(self, tmp_path):
        (tmp_path / "alpha").mkdir()
        assert cw.is_channel_dir(tmp_path / "alpha")

    @pytest.mark.parametrize("name", ["_briefings", ".lancedb", "_standalone"])
    def test_dot_and_underscore_dirs_are_not_channels(self, tmp_path, name):
        (tmp_path / name).mkdir()
        assert not cw.is_channel_dir(tmp_path / name)

    def test_a_file_is_not_a_channel(self, tmp_path):
        (tmp_path / "taxonomy.json").write_text("{}", encoding="utf-8")
        assert not cw.is_channel_dir(tmp_path / "taxonomy.json")


class TestSevereFlagsRejectsANonList:
    @pytest.mark.parametrize("flags", [7, "monolithic_severe", {"monolithic_severe": True}, 1.5, True])
    def test_a_scalar_or_mapping_is_not_severe_and_does_not_raise(self, flags):
        # An int used to pass the `if not flags` guard and raise TypeError on
        # iteration, aborting the whole catalog build over one meta.
        assert cw.transcript_quality_flags_are_severe(flags) is False

    def test_a_list_is_still_judged(self):
        assert cw.transcript_quality_flags_are_severe(["monolithic_severe"]) is True
        assert cw.transcript_quality_flags_are_severe(["blind_gap_mild"]) is False
        assert cw.transcript_quality_flags_are_severe(None) is False
        assert cw.transcript_quality_flags_are_severe([]) is False

    def test_the_set_the_chunked_writer_passes_is_still_judged(self):
        # video_intel's chunked writer accumulates flags into a set before
        # sorting; a list-only check would read every chunked severe as healthy.
        assert cw.transcript_quality_flags_are_severe({"monolithic_severe", "blind_gap_mild"}) is True
        assert cw.transcript_quality_flags_are_severe({"blind_gap_mild"}) is False


class TestOneWinnerRule:
    def test_the_record_dict_counter_is_gone_on_purpose(self):
        # The winner rule had two definitions (a record-dict counter with no
        # production caller, and `_sibling_artifact_count`); a second copy is
        # how two rules drift [core: one-definition].
        assert not hasattr(cw, "artifact_count")
        assert not hasattr(cw, "_artifact_count")
        assert not hasattr(vi, "_artifact_count")
        assert cw.WINNER_ARTIFACT_SUFFIXES == (cw.MINDMAP_SUFFIX, cw.CONCEPTS_SUFFIX)


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


class TestFrontmatterValue:
    PAGE = "---\ngenerator: wiki_concepts.py\ncreator: alpha\nconcept_id: shared.thing\nconcept: Shared Thing\n---\n\n# Body\n"

    @pytest.mark.parametrize(
        ("key", "expected"),
        [("creator", "alpha"), ("concept_id", "shared.thing"), ("concept", "Shared Thing")],
    )
    def test_returns_the_scalar_for_a_key(self, key, expected):
        assert cw.frontmatter_value(self.PAGE, key) == expected

    def test_concept_does_not_match_concept_id(self):
        page = "---\nconcept_id: shared.thing\n---\n"
        assert cw.frontmatter_value(page, "concept") is None

    def test_missing_key_returns_none(self):
        assert cw.frontmatter_value(self.PAGE, "nope") is None

    def test_no_front_matter_returns_none(self):
        assert cw.frontmatter_value("# Just a page\ncreator: alpha\n", "creator") is None

    def test_unclosed_front_matter_returns_none(self):
        assert cw.frontmatter_value("---\ncreator: alpha\n", "creator") is None

    def test_empty_value_returns_none(self):
        assert cw.frontmatter_value("---\ncreator:\n---\n", "creator") is None

    @pytest.mark.parametrize(
        "page",
        [
            PAGE,
            "---\nconcept_id: x\n---\n",
            "# no frontmatter\ncreator: a\n",
            "---\ncreator: a\n",
            "---\ncreator:\n---\n",
        ],
    )
    def test_frontmatter_values_agrees_with_frontmatter_value_per_key(self, page):
        keys = ("creator", "concept_id", "concept", "nope")
        assert cw.frontmatter_values(page, keys) == {key: cw.frontmatter_value(page, key) for key in keys}


class TestParseFrontMatterIsLazyAboutYaml:
    def test_yaml_is_not_imported_by_importing_corpus_walk(self):
        child = textwrap.dedent("""
            import sys
            import corpus_walk
            print('yaml before call:', 'yaml' in sys.modules)
            fm, _ = corpus_walk.parse_front_matter('---\\nvideo_ids: [a]\\n---\\nbody')
            print('parsed:', fm)
            print('yaml after call:', 'yaml' in sys.modules)
            """)
        result = subprocess.run(
            [sys.executable, "-c", child], cwd=str(SCRIPTS_DIR), capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        assert "yaml before call: False" in result.stdout
        assert "parsed: {'video_ids': ['a']}" in result.stdout
        assert "yaml after call: True" in result.stdout


# ---------------------------------------------------------------------------
# Briefing counts
# ---------------------------------------------------------------------------


def _write_briefing(briefings_dir, name, video_ids):
    path = briefings_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    front = yaml.safe_dump({"artifact_type": "viewing_guide", "video_ids": video_ids})
    path.write_text(f"---\n{front}---\n\n# guide\n", encoding="utf-8")


class TestBriefingVideoCounts:
    def test_counts_per_briefing_file(self, tmp_path):
        briefings = tmp_path / "_briefings"
        _write_briefing(briefings, "one.md", ["a", "b", "b"])
        _write_briefing(briefings, "sales/two.md", ["a"])
        counts = cw.briefing_video_counts(briefings)
        assert counts["a"] == 2
        assert counts["b"] == 1

    def test_load_seen_video_ids_is_the_key_set(self, tmp_path):
        briefings = tmp_path / "_briefings"
        _write_briefing(briefings, "one.md", ["a", "b", 123, None, ""])
        _write_briefing(briefings, "two.md", ["c"])
        assert cw.load_seen_video_ids(briefings) == set(cw.briefing_video_counts(briefings))
        # The pre-move str() coercion of truthy non-strings is preserved.
        assert cw.load_seen_video_ids(briefings) == {"a", "b", "123", "c"}

    def test_a_non_utf8_briefing_is_skipped_and_the_others_counted(self, tmp_path, caplog):
        briefings = tmp_path / "_briefings"
        _write_briefing(briefings, "good.md", ["a"])
        torn = briefings / "torn.md"
        torn.write_bytes(b"---\nvideo_ids: [b]\ntitle: \xd0\n---\n")
        skipped: list[str] = []
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            counts = cw.briefing_video_counts(briefings, skipped)
        assert counts == {"a": 1}
        assert skipped == [str(torn)]
        assert any("torn.md" in r.getMessage() for r in _warnings(caplog))
        # load_seen_video_ids keeps its one-argument signature.
        assert cw.load_seen_video_ids(briefings) == {"a"}

    def test_missing_dir_is_empty(self, tmp_path):
        assert cw.briefing_video_counts(tmp_path / "_briefings") == {}
        assert cw.load_seen_video_ids(tmp_path / "_briefings") == set()


# ---------------------------------------------------------------------------
# Walker (unit U6): sibling index, one record per video_id, counters
# ---------------------------------------------------------------------------


def _video(
    channel_dir,
    prefix,
    video_id,
    *,
    title=None,
    channel=None,
    mindmap=False,
    concepts=False,
    transcript=False,
    meta=None,
):
    """Write one synthetic video's artifacts; returns the meta path."""
    channel_dir.mkdir(parents=True, exist_ok=True)
    data = {"video_id": video_id, "title": title if title is not None else prefix, "published": "2026-01-01"}
    if channel is not None:
        data["channel"] = channel
    if meta is not None:
        data = meta
    meta_path = channel_dir / f"{prefix}{cw.META_SUFFIX}"
    meta_path.write_text(json.dumps(data), encoding="utf-8")
    if mindmap:
        (channel_dir / f"{prefix}{cw.MINDMAP_SUFFIX}").write_text("# m\n", encoding="utf-8")
    if concepts:
        (channel_dir / f"{prefix}{cw.CONCEPTS_SUFFIX}").write_text('{"concepts": []}', encoding="utf-8")
    if transcript:
        (channel_dir / f"{prefix}{cw.TRANSCRIPT_SUFFIX}").write_text("t\n", encoding="utf-8")
    return meta_path


def _partitions(result):
    return result.metas == len(result.records) + result.duplicates + result.skipped + result.no_id


class TestChannelSiblingIndex:
    def test_a_bracketed_prefix_finds_its_mindmap_literally(self, tmp_path):
        ch = tmp_path / "alpha"
        prefix = "2026-01-01-[live]-title"
        _video(ch, prefix, "vid1", mindmap=True)
        index = cw.channel_sibling_index(ch)
        assert set(index[prefix]) == {cw.META_SUFFIX, cw.MINDMAP_SUFFIX}
        assert index[prefix][cw.MINDMAP_SUFFIX] == ch / f"{prefix}{cw.MINDMAP_SUFFIX}"

    def test_corrupt_sidecar_raw_sidecar_and_srt_are_not_in_the_index(self, tmp_path):
        ch = tmp_path / "alpha"
        _video(ch, "p", "vid1")
        (ch / "p.meta.corrupt.json").write_text("{", encoding="utf-8")
        (ch / "p.transcript.raw.txt").write_text("raw", encoding="utf-8")
        (ch / "p.en.srt").write_text("1", encoding="utf-8")
        assert cw.channel_sibling_index(ch) == {"p": {cw.META_SUFFIX: ch / "p.meta.json"}}

    def test_a_directory_named_like_an_artifact_is_not_a_sibling(self, tmp_path):
        ch = tmp_path / "alpha"
        _video(ch, "p", "vid1")
        (ch / "p.mindmap.md").mkdir()
        assert cw.channel_sibling_index(ch) == {"p": {cw.META_SUFFIX: ch / "p.meta.json"}}


class TestIterMetaRecords:
    def test_duplicate_id_yields_one_record_with_the_richer_title_and_an_alias(self, tmp_path):
        ch = tmp_path / "alpha"
        _video(ch, "2026-01-01-a-old", "vid1", title="Old title")
        _video(ch, "2026-01-01-b-new", "vid1", title="New title", mindmap=True, concepts=True)
        result = cw.iter_meta_records(tmp_path)
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.prefix == "2026-01-01-b-new"
        assert rec.meta["title"] == "New title"
        assert rec.aliases == ["Old title"]
        assert [loser.prefix for loser in rec.losers] == ["2026-01-01-a-old"]
        assert result.duplicates == 1

    def test_an_equal_count_tie_keeps_the_first_in_sorted_order(self, tmp_path):
        # Falsified once with `>=`: the later record won and this went red.
        ch = tmp_path / "alpha"
        _video(ch, "b-second", "vid1", title="Second", mindmap=True)
        _video(ch, "a-first", "vid1", title="First", concepts=True)
        rec = cw.iter_meta_records(tmp_path).records[0]
        assert rec.prefix == "a-first"
        assert rec.aliases == ["Second"]

    def test_an_equal_count_tie_follows_filename_order_not_prefix_order(self, tmp_path):
        # `foo-2.meta.json` sorts before `foo.meta.json` ('-' < '.'), but the
        # bare prefix `foo` sorts before `foo-2`. The pre-#260 walker took the
        # first of sorted(glob('*.meta.json')); falsified by sorting on the bare
        # prefix, which kept `foo` and went red.
        ch = tmp_path / "alpha"
        _video(ch, "2026-01-01-foo", "vid1", title="Plain", mindmap=True)
        _video(ch, "2026-01-01-foo-2", "vid1", title="Suffixed", mindmap=True)
        pre_260_first = sorted(ch.glob(f"*{cw.META_SUFFIX}"))[0].name[: -len(cw.META_SUFFIX)]
        assert pre_260_first == "2026-01-01-foo-2"
        rec = cw.iter_meta_records(tmp_path).records[0]
        assert rec.prefix == pre_260_first
        assert rec.aliases == ["Plain"]

    def test_the_transcript_never_counts_toward_the_winner(self, tmp_path):
        # Falsified once by adding TRANSCRIPT_SUFFIX to the counted suffixes:
        # the 2-2 tie then kept the first record and this went red.
        ch = tmp_path / "alpha"
        _video(ch, "a-transcript-mindmap", "vid1", transcript=True, mindmap=True)
        _video(ch, "b-mindmap-concepts", "vid1", mindmap=True, concepts=True)
        rec = cw.iter_meta_records(tmp_path).records[0]
        assert rec.prefix == "b-mindmap-concepts"

    def test_duplicate_alias_titles_are_deduplicated_and_exclude_the_winner(self, tmp_path):
        ch = tmp_path / "alpha"
        _video(ch, "a", "vid1", title="Same")
        _video(ch, "b", "vid1", title="Same")
        _video(ch, "c", "vid1", title="Win", mindmap=True)
        _video(ch, "d", "vid1", title="Win")
        rec = cw.iter_meta_records(tmp_path).records[0]
        assert rec.prefix == "c"
        assert rec.aliases == ["Same"]

    def test_the_same_id_in_two_channel_folders_lists_both_channels(self, tmp_path):
        _video(tmp_path / "alpha", "p1", "vid1", mindmap=True)
        _video(tmp_path / "beta", "p2", "vid1")
        result = cw.iter_meta_records(tmp_path)
        assert len(result.records) == 1
        rec = result.records[0]
        assert rec.channel == "alpha"
        assert rec.channels == ["alpha", "beta"]
        assert rec.channel_dir == tmp_path / "alpha"

    def test_excluded_folders_are_skipped_and_their_metas_counted(self, tmp_path):
        for name in ("_wiki", "_atlas", "_quarantine", "_standalone", ".lancedb"):
            _video(tmp_path / name, "p", f"id-{name}")
        (tmp_path / "empty").mkdir()
        result = cw.iter_meta_records(tmp_path)
        assert result.records == []
        assert result.excluded_metas == 5
        assert result.metas == 0

    def test_a_non_existent_corpus_is_an_empty_result(self, tmp_path):
        result = cw.iter_meta_records(tmp_path / "nope")
        assert result.records == []
        counters = (result.metas, result.duplicates, result.skipped, result.no_id, result.excluded_metas)
        assert counters == (0, 0, 0, 0, 0)
        assert result.skipped_files == []

    def test_the_counters_partition_the_input(self, tmp_path, caplog):
        ch = tmp_path / "alpha"
        _video(ch, "good", "vid-good")
        _video(ch, "dup-a", "vid-dup")
        _video(ch, "dup-b", "vid-dup")
        _video(ch, "int-id", 123)
        torn = ch / f"torn{cw.META_SUFFIX}"
        torn.write_bytes(b'{"video_id": "x", "title": "\xd0')
        _video(tmp_path / "_quarantine", "q", "vid-q")
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            result = cw.iter_meta_records(tmp_path)
        assert result.metas == 5
        assert len(result.records) == 2
        assert result.duplicates == 1
        assert result.skipped == 1
        assert result.no_id == 1
        assert result.excluded_metas == 1
        assert result.skipped_files == [str(torn)]
        assert _partitions(result)
        assert any("int-id.meta.json" in r.getMessage() for r in _warnings(caplog))

    def test_one_iterdir_per_channel_folder(self, tmp_path, monkeypatch):
        for i in range(6):
            _video(tmp_path / "alpha", f"p{i}", f"a{i}", mindmap=True, concepts=True)
        _video(tmp_path / "beta", "p", "b0")
        calls: dict[str, int] = {}
        real = os.scandir

        def counting(path):
            calls[pathlib.Path(path).name] = calls.get(pathlib.Path(path).name, 0) + 1
            return real(path)

        monkeypatch.setattr(cw.os, "scandir", counting)
        result = cw.iter_meta_records(tmp_path)
        assert len(result.records) == 7
        assert calls["alpha"] == 1
        assert calls["beta"] == 1

    def test_a_non_string_channel_falls_back_to_the_folder_with_a_warning(self, tmp_path, caplog):
        _video(tmp_path / "alpha", "p", "vid1", meta={"video_id": "vid1", "channel": ["x"]})
        with caplog.at_level(logging.WARNING, logger="video_intel.corpus_walk"):
            rec = cw.iter_meta_records(tmp_path).records[0]
        assert rec.channel == "alpha"
        assert rec.channels == ["alpha"]
        assert any("`channel`" in r.getMessage() for r in _warnings(caplog))

    def test_a_meta_channel_differing_from_its_folder_is_kept(self, tmp_path):
        _video(tmp_path / "alpha", "p", "vid1", channel="Alpha Creator")
        _video(tmp_path / "beta", "q", "vid2", channel="Alpha Creator")
        _video(tmp_path / "gamma", "r", "vid2")
        by_id = {r.video_id: r for r in cw.iter_meta_records(tmp_path).records}
        assert by_id["vid1"].channel == "Alpha Creator"
        assert by_id["vid1"].channels == ["Alpha Creator"]
        assert by_id["vid2"].channels == ["Alpha Creator", "gamma"]

    def test_records_are_sorted_by_channel_then_prefix(self, tmp_path):
        _video(tmp_path / "beta", "a", "v3")
        _video(tmp_path / "alpha", "b", "v2")
        _video(tmp_path / "alpha", "a", "v1")
        assert [(r.channel, r.prefix) for r in cw.iter_meta_records(tmp_path).records] == [
            ("alpha", "a"),
            ("alpha", "b"),
            ("beta", "a"),
        ]


class TestCollectCorpusVideosIsAProjection:
    def test_same_ids_and_chosen_titles_as_the_walker(self, tmp_path):
        ch = tmp_path / "alpha"
        _video(ch, "a-old", "vid1", title="Old")
        _video(ch, "b-new", "vid1", title="New", mindmap=True)
        _video(ch, "c-tie", "vid2", title="Tie first", concepts=True)
        _video(ch, "d-tie", "vid2", title="Tie second", mindmap=True)
        _video(tmp_path / "beta", "e", "vid1", title="Elsewhere")
        _video(ch, "f", "vid3", title="Solo", mindmap=True, concepts=True)
        walker = {r.video_id: r.meta["title"] for r in cw.iter_meta_records(tmp_path).records}
        projected = {v["video_id"]: v["title"] for v in vi.collect_corpus_videos(tmp_path)}
        assert projected == walker
        assert walker == {"vid1": "New", "vid2": "Tie first", "vid3": "Solo"}

    def test_the_projection_keeps_its_paths_url_fallback_and_topic_union(self, tmp_path):
        ch = tmp_path / "alpha"
        _video(ch, "a", "vid1", meta={"video_id": "vid1", "title": "A", "topics": ["sales"]})
        _video(ch, "b", "vid1", title="B", mindmap=True)
        [rec] = vi.collect_corpus_videos(tmp_path)
        assert rec["title"] == "B"
        assert rec["url"] == "https://www.youtube.com/watch?v=vid1"
        assert rec["mindmap_path"] == ch / "b.mindmap.md"
        assert rec["concepts_path"] is None
        assert rec["topics"] == ["sales"]
        assert rec["channel"] == "alpha"
