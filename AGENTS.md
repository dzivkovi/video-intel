# AGENTS.md - video-intel

**Codex (and any coding agent): read [`CLAUDE.md`](./CLAUDE.md) now, in full, before doing anything else, and follow it as written.** It is your operating manual for this repo: what the plugin is (multimodal video analysis using Gemini as a proxy), the architecture, the CLI surface and subcommands, the prompt and config conventions, the cross-cutting review principles, the testing/eval framework, and the release process. It also routes you to [`specs/agent-rules.md`](./specs/agent-rules.md), which you MUST read and adhere to before executing any task. CLAUDE.md is written agent-neutral and applies to you exactly as it applies to Claude Code.

**Per-subsystem guardrails do NOT live in `CLAUDE.md`.** They are in [`.claude/rules/*.md`](.claude/rules/), which Claude Code auto-loads when a session reads a matching source path - **and which you, Codex, do not auto-load at all.** So before reviewing or editing, read the rule file for the subsystem you are touching. They are binding, exactly like `CLAUDE.md`:

| If you touch | Read |
|---|---|
| the transcript call, chunking, quality assessment, captions | `.claude/rules/transcript.md` |
| `config.yaml`, scan channel gates, manual `--url`, dedupe, prune-shorts | `.claude/rules/scan-config.md` |
| the LanceDB index, search, concepts, taxonomy, topics, nugget | `.claude/rules/search-index.md` |
| the retrieval eval, model scorecards, the mindmap prompt | `.claude/rules/evals.md` |
| briefings, the headline digest, the personalization profile | `.claude/rules/briefings.md` |
| README, INSTALLATION, `docs/`, the SKILL.md files | `.claude/rules/docs-currency.md` |
| `intel_graph.py`, `lead_lag_report.py`, `burst_report.py`, `sdsm_network.py` | `.claude/rules/intelligence-layer.md` |
| `translate_video.py`, its prompts, its tests | `.claude/rules/translate-bcs.md` |

Most of them govern `scripts/video_intel.py`, so a change to that file usually means reading more than one. A review that skips them is a review with the guardrails switched off.

**Source of truth: [`CLAUDE.md`](CLAUDE.md) plus those rule files.** This file is intentionally a thin pointer so they cannot drift. Do not add instructions here; put every durable change in `CLAUDE.md` or the matching rule file, never in this file.

(Why this shape: Claude Code auto-loads `CLAUDE.md` natively and does not read `AGENTS.md`; Codex auto-loads `AGENTS.md`. Pointing `AGENTS.md` at `CLAUDE.md` gives both tools one canon, zero duplication.)
