#!/usr/bin/env python3
"""Weak-signal / commonality-detection layer: DuckDB truth + Neo4j-GDS lens.

Issue #85. Loads the existing corpus artifacts (meta.json, transcripts,
concepts.json, taxonomy.json) into a DuckDB truth store shaped as the
6-node / 6-edge starter schema from docs/brainstorms/2026-05-28-intelligence-
layer-roadmap.md, projects a disposable co-occurrence graph into Neo4j,
runs community detection (seeded Leiden by default for reproducibility;
Louvain via --algo louvain) + PageRank via the GDS plugin, writes the
algorithm outputs back into DuckDB, and verifies the issue #85 acceptance
gate (alias recovery + three known cross-vocabulary pairs, each with a
quote @ video @ timestamp citation).

Design rules (see docs/plans/2026-07-02-001-feat-intel-graph-weak-signal-plan.md):

- DuckDB is the truth store; the Neo4j graph is regenerable and may be
  wiped at any time. Algorithm outputs persist in DuckDB, not the graph.
- Anti-circularity: the Louvain input graph is built ONLY from
  shared-artifact co-occurrence between surface-term entities.
  taxonomy.json / concept_id normalization NEVER contribute edges - they
  are the answer key for the alias-recovery gate.
- Provenance: a claim is presentable only if it has at least one
  `expresses` row carrying a verbatim quote + timestamp.

Operationally separate from the scan/transcript/search pipeline - this
script only READS corpus artifacts. It imports `chunk_transcript` from
video_intel so segments share the exact grain of the LanceDB index.

Usage:
    python scripts/intel_graph.py load    [--output-dir DIR] [--db PATH] [--force]
    python scripts/intel_graph.py project [--db PATH] [--neo4j-uri URI] [--algo leiden|louvain] [--gamma G] [--max-df N]
    python scripts/intel_graph.py verify  [--db PATH] [--report PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from timestamp_utils import timestamped_url
from video_intel import chunk_transcript, load_config

log = logging.getLogger("intel_graph")

DEFAULT_DB = Path.home() / ".cache" / "video-intel" / "intel.duckdb"
MIN_PHRASE_LEN = 5  # shorter surface phrases produce mostly false lexical matches
MAX_DF_DEFAULT = 50  # drop surface terms present in more artifacts than this before projection
MIN_SHARED_DEFAULT = 1  # minimum shared artifacts for a co-occurrence edge
MEGACOMMUNITY_FRACTION = 0.30  # a shared community larger than this fraction is not a recovery
QUOTE_CONTEXT_CHARS = 300
PERMUTATION_REPS = 20
PERMUTATION_SEED = 85  # issue number; fixed for reproducibility

GDS_GRAPH_NAME = "vi_cooc"
NODE_LABEL = "VI_Entity"

# The three known cross-vocabulary links from ADR-0017 /
# work/2026-07-01/01-hybrid-search-explained-magic-vs-slop.md. Each pair is
# recovered only if the two vocabulary sides land in the same (non-mega)
# Louvain community AND a verbatim transcript citation exists.
KNOWN_PAIRS: list[dict[str, Any]] = [
    {
        "name": "reliable agents == Ralph Wiggum loop / force-feed",
        "user_patterns": ["reliab"],
        "creator_patterns": ["ralph", "wiggum", "force-feed"],
        "citation_phrases": ["ralph wiggum", "ralph loop", "force-feed"],
    },
    {
        "name": "context engineering == Factorio parallel sessions",
        "user_patterns": ["context engineering"],
        "creator_patterns": ["factorio"],
        "citation_phrases": ["factorio"],
    },
    {
        "name": "developers shifted Cursor -> Claude Code",
        "user_patterns": ["claude code"],
        "creator_patterns": ["cursor"],
        "citation_phrases": None,
        "comention": ["cursor", "claude code"],
    },
]


def require_duckdb():
    try:
        import duckdb

        return duckdb
    except ImportError:
        log.error("duckdb not installed. Run: pip install 'video-intel[intelligence]'")
        sys.exit(1)


def require_neo4j():
    try:
        from neo4j import GraphDatabase

        return GraphDatabase
    except ImportError:
        # not in the [intelligence] extras: #95 retired Neo4j/GDS as the
        # analytics engine, so this experimental path is a manual install
        log.error("neo4j driver not installed. Run: pip install neo4j (plus a running Neo4j server)")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Normalization helpers (pure, unit-tested)
# ---------------------------------------------------------------------------


def normalize_phrase(phrase: str) -> str:
    """Lowercase and collapse whitespace. This is the canonical surface form."""
    return re.sub(r"\s+", " ", phrase.strip().lower())


def entity_id_for(phrase: str) -> str:
    """Deterministic slug id for a normalized surface phrase.

    When slugification is lossy (punctuation dropped), a short stable hash
    of the normalized phrase is appended so 'c++', 'c#', and 'c' stay three
    distinct entities instead of silently merging - a merged entity would
    inherit the first phrase's canonical + match_pattern and ground later
    phrases against the wrong literal text (a provenance violation).
    """
    norm = normalize_phrase(phrase)
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    if not slug:
        return ""
    if re.sub(r"[a-z0-9 ]", "", norm):
        slug += "-" + hashlib.md5(norm.encode("utf-8")).hexdigest()[:6]
    return f"term:{slug}"


def match_pattern_for(phrase: str) -> str:
    """Word-boundary regex for lexical grounding of a normalized phrase.

    Uses explicit non-alphanumeric boundaries instead of bare ``\\b`` when
    the phrase starts or ends with a non-word character (``.NET``, ``C++``,
    ``(mcp)``) - the same failure mode the taxonomy query expander handles.
    """
    norm = normalize_phrase(phrase)
    escaped = re.escape(norm)
    prefix = r"\b" if norm[:1].isalnum() else r"(?:^|[^a-z0-9])"
    suffix = r"\b" if norm[-1:].isalnum() else r"(?:[^a-z0-9]|$)"
    return f"{prefix}{escaped}{suffix}"


def quote_around(text: str, needle: str, context: int = QUOTE_CONTEXT_CHARS) -> str:
    """Verbatim snippet of ``text`` centered on the first word-boundary
    occurrence of ``needle`` (substring fallback keeps the snippet useful
    when the caller already verified presence some other way)."""
    match = re.search(match_pattern_for(needle), text.lower())
    idx = match.start() if match else text.lower().find(normalize_phrase(needle))
    if idx < 0:
        return text[:context].strip()
    start = max(0, idx - context // 2)
    end = min(len(text), idx + len(needle) + context // 2)
    snippet = text[start:end].strip()
    return ("..." if start > 0 else "") + snippet + ("..." if end < len(text) else "")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sources   (source_id VARCHAR PRIMARY KEY, name VARCHAR, kind VARCHAR);
CREATE TABLE IF NOT EXISTS artifacts (artifact_id VARCHAR PRIMARY KEY, source_id VARCHAR, kind VARCHAR,
                                      title VARCHAR, published_at DATE, url VARCHAR);
CREATE TABLE IF NOT EXISTS segments  (segment_id VARCHAR PRIMARY KEY, artifact_id VARCHAR,
                                      position INTEGER, start_seconds INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS entities  (entity_id VARCHAR PRIMARY KEY, canonical VARCHAR, kind VARCHAR,
                                      match_pattern VARCHAR, community_id BIGINT, pagerank DOUBLE);
CREATE TABLE IF NOT EXISTS concepts  (concept_id VARCHAR PRIMARY KEY, canonical VARCHAR,
                                      domain VARCHAR, first_seen_at DATE);
CREATE TABLE IF NOT EXISTS claims    (claim_id VARCHAR PRIMARY KEY, statement TEXT, target VARCHAR,
                                      stance VARCHAR, time_horizon VARCHAR);
CREATE TABLE IF NOT EXISTS published    (source_id VARCHAR, artifact_id VARCHAR);
CREATE TABLE IF NOT EXISTS has_segment  (artifact_id VARCHAR, segment_id VARCHAR);
CREATE TABLE IF NOT EXISTS mentions     (segment_id VARCHAR, entity_id VARCHAR);
CREATE TABLE IF NOT EXISTS expresses    (segment_id VARCHAR, claim_id VARCHAR, quote TEXT,
                                         confidence DOUBLE, extractor_model VARCHAR,
                                         prompt_version VARCHAR, extracted_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS about        (claim_id VARCHAR, concept_id VARCHAR);
-- Bend 1 (see the 2026-07-02 pen-test): concept observations are video-grain
-- (mindmap extraction), so artifact_id is always present; segment_id fills
-- only when lexical grounding finds the phrase in this artifact's transcript.
-- The trailing extractor columns are per-observation provenance carried
-- forward into `expresses` at grounding time.
CREATE TABLE IF NOT EXISTS has_concept  (artifact_id VARCHAR, segment_id VARCHAR, concept_id VARCHAR,
                                         entity_id VARCHAR, as_mentioned VARCHAR, confidence DOUBLE,
                                         grounded BOOLEAN, extractor_model VARCHAR,
                                         prompt_version VARCHAR, extracted_at VARCHAR);
-- Derived analytics cache (regenerated by `project`); kept in DuckDB so
-- `verify` runs without a live Neo4j.
CREATE TABLE IF NOT EXISTS co_occurs (entity_a VARCHAR, entity_b VARCHAR, weight BIGINT);
CREATE TABLE IF NOT EXISTS projection_meta (algo VARCHAR, gamma DOUBLE, projected_at TIMESTAMP);
"""

