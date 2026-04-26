---
title: Verify AI claims about platform APIs in source-of-truth UI before acting
date: 2026-04-25
category: workflow-issues
module: development_workflow
problem_type: workflow_issue
component: development_workflow
severity: medium
applies_when:
  - An AI assistant claims a specific feature, button, trigger, or API exists in a third-party platform
  - Two AIs (or two sessions of the same AI) give conflicting answers about a technical detail
  - You are about to act on AI-generated YAML, code, or configuration that depends on a specific platform feature
  - The recommendation involves a version-specific or label-specific claim ("the X workflow named Y triggers on Z")
related_components:
  - documentation
  - tooling
tags:
  - ai-collaboration
  - hallucination-mitigation
  - verification-discipline
  - github-actions
  - github-projects-v2
  - workflow
---

# Verify AI claims about platform APIs in source-of-truth UI before acting

## Context

When asking an AI assistant about a platform's API or UI features, the AI may
hallucinate plausible-sounding but non-existent features. AIs are confident
pattern-matchers — when a user describes a desired behavior ("auto-route PRs
to Review"), the AI produces YAML or instructions that *look* correct because
they pattern-match against similar real APIs.

This recurred three times in a single session on 2026-04-25 during PR #43
([feat(skills): verification / fact-check triggers for video-intel-search](
https://github.com/dzivkovi/video-intel/pull/43)):

1. **Claude (in this session) hallucinated a `Pull request opened` trigger**
   as a built-in GitHub Projects v2 workflow. It does not exist. Recommendation
   to "enable two toggles" was acted-on-then-corrected only because the user
   went to the actual Workflows page and screenshotted what was literally
   there. Captured in [`work/2026-04-25/11-github-project-board-automation-for-ce.md`](../../work/2026-04-25/11-github-project-board-automation-for-ce.md)
   (incorrect) and [`work/2026-04-25/12-board-automation-vs-agent-rules-debate.md`](../../work/2026-04-25/12-board-automation-vs-agent-rules-debate.md)
   (corrected).
2. **Gemini independently hallucinated** that the existing "Pull request
   linked to issue → Review" workflow could move issues to Review even
   without an explicit `Closes #N` link. Partially right (the workflow
   exists) but wrong about its trigger semantics. Discovering Gemini was
   right about workflow existence but wrong about semantics required reading
   the `gh project field-list` output and the official GitHub docs in
   parallel.
3. **The autonomous chain (subagent context) claimed `plugin-dev:plugin-validator`
   and `plugin-dev:skill-reviewer` were unavailable.** True for that subagent's
   tool registry, but the main session DID have access. The "limitation" was
   real but narrower than first stated. Caught only when the main session
   ran the agents post-chain and posted canonical evidence to the PR.

The user's `feedback_grounded_claims.md` memory already names this risk
("Separate documented facts from empirical inferences; verify against docs
first") (auto memory [claude]) — but the failure mode kept recurring across
two distinct AI assistants in one session. That repeating pattern is the
signal worth compounding.

## Guidance

When an AI assistant (any of them — Claude, Gemini, Copilot, Codex) claims a
specific feature exists in a platform's API or UI:

### 1. Verify in the source-of-truth UI before acting

For UI-driven platforms, **screenshot the actual UI**. Don't rephrase the
AI's claim and assume; open the platform and look at what's literally there.

- GitHub Projects v2 workflows → open `https://github.com/users/<user>/projects/<n>/workflows` and look at the left sidebar list
- GitHub Actions triggers → check [docs.github.com/en/actions/using-workflows/events-that-trigger-workflows](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows)
- Anthropic plugin format → open [docs.anthropic.com](https://docs.anthropic.com) directly
- Any third-party API → go to the official docs page, not the AI's recall

For CLI-driven platforms, run a non-destructive command that surfaces the
real shape:

- `gh api graphql -f query='{ ... }'` to see what fields/types actually exist
- `gh secret list`, `gh project field-list`, `gh workflow list` etc. — read-only enumerations

### 2. Treat AI disagreement as a hallucination signal

If two AIs disagree on a technical claim, that's a strong signal that at
least one is hallucinating. **Don't try to resolve the disagreement by
argument.** Verify the actual behavior in the source-of-truth before
trusting either side.

The 2026-04-25 case: Claude said one workflow triggered things; Gemini said
a different workflow triggered things; the user screenshotted the
Workflows tab and proved BOTH partially wrong. The screenshot resolved
in <30 seconds what would have taken many message rounds of back-and-forth.

### 3. Highest-risk claim shapes

Some claim shapes are unusually high-risk for hallucination. Treat as
guilty-until-verified:

- **Specific version, button label, trigger name, or option enum** ("the
  `Pull request opened` workflow", "the `--ready-for-review` flag")
- **Specific file paths in third-party packages** (the AI may invent a
  plausible-sounding path)
- **Specific minor or patch version of a feature** ("available since v2.4.0"
  is often a guess)
- **Free-tier vs paid-tier feature claims** for SaaS platforms — these
  change often and the AI's training data is stale

For these shapes, verify literally — don't rephrase, don't assume.

### 4. For YAML / code recommendations

When an AI generates YAML or code that depends on a third-party API or
schema, **prefer copy-paste from the platform's official documentation
page** over AI-generated patterns, even when both look identical. The
official docs page is updated by the platform team; the AI's recall of
the same content is frozen at training cutoff and may have invented
field names.

Concrete example from this PR: the `route-pr-to-review.yml` workflow file
at [`.github/workflows/route-pr-to-review.yml`](../../.github/workflows/route-pr-to-review.yml)
was ultimately written using GitHub's official documentation pattern
(`gh api graphql` from
[docs.github.com](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)),
**not** a third-party action and **not** the hallucinated
`actions/add-to-project + status` chain that an earlier draft suggested.

## Why This Matters

Hallucination at the YAML / config / API-call layer wastes the most cycles
because:

1. **The recommendation looks correct.** Pattern-match perfection means it
   passes a code-review smell test.
2. **The failure mode is a runtime error.** You only find out the trigger
   doesn't exist after pushing the YAML and watching it fail in CI.
3. **Debugging requires unwinding the AI's reasoning to find the false claim**
   — much slower than verifying upfront.
4. **The debugging session may itself produce more hallucinations.** AIs
   compound errors when probing their own incorrect claims.

The cost asymmetry: 30 seconds to screenshot a UI vs 30 minutes to debug a
broken YAML driven by a hallucinated trigger name. Always pay the 30
seconds.

## When to Apply

- Before merging any PR that depends on AI-generated platform configuration
- Before committing any GitHub Actions workflow that uses unfamiliar triggers
  or actions
- When two AIs disagree on a technical detail
- When the AI cites a specific button label, version number, or option name
  you haven't seen before
- During pair-programming with AI on infrastructure / platform tasks where
  the platform's behavior is the source of truth

## Examples

### Hallucination caught (2026-04-25)

**Claim (Claude, in [`work/2026-04-25/11-github-project-board-automation-for-ce.md`](../../work/2026-04-25/11-github-project-board-automation-for-ce.md)):**
> "Go to https://github.com/users/dzivkovi/projects/8/workflows and turn on
> these built-in rules: 1. **'Pull request opened'** → Set Status: `Review`"

**Reality (user screenshot of the same URL):**
The actual workflow list contained: `Auto-add sub-issues`, `Auto-add to
project`, `Auto-close issue`, `Item added to project`, **`Pull request
linked to issue`**, `Pull request merged`, plus four off-by-default
workflows. **No `Pull request opened` trigger existed.**

**Resolution:** Wrote `.github/workflows/route-pr-to-review.yml` using the
GitHub-official `gh api graphql` pattern instead, copy-adapted from
[docs.github.com](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions).

### Disagreement-as-signal pattern

**Claude said:** "There is no built-in rule 'when a linked PR is opened,
move the Issue to Review.'" (from [`work/2026-04-25/12-board-automation-vs-agent-rules-debate.md`](../../work/2026-04-25/12-board-automation-vs-agent-rules-debate.md))

**Gemini said:** "Your board is already perfectly wired to move cards to
the Review column, *if* the agent links the PR to the issue."

**Source of truth:** The screenshot showed `Pull request linked to issue
→ Status: Review` IS a real, on-by-default workflow. Gemini was right
about existence; Claude was wrong to dismiss it. **Both** were imprecise
about the workflow's actual trigger semantics (it fires when a PR-to-issue
link is created, but moves the PR item, not the linked issue's card — a
distinction neither AI captured cleanly until the user verified
empirically).

## Related

- [`work/2026-04-25/11-github-project-board-automation-for-ce.md`](../../work/2026-04-25/11-github-project-board-automation-for-ce.md) — original incorrect recommendation
- [`work/2026-04-25/12-board-automation-vs-agent-rules-debate.md`](../../work/2026-04-25/12-board-automation-vs-agent-rules-debate.md) — disagreement resolution
- PR #43 — the work this learning came out of
- Memory `feedback_grounded_claims.md` (auto memory [claude]) — companion guidance: separate documented facts from empirical inferences
- [`docs/solutions/integration-issues/plugin-install-reads-from-working-tree-not-frozen-cache-20260425.md`](../integration-issues/plugin-install-reads-from-working-tree-not-frozen-cache-20260425.md) — companion learning from the same PR (also driven by an AI claim that turned out wrong)
