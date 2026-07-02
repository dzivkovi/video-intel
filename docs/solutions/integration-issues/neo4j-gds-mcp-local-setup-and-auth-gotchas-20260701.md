---
title: Neo4j 5.x + GDS Docker setup gotchas, and an MCP auth failure caused by a stale VS Code process environment
date: 2026-07-01
category: integration-issues
module: intelligence-layer-environment
problem_type: integration_issue
component: tooling
symptoms:
  - "Neo4j container exits immediately with 'Unrecognized setting: db.tx_timeout'"
  - "GDS/APOC plugin jars install successfully but gds.* / apoc.* procedures fail when called"
  - "claude mcp list / the /mcp panel shows an MCP server as '✔ Connected' but every actual query returns Neo.ClientError.Security.Unauthorized"
  - "Fixing .mcp.json, ~/.bashrc, and reconnecting the MCP server repeatedly does not resolve the auth failure"
  - "Multiple mcp-neo4j-cypher server processes accumulate across 'Reconnect' clicks instead of the old one being replaced"
root_cause: environment_state
resolution_type: config_change
severity: medium
tags:
  - neo4j
  - graph-data-science
  - docker
  - mcp
  - claude-code
  - windows
  - environment-variables
related_components:
  - documentation
---

# Neo4j 5.x + GDS Docker setup gotchas, and an MCP auth failure caused by a stale VS Code process environment

## Problem

Two separate but easily-conflated problems when standing up a local Neo4j + Graph Data Science (GDS) environment for the [intelligence-layer roadmap](../../brainstorms/2026-05-28-intelligence-layer-roadmap.md) and wiring an MCP server to it for interactive use:

1. **Neo4j 5.x Docker configuration gotchas** that produce either a container that won't boot, or one that boots but silently blocks the GDS/APOC procedures the whole exercise exists to use.
2. **An MCP server that authenticates fine at the protocol level but fails every real query** — and the failure persisted through a dozen plausible-looking fixes (editing `.mcp.json`, fixing a shell profile, clicking "Reconnect," restarting VS Code) because the actual root cause was outside all of those layers.

Both were fully diagnosed and resolved in one session; this doc exists so the next person (or the next Fable/Claude session) doesn't re-spend the time.

## Symptoms

**Neo4j boot failure:**

```
Failed to read config: Unrecognized setting. No declared setting with name: db.tx_timeout.
Run with '--verbose' for a more detailed error message.
```

`docker ps -a` shows the container as `Exited (1)` shortly after start. Neo4j 5.x has strict config validation on by default (`server.config.strict_validation.enabled`), and an invalid environment-variable-derived setting name is a hard boot failure, not a warning.

**GDS/APOC procedures blocked despite successful plugin install:**

Docker logs show the plugin jars downloading and installing cleanly (`Installing Plugin 'graph-data-science' from https://graphdatascience.ninja/... to /var/lib/neo4j/plugins/graph-data-science.jar`), and the container starts. But `gds.*` / `apoc.*` procedures are unusable when called — plugin loading and plugin *authorization to run* are two separate gates in Neo4j 5.x.

**MCP "Connected" but unauthorized:**

```
Error calling tool 'read_neo4j_cypher': {neo4j_code: Neo.ClientError.Security.Unauthorized}
{message: The client is unauthorized due to authentication failure.}
```

...while `claude mcp list` / the `/mcp` panel simultaneously reports `neo4j-fable: ✔ Connected`, and `claude mcp get neo4j-fable` shows the correct password value on disk.

## What Didn't Work