ALL_TABLES = [
    "sources",
    "artifacts",
    "segments",
    "entities",
    "concepts",
    "claims",
    "published",
    "has_segment",
    "mentions",
    "expresses",
    "about",
    "has_concept",
    "co_occurs",
    "projection_meta",
]


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def resolve_output_dir(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    cfg = load_config()
    return Path(cfg["output_dir"])


def iter_channel_dirs(output_dir: Path):
    """Channel directories, skipping dot- and underscore-prefixed dirs
    (mirrors collect_corpus_videos so _briefings/ is never a channel)."""
    for child in sorted(output_dir.iterdir()):
        if child.is_dir() and not child.name.startswith((".", "_")):
            yield child


def load_corpus(con, output_dir: Path) -> dict[str, int]:
    """Populate the truth store from corpus artifacts. Idempotent: the store
    is fully rebuilt on every load (it is regenerable by construction).
    Returns row counts per table."""
    # Parse the taxonomy BEFORE wiping any tables: a truncated/partial
    # taxonomy.json (cloud-mount partial reads are a documented hazard,
    # issue #67) must not abort the load after the store was emptied.
    taxonomy_path = output_dir / "taxonomy.json"
    concept_rows = []
    try:
        tax = json.loads(taxonomy_path.read_text(encoding="utf-8"))
        concepts_obj = tax.get("concepts", {}) if isinstance(tax, dict) else {}
        for cid, c in concepts_obj.items():
            concept_rows.append((cid, c.get("preferred_label", cid), c.get("domain", ""), c.get("first_seen")))
    except (OSError, json.JSONDecodeError):
        log.warning("taxonomy.json missing or unreadable at %s - concepts table will be sparse", taxonomy_path)

    con.execute(SCHEMA_SQL)
    # Wipe + rebuild inside one transaction: a fallible read mid-load (bad
    # transcript, hostile concept value) must roll back to the previous
    # store, never leave it emptied or half-rebuilt.
    con.execute("BEGIN TRANSACTION")
    try:
        counts = _load_corpus_txn(con, output_dir, concept_rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return counts


def _load_corpus_txn(con, output_dir: Path, concept_rows: list[tuple]) -> dict[str, int]:
    for table in ALL_TABLES:
        con.execute(f"DELETE FROM {table}")

    sources, artifacts, segments = [], {}, {}
    published, has_segment = set(), []
    entities: dict[str, tuple] = {}
    has_concept: list[tuple] = []
    claims: dict[str, tuple] = {}
    about: list[tuple] = []
    videos_with_segments: set[str] = set()
    skipped_no_id = 0

    for channel_dir in iter_channel_dirs(output_dir):
        channel = channel_dir.name
        sources.append((channel, channel, "youtube_channel"))
        for concepts_path in sorted(channel_dir.glob("*.concepts.json")):
            prefix = concepts_path.name[: -len(".concepts.json")]
            meta_path = channel_dir / f"{prefix}.meta.json"
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    log.warning("unreadable meta for %s/%s", channel, prefix)
            try:
                cdata = json.loads(concepts_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                log.warning("unreadable concepts.json for %s/%s - skipped", channel, prefix)
                continue
            video_id = meta.get("video_id") or cdata.get("video_id")
            if not video_id:
                skipped_no_id += 1
                continue
            # video_id is the identity, slug is decoration: first prefix wins,
            # later title-rotation siblings only contribute rows they add.
            if video_id not in artifacts:
                artifacts[video_id] = (
                    video_id,
                    channel,
                    "video",
                    meta.get("title", prefix),
                    meta.get("published") or None,  # '' would fail the DATE cast
                    meta.get("video_url") or f"https://www.youtube.com/watch?v={video_id}",
                )
            published.add((channel, video_id))

            transcript_path = channel_dir / f"{prefix}.transcript.md"
            if transcript_path.exists():
                # Whole-video guard, not per-position: a title-rotation
                # sibling transcript with more chunks would otherwise splice
                # its tail onto the first transcript's segments.
                if video_id in videos_with_segments:
                    log.warning("skipping sibling transcript for %s/%s (run dedupe)", channel, prefix)
                else:
                    videos_with_segments.add(video_id)
                    for pos, chunk in enumerate(chunk_transcript(transcript_path)):
                        seg_id = f"{video_id}:{pos}"
                        segments[seg_id] = (seg_id, video_id, pos, chunk["timestamp_seconds"], chunk["text"])
                        has_segment.append((video_id, seg_id))

            extractor_model = meta.get("model", "unknown")
            prompt_version = f"concepts@{cdata.get('source_prompt', 'unknown')}"
            extracted_at = meta.get("processed") or datetime.now(UTC).isoformat()
            for row in cdata.get("concepts", []):
                phrase = normalize_phrase(row.get("as_mentioned") or "")
                concept_id = row.get("concept_id") or ""
                if not phrase or not concept_id:
                    continue
                eid = entity_id_for(phrase)
                if not eid:
                    continue
                if eid not in entities:
                    entities[eid] = (eid, phrase, "surface_term", match_pattern_for(phrase))
                claim_id = f"{video_id}|{concept_id}|{eid}"
                if claim_id not in claims:
                    label = row.get("preferred_label", concept_id)
                    claims[claim_id] = (
                        claim_id,
                        f"{channel} discusses {label} (as '{phrase}')",
                        concept_id,
                        "discusses",
                        "n/a",
                    )
                    about.append((claim_id, concept_id))
                    has_concept.append(
                        (
                            video_id,
                            None,
                            concept_id,
                            eid,
                            phrase,
                            float(row.get("confidence") or 0.0),
                            False,
                            extractor_model,
                            prompt_version,
                            extracted_at,
                        )
                    )

    if sources:
        con.executemany("INSERT OR REPLACE INTO sources VALUES (?,?,?)", sources)
    if concept_rows:
        con.executemany("INSERT OR REPLACE INTO concepts VALUES (?,?,?,?)", concept_rows)
    if artifacts:
        con.executemany("INSERT INTO artifacts VALUES (?,?,?,?,?,?)", list(artifacts.values()))
    if published:
        con.executemany("INSERT INTO published VALUES (?,?)", sorted(published))
    if segments:
        con.executemany("INSERT INTO segments VALUES (?,?,?,?,?)", list(segments.values()))
        con.executemany("INSERT INTO has_segment VALUES (?,?)", has_segment)
    if entities:
        con.executemany("INSERT INTO entities VALUES (?,?,?,?,NULL,NULL)", list(entities.values()))
    if claims:
        con.executemany("INSERT INTO claims VALUES (?,?,?,?,?)", list(claims.values()))
        con.executemany("INSERT INTO about VALUES (?,?)", about)
    if has_concept:
        con.executemany("INSERT INTO has_concept VALUES (?,?,?,?,?,?,?,?,?,?)", has_concept)

    if skipped_no_id:
        log.warning("skipped %d concepts.json files with no video_id (run repair-metas)", skipped_no_id)

    ground_mentions(con)
    ground_claims(con)

    counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ALL_TABLES}
    return counts


def ground_mentions(con) -> int:
    """Corpus-wide lexical grounding: every surface-term entity is searched
    against every transcript segment (word-boundary regex over a contains
    prefilter). This is what glues per-video concept cliques together - only
    5.8 percent of surface phrases repeat across videos in concepts.json,
    but their literal text appears in far more transcripts than that."""
    con.execute("DELETE FROM mentions")
    con.execute(
        f"""
        INSERT INTO mentions
        SELECT s.segment_id, e.entity_id
        FROM segments s
        JOIN entities e
          ON length(e.canonical) >= {MIN_PHRASE_LEN}
         AND contains(lower(s.text), e.canonical)
         AND regexp_matches(lower(s.text), e.match_pattern)
        """
    )
    n = con.execute("SELECT count(*) FROM mentions").fetchone()[0]
    log.info("lexical grounding: %d segment-entity mentions", n)
    return n


def ground_claims(con) -> int:
    """Fill has_concept.segment_id/grounded and write provenance-bearing
    expresses rows where an observation's surface phrase literally occurs in
    a segment of the SAME artifact. Claims without an expresses row stay in
    the store but are never presentable (gate requirement 3)."""
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _grounding AS
        SELECT hc.rowid AS hc_rowid,
               hc.artifact_id, hc.concept_id, hc.entity_id, hc.confidence,
               hc.extractor_model, hc.prompt_version, hc.extracted_at,
               min_by(s.segment_id, s.position) AS segment_id,
               min_by(s.text, s.position) AS quote
        FROM has_concept hc
        JOIN mentions m ON m.entity_id = hc.entity_id
        JOIN segments s ON s.segment_id = m.segment_id AND s.artifact_id = hc.artifact_id
        GROUP BY ALL
        """
    )
    con.execute(
        """
        UPDATE has_concept
        SET segment_id = g.segment_id, grounded = TRUE
        FROM _grounding g
        WHERE has_concept.rowid = g.hc_rowid
        """
    )
    con.execute("DELETE FROM expresses")
    con.execute(
        """
        INSERT INTO expresses
        SELECT g.segment_id,
               g.artifact_id || '|' || g.concept_id || '|' || g.entity_id,
               g.quote,
               g.confidence,
               g.extractor_model || '+lexical-grounding-v1',
               g.prompt_version,
               try_cast(g.extracted_at AS TIMESTAMP)
        FROM _grounding g
        """
    )
    n = con.execute("SELECT count(*) FROM expresses").fetchone()[0]
    grounded = con.execute("SELECT count(*) FROM has_concept WHERE grounded").fetchone()[0]
    total = con.execute("SELECT count(*) FROM has_concept").fetchone()[0]
    log.info("claim grounding: %d expresses rows; %d/%d has_concept rows grounded", n, grounded, total)
    return n


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------


def compute_co_occurrence(con, max_df: int = MAX_DF_DEFAULT, min_shared: int = MIN_SHARED_DEFAULT) -> int:
    """Materialize the entity co-occurrence table from shared artifacts.

    An entity is 'observed in' an artifact via a concepts.json row
    (has_concept) OR a lexical mention in that artifact's transcript
    (mentions -> segments). Entities above max_df artifacts are dropped as
    hub noise before pairing. NO taxonomy/concept edges - answer key only.
    """
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE _obs AS
        WITH raw AS (
            SELECT artifact_id, entity_id FROM has_concept
            UNION
            SELECT s.artifact_id, m.entity_id FROM mentions m JOIN segments s USING (segment_id)
        ),
        df AS (
            SELECT entity_id, count(DISTINCT artifact_id) AS df FROM raw GROUP BY 1
        )
        SELECT r.artifact_id, r.entity_id
        FROM raw r JOIN df USING (entity_id)
        WHERE df.df <= ?
        """,
        [int(max_df)],
    )
    con.execute("DELETE FROM co_occurs")
    con.execute(
        """
        INSERT INTO co_occurs
        SELECT a.entity_id, b.entity_id, count(DISTINCT a.artifact_id) AS weight
        FROM _obs a JOIN _obs b
          ON a.artifact_id = b.artifact_id AND a.entity_id < b.entity_id
        GROUP BY 1, 2
        HAVING count(DISTINCT a.artifact_id) >= ?
        """,
        [int(min_shared)],
    )
    n = con.execute("SELECT count(*) FROM co_occurs").fetchone()[0]
    log.info("co-occurrence: %d edges (max_df=%d, min_shared=%d)", n, max_df, min_shared)
    return n


def _community_call(algo: str, gamma: float) -> str:
    """Cypher for the community-detection stream call.

    louvain: what issue #85 names, but GDS Louvain accepts no randomSeed, so
    partitions vary run-to-run even at concurrency=1 (observed on the real
    corpus: 69 vs 70 communities across two runs, flipping a boundary pair).
    leiden: Louvain's successor; seeded + concurrency=1 + a deterministically
    ordered projection -> fully reproducible. gamma is Leiden's resolution
    knob (lower = coarser communities); cross-vocabulary bridges are
    partition-scale-dependent, so gamma is a first-class lever here.
    """
    if algo == "leiden":
        return f"""
            CALL gds.leiden.stream('{GDS_GRAPH_NAME}',
                {{relationshipWeightProperty: 'weight', concurrency: 1,
                  randomSeed: {PERMUTATION_SEED}, gamma: {float(gamma)}}})
            YIELD nodeId, communityId
            RETURN gds.util.asNode(nodeId).entity_id AS eid, communityId
            """
    return f"""
        CALL gds.louvain.stream('{GDS_GRAPH_NAME}',
            {{relationshipWeightProperty: 'weight', concurrency: 1}})
        YIELD nodeId, communityId
        RETURN gds.util.asNode(nodeId).entity_id AS eid, communityId
        """


def project_to_neo4j(
    con, uri: str, user: str, password: str, algo: str = "leiden", gamma: float = 1.0
) -> dict[str, Any]:
    """Wipe and rebuild the disposable VI_ projection, run community
    detection (Leiden by default, seeded and deterministic; Louvain on
    request) + PageRank via GDS, write results back into DuckDB entities."""
    GraphDatabase = require_neo4j()
    # Invalidate previous algorithm outputs BEFORE the fallible external
    # phase: a projection that dies mid-way must leave `verify` loudly
    # stateless ("run project first"), never silently serving the previous
    # run's communities as current.
    con.execute("CREATE TABLE IF NOT EXISTS projection_meta (algo VARCHAR, gamma DOUBLE, projected_at TIMESTAMP)")
    con.execute("UPDATE entities SET community_id = NULL, pagerank = NULL")
    con.execute("DELETE FROM projection_meta")
    # ORDER BY everywhere: GDS seeds tie-breaking off internal node ids,
    # which follow insertion order - an unordered UNION here made even
    # seeded Leiden runs differ.
    nodes = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT entity_a FROM co_occurs UNION SELECT DISTINCT entity_b FROM co_occurs ORDER BY 1"
        ).fetchall()
    ]
    edges = con.execute("SELECT entity_a, entity_b, weight FROM co_occurs ORDER BY 1, 2").fetchall()
    log.info("projecting %d nodes / %d edges into Neo4j at %s", len(nodes), len(edges), uri)

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            s.run(f"MATCH (n:{NODE_LABEL}) DETACH DELETE n")
            s.run(f"CREATE INDEX vi_entity_id IF NOT EXISTS FOR (n:{NODE_LABEL}) ON (n.entity_id)")
            for i in range(0, len(nodes), 2000):
                s.run(
                    f"UNWIND $batch AS eid CREATE (:{NODE_LABEL} {{entity_id: eid}})",
                    batch=nodes[i : i + 2000],
                )
            for i in range(0, len(edges), 2000):
                s.run(
                    f"""
                    UNWIND $batch AS row
                    MATCH (a:{NODE_LABEL} {{entity_id: row[0]}}), (b:{NODE_LABEL} {{entity_id: row[1]}})
                    CREATE (a)-[:CO_OCCURS {{weight: row[2]}}]->(b)
                    """,
                    batch=[list(e) for e in edges[i : i + 2000]],
                )
            s.run(f"CALL gds.graph.drop('{GDS_GRAPH_NAME}', false)")
            s.run(
                f"""
                CALL gds.graph.project('{GDS_GRAPH_NAME}', '{NODE_LABEL}',
                    {{CO_OCCURS: {{orientation: 'UNDIRECTED', properties: 'weight'}}}})
                """
            )
            communities = s.run(_community_call(algo, gamma)).values()
            pagerank = s.run(
                f"""
                CALL gds.pageRank.stream('{GDS_GRAPH_NAME}',
                    {{relationshipWeightProperty: 'weight', concurrency: 1}})
                YIELD nodeId, score
                RETURN gds.util.asNode(nodeId).entity_id AS eid, score
                """
            ).values()
            s.run(f"CALL gds.graph.drop('{GDS_GRAPH_NAME}', false)")
    finally:
        driver.close()

    # Set-based write-back (one UPDATE ... FROM, same pattern as
    # ground_claims) instead of per-row executemany UPDATEs.
    ranks = dict(pagerank)
    rows = [(eid, community, ranks.get(eid)) for eid, community in communities]
    con.execute("CREATE OR REPLACE TEMP TABLE _algo_out (entity_id VARCHAR, community_id BIGINT, pagerank DOUBLE)")
    con.executemany("INSERT INTO _algo_out VALUES (?,?,?)", rows)
    con.execute(
        "UPDATE entities SET community_id = a.community_id, pagerank = a.pagerank "
        "FROM _algo_out a WHERE entities.entity_id = a.entity_id"
    )
    n_comm = con.execute("SELECT count(DISTINCT community_id) FROM entities WHERE community_id IS NOT NULL").fetchone()[
        0
    ]
    con.execute("CREATE TABLE IF NOT EXISTS projection_meta (algo VARCHAR, gamma DOUBLE, projected_at TIMESTAMP)")
    con.execute("DELETE FROM projection_meta")
    con.execute("INSERT INTO projection_meta VALUES (?, ?, current_timestamp)", [algo, gamma])
    log.info(
        "%s (gamma=%s): %d communities over %d nodes; pagerank written back", algo, gamma, n_comm, len(communities)
    )
    return {"nodes": len(nodes), "edges": len(edges), "communities": n_comm, "algo": algo, "gamma": gamma}


