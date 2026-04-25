---
title: Search Skill Portability - Split SKILL.md and extend corpus discovery
type: feat
status: active
date: 2026-04-23
origin: docs/brainstorms/2026-04-23-search-skill-portability-requirements.md
---

# Search Skill Portability - Split SKILL.md and extend corpus discovery

## Overview

Today the `video-intel` plugin's skills are only discoverable when Claude Code's CWD equals the plugin repo. The user wants to query their video corpus from any project they are working in, while keeping the expensive curate work (scan, index, process) pinned to the plugin repo. This plan splits the single `skills/video-intel/SKILL.md` into two descriptions - a read-only `video-intel-search` that is safe to install globally, and a trimmed `video-intel` scoped to curate verbs - and extends `load_config()` in `scripts/video_intel.py` to resolve `output_dir` via a four-step precedence so the script can locate the corpus when invoked from any CWD.

No Python script split. Both skills continue to invoke the same `scripts/video_intel.py`. No retrieval behavior changes - the Voyage graceful-fallback that came up in review is deferred to a follow-up PR per the origin doc's scope boundaries.

## Problem Frame

Three compounding reasons the corpus query workflow fails from any non-plugin project, carried from the origin document:

1. Skill discovery is project-scoped via `.claude/settings.json`'s `extraKnownMarketplaces` with `"path": "."` - the plugin's skills only appear when CWD equals the plugin repo.
2. The single `video-intel` skill bundles curate and query operational profiles under one description - a global install would put `scan`, `index`, `process` verbs into every project's skill surface.
3. `scripts/video_intel.py` resolves `config.yaml` via `SKILL_DIR` (the script's own parent directory) - one canonical config for the plugin checkout, with no mechanism for a user to point a user-installed plugin at a different `output_dir` without editing cached plugin files.

(see origin: `docs/brainstorms/2026-04-23-search-skill-portability-requirements.md`)

## Requirements Trace

**Skill Surface Split**

- R1. New `skills/video-intel-search/SKILL.md` scoped to pre-built-corpus read intent (search, nugget, status). Trigger phrases in R1 verbatim.
- R2. Existing `skills/video-intel/SKILL.md` narrowed to ingest/curate intent (scan, transcript, mindmap, concepts, taxonomy-build, process, index, dedupe). Query-intent triggers removed or moved to R1.
- R3. `skills/translate-bcs/SKILL.md` unchanged.
- R4. Both skills continue to invoke `scripts/video_intel.py`; no script split.

**Corpus Discovery**

- R5. `load_config()` resolves `output_dir` in order: (1) `SKILL_DIR/config.yaml` (existing behavior), (2) `VIDEO_INTEL_OUTPUT_DIR` env var, (3) `~/.video-intel/config.yaml`, (4) hard error naming both overrides.
- R6. User-level config accepts `output_dir` (required) and `vector_db_dir` (optional).
- R6a. Curate commands fail fast with an actionable message when `channels:` is absent from the resolved config; search commands (`search`, `nugget`, `status`) do not require `channels:`.
- R6b. Unsupported keys in the user-level config are silently ignored with one INFO log naming them.
- R7. Curate workflow in the plugin repo is unchanged - `SKILL_DIR/config.yaml` still wins when present.

**Documentation**

- R12. CLAUDE.md Architecture section gains a subsection describing corpus-discovery precedence and the user-level config shape.
- R13. A user-level install procedure is documented covering the absolute-path `extraKnownMarketplaces` entry, env var, optional user config, and the note that curate still requires the plugin repo.

**Success Criteria (pointers into the origin doc; verified in this plan's Verification fields):**

- SC-1: 15-row phrase routing matrix from origin doc Success Criteria
- SC-2: search from non-plugin CWD with env var set returns hybrid results
- SC-3: plugin-repo config wins over env var for curate commands
- SC-3a: unit test exercises each step of the four-step resolution chain
- SC-4: error when all signals absent, message names both env var and user-config path
- SC-5: `ruff format . && ruff check . --fix && pytest -m "not integration" -q` passes
- SC-6: both SKILL.md files ship in the same PR
- SC-7: CLAUDE.md updated same PR

## Scope Boundaries

- User-level install **automation** (a script that writes to `~/.claude/settings.json`). Documentation only.
- `${CLAUDE_SKILL_DIR}` placeholder cleanup in SKILL.md files.
- Python script split (one module per skill).
- Voyage graceful fallback when `VOYAGE_API_KEY` is unset. (Note: retained as an explicit exit-1 behavior from today; a follow-up PR addresses it.)
- New `list` / `open` subcommands in video-intel-search.
- Any change to `skills/translate-bcs/SKILL.md`.
- Backwards-compat naming for `VIDEO_INTEL_OUTPUT_DIR`.
- Sibling env var `VIDEO_INTEL_VECTOR_DB_DIR` to override the index location in env-var-only mode. Env-var-only installs cannot override `vector_db_dir`; users on a cloud-synced `output_dir` (ADR-0016 scenario) must either use a user-level `~/.video-intel/config.yaml` to set both keys, or accept that the index lives under `output_dir/.lancedb`. Document in CLAUDE.md; address in a follow-up PR if usage reveals friction.

### Deferred to Separate Tasks

- Graceful Voyage fallback in `hybrid_search()`: separate PR, because the fallback is a meaningful refactor (LanceDB's `query_type="hybrid"` does not decompose cleanly into vector and BM25 legs).
- `${CLAUDE_SKILL_DIR}` placeholder cleanup: cosmetic, only matters if the placeholder convention breaks in practice.
- Install-doc multi-OS automation: document-only today; if install friction appears, write a helper script in a follow-up.

## Context & Research

### Relevant Code and Patterns

- `scripts/video_intel.py:48` - `SKILL_DIR = Path(__file__).resolve().parent.parent`. The key anchor: config resolution is script-file-relative, never CWD-relative. Preserved as step 1 of the new precedence.
- `scripts/video_intel.py:57-63` - `load_config()`. 8-line function reading `SKILL_DIR / "config.yaml"`. The one function to extend with the four-step precedence.
- `scripts/video_intel.py:66-71` - `resolve_output_dir(config)`. Expands `~`, makes the dir, returns. Unchanged - reads from whatever config the resolver returned.
- `scripts/video_intel.py:74-83` - `resolve_vector_db_dir(config, output_dir)`. ADR-0016 precedent for override-via-config-key. Same shape: read a key, fall back to a default. Unchanged in this PR but mirrors the override posture.
- `scripts/video_intel.py:2066-2070` - Existing `args.channel not in configured` guard inside `cmd_mindmap`/`cmd_transcript` local-file path. Confirms the pattern: validate at entry, log actionable error, `sys.exit(1)`. The new curate guard mirrors this idiom at a different level (config-wide, not arg-specific).
- `skills/video-intel/SKILL.md:1-29` - current YAML frontmatter with ~30 trigger phrases spanning query, scan, nugget, dedupe, process, concepts. The split moves query/nugget/status triggers to the new search skill; the rest stay.
- `skills/video-intel/SKILL.md:198-547` - body sections covering every subcommand. The split redistributes body per ownership (search/nugget/status sections move to the new skill; scan/transcribe/process/index/dedupe stay).
- `skills/translate-bcs/SKILL.md` - reference for the right grain of a scoped SKILL.md description. Unchanged.
- `.claude-plugin/plugin.json` - plugin manifest. Shared `scripts/` dir already in place; confirm during implementation whether adding a second skill under `skills/video-intel-search/` requires any manifest update (expected: no; the plugin already ships two skills, `video-intel` and `translate-bcs`).
- `.claude/settings.json` - current project-scoped plugin registration via `extraKnownMarketplaces: {"path": "."}`. The pattern the install doc tells users to replicate at user level with an absolute path.

### Institutional Learnings

- `docs/adr/ADR-0016-vector-db-path-config.md` - precedent for config-override-via-key. Same design shape this plan uses for `output_dir`: read an override, fall back to a default. Mirrors the posture we want for env-var-and-user-config overrides.
- `docs/adr/ADR-0017-kb-layer-strategy.md` - retrieval-quality roadmap. This plan does not move the 1/25 eval baseline; ADR-0017 Stage 2 LightRAG is the next retrieval-quality PR. Portability and retrieval are independent axes.

### External References

- Claude Code plugin docs (CLAUDE.md reference): marketplace registration via `extraKnownMarketplaces` accepts directory sources with either relative or absolute paths. User-level `~/.claude/settings.json` follows the same schema as project-level `.claude/settings.json`.

## Key Technical Decisions

- KD1. **Four-step precedence with `SKILL_DIR` winning.** `SKILL_DIR/config.yaml` stays the default; env var and user-level config are overrides reachable only when the default is absent. Preserves existing curate behavior for authors with a local (untracked) `config.yaml`; introduces portability for users who want to point a cached plugin at a different corpus via env var or user config. Rationale carried from origin D3 (correctness over ergonomics: a stale env var must not silently redirect `scan` away from the author's local canonical corpus). **Depends on Unit 0** - if `config.yaml` stays tracked in git, step 1 would resolve to the author's paths for every marketplace user and the precedence would be dead code.
- KD2. **User-level config is a minimal subset.** `output_dir` required, `vector_db_dir` optional, everything else silently ignored with one INFO log line naming ignored keys. Matches origin D4 (YAGNI - today's requirement is a corpus pointer, nothing more).
- KD3. **Curate guard as a per-command helper, not a load-time split.** Introduce `require_channels_config(config)` that raises with the actionable message. Call it at entry of each curate command (`cmd_scan`, `cmd_mindmap`, `cmd_transcript`, `cmd_process`, `cmd_concepts`, `cmd_taxonomy_build`, `cmd_dedupe`, `cmd_index`). Search commands (`cmd_search`, `cmd_nugget`, `cmd_status`) do not call the helper. Rationale: `load_config()` stays as one function; the policy lives in the command dispatch layer where it belongs. Alternative "split into `load_config_full` / `load_config_minimal`" was considered and rejected because it would touch every subcommand's entry point without reducing complexity.
- KD4. **SKILL.md body split by subcommand ownership.** Each SKILL.md body documents only its own subcommands. Shared setup (install, config) stays in both for self-containment. Rationale: Claude Code selects one skill per request - making each body self-contained avoids the "read the other skill's body" trap.
- KD5. **Routing verification is the 15-phrase matrix, manual.** Automated tests do not verify skill routing in Claude Code (no harness for the selector). The matrix lives in the PR description and is executed manually by opening the skill in Claude Code with the right CWD and confirming each phrase's routing. Origin Success Criterion 1 already specifies the matrix.
- KD6. **Docstring over ADR for the resolution layer.** ADR-0016 was warranted because `probe_atomic_writes` catches a real filesystem bug that costs ~$0.30 per failed run. This PR's four-step precedence is a standard env-var-override-of-config pattern; the rationale fits in a ~10-line docstring above `load_config()` plus a CLAUDE.md Architecture subsection (Unit 5). Originally proposed as ADR-0019 by ce-learnings-researcher; descoped during doc-review (P2 finding from product-lens + scope-guardian). Reserve ADR numbering for decisions with non-obvious durable constraints.
- KD7. **One INFO log names which precedence step won.** Mirrors ADR-0016's `probe_atomic_writes` "tell the user where state lives" ergonomic. Example: `"Config resolved from SKILL_DIR/config.yaml"` / `"Config resolved from VIDEO_INTEL_OUTPUT_DIR=/x"` / `"Config resolved from ~/.video-intel/config.yaml"`. Emitted once per `load_config()` invocation at INFO level, inside `load_config()` itself. Downstream helpers that need the source string read a module-level variable; no signature change to `load_config()`.

## Open Questions

### Resolved During Planning

- Shape of curate guard: per-command helper (KD3). Rejected: split `load_config()` into full vs minimal.
- Ownership of `nugget` and `status` in the split: both go to `video-intel-search` per origin R1 and the "needs `channels:`" split axis.
- SKILL.md body distribution: each SKILL.md owns only its subcommands' body sections. Shared prerequisites repeated in both for self-containment.
- `cmd_mindmap` / `cmd_transcript` / `cmd_process` guard scope: call the guard ONLY inside the `if args.channel:` branch, not unconditionally. Loose-file invocations without a channel do not read `config["channels"]` and must continue to work. `cmd_taxonomy_build` and `cmd_index` do not call the guard - they read only `output_dir`. See Unit 2 Approach for the exact call-site list.
- `load_config()` return shape: unchanged (dict). The winning source string is stored in a module-level variable inside `load_config()` for any helper that needs it; the KD7 INFO log is emitted inside `load_config()` itself.

### Deferred to Implementation

- **Exact location of the helper `require_channels_config(config)`:** adjacent to `load_config()` in `scripts/video_intel.py` near line 63 is the natural home, but implementer should place it next to whichever other helpers are most cohesive once the file is open.
- **INSTALL.md vs CLAUDE.md for the install procedure:** pick during implementation based on the doc's length. If the procedure runs under ~40 lines with the OS-matrix, fold into CLAUDE.md under a new "User-level install" section; if longer, split into `INSTALL.md` and link from CLAUDE.md and README.
- **OS-matrix for absolute paths:** document Windows (`C:\\Users\\...`), macOS (`/Users/...`), and Linux (`/home/...`) in whichever doc home is picked. Small, stable content.
- **Exact text of the two new user-facing error messages** (R6a channels-absent, R5 all-signals-absent) - draft during implementation, lock in the test fixtures.

## Implementation Units

- [ ] **Unit 0: Untrack `config.yaml`, ship `config.yaml.example`**

**Goal:** Remove `config.yaml` from git tracking and commit a sanitized `config.yaml.example` in its place so that installed users cannot inherit the plugin author's local paths. Without this, step 1 of the new precedence (R5) silently wins for every marketplace-installed user because the plugin cache includes the committed `config.yaml`.

**Requirements:** Unblocks R5. Surfaced by ce-doc-review (P0-B) as a precondition for the precedence chain to be reachable at all.

**Dependencies:** None. Must land before Unit 1's tests can meaningfully exercise the step-2/step-3/step-4 branches (today's `config.yaml` presence in git would make those branches unreachable in test environments that run from a checkout).

**Files:**
- Modify: `.gitignore` (add `config.yaml`)
- Remove from git (keep on disk): `config.yaml` via `git rm --cached config.yaml`
- Create: `config.yaml.example` (sanitized template with placeholder paths like `output_dir: ~/video-intel`)
- Modify: `scripts/video_intel.py` - the existing `load_config()` error message at line 60 already says "Copy config.yaml.example to config.yaml and edit it"; no change needed, but verify Unit 1's new error wording still names the example file.
- Modify: `CLAUDE.md` - under Installation / Commands, add a one-line note: "Copy `config.yaml.example` to `config.yaml` and edit before first use."

**Approach:**
- `git rm --cached config.yaml` removes it from the index while leaving the working-copy file intact (your local `config.yaml` stays put).
- Add `config.yaml` to `.gitignore`.
- Create `config.yaml.example` by copying your current `config.yaml` structure and sanitizing: replace absolute user paths with tilde-expandable paths or placeholders, remove any entries that are user-specific rather than pattern-specific.
- Verify in the same commit that `git status` does not flag your local `config.yaml` as untracked-to-be-committed (the `.gitignore` entry suppresses that).

**Patterns to follow:**
- Standard open-source convention for `<name>.yaml.example` shipping. Matches the existing error message's expectation: `"Copy config.yaml.example to config.yaml and edit it"` at `scripts/video_intel.py:60`.

**Test scenarios:**
- Test expectation: none -- this is a git-state and file-layout change; `test_load_config.py` in Unit 1 exercises the resulting precedence behavior.

**Verification:**
- `git ls-files config.yaml` returns empty (untracked).
- `git ls-files config.yaml.example` returns the path (tracked).
- `config.yaml.example` does NOT contain author-specific paths (grep for `G:/My Drive`, `/Users/danie`, etc.).
- Local `config.yaml` still exists on disk and is ignored by git (`git status` stays clean after edits to local config.yaml).
- After `git stash` + fresh clone + attempt to run any command: `load_config()` fails at step 4 with the R5 terminal error (proving installed users no longer inherit the author's config).

- [ ] **Unit 1: Extend `load_config()` with the four-step precedence**

**Goal:** `scripts/video_intel.py`'s `load_config()` resolves `output_dir` via `SKILL_DIR/config.yaml` - `$VIDEO_INTEL_OUTPUT_DIR` - `~/.video-intel/config.yaml` - hard error, returns a config dict in all success paths.

**Requirements:** R5, R6, R6b

**Dependencies:** Unit 0 (config.yaml must be untracked before step-2/step-3/step-4 branches are reachable in test or install scenarios).

**Files:**
- Modify: `scripts/video_intel.py` (lines 57-63, `load_config()` function)
- Create: `tests/test_load_config.py`

**Approach:**
- Add an internal helper (or inline branching) that, when `SKILL_DIR/config.yaml` does not exist, consults `os.environ.get("VIDEO_INTEL_OUTPUT_DIR")` - if set, construct and return a minimal dict `{"output_dir": <path>}`.
- For the env var path: treat `os.environ.get("VIDEO_INTEL_OUTPUT_DIR", "").strip() or None` as the presence check (empty string counts as unset). If present, validate the value is an absolute path (`Path(value).is_absolute()`); if relative, error with `"VIDEO_INTEL_OUTPUT_DIR must be an absolute path, got: <value>"` and `sys.exit(1)`.
- If env var unset, check `Path.home() / ".video-intel" / "config.yaml"` - if it exists, parse with `yaml.safe_load` (wrap in try/except yaml.YAMLError; on parse failure error with the file path + exception and `sys.exit(1)`). After parsing, validate `output_dir` is present (`sys.exit(1)` naming the file and missing key if not). Then filter to supported keys (`output_dir`, `vector_db_dir`). Any other key surfaces in one INFO log line: `"Ignoring unsupported keys in ~/.video-intel/config.yaml: <comma-separated list>"` (R6b).
- If all three absent, emit the hard error naming both override paths and `sys.exit(1)` (R5 step 4; R6a and this R5 terminal error have distinct messages and distinct call sites).
- The error message (per SC-4): `"No config found. Set VIDEO_INTEL_OUTPUT_DIR=<corpus-path> or create ~/.video-intel/config.yaml with 'output_dir: <corpus-path>'. See CLAUDE.md for the user-level install procedure."` - exact string locked during implementation; the test fixture in `tests/test_load_config.py` asserts substring matches for both override names.
- `load_config()` returns a dict, unchanged signature. The winning source is recorded in a module-level variable (e.g., `_LAST_RESOLVED_SOURCE`) for downstream helpers that need it, and the KD7 INFO log is emitted inside `load_config()` itself - one line per call, naming the winning source: `"Config resolved from <path-or-env-description>"`. No change to any call site's assignment (`config = load_config()` stays).
- `resolve_output_dir(config)` at `scripts/video_intel.py:66-71` is unchanged - it reads `config["output_dir"]` and handles `~` expansion.

**Execution note:** Test-first. Write failing unit tests for each of the four precedence steps (including the extra-key INFO log), then implement to green.

**Technical design:** *(directional guidance, not implementation specification)*

Precedence as a decision flow:

```text
load_config():
  if SKILL_DIR/config.yaml exists:
      return yaml.safe_load(that file)       # step 1
  if VIDEO_INTEL_OUTPUT_DIR in env:
      return {"output_dir": env value}        # step 2
  if ~/.video-intel/config.yaml exists:
      raw = yaml.safe_load(that file)
      filter to supported keys (output_dir, vector_db_dir)
      log.info if any other keys were present
      return filtered                         # step 3
  log.error(<message that names both overrides>)
  sys.exit(1)                                 # step 4
```

**Patterns to follow:**
- Tilde expansion idiom: `Path(value).expanduser()` as used in `resolve_output_dir()` at `scripts/video_intel.py:67`.
- Error-and-exit idiom: `log.error(msg, ...); sys.exit(1)` as used throughout the file (e.g., `scripts/video_intel.py:60-61`).
- Override-vs-default posture: `resolve_vector_db_dir()` at `scripts/video_intel.py:74-83` reads a config key with a default fallback - same shape for "read env var, fall back to user config, fall back to error."

**Test scenarios:**
- Happy path: `SKILL_DIR/config.yaml` exists and is valid -> `load_config()` returns its parsed contents. No env var consulted. (SC-3, SC-3a step a absent)
- Happy path: `SKILL_DIR/config.yaml` absent, `VIDEO_INTEL_OUTPUT_DIR=/tmp/some-corpus` set -> `load_config()` returns `{"output_dir": "/tmp/some-corpus"}`. (SC-3a step a)
- Happy path: `SKILL_DIR/config.yaml` absent, env var unset, `~/.video-intel/config.yaml` exists with `{output_dir: /x, vector_db_dir: /y}` -> returns `{"output_dir": "/x", "vector_db_dir": "/y"}`. (SC-3a step b)
- Happy path: `SKILL_DIR/config.yaml` absent, env var unset, user config has extras (`{output_dir: /x, model: foo, channels: [...]}`) -> returns `{"output_dir": "/x"}`, logs one INFO line naming "model, channels". (R6b)
- Error path: all three absent -> `SystemExit(1)`, error log contains both `VIDEO_INTEL_OUTPUT_DIR` and `~/.video-intel/config.yaml`. (SC-3a step c; SC-4)
- Precedence: `SKILL_DIR/config.yaml` present AND env var set to a sentinel -> env var is NOT consulted; plugin config wins. (SC-3)
- Precedence: env var set AND user config present -> env var wins; user config is NOT read. (Step 2 wins over step 3)
- Error path: `VIDEO_INTEL_OUTPUT_DIR` set to a relative path (e.g., `"my-corpus"`) -> `SystemExit(1)`, error names "must be an absolute path". Prevents silent anchoring to SKILL_DIR via `resolve_output_dir()`'s relative-path fallback.
- Edge case: `VIDEO_INTEL_OUTPUT_DIR=""` (empty string, e.g., from an exported-but-blank shell variable) -> treated as unset; load_config proceeds to step 3 (user config) as if the env var were not present.
- Error path: user config exists but is malformed YAML (parser error) -> `SystemExit(1)`, error names the file path and the parser exception.
- Error path: user config exists and parses but is missing `output_dir` (e.g., only `vector_db_dir: /x`) -> `SystemExit(1)`, error names the file and "missing required key: output_dir".

**Verification:**
- `pytest tests/test_load_config.py -q` green on all seven scenarios.
- Running `python scripts/video_intel.py search "anything"` from inside the plugin repo behaves functionally identically to today (one new INFO log line from KD7 is the only observable diff).
- Running the same command from `/tmp` with `VIDEO_INTEL_OUTPUT_DIR` set to a real corpus path loads the env-var-sourced config and proceeds.

- [ ] **Unit 2: Add `require_channels_config()` curate guard**

**Goal:** Curate commands fail fast with an actionable message when the resolved config lacks `channels:`. Search commands (`search`, `nugget`, `status`) do not run the check.

**Requirements:** R6a

**Dependencies:** Unit 1 (the new env-var/user-config paths make `channels:`-absent configs reachable for the first time).

**Files:**
- Modify: `scripts/video_intel.py` (add helper adjacent to `load_config()` around line 63; call at entry of each curate command handler)
- Create: `tests/test_curate_guard.py`

**Approach:**
- New helper: `require_channels_config(config: dict) -> None`. Reads `config.get("channels")`. If falsy (None or empty list), emit `log.error("This command requires 'channels:' in config.yaml. Run from the plugin repo, or set VIDEO_INTEL_OUTPUT_DIR to point at a checkout that has channels configured.")` then `sys.exit(1)`. The user already saw the KD7 INFO log (from `load_config()`) earlier in the same invocation naming the winning source, so the error message does not need to repeat it. Single-parameter signature; no dependency on `load_config()`'s return shape.
- Call the helper at the top of commands that actually read `config["channels"]`:
  - `cmd_scan` (unconditional - always reads channels list)
  - `cmd_concepts` (iterates configured channels)
  - `cmd_dedupe` (groups by channel)
- Call the helper ONLY inside the `if args.channel:` branch of commands that have a loose-file fallback path:
  - `cmd_mindmap` (local-file-no-channel path doesn't need channels:)
  - `cmd_transcript` (same)
  - `cmd_process` (same)
- Do NOT call the helper in:
  - `cmd_taxonomy_build`, `cmd_index` - they read `output_dir` only, never `channels:`. A user with the search skill installed globally can legitimately rebuild derived artifacts after a corpus refresh.
  - `cmd_search`, `cmd_nugget`, `cmd_status` - search-side read-only commands.
- Exact search targets: grep for `def cmd_scan`, `def cmd_concepts`, `def cmd_dedupe` for unconditional calls. For the three loose-file commands, add the call inside the existing `if args.channel:` or `if channel_name:` branch around `scripts/video_intel.py:2066-2070` (and sibling sites).

**Execution note:** Test-first.

**Patterns to follow:**
- Existing validation idiom at `scripts/video_intel.py:2066-2070`: validate, `log.error(...)`, `sys.exit(1)`. Same shape, different scope (config-wide, not arg-specific).

**Test scenarios:**
- Happy path: curate command with `{"channels": [...], "output_dir": "..."}` runs the helper without raising. (R7)
- Error path: curate command with `{"output_dir": "..."}` (no `channels`) -> `SystemExit(1)`, error log contains "channels:" and "plugin repo". (R6a)
- Error path: curate command with `{"channels": [], "output_dir": "..."}` (empty list) -> `SystemExit(1)`. (empty-list edge case)
- Happy path: search commands (`search`, `nugget`, `status`) called with a no-channels config do NOT invoke the guard and proceed normally.
- Integration: parametrize over all eight curate command entry points; each one errors-and-exits when `channels:` is absent.

**Verification:**
- `pytest tests/test_curate_guard.py -q` green.
- Manual check: `VIDEO_INTEL_OUTPUT_DIR=/tmp/corpus python scripts/video_intel.py scan --dry-run` from `/tmp` (not the plugin repo) -> exits 1 with the expected message.
- Manual check: same command for `search "mcp"` succeeds (no guard).

- [ ] **Unit 3: Create `skills/video-intel-search/SKILL.md`**

**Goal:** New skill file scoped to pre-built-corpus read intent. Description fires cleanly on query/nugget/status phrases; body documents only the `search`, `nugget`, and `status` subcommands.

**Requirements:** R1

**Dependencies:** None (can land parallel with Unit 1/2/4).

**Files:**
- Create: `skills/video-intel-search/SKILL.md`

**Approach:**
- YAML frontmatter follows the pattern of `skills/video-intel/SKILL.md:1-29` and `skills/translate-bcs/SKILL.md:1-23`. `name: video-intel-search`. `description:` scoped to the R1 trigger-phrase set (search, nugget, status) - verbatim from the origin doc R1.
- Body sections ported from the existing `skills/video-intel/SKILL.md`:
  - "Natural-Language Content Queries" (query routing, output shape, date-window queries, fallback note)
  - "Hybrid search (evidence queries)" - the concrete CLI recipes
  - "Synthesize a consultant-grade nugget brief (cross-creator)" - the `nugget` recipes
  - The `status` subcommand (add if missing - grep the current SKILL.md for existing `status` mentions and lift them)
  - "Evaluate Search Quality" - the eval harness section. The eval ties to search, not curate.
- Shared setup sections that both SKILL.md files need: "Prerequisites" (GEMINI_API_KEY not required for concept search; VOYAGE_API_KEY required for vector), "Configuration" (minimal copy - just enough to point at the corpus), "Output Structure" (so the reader knows what the corpus looks like on disk).
- Include the install-procedure pointer ("For user-level install, see CLAUDE.md / INSTALL.md") so users who install the search skill globally can find setup guidance.
- **Ambiguous phrases mapping:** "summarize this video", "watch this for me", "is this worth watching", "any YouTube URL + question", "what should I watch" all describe query intent ("I want to know about this video") and belong in `video-intel-search`'s description. If the video is not indexed yet, the skill's body instructs Claude to tell the user to run `scan` or `process --file` from the plugin repo. This keeps the search skill's surface intuitive without forcing users to know whether a video is indexed before asking.

**Patterns to follow:**
- `skills/translate-bcs/SKILL.md` description shape: explicit trigger phrases, clear "use when" framing, "not for" exclusions pointing at the curate skill for curate intents.
- `skills/video-intel/SKILL.md` body structure for subcommand documentation (bash-command blocks, Options lists, Patterns/caveats prose).

**Test scenarios:**
- The skill-selector routing behavior itself is not unit-testable (SC-1 matrix is manual). But the disjointness invariant between the two descriptions IS testable. Create `tests/test_skill_descriptions.py` with:
  - Happy path: parse both SKILL.md files' YAML frontmatter; assert R1 trigger phrases (canonical list pulled from a fixture) appear in `skills/video-intel-search/SKILL.md` description and NONE appear in `skills/video-intel/SKILL.md` description.
  - Happy path: the inverse - R2 curate trigger phrases appear in `skills/video-intel/SKILL.md` and NONE appear in `skills/video-intel-search/SKILL.md`.
  - Edge case: ambiguous phrases ("summarize this video", "is this worth watching", "any YouTube URL + question", "watch this for me") appear in `skills/video-intel-search/SKILL.md` with a pointer to the curate skill for "not indexed yet" cases. See Approach for the mapping rationale.

**Verification:**
- File exists and parses as valid markdown with a valid YAML frontmatter block.
- Description contains every R1 trigger phrase verbatim.
- Description does NOT contain any of the R2 curate trigger phrases (scan, index, process, dedupe, mindmap, transcript, concepts, taxonomy-build).
- `pytest tests/test_skill_descriptions.py -q` green.
- Linter: any repo-configured markdown linter passes on the new file.

- [ ] **Unit 4: Trim `skills/video-intel/SKILL.md` to curate-only**

**Goal:** Existing SKILL.md's description and body are narrowed to curate intent. Query/nugget/status triggers removed. Cross-reference the new search skill for read intent.

**Requirements:** R2

**Dependencies:** None (can land parallel with Unit 3). Sequencing note: Units 3 and 4 must both land in the same PR per SC-6 to avoid a transient state where neither skill triggers for query.

**Files:**
- Modify: `skills/video-intel/SKILL.md` (description lines 3-29; body lines 198-547 selectively)

**Approach:**
- Description: rewrite to the R2 trigger set only (scan, transcribe, mindmap, concepts, taxonomy-build, process, index, dedupe). Explicit "not for" pointer: "For search, nugget, and status queries, use the `video-intel-search` skill."
- Body: remove sections now owned by `video-intel-search`:
  - "Natural-Language Content Queries"
  - "Hybrid search (evidence queries)"
  - "Synthesize a consultant-grade nugget brief (cross-creator)"
  - Status / search rows from the "Interpreting User Intent" table
  - "Evaluate Search Quality"
- Retain all curate-oriented sections (scan, transcribe, process, mindmap, dedupe, concepts extraction side, taxonomy-build, configuration, channel management).
- Update "What This Skill Does" narrowing funnel: now three layers that are all curate (scan -> transcript -> concepts extraction), no query layer.
- Update the cross-references to ADR-0013 / ADR-0016 / ADR-0017 - keep the pointers that apply to indexing; the query-side ADRs move to the new skill.

**Patterns to follow:**
- Keep the existing tone and section ordering for the curate sections - surgical deletions, not a rewrite.

**Test scenarios:**
- *(No automated test; see Unit 3 rationale.)*
- Test expectation: none -- SKILL.md is markdown; routing matrix (SC-1) is the manual verification.

**Verification:**
- Description contains every R2 trigger phrase.
- Description does NOT contain any of these query-intent phrases: "search", "find videos", "what do creators say", "nugget brief", "consultant brief", "synthesize insights", "show my channels" (this last one is curate-adjacent but the natural split is "show status" goes to search, "manage channels" stays here - verify during trim), "status", "corpus status".
- Body retains every curate subcommand's Documentation section; no missing subcommands.

- [ ] **Unit 5: Update CLAUDE.md with corpus-discovery section and install procedure**

**Goal:** CLAUDE.md's Architecture section describes the four-step precedence and the user-level config. A new "User-level install" subsection (or separate INSTALL.md) documents the absolute-path `extraKnownMarketplaces` entry, env var, user config, and the note about curate. `.claude-plugin/plugin.json` version bumped in the same PR so installers see a coherent snapshot. A short docstring above `load_config()` captures the rationale for the precedence (in lieu of an ADR - see KD6).

**Requirements:** R12, R13, SC-6.

**Dependencies:** Unit 1 (content describes the just-implemented precedence). Soft dependency on Units 3-4 for accurate skill naming.

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.claude-plugin/plugin.json` (version bump)
- Optionally create: `INSTALL.md` (decision deferred per Open Questions - pick based on content length)

**Approach:**
- CLAUDE.md Architecture section: add a subsection after "Config:" describing the four-step precedence. Short (~15 lines). Cross-reference ADR-0016 as precedent.
- User-level install section (in CLAUDE.md or INSTALL.md per the deferred decision): structured as
  1. What this enables (portable search skill reaches any CWD; curate stays pinned to the plugin repo)
  2. Step 1 - add `extraKnownMarketplaces` to `~/.claude/settings.json` with the absolute path to the plugin repo checkout. OS-matrix table (Windows `C:\\Users\\<you>\\ws\\Skills\\video-intel`, macOS `/Users/<you>/ws/Skills/video-intel`, Linux `/home/<you>/ws/Skills/video-intel`).
  3. Step 2 - choose a corpus pointer method: env var (simplest) OR user config (richer). Show both.
  4. Step 3 - verify with `python scripts/video_intel.py search --help` from any CWD outside the plugin repo.
  5. Note: curate commands (`scan`, `index`, etc.) still require running from inside the plugin repo.
- Update CLAUDE.md Commands section with the new env var name and one example of each override being set.

**Patterns to follow:**
- Existing CLAUDE.md sections' tone (terse, action-oriented).
- ADR-0016 subsection's mention of `vector_db_dir` override as the precedent.

**Test scenarios:**
- Test expectation: none -- CLAUDE.md is documentation. Verification is human read-through and running the install procedure end-to-end on the author's machine before merge.

**Verification:**
- The procedure runs clean start-to-finish on the author's machine (including the OS-appropriate absolute path).
- A non-author reader can follow the procedure without asking questions (dog-food on the Proof review if one happens).

## Output Structure

    config.yaml.example                           # new, tracked (Unit 0)
    config.yaml                                   # existing, now UNTRACKED (Unit 0)

    skills/
    └── video-intel-search/
        └── SKILL.md                              # new (Unit 3)

    tests/
    ├── test_load_config.py                       # new (Unit 1)
    ├── test_curate_guard.py                      # new (Unit 2)
    └── test_skill_descriptions.py                # new (Unit 3 mutual-exclusion test)

Four new files (config.yaml.example, video-intel-search SKILL.md, and the three test files), one untrack (config.yaml), and modifications to existing files.

## System-Wide Impact

- **Interaction graph:** No runtime interaction changes - same script, same subcommands, same artifacts. The change is at config-resolution time and skill-selector time, both of which run once per invocation.
- **Error propagation:** Two new `sys.exit(1)` sites (R5 step 4 terminal error, R6a curate guard). Each has an actionable message. No silent failures introduced.
- **State lifecycle risks:** None. No persisted state changes, no new files on disk beyond the optional user-level `~/.video-intel/config.yaml` (which the user creates, not the script).
- **API surface parity:** The CLI surface of `scripts/video_intel.py` is unchanged. No new subcommands, no new flags, no removed flags.
- **Integration coverage:** Routing behavior between the two skills is not unit-testable (no harness for Claude Code's skill selector). SC-1's 15-phrase matrix is manual verification done in the PR description and during review.
- **Unchanged invariants:**
  - Existing `scan`, `index`, `process`, etc. behavior from the plugin repo is functionally identical to today (SC-3). One new INFO log line per invocation per KD7 is the only observable diff; no change to artifacts, exit codes, or ranked search results.
  - Hybrid search result shape (ranked list of `(video_id, timestamp_seconds, relevance)` tuples) when `VOYAGE_API_KEY` is present is unchanged against the existing golden eval dataset.
  - `translate-bcs` skill unchanged in all respects.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Routing ambiguity: a phrase fires both skills silently | SC-1 15-phrase matrix is manual verification in the PR. Split description scoping (curate-only trigger phrases in video-intel, query-only in video-intel-search, cross-references via "not for"). If drift emerges post-merge, the fix is tightening description wording, not architecture. |
| The plugin-manifest format rejects two skills under `skills/` at a user-level install | The plugin already ships two skills (`video-intel`, `translate-bcs`). Adding a third follows the same path. Verify during Unit 3 implementation by smoke-testing the plugin locally before merge. |
| A stale `VIDEO_INTEL_OUTPUT_DIR` silently redirects a curate operation | Prevented by KD1 (plugin-repo config wins over env var). The author's untracked `config.yaml` (Unit 0) always hits step 1 when present locally; curate operations from the author's checkout are unaffected. |
| After Unit 0 untracks `config.yaml`, a fresh clone / CI environment has no `config.yaml` and curate commands would fail at step 4 error | Expected behavior. Users copy `config.yaml.example` to `config.yaml` and edit, per the existing error message. CI jobs that need to exercise curate paths provide their own test fixture config via the Unit 1 test scaffolding. |
| `load_config()` is called from more places than grep shows (e.g., test helpers) | Grep all usages before editing. Any caller that relied on the hard error when config is missing still gets it (just at step 4 now). The return type remains a dict, so callers reading specific keys are unaffected unless they relied on exhaustive keys (the user-level minimal case returns only `output_dir` [+ optional `vector_db_dir`]). Curate commands' `channels:` access is covered by Unit 2's guard. |
| Install doc runs cleanly on the author's machine but fails on a clean environment | Dog-food by installing on a second checkout of Claude Code (or a fresh profile) before merge. If a second checkout is not available, note the risk in the PR and invite follow-up from other users. |

## Documentation / Operational Notes

- New env var `VIDEO_INTEL_OUTPUT_DIR` joins the existing ones (`GEMINI_API_KEY`, `YOUTUBE_API_KEY`, `VOYAGE_API_KEY`). No runtime dependency on it; it's a fallback.
- User-level `~/.video-intel/config.yaml` is a user-authored file, never auto-created by the script. The script only reads it.
- Monitoring: none. No background jobs, no metrics, no alerts added.
- Rollout: single PR per SC-6. No feature flag. Rollback is `git revert`.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-23-search-skill-portability-requirements.md](../brainstorms/2026-04-23-search-skill-portability-requirements.md)
- `scripts/video_intel.py:48,57-63` - `SKILL_DIR` and `load_config()`
- `scripts/video_intel.py:66-71` - `resolve_output_dir()`
- `scripts/video_intel.py:74-83` - `resolve_vector_db_dir()` (ADR-0016 precedent)
- `scripts/video_intel.py:2066-2070` - existing validation idiom used as a pattern for the curate guard
- `skills/video-intel/SKILL.md` - source of the description to split
- `skills/translate-bcs/SKILL.md` - reference for scoped SKILL.md description shape
- `.claude-plugin/plugin.json` - plugin manifest
- `.claude/settings.json` - current project-scoped registration
- `docs/adr/ADR-0016-vector-db-path-config.md` - precedent for config-override-via-key
- `docs/adr/ADR-0017-kb-layer-strategy.md` - retrieval-quality roadmap context (orthogonal to this PR)
- `CLAUDE.md` - skill-parity rule, surgical-changes rule, existing Architecture section
- `specs/agent-rules.md` - §1 cognitive load, §2 Python/ruff/types, §3 TDD, §7 priority ordering (correctness > cognitive load > coverage)