- **`-e NEO4J_db_tx__timeout=120s`.** Looks like a reasonable guess at the env-var-to-config-key mapping Neo4j's Docker image uses, but it maps to the invalid setting `db.tx_timeout`. The correct key is `NEO4J_db_transaction_timeout`.
- **`NEO4J_PLUGINS='["graph-data-science","apoc"]'` alone.** This installs the plugin jars correctly (confirmed via GDS auto-downloading the correct version, `2.13.4`, for Neo4j `5.26.7`) but does **not** authorize their procedures to run. Neo4j 5.x separately gates *which* procedures are callable via `dbms.security.procedures.allowlist` / `dbms.security.procedures.unrestricted`. Without also setting `NEO4J_dbms_security_procedures_unrestricted=apoc.*,gds.*,db.*` and the matching `_allowlist`, `gds.version()` and friends are blocked at call time even though the jars loaded.
- **Trusting `claude mcp list` / the `/mcp` panel's "✔ Connected" status as proof of a working connection.** This status only reflects that the MCP stdio handshake (process launch + JSON-RPC `initialize`) succeeded. It says nothing about whether the underlying Neo4j credentials the server was launched with are actually valid. A server can be fully "Connected" by this definition and fail every real query.
- **`${NEO4J_PASSWORD}` / `${NEO4J_PASSWORD:-default}` interpolation in `.mcp.json`, on its own.** Claude Code genuinely supports this syntax (documented: `${VAR}` expands to the env var; `${VAR:-default}` falls back if unset). The syntax was never the bug. The problem was *which* process environment Claude Code was expanding it against.
- **Fixing `~/.bashrc` and re-exporting the password in an interactive shell.** A password with a trailing `!` had been mangled by bash history expansion when originally exported (`echo "...!" >> ~/.bashrc` ate the `!`), producing a silently-wrong stored value. Fixing this made *interactive shells* correct — but the VS Code extension host that actually spawns MCP servers does not source `~/.bashrc`, so this fix had zero effect on the failing connection.
- **Clicking "Reconnect" in the `/mcp` panel, repeatedly.** Each click did spawn a *new* `mcp-neo4j-cypher` server process — but the old ones were never terminated first. Process enumeration (`Get-CimInstance Win32_Process | Where-Object CommandLine -match 'mcp-neo4j-cypher'`) showed three separate live server trees accumulated across three "Reconnect" cycles, none of which corresponded to the moment of the actual query attempts.
- **"Restarting VS Code" by closing and reopening windows.** `Get-Process Code | Sort StartTime` showed the *oldest* `Code.exe` process surviving unchanged (same PID, same original `StartTime`, hours earlier) across multiple apparent "restarts." Windows commonly keeps a background VS Code launcher/helper process alive even when every visible window is closed. Every child process spawned from that surviving root — including every new MCP server — inherited whatever environment that root process had at its own birth, which included a stale, wrong `NEO4J_PASSWORD` (most likely inherited from a Git Bash terminal that had the mangled password exported at the moment `code .` was run from it, hours before the password was fixed).

## Root Cause

Two independent, unrelated root causes were tangled together by symptom similarity:

1. **Neo4j-side:** two separate Docker env-var mistakes — an invalid config-key name (boot failure) and a missing procedure-authorization allowlist (silent procedure-call failure despite successful plugin install).
2. **MCP-side:** a long-lived parent OS process (the VS Code root process) captured a stale/wrong environment variable at its own launch time and kept re-poisoning every child process it spawned, including every freshly-relaunched MCP server, regardless of how many times the *configuration file* or the *interactive shell* were corrected. Neither `.mcp.json` correctness nor `~/.bashrc` correctness could fix this, because the poisoned value lived one process-tree level above both of them, in a process that predated the fixes and never actually terminated.

## Solution

**For the Neo4j Docker gotchas:** use the exact env vars and image pin below (also documented, without the diagnostic detail, in [`docs/intelligence-layer-environment-setup.md`](../../intelligence-layer-environment-setup.md)):

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

**For diagnosing "MCP says Connected but queries fail":**

1. **Validate the database directly first, bypassing every client layer.** `docker exec <container> cypher-shell -u neo4j -p '<password>' "RETURN gds.version(), apoc.version(), 1 AS ok;"`. If this works, the database and credentials are provably correct, and the bug is somewhere in the client/process layer above it — stop looking at Neo4j config.
2. **Don't trust "Connected" — run a real query through the MCP tool itself** and check for an actual result, not just a status light.
3. **If a real query fails despite (1) succeeding, suspect process-environment staleness before config.** Enumerate the actual running server processes and their start times (`Get-CimInstance Win32_Process | Where-Object CommandLine -match '<server-name>' | Select ProcessId, CreationDate`) and compare against your most recent "fix" or "restart" timestamp. A process older than your last fix is using a stale environment no matter what the config file says.
4. **Check whether the true root process ever died**, not just the visible window. `Get-Process Code | Sort StartTime | Select -First 1` (or the equivalent for your IDE) — if the oldest process predates your restart attempts, the restart didn't actually happen at the process level.
5. **The definitive fix is killing the poisoned process tree, or a full OS reboot** — not another config edit. `taskkill /F /T /PID <root-pid>` on the surviving root process forces a genuinely fresh environment on the next launch.
6. **For a throwaway, localhost-only credential with no real blast radius, hardcoding it directly in the config file is a legitimate simplification, not a downgrade.** It was verified clean against `gitleaks` (`gitleaks dir` and `gitleaks stdin`, both exit 0) before making this call. Environment-variable indirection is the right tool for a credential that has real exposure (a production key, a shared team secret); it added failure surface here without adding protection, since the value poses no risk if read by anyone with access to `localhost`.

## Lesson

Match security effort to actual exposure. Indirecting a throwaway `localhost`-only dev password through `${VAR:-default}` syntax felt like the responsible default, but the credential had no real blast radius and the indirection introduced a genuine, time-consuming failure mode (a poisoned parent-process environment silently shadowing every fix) without buying any real protection. The pragmatic call — verified safe, then hardcoded — was correct from the start.