# ---------------------------------------------------------------------------
# verify - the issue #85 acceptance gate
# ---------------------------------------------------------------------------


def alias_cohesion(con) -> dict[str, Any]:
    """Gate 1: for each taxonomy concept observed as >= 2 distinct surface
    terms (with communities), cohesion = max fraction of its terms in one
    community. Compared against a label-permutation baseline so a
    megacommunity cannot fake a win."""
    rows = con.execute(
        """
        SELECT hc.concept_id, e.entity_id, e.community_id
        FROM (SELECT DISTINCT concept_id, entity_id FROM has_concept) hc
        JOIN entities e USING (entity_id)
        WHERE e.community_id IS NOT NULL
        ORDER BY hc.concept_id, e.entity_id
        """
    ).fetchall()
    by_concept: dict[str, list[int]] = {}
    for concept_id, _eid, community in rows:
        by_concept.setdefault(concept_id, []).append(community)
    sets = {c: comms for c, comms in by_concept.items() if len(comms) >= 2}

    def mean_cohesion(assign: dict[str, list[int]]) -> float:
        vals = [max(Counter(comms).values()) / len(comms) for comms in assign.values()]
        return sum(vals) / len(vals) if vals else 0.0

    observed = mean_cohesion(sets)

    all_labels = [c for comms in sets.values() for c in comms]
    rng = random.Random(PERMUTATION_SEED)
    baselines = []
    for _ in range(PERMUTATION_REPS):
        rng.shuffle(all_labels)
        i, permuted = 0, {}
        for cid, comms in sets.items():
            permuted[cid] = all_labels[i : i + len(comms)]
            i += len(comms)
        baselines.append(mean_cohesion(permuted))
    baseline = sum(baselines) / len(baselines) if baselines else 0.0

    perfect = sum(1 for comms in sets.values() if len(set(comms)) == 1)
    return {
        "alias_sets_evaluated": len(sets),
        "mean_cohesion": round(observed, 4),
        "permutation_baseline": round(baseline, 4),
        "lift": round(observed - baseline, 4),
        "fully_cohesive_sets": perfect,
    }


