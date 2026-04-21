---
title: "Compound Engineering v2.34 → v2.68 upgrade + Codex integration"
date: 2026-04-20
category: workflow-issues
tags: [compound-engineering, codex, plugin-migration, ce-setup, review-gate, delegation, kieran-rhetoric, claude-code, conventions]
components:
  - .claude-plugin/settings (marketplace swap)
  - .compound-engineering/config.local.yaml
  - .compound-engineering/config.local.example.yaml
  - .gitignore
  - CLAUDE.md (§ Code Review Guardrails)
  - openai-codex/codex plugin (/codex:rescue, /codex:review, /codex:setup)
severity: medium
symptoms:
  - "Running old Compound Engineering v2.34.x installed from the every-marketplace namespace; 34 versions behind latest."
  - "Old /workflows:* command prefix no longer aligns with the new skill-based /ce-* architecture."
  - "Obsolete compound-engineering.local.md at the repo root still lists review_agents in frontmatter (review-agent selection is now automatic in v2.68)."
  - "No .compound-engineering/config.local.yaml exists; CE has no machine-local state."
  - "Missing support tools flagged by /ce-setup health check (agent-browser, vhs, silicon)."
  - "Confusion about which Codex mechanism matches a 'second-opinion' workflow: work_delegate, /codex:rescue, /codex:review, or the review gate."
root_cause: >
  Two unrelated things compounded into one upgrade event.
  (1) Compound Engineering moved marketplaces (every-marketplace → compound-engineering-plugin) and removed the legacy `commands/` directory in favor of skill-based `/ce-*` triggers (v2.34 → v2.68.1). Installed repos carry stale config that references the old format.
  (2) The OpenAI Codex plugin exposes *three* distinct Codex integration surfaces (work-delegation via CE config; on-demand `/codex:review` and `/codex:rescue`; automatic per-turn review gate), and the companion's user-facing blurbs invite the wrong mental model about which does what. Without reading the plugin source, a user cannot pick the right surface for their workflow.
---

# Compound Engineering v2.34 → v2.68 upgrade + Codex integration

## Who this is for

You already have Compound Engineering installed from an older marketplace (`every-marketplace`, `compound-engineering@every-marketplace`, or anything predating v2.68). You've been running `/workflows:*` commands or copying a hand-authored `compound-engineering.local.md` at repo root. You want to catch up to the current framework, wire up Codex as a second opinion, and stop maintaining stale config.

If you're a fresh install off `EveryInc/compound-engineering-plugin` on a repo with no CE history, you can skip the "old → new migration" section and jump to "/ce-setup walkthrough."

---

## Part 1 — Upgrade from the old marketplace

### The architecture shift you're catching up to

| | v2.34.x (old) | v2.68.1 (current) |
|---|---|---|
| Marketplace | `every-marketplace` | `compound-engineering-plugin` |
| Command surface | `commands/workflows/*.md` → `/workflows:brainstorm`, `/workflows:plan`, `/workflows:review` | No `commands/` directory. ~43 skills under `skills/ce-*` triggered by description-matching. |
| Reviewer selection | Hand-authored `compound-engineering.local.md` with `review_agents:` frontmatter | Automatic tiered selection by `/ce-code-review`; no per-repo reviewer list |
| Local config | None (review list lived in markdown) | `.compound-engineering/config.local.yaml` (gitignored) + `.compound-engineering/config.local.example.yaml` (committed) |
| Codex integration | N/A | `work_delegate:` block in config + separate openai-codex plugin |

### The four-command migration

Run inside Claude Code:

```bash
/plugin uninstall compound-engineering@every-marketplace
/plugin marketplace remove every-marketplace
/plugin marketplace add EveryInc/compound-engineering-plugin
/plugin install compound-engineering@compound-engineering-plugin
/reload-plugins
```

After `/reload-plugins` you should see `compound-engineering@compound-engineering-plugin` in `/plugin` with v2.68.1 or newer. Claude Code re-indexes on reload; if `/ce-*` skills are still unrecognized, fully quit and relaunch.

