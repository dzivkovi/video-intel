---
title: Fix marketplace-key mismatch and document user-level install properly
type: fix
status: active
date: 2026-04-24
origin: docs/plans/2026-04-23-001-feat-search-skill-portability-plan.md
---

# Fix marketplace-key mismatch and document user-level install properly

## Overview

The `video-intel-local` marketplace key that ships in the repo's project-scoped `.claude/settings.json` and that PR #35's CLAUDE.md told users to copy to their user-level settings silently breaks the plugin install on Claude Code. Claude Code's internal plugin registry normalizes the marketplace key to match `plugin.json`'s `name` field (`video-intel`), stripping any suffix. The `enabledPlugins` entry then points at `"video-intel@video-intel-local"` - a marketplace that no longer exists under that name - so the plugin never installs and no skills register.

Surfaced yesterday after 24 hours of debugging: user pasted the install command, CLI verification passed, skills still didn't appear in other projects. Fixed in user's settings by renaming `"video-intel-local"` → `"video-intel"` in both places. After Claude Code restart all three skills (video-intel, video-intel-search, translate-bcs) became globally available and routed correctly (verified via four successful skill invocations from ~/Documents/mock-api).

This plan folds the fix into PR #35 (branch `feat/search-skill-portability`) before merge so no new cloner of this repo stumbles into the same trap.

A second, smaller friction surfaced during the same session: a fresh agent invoked `python video_intel.py nugget "X" --log-level info` and hit an argparse error. The flag must go before the subcommand. `skills/translate-bcs/SKILL.md` already documents this (line 86); the two video-intel SKILL.md files do not.

## Problem Frame

Two install-surface bugs discovered in the same PR-35 validation session:

1. **Marketplace name mismatch** (high-impact, silent failure mode). Repo's `.claude/settings.json` uses marketplace key `"video-intel-local"`. Claude Code's registry stores it as `"video-intel"` (matching `plugin.json` name). `enabledPlugins["video-intel@video-intel-local"]` references a marketplace under a name that does not exist. Plugin never installs. User sees no skills. CLI works (scripts don't care about Claude Code's plugin registry) so everything looks fine from a smoke test, but Claude itself sees no skills.

2. **`--log-level` placement undocumented** (low-impact but reproducible friction). A first-time agent will naturally try `nugget "X" --log-level info` and hit argparse. `translate-bcs/SKILL.md` tells Claude the flag goes before the subcommand; `video-intel/SKILL.md` and `video-intel-search/SKILL.md` do not.

Both are pure documentation and settings drift. No code logic is wrong.

## Requirements Trace

- R1. Repo's `.claude/settings.json` marketplace key matches what Claude Code's registry will normalize it to (`video-intel`, matching `.claude-plugin/plugin.json`'s `name` field).
- R2. INSTALLATION.md covers user-level install (missing today - only project-level install is documented).
- R3. INSTALLATION.md and CLAUDE.md both warn that the marketplace key in `extraKnownMarketplaces` must match `plugin.json`'s `name` field. Explanation includes symptom ("skills never appear") and diagnosis pointer (Claude Code registry normalizes the key).
- R4. CLAUDE.md's User-level install subsection is shortened to point at INSTALLATION.md as the canonical procedure, keeping CLAUDE.md tight.
- R5. `skills/video-intel/SKILL.md` and `skills/video-intel-search/SKILL.md` document that `--log-level` goes before the subcommand.
- R6. Plan and changes land in PR #35 same branch, not a follow-up. User explicitly asked to ship cleanly before merge.

## Scope Boundaries

- No Python code changes. Scripts are correct; the bug is in the settings ship-template and install docs.
- No new tests. Install procedure isn't unit-testable (Claude Code's plugin selector is not inspectable from Python).
- No changes to `skills/translate-bcs/SKILL.md`. It already documents `--log-level` placement.
- No restructuring of CLAUDE.md's Corpus Discovery or Architecture sections. Those are correct; only the User-level install subsection is being trimmed + pointed.
- No new ADR. The fix is a drift correction, not a new design decision.

## Context & Research

### Relevant Code and Patterns