def _terms_matching(con, pattern: str) -> list[tuple[str, int]]:
    """Entities (with a community) whose canonical form contains pattern."""
    return con.execute(
        "SELECT entity_id, community_id FROM entities WHERE community_id IS NOT NULL AND contains(canonical, ?)",
        [normalize_phrase(pattern)],
    ).fetchall()


def _anchor_terms(con, phrase: str, limit: int = 200) -> list[tuple[str, int]]:
    """Fallback vocabulary resolution: artifacts whose transcript contains
    the phrase -> the surface terms observed in those artifacts. Used when a
    query-side vocabulary ('reliable agents') has no surface-term entity."""
    return con.execute(
        """
        WITH anchors AS (
            SELECT DISTINCT artifact_id FROM segments WHERE contains(lower(text), ?)
        ),
        terms AS (
            SELECT hc.entity_id FROM has_concept hc JOIN anchors USING (artifact_id)
            UNION ALL
            SELECT m.entity_id FROM mentions m JOIN segments s USING (segment_id)
            JOIN anchors a ON a.artifact_id = s.artifact_id
        )
        SELECT e.entity_id, e.community_id
        FROM (
            SELECT t.entity_id, count(*) AS n
            FROM terms t JOIN entities e2 ON e2.entity_id = t.entity_id
            WHERE e2.community_id IS NOT NULL
            GROUP BY 1 ORDER BY n DESC, t.entity_id LIMIT ?
        ) ranked
        JOIN entities e USING (entity_id)
        """,
        [normalize_phrase(phrase), limit],
    ).fetchall()


