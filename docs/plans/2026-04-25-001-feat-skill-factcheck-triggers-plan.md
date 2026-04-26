---
title: "feat: Skill triggers for fact-check / paraphrase verification"
type: feat
status: active
date: 2026-04-25
origin: work/2026-04-25/07-skill-search-improvement-planning-prep.md
---

# feat: Skill triggers for fact-check / paraphrase verification

## Overview

Close a triggering gap in `skills/video-intel-search/SKILL.md` so an agent
receiving a verification-shaped prompt (e.g., *"verify whether Nate B. Jones
really said 'zero human keystrokes' in his videos"*) reaches for `search
--vector` on first action instead of grepping the corpus directory directly.

Three additions land in `skills/video-intel-search/SKILL.md`: six new
verification trigger phrases in the frontmatter description, an anti-pattern
callout warning against direct `grep` of the corpus, and a new row in the
"Mode reference" routing table mapping verification intent to `search --vector
--channel C`. One row in `skills/video-intel/SKILL.md` is updated so a curate
skill agent receiving a verification query bounces it to `video-intel-search`.
Plugin version bumps `1.11.0` → `1.11.1` (patch) per the multi-skill plugin
contract. Mutex test (`tests/test_skill_descriptions.py`) is extended to lock
the new triggers in place and prevent regression.

## Problem Frame

This morning a fact-check on the LinkedIn Dark Factories post was wrong on the
first pass because the assistant used the `Grep` tool directly against the
corpus path instead of invoking the `video-intel-search` skill. Vector search
(when later attempted) surfaced the canonical Nate B. Jones quote at `[05:20]`
in one shot. Grep had returned a false negative because the speaker used
different vocabulary from the paraphrase under test.

The deeper problem (see origin: `work/2026-04-25/07-skill-search-improvement-planning-prep.md`)
is a **skill-design gap**, not just a tool-discipline failure: the existing
trigger phrases in `video-intel-search/SKILL.md` are **discovery-shaped**
("find videos about X", "what did [creator] say about [Y]"). None of the ~22
phrases use verification verbs ("verify", "fact-check", "did X really say"),
so a fresh agent doing fact-checking does not see itself in the trigger list
and the skill never gets invoked. The independently-run `plugin-dev:skill-reviewer`
baseline rated triggering effectiveness on the verification scenario at
**1.5/5 (low)**.

The fix is durable: skill-level changes help every future user without
bloating per-session memory, and the plugin already ships an explicit
mutex-test pattern (`tests/test_skill_descriptions.py`) that can absorb the
new trigger phrases as a regression guard.

## Requirements Trace

- **R1.** *Trigger-recognition contract.* A fresh agent given *"Verify
  whether Nate B. Jones really said 'zero human keystrokes' in his videos"*
  reaches for `search --vector --channel natebjones` as its first action; no
  grep on the corpus path. (origin: success criterion 1)
- **R2.** *Deterministic mutex tests pass on the new verification trigger
  substrings* — all 5 new substrings (`"verify whether"`, `"fact-check"`,
  `"really say"`, `"quote real"`, `"find the source"`) appear in
  `video-intel-search/SKILL.md` description and are absent from
  `video-intel/SKILL.md` description, locked by the existing parametrized
  pattern in `tests/test_skill_descriptions.py`. (origin: success criterion 2,
  with the doc-review revision: replaced non-deterministic skill-reviewer
  rubric proxy with deterministic substring tests.)
- **R2b.** *Post-merge manual smoke test documented in PR description* —
  the PR body includes the verbatim command Daniel runs from a fresh CWD:
  `claude -p "Verify whether Nate B. Jones really said 'zero human
  keystrokes' in his videos"`. Expected first tool call: `search`
  invocation (not `Grep` / `Read` / corpus path). This is not a chain gate
  — it's a sanity check Daniel runs at his desk after merging.
- **R3.** *plugin-validator passes* on the version bump and frontmatter
  changes. (origin: success criterion 3)
- **R4.** *No regression on existing discovery triggers* — the existing 22
  phrases continue to route correctly to `video-intel-search`, locked in by
  the existing `SEARCH_TRIGGERS` parametrized mutex test. (origin: success
  criterion 4)
- **R5.** *Anti-pattern callout absorbs the lesson explicitly* — the **why**
  is named (vocabulary mismatch / paraphrase-vs-keyword distinction), not just
  the **what**. (origin: success criterion 5)
- **R6.** *Curate-skill agents bounce verification queries to search* — the
  "Wrong skill" pointer row in `video-intel/SKILL.md` lists verify/fact-check
  example phrases. (origin: skill-reviewer recommendation D)
- **R7.** *Plugin manifest version bump in same PR* — `.claude-plugin/plugin.json`
  `version` `1.11.0` → `1.11.1` (patch). (origin: plugin-validator constraint)
- **R8.** *Frontmatter content uses ASCII-quoted strings only* — no em-dashes,
  no smart quotes, no unquoted colons in the folded scalar. (origin:
  plugin-validator constraint)
- **R9.** *CLAUDE.md guardrail bullet absorbs the lesson at the code-review
  layer* — a new bullet in the "Code Review Guardrails" section names
  paraphrase verification's vector-search-first discipline so reviewers
  catch direct-grep regressions even when skill triggers don't fire.
  (origin: doc-review adversarial F5 — backup channel for the failure mode.)

## Scope Boundaries

- The retrieval engine itself (LanceDB, Voyage embeddings, RRF math) — not
  touched. Trigger-phrase changes do not move retrieval-quality.
- The eval harness in `tests/evals/` — not touched. The 1/25 baseline is
  unrelated to skill-triggering.
- The `video-intel` curate skill **write path** — only the single "Wrong
  skill" pointer row at the bottom of the "Interpreting User Intent" table is
  modified. No changes to scan/transcript/process/dedupe/prune-shorts
  guidance.
- The `translate-bcs` skill — different domain (subtitle translation),
  unaffected by verification routing.
- Memory or auto-memory — Daniel chose skill-level fix over memory-level. No
  files added under `memory/`.
- All living/historical artifacts under `docs/plans/`, `docs/solutions/`,
  `docs/brainstorms/`, `work/` — not modified, only referenced.
- The pre-existing em-dash in `skills/translate-bcs/SKILL.md` line ~16 — known
  to plugin-validator, **out of scope** for this PR. Do not opportunistically
  clean up.
- New skill directories or marketplace key changes — not in scope. Plugin
  `name` remains `video-intel`.

## Context & Research

### Relevant Code and Patterns

- **`skills/video-intel-search/SKILL.md`** (lines 1-180): primary edit
  target. Frontmatter spans lines 1-28 (closing `---` on line 28); folded
  scalar at lines 3-27 holds 22 trigger phrases. "How to Use" section
  starts at line 80. "Mode reference" routing table at lines 100-107 (6
  rows). Existing routing tip blockquote at lines 113-116 shows the
  precedent format for callouts.
- **`skills/video-intel/SKILL.md`** (line 121): the "Wrong skill" pointer
  row in the "Interpreting User Intent" table — single-row edit only. The
  surrounding `Triage workflow` table at lines 58-62 already maps Evidence
  intent to `--vector` so no change there.
- **`tests/test_skill_descriptions.py`** (lines 1-130): the mutex-test
  pattern. `SEARCH_TRIGGERS` (lines 38-50) and `CURATE_TRIGGERS` (lines 54-66)
  are case-insensitive substring lists. Two parametrized test classes
  (`TestSearchTriggersInSearchOnly`, `TestCurateTriggersInCurateOnly`) assert
  each phrase appears in its own description and is absent from the other.
  The pattern is extension-friendly: append substrings to the lists and the
  parametrized tests pick them up automatically.
- **`.claude-plugin/plugin.json`** (current `"version": "1.11.0"`): manifest
  with `name: "video-intel"`, no `skills:` array (skills are auto-discovered
  from `skills/*/SKILL.md`). Version is the only field that changes.
- **`CLAUDE.md` Code Review Guardrails** (skill-parity section): "When a PR
  adds a new CLI subcommand or flag... the matching `SKILL.md` entry lives in
  the same PR." Inverse holds here: when a skill description changes, the
  same PR carries the matching test and version bump.
- **PR #41's Unit 5 precedent** (just merged): six-file update spanning two
  SKILL.md files + test extension + config example + CLAUDE.md + plugin.json
  version bump. This plan follows the same shape, scoped down because no CLI
  surface changes.

### Institutional Learnings

- **`work/2026-04-25/07-skill-search-improvement-planning-prep.md`** — the
  origin diagnostic. Documents the grep-vs-vector failure mode, the three
  structural additions, the success criteria, and the out-of-scope guardrails.
  Treat as the requirements doc.
- **plugin-dev:skill-reviewer baseline (run 2026-04-25):** confirms
  triggering effectiveness on verification scenario is **1.5/5**, validates
  six trigger phrase recommendations, names placement for the anti-pattern
  callout, recommends the noun-phrase guardrail on `"check the corpus for
  [claim]"` to avoid false positives like *"check the corpus directory for
  free disk space"*.
- **plugin-dev:plugin-validator baseline (run 2026-04-25):** plugin
  structure passes; required version bump `1.11.0 → 1.11.1` (patch);
  frontmatter contract — ASCII-quoted strings inside folded scalar, no em-
  dashes, no smart quotes. Pre-existing em-dash in `translate-bcs/SKILL.md` is
  explicitly out of scope.

### External References

None required. This work is internal to the plugin contract and the
mutex-test pattern is established locally.

## Key Technical Decisions

- **KD1: Anti-pattern callout placement.** Insert as a blockquote
  `> **Do not...**` immediately after the `## How to Use` header (line 80 in
  current file), before the first `### Find videos about a topic (start
  here)` subsection. **Rationale:** an agent navigating the body for the
  first time hits "How to Use" → callout → command examples. The callout is
  the first content the agent reads after the section header, maximizing the
  chance it overrides the default-grep instinct. Alternatives considered:
  putting it at the top of the SKILL.md (rejected — competes with the
  section-overview prose); placing it next to the existing routing-tip
  callout at line 113 (rejected — that's mid-section, an agent might never
  reach it before reaching for grep).

- **KD2: Routing-table row placement.** Insert as a NEW row between current
  row 4 (creator + topic = evidence query → `--vector --channel C`) and row 5
  (recent X from creator). **Rationale:** row 4 is the closest semantic
  neighbor (both end at `--vector --channel C`) and grouping vector-search-
  first intents adjacent helps a reader scanning the table. Alternative
  considered: making verification the first row (rejected — the current first
  row "which videos cover X" is the most-common discovery intent and should
  remain the entry point).

- **KD3: Six trigger phrases (post-doc-review revision).** The original
  `plugin-dev:skill-reviewer` baseline recommended six phrases, all
  verification-verb-shaped. The doc-review adversarial pass (P0 finding F2)
  flagged that real-world fact-check prompts often use no verification verb
  at all (e.g., *"Is this real?"*, *"Find the source for this Nate quote"*,
  *"Where did Nate say this?"*). To close that recall gap, two of the
  original six were swapped out for discovery-shaped verification triggers.
  Final six:
  - `"verify whether [creator] said [paraphrase]"`
  - `"fact-check this quote against [creator]'s videos"`
  - `"did [creator] really say [X]"`
  - `"is this [creator] quote real"` *(replaces "is this paraphrase
    accurate" — the new form catches the dominant paste-and-verify
    framing where users don't reach for verification verbs)*
  - `"find the source for this [creator] claim"` *(replaces "confirm
    [creator] said [Y]" — the new form catches the source-locator framing)*
  - `"check the corpus for the quote [paraphrase]"` *(guardrail-tweaked
    from the original "check the corpus for [claim]" — the noun-phrase
    qualifier `the quote` prevents false-positive triggers on unrelated
    "check the corpus" framings like free disk space, status checks, etc.)*

  **Rationale:** the swap (3 keep + 2 replace + 1 guardrail) preserves the
  6-phrase budget while improving recall on realistic fact-check phrasings.
  The dropped phrases (`"is this paraphrase accurate"`, `"confirm [creator]
  said [Y]"`) were paraphrases-of-paraphrases that bloated the description
  without adding distinct routing signal. The last phrase is retained for
  description symmetry but is not load-bearing — the first five carry the
  recall.

- **KD4: Test extension via SEARCH_TRIGGERS substrings.** Extend the
  existing `SEARCH_TRIGGERS` list in `tests/test_skill_descriptions.py` with
  five high-distinctiveness substrings: `"verify whether"`, `"fact-check"`,
  `"really say"`, `"quote real"`, `"find the source"`. **Rationale:** the
  existing parametrized `TestSearchTriggersInSearchOnly` class will pick up
  the new substrings automatically and assert they appear in
  `video-intel-search` and are absent from `video-intel`. No new test class
  needed — the mutex contract is already structured for substring extension.
  Substring choices avoid collisions: `"verify"` alone is too generic;
  `"check"` alone risks false matches against curate's `"check for new
  videos"`; the chosen substrings each tie to a specific trigger phrase
  (`"verify whether"` → phrase 1, `"fact-check"` → phrase 2, `"really say"`
  → phrase 3, `"quote real"` → phrase 4, `"find the source"` → phrase 5).
  Phrase 6 (`"check the corpus for the quote..."`) is intentionally not
  given a mutex substring — its real-world recall is low and adding a
  unique substring like `"corpus for the quote"` would be brittle if the
  phrase ever gets refined. Five out of six covered is enough to lock the
  contract; the sixth is body content covered by broader regression.

- **KD5: Body callout regression test.** Add one new test
  (`test_anti_grep_callout_present_in_search_skill_body`) that grep-asserts
  the callout substrings `"Do not"`, `` "`grep`" ``, and `"vocabulary"`
  appear in `skills/video-intel-search/SKILL.md` body (post-frontmatter).
  **Rationale:** prose tests are usually fragile, but this guards the
  specific regression most likely to bite — someone "tightening" the body
  and accidentally removing the callout. Three substring assertions on a
  single, named callout is cheap and locks the lesson's "why" (vocabulary
  mismatch) along with the "what" (no grep).

- **KD6: Cross-skill pointer test.** Add one new test
  (`test_curate_skill_routes_verify_intent_to_search_skill`) that grep-asserts
  **both** substrings — `"verify quote"` AND `"fact-check"` — appear in
  `video-intel/SKILL.md` body, plus the literal `"video-intel-search"`
  appears in the same row to lock pointer integrity. **Rationale:** Unit 2's
  edit adds both phrases to the same "Wrong skill" pointer row; AND-style
  assertion prevents a silent half-rollback (someone removing one phrase
  while leaving the other). The test scopes to the body (post-frontmatter),
  not the description.

- **KD7: Version bump magnitude.** Patch (`1.11.0 → 1.11.1`), not minor.
  **Rationale:** plugin-validator's analysis — frontmatter description edits
  change discoverability/routing surface but do not add CLI commands or break
  existing trigger phrases. Per project release-process pattern, that's a
  patch. A minor bump would only be warranted if a new skill, subcommand, or
  flag landed in the same PR.

- **KD8: Plan location.** Save to `docs/plans/`, not the gitignored
  `plans/`. **Rationale:** the chain-driven plan is the contract that
  ce-doc-review reviews, ce-work executes, and the PR cites for traceability.
  Recent precedent (PR #41's `docs/plans/2026-04-24-002-feat-skip-shorts-and-
  prune-plan.md`) confirms `docs/plans/` is the chain location. The
  gitignored `plans/` directory is for ad-hoc session work, not chain
  plans. The brief's mention of `plans/` was a slip; this overrides.

## Open Questions

### Resolved During Planning

- **Where exactly does the anti-pattern callout sit?** → Right after `##
  How to Use` (line 80 in current file), before the first `### Find videos
  about a topic` subsection. (KD1)
- **Where does the new routing-table row go?** → Between current row 4 and
  row 5 of the "Mode reference" table. (KD2)
- **Which guardrail does the "check the corpus for [claim]" trigger
  need?** → Tighten to *"check the corpus for the quote [paraphrase]"*. (KD3)
- **What does the regression test look like?** → Three substring additions
  to `SEARCH_TRIGGERS` (`"verify whether"`, `"fact-check"`, `"really say"`)
  + one new body-callout test + one new cross-skill pointer test. (KD4-KD6)
- **What's the version bump?** → Patch, `1.11.0 → 1.11.1`. (KD7)
- **Where does the plan file live?** → `docs/plans/`, committed, ships with
  the PR. (KD8)

### Deferred to Implementation

- **Exact YAML re-indentation of the folded scalar after adding 6 phrases.**
  The folded scalar at lines 3-27 wraps at ~70 chars; the implementer should
  match the existing wrap rhythm. Easy to verify visually after edits — not
  worth pre-specifying. If the indentation turns out fragile, the implementer
  can switch to a literal block scalar (`description: |`), but that's a
  bigger change than warranted. Default: stay with `>` folded.
- **Whether to run skill-reviewer / plugin-validator one more time after
  Unit 1 lands or wait until all three units are merged.** Recommendation:
  re-run after Unit 1 alone — that's where R2 (skill-reviewer rates higher)
  is actually verified. Units 2-3 are mechanical and don't need re-baselining.
  But the implementer can defer either way; it doesn't change the PR shape.

## Implementation Units

- [ ] **Unit 1: video-intel-search frontmatter triggers + body callout + routing-table row + tests**

**Goal:** Add the verification-shaped triggers and anti-grep guidance that
make a fresh fact-checking agent reach for `search --vector` first. This is
the unit where the trigger-recognition contract (R1) actually lands.

**Requirements:** R1, R2, R4, R5, R8

**Dependencies:** None (entry unit).

**Files:**
- Modify: `skills/video-intel-search/SKILL.md`
- Modify: `tests/test_skill_descriptions.py`

**Approach:**

1. **Frontmatter description (lines 3-27).** Append the six verification
   triggers as ASCII double-quoted strings inside the existing `description:
   >` folded scalar. Match the existing wrap rhythm (~70 chars per line).
   Phrases (per KD3 final list, post-doc-review revision):
   `"verify whether [creator] said [paraphrase]"`, `"fact-check this quote
   against [creator]'s videos"`, `"did [creator] really say [X]"`, `"is
   this [creator] quote real"`, `"find the source for this [creator]
   claim"`, `"check the corpus for the quote [paraphrase]"`.
2. **Body callout (after line 80, `## How to Use` header).** Insert a
   blockquote callout: *"Do not `grep` / `Grep` / `rg` the `output_dir`
   directory directly when verifying a paraphrase. The speaker's
   vocabulary almost never matches a paraphrase verbatim, so keyword
   search returns false negatives. Always start with `search --vector`,
   which uses semantic similarity to overcome that vocabulary mismatch.
   Direct file search is only appropriate when the user has already given
   you an exact phrase known to appear in transcripts."* The callout uses
   ASCII punctuation only (no em-dashes, no smart quotes); the word
   "vocabulary" appears twice so KD5's regression test passes.
3. **Routing-table row (between current rows 4 and 5 in "Mode reference"
   table at lines 100-107).** Insert: `| **"verify [creator] said
   [paraphrase]"** / **"fact-check this quote"** / **"did [creator] really
   say [X]"** | **\`search "<key noun phrase>" --vector --channel C\`** then
   **\`nugget\`** if multiple chunks help | **Paraphrase verification is a
   semantic question, not a keyword question. The speaker's vocabulary
   likely differs from the paraphrase — vector match catches it where
   keyword grep misses. Try 2-3 noun-phrase variants if the first returns
   nothing.** |`
4. **Test extension (`tests/test_skill_descriptions.py`).** Append five
   substrings to `SEARCH_TRIGGERS` list: `"verify whether"`, `"fact-check"`,
   `"really say"`, `"quote real"`, `"find the source"`. Add a new test
   method to `TestSkillMetadataSanity` (or a new class
   `TestAntiPatternCalloutInSearchSkill`):
   `test_anti_grep_callout_present_in_search_skill_body` — read the body
   (post-frontmatter) of `SEARCH_SKILL`, lower-case, assert all three
   substrings present: `"do not"`, `` "`grep`" ``, `"vocabulary"`.

**Execution note:** Test-first. Append the three SEARCH_TRIGGERS substrings
and add the body-callout test FIRST (RED — both fail because the description
and body don't yet contain the new content). Then make the SKILL.md edits
(GREEN — frontmatter triggers, body callout, routing row). Then re-run the
mutex tests AND the body callout test to confirm GREEN.

**Patterns to follow:**
- The folded scalar layout in current `description:` (lines 3-27) — match
  wrapping rhythm.
- The existing routing-tip blockquote at lines 113-116 — same callout
  visual format.
- The current "Mode reference" table column ordering (Intent | Command |
  Notes) — preserve.
- The `_load_description()` helper in `tests/test_skill_descriptions.py`
  for parsing — for the body-callout test, read the file with `.read_text()`
  and split off the frontmatter manually (mirror the pattern in
  `_load_description` but return body text instead of parsed YAML).

**Test scenarios:**
- *Happy path:* `test_phrase_in_search_skill[verify whether]` — substring
  found in `video-intel-search` description (lower-cased). Expected: pass
  after frontmatter edit, fail before.
- *Happy path:* `test_phrase_in_search_skill[fact-check]` — substring
  found in description. Expected: pass after frontmatter edit.
- *Happy path:* `test_phrase_in_search_skill[really say]` — confirms
  `"really say"` (extracted from phrase 3 `"did [creator] really say
  [X]"`) appears. Expected: pass after frontmatter edit.
- *Happy path:* `test_phrase_in_search_skill[quote real]` — confirms
  `"quote real"` (extracted from phrase 4 `"is this [creator] quote
  real"`) appears. Expected: pass after frontmatter edit.
- *Happy path:* `test_phrase_in_search_skill[find the source]` — confirms
  `"find the source"` (extracted from phrase 5 `"find the source for
  this [creator] claim"`) appears. Expected: pass after frontmatter edit.
- *Body regression:* `test_anti_grep_callout_present_in_search_skill_body` —
  asserts callout substrings `"do not"`, `` "`grep`" ``, `"vocabulary"` all
  appear in body. Expected: pass after callout insert, fail before.
- *No-regression:* every existing parametrized
  `TestSearchTriggersInSearchOnly` and `TestCurateTriggersInCurateOnly`
  case still passes — the existing 22 phrases stay routed correctly.
- *Anti-leak:* `test_phrase_not_in_curate_skill` for each new substring
  (`"verify whether"`, `"fact-check"`, `"really say"`, `"quote real"`,
  `"find the source"`) — none of them appear in `video-intel` description.
  Expected: pass. Note: Unit 2 will add `"verify quote"` and `"fact-check"`
  to the curate skill's BODY (the "Wrong skill" row), but not to the
  description; the test reads the `description` field only via
  `_load_description()`, so Unit 2 cannot break this test.

**Verification:**
- `pytest tests/test_skill_descriptions.py -v` — all parametrized cases
  pass, including the five new substrings.
- The full mutex matrix (existing 11 search + 11 curate phrases + 5 new
  search substrings = 27+ parametrized cases × 2 classes = 54+ test cases)
  passes without modification to existing entries.
- Manual: `head -30 skills/video-intel-search/SKILL.md` shows the six new
  trigger phrases in the folded scalar, ASCII-quoted, properly indented.
- Manual: the rendered "How to Use" section in the SKILL.md now shows the
  anti-grep callout immediately under the section header.
- `ruff format tests/ && ruff check tests/ --fix` returns clean.

---

- [ ] **Unit 2: video-intel curate-skill cross-pointer + test**

**Goal:** Update the "Wrong skill" pointer row in `video-intel/SKILL.md` so a
curate-skill agent receiving a verification query (e.g., the user types
"fact-check this Nate B. Jones claim") routes the user to
`video-intel-search` instead of grepping the corpus from the curate side.

**Requirements:** R6, R8 (ASCII-only edit), R4 (no regression on existing
discovery triggers).

**Dependencies:** None (independent edit; no shared file with Unit 1).

**Files:**
- Modify: `skills/video-intel/SKILL.md` (single row, line 121)
- Modify: `tests/test_skill_descriptions.py`

**Approach:**

1. **Single-row edit at line 121.** Current row reads:
   `| "find videos about X", "search for Y", "nugget brief on Z", "corpus
   status" | — | **Wrong skill.** These are read-only queries; use the
   **video-intel-search** skill. |`
   Update the example list to add verification phrases, e.g.:
   `| "find videos about X", "search for Y", "nugget brief on Z", "corpus
   status", "verify quote", "fact-check claim against [creator]" | — |
   **Wrong skill.** These are read-only queries; use the
   **video-intel-search** skill. |`
   Two ASCII-style edits only: add `"verify quote"` and `"fact-check claim
   against [creator]"` to the comma-separated example list. Em-dash on the
   command column stays as ASCII-em-dash unicode (preserved as-is, not added).
   No body or frontmatter changes elsewhere.
2. **Test addition.** Add new test class
   `TestCurateSkillBouncesVerificationQueries` (or extend
   `TestSkillMetadataSanity`) with method
   `test_curate_skill_routes_verify_intent_to_search_skill`. Read the body
   (post-frontmatter) of `CURATE_SKILL`, lower-case, assert: `"verify quote"
   in body` AND `"fact-check" in body` AND the same line/section also
   contains `"video-intel-search"` so we know the pointer is preserved.
   Use a body-substring helper similar to KD5's body-callout pattern.

**Execution note:** Test-first. Add the new test first (RED — fails because
"verify quote" / "fact-check" not yet in `video-intel/SKILL.md`). Then make
the row edit (GREEN). Run all skill-description tests to confirm no
regression on Unit 1's mutex tests.

**Patterns to follow:**
- The cell-content style of the existing "Wrong skill" row at line 121:
  comma-separated quoted phrases, em-dash placeholder in the Command
  column.
- The body-substring test helper used in Unit 1 (KD5).
- The existing `TestSkillMetadataSanity` class structure.

**Test scenarios:**
- *Happy path:* `test_curate_skill_routes_verify_intent_to_search_skill` —
  body contains both `"verify quote"` and `"fact-check"` and the literal
  `"video-intel-search"` (the pointer is preserved). Expected: pass after
  edit, fail before.
- *No-regression:* all parametrized `TestCurateTriggersInCurateOnly` cases
  still pass (the new phrases are body-only, not in the description, so the
  description-substring mutex check is unaffected).
- *No-regression:* all parametrized `TestSearchTriggersInSearchOnly` cases
  still pass (no edit to `video-intel-search` in Unit 2).
- *Anti-collision:* `"fact-check"` substring is added in BOTH
  `video-intel-search` description (Unit 1) AND `video-intel` body (Unit 2).
  Verify the existing `test_phrase_not_in_curate_skill[fact-check]` test
  (which reads only the curate `description` field, not body) still passes.
  This is by design — the search description owns the trigger; the curate
  body has it only as a "Wrong skill" example to reroute. The body-vs-
  description split is the existing convention.

**Verification:**
- `pytest tests/test_skill_descriptions.py -v` — all parametrized + new
  body-test cases pass; no regression on Unit 1 tests.
- Manual: line 121 of `video-intel/SKILL.md` shows the four new example
  phrases in the comma-separated list, with the em-dash and "Wrong skill"
  text preserved.
- `ruff format tests/ && ruff check tests/ --fix` returns clean.

---

- [ ] **Unit 3: plugin manifest version bump + final verification**

**Goal:** Bump the plugin version to reflect the discoverability/routing
change and re-run the two plugin-dev agents to confirm the success criteria
(R2, R3) are met after Unit 1 + Unit 2 land.

**Requirements:** R2, R3, R7

**Dependencies:** Units 1 and 2 must be complete and committed (so the
agents review the actual final state of the SKILL.md files).

**Files:**
- Modify: `.claude-plugin/plugin.json` (single field, `version`)

**Approach:**

1. **Single-field edit.** Change `"version": "1.11.0"` to `"version":
   "1.11.1"` in `.claude-plugin/plugin.json`. No other field changes.
2. **Re-run the two baseline agents** as part of the Gate 1 evidence the
   PR description will cite:
   - `plugin-dev:skill-reviewer` on the updated `video-intel-search/SKILL.md`
     and `video-intel/SKILL.md`. Expected: triggering effectiveness on the
     verification scenario rated higher than the 1.5/5 baseline. Exact
     numeric improvement is the agent's call; the requirement is "higher
     than baseline."
   - `plugin-dev:plugin-validator` on the full plugin. Expected: PASS,
     including the version bump and frontmatter ASCII-content checks.
3. **Capture both agent outputs** (the structured reports) and attach to
   the PR description as Gate 1 evidence so a human reviewer can verify R2
   and R3 against actual agent output, not just plan claims.

**Execution note:** No test changes. This unit is a manifest version bump
+ verification-only step.

**Patterns to follow:**
- The PR #41 Gate 1 evidence pattern: agent output captured to a file and
  referenced in the PR description.
- Plugin version bump in PR #41 (`1.10.0 → 1.11.0`): minor bump because new
  CLI command (`prune-shorts`). This unit's bump is patch, smaller, but the
  same field-edit shape.

**Test scenarios:**
- *Test expectation: none — manifest version is metadata, no behavioral
  change.* The `tests/test_skill_descriptions.py` suite still passes
  unchanged (those tests don't read `plugin.json`).
- *Manual verification (Gate 1 evidence):*
  - skill-reviewer agent rerun returns a higher rating on the verification
    scenario than the 1.5/5 baseline.
  - plugin-validator agent rerun returns PASS on plugin structure and
    frontmatter content.

**Verification:**
- `cat .claude-plugin/plugin.json` shows `"version": "1.11.1"`.
- skill-reviewer rerun output captured (text file or PR comment) shows the
  higher rating.
- plugin-validator rerun output captured shows PASS.
- All previously-passing tests still pass.

---

- [ ] **Unit 4: CLAUDE.md guardrail for paraphrase verification (backup layer)**

**Goal:** Add one bullet to CLAUDE.md "Code Review Guardrails" section so a
reviewer catches direct-grep regressions on paraphrase-verification sessions
even if the skill triggers don't fire. This is the backup channel for the
failure mode (doc-review adversarial F5).

**Requirements:** R9

**Dependencies:** None (independent edit; can land in any order with Units
1-3 but conventionally lands last in the unit sequence so the guardrail
references shipped behavior).

**Files:**
- Modify: `CLAUDE.md` (single bullet addition under "Code Review Guardrails"
  section)

**Approach:**

1. **Locate the "Code Review Guardrails" section in `CLAUDE.md`** — currently
   has 6 bulleted guardrails (bounded retries, probe before pay, timestamps,
   skill-parity, video id is identity, prune-shorts deletion patterns,
   out-of-scope cleanup flags). Append a 7th (or insert at a topical
   neighbor — adjacent to skill-parity makes sense since both concern
   skill behavior).
2. **The bullet text** (ASCII-only, no em-dashes, matches the existing
   bullet voice — declarative, action-oriented, with a "why" + "reviewers"
   coda):

   > **Paraphrase verification uses `search --vector`, not `Grep`.** The
   > speaker's vocabulary almost never matches a paraphrase verbatim;
   > keyword search returns false negatives. A user prompt like "verify
   > whether [creator] said [X]" or "is this quote real" routes to the
   > `video-intel-search` skill's hybrid-search command. Reviewers: grep
   > for direct corpus `Grep`/`Read` calls in any verification-shaped
   > session — those are bugs.

3. **No test addition.** CLAUDE.md guardrails are review prose. The
   discipline is enforced by ce-code-review reading them, not by pytest.

**Execution note:** No tests. Single-bullet addition. Verify the surrounding
section still renders correctly (no broken markdown).

**Patterns to follow:**
- The voice and shape of the existing 6 Code Review Guardrails bullets:
  declarative title, "why" body, "Reviewers:" coda where applicable.
- ASCII-only punctuation (note CLAUDE.md uses em-dashes elsewhere, but new
  content from this PR follows specs/agent-rules.md §6 — ASCII).

**Test scenarios:**
- *Test expectation: none — CLAUDE.md is documentation/review prose, not
  behavior-bearing code. The discipline is enforced by reviewers reading
  it, not by pytest.*

**Verification:**
- `grep "Paraphrase verification" CLAUDE.md` returns the new bullet.
- The "Code Review Guardrails" section still parses as a clean bulleted
  list (visual smoke test).
- The full `pytest -m "not integration"` suite still passes (CLAUDE.md
  changes don't break tests; this is a no-op verification).

## System-Wide Impact

- **Interaction graph:** The change affects how Claude Code's skill-selector
  routes user prompts to plugin skills. A user typing "verify whether
  Daniel said X" or "fact-check this paraphrase" will now invoke
  `video-intel-search` instead of (a) invoking nothing, or (b) the agent
  reaching for `Grep` directly. No code paths inside the script
  (`scripts/video_intel.py`) change.
- **Error propagation:** N/A — no error-handling changes.
- **State lifecycle risks:** N/A — no persistent state changes. The skill
  description is read at skill-discovery time by Claude Code; no migration.
- **API surface parity:** The `video-intel-search` skill's CLI surface is
  unchanged. Only the natural-language routing surface (the description
  triggers) is updated. This satisfies the CLAUDE.md "skill-parity rule"
  automatically — there is no CLI surface drift to catch.
- **Integration coverage:** Trigger-recognition is verified manually
  (success criterion R1: a fresh agent reaches for `search --vector` first).
  Unit tests cover the description-content contract; the recognition
  behavior itself is not directly testable without a Claude Code
  integration test, which is out of scope.
- **Unchanged invariants:**
  - The mutex contract between `video-intel-search` and `video-intel`
    descriptions (no trigger phrase appears in both).
  - The retrieval engine (LanceDB, Voyage, RRF) — bit-for-bit identical.
  - The eval baseline (1/25) — unchanged. Trigger-phrase changes do not
    affect retrieval quality.
  - The plugin marketplace key (`video-intel`) and the
    `extraKnownMarketplaces` / `enabledPlugins` agreement — unchanged.
  - The pre-existing em-dash in `translate-bcs/SKILL.md` line ~16 —
    explicitly preserved (out of scope).

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Folded-scalar YAML breaks after adding 6 phrases (indentation edge) | Implementer eyeballs `head -30 skills/video-intel-search/SKILL.md` after edit; `_load_description()` test asserts YAML parses. If indentation breaks, switch to literal scalar (`description: |`) — minor, but flagged. |
| New trigger `"check the corpus for the quote [paraphrase]"` false-positives on unrelated "check the corpus" queries | Guardrail (KD3): noun-phrase qualifier `for the quote` prevents the documented worry. Skill-reviewer rerun in Unit 3 catches if real-world users still trip the false positive. |
| skill-reviewer doesn't rate the update higher than 1.5/5 baseline (R2 fails) | Unit 1's three-pronged change (triggers + callout + routing row) is over-specified for the scenario. If the rerun still scores low, the issue is the rubric, not the change — escalate to the user with the agent output for judgment. |
| Cross-skill drift — `"fact-check"` appears in both search description (Unit 1) and curate body (Unit 2) | By design, see Test scenarios in Unit 2. The test reads only the `description` field; body content can repeat. The test pattern was already designed for this. |
| Plugin version bump conflicts with another in-flight bump | Low — Daniel works solo; the only in-flight branch is `feat/skill-factcheck-triggers`. Confirm `git pull` before the version-bump commit to be safe. |
| Implementer "tightens" the body callout post-Unit-1 and removes the "vocabulary" word | KD5's body-callout test asserts `"vocabulary"` is present; future edits that drop it fail. |

## Documentation / Operational Notes

- **No `docs/solutions/` entry needed for this PR.** The skill update IS
  the durable artifact. Note 07 captures the diagnostic; if a future-self
  wants a tighter writeup, that's a follow-up — not this PR. (Per CLAUDE.md
  guardrail #6, `docs/solutions/` is for solved problems with cross-PR
  applicability.)
- **No `CLAUDE.md` update needed.** The "Code Review Guardrails" section
  already covers skill-parity; the verification-trigger lesson does not
  rise to the level of a new guardrail bullet (it's a one-instance design
  fix, not a recurring trap).
- **PR description must include Gate 1 evidence:** the skill-reviewer and
  plugin-validator rerun outputs from Unit 3, captured verbatim.
- **Branch:** `feat/skill-factcheck-triggers`. No direct-to-main per PR
  Workflow memory (touches discoverability surface; >1 file; explicit
  user-facing behavior).

## Sources & References

- **Origin document (treat as requirements):** `work/2026-04-25/07-skill-search-improvement-planning-prep.md`
- **Companion kick-off note:** `work/2026-04-25/08-next-session-kickoff-skill-improvement.md`
- **Recent precedent (PR #41):** `docs/plans/2026-04-24-002-feat-skip-shorts-and-prune-plan.md` (same chain shape, different scope)
- **CLAUDE.md Code Review Guardrails** — skill-parity rule, surgical-changes rule, out-of-scope cleanup-flag rule
- **`specs/agent-rules.md` §1** — surgical changes; §6 — voice rules
- **plugin-dev:skill-reviewer baseline (run 2026-04-25)** — rated current triggering 1.5/5; recommended 6 trigger phrases + callout + routing row
- **plugin-dev:plugin-validator baseline (run 2026-04-25)** — confirmed plugin structure passes; required `1.11.0 → 1.11.1` patch bump
- **Mutex test pattern:** `tests/test_skill_descriptions.py` (`SEARCH_TRIGGERS`, `CURATE_TRIGGERS`, two parametrized test classes)
- **Anthropic plugin format:** `.claude-plugin/plugin.json` schema (auto-discovers `skills/*/SKILL.md`)