### Command rename reference (for muscle memory)

| Old | New | Notes |
|---|---|---|
| `/workflows:brainstorm` | `/ce-brainstorm` | Same intent |
| `/workflows:plan` | `/ce-plan` | Same intent; `/deepen-plan` merged into it |
| `/workflows:work` | `/ce-work` (or `/ce-work-beta` for delegation-aware variant) | — |
| `/workflows:review` | `/ce-code-review` | Renamed; automatic reviewer selection |
| `/workflows:compound` | `/ce-compound` | `/ce-compound-refresh` added |
| `/feature-video` | `/ce-demo-reel` | Likely rename |
| `/test-browser` | `/ce-test-browser` | Same intent |
| `/resolve_todo_parallel` | `/ce-todo-resolve` | Plus `/ce-todo-create`, `/ce-todo-triage` |

New skills added in v2.68 that didn't exist in v2.34: `/ce-ideate` (divergent ideation before brainstorm), `/ce-setup` (this upgrade helper), `/ce-pr-description`, `/ce-commit`, `/ce-commit-push-pr`, language-specific writers (`/ce-dhh-rails-style`, `/ce-andrew-kane-gem-writer`, `/ce-dspy-ruby`), and `/ce-compound-refresh` (re-audits `docs/solutions/` against current code).

### What doesn't change

- `plans/` directory and `.claude/settings.json` `plansDirectory` continue to work.
- `docs/solutions/` output from `/ce-compound` follows the same three-bucket rule (this file is an example).
- No data migration needed.

---

## Part 2 — `/ce-setup` walkthrough

After the marketplace swap, run `/ce-setup`. It's a two-phase skill:

**Phase 1 — Diagnose (read-only).** Runs `scripts/check-health` and reports tool/config status. Always safe.

**Phase 2 — Fix (interactive, confirmation per step).**
1. Flags the obsolete `compound-engineering.local.md` at repo root (reviewer selection is now automatic).
2. Bootstraps `.compound-engineering/config.local.example.yaml` (committed template, always refreshed) and offers to create `.compound-engineering/config.local.yaml` (gitignored working copy).
3. Offers `.gitignore` entry `.compound-engineering/*.local.yaml`.
4. Offers to install missing CE support tools (agent-browser, vhs, silicon, etc.).

### The obsolete-file decision matters

`compound-engineering.local.md` accumulated hand-written reviewer-facing guidance over time. Don't just delete it — read it first. Content typically falls into three categories:

| Category | Example | Where it goes |
|---|---|---|
| Already-captured reviewer defaults | `review_agents: kieran-python-reviewer` | Drop. Auto-selected now. |
| Project overview / architecture duplicated from CLAUDE.md | "This is a Claude Code plugin that…" | Drop. CLAUDE.md is canonical. |
| **Unique reviewer heuristics** | "grep for `timestamp_seconds` in any diff touching hybrid_search" | **Migrate into CLAUDE.md as a `## Code Review Guardrails` section.** |
| Stale memory duplicates | "Ruff-formatted, type-hinted, pytest-driven" | Drop. Memory already captures preferences. |

For this repo, the migration ended up being five reviewer-facing rules:

1. **Bounded retries only.** Transcript path is bounded-retry → salvage → partial-write. Don't promote to unbounded loop.
2. **Probe before you pay.** `probe_atomic_writes()` must run before any Voyage embedding call in `build_search_index`. Reviewers grep for the probe.
3. **Timestamps are data, not decoration.** Reviewers grep for `timestamp_seconds` in diffs touching `hybrid_search`, `_dedup_by_video`, or chunk rendering.
4. **Skill-parity: same diff, not follow-up.** New CLI subcommand/flag → matching `SKILL.md` entry in the same PR.
5. **Out of scope for cleanup flags.** `docs/plans/*.md`, `docs/solutions/*.md`, `work/**/*`, root `plans/` are living or historical. Review agents must not flag them for deletion or rewriting.

