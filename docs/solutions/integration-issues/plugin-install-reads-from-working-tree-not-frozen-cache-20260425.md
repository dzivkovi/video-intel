---
title: "Plugin install reads from working tree, not branch-frozen cache: pre-merge skill testing is valid"
date: 2026-04-25
category: integration-issues
module: plugin_packaging
problem_type: integration_issue
component: tooling
symptoms:
  - Pre-merge smoke test recommendations were framed around "wait for merge to test the new SKILL.md"
  - Confusion about whether `feat/<branch>` checkout exposes new triggers to a fresh `claude` session
  - Mock-api session in a different CWD also showed "new" skill behavior — created a false impression of plugin caching
root_cause: inadequate_documentation
resolution_type: documentation_update
severity: medium
related_components:
  - documentation
  - development_workflow
tags:
  - claude-code
  - plugin-development
  - working-tree
  - branch-isolation
  - worktree
  - skill-iteration
---

# Plugin install reads from working tree, not branch-frozen cache

## Problem

When developing skills locally for a Claude Code plugin (specifically the
`video-intel` plugin in this repo, but the pattern applies to any plugin
installed via the user-level marketplace path), the user-level install
uses `~/.claude/settings.json` with `extraKnownMarketplaces` pointing at
the repo's local checkout path.

