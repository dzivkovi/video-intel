"""Tests for scripts/intel_graph.py (issue #85).

Covers the pure helpers, the DuckDB truth-store loader on a synthetic
mini-corpus, lexical grounding (incl. word-boundary discipline), the
anti-circularity contract of the co-occurrence builder, and the acceptance
gate logic with fabricated community assignments. Neo4j-dependent code is
exercised by an integration test that skips when no server is reachable.
"""

from __future__ import annotations

import json
import os
import typing

import intel_graph as ig
import pytest

duckdb = pytest.importorskip("duckdb")

# ---------------------------------------------------------------------------
# Fixture corpus
# ---------------------------------------------------------------------------

TRANSCRIPT_A = """# Transcript: Video A

**Source:** https://www.youtube.com/watch?v=vidA

---

[00:10] Alice: "The ralph loop is how I ship code while I sleep."

[00:40] Alice: "You need prd generation before you let it run."

[01:20] Alice: "I moved my whole workflow from cursor over to claude code."
"""

TRANSCRIPT_B = """# Transcript: Video B

**Source:** https://www.youtube.com/watch?v=vidB

---

[00:05] Bob: "Building reliable agents means constraining the loop."

[00:35] Bob: "We rely on prd generation for every feature."

[01:05] Bob: "This tool is the precursor to everything that followed."
"""


