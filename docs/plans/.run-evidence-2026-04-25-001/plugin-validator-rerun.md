# plugin-validator rerun evidence — 2026-04-25

PR: feat/skill-factcheck-triggers
Plan: docs/plans/2026-04-25-001-feat-skill-factcheck-triggers-plan.md

## Important caveat

The autonomous SDLC chain that ran tonight does **not** have access to the
`plugin-dev:plugin-validator` agent in its skill registry — it is not in
the available-skills list nor in the deferred-tools index. The brief asked
for an agent rerun; what is captured below is a **manual structural
validation** following the same checklist the validator agent runs. Daniel
should run the actual agent at his desk in the morning if he wants the
canonical agent output; this manual rerun is the best the chain can do.

## Manual validation checklist

### 1. Plugin manifest

`.claude-plugin/plugin.json`:

```json
{
  "name": "video-intel",
  "version": "1.11.1",
  "description": "...",
  "author": { "name": "Daniel Zivkovic" }
}
```

- [x] `name` matches expected marketplace key (`video-intel`)
- [x] `version` bumped from `1.11.0` to `1.11.1` (patch — frontmatter
      description / routing surface change, no new CLI commands)
- [x] No other field changes
- [x] JSON parses cleanly (verified via `json.loads`)

### 2. SKILL.md frontmatter contract

Verified via `yaml.safe_load` on each `skills/*/SKILL.md`:

| Skill | Frontmatter parses | name matches dir | description ASCII-clean |
|---|---|---|---|
| `translate-bcs` | yes | yes | NO (pre-existing em-dash at offset 983 — explicitly out of scope per plan §Scope Boundaries) |
| `video-intel` | yes | yes | yes |
| `video-intel-search` | yes | yes | yes |

The `translate-bcs` em-dash at hex `0x2014` is the documented
pre-existing finding from the baseline run — the plan and brief
explicitly mark it as out of scope for this PR.

The two skills changed in this PR (`video-intel-search` for Unit 1 and
`video-intel` for Unit 2) both have ASCII-clean descriptions per the
contract in R8.

### 3. Description-content checks (Unit 1 + Unit 2 surface)

`video-intel-search/SKILL.md` description:

- [x] Six new verification triggers present in folded scalar (per KD3):
      `verify whether`, `fact-check`, `did [creator] really say`,
      `is this [creator] quote real`, `find the source`, `check the
      corpus for the quote`
- [x] All quoting uses ASCII double-quotes (no smart quotes)
- [x] Existing 22 trigger phrases preserved (no regression)
- [x] Folded scalar parses without indentation errors

`video-intel/SKILL.md` body line ~121:

- [x] "Wrong skill" pointer row updated with `verify quote` and
      `fact-check claim against [creator]`
- [x] Pointer text still names `video-intel-search` as the destination
- [x] Em-dash placeholder in Command column preserved as-is (was already
      unicode em-dash; not added by this PR)

### 4. Mutex test contract

Run: `pytest tests/test_skill_descriptions.py -v`

Result: **59 passed** (52 baseline + 5 new SEARCH_TRIGGERS substrings + 1
new body-callout test + 1 new cross-skill bounce test).

The mutex contract — every search-trigger substring appears in
video-intel-search description and is absent from video-intel
description — holds across all 16 SEARCH_TRIGGERS (11 baseline + 5
verification) without modification to the curate description.

## Summary

PASS on the manual structural checklist. The plugin manifest version
bump, frontmatter ASCII compliance on the two changed skills, and the
mutex test contract all check out. Pre-existing translate-bcs em-dash is
out of scope and untouched.

If Daniel wants the canonical `plugin-dev:plugin-validator` agent output
for the PR record, run it locally tomorrow against the merged or
about-to-merge branch — the manual structural validation above should
match.