The non-obvious consequence: **all `claude` sessions globally read the
SKILL.md (and other plugin-shipping files) from whatever branch is
currently checked out at that path.** This was not documented anywhere in
`CLAUDE.md` or `INSTALLATION.md`, and the misconception ("plugin must be
cached frozen at merge time") drove incorrect smoke-test guidance during
PR #43.

## Symptoms

- Recommendation in PR #43 chat said: "smoke test should run *post-merge*
  because the plugin reads from a cached version of main."
- User ran the same smoke test from `feat/skill-factcheck-triggers`
  pre-merge anyway; it demonstrably read the new SKILL.md content
  (including the new anti-grep callout language and the new trigger phrases).
  Pre-merge testing was valid.
- User then ran the same prompt from a different project
  (`~/Documents/mock-api`) "to test against the previously installed
  skill." Same install was loaded — the mock-api session ALSO read the
  feat-branch's SKILL.md. Not a true A/B test, just a same-install
  session from a different working directory. Created momentary confusion
  about whether the plugin had cached anything.
- After running `git checkout main` for any unrelated reason while
  another `claude` session was open against the new branch's behavior,
  the second session would silently lose access to the branch's skill
  changes. No notification, no error — just a different SKILL.md
  loading from disk.

## What Didn't Work

- Mental model "plugin is cached at install time, refreshes on merge."
  Wrong. There is no install-time cache for the user-level marketplace
  path; the plugin loader reads files from disk on each session start.
- Mental model "different CWD = different plugin context." Wrong. CWD
  affects scripts the plugin invokes (corpus discovery, etc.), but the
  SKILL.md and CLAUDE.md content are read from the marketplace path
  regardless of CWD.
- `git stash` to "isolate" plugin state. Stashing untracked files moves
  them out of the working tree, but the plugin still reads files from
  the active checkout. Stash does not isolate plugin state.

## Solution

**Whatever branch is checked out at the plugin marketplace path affects
every `claude` session on the machine globally**, regardless of CWD. The
practical implications fall out cleanly:

### 1. Pre-merge smoke testing is valid

When iterating on `SKILL.md`, `CLAUDE.md`, `specs/agent-rules.md`, or any
other plugin-shipping file, you can test from **any** CWD as long as the
branch with your changes is checked out at the marketplace path. No need
to merge first.

The R2b smoke test for PR #43 was run from `feat/skill-factcheck-triggers`
and produced canonical evidence the new triggers worked. That evidence
landed on the PR before merge, satisfying R1, R2, and R2b.

### 2. Branch-switching has global side effects

If you `git checkout main` to look at something while another `claude`
session is open and depending on a feature branch's plugin behavior,
the second session will momentarily see `main`'s SKILL.md. No errors —
just different routing.

For parallel claude sessions where any session is iterating on plugin
internals, **prefer worktrees**:

```bash
git worktree add ../video-intel-experiment feat/some-experiment
```

A worktree at a different filesystem path does NOT affect the
marketplace-registered path's plugin state. Each session running from
the original checkout sees the branch checked out there; sessions
running from the worktree see the worktree's branch. Clean isolation.

### 3. `git stash` is not plugin isolation

Even stashed working tree changes affect the active checkout's SKILL.md,
because the plugin reads files from disk, not from the git index. If you
need to test "what does claude do with the OLD SKILL.md", you must
either `git checkout main` (and accept the global side effect on parallel
sessions) or use a worktree.

## Why This Works

The plugin marketplace mechanism in Claude Code is designed for
filesystem-pointed marketplaces — the registered path is read fresh each
session, supporting rapid plugin development without a publish cycle.
This is a feature, not a bug. The implication for plugin developers
testing from a working tree is: pre-merge testing is the canonical
loop, not a workaround.

The "cached install" misconception came from reasoning by analogy with
package managers (npm, pip, Cargo) where install-time materializes a
frozen snapshot. Claude Code's user-level plugin install pattern is
closer to a `pip install -e .` editable install, but **without** the
explicit user gesture that signals "I want live updates." That's the
gap a plugin developer needs to internalize.

## Prevention

1. **Document the global-checkout behavior** in `CLAUDE.md`'s
   "User-level install" section so future installers (or a fresh
   future-self after months away) understand it without rediscovery.
2. **When troubleshooting "why isn't my new skill triggering?"**, first
   verify the marketplace path's checked-out branch matches the branch
   you THINK is being read. `git -C <marketplace-path> branch
   --show-current`.
3. **For parallel claude sessions on the same plugin repo**, default to
   worktrees if any session iterates on plugin internals. Otherwise,
   branch switches in one terminal can subtly break the others.
4. **For plugin distribution under user-level install**, this behavior
   is fine for solo use. For multi-user plugins distributed via the
   broader Anthropic marketplace, the install pattern is different
   (cache-based) and the global-checkout caveat does NOT apply.
   Distinguish the two install modes in any plugin's
   `INSTALLATION.md`.

### Concrete CLAUDE.md addition (suggested for the plugin itself)

```markdown
### User-level install behavior — important caveat

The user-level install at `~/.claude/settings.json` registers a marketplace
at the **repo path on disk**. The plugin is read fresh from that path on
every `claude` session start — there is no install-time cache for this
mode. Implications:

- **Pre-merge testing works.** Whatever branch is checked out in the
  marketplace path is what every `claude` session sees, globally.
- **Branch-switching has global side effects.** If you switch branches
  in one terminal, every other open `claude` session loses access to
  the previous branch's SKILL.md content on its next prompt.
- **Use worktrees for parallel work** when any session is iterating on
  plugin internals.
```

## Related Issues

- PR #43 (the work this learning came out of) — verifies that pre-merge
  smoke testing produced canonical evidence the new triggers worked
- [`work/2026-04-25/11-github-project-board-automation-for-ce.md`](../../work/2026-04-25/11-github-project-board-automation-for-ce.md) and
  [`work/2026-04-25/12-board-automation-vs-agent-rules-debate.md`](../../work/2026-04-25/12-board-automation-vs-agent-rules-debate.md) —
  context on what was happening when the misconception surfaced
- [`docs/solutions/workflow-issues/ai-hallucination-cross-check-via-source-of-truth-ui-20260425.md`](../workflow-issues/ai-hallucination-cross-check-via-source-of-truth-ui-20260425.md) —
  companion learning from the same PR (verify AI claims before acting)
- Memory `feedback_compound_engineering.md` (auto memory [claude]) — broader CE plugin usage context
- Memory `reference_github_projects_pat_setup.md` (auto memory [claude]) — companion infrastructure note from the same PR