These live in `CLAUDE.md` now, auto-loaded for every Claude Code session including review agents. The obsolete file is deleted (recoverable from git history).

### The config-file pair

```
.compound-engineering/
├── config.local.example.yaml   ← committed, always-refreshed template
└── config.local.yaml           ← gitignored, your machine-local copy
```

Right after creation both files are **identical**. Every line is commented out — CE uses safe defaults until you opt in. Same pattern as `.env.example` / `.env`: the example documents knobs, the local holds your actual toggles.

The `.gitignore` glob `.compound-engineering/*.local.yaml` is subtle but correct: it matches `config.local.yaml` but **not** `config.local.example.yaml`, because the latter ends in `.example.yaml`, not `.local.yaml`. Verify with `git check-ignore -v <path>` — it only reports matched files, so the absence of output on the `.example` file is confirmation.

### Tool installs

The health check may flag these as missing:

| Tool | Install | Why CE uses it |
|---|---|---|
| agent-browser | `npm install -g agent-browser && agent-browser install && npx skills add ...` | Browser automation for screenshots and testing; pulls its own Chrome (~180 MB). |
| vhs | `scoop install vhs` (Windows) / `brew install vhs` / `go install github.com/charmbracelet/vhs` | Record GIFs of CLI interactions for PR descriptions. |
| silicon | `scoop install silicon` / `brew install silicon` / `cargo install silicon` | Generate code-screenshot images. |
| gh, jq, ffmpeg | Usually already present | GitHub CLI, JSON processing, video processing. |

After install, rerun `/ce-setup` — it's idempotent and the final green `6/6 tools` state is a stronger guarantee than assuming the commands worked.

---

## Part 3 — The Codex linkage

This is where the setup flow stops being obvious. The `.compound-engineering/config.local.example.yaml` template has six `work_delegate_*` lines mentioning Codex, all commented out:

```yaml
# work_delegate: codex
# work_delegate_consent: true
# work_delegate_sandbox: yolo
# work_delegate_decision: auto
# work_delegate_model: gpt-5.4
# work_delegate_effort: high
```

And a separate plugin (`openai-codex/codex`) exposes `/codex:setup`, `/codex:review`, `/codex:rescue`, `/codex:adversarial-review`, `/codex:status`, `/codex:cancel`, `/codex:result`.

**These are three different mechanisms serving three different intents.** Picking the wrong one wastes time, rate limits, or both.

### Three Codex mechanisms, one decision table

| Mechanism | Intent | What Codex does | Where configured | Who invokes |
|---|---|---|---|---|
| **`work_delegate: codex`** (in CE config) | Implementation handoff | Writes the code, runs tests, reports JSON result | `.compound-engineering/config.local.yaml` | `/ce-work` automatically during execution |
| **`/codex:rescue`** / **`/codex:review`** / **`/codex:adversarial-review`** | On-demand second opinion | Reads repo state, critiques, returns findings — does NOT edit | Slash-command invocation | You, explicitly |
| **Review gate** (stop hook) | Automatic second opinion after every edit-producing Claude turn | Reads Claude's last turn + repo state, returns `ALLOW` or `BLOCK: <reason>` | `/codex:setup --enable-review-gate` | Claude Code's `Stop` hook, per turn |

### Mechanism 1: `work_delegate: codex` — implementation handoff

When enabled, `/ce-work` hands each implementation unit to `codex exec` with an XML-structured prompt (`<task>`, `<files>`, `<patterns>`, `<approach>`, `<constraints>`, `<testing>`, `<verify>`, `<output_contract>`) and a JSON output schema. Codex writes the code, runs tests internally, and reports back. Claude stays in charge of planning, git, and PR creation.

The six config knobs:

