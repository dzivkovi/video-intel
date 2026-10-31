---
title: "Multi-gate AI PR validation: how a systems-engineering mindset closes the gap between AI-generated and merge-ready"
date: 2026-04-27
last_updated: 2026-10-31
category: workflow-issues
module: pr-validation-discipline
problem_type: workflow_issue
component: development_workflow
severity: medium
applies_when:
  - "User accepts an overnight or autonomous-mode AI-generated PR for merge"
  - "Diff scope is architectural (cross-cutting refactor, new orchestrator path, retired guarantee)"
  - "Output is consumed by downstream systems (knowledge graph, search index, billing) where wrong-but-plausible is worse than visibly-broken"
  - "User has limited synchronous attention and wants to ship at confidence > 95%, not at confidence = tests-green"
  - "A multi-persona or multi-layer review has returned and a review-fix commit is about to be merged"
tags: [pr-validation, gate-framework, systems-engineering, adversarial-review, real-input-smoke, dark-factories, compound-engineering, probability-based-confidence, review-triage, fix-commit-reverify, disjoint-review-layers]
---

# Multi-gate AI PR validation: how a systems-engineering mindset closes the gap between AI-generated and merge-ready

## Context

This doc exists because I did not see, until I started running Dark Factories nightly, how much engineering effort the human-validation phase actually takes. A public post on "Dark Factories" captured the AI overnight chain (Compound Engineering plugin running brainstorm → plan → work → commit → review → resolve while you sleep) accurately for a small case but understated the discipline that architectural diffs into automated downstream systems actually require. The chain reliably produces merge-shape PRs: green tests, ruff clean, multi-agent code review applied, smoke evidence captured. This doc covers what comes after: how the human at the merge button decides whether the merge-shape PR is actually merge-ready.

The discipline that closes the gap is V&V (verification and validation), the systems-engineering practice I ran formally earlier in my career before the term left my daily vocabulary. That team's job was to validate developer-team releases before they shipped to global customers. A sign-off cycle took days to weeks per release, not minutes. Scope: integration across dependent systems, security and scalability, edge functional cases, multi-engineer probability-based sign-off (binary go/no-go was a non-starter at that scale; the team committed at confidence levels instead). Running Dark Factories daily across multiple GitHub issues brought the discipline back, at a faster cadence but with the same scope.

