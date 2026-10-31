---
title: "Smoke sandboxes that cannot spend, leak or touch live data"
date: 2026-10-31
category: workflow-issues
module: smoke-test-harnesses
problem_type: workflow_issue
component: development_workflow
severity: high
applies_when:
  - "A smoke or Gate 1 harness drives the real CLI against a copy of the corpus"
  - "A smoke claims zero paid calls through the lazy-skip path"
  - "A smoke needs the real config (for example with injected typos) instead of a parked one"
  - "A harness runs on Windows and writes long per-video artifact names"
tags: [smoke-test, sandbox, gate1, lazy-skip, is-processed, config-snapshot, public-repo, windows, max-path]
---

# Smoke sandboxes that cannot spend, leak or touch live data

## Context

A smoke harness is only useful if it cannot do damage while proving something. Four separate harness defects showed how a "safe" sandbox can still spend money, write corpus copies into a public repo, or rewrite the live config record. Each one looked plausible until a real run exposed it. The CLAUDE.md "Smoke-test safety" note already covers parking `config.yaml` so `VIDEO_INTEL_OUTPUT_DIR` wins; this doc covers what that note does not.

## Guidance

- **Copy the full per-video artifact set, not metas alone.** `is_processed()` resolves the prefix through the `video_id` index built from metas, but the actual "done" check in `_mode_artifact_present` is a non-empty `{prefix}.mindmap.md` or `{prefix}.transcript.md`. A sandbox of metas and `concepts.json` only looks complete and is not: one "zero paid calls" lazy-skip smoke silently ran a real transcript and mindmap. Copy every artifact, then assert that no `usage ...` line (the line `log_usage_metadata` emits on each Gemini call made by `process_mindmap`, `process_transcript` and `process_concepts`) appears in the log.
- **Write outputs only to a temp dir or a path `git check-ignore` confirms.** A harness that reassigns `sys.argv` to drive the CLI and then derives its output folder from `sys.argv[0]` lands in the repo root. In a public repo one `git add -A` then publishes corpus copies. The `.gitignore` pattern `gate1_*_out/` is a backstop, not the plan.
- **When the smoke needs the real config, redirect `output_dir` inside a copy instead of parking it.** `cmd_scan` calls `backup_config_if_changed(output_dir)` before its first fetch and before the `--dry-run` return, so even a dry run with an edited config writes a snapshot into whatever corpus `output_dir` names. Point the copied config at a scratch corpus, and afterwards check that the live `config.latest.yaml` kept its mtime.
- **One copied tree per paid leg.** Run each paid leg from its own copy of `scripts/`, `prompts/` and a scratch config, so another agent can keep editing the worktree without changing what the leg executes.
- **Use a generated clip for local-file paths.** An ffmpeg-generated 5-second clip exercises `process --file` cheaply. Run it twice: the second run must make zero uploads, which proves the lazy-skip.
- **Keep smoke roots short on Windows.** Sidecar names run to about 130 characters (a 91-character prefix plus a suffix like `.transcript.raw.chunk12-12000-15000.txt`), so a deep smoke root pushes them past the 260-character MAX_PATH limit.

## Why This Matters

Each defect fails quietly. The metas-only sandbox reports success while billing Gemini. The repo-root output looks like scratch until it is pushed. The config snapshot is content-compared and silent when it writes, so a wrong `config.latest.yaml` misleads every later scan. A harness that cannot tell pass from fail is worse than no harness, because it is believed.

## When to Apply

- Before any smoke that claims "no paid calls", "no live writes" or "dry run only"
- Before running any harness that writes files from a repo checkout
- When a path a glob just returned raises `FileNotFoundError` on Windows

## Examples

A path that a glob returned a moment ago, then `FileNotFoundError` on open: the Windows 260-character path limit is one known cause, not the diagnosis. Measure the full path length before concluding anything. "It reproduces on main too" proves nothing when both runs share the same environment and the same long root; one bug was nearly filed against healthy code this way.

Pre-flight checklist for a smoke harness:

- Full artifact set copied; assert no `usage` line on the lazy-skip leg
- Output dir is a temp dir, or `git check-ignore <path>` exits 0
- Copied config with `output_dir` redirected; live `config.latest.yaml` mtime unchanged afterwards
- Each paid leg runs from its own copied tree under a short root
- Never test a secret with any `${v:-...}` form, because it prints the value; use `[ -n "$v" ]`

## Related

- CLAUDE.md, "Smoke-test safety" and "Idempotency"
- `.claude/rules/scan-config.md`, config snapshot invariants
- `docs/troubleshooting.md` has no MAX_PATH row yet; adding one is a pending follow-up
