# AGENTS.md - video-intel

**Codex (and any coding agent): read [`CLAUDE.md`](./CLAUDE.md) now, in full, before doing anything else, and follow it as written.** It is your complete operating manual for this repo: what the plugin is (multimodal video analysis using Gemini as a proxy), the architecture, the CLI surface and subcommands, the prompt and config conventions, the code-review guardrails, the testing/eval framework, and the release process. It also routes you to [`specs/agent-rules.md`](./specs/agent-rules.md), which you MUST read and adhere to before executing any task. CLAUDE.md is written agent-neutral and applies to you exactly as it applies to Claude Code.

**This repo keeps ONE source of truth: [`CLAUDE.md`](CLAUDE.md).** This file is intentionally a thin pointer so the two cannot drift. Do not add instructions here; put every durable change in `CLAUDE.md`, never in this file.

(Why this shape: Claude Code auto-loads `CLAUDE.md` natively and does not read `AGENTS.md`; Codex auto-loads `AGENTS.md`. Pointing `AGENTS.md` at `CLAUDE.md` gives both tools one canon, zero duplication.)