def _citation_for_phrase(con, phrases: list[str]) -> dict[str, Any] | None:
    """Earliest-published verbatim segment quote for any of the phrases.

    Word-boundary matched, same discipline as grounding: a presented quote
    for 'cursor' must never be a 'precursor' hit (Codex peer-review catch).
    """
    for phrase in phrases:
        row = con.execute(
            """
            SELECT s.text, s.start_seconds, a.title, a.url, a.artifact_id, so.name
            FROM segments s
            JOIN artifacts a ON a.artifact_id = s.artifact_id
            JOIN sources so ON so.source_id = a.source_id
            WHERE contains(lower(s.text), ?) AND regexp_matches(lower(s.text), ?)
            ORDER BY a.published_at NULLS LAST, s.start_seconds
            LIMIT 1
            """,
            [normalize_phrase(phrase), match_pattern_for(phrase)],
        ).fetchone()
        if row:
            text, seconds, title, url, video_id, channel = row
            return {
                "quote": quote_around(text, phrase),
                "video": title,
                "video_id": video_id,
                "channel": channel,
                "timestamp_seconds": seconds,
                "url": timestamped_url(url, seconds),
            }
    return None


# Verbs that signal a tool migration; used to prefer co-mention segments
# whose text actually evidences a SHIFT, not just two product names nearby.
# Word-boundary regexes, not substrings: bare 'shift' matched 'shift-tab'
# (a keyboard shortcut) on the real corpus and produced a slop citation.
SHIFT_MARKERS = [
    r"\bmoved\b",
    r"\bswitch(?:ed|ing)?\b",
    r"\bshift(?:ed|ing)\b",
    r"\bmigrat\w*\b",
    r"\bover to\b",
    r"\binstead of\b",
    r"\breplaced\b",
]