| Knob | Values | Meaning |
|---|---|---|
| `work_delegate` | `codex` or `false` | Master on/off |
| `work_delegate_consent` | `true`/`false` | One-time agreement to invoke Codex in a sandbox. Written automatically on first consent. |
| `work_delegate_sandbox` | `yolo` or `full-auto` | `yolo` = full system + network access (needed for tests/deps). `full-auto` = workspace-write only, no network. `yolo` uses `--dangerously-bypass-approvals-and-sandbox`, which is why consent gating exists. |
| `work_delegate_decision` | `auto` or `ask` | Silent vs. prompt-per-plan |
| `work_delegate_model` | any valid Codex model, default `gpt-5.4` | Model choice |
| `work_delegate_effort` | `minimal`, `low`, `medium`, `high` (default), `xhigh` | Codex `model_reasoning_effort` |

**This is for scale economics**, not second-opinion. When you have cheap Codex credits for bulk work and want Claude's context saved for planning. On a basic ChatGPT plan it's likely to hit rate limits on any non-trivial plan (one plan can fan out 5+ `codex exec` calls at high effort).

### Mechanism 2: On-demand `/codex:*` commands

The openai-codex plugin exposes several review-only commands. None edit code; all return Codex's findings verbatim.

| Command | When to use |
|---|---|
| `/codex:rescue` | Stuck, want a second opinion on a diagnosis or fix approach, or want to hand off an investigation. |
| `/codex:review [--wait\|--background] [--base <ref>] [--scope auto\|working-tree\|branch]` | Code review against local git state. Use `--background` for big refactors so Claude keeps working while Codex reviews. |
| `/codex:adversarial-review` | Same as review but more aggressive framing. Use when you want Codex to actively try to break the implementation. |
| `/codex:result` | Retrieve the findings from a backgrounded `/codex:review`. |
| `/codex:status` / `/codex:cancel <id>` | Inspect or kill in-flight Codex tasks. |

`/codex:setup` is the health check + optional review-gate toggle.

**How to phrase the request.** The Codex command is just the delivery envelope — what actually shapes the review is the prose you put after it. Three realistic examples that match this repo's patterns:

1. **Plan review (pre-implementation second opinion).**
   ```
   /codex:rescue review the plan at docs/plans/2026-04-20-feat-kb-stage2-lightrag-plan.md — challenge the approach, flag risks, suggest alternatives. Don't write code.
   ```
   Use before you invoke `/ce-work`. Scope is bounded ("that one plan file"), intent is explicit ("challenge", "flag", "suggest"), and the `Don't write code` terminator prevents Codex from drifting into implementation on a review task.

2. **Post-refactor code review (after edits land, before PR).**
   ```
   /codex:review --background --base main
   ```
   Non-interactive; Codex reviews the whole branch diff async while Claude keeps working. Retrieve with `/codex:result` when ready. Good default for "I just finished a big refactor, what did I miss?"

3. **Stuck on a diagnosis (hand off an investigation).**
   ```
   /codex:rescue I've been debugging why hybrid_search returns 0 hits for Q03 in tests/evals/ even though the video exists in LanceDB. I've checked the embedding path and the BM25 index; both look populated. Read tests/evals/test_search_quality.py and scripts/video_intel.py::hybrid_search, then tell me what diagnostic I haven't tried yet.
   ```
   Use when the Claude session has burned context on a dead-end investigation. Codex starts fresh, reads the same code, and often spots what you stopped looking at. The key phrasing is "what I've tried" + "what I want" — Codex performs better when the prompt distinguishes observation from ask.

General shape for any of these: **scope** (which file / branch / symptom) + **intent verb** ("challenge", "review", "diagnose", not "fix") + **explicit non-goal** ("Don't write code", "don't open a PR", "report only") when you want read-only behavior. Vague prompts ("look at this") produce generic findings; specific prompts produce specific findings.

### Mechanism 3: The review gate (the one with subtle behavior)

The companion's blurb says "require a fresh review before stop" — which invites the mental model **"one review at PR or session end."** That mental model is **wrong.** The gate's actual behavior, verified by reading `hooks/hooks.json`, `scripts/stop-review-gate-hook.mjs`, and `prompts/stop-review-gate.md` in the openai-codex plugin:

