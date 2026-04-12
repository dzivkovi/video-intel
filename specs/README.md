# Specs

Agent rules, product vision, and task specs for Video Intel.

## Files

- **`agent-rules.md`**: AI developer rules of engagement covering cognitive load, TDD, git hygiene, architectural authority, communication style, and priority / stopping conditions. Portable across projects.

## Portability

`agent-rules.md` is the one thing you bring to a new project. It is written to be project-agnostic; repo-specific carve-outs live in `CLAUDE.md` and override it.

## CLAUDE.md backlink ritual (important, easy to forget)

When adopting this file in a new project, the project's `CLAUDE.md` must point at it in **two** places, not one:

1. **At the top**, under the first heading, so the agent loads the rules before interpreting anything else.
2. **At the bottom**, as the final instruction, so the agent is reminded of the rules after long contexts where the opening lines may have fallen out of the attention window.

Template to paste into `CLAUDE.md`:

```markdown
## System Instructions

Before executing any task, you MUST read and strictly adhere to the constraints defined in `specs/agent-rules.md`. This file governs all code you write, every commit you make, and how you communicate with me. Read it before doing anything else.

... rest of CLAUDE.md ...

## Reminder

The rules in `specs/agent-rules.md` apply to every task in this repo without exception. When in doubt, re-read them. If a rule here in `CLAUDE.md` conflicts with `specs/agent-rules.md`, `CLAUDE.md` wins because it is project-specific (this override is stated at the top of `specs/agent-rules.md`).
```

**Why two backlinks and not one:** system prompts and long conversations both suffer from "lost in the middle" attention decay. A single pointer at the top can drop out of the agent's working memory after many turns. Bookending the pointer catches both the start-of-conversation read and the end-of-context refresh.

## Where to find things

- **Product vision:** `../ARCHITECTURE.md` (system overview and two-tier roadmap)
- **Current status and project-specific overrides:** `../CLAUDE.md`
- **Architectural decisions:** `../docs/adr/`
- **Solved problems:** `../docs/solutions/`
