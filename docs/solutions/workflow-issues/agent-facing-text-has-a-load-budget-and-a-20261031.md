---
title: "Agent-facing text has a load budget and a routing door: rule files, skill descriptions and trigger phrases"
date: 2026-10-31
category: workflow-issues
module: agent-guidance-surface
problem_type: workflow_issue
component: development_workflow
severity: medium
applies_when:
  - "Adding, moving or splitting a guardrail between CLAUDE.md and .claude/rules/*.md"
  - "Choosing the paths a new rule file declares"
  - "Adding a CLI subcommand that a skill must route to"
  - "Editing the description frontmatter of either video-intel skill"
symptoms:
  - "A PR claimed code sessions would load roughly the same amount of guidance after the rule-file split"
  - "A new command's first cut had a body-only routing row, so the skill would never have offered it"
  - "Trigger phrases sat in both skill descriptions while the mutex suite stayed green"
tags: [claude-rules, guardrails, context-budget, skill-routing, skill-description, trigger-phrases, docs-currency]
---

# Agent-facing text has a load budget and a routing door

## Context

The agent reads this repo through two surfaces that behave unlike ordinary docs. Rule files under `.claude/rules/` load when a session reads a path their `paths:` frontmatter matches (the split out of CLAUDE.md is issue #242). Skills load only when their frontmatter `description` matches what the user said. Both surfaces have a budget and a door, and three changes went wrong by treating them as plain text.

## Guidance

**Rule files scoped to `scripts/video_intel.py` all co-load.** Six of the eight rule files list `scripts/video_intel.py` in `paths:` (briefings, docs-currency, evals, scan-config, search-index, transcript), because that one file of over 16,000 lines defines nearly every function they govern. A session that reads it loads the core file plus about 240,000 characters of guardrails. The split measured about 3.3% more than the old monolith for such sessions and 74% less for every other session; with the guardrails added since, it is about 16% more and about 72% less today. So the lever for code sessions is a module split, not more rule files, and a new rule is scoped to the narrowest real file it governs.

**Invariants with a test contract go into a loaded guardrail in the same PR.** A `docs/solutions/` entry is pull-only: nothing loads it unless someone searches. The story of how an invariant was found belongs there; the invariant itself belongs in the rule file that fires for its code.

**Moving guardrail text needs three checks beyond "every line survived":**

- Grep for positional references. "See the entry below" is wrong once the entry lives in another file; `tests/test_guardrail_inventory.py` keeps a `POSITIONAL_REFERENCES` list for this.
- Compare content inside surviving bullets, not only that each bullet resolves. A 44-character resolution check let a cut instruction through inside a bullet that still matched.
- Prove each moved rule still fires for its own file. `REQUIRED_TRIGGERS` in the same test maps each rule file to the code it governs, because a glob that matches some file is not the same as a glob that matches the subject.

**Know what the docs-currency check does not sweep.** `LIVING_DOCS` in `tests/test_docs_currency.py` covers README, INSTALLATION, selected `docs/` files and the three SKILL.md files. It excludes CLAUDE.md and the rule files, so a stale default there (a chunk-minutes default of 50 after #157 lowered it to 30) is caught only by hand. Adding them is not free: the stale-model check deliberately has no exclusion list (`.claude/rules/docs-currency.md`, #204), and guardrail prose routinely names superseded models as history.

**A new command needs trigger phrases in the skill's frontmatter `description`.** The description decides whether the skill loads at all; the body is read only after that. The skill-parity rule in CLAUDE.md is satisfied by a body-only routing row, and that is exactly how the first cut of the standalone `headlines` command (#247) came to have a body-only routing row; review caught it before merge. The curate skill's description now carries "any headlines" and similar phrases.

**Descriptions past the listing budget are truncated.** The curate skill's description (about 3,400 characters) is cut mid-phrase in the skill listing today, and the search skill's is cut too. Front-load the distinctive triggers; anything after the cut does not route.

**The routing mutex test checks only the phrases on its lists.** `tests/test_skill_descriptions.py` asserts that each phrase in `SEARCH_TRIGGERS` and `CURATE_TRIGGERS` appears in one description and not the other. A phrase on neither list can sit in both descriptions with the suite green. A clean-context routing pass (ask a fresh session which skill each phrase should load) caught such collisions in one run.

## Why This Matters

Text the agent never loads protects nothing, and nothing reports that it stopped loading. Text that always loads costs context on every session that touches the big file. Both failures are silent and both look like success in review.

## When to Apply

- Before claiming a guidance move is load-neutral: measure characters loaded for a session that reads `scripts/video_intel.py`.
- When a PR adds a subcommand: check the description, not only the body.
- When a PR edits either description: add any new phrase to the matching trigger list.

## Related

- `CLAUDE.md` (rule-file table, skill-parity bullet)
- `.claude/rules/docs-currency.md`
- `tests/test_guardrail_inventory.py`, `tests/test_docs_currency.py`, `tests/test_skill_descriptions.py`
- Issues #242, #204, #157, #247