The receipt is [PR #55](https://github.com/dzivkovi/video-intel/pull/55) (issue #54: invert the mindmap pipeline to read from on-disk transcripts instead of re-watching the video). Five hours of human validation surfaced two real bugs that a fully passing test suite plus a multi-persona code review had not:

1. **The `transcript_status` writer-literal mismatch.** Production writes `"complete"` on success. The new partial-detection check tested against `"ok"` only. Every healthy single-shot transcript would have shipped with a misleading `mindmap_source_status: "partial"` flag and a `<!-- source: partial transcript (complete) -->` HTML comment. Caught when the user inspected the meta.json from the first real-input smoke. The unit-test fixture used `"ok"`, so the test suite was silent.
2. **The title-rotation orchestrator bug.** When a YouTube creator A/B-tests video titles, `cmd_mindmap` and `_cmd_process_url` computed transcript paths from the current API title and missed the existing transcript at the original prefix. Caught when a smoke against a channel whose title had rotated since the transcript was written failed at Gemini's 1M-token cap. A reviewer agent had flagged the exact pattern as a low-confidence finding (C2/C3); I dismissed it. The smoke against rotated-title content reproduced it as a hard failure.

Both bugs would have shipped to the whole corpus and corrupted downstream knowledge-graph layers if the human-validation phase had been compressed to "tests pass, ship."

Section 8 and Example 5 were added later, after a batch of review cycles showed that the gates are necessary but not sufficient: a review is not finished when the reviewers return.

## Guidance

### 1. Tests pass ≠ merge-ready. Real-input smoke is the floor.

For AI-generated diffs, "tests green" reliably proves only that the helpers compose as the test author imagined. It does not prove:

- The fixture data shape matches production data shape (the C1 bug)
- The orchestrator's input-resolution path matches production input variability (the title-rotation bug)
- The output's secondary properties (timestamp grounding, schema compatibility, cost) sit inside tolerances (Goodhart prompt-tuning)
- The change interacts cleanly with code paths the test author did not think to mock

A real-input smoke runs the production CLI with production-shape arguments against production-shape data, captures the primary observable output, and inspects it line by line. Not "does it return a non-error status." Does the output match what a human consumer or downstream system would expect.

This is non-negotiable. It complements rather than replaces the test suite. Tests verify pieces; smoke verifies emergent behavior.

### 2. The multi-gate framework: three gates, each catches a different class of failure

| Gate | Purpose | When it fires | What it catches |
|---|---|---|---|
| **Gate 0: Architectural pilot** | Validate the chosen approach before committing to implementation effort | Before plan or code, when the architecture is novel | "This approach won't work" failures: model-specific limits, API-shape mismatches, cost-per-invocation surprises |
| **Gate 1: Real-input smoke** | Confirm the implementation produces its primary observable signal on production-shape data | Post-implementation, post-tests-green, before any push or merge decision | Fixture-vs-production mismatches, integration bugs the unit suite mocked away, first-touch operator-experience issues |
| **Gate 2: Explicit human go for destructive ops or merge** | Prevent the agent from making irreversible decisions on behalf of the human | Before any destructive operation OR the merge button, regardless of how good the upstream gates looked | Agent overconfidence, cases where the agent missed something a human would catch on a second look at diff and smoke output together |

The gates are additive, not substitutable. The PR #55 session needed all three:

- **Gate 0:** A 30-second empirical pilot against an on-disk long-interview transcript proved mindmap-from-transcript was qualitatively comparable to mindmap-from-video before any architectural code was written. Cheap insurance against the architectural premise being wrong.
- **Gate 1:** The first two content-class smokes (a solo creator and a long podcast) caught the C1 bug. A third smoke, against a channel whose title had rotated and run without the title-override workaround, caught the title-rotation bug.
- **Gate 2:** The user held the merge button after seeing diff and smoke output together. The pause was used to ask for the third smoke and a deeper compare against the legacy baseline. That last pause caught a regex bug in my grounding analyzer that would have understated grounding rates on solo-creator content by a wide margin.

### 3. Adversarial multi-persona code review surfaces issues a single reviewer misses

Single-reviewer code review on AI-generated code has a known failure mode: the reviewer (human or agent) anchors on the same mental model the implementer used and inherits the same blind spots. The fix is conditional multi-persona review: different reviewers selected by what the diff actually touches.

The pattern that worked on PR #55 (`/ce-code-review` invocation):

| Persona | Always-on | Conditional triggers |
|---|---|---|
| Correctness | yes | : |
| Maintainability | yes | : |
| Testing | yes | : |
| Project standards | yes | : |
| Adversarial | : | Diff ≥ 50 changed lines, or touches auth / payments / data-mutations / external APIs |
| Kieran-Python / Kieran-TypeScript / DHH-Rails | : | Language matches |
| API-contract | : | Diff touches API routes, request/response types, serialization |
| Performance | : | Diff touches DB queries, hot loops, caching |
| Security | : | Diff touches auth, public endpoints, user input, permissions |
| CLI-readiness | : | Diff touches CLI command definitions or argument parsing |
| Reliability | : | Diff touches error handling, retries, circuit breakers, timeouts |

Two key disciplines:

- **Run reviewers in parallel.** Five concurrent reviewers cost the same wall-clock as one. The cost is token spend, not human time.
- **Gate findings on confidence, not just severity.** A reviewer's P1 with 0.65 confidence and a P2 with 0.95 confidence often deserve attention in opposite proportions to severity-only ranking. The title-rotation bug was a P2 with 0.7 confidence that I dismissed. Reproducing it on real input flipped it to a verified P1.

The complementary discipline: adversarial reviewers actively try to break the implementation, not just check it against known patterns. They construct failure scenarios. This is different from correctness review and surfaces a distinct class of bug.

### 4. Evidence preservation: capture before/after artifacts under `docs/plans/gate1-evidence/`

When a smoke runs, the captured output is the durable artifact. Three rules:

- **Capture both inputs and outputs.** The mindmap.md the smoke produced AND the meta.json sibling AND the log line showing token usage. A smoke without a captured output is unrepeatable.
- **Capture A/B pairs when an alternative was tested.** PR #55 committed a before-prompt-tune and after-prompt-tune mindmap for one content class and a tuned and verbose mindmap for another (`issue-54-<content-class>-mindmap-before-prompt-tune.md` and siblings). The pairs make the alternative-rejection auditable in 6 months.
- **Commit the evidence to the PR.** It belongs in the same review surface as the code. Reviewers see the smoke output and can second-guess the verdict.

The convention `docs/plans/gate1-evidence/{topic}-{shape}.md` works because it's grep-able, doesn't pollute test fixtures, and survives bulk-clean operations on `tests/` or `tmp/`.

### 5. Confidence-based language over binary go/no-go

Systems engineering, codified in ISO/IEC/IEEE 15288 and elaborated by published practitioners, signs off in probability bands, not binary verdicts. The trained habit:

| Bad | Better |
|---|---|
| "Looks good, ship it" | "I'm at 90% confidence on the architecture; the remaining 10% is whether [specific thing] holds at scale" |
| "Tests pass, merging" | "Tests pass; the smoke against [content type X] also passes; I haven't run [content type Y] which is ~5% of the corpus" |
| "Reviewer flagged this; ignoring as low-confidence" | "Reviewer flagged this with 0.65 confidence; I disagree at 0.55 confidence; the resolution is to construct a smoke that reproduces or refutes" |

The discipline forces the agent to name what it does not know. On PR #55, the user's habit of asking for confidence intervals around grounding rates and concept-count multipliers (not just yes/no) is what kept the analysis from rubber-stamping. The third-content-class smoke was triggered by the user noting "you've shown two data points; I want a third before I commit."

### 6. Backup before destructive operations on real data, even when authorized

The user can authorize a destructive operation once. The operation can still be wrong. Backup-before-destruct is the cheap insurance.

The PR #55 pattern: before copying the new verbose mindmaps over two videos' existing canonical `.mindmap.md` files in the corpus, rename the existing files to `.mindmap.legacy.md`. The `.legacy.md` files match the resolver's wildcard fallback (`*.mindmap*.md` lexicographic-sort) so they're a gentle, useful fallback if the canonical is later deleted. Not a footgun.

The `.legacy.md` cost ~7 KB per video. The downside risk it insures against ("I just overwrote my best artifact and now the new one is broken") would cost an hour to recover from a re-run. Trivial cost-benefit. Do it.

### 7. Where this discipline is overkill

This framework is not the universal floor. Apply when scope and blast radius warrant it:

- PR ≥ 100 changed lines AND touches an orchestrator OR a user-facing API: full framework
- PR introduces a new architectural seam (new helper function category, new prompt template family, new resolver path): full framework
- PR's output is consumed by downstream automated systems (graph builder, search index, billing): full framework
- PR is < 30 lines and touches one well-tested helper: tests-pass plus a 1-line smoke is sufficient
- PR is a typo fix or comment update: no smoke needed; ruff plus ship

The cost of over-applying the framework is real (5 hours per PR is unsustainable across a 100-PR project). The cost of under-applying it on architectural changes is also real (silent corruption of downstream layers). The agent and the human should agree on scope at the start of each session.

### 8. A review is finished only when every layer is triaged, the fix commit is re-verified, and every claim has been executed

"Both layers ran, everything load-bearing applied" is not a merge signal, and neither is "review found X, X fixed". Four habits close the gap.

**Triage every expected output, not the ones you happened to read.** `/ce-code-review` has each persona write its full analysis to its own file in a per-run artifact folder, and that folder does not travel with the PR. In one multi-persona review only one persona's output was read before the session moved on: several findings sat unread for days (one of them a false failure exit code) and some lenses never ran at all. Before merging, list the outputs the review should have produced, confirm each file exists, record a disposition for every finding (applied, rejected with a reason, deferred to an issue), and write a lens that did not run as "not run", never as silence.

**Treat the review-fix commit as new, unreviewed code with the same blast radius.** In one batch, the fixes themselves introduced defects that pointed the opposite way from the bug they fixed:

- A probe-before-pay guard hoisted above the upload also landed above a deliberate `skip: true` early return, so a suppressed video stopped being a no-op exit 0 (#168; the guard now sits strictly between the skip return and the upload).
- An accumulation helper validated the file it read but not the dict it mutates, so `taxonomy.setdefault("concepts", {})[cid] = ...` could raise `TypeError` inside a loop that had already paid for a Gemini call (#176).
- A canonical-selection change fed a move-and-delete step that trusted a meta's unverified `modes_completed` claim, which could delete a loser's only real copy of a mode (#159; now verified against disk before computing what is missing).

These surfaced in an executing re-verify dispatched against the fix commit, in the peer pass, or in a later review round, not in the original review. Re-run the executing reviewers and the [core: real-caller] falsification against the fix commit itself.

**The layers are disjoint detectors, so neither replaces the other.** Executing reviewers found wiring and coverage gaps: an argument parser that still refused the flags the feature implemented, so the whole feature was unreachable from the CLI while every unit test passed (#206). The cross-family Codex peer pass found ordering, identity and edge cases (the #168 lower pin, the #186 adoption conflict, the #189 token-equality bug) and documentation that was false: a rule entry claimed a helper defect reached users when no caller could feed it that input, and tracing the callers corrected it (#197). A prose claim of reach is checked by tracing callers, never by reading the claim.

**Agreement is not confirmation when reviewers share a brief.** Two reviewers handed the same premise will agree with it. Check the brief's premises, treat a reviewer's justification for clearing a risk as a claim to verify, and read the accused code yourself. That is how a span metric gets caught that scores two entries, one at the start of a window and one near its end, as near-full coverage over an empty middle (the old `_assess_chunk_coverage`, replaced in #157).

## Why This Matters

The Compound Engineering pattern dramatically compresses the *write* phase of software (overnight chain produces a merge-shape PR by morning). The temptation is to compress the *validate* phase proportionally. Two hours of validation feels excessive when the diff was produced in two minutes.

It is not excessive. The validation phase scales with scope, not with implementation time. A 200-line architectural diff produced in 20 minutes still needs the same multi-content-class smoke testing as a 200-line architectural diff produced in 20 hours. The cost of validation is owed to the *change*, not to the labor that produced the change.

The Dark Factories framing collapses these two phases for narrative clarity. The reality:

- **AI-write phase:** minutes to hours, scales with model quality and prompt design
- **Human-validate phase:** minutes to days, scales with diff scope and downstream blast radius

The two phases are independent. Compressing the validate phase to match the write phase produces silent corruption: not visible breakage that a smoke would catch, but plausibly-wrong outputs that downstream systems consume as truth. For a knowledge-graph use case (this repo), wrong-but-plausible mindmap timestamps are more dangerous than mindmap generation failures, because the failure is invisible until a downstream layer surfaces it.

The same reasoning extends to the review itself (section 8). A review layer that ran but was not read, or a fix commit that nobody re-verified, gives the merge decision the confidence of a full review with the coverage of a partial one.

### Systems Engineering frame

The discipline this doc applies (probability-based sign-off, real-input verification, adversarial cross-examination, evidence preservation) is codified in ISO/IEC/IEEE 15288:2023 (international standard for system life-cycle processes; Edition 2, current) and elaborated in publicly accessible bodies of work such as INCOSE's Systems Engineering Body of Knowledge. It has been mainstream practice for decades in financial systems engineering (where the V-model and pre-release V&V have been standard for billing systems, payment processors, and global-deployment products since the 1990s) and in telecom carrier-grade engineering (often under names like "System Test" or "Integration & Verification" rather than "Systems Engineering"). Both industries share the failure profile that motivated the discipline's formalization: the cost of a wrong-but-plausible output is far higher than the cost of a visible failure.

Modern web and SaaS culture (~2010 to 2024) moved the validation phase rightward, into CI tests and production observability, because the cost-benefit favored fast iteration over pre-release sign-off. Cheap rollback and observability replaced pre-release V&V for non-critical bugs. AI-generated PRs flip the cost-benefit back. The failure profile (wrong-but-plausible outputs flowing into downstream automated systems with hard-to-revert blast radius) is exactly what systems engineering V&V originally won on. The methodology in this doc is not new. It is a re-application of decades-old discipline to a newly-acute failure profile, run at compressed cadence: evenings on a single architectural diff, days across a project's worth of diffs.

## When to Apply

- The user is the merge-button holder for an AI-generated PR they did not write line by line
- The diff is architectural (cross-cutting refactor, new orchestrator, retired guarantee, schema change)
- The output of the changed code is consumed by an automated downstream system (not just rendered to a human)
- The user wants to ship at confidence > 95%, not at confidence = "tests green"
- The session has time budget for at least Gate 0 plus Gate 1; Gate 2 is always available since it's just a pause
- A review has returned and a fix commit is ready: section 8 applies to every such merge, whatever the diff size

**Do NOT apply** when:

- Diff is < 30 changed lines and touches a single well-tested helper
- Output is purely human-rendered (a typo in a doc, a CSS tweak, a log message)
- The user is iterating in a high-trust loop (their own throwaway repo, exploratory work) and accepts that they might have to revert
- A clear "reality check" action does not exist. If you can't construct a smoke, the gate framework is not buying you anything.

## Examples

### Example 1: Gate 0 prevents committing to a flawed architecture

Issue #54 proposed inverting the mindmap pipeline so mindmap reads from on-disk transcripts. Before any architectural code was written, a 50-line throwaway Python script ran the proposed prompt against an existing long-interview transcript and inspected the output:

```bash
# throwaway script, not committed; output captured in docs/plans/gate1-evidence/issue-54-empirical-pilot.md
# Elapsed: 49.2s
# Output bytes: 15,395
# Finish reason: STOP
# Input tokens: 47424   Output tokens: 5075
```

The pilot took 50 seconds at negligible cost. The output was qualitatively comparable to the legacy mindmap-from-video on solo-creator content. If the pilot had produced unusable output, 5 hours of architectural code work would have been avoided entirely. Gate 0 is the cheapest gate in the framework on a per-token basis.

### Example 2: Gate 1 catches a fixture-vs-production divergence (the C1 bug)

After implementation plus the full test suite passing plus ruff clean, the first real-input smoke ran:

```bash
python scripts/video_intel.py mindmap \
  --url "https://www.youtube.com/watch?v=<VIDEO_ID>" \
  --channel <channel>
```

Inspecting the resulting `meta.json`, the user noticed `transcript_status: "complete"` while my code only treated `"ok"` as healthy. The corruption: every successful single-shot transcript would have produced a mindmap stamped `mindmap_source_status: "partial"` with a misleading `<!-- source: partial transcript (complete) -->` header. The unit test fixture used `"ok"`, so the test suite was silent on the bug.

The fix was a one-line constant (now `HEALTHY_TRANSCRIPT_STATUSES = {"ok", "complete"}` in `scripts/corpus_walk.py`) and a parametrized regression test covering all three writer literals. The fix took 3 minutes; finding the bug needed 5 minutes of human inspection of a real artifact. The unit test alone would never have surfaced it.

### Example 3: Gate 2 catches a reviewer-flagged-then-dismissed bug

The `/ce-code-review` adversarial reviewer flagged a low-confidence finding (C2/C3) about title rotation: `cmd_mindmap` and `_cmd_process_url` looked up transcripts by computed prefix, but a rotated title would compute a different prefix than the existing on-disk transcript. I dismissed the finding as low-confidence speculation.

When the user, holding the merge button, asked for a third smoke against a channel they happened to know had a rotated title, the bug reproduced as a hard 1M-token-cap failure. The reviewer was right, I was wrong, the user's habit of running a third real-input smoke caught what I dismissed. The fix consulted PR #31's `_load_video_id_index` helper and added two regression tests. The pause at Gate 2 was what triggered the third smoke. Without it, the bug ships.

### Example 4: Confidence-based commitment over binary verdict

After both the long interview and the podcast showed verbose-prompt grounding > tuned-prompt grounding, the agent recommended "ship verbose." The user pushed back: "I see two good options; I cannot decide between them." The probability-based reframe:

- "Verbose is strictly better on grounding on the long interview."
- "But: that's two data points across two content classes. Solo-creator content (a major fraction of the corpus) has not been tested. Confidence on the verdict is ~80%, not 99%."
- "The third smoke (a solo tutorial) closes the gap if it shows the same direction. Cost: about two minutes."

The third smoke produced higher grounding on verbose than on legacy and confirmed the verdict at high confidence. The user's habit of refusing to commit on 80% confidence forced the data collection that closed the gap. Binary go/no-go would have committed too early.

### Example 5: the fix commit is the riskiest diff in the PR

Issue #168 hoisted the `chunk_minutes` guard on `process --file` above `upload_local_video`, so a one-character config typo stopped buying a full MP4 upload. The first cut was correct about the upload and wrong about the skip: it also landed above the legacy `skip: true` early return, so a deliberately suppressed video started failing on a knob governing work that was never going to happen. The original review had passed the change; the Codex peer pass against the fix caught it. Both pins are now tests (`tests/test_chunk_minutes_guards.py::TestProcessFileChunkMinutesGuard::test_guard_runs_before_the_gemini_upload_not_after` and `::test_a_deliberately_skipped_video_stays_a_no_op_exit_0`), and each was falsified by moving the guard the wrong way and confirming exactly one test failed. The lesson generalizes: a review-fix commit moves code under time pressure with less scrutiny than the original diff, so it gets the same executing re-verify.

## Related

- [docs/solutions/workflow-issues/full-sdlc-chain-via-context-packets-20260423.md](full-sdlc-chain-via-context-packets-20260423.md): the chain-orchestration side of the same workflow (how to run the SDLC chain end-to-end). This doc covers chain *output validation*; that doc covers chain *operation*. Read both.
- [docs/solutions/workflow-issues/compound-engineering-four-artifacts-20260417.md](compound-engineering-four-artifacts-20260417.md): the brainstorm/plan/issue/PR artifact flywheel that produces the AI-generated PR this framework validates.
- [docs/solutions/workflow-issues/ai-hallucination-cross-check-via-source-of-truth-ui-20260425.md](ai-hallucination-cross-check-via-source-of-truth-ui-20260425.md): adjacent pattern (cross-checking AI output against an authoritative non-AI surface). Same family of discipline at a different layer.
- [PR #55](https://github.com/dzivkovi/video-intel/pull/55): the concrete execution this learning documents (issue #54: invert the mindmap pipeline).
- `CLAUDE.md` Code Review Guardrails, [core: real-caller] and [core: writer's-path]: the falsification rule section 8 asks you to re-run against the fix commit.
- `.claude/rules/scan-config.md` (#159, #169 entries, and the #168 amendment in the #135 entry), `.claude/rules/search-index.md` (#176 entry), `.claude/rules/translate-bcs.md` (#197 reachability, #206 item 1): the fix-commit and disjoint-layer cases in section 8.
- `specs/agent-rules.md` §4 (Git Hygiene), §7 (Priority & Stopping Conditions): the destructive-action rules that shape the Gate 2 boundary.

### Useful Systems Engineering resources

For readers exploring the discipline this doc applies to AI-generated PRs:

- **[ISO/IEC/IEEE 15288:2023](https://en.wikipedia.org/wiki/ISO/IEC_15288)**: *Systems and software engineering. System life cycle processes*. Edition 2 (current; supersedes the withdrawn 2015 edition). International standard for the life-cycle and V&V processes the gate framework above re-applies.
- **INCOSE Systems Engineering Body of Knowledge (SEBoK)**: [sebokwiki.org](https://sebokwiki.org/). Open reference for systems engineering practice; readable without membership.
- **[V-model](https://en.wikipedia.org/wiki/V-model) and [V&V (verification and validation)](https://en.wikipedia.org/wiki/Verification_and_validation)**: V (verification) asks "Are we building the thing right?" V&V adds "Are we building the right thing?" The V-model is the visual structure: every level of design has a corresponding level of verification (requirements ↔ acceptance test, architecture ↔ system test, detailed design ↔ unit test). Predates Agile by decades; structural ancestor of the gate framework above.
- **Joseph E. Kasser, *[Systems Thinker's Toolbox: Tools for Managing Complexity](https://www.amazon.ca/Systems-Thinkers-Toolbox-Managing-Complexity/dp/1138606766)*** (CRC Press, 2018, 526 pages): a catalog of 100+ systems-thinking tools (lists, causal loops, templates, processes, methodologies, frameworks). Useful from either direction. Practitioners who arrived at systems thinking empirically use it to put names on patterns already in their toolkit. Readers new to the discipline use it as a reference to learn from. The book's breadth can be overwhelming on a first pass.