def _citation_for_comention(con, a: str, b: str) -> dict[str, Any] | None:
    """Best co-mention segment for two phrases. Segments containing a
    migration verb outrank plain co-mentions - a claim named 'shifted X -> Y'
    should not be cited with a quote that merely names both tools."""
    rows = con.execute(
        """
        SELECT s.text, s.start_seconds, ar.title, ar.url, ar.artifact_id, so.name
        FROM segments s
        JOIN artifacts ar ON ar.artifact_id = s.artifact_id
        JOIN sources so ON so.source_id = ar.source_id
        WHERE contains(lower(s.text), ?) AND contains(lower(s.text), ?)
          AND regexp_matches(lower(s.text), ?) AND regexp_matches(lower(s.text), ?)
        ORDER BY ar.published_at NULLS LAST, s.start_seconds
        LIMIT 100
        """,
        [normalize_phrase(a), normalize_phrase(b), match_pattern_for(a), match_pattern_for(b)],
    ).fetchall()
    if not rows:
        return None
    scored = max(rows, key=lambda r: sum(bool(re.search(m, r[0].lower())) for m in SHIFT_MARKERS))
    text, seconds, title, url, video_id, channel = scored
    marker_match = next((m for m in (re.search(p, text.lower()) for p in SHIFT_MARKERS) if m), None)
    center = marker_match.group(0) if marker_match else a
    return {
        "quote": quote_around(text, center),
        "video": title,
        "video_id": video_id,
        "channel": channel,
        "timestamp_seconds": seconds,
        "url": timestamped_url(url, seconds),
    }


MODAL_TIE_LIMIT = 3  # more all-tied communities than this = no center of gravity


def _modal_set(counts: Counter) -> set[int]:
    """Communities at the side's maximum term count (ties included), unless
    the side is degenerate: max count 1 spread over many communities means
    every community is 'modal' and the criterion would collapse to the plain
    any-overlap the gate design rejects. Such a side anchors nothing."""
    if not counts:
        return set()
    peak = max(counts.values())
    modal = {c for c, n in counts.items() if n == peak}
    if peak == 1 and len(modal) > MODAL_TIE_LIMIT:
        return set()
    return modal