- `.claude/settings.json` - project-scoped marketplace registration for anyone who clones the repo. Currently uses `"video-intel-local"` as key; needs `"video-intel"`.
- `.claude-plugin/plugin.json` - authoritative source for the plugin `name` field (`video-intel`). The marketplace key in `settings.json` must match this exactly.
- `INSTALLATION.md` (178 lines) - the canonical install doc, already covers Prerequisites, project-level Claude Code install via "clone + trust prompt", other platforms, Verify, Configure, Update. Missing: user-level install (new in PR #35).
- `CLAUDE.md` - has a User-level install subsection added in PR #35. Will shrink to a pointer + the gotcha callout.
- `skills/translate-bcs/SKILL.md:86` - existing `--log-level` placement note, the pattern to mirror.
- `skills/video-intel/SKILL.md` - `Important: These Commands Are Slow` section near the top is the natural home for a `--log-level` note.
- `skills/video-intel-search/SKILL.md` - same structural home.

### Institutional Learnings

- Yesterday's inline code review (PR #35, commit `0a66fef`) flagged a related gap (missing positive-case test for curate guard). Fixed. No existing `docs/solutions/` entry for this marketplace-name class of bug - this plan's work could seed one in a follow-up if the pattern recurs.
- The one-skill-body-per-SKILL.md rule (KD4 in the parent plan) continues to apply: each SKILL.md documents only its own subcommands. The `--log-level` note is a shared-surface detail, so duplicating it across both video-intel SKILL.md files is correct per that rule.

### External References

- None needed. Claude Code's plugin manifest handling is the authoritative source; the bug was an undocumented normalization behavior.

## Key Technical Decisions

- KD1. **Marketplace key must match plugin.json `name` field, no suffix.** Removing `-local` aligns the repo-shipped settings with Claude Code's internal registry normalization. Same principle extends to any user-level install copy-pasted from INSTALLATION.md or CLAUDE.md.
- KD2. **INSTALLATION.md is the canonical install procedure; CLAUDE.md points at it.** INSTALLATION.md is human-facing and already covers project-level install well. User-level install belongs in the same doc, not duplicated into CLAUDE.md. CLAUDE.md keeps a short pointer + the gotcha callout (agent-facing reference material, not full walkthrough).
- KD3. **Warn users with the exact symptom and diagnosis.** The failure mode is silent (plugin never installs, no error message). The doc has to name the symptom ("skills never appear in other projects") so users searching for that phrase find the fix. Callout format, not prose.
- KD4. **Ship in PR #35, not a follow-up.** User explicitly requested. Scope is ~5 files, zero code, zero tests. Bundling is cleaner than splitting.

## Open Questions

### Resolved During Planning

- **CLAUDE.md vs INSTALLATION.md for user-level install procedure:** INSTALLATION.md wins. CLAUDE.md gets a short pointer. Rationale: INSTALLATION.md is the human-discoverable install doc already; adding user-level there keeps the reader in one place.
- **Should the gotcha be a `> Warning:` blockquote, a note section, or inline prose?** Blockquote callout at top of each location (INSTALLATION.md user-level section + CLAUDE.md user-level pointer). Maximizes scannability for a user skimming for "why isn't it working?"
- **Do we need to also update translate-bcs SKILL.md?** No - it already has the `--log-level` placement note. Surgical-changes rule (specs/agent-rules.md §1) says do not touch.

### Deferred to Implementation

- **Exact wording of the marketplace-key callout.** Draft during Unit 2 implementation; test by re-reading through the lens of yesterday's bug session ("would this have prevented the 24-hour debug?").
- **Whether to add a troubleshooting section to INSTALLATION.md** covering "skills not appearing after install" with this marketplace-key bug as the first entry. If yes, small addition at the bottom of INSTALLATION.md. If no, the callout in the User-level install section is enough. Implementer decides based on final prose length.

## Implementation Units

- [ ] **Unit 1: Rename marketplace key in repo's `.claude/settings.json`**

**Goal:** The project-scoped marketplace key and enabledPlugins entry match `.claude-plugin/plugin.json`'s `name` field (`video-intel`), so new cloners of this repo do not hit the same silent-install failure the user hit yesterday.

**Requirements:** R1.

**Dependencies:** None.

**Files:**
- Modify: `.claude/settings.json`

**Approach:**
- Rename `extraKnownMarketplaces["video-intel-local"]` → `extraKnownMarketplaces["video-intel"]`.
- Rename `enabledPlugins["video-intel@video-intel-local"]` → `enabledPlugins["video-intel@video-intel"]`.
- `path: "."` stays untouched - directory-source marketplace pointing at the repo root is correct.

**Patterns to follow:**
- Match exactly what the author already set in their user-level `~/.claude/settings.json` after yesterday's fix (lines with `"video-intel": { source: directory, path: ... }` and `"video-intel@video-intel": true`).

**Test scenarios:**
- Test expectation: none -- this is a settings.json key rename; behavior change is verified by Claude Code restart, not pytest. Existing 595 tests continue to pass (no script logic touched).

**Verification:**
- File diff shows exactly two string changes: `"video-intel-local"` → `"video-intel"` as marketplace key and `"video-intel@video-intel-local"` → `"video-intel@video-intel"` as enabledPlugins key.
- Running `python scripts/video_intel.py status` from the plugin repo still works (SKILL_DIR-based config resolution does not depend on the marketplace key).

- [ ] **Unit 2: Add User-level install section to INSTALLATION.md with marketplace-key gotcha**

**Goal:** INSTALLATION.md becomes the canonical install doc for both project-scoped (existing) and user-level (new) installs, with a prominent callout warning about the marketplace-key normalization behavior.

**Requirements:** R2, R3.

**Dependencies:** Unit 1 (the example JSON shown to users should match the fixed key).

**Files:**
- Modify: `INSTALLATION.md`

**Approach:**
- Add a new section after the existing `### Claude Code (recommended)` subsection (which covers project-scoped install via clone). Proposed heading: `### Claude Code user-level (access from any project)`.
- Section structure:
  1. One-sentence purpose: what this enables (read-only search skill reachable from any Claude Code session anywhere on the machine).
  2. A `> Warning:` callout at the top of the section naming the marketplace-key rule. Must say: the key under `extraKnownMarketplaces` and the suffix after `@` in `enabledPlugins` must both be `video-intel` (matching `.claude-plugin/plugin.json`'s `name` field). Adding any suffix like `-local` will silently fail. Symptom if you get it wrong: skills never appear in other projects.
  3. Three numbered steps: (a) edit `~/.claude/settings.json` and paste the exact JSON (show it with `"video-intel"` as both keys); (b) either set `VIDEO_INTEL_OUTPUT_DIR` env var OR create `~/.video-intel/config.yaml`; (c) verify with `python /abs/path/to/plugin/scripts/video_intel.py status` from any non-plugin directory.
  4. OS matrix for the absolute path (Windows `C:/...`, macOS `/Users/...`, Linux `/home/...`).
  5. One-sentence note: curate operations (scan, index, concepts, dedupe, process) still require the plugin repo as CWD; only read/search/nugget/status operations work from a user-level install.
- Optional addition (deferred decision, see Open Questions): a small Troubleshooting subsection at the bottom of INSTALLATION.md covering "skills not appearing after install" with the marketplace-key bug as the first entry.

**Patterns to follow:**
- Existing INSTALLATION.md section structure: heading, 1-2 prose sentences, code/JSON block, verification step. Mirror that voice.
- The gotcha callout phrasing must include words a frustrated user would grep for: "skills not appearing", "plugin not loading", "marketplace key".

**Test scenarios:**
- Test expectation: none -- markdown content. Verification is human read-through against yesterday's debug experience (see Verification).

**Verification:**
- Read the new section as if you are the user yesterday before they hit the bug. Answer the question: "Would this prose have prevented my 24-hour debug?" If yes, ship. If no, tighten wording.
- Grep for `video-intel-local` in `INSTALLATION.md` - should return zero matches after this unit.
- The OS matrix covers Windows, macOS, Linux with clear example paths.

- [ ] **Unit 3: Shorten CLAUDE.md's User-level install subsection and point at INSTALLATION.md**

**Goal:** CLAUDE.md's User-level install section stops duplicating INSTALLATION.md's content and becomes a concise pointer. The gotcha callout lives in both docs (CLAUDE.md for agents reading it, INSTALLATION.md for humans reading it) but the full walkthrough stays in one place.

**Requirements:** R3, R4.

**Dependencies:** Unit 2 (INSTALLATION.md must have the full section before CLAUDE.md can point at it).

**Files:**
- Modify: `CLAUDE.md` (the `### User-level install` subsection added in PR #35)

**Approach:**
- Replace the current step-by-step walkthrough in CLAUDE.md with a 5-7 line section:
  1. One sentence: what this enables.
  2. A `> Warning:` callout with the marketplace-key rule (same content as INSTALLATION.md's callout but tighter).
  3. A single pointer sentence: "For the full step-by-step install procedure (JSON to paste, env var setup, OS-specific paths), see [INSTALLATION.md](INSTALLATION.md)."
  4. Preserve the one-sentence curate-still-requires-plugin-repo note.
- Do not touch the Corpus Discovery subsection above it - that's technical reference material that belongs in CLAUDE.md.
- Do not touch the Architecture, Commands, Release Process, or any other CLAUDE.md section.

**Patterns to follow:**
- Surgical-changes rule (specs/agent-rules.md §1): only change what this unit requires. Do not reformat adjacent lines.

**Test scenarios:**
- Test expectation: none -- markdown content.

**Verification:**
- Word count of the User-level install section is meaningfully lower than before (was a multi-step walkthrough, now a pointer + callout).
- The INSTALLATION.md link resolves (file exists, anchor or page-level link is correct).
- Grep for `video-intel-local` in `CLAUDE.md` - zero matches after this unit.

- [ ] **Unit 4: Add `--log-level` placement note to both video-intel SKILL.md files**

**Goal:** A first-time agent invoking `python video_intel.py nugget "X" --log-level info` is told by the SKILL.md body to put the flag before the subcommand, preventing the argparse error the other chat's agent hit yesterday.

**Requirements:** R5.

**Dependencies:** None (parallelizable with Units 1-3).

**Files:**
- Modify: `skills/video-intel/SKILL.md`
- Modify: `skills/video-intel-search/SKILL.md`

**Approach:**
- In each file, find the `## Important: These Commands Are Slow` section (or equivalent near-top Important/Tips block).
- Add one bullet consistent with the voice already there. Suggested: `**`--log-level` goes before the subcommand.** `python video_intel.py --log-level info nugget "query"` works; `nugget "query" --log-level info` errors with argparse. Same for all subcommands.`
- Keep the wording short - this is a tip, not a manual.

**Patterns to follow:**
- `skills/translate-bcs/SKILL.md:86` for the phrasing precedent. Do not copy verbatim - adapt to video-intel's subcommand examples (`scan`, `transcript`, `mindmap`, `process`, `concepts` for curate; `search`, `nugget`, `status` for search).

**Test scenarios:**
- Test expectation: none -- markdown content.

**Verification:**
- Both files contain the note. Phrasing is consistent between them (same sentence, different subcommand example per skill).
- `tests/test_skill_descriptions.py` still passes (41 parametrized tests - the trigger-phrase mutual-exclusion check is unaffected by body edits).

## System-Wide Impact

- **Interaction graph:** No runtime graph changes. The fix lands in static settings/docs; runtime code is unchanged.
- **Error propagation:** The marketplace-key bug's failure mode is silent (plugin never installs, no error). Unit 1 + Unit 2 + Unit 3 together convert this to a prevented failure by getting the defaults right and warning anyone who edits the install manually.
- **API surface parity:** N/A - no API or CLI surface change.
- **Integration coverage:** The routing matrix from PR #35's parent plan (SC-1, 15 phrases) still applies post-merge. No change.
- **Unchanged invariants:**
  - `scripts/video_intel.py` behavior is byte-identical (no touch).
  - `load_config()` precedence chain is unchanged.
  - `require_channels_config()` guard scope is unchanged.
  - Existing 595 tests continue to pass (no code touched that tests exercise).

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Someone who already cloned the repo before this fix has their local `.claude/settings.json` with the old `video-intel-local` key, and `git pull` merges the new `"video-intel"` cleanly, but their Claude Code session has cached the old registration. | Add a one-line note to the Updating section of INSTALLATION.md: "After pulling v1.10.x or later, close and reopen Claude Code to pick up the corrected marketplace registration." Small addition; surfaces the restart-to-reload requirement without requiring users to read release notes. |
| The gotcha callout gets skimmed past. User still pastes a suffix by accident. | The callout phrasing has to name the symptom ("skills never appear in other projects") so a user searching for that phrase during debugging finds the cause. Verification step in Unit 2 explicitly tests the prose against yesterday's debug experience. |
| Changing repo-shipped `.claude/settings.json` invalidates any automation or hook that greps for `video-intel-local`. | No such automation exists in this repo (grep-verified during planning). External callers would be edge-case and are out of this PR's scope. |
| INSTALLATION.md grows large enough that users miss the new User-level section. | Keep the section concise (target: fits on one screen at normal zoom). The troubleshooting addition is deferred - add only if the section feels thin without it. |

## Documentation / Operational Notes

- The repo ships with one tracked, human-authored config: `config.yaml.example`. The user's own `config.yaml` is gitignored (Unit 0 of PR #35's parent plan). No config-shape documentation is affected by this PR.
- `plugin.json` `version` is already bumped to 1.10.0 in PR #35. This doc fix does not warrant another version bump - it's part of the same 1.10.0 release.
- Monitoring / rollout / metrics: none. This is a doc fix.

## Sources & References

- **Origin plan:** [docs/plans/2026-04-23-001-feat-search-skill-portability-plan.md](2026-04-23-001-feat-search-skill-portability-plan.md)
- **Parent brainstorm:** [docs/brainstorms/2026-04-23-search-skill-portability-requirements.md](../brainstorms/2026-04-23-search-skill-portability-requirements.md)
- **Related PR:** https://github.com/dzivkovi/video-intel/pull/35
- **Yesterday's debug session:** captured in work/2026-04-24/ notes (and in the extensive conversation log that surfaced the bug)
- `.claude-plugin/plugin.json` - authoritative `name` field (`video-intel`)
- `.claude/settings.json` - target of Unit 1
- `INSTALLATION.md` - target of Unit 2, 178 lines, covers project-level install + other platforms
- `CLAUDE.md` - target of Unit 3, User-level install subsection added in PR #35
- `skills/video-intel/SKILL.md`, `skills/video-intel-search/SKILL.md` - targets of Unit 4
- `skills/translate-bcs/SKILL.md:86` - `--log-level` placement note precedent

## Next Steps

Plan ready. No blocking questions. Proceed to `/ce-work` to execute all 4 units on the existing `feat/search-skill-portability` branch, then push to PR #35.