def make_corpus(tmp_path):
    out = tmp_path / "corpus"
    (out / "_briefings").mkdir(parents=True)
    (out / "_briefings" / "junk.md").write_text("not a channel", encoding="utf-8")
    (out / ".lancedb").mkdir()

    (out / "taxonomy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "built_from": 3,
                "concepts": {
                    "ai.loop": {"preferred_label": "Agentic Loop", "domain": "ai", "first_seen": "2026-01-01"},
                    "ai.spec": {"preferred_label": "Specs", "domain": "ai", "first_seen": "2026-01-01"},
                    "ai.tool": {"preferred_label": "Tooling", "domain": "ai", "first_seen": "2026-01-01"},
                    "ai.ctx": {"preferred_label": "Context", "domain": "ai", "first_seen": "2026-01-01"},
                },
            }
        ),
        encoding="utf-8",
    )

    def write_video(channel, prefix, video_id, transcript, concepts, title="T"):
        ch = out / channel
        ch.mkdir(exist_ok=True)
        (ch / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": video_id,
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                    "title": title,
                    "published": "2026-01-01",
                    "model": "gemini-test",
                    "processed": "2026-01-02T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        if transcript:
            (ch / f"{prefix}.transcript.md").write_text(transcript, encoding="utf-8")
        (ch / f"{prefix}.concepts.json").write_text(
            json.dumps(
                {
                    "video_id": video_id,
                    "source_prompt": "mindmap-knowledge",
                    "concepts": concepts,
                }
            ),
            encoding="utf-8",
        )

    write_video(
        "alpha",
        "2026-01-01-video-a",
        "vidA",
        TRANSCRIPT_A,
        [
            {
                "concept_id": "ai.loop",
                "preferred_label": "Agentic Loop",
                "as_mentioned": "ralph loop",
                "confidence": 1.0,
            },
            {"concept_id": "ai.spec", "preferred_label": "Specs", "as_mentioned": "prd generation", "confidence": 0.9},
            {"concept_id": "ai.tool", "preferred_label": "Tooling", "as_mentioned": "cursor", "confidence": 0.8},
        ],
        title="Video A",
    )
    write_video(
        "beta",
        "2026-01-02-video-b",
        "vidB",
        TRANSCRIPT_B,
        [
            {
                "concept_id": "ai.loop",
                "preferred_label": "Agentic Loop",
                "as_mentioned": "autonomous loop",
                "confidence": 1.0,
            },
            {"concept_id": "ai.spec", "preferred_label": "Specs", "as_mentioned": "prd generation", "confidence": 0.9},
        ],
        title="Video B",
    )
    # No transcript: observations stay ungrounded, claims not presentable.
    write_video(
        "beta",
        "2026-01-03-video-c",
        "vidC",
        None,
        [{"concept_id": "ai.ctx", "preferred_label": "Context", "as_mentioned": "context stuff", "confidence": 0.7}],
        title="Video C",
    )
    return out


@pytest.fixture
def corpus(tmp_path):
    return make_corpus(tmp_path)


@pytest.fixture
def con(corpus):
    con = duckdb.connect(":memory:")
    ig.load_corpus(con, corpus)
    return con


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_normalize_phrase(self):
        assert ig.normalize_phrase("  Ralph   Loop ") == "ralph loop"

    def test_entity_id_for_plain_phrase_stays_clean(self):
        assert ig.entity_id_for("Ralph Loop") == "term:ralph-loop"
        assert ig.entity_id_for("***") == ""

    def test_entity_id_for_punctuation_distinct_phrases_stay_distinct(self):
        """Slug collision would merge 'c++', 'c#', and 'c' into one entity,
        grounding later phrases against the wrong match_pattern (found by
        the adversarial + correctness reviewers independently)."""
        ids = {ig.entity_id_for(p) for p in ["c++", "c#", "c", "claude code", "claude-code"]}
        assert len(ids) == 5

    def test_entity_id_for_lossy_slug_is_deterministic(self):
        assert ig.entity_id_for("C++") == ig.entity_id_for("c++")

    def test_match_pattern_word_boundary(self):
        import re

        pat = ig.match_pattern_for("cursor")
        assert re.search(pat, "i use cursor daily")
        assert not re.search(pat, "the precursor to everything")

    def test_match_pattern_non_alnum_edges(self):
        import re

        pat = ig.match_pattern_for("(mcp)")
        assert re.search(pat, "protocol (mcp) is here")
        pat2 = ig.match_pattern_for("c++")
        assert re.search(pat2, "wrote it in c++ today")

    def test_quote_around(self):
        text = "a" * 500 + " ralph loop " + "b" * 500
        q = ig.quote_around(text, "Ralph Loop")
        assert "ralph loop" in q
        assert q.startswith("...") and q.endswith("...")
        assert len(q) < 400

    def test_timestamped_url(self):
        assert ig.timestamped_url("https://x.com/watch?v=1", 30) == "https://x.com/watch?v=1&t=30"
        assert ig.timestamped_url("https://x.com/v", 30) == "https://x.com/v?t=30"
        assert ig.timestamped_url(None, 30) == ""


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class TestLoadCorpus:
    def test_counts(self, con):
        counts = {
            t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ["sources", "artifacts", "segments", "entities", "concepts", "claims"]
        }
        assert counts["sources"] == 2
        assert counts["artifacts"] == 3
        assert counts["concepts"] == 4
        # 4 distinct surface phrases: ralph loop, prd generation, cursor, autonomous loop, context stuff -> 5
        assert counts["entities"] == 5
        assert counts["claims"] == 6
        assert counts["segments"] > 0

    def test_briefings_dir_not_a_channel(self, con):
        rows = con.execute("SELECT source_id FROM sources ORDER BY 1").fetchall()
        assert rows == [("alpha",), ("beta",)]

    def test_grounding_marks_only_matching_segments(self, con):
        grounded = dict(con.execute("SELECT as_mentioned, grounded FROM has_concept ORDER BY as_mentioned").fetchall())
        assert grounded["ralph loop"] is True
        assert grounded["cursor"] is True
        assert grounded["context stuff"] is False  # vidC has no transcript

    def test_word_boundary_blocks_precursor(self, con):
        # 'cursor' appears in vidA speech; vidB says only 'precursor', which
        # must NOT produce a mention (word-boundary regex).
        rows = con.execute(
            """
            SELECT DISTINCT s.artifact_id FROM mentions m
            JOIN segments s USING (segment_id)
            JOIN entities e USING (entity_id)
            WHERE e.canonical = 'cursor'
            """
        ).fetchall()
        assert rows == [("vidA",)]

    def test_expresses_provenance_columns(self, con):
        rows = con.execute(
            """
            SELECT e.quote, e.confidence, e.extractor_model, e.prompt_version
            FROM expresses e JOIN claims c ON c.claim_id = e.claim_id
            WHERE c.statement LIKE '%ralph loop%'
            """
        ).fetchall()
        assert len(rows) == 1
        quote, confidence, model, prompt = rows[0]
        assert "ralph loop" in quote.lower()
        assert confidence == 1.0
        assert model == "gemini-test+lexical-grounding-v1"
        assert prompt == "concepts@mindmap-knowledge"

    def test_ungrounded_claims_not_presentable(self, con):
        presentable = {r[0] for r in con.execute("SELECT DISTINCT claim_id FROM expresses").fetchall()}
        ctx_claim = con.execute("SELECT claim_id FROM claims WHERE target = 'ai.ctx'").fetchone()[0]
        assert ctx_claim not in presentable

    def test_reload_is_idempotent(self, corpus, con):
        before = con.execute("SELECT count(*) FROM has_concept").fetchone()[0]
        ig.load_corpus(con, corpus)
        after = con.execute("SELECT count(*) FROM has_concept").fetchone()[0]
        assert before == after

    def test_empty_published_string_loads_as_null(self, corpus):
        """video_intel can persist published: '' - it must coerce to NULL,
        not crash the whole load on the VARCHAR->DATE cast."""
        meta_path = corpus / "beta" / "2026-01-03-video-c.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["published"] = ""
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        con = duckdb.connect(":memory:")
        ig.load_corpus(con, corpus)
        row = con.execute("SELECT published_at FROM artifacts WHERE artifact_id = 'vidC'").fetchone()
        assert row == (None,)

    def test_malformed_taxonomy_does_not_abort_load(self, corpus):
        (corpus / "taxonomy.json").write_text('{"concepts": {truncated', encoding="utf-8")
        con = duckdb.connect(":memory:")
        counts = ig.load_corpus(con, corpus)
        assert counts["concepts"] == 0
        assert counts["artifacts"] == 3  # rest of the load proceeded


class TestTitleRotationSiblings:
    """Two prefixes sharing one video_id (title rotation, pre-dedupe state).
    video_id is the identity; the loader must not duplicate artifacts or
    splice two transcript versions into one segment stream."""

    def make_rotated(self, tmp_path):
        corpus = make_corpus(tmp_path)
        ch = corpus / "alpha"
        (ch / "2026-01-05-video-a-rotated.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "vidA",
                    "video_url": "https://www.youtube.com/watch?v=vidA",
                    "title": "Video A (rotated title)",
                    "published": "2026-01-01",
                    "model": "gemini-test",
                    "processed": "2026-01-06T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )
        # Longer sibling transcript: extra chunks must NOT be spliced in.
        (ch / "2026-01-05-video-a-rotated.transcript.md").write_text(
            TRANSCRIPT_A + '\n[02:00] Alice: "Extra tail line one from the rotated sibling."\n'
            '\n[02:30] Alice: "Extra tail line two."\n'
            '\n[03:00] Alice: "Extra tail line three."\n'
            '\n[03:30] Alice: "Extra tail line four."\n'
            '\n[04:00] Alice: "Extra tail line five."\n'
            '\n[04:30] Alice: "Extra tail line six."\n',
            encoding="utf-8",
        )
        (ch / "2026-01-05-video-a-rotated.concepts.json").write_text(
            json.dumps(
                {
                    "video_id": "vidA",
                    "source_prompt": "mindmap-knowledge",
                    "concepts": [
                        {
                            "concept_id": "ai.ctx",
                            "preferred_label": "Context",
                            "as_mentioned": "rotated-only concept",
                            "confidence": 0.6,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return corpus

    def test_one_artifact_row_first_prefix_wins(self, tmp_path):
        corpus = self.make_rotated(tmp_path)
        con = duckdb.connect(":memory:")
        ig.load_corpus(con, corpus)
        rows = con.execute("SELECT title FROM artifacts WHERE artifact_id = 'vidA'").fetchall()
        assert rows == [("Video A",)]

    def test_sibling_transcript_not_spliced(self, tmp_path):
        corpus = self.make_rotated(tmp_path)
        con = duckdb.connect(":memory:")
        ig.load_corpus(con, corpus)
        texts = con.execute("SELECT text FROM segments WHERE artifact_id = 'vidA'").fetchall()
        assert all("Extra tail line" not in t[0] for t in texts)

    def test_sibling_unique_concept_still_contributes(self, tmp_path):
        corpus = self.make_rotated(tmp_path)
        con = duckdb.connect(":memory:")
        ig.load_corpus(con, corpus)
        row = con.execute(
            "SELECT count(*) FROM has_concept WHERE artifact_id = 'vidA' AND as_mentioned = 'rotated-only concept'"
        ).fetchone()
        assert row[0] == 1


class TestCli:
    def test_db_flag_parses_after_subcommand(self):
        """The documented CLI form `load --db PATH` must parse - argparse
        rejects flags defined only on the top-level parser."""
        args = ig.build_parser().parse_args(["load", "--db", "x.duckdb"])
        assert args.db == "x.duckdb"
        args2 = ig.build_parser().parse_args(["verify", "--db", "y.duckdb", "--report", "r.json"])
        assert args2.db == "y.duckdb"

    def test_db_flag_defaults_when_omitted(self):
        args = ig.build_parser().parse_args(["load"])
        assert args.db == str(ig.DEFAULT_DB)

    def test_force_unlink_refuses_non_duckdb(self, tmp_path):
        victim = tmp_path / "important.txt"
        victim.write_text("do not delete", encoding="utf-8")
        with pytest.raises(SystemExit):
            ig.force_unlink_db(victim)
        assert victim.exists()

    def test_force_unlink_deletes_duckdb(self, tmp_path):
        db = tmp_path / "intel.duckdb"
        db.write_text("", encoding="utf-8")
        ig.force_unlink_db(db)
        assert not db.exists()


# ---------------------------------------------------------------------------
# Co-occurrence (anti-circularity contract)
# ---------------------------------------------------------------------------


class TestCoOccurrence:
    def test_shared_concept_alone_creates_no_edge(self, con):
        """ralph loop (vidA) and autonomous loop (vidB) share concept ai.loop
        but never share an artifact - the taxonomy must NOT create an edge."""
        ig.compute_co_occurrence(con, max_df=50, min_shared=1)
        rows = con.execute(
            "SELECT 1 FROM co_occurs WHERE (entity_a = 'term:ralph-loop' AND entity_b = 'term:autonomous-loop')"
            " OR (entity_a = 'term:autonomous-loop' AND entity_b = 'term:ralph-loop')"
        ).fetchall()
        assert rows == []

    def test_shared_artifact_creates_edge(self, con):
        ig.compute_co_occurrence(con, max_df=50, min_shared=1)
        row = con.execute(
            "SELECT weight FROM co_occurs WHERE entity_a = 'term:prd-generation' AND entity_b = 'term:ralph-loop'"
        ).fetchone()
        assert row and row[0] == 1

    def test_lexical_mention_extends_observation(self, con):
        """'prd generation' is in both transcripts, so both alias phrases
        gain a shared hub neighbor - the glue mechanism under test."""
        ig.compute_co_occurrence(con, max_df=50, min_shared=1)
        rows = con.execute(
            "SELECT entity_a, entity_b FROM co_occurs WHERE entity_a = 'term:autonomous-loop' OR entity_b = 'term:autonomous-loop'"
        ).fetchall()
        partners = {a if a != "term:autonomous-loop" else b for a, b in rows}
        assert "term:prd-generation" in partners

    def test_max_df_drops_hubs(self, con):
        ig.compute_co_occurrence(con, max_df=1, min_shared=1)
        rows = con.execute(
            "SELECT 1 FROM co_occurs WHERE entity_a = 'term:prd-generation' OR entity_b = 'term:prd-generation'"
        ).fetchall()
        assert rows == []


# ---------------------------------------------------------------------------
# Gate logic with fabricated communities
# ---------------------------------------------------------------------------


def fabricate_communities(con, mapping: dict[str, int]):
    for eid, comm in mapping.items():
        con.execute("UPDATE entities SET community_id = ?, pagerank = 0.1 WHERE entity_id = ?", [comm, eid])


class TestAliasCohesion:
    def test_cohesive_alias_set_scores_high(self, con):
        fabricate_communities(
            con,
            {
                "term:ralph-loop": 1,
                "term:autonomous-loop": 1,
                "term:prd-generation": 1,
                "term:cursor": 2,
                "term:context-stuff": 3,
            },
        )
        result = ig.alias_cohesion(con)
        # Only ai.loop and ai.spec have >= 2 observations... ai.spec has the
        # same phrase twice -> one distinct entity -> excluded. So one set.
        assert result["alias_sets_evaluated"] == 1
        assert result["mean_cohesion"] == 1.0
        assert result["fully_cohesive_sets"] == 1

    def test_split_alias_set_scores_half(self, con):
        fabricate_communities(con, {"term:ralph-loop": 1, "term:autonomous-loop": 2})
        result = ig.alias_cohesion(con)
        assert result["mean_cohesion"] == 0.5

    def test_permutation_baseline_is_deterministic_and_bounded(self, con):
        """The permutation baseline is the gate's honesty control - a broken
        permutation loop must not be able to silently fake a lift win."""
        fabricate_communities(con, {"term:ralph-loop": 1, "term:autonomous-loop": 2})
        first = ig.alias_cohesion(con)
        second = ig.alias_cohesion(con)
        assert first["permutation_baseline"] == second["permutation_baseline"]
        assert 0.0 < first["permutation_baseline"] <= 1.0
        assert first["lift"] == round(first["mean_cohesion"] - first["permutation_baseline"], 4)


class TestModalAnchoredShared:
    def test_scatter_overlap_does_not_count(self):
        """Both sides have one stray term in community 9, but neither side's
        center of gravity is there - the P3 lesson from the real corpus."""
        from collections import Counter

        user = Counter({1: 10, 9: 1})
        creator = Counter({2: 16, 9: 1})
        assert ig.modal_anchored_shared(user, creator) == set()

    def test_modal_side_reaching_other_counts(self):
        from collections import Counter

        user = Counter({333: 11, 9: 1})  # modal 333
        creator = Counter({2776: 16, 333: 14})  # 333 not modal but present
        assert ig.modal_anchored_shared(user, creator) == {333}

    def test_ties_are_modal(self):
        from collections import Counter

        user = Counter({1: 1, 2: 1})
        creator = Counter({2: 3})
        assert ig.modal_anchored_shared(user, creator) == {2}

    def test_empty_side(self):
        from collections import Counter

        assert ig.modal_anchored_shared(Counter(), Counter({1: 2})) == set()

    def test_all_ties_degenerate_side_anchors_nothing(self):
        """A side with max count 1 spread over many communities has no
        center of gravity - without the guard, every community is 'modal'
        and the criterion collapses to plain any-overlap."""
        from collections import Counter

        degenerate = Counter({i: 1 for i in range(10)})
        also_degenerate = Counter({20: 1, 21: 1, 22: 1, 23: 1, 5: 1})
        assert ig.modal_anchored_shared(degenerate, also_degenerate) == set()

    def test_small_tie_set_still_anchors(self):
        """A genuinely small side (e.g. factorio's single term, or ralph's
        three one-count terms) keeps its modal set - the guard only fires
        on wide all-ties scatter."""
        from collections import Counter

        small = Counter({1: 1, 2: 1, 3: 1})  # == MODAL_TIE_LIMIT
        other = Counter({1: 5})
        assert 1 in ig.modal_anchored_shared(small, other)


class TestCheckPair:
    PAIR: typing.ClassVar[dict] = {
        "name": "test pair",
        "user_patterns": ["autonomous"],
        "creator_patterns": ["ralph"],
        "citation_phrases": ["ralph loop"],
    }

    def test_recovered_when_shared_small_community(self, con):
        fabricate_communities(
            con,
            {
                "term:ralph-loop": 1,
                "term:autonomous-loop": 1,
                "term:prd-generation": 2,
                "term:cursor": 2,
                "term:context-stuff": 3,
            },
        )
        sizes = {1: 2, 2: 2, 3: 1}
        result = ig.check_pair(con, self.PAIR, sizes, total_nodes=20)
        assert result["recovered"] is True
        assert result["shared_community"] == 1
        assert result["citation"]["video_id"] == "vidA"
        assert result["citation"]["timestamp_seconds"] is not None
        assert "&t=" in result["citation"]["url"] or "?t=" in result["citation"]["url"]

    def test_megacommunity_rejected(self, con):
        fabricate_communities(
            con,
            {
                e: 1
                for e in [
                    "term:ralph-loop",
                    "term:autonomous-loop",
                    "term:prd-generation",
                    "term:cursor",
                    "term:context-stuff",
                ]
            },
        )
        sizes = {1: 5}
        result = ig.check_pair(con, self.PAIR, sizes, total_nodes=5)
        assert result["recovered"] is False
        assert "megacommunity" in result["miss_reason"]

    def test_disjoint_communities_missed(self, con):
        fabricate_communities(con, {"term:ralph-loop": 1, "term:autonomous-loop": 2})
        result = ig.check_pair(con, self.PAIR, {1: 1, 2: 1}, total_nodes=2)
        assert result["recovered"] is False
        assert "modal-anchored" in result["miss_reason"]

    def test_comention_citation(self, con):
        fabricate_communities(con, {"term:ralph-loop": 1, "term:cursor": 1})
        pair = {
            "name": "cursor -> claude code",
            "user_patterns": ["claude code"],
            "creator_patterns": ["cursor"],
            "citation_phrases": None,
            "comention": ["cursor", "claude code"],
        }
        result = ig.check_pair(con, pair, {1: 2}, total_nodes=5)
        assert result["citation"] is not None
        assert result["citation"]["video_id"] == "vidA"
        assert "cursor" in result["citation"]["quote"].lower()

    def test_anchor_fallback_when_no_entity_matches(self, con):
        """'reliable agents' exists only in vidB's transcript, not as any
        surface term - resolution must fall back to lexical anchoring."""
        fabricate_communities(
            con,
            {"term:ralph-loop": 1, "term:autonomous-loop": 1, "term:prd-generation": 1},
        )
        pair = {
            "name": "anchor test",
            "user_patterns": ["reliable agents"],
            "creator_patterns": ["ralph"],
            "citation_phrases": ["ralph loop"],
        }
        result = ig.check_pair(con, pair, {1: 3}, total_nodes=20)
        assert result["user_resolution"] == "anchor"
        assert result["recovered"] is True


class TestRunGate:
    def test_report_shape_and_render(self, con):
        fabricate_communities(
            con,
            {
                "term:ralph-loop": 1,
                "term:autonomous-loop": 1,
                "term:prd-generation": 1,
                "term:cursor": 2,
                "term:context-stuff": 3,
            },
        )
        report = ig.run_gate(con)
        assert set(report) >= {
            "graph",
            "gate1_alias_recovery",
            "gate2_known_pairs",
            "gate3_provenance",
            "pagerank_top20",
            "gate2_recovered",
        }
        assert len(report["gate2_known_pairs"]) == 3
        md = ig.render_report_md(report)
        assert "Gate 1" in md and "Gate 2" in md and "Gate 3" in md

    def test_requires_project_first(self, con):
        with pytest.raises(SystemExit):
            ig.run_gate(con)


# ---------------------------------------------------------------------------
# Neo4j integration (skips when no server reachable)
# ---------------------------------------------------------------------------


def _neo4j_available() -> bool:
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(
            "bolt://localhost:7687",
            auth=("neo4j", os.environ.get("NEO4J_PASSWORD", "Sup3rSecur3!")),
            connection_timeout=2,
        )
        with driver.session() as s:
            s.run("RETURN gds.version()").single()
        driver.close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _neo4j_available(), reason="no Neo4j+GDS at bolt://localhost:7687")
class TestNeo4jIntegration:
    def test_project_runs_louvain_and_writes_back(self, con):
        ig.compute_co_occurrence(con, max_df=50, min_shared=1)
        stats = ig.project_to_neo4j(
            con,
            "bolt://localhost:7687",
            "neo4j",
            os.environ.get("NEO4J_PASSWORD", "Sup3rSecur3!"),
        )
        assert stats["nodes"] > 0
        assert stats["communities"] > 0
        communitized = con.execute("SELECT count(*) FROM entities WHERE community_id IS NOT NULL").fetchone()[0]
        assert communitized == stats["nodes"]