def modal_anchored_shared(user_counts: Counter, creator_counts: Counter) -> set[int]:
    """Shared communities that are structurally meaningful, not scatter.

    Any-overlap is too weak for high-frequency vocabularies: with 67
    'claude code' terms spread over dozens of communities, at least one
    accidental overlap with any other term set is near-guaranteed. A shared
    community qualifies only if it is a MODAL community (dominant by term
    count, ties included) for at least one side and contains at least one
    term of the other side - i.e. one vocabulary's center of gravity
    actually reaches the other vocabulary.
    """
    if not user_counts or not creator_counts:
        return set()
    user_modal = _modal_set(user_counts)
    creator_modal = _modal_set(creator_counts)
    return {c for c in user_modal if creator_counts.get(c, 0) >= 1} | {
        c for c in creator_modal if user_counts.get(c, 0) >= 1
    }


def check_pair(con, pair: dict[str, Any], community_sizes: dict[int, int], total_nodes: int) -> dict[str, Any]:
    """Gate 2: one known cross-vocabulary pair. Recovered only if the two
    vocabulary sides share a modal-anchored, non-mega Louvain community AND
    a verbatim citation exists."""
    result: dict[str, Any] = {"pair": pair["name"], "recovered": False}

    def resolve(patterns: list[str]) -> tuple[Counter, str]:
        # Dedup by entity_id across patterns: overlapping patterns must not
        # let one surface term vote multiple times in the modal count
        # (Codex peer-review catch - inflated votes could manufacture a
        # modal community).
        terms: dict[str, int] = {}
        for p in patterns:
            terms.update(_terms_matching(con, p))
        if terms:
            return Counter(terms.values()), "entity"
        anchored: dict[str, int] = {}
        for p in patterns:
            anchored.update(_anchor_terms(con, p))
        return Counter(anchored.values()), "anchor"

    user_counts, user_mode = resolve(pair["user_patterns"])
    creator_counts, creator_mode = resolve(pair["creator_patterns"])
    result["user_communities"] = {str(c): n for c, n in user_counts.most_common(5)}
    result["creator_communities"] = {str(c): n for c, n in creator_counts.most_common(5)}
    result["user_resolution"] = user_mode
    result["creator_resolution"] = creator_mode

    shared = modal_anchored_shared(user_counts, creator_counts)
    acceptable = {c for c in shared if community_sizes.get(c, 0) / max(total_nodes, 1) < MEGACOMMUNITY_FRACTION}
    rejected_mega = sorted(shared - acceptable)
    if rejected_mega:
        result["rejected_megacommunities"] = [
            {"community": c, "size": community_sizes.get(c, 0)} for c in rejected_mega
        ]

    citation = (
        _citation_for_comention(con, *pair["comention"])
        if pair.get("comention")
        else _citation_for_phrase(con, pair.get("citation_phrases") or [])
    )
    result["citation"] = citation

    if acceptable and citation:
        result["recovered"] = True
        best = max(acceptable, key=lambda c: user_counts.get(c, 0) + creator_counts.get(c, 0))
        result["shared_community"] = best
        result["shared_community_size"] = community_sizes.get(best, 0)
        result["shared_community_term_counts"] = {
            "user": user_counts.get(best, 0),
            "creator": creator_counts.get(best, 0),
        }
    elif acceptable and not citation:
        result["miss_reason"] = "communities align but no verbatim citation found (not presentable)"
    elif shared and not acceptable:
        result["miss_reason"] = "only shared community is a megacommunity (trivial, rejected)"
    else:
        result["miss_reason"] = "no modal-anchored shared community (scatter overlap does not count)"
    return result


def run_gate(con) -> dict[str, Any]:
    """The full issue #85 acceptance gate. Requires `load` + `project`."""
    total_nodes = con.execute("SELECT count(*) FROM entities WHERE community_id IS NOT NULL").fetchone()[0]
    if total_nodes == 0:
        raise SystemExit("no communities in DuckDB - run `project` before `verify`")
    community_sizes = dict(
        con.execute("SELECT community_id, count(*) FROM entities WHERE community_id IS NOT NULL GROUP BY 1").fetchall()
    )
    top_pagerank = con.execute(
        "SELECT canonical, round(pagerank, 4) FROM entities WHERE pagerank IS NOT NULL ORDER BY pagerank DESC LIMIT 20"
    ).fetchall()
    presentable = con.execute("SELECT count(DISTINCT claim_id) FROM expresses").fetchone()[0]
    total_claims = con.execute("SELECT count(*) FROM claims").fetchone()[0]

    try:
        algo_row = con.execute("SELECT algo, gamma FROM projection_meta").fetchone()
    except Exception:
        algo_row = None
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "graph": {
            "algo": algo_row[0] if algo_row else "unknown",
            "gamma": algo_row[1] if algo_row else None,
            "communitized_entities": total_nodes,
            "communities": len(community_sizes),
            "largest_community": max(community_sizes.values()) if community_sizes else 0,
            "megacommunity_cap": MEGACOMMUNITY_FRACTION,
        },
        "gate1_alias_recovery": alias_cohesion(con),
        "gate2_known_pairs": [check_pair(con, p, community_sizes, total_nodes) for p in KNOWN_PAIRS],
        "gate3_provenance": {
            "claims_total": total_claims,
            "claims_presentable": presentable,
            "presentable_fraction": round(presentable / total_claims, 4) if total_claims else 0.0,
        },
        "pagerank_top20": [{"term": t, "score": s} for t, s in top_pagerank],
    }
    report["gate2_recovered"] = sum(1 for p in report["gate2_known_pairs"] if p["recovered"])
    return report