1. **Trigger: Claude Code's `Stop` hook — fires at the end of every Claude turn**, not once per session. Claude Code exposes a separate `SessionEnd` hook; the review gate is **not** on that one.
2. **Auto-filter in the prompt.** The `stop-review-gate.md` template tells Codex to return `ALLOW` immediately without investigation if the previous turn was "only a status update, a summary, a setup/login check, a review result, or output from a command that did not itself make direct edits in that turn." Conversational/planning/status turns pass cheaply.
3. **Real review only on edit-producing turns.** When Claude's last turn modified code, Codex reads the repo state, verifies edits happened, checks "second-order failures, empty-state behavior, retries, stale state, rollback risk, and design tradeoffs," and grounds findings in the actual code (not just Claude's message).
4. **Block mechanics.** `BLOCK: <reason>` emits `{"decision": "block", "reason": "..."}` to Claude Code, prevents the turn from ending, and feeds the reason back into Claude's next turn. Claude **must respond** to the feedback before the gate releases. This is a hard convergence loop, not an advisory comment.
5. **Latency cap.** 15-minute timeout per review call via `spawnSync`. Real latency typically 30s–5min depending on edit size.

### Decision framework: gate vs. on-demand

| Axis | Gate ON | Gate OFF + manual `/codex:review --background` |
|---|---|---|
| Coverage | Every edit-producing turn, automatic | Only when you invoke |
| Review scope | One turn's edits (often small diff) | Working tree or branch diff (your choice) |
| Latency cost | Paid per edit-turn (30s–5min typical, 15min cap) | Paid only when invoked; `--background` is non-blocking |
| Fit for "sometimes validate" | Forces always (subject to edit-turn filter) | Matches "sometimes" exactly |
| Fit for "priceless on big refactors" | Catches them the moment edits land | Depends on remembering to invoke |
| Rate-limit pressure (basic ChatGPT plan) | High during active edit sessions | Low |
| Interruption cost | Can stall fast iterative loops | None |
| Failure mode if you forget | N/A | Refactor ships unreviewed |

**The honest recommendation for most users on a basic ChatGPT plan:** leave the gate **off**. Use `/codex:review --background --base main` after major refactors and `/codex:rescue` when stuck. Flip the gate on later if you notice yourself skipping reviews you should have done. The toggle is one command: `/codex:setup --enable-review-gate`.

**When the gate is worth enabling:** you've caught yourself merging too fast without review, OR you're shipping solo and want a hard forcing function, OR you have plentiful Codex credits and don't mind the per-turn latency.

### Typical on-demand workflow

1. Refactor with Claude normally.
2. After edits land (commit optional): `/codex:review --background --base main`.
3. Keep working while Codex chews on it (~minutes).
4. `/codex:result` to see findings.
5. Incorporate or dismiss — your call.

---

## Part 4 — Ground truths

Rules the future-you should read before deviating:

- **CE review-agent selection is automatic in v2.68+.** Don't recreate `compound-engineering.local.md`. If you have reviewer-facing guidance, put it in `CLAUDE.md` under a `## Code Review Guardrails` section.
- **`.compound-engineering/config.local.example.yaml` is committed. `.compound-engineering/config.local.yaml` is not.** Verify gitignore with `git check-ignore -v` on both paths; the example file should produce no output (not ignored), the local file should report the matching rule.
- **`work_delegate: codex` is implementation handoff, not review.** If you want a second opinion, you want `/codex:rescue` or `/codex:review`, not `work_delegate`.
- **The review gate fires per-turn, not per PR or per session.** `Stop` ≠ `SessionEnd`. Always read `hooks/hooks.json` to disambiguate when a plugin's blurb is hand-wavy.
- **The review gate's auto-ALLOW filter is what makes it economically viable.** Without the prompt's edit-turn filter, every conversational turn would burn a slow Codex round-trip. The filter is the difference between "unusable" and "useful."
- **BLOCK is a convergence loop, not a comment.** `BLOCK: <reason>` physically prevents the turn from ending until Claude addresses it. Unlike PR comments, you cannot ignore it without disabling the gate.
- **For basic ChatGPT plans, prefer on-demand review.** The gate can fire 5–20 times per active session. Rate limits will bite.
- **Small edit-turn reviews are often *smaller surface* than PR reviews.** Counter to intuition, the per-turn scope (often a few hundred lines) is a feature, not a bug — more focused findings, faster feedback.
- **Keep the wrong first-pass framing visible when you correct yourself.** Documents that overwrite mistakes lose the reasoning trail. The point of compounding is the delta, not just the final state.

