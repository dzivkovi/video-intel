---
title: "End-to-end SDLC chain in one session via context-packet handoffs"
date: 2026-04-23
last_updated: 2026-04-23
category: workflow-issues
module: compound-engineering-chain
problem_type: workflow_issue
component: development_workflow
severity: medium
applies_when:
  - "User asks to 'automate the whole process' or names Ralph-loop / LFG-style SDLC chaining"
  - "A single feature is small enough to ship in one session (PR-sized, not project-sized)"
  - "User explicitly grants minimal-interruption / auto-mode posture"
  - "Prior context (user memory, project standards, existing skill outputs) can seed each downstream skill"
tags: [compound-engineering, ralph-loop, lfg, sdlc-chain, context-packet, auto-mode, workflow]
---

> **2026-04-23 correction (morning after).** The original draft of this doc
> framed "post-merge validation IS the chain's true last step." That's wrong
> for a distributed plugin (this repo). Session 2 shipped a bug to main that
> a 3-minute smoke test would have caught pre-merge. The correction is in
> §4 below ("Pre-merge smoke test is a required gate, not post-merge") and
> §7 ("When to Apply"). Session 3's chain should load this correction
> first, not the original framing. See also
> [work/2026-04-23/03-dark-factory-reflection-two-sessions.md](../../../work/2026-04-23/03-dark-factory-reflection-two-sessions.md)
> for the full post-mortem.

# End-to-end SDLC chain in one session via context-packet handoffs

## Context

On 2026-04-22 evening, Daniel asked for one small feature (eliminate the double-upload of local MP4s to Gemini) and said the magic sentence:

> I ask because I trust your judgment I know that there is Ralh loop pattern (i think LFG in Compound Engineering (ce) framework) so that you could go through the whole process from brainstorming, planning, coding, validating until PR ir ready then automatically peer rewiered by another ce- command and improved and retested... until fixed.

The request was: chain `ce-brainstorm → ce-plan → ce-work → ce-commit-push-pr → ce-code-review → ce-resolve-pr-feedback → (implicit) merge` in one session with minimal interruptions, honoring user memory (engineering preferences, PR workflow, commit discipline) throughout.

