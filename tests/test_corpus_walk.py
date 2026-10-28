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
    ("_artifact_count", "artifact_count"),
    ("_artifact_count", "_artifact_count"),
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
            "_artifact_count",
            "artifact_count",
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


class TestArtifactCount:
    def test_counts_only_mindmap_and_concepts(self):
        assert cw.artifact_count({"mindmap_path": "m", "concepts_path": "c", "transcript_path": "t"}) == 2
        assert cw.artifact_count({"mindmap_path": None, "concepts_path": "c"}) == 1
        assert cw.artifact_count({}) == 0


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

    def test_missing_dir_is_empty(self, tmp_path):
        assert cw.briefing_video_counts(tmp_path / "_briefings") == {}
        assert cw.load_seen_video_ids(tmp_path / "_briefings") == set()
