---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: work/2026-09-05/01-briefings-vs-cliffnotes-template-is-the-asset.md
origin: docs/reading-layer.md
created: 2026-09-05
depth: standard
---

# feat(prompts): a reading layer with two shipped templates and a prompt-directory override

## Summary

The AI Tinkerers pass produced a Cliff-Notes-style summary and a topic digest, both improvised inside one session's subagent prompts, so neither survived the session. This plan ships the reading layer documented in `docs/reading-layer.md`: two plain, versioned default templates (`prompts/topic-digest.md`, `prompts/cliffnotes-distiller.md`) in the public repo, plus a `prompt_dirs:` / `$VIDEO_INTEL_PROMPT_DIR` precedence chain in `resolve_prompt_path` so an operator's sharper template (including a private CliffNotes distiller already living outside this repo) overrides the shipped default without forking the code or hard-wiring a private path in.

## Problem

A document type invented once, in one conversation, is not durable: the value of a summary template is the recipe (fixed sections, a ranking, a "so what" per item), not the words of one run, and that recipe lived only in a session transcript. Separately, `resolve_prompt_path` / `load_prompt` resolved a name to exactly one path, `SKILL_DIR / "prompts" / f"{name}.md"`, so a private, sharpened prompt could only be used by editing the shared repo file (destroying the default for everyone else) or by hard-wiring a private path into the code (breaking a fresh clone, violating no-hidden-dependency).

## Key Decisions

**KD1 - Precedence is config, then env var, then bundled.** `prompt_search_dirs()` returns `[*PROMPT_DIRS, *_env_prompt_dirs(), SKILL_DIR / "prompts"]`. A checkout's `config.yaml` is explicit and reviewable; a shell env var can be stale or inherited, so it must not outrank config - mirrors `load_config()`'s precedence (plugin-local config beats `$VIDEO_INTEL_OUTPUT_DIR`).

**KD2 - Existence is checked only to pick the winner; `load_prompt` keeps the exit.** `resolve_prompt_path` returns the first candidate that exists, or the bundled path unchanged when nothing exists; it never raises. `load_prompt` still checks existence and `sys.exit(1)`s, naming every directory searched. The split exists because `scan --dry-run`'s preflight (issue #169) needs to ask "would this name resolve?" without exiting, and duplicating the search logic there would be the PR #136 checker/writer-path-drift class.

**KD3 - A relative or non-string `prompt_dirs` entry is dropped with a warning, never resolved against the CWD.** A path whose meaning depends on the shell's current folder is worse than no override - follows the degrade-never-abort convention issue #213 set for `channels:`.

**KD4 - A missing override directory is allowed, not an error**, so one shared config can name a folder only some machines have.

**KD5 - `translate_video.py` is untouched.** Operationally separate by standing convention; it gets its own mechanism if it ever needs one.

**KD6 - Three document shapes; the shipped templates are plain, not personalized.** `docs/reading-layer.md` names a briefing (own corpus, time window), a CliffNotes distillation (one long source, timestamped), and a topic digest (many short sources, ranked). Both new prompts are generic, reader-agnostic defaults - a sharpened version belongs in a private `prompt_dirs` folder, since the moat is the pipeline and the profile, not the prompt text.

## File-by-File Changes

- `scripts/video_intel.py` - `PROMPT_DIR_ENV_VAR`, module-level `PROMPT_DIRS`, `_LOGGED_PROMPT_OVERRIDES`; `_USER_CONFIG_SUPPORTED_KEYS` gains `"prompt_dirs"`; new `_coerce_prompt_dirs()`, `_env_prompt_dirs()`, `prompt_search_dirs()`; `load_config()` resets `PROMPT_DIRS = []` every call; `resolve_prompt_path()` searches `prompt_search_dirs()`, logging once per name; `load_prompt()`'s error names every directory searched.
- `prompts/topic-digest.md`, `prompts/cliffnotes-distiller.md` (new v1.0 templates); `docs/reading-layer.md` (new design rationale).
- `config.yaml.example`, `README.md`, `CLAUDE.md` - document `prompt_dirs:`, the precedence chain, and the two new prompt names. Re-run `tests/test_docs_currency.py`'s `LIVING_DOCS` sweep after landing.
- `tests/test_prompt_dirs.py` (new) - contract below.

## Test Contract (`tests/test_prompt_dirs.py`)

