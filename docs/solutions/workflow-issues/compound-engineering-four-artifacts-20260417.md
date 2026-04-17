---
title: "Compound Engineering: four artifact types, no duplication"
date: 2026-04-17
category: workflow-issues
tags: [compound-engineering, workflow, planning, issues, brainstorm, conventions]
components: [docs/brainstorms/, docs/plans/, .github/issues]
severity: medium
symptoms:
  - "GitHub issues carry byte-for-byte copies of plan files (drift risk)"
  - "Confusion about whether brainstorms and plans are redundant"
  - "User proposing to collapse artifact types to reduce perceived duplication"
root_cause: "The /workflows:plan skill's default 'gh issue create --body-file <plan>' dumps the full plan markdown into the issue body, creating two copies of the same content. Separately, the agent failed to state the role distinction between brainstorms (WHAT) and plans (HOW), letting the user assume they were duplicates."
---

# Compound Engineering: four artifact types, no duplication

## The question that prompted this

During the rev-4 plan loop on 2026-04-17, Daniel asked: "Do we have equivalent plans in `docs/brainstorms/` or are they purely on GitHub as issues? Am I duplicating things now, is it one or the other?"

The honest answer has two parts: brainstorms and plans are **not** duplicates (different phases), but issues and plans **had** become duplicates in this repo because the `/workflows:plan` skill pastes the full plan body into the issue. The first is a teaching moment; the second is a drift bug worth fixing.

## The four artifacts (CE flywheel)

Reference image at [images/Compound-Engineering.jpg](../../../images/Compound-Engineering.jpg). The latest guide lives at [every.to/guides/compound-engineering](https://every.to/guides/compound-engineering) and evolves often (treat it as living source).

| Phase | Artifact | Role | Lives in | Updated |
|-------|----------|------|----------|---------|
| Ideate | (new) `/ce:ideate` output | PM-level "find work to do"; triage what deserves a brainstorm | GitHub issues (proposed) or a queue | Per incoming idea/signal |
| Brainstorm | `<date>-<topic>-brainstorm.md` | Collaborative WHAT/WHY dialogue; chooses between option A/B/C | `docs/brainstorms/` | Once per feature idea, frozen after |
| Plan | `<date>-<type>-<name>-plan.md` | HOW blueprint: function signatures, line refs, acceptance criteria | `docs/plans/` | Revised in place (rev 2, 3, 4) as understanding improves |
| Work (LFG) | PR with `Closes #N` | Plan to Code to Review to Test to PR, automated | GitHub PR on a feat/ branch | Per implementation pass |
| Polish | (new) `/ce:polish` output | Human UX refinement loop on a shipped PR | Follow-up commits or a new PR | Per round of feedback |
| (tracking) | GitHub issue | Backlog pointer + discussion surface; **NOT a copy of the plan** | GitHub issues | Summary + link, rarely changes |

Plugin prefix note: the guide now uses `/ce:ideate`, `/ce:brainstorm`, `/ce:polish` as the current command names. The older `/workflows:brainstorm`, `/workflows:plan`, `/workflows:work` verbs still work on installed plugin versions prior to the prefix change. Command names will migrate; the artifact roles above are stable.

## What each artifact answers

- **Brainstorm**: "What problem are we actually solving? What options did we consider? Which did we pick and why?"
- **Plan**: "Exactly how do we build the chosen option? Which files change? What are the acceptance criteria?"
- **Issue**: "This is on the backlog. Here is where the plan lives. Here is the discussion."
- **PR**: "Here is the code that implements the plan. `Closes #N`."

A brainstorm without a plan is a sketch. A plan without a brainstorm is fine if the WHAT was obvious (skip brainstorm for clear ideas). An issue that is a full copy of the plan is drift waiting to happen.

## The anti-pattern we created

For issues #1, #3, and #5 in this repo, `gh issue create --body-file docs/plans/<plan>.md` pasted the entire plan markdown (500+ lines) into the issue body. That creates two copies:

1. `docs/plans/<plan>.md` (committed, version-controlled, revisable via follow-up commits)
2. `https://github.com/.../issues/N` body (static snapshot, no connection to plan revisions)

The moment a rev 5 lands, the issue body is stale. Readers on GitHub see old specs. Readers in the repo see current specs. Nothing tells them which is authoritative.

## The fix

Issue body is a **pointer**, not a copy. Template:

```markdown
## Summary
Short paragraph (2 to 4 sentences) describing the feature and why.

## Plan
Full specification: `docs/plans/<plan>.md` (commits SHA1, SHA2, ...).
GitHub link: https://github.com/<org>/<repo>/blob/main/docs/plans/<plan>.md

## Acceptance
See the plan's "Acceptance Rules" and "Acceptance Criteria" sections.

## Related artifacts
- Brainstorm: `docs/brainstorms/<brainstorm>.md` (if present)
- Prior plans: <list if this supersedes earlier work>
```

Benefits: (a) plan stays canonical; (b) issue gets updated only when summary changes, which is rare; (c) revisions are visible via the commit list, not as stale markdown; (d) PR body does the same: link the plan, do not paste it.

## Behavioral rule for the agent (me)

When Daniel proposes collapsing these artifact types ("is it one or the other?", "delete docs/brainstorms/", "just use issues"), **push back first** with this learning before acting. The CE flywheel depends on phase separation:

- Brainstorm to Plan: keeps the WHY from bleeding into the HOW (plans should not re-argue design decisions).
- Plan to Issue: keeps version control authoritative over a GitHub snapshot.
- Plan to PR: keeps implementation linked to spec via `Closes #N`.

Collapsing any of these loses a property the flywheel uses to compound. State the property that would be lost, then ask whether Daniel still wants to collapse (sometimes the answer is yes; taste varies per project).

## Ground truths for future conversations

- Do **not** delete `docs/brainstorms/` or `docs/plans/`. They answer different questions.
- Do **not** paste full plan bodies into GitHub issues going forward. Use the pointer template above.
- Treat the Every.to CE guide as living source of truth: [every.to/guides/compound-engineering](https://every.to/guides/compound-engineering).
- New command namespace is `/ce:*` (ideate, brainstorm, polish). Old `/workflows:*` still works on the installed plugin version but will migrate.
- Image reference: [images/Compound-Engineering.jpg](../../../images/Compound-Engineering.jpg) shows the current flywheel shape with human-in-the-loop (red) vs automated (green) phases.

## Why this matters for this repo specifically

This project is itself an AI-skill-authoring workflow. The artifacts we produce (skills, plans, brainstorms, solutions) compose into a teaching corpus that future `video-intel` conversations draw on. Collapsing artifact types here would not just lose CE discipline; it would shrink the surface that future Claude conversations learn from.

## Append-only notes (per three-bucket rule)

- 2026-04-17: Initial write. Triggered by Daniel noticing issue/plan duplication in issue #5, and by the Compound-Engineering.jpg diagram showing the new `/ce:ideate` and `/ce:polish` phases that are upstream/downstream of the classic Plan-Code-Review loop.
