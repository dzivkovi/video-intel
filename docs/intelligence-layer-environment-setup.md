# Intelligence-Layer Environment Setup: Neo4j + GDS + DuckDB

This is the environment prep guide for the DuckDB-truth / Neo4j-graph intelligence layer described in [`docs/brainstorms/2026-05-28-intelligence-layer-roadmap.md`](brainstorms/2026-05-28-intelligence-layer-roadmap.md) and tracked in [issue #85](https://github.com/dzivkovi/video-intel/issues/85). Neither database is required for the core video-intel pipeline (scan/transcript/mindmap/search) — this is only needed once you're working on the intelligence layer.

If something goes wrong following this guide, check [`docs/solutions/integration-issues/neo4j-gds-mcp-local-setup-and-auth-gotchas-20260701.md`](solutions/integration-issues/neo4j-gds-mcp-local-setup-and-auth-gotchas-20260701.md) first — it documents the specific failure modes this guide's steps are designed to avoid, condensed from about a year and a half of hands-on Neo4j/GDS work across several projects.

## DuckDB — the truth store

**What it is.** An embedded, in-process analytical (OLAP) database — think "SQLite for analytics," not a server. There is nothing to run in the background, nothing to containerize, nothing to authenticate against. It lives inside your Python process and reads/writes either a single `.db` file or works fully in-memory.

**Install.**

```bash
pip install duckdb
```

That's the entire installation. No Docker, no plugins, no version-matching, no auth. Verify it:

```bash
python -c "import duckdb; con = duckdb.connect(':memory:'); con.execute('CREATE TABLE t (id INT)'); con.execute('INSERT INTO t VALUES (1)'); print(con.execute('SELECT * FROM t').fetchall())"
# expect: [(1,)]
```

This project pins it as an optional dependency — install with the rest of the intelligence-layer extras via `pip install -e ".[intelligence]"`.

**Why DuckDB specifically, and why it's the right fit here:**

- **It answers questions hybrid search structurally cannot.** BM25 + vector retrieval finds passages *similar* to a query. It cannot compute "which concept was mentioned by 3+ creators in the last 30 days" (an aggregate) or "which tool did creators stop using" (a negation/polarity question — vector embeddings collapse negation, so "love X" and "no longer love X" sit close together in embedding space). Those are `GROUP BY` / `JOIN` / `WHERE NOT` questions, and DuckDB runs them directly over structured claims extracted from the corpus.
- **It's embedded, matching the project's existing philosophy.** LanceDB (the vector index this project already uses) is also embedded and serverless — no daemon to manage, no ops burden, works the same on a laptop as in CI. DuckDB extends that same "no infrastructure" posture to the structured/analytical side instead of reaching for a server database.
- **Arrow-native interop with LanceDB.** Both DuckDB and LanceDB are built on Apache Arrow's columnar format, so data can move between the structured truth store (DuckDB) and the retrieval index (LanceDB) with zero-copy, zero-serialization overhead. This is a proven, years-old integration pattern, not a novel pairing.
- **Reads flat files directly, no ETL.** The corpus already exists on disk as `concepts.json`, `taxonomy.json`, and transcript markdown. DuckDB can query JSON/CSV/Parquet files in place — no separate ingestion pipeline is required just to get data in front of SQL.
- **Columnar + vectorized execution.** Aggregate queries over thousands of rows run fast on ordinary hardware — this project's corpus (a few thousand chunks) is nowhere near the scale where this matters, but it means the same code scales without a rewrite if the corpus grows by 100x.
- **SQL is a stable, auditable interface.** Any future contributor — or an autonomous coding agent — can read a DuckDB query and know exactly what it does. That's a meaningfully different trust surface than opaque vector-similarity math, which matters for a project whose whole premise is provenance and citeable claims.
- **Portable by construction.** The entire "truth" artifact is one file (or an in-memory session dumped to one file). Copying, versioning, and backing it up is a `cp`, not a database migration.

This maps directly onto the project's own architectural thesis (see `examples/nugget-lightrag-vs-openbrain-architectural-tension.md`): **SQL is the immutable source of truth; any graph is a disposable, regenerable presentation layer on top of it.** DuckDB is the concrete implementation of the "SQL = truth" half of that sentence.

## Neo4j + Graph Data Science (GDS) — the regenerable graph lens

**What it is.** A graph database, run here as a local Docker container. Unlike DuckDB, this genuinely is a server with plugins, auth, and memory tuning to get right — which is why it's finicky and why this section is longer. The **Graph Data Science (GDS)** plugin is what actually ships the graph algorithms (Louvain community detection, PageRank, betweenness centrality) — base Neo4j does not include them.

**The proven `docker run` command** (image pinned, memory tuned for local dev, the two `security_procedures` lines are load-bearing — see why in the solutions doc):

```bash
docker run -d --name neo4j-fable --memory=8g --memory-swap=8g \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/Sup3rSecur3! \
  -e NEO4J_PLUGINS='["apoc","graph-data-science"]' \
  -e NEO4J_dbms_security_procedures_unrestricted=apoc.*,gds.*,db.* \
  -e NEO4J_dbms_security_procedures_allowlist=apoc.*,gds.*,db.* \
  -e NEO4J_server_memory_heap_initial__size=4G \
  -e NEO4J_server_memory_heap_max__size=4G \
  -e NEO4J_server_memory_pagecache_size=4G \
  -e NEO4J_db_transaction_timeout=120s \
  -e NEO4J_db_lock_acquisition_timeout=120s \
  neo4j:5.26.7-community
```

**Validate at the database level first — before trusting any client on top of it.** This is the single most important habit for this stack: Neo4j Browser and Cypher-shell talk to the database directly, with no IDE, no MCP layer, no process-environment inheritance in between. If this step passes, the database is correct and any later failure is somewhere else in the stack, not here.

```bash
docker exec neo4j-fable cypher-shell -u neo4j -p 'Sup3rSecur3!' "RETURN gds.version() AS gds, apoc.version() AS apoc, 1 AS ok;"
# expect: gds and apoc version strings, ok=1
```

Or in the browser at [http://localhost:7474](http://localhost:7474):

```cypher
RETURN gds.version() AS gds_version, apoc.version() AS apoc_version;
```

If either errors, the fix is almost always the `security_procedures` allowlist (plugins load but their procedures are blocked without it) — see the solutions doc for the exact failure signature.

**Why Neo4j + GDS for this project, specifically:**

- **The algorithms map directly onto the project's own weak-signal-detection goal.** Louvain community detection finds clusters of concepts creators discuss with different vocabulary (the exact "reliable agents" ≈ "Ralph Wiggum loop" problem hybrid search can't bridge). PageRank and betweenness centrality surface which concepts or entities are structurally central — the same algorithms used for social-network analysis (finding brokers, hubs) apply unchanged to a knowledge graph of concepts and creators.
- **It's a lens, not a second source of truth.** Per the project's architecture: DuckDB holds the claims and provenance; Neo4j is a regenerable graph *projected* from that truth for algorithms that need graph structure. If Neo4j's schema or even the choice of graph engine turns out wrong, nothing is lost — it's rebuilt from DuckDB. This is why it's safe to be opinionated about *how* Neo4j is run (Docker, local, throwaway) without that choice becoming a long-term architectural commitment.
- **GDS ships algorithms DuckDB does not.** DuckDB is excellent at relational aggregates; it is not a graph-traversal or community-detection engine (its escape hatch for that, if ever needed, is the DuckPGQ extension — still inside DuckDB, no new vendor). GDS is purpose-built for exactly the graph algorithms this project's roadmap calls for.

## MCP — the interactive lens (optional, for humans exploring the graph)

The DuckDB/Neo4j build itself talks to Neo4j through the Python `neo4j` driver directly — MCP is **not required** for the intelligence-layer build to run. Wire it up if you (or Claude) want to query the graph conversationally during development or review.

`.mcp.json`:

```json
{
  "mcpServers": {
    "neo4j-fable": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-neo4j-cypher@0.6.0", "--transport", "stdio"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "Sup3rSecur3!",
        "NEO4J_DATABASE": "neo4j"
      }
    }
  }
}
```

The password is hardcoded intentionally here, not indirected through an environment variable. This is a deliberate, evidence-based choice, not a shortcut: the database is bound to `localhost` only (nothing outside the machine can reach it), the value was verified clean against `gitleaks` (both `gitleaks dir` and `gitleaks stdin`, exit 0 either way — it does not match gitleaks' default secret-detection rules), and environment-variable indirection for this specific credential turned out to add real risk (a stale process environment silently serving a wrong or empty value) without buying any real protection. Match the security effort to the actual exposure: a real API key or a production credential earns indirection; a throwaway localhost dev password for a local graph spike does not.

Tools exposed once connected: `read_neo4j_cypher`, `write_neo4j_cypher` (can be disabled with `NEO4J_READ_ONLY=true` for safe exploration), and `get_neo4j_schema` (requires APOC, which the `docker run` command above already installs).

**Validate the full path**, not just the "Connected" status shown by `/mcp` or `claude mcp list` — that status only confirms the MCP protocol handshake succeeded, not that the underlying Neo4j credentials are valid. Run an actual query through the tool and confirm it returns data:

```
RETURN gds.version() AS gds_version, apoc.version() AS apoc_version, 1 AS ok
```

If it errors with `Neo.ClientError.Security.Unauthorized` despite a correct `.mcp.json` and a database already proven to work via `cypher-shell`, see the solutions doc — this is almost always a stale process environment, not a config problem.

## Precondition checklist

- [ ] `docker exec neo4j-fable cypher-shell ...` returns `gds`/`apoc` versions and `ok=1`
- [ ] `SHOW PROCEDURES` includes `gds.louvain.stream`, `gds.pageRank.stream`, `gds.betweenness.stream`
- [ ] `python -c "import duckdb; ..."` round-trips a create/insert/query
- [ ] (optional) MCP tool call returns real data, not just a "Connected" status