- `TestBundledDefaultWhenNothingConfigured` - unconfigured resolution unchanged; no override log fires.
- `TestConfigPromptDirsWin` - a configured dir's same-named file wins over bundled; logs once, not on repeats.
- `TestFallThroughToTheNextDirectory` - a dir lacking the file falls through; a nonexistent dir is not an error.
- `TestEnvVarPromptDirs` - `os.pathsep`-joined paths searched in order; config beats env; env beats bundled.
- `TestMalformedEntriesDegrade` - relative, non-string, wrong-typed entries degrade with a warning, never resolved against the CWD; a bare absolute string counts as one entry.
- `TestLoadPromptAgreesWithTheResolver` - `load_prompt` reads exactly what the resolver names; unknown name exits 1 naming every searched dir.
- `TestPromptSearchDirsOrder` - bundled is always last; unconfigured search is the bundled dir alone.
- `TestLoadConfigPopulatesPromptDirs` - plugin-local and user-level config both populate `PROMPT_DIRS`; absent key clears a prior load's value; env-fallback leaves it empty.

Added after the four-reviewer pass (Codex, guidelines, test analysis, silent-failure hunt):

- `TestADirectoryNamedLikeAPromptIsNotACandidate` - a folder called `<name>.md` inside an override dir is skipped by BOTH the resolver and `load_prompt`, so selection is `is_file()` and the two halves still agree.
- `TestAnUnreadableOverrideDirDegrades` - a `PermissionError` from `is_file()` warns once per directory and falls through instead of escaping the report-only preflight; `validate_channel_knobs` still returns.
- `TestFallingBackToBundledIsNeverSilent` - override dirs configured but the name only exists bundled warns once per name; deleting the override file mid-run switches to bundled WITH that warning; a second overridden name logs its own INFO line; no warning at all when nothing is configured.
- `TestEntryNormalization` - `~` is expanded (deleting `.expanduser()` fails this); a shell-quoted env value resolves and is not called relative; an entry naming a file warns and is dropped; a nonexistent absolute dir is kept; empty and whitespace `os.pathsep` segments are ignored.
- `TestEnvParsingIsMemoized` - a malformed env entry warns once across five resolutions; a changed variable is parsed again.
- `TestUnreadablePromptFileExitsOne` - an unreadable and a non-UTF-8 prompt file each exit 1 with the file named.

## Risks and Deliberate Non-Goals

A typo'd `prompt_dirs` entry degrades to a warning plus the bundled default rather than failing loudly, matching the existing tradeoff for `channels:` and prompt-name typos. A moved or renamed override directory is skipped silently, accepted because erroring would break a shared config on any machine without that folder. Out of scope: a prompt-editing UI or template registry; any change to `briefings` or `nugget`; the private `/community-scan` skill from the decision note, which stays outside this repo since it encodes one operator's sources, login, and rubric.

## Reviewers

- **Single resolver.** `resolve_prompt_path` is the only place a name becomes a path. Re-deriving the bundled path elsewhere, especially inside `scan --dry-run`'s preflight, reopens the PR #136 checker/writer-path-drift class.
- **Checker/writer agreement.** `load_prompt` must read exactly the file `resolve_prompt_path` names, with no independent path logic - `TestLoadPromptAgreesWithTheResolver` proves this by construction.
- **No hidden dependency on a private file.** Only `prompt_dirs:` and `$VIDEO_INTEL_PROMPT_DIR` may introduce a private path; a fresh clone with neither set resolves as before.
- **Precedence is config-then-env-then-bundled, concatenated, never merged or reversed** - review `TestEnvVarPromptDirs` as one class.
- **`PROMPT_DIRS` resets at the top of every `load_config()` call**, or a stale value leaks across loads.
- **`translate_video.py` stays untouched**; wiring it in needs its own justification and smoke test.

Added after the four-reviewer pass:

- **Selection is `is_file()`, never `exists()`.** A directory named `<name>.md` passes an existence check, so the preflight reports the name as resolving and the loader dies on the read - the same checker/writer split one layer down.
- **The resolver never raises.** `is_file()` on an unreadable folder raises `PermissionError`, and this code runs inside `scan --dry-run`, documented as report-only. Catch `OSError`, warn once per directory, continue.
- **Degrade, but never silently.** Malformed entries warn and drop; a missing override directory is allowed. A prompt that falls back to the bundled template while overrides are configured is the case that must NOT be silent - one WARNING per name per process. The INFO memo is keyed on `(name, directory)` and the fallback WARNING has its own memo, so an override-then-bundled switch mid-run is visible.
- **Env parsing is memoized on the raw variable string**, or one malformed entry warns once per prompt lookup (measured: 150+ lines in a scan). Matched surrounding quotes are stripped first - cmd.exe keeps them and the entry was then reported as relative.
- **`load_prompt`'s read is guarded** by `(OSError, UnicodeDecodeError)` and exits 1 like "not found".
- **A/B scorecards run against the bundled prompt or record the override.** `scripts/model_eval.py` never calls `load_config` but loads through the same module-level resolver, so an env override would silently turn a model card into a claim about a private prompt.