---

## Validation checklist

After running through this migration end-to-end:

- [ ] `/plugin` shows `compound-engineering@compound-engineering-plugin` at v2.68.1 or newer.
- [ ] `/ce-setup` reports `6/6 tools` green and `Config: ✅`.
- [ ] `compound-engineering.local.md` is deleted from repo root.
- [ ] `CLAUDE.md` has a `## Code Review Guardrails` section containing the repo's unique reviewer heuristics.
- [ ] `.compound-engineering/config.local.example.yaml` exists and is tracked by git.
- [ ] `.compound-engineering/config.local.yaml` exists and is gitignored (`git check-ignore -v` reports the matching rule).
- [ ] `git status` is clean or only shows intended changes.
- [ ] `/codex:setup` reports all green (Node, npm, Codex CLI, Auth, Session runtime).
- [ ] You can articulate — in one sentence each — what `work_delegate: codex`, `/codex:rescue`, `/codex:review`, and the review gate each do.

---

## Reference

- Compound Engineering marketplace: https://github.com/EveryInc/compound-engineering-plugin
- CE skill list (current): https://github.com/EveryInc/compound-engineering-plugin/tree/main/plugins/compound-engineering/skills
- Compound Engineering living guide: https://every.to/guides/compound-engineering
- OpenAI Codex plugin: ships under `openai-codex/codex` in `~/.claude/plugins/cache/`
- Prior journey note: [`work/2026-04-20/05-compound-engineering-v2-upgrade.md`](../../../work/2026-04-20/05-compound-engineering-v2-upgrade.md) — the original upgrade-path discovery that preceded this session.
- Session notes this doc was distilled from: [`work/2026-04-20/15-codex-delegation-vs-review-explanation.md`](../../../work/2026-04-20/15-codex-delegation-vs-review-explanation.md) — conversational form, includes the wrong-first-pass framings that got corrected as understanding compounded.
- Related CE solution: [`docs/solutions/workflow-issues/compound-engineering-four-artifacts-20260417.md`](compound-engineering-four-artifacts-20260417.md) — the four-artifact flywheel (ideate / brainstorm / plan / work / polish).

---

## Append-only notes (per three-bucket rule)

- **2026-04-20 initial write.** Consolidates a single-session upgrade-plus-exploration arc into a durable reference. Covers marketplace migration, `/ce-setup` flow, obsolete-file content migration into `CLAUDE.md`, config-file conventions, and all three Codex integration surfaces (work-delegation, on-demand, review gate). The wrong-first-pass framing on the review gate is intentionally preserved because the correction arc is load-bearing for readers who see the same "before stop" blurb and would otherwise reach the same wrong conclusion.

---

## Credits and context

This document exists because Daniel Zivkovic has been an early adopter of Kieran Klaassen's Compound Engineering framework since roughly May–June 2025, through many framework iterations. The upgrade experience documented here is the N-th such iteration, and writing it down as a solution doc is the compounding step: the next time anyone walks this path, they walk it with today's clarity rather than rediscovering it.

Kieran was also a guest speaker at the **Serverless Toronto** meetup, presenting the framework live: **[YouTube — Compound Engineering talk](https://www.youtube.com/watch?v=8IOeygZRIY8)** (linked resources in the YouTube description). Watch for the original mental model direct from the framework author; the document above is how that model lands in a specific project's workflow after several versions of drift and recovery.