The resulting PR [#32](https://github.com/dzivkovi/video-intel/pull/32) shipped a working feature with 525 tests green, merged as commit `8edea1b`, followed by a small direct-commit follow-up (`130a02e`) that fixed two observability bugs caught only at runtime. The chain worked end-to-end in one continuous session.

## Guidance

### 1. Treat the chain as one continuous reasoning session, not six isolated skill invocations

Each skill is invoked with a **dense context packet** summarizing:

- What the prior skill produced (path to the artifact, key decisions made)
- What's already been ruled out (non-goals)
- What's deferred to the current skill to resolve
- User preferences carried from memory (PR workflow, commit discipline, grounded-claims, no-auto-commit, etc.)
- Any ambiguities the current skill should not re-litigate because they were already settled upstream

Example packet structure passed to `/ce-plan`:

```markdown
Context carried from the brainstorm and doc-review rounds:
- Branch: feat/pipeline-one-upload (not yet created; plan should include branch-creation step)
- PR-worthy per the user's PR workflow memory (code + ≥50 lines + ≥3 files + cost-affecting)

Key design decisions already made in the requirements doc:
- Shape is a new `process` subcommand, not a flag on existing commands
- Reuse target for concepts is `process_concepts()` at scripts/video_intel.py:1593 (per-video helper)
...

Open questions to resolve DURING plan (not before):
1. Name the concrete Gemini SDK exception type / HTTP code / message pattern...
2. Decide the internal helper shape...

Non-goals (out of scope — plan should not expand into these):
- Explicit context caching (client.caches.create)
- --url variant of process
...

Constraints from specs/agent-rules.md that the plan must honor:
- TDD cycle: RED → GREEN → REFACTOR
- Test naming: test_<what>_<when>_<expected>
...
```

Without this packet discipline, each skill re-derives context from conversation history, wastes tokens, and frequently makes decisions that contradict what an earlier skill already decided.

### 2. Auto-mode means "exercise judgment on routine decisions" — not "never pause"

The user granting auto-mode / minimal-interruptions is the agent's license to:

- Resolve ambiguities from user memory or prior chain decisions without asking
- Apply `safe_auto` review findings silently
- Choose between equivalent implementation options using the project's established conventions

Auto-mode is **not** license to take destructive or shared-state-affecting actions without an explicit ask. In this session, the user confirmed this directly:

> I thought you gonna merge it?

Merging, force-pushing, external API calls, and anything else visible to others still needs a pause. The pattern that worked: end the authorized chain at the last non-destructive step (`ce-resolve-pr-feedback`), then ask for merge. Per `specs/agent-rules.md` §7:

> **Never in the proceed list:** committing, pushing, merging, interacting with external services...

### 3. Layered review catches different issues — don't skip any layer

Each review layer surfaced a distinct class of bug:

| Layer | Caught |
|-------|--------|
| Brainstorm review (5 personas) | Product/requirements issues: scope creep in observability bundle, unjustified orchestrator abstraction, ambiguous exit-code contract |
| Plan review (3 personas, headless) | My own internal contradictions: exit-code contradiction between Unit 2's Approach and its test scenarios, raise-vs-return mismatch on file-expiry detection |
| Code review (4 personas) | Three P1 implementation bugs: initial upload had no try/except (cross-reviewer agreement), `startswith("error:")` missed "error parsing JSON:" prefix, sidecar forced upload but didn't thread force=True |
| Post-merge reality check | Two runtime bugs tests couldn't catch: logger setup didn't propagate level to `gemini_common` (silently filtered all `usage` log lines), log format missed gemini-3's `thoughts_token_count` field |

**The code-review layer is non-negotiable even when spec-level reviews look clean.** Requirements/plan reviews check *what we're building*; code review checks *what we actually wrote*. These are different failure domains. Shipping without code review was the path I almost took — the 3 P1s would have gone to production.

### 4. Pre-merge smoke test is a required gate, not post-merge (corrected 2026-04-23)

**The original framing of this section was wrong.** Running `process --file` on a real local video caught two bugs that 525 unit tests didn't:

- Logger propagation (`logging.getLogger("gemini_common").setLevel(...)` missing from `main()`)
- Missing field in log line (`thoughts_token_count` not surfaced for gemini-3.x responses)

Both are exactly the class of bug that only shows up when the CLI meets reality: `basicConfig` behavior in a concrete Python process, Gemini SDK's actual response shape under the configured model. Unit tests proved the helpers worked in isolation; neither tested end-to-end stderr filtering or the real SDK response shape.

**The original framing** said: "after `ce-commit-push-pr` merges, run the CLI on one real input." That's wrong for a distributed plugin. This repo ships via git to other users. Any user who pulled main between the bad merge and the follow-up fix got a feature whose stated purpose was silent.

**The corrected rule — two gates, not one:**

| Gate | Purpose | Triggered by |
|---|---|---|
| **Gate 1 — Smoke test** | Prove the feature produces its primary observable signal on real input | End of implementation, **before any merge/push decision** |
| **Gate 2 — Destructive preview** | Prove the destructive operation's effect matches expectations | Before any shared-state mutation (file delete, force-push, external-service mutation) |

Gate 1 is the universal floor. Every feature — destructive or not — needs it. Gate 2 is additive when destructive state is involved. Session 1's feature had both; Session 2's feature had neither, and that's what led to the silent-broken-on-main outcome.

**What "primary observable signal" means depends on the feature:**

- A new CLI subcommand → run it on one real input and verify the expected output appears
- A new API endpoint → curl it and verify the response
- A new UI component → screenshot it and verify the layout
- A new log line → grep for it after running the feature

For Session 2 specifically, Gate 1 would have been: `python scripts/video_intel.py process --file <real.mp4>` plus `grep "^usage"` on the output. 3-minute check, caught both bugs before merge.

**"Tests green" is never a sufficient merge trigger on its own.** Tests prove helpers work in isolation; runtime integration (logger setup, SDK response shape, configuration wiring) can fail while every unit test passes.

### 5. "Ship the thermometer, not the fix" — validated as a pattern

The PR's core premise was "one upload → transcript call hits implicit cache." Rather than betting on that, we shipped observability that would **confirm or refute** the premise post-merge. When the logger bug was fixed and we finally saw the `usage ...` log lines:

```
usage mindmap    prompt=52438 cached=0     candidates=661  total=54602
usage transcript prompt=52528 cached=0     candidates=4988 total=62645
usage concepts   prompt=58564 cached=57256 candidates=477  total=62480
```

The stated premise **did not pay off** (`cached=0` on transcript) but an unexpected win appeared on the concepts call (97.8% cache hit from the shared taxonomy prefix). Without the observability we would have had false confidence that implicit caching was doing work it wasn't. The thermometer saved us. The motivated follow-up PR is now "explicit `client.caches.create()` around the video for the transcript span" — a specific, data-justified scope.

## Why This Matters

Without this pattern, a "chain the whole SDLC" request typically fails in one of three ways:

1. **Context loss between skills.** Each skill starts from scratch, wastes tokens re-deriving context, and often contradicts an earlier decision. The session produces a plan that doesn't match the brainstorm, or code that contradicts the plan.
2. **Review theater.** The agent runs the review skills but doesn't act on findings that would change direction. The chain ships with known bugs because fixing them mid-chain is treated as "breaking the flow." The code-review and resolve-feedback skills exist specifically to break and re-enter the flow cleanly.
3. **Premature victory declaration.** The agent stops at "PR merged" and treats that as success. Two runtime bugs in this session showed why that's wrong: unit tests can miss class-level issues (logger configuration, SDK response shape) that only runtime reveals.

The working pattern — context packets, judgment-calibrated auto-mode, layered review, pre-merge smoke test, thermometer-first for uncertain premises — compounds. The next "can you automate the SDLC?" request can reference this doc and skip the learning curve.

## When to Apply

- User explicitly asks to chain brainstorm → plan → work → review → merge, or names Ralph-loop / LFG / `/ce-loop` patterns
- Feature scope is PR-sized (one session, ≤200 lines of non-test code, single-concern)
- User has auto-mode posture active AND a durable user-memory store the agent can honor
- **A clear "reality check" action exists that can run PRE-merge** — a CLI invocation with real arguments, a browser test, a log-line grep, or observable output the agent can inspect before the merge decision. If no such check is possible, Gate 1 cannot fire and the chain should not run to merge.
- **The chain terminates at "merge-ready PR," not at "merged to main"**, when the repo distributes code to other users (any plugin or library published via git). For purely private code, merge-on-approval inside the chain is acceptable; for distributed code, the merge button is always the user's manual action after seeing both the diff AND the smoke-test output.

**Do NOT apply** when:

- The feature is large enough to need multi-session scope — split into multiple chains
- User preferences are undocumented — ask first, don't guess
- The change affects shared state (production data, public endpoints) — destructive-action rules override auto-mode
- There is no observable signal the agent can verify before merge — the chain cannot manufacture a Gate 1 check, and "tests green" alone is never sufficient
- The scope is exploratory ("I have an idea about X, not sure what to build") — that's a brainstorm conversation, not a chain

## Examples

### Example 1: context-packet handoff from brainstorm to plan

After `/ce-brainstorm` produced the requirements doc, the handoff to `/ce-plan`:

```
Skill("ce-plan", """Plan implementation of the `process` subcommand per the reviewed requirements doc.

Requirements source: docs/brainstorms/2026-04-22-process-subcommand-one-upload-requirements.md

Key design decisions already made:
- Shape is a new `process` subcommand, not a flag on existing commands
- Reuse target for concepts is `process_concepts()` at scripts/video_intel.py:1593

Open questions to resolve DURING plan:
1. Name the concrete Gemini SDK exception type...
2. Decide the internal helper shape...

Non-goals: Explicit context caching, --url variant, parallelism...

Constraints from specs/agent-rules.md that must be honored:
- TDD cycle: RED → GREEN → REFACTOR
- Ruff format + ruff check + pytest -m 'not integration' must pass...
""")
```

The plan skill didn't need to re-derive any of this. It resolved the Open Questions (both turned out to be cleanly resolvable with small code reads), wrote the plan, and handed off downstream.

### Example 2: knowing when to pause

Mid-chain during `/ce-resolve-pr-feedback`, the skill wanted to spawn subagents via GraphQL to fetch review threads from GitHub. But PR #32 had no GitHub review threads (the findings came from the local `/ce-code-review` synthesis), so the GraphQL path was a dead end. The agent recognized this and took the fix-validate-commit-push path directly instead of following the skill's literal instructions. This kind of "know when the skill's default workflow doesn't apply" judgment is what auto-mode licenses.

### Example 3: the reality-check that closed the loop

After merge — though per the corrected rule in §4 this should have been a pre-merge check — one shell command settled the implicit-cache question:

```bash
python scripts/video_intel.py --log-level info process \
  --file "G:/My Drive/video-intel/earlyaidopters/Claude Design System Prompt Leak + Tips.mp4" --force
```

Grep the output for `^usage`. The `cached=` values on each line tell the story: mindmap and transcript calls missed the cache, concepts call hit 97.8%. No follow-up investigation needed — one invocation, one log read, done.

## Related

- [docs/solutions/workflow-issues/compound-engineering-four-artifacts-20260417.md](compound-engineering-four-artifacts-20260417.md) — the four-artifact flywheel (brainstorm, plan, issue, PR) that this chain instantiates
- [docs/solutions/workflow-issues/compound-engineering-v2-upgrade-and-codex-integration-20260420.md](compound-engineering-v2-upgrade-and-codex-integration-20260420.md) — the plugin upgrade that made these skills available
- [PR #32](https://github.com/dzivkovi/video-intel/pull/32) — the concrete execution this learning documents
- `specs/agent-rules.md` §4 (Git Hygiene), §7 (Priority & Stopping Conditions) — the destructive-action rules that shape the "pause before merge" boundary