def render_report_md(report: dict[str, Any]) -> str:
    g1 = report["gate1_alias_recovery"]
    lines = [
        "# intel_graph verify - issue #85 acceptance gate",
        "",
        f"Generated: {report['generated_at']}",
        "",
        f"Graph: {report['graph']['communitized_entities']} entities, "
        f"{report['graph']['communities']} communities "
        f"(algo={report['graph']['algo']}, gamma={report['graph']['gamma']}), "
        f"largest = {report['graph']['largest_community']}",
        "",
        "## Gate 1 - alias recovery (taxonomy = answer key, never an edge)",
        "",
        f"- alias sets evaluated: {g1['alias_sets_evaluated']}",
        f"- mean cohesion: {g1['mean_cohesion']} vs permutation baseline {g1['permutation_baseline']} "
        f"(lift {g1['lift']})",
        f"- fully cohesive sets: {g1['fully_cohesive_sets']}",
        "",
        f"## Gate 2 - known cross-vocabulary pairs ({report['gate2_recovered']}/3 recovered)",
        "",
    ]
    for p in report["gate2_known_pairs"]:
        status = "RECOVERED" if p["recovered"] else "MISSED"
        lines.append(f"### {status}: {p['pair']}")
        lines.append("")
        if p["recovered"]:
            lines.append(
                f"- shared community {p['shared_community']} (size {p['shared_community_size']}), "
                f"resolution: user={p['user_resolution']}, creator={p['creator_resolution']}"
            )
        else:
            lines.append(f"- miss reason: {p.get('miss_reason', 'unknown')}")
        c = p.get("citation")
        if c:
            lines.append(
                f'- citation: "{c["quote"][:220]}" @ {c["video"]} ({c["channel"]}) '
                f"@ t={c['timestamp_seconds']}s {c['url']}"
            )
        else:
            lines.append("- citation: NONE FOUND")
        lines.append("")
    g3 = report["gate3_provenance"]
    lines += [
        "## Gate 3 - provenance",
        "",
        f"- {g3['claims_presentable']}/{g3['claims_total']} claims presentable "
        f"({g3['presentable_fraction'] * 100:.1f}% carry quote @ segment @ timestamp)",
        "",
        "## PageRank top 20",
        "",
    ]
    lines += [f"- {r['term']} ({r['score']})" for r in report["pagerank_top20"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args()
    duckdb = require_duckdb()
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.command == "load":
        if args.force:
            force_unlink_db(db_path)
        con = duckdb.connect(str(db_path))
        counts = load_corpus(con, resolve_output_dir(args.output_dir))
        log.info("truth store loaded: %s", counts)
    elif args.command == "project":
        con = duckdb.connect(str(db_path))
        compute_co_occurrence(con, args.max_df, args.min_shared)
        stats = project_to_neo4j(
            con, args.neo4j_uri, args.neo4j_user, args.neo4j_password, algo=args.algo, gamma=args.gamma
        )
        log.info("projection complete: %s", stats)
    elif args.command == "verify":
        con = duckdb.connect(str(db_path))
        report = run_gate(con)
        md = render_report_md(report)
        # Persist BEFORE printing: on Windows a redirected stdout is cp1252
        # and verbatim transcript quotes routinely carry non-ASCII, so a
        # print failure must not lose the run's report files.
        if args.report:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            report_path.with_suffix(".md").write_text(md, encoding="utf-8")
            log.info("report written to %s (+ .md)", report_path)
        try:
            print(md)
        except UnicodeEncodeError:
            sys.stdout.buffer.write(md.encode("utf-8", errors="replace"))


def build_parser() -> argparse.ArgumentParser:
    # --db lives on each subcommand (shared parent parser), matching the
    # documented `intel_graph.py <command> --db PATH` form - on the top-level
    # parser alone, argparse rejects the flag after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=str(DEFAULT_DB), help=f"DuckDB path (default {DEFAULT_DB})")

    parser = argparse.ArgumentParser(description="DuckDB truth + Neo4j-GDS weak-signal lens (issue #85)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_load = sub.add_parser("load", parents=[common], help="build the DuckDB truth store from corpus artifacts")
    p_load.add_argument("--output-dir", help="corpus root (default: video_intel config resolution)")
    p_load.add_argument("--force", action="store_true", help="delete the db file and rebuild")

    p_proj = sub.add_parser(
        "project", parents=[common], help="project co-occurrence graph into Neo4j, run community detection + PageRank"
    )
    p_proj.add_argument("--neo4j-uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    p_proj.add_argument("--neo4j-user", default=os.environ.get("NEO4J_USER", "neo4j"))
    p_proj.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD", ""))
    p_proj.add_argument("--max-df", type=int, default=MAX_DF_DEFAULT)
    p_proj.add_argument("--min-shared", type=int, default=MIN_SHARED_DEFAULT)
    p_proj.add_argument(
        "--algo",
        choices=["leiden", "louvain"],
        default="leiden",
        help="community detection algorithm (leiden is seeded + deterministic; louvain varies run-to-run)",
    )
    p_proj.add_argument(
        "--gamma",
        type=float,
        default=1.0,
        help="leiden resolution: lower = coarser communities (ignored for louvain)",
    )

    p_ver = sub.add_parser("verify", parents=[common], help="run the issue #85 acceptance gate")
    p_ver.add_argument("--report", help="write JSON report here (markdown sibling with .md)")
    return parser


def force_unlink_db(db_path: Path) -> None:
    """--force deletion guard: only ever delete a .duckdb file. Every other
    destructive path in this repo (dedupe, prune-shorts) is allowlisted or
    dry-run by default; a mistyped --db must not delete an arbitrary file."""
    if not db_path.exists():
        return
    if db_path.suffix != ".duckdb":
        raise SystemExit(f"--force refuses to delete a non-.duckdb file: {db_path}")
    db_path.unlink()


if __name__ == "__main__":
    main()
