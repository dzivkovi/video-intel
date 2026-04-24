---
date: 2026-04-23
topic: search-skill-portability
---

# Search Skill Portability - Requirements

## Problem Frame

The video corpus query workflow is trapped inside the plugin repo. Today the user has to open Claude Code with CWD set to `c:\Users\danie\ws\Skills\video-intel` to reach `search`. That is the opposite of how they use the corpus: they spend most working hours in other projects, occasionally want to ask "when did Nate talk about MCP?", and currently can only do that by context-switching back to the plugin repo.

Three compounding reasons it fails today, confirmed in session 2026-04-23:

1. **Skill discovery is project-scoped.** `.claude/settings.json` registers the plugin via `extraKnownMarketplaces` with `"path": "."`. The plugin's skills are therefore only discoverable when CWD equals the plugin repo. Verified: in the session transcript, `translate-bcs` and `video-intel` did not appear in the available-skills list when invoked, and the root cause traced to this project-scoped path.
2. **The single `video-intel` skill bundles incompatible operational profiles.** Curate operations (scan, index, process) write expensive artifacts, require three API keys, and must run in one canonical location. Query operations (search) read local artifacts, need zero or one API key, and make sense from anywhere. Today they share one SKILL.md description, which means installing the skill globally would install the curate surface too -- something the user explicitly does not want, because the write surface makes no sense outside its home repo.
3. **The script has one canonical config, hard-coded to the plugin checkout.** `video_intel.py` resolves `config.yaml` relative to `SKILL_DIR` (`Path(__file__).resolve().parent.parent`, see `scripts/video_intel.py:48,57-58`), which means every invocation from every CWD reads the same `config.yaml`. That is good for discovery (no missing-config error from another CWD) but blocks any portability scenario where the user wants to point at a different `output_dir` without editing the committed `config.yaml`. For a user-installed plugin (cached under `~/.claude/plugins/`), editing the cached file is fragile and gets overwritten on plugin update. This is the real corpus-discovery blocker that env var + user-level config solves.

The user's stated goal (verbatim): "make access to the index search skill portable and we can indexing part which is heavy and slow in this folder/project". Portable read, project-scoped write.

## Requirements

**Skill Surface Split**

- R1. A new skill `skills/video-intel-search/SKILL.md` ships in this plugin. Its description is scoped to pre-built-corpus read intent: searching the corpus by concept, keyword, or semantic similarity; synthesizing cross-creator briefs via `nugget`; reporting corpus status (last scan, video count per channel). Trigger phrases cover English ("search my videos for X", "find videos about Y", "when did speaker say Z", "nugget brief on X", "what do creators say about X", "synthesize insights across creators", "show corpus status", "when was this last scanned"). The split axis is "does this command need `channels:` configured?" - if no, it lives here.
- R2. The existing `skills/video-intel/SKILL.md` description is narrowed to ingest/curate intent only: scan, transcribe, mindmap, concepts, taxonomy-build, process, index, dedupe. Trigger phrases that currently match query intent (e.g. "search", "find", "look up", "nugget brief", "what do creators say", "status") are removed or moved to R1 so the two skills stay routing-disjoint. Curate commands require `channels:` in the resolved config.
- R3. `skills/translate-bcs/SKILL.md` is not modified in this PR. Its description is already scoped correctly and has no overlap with the split.
- R4. Both video-intel skills continue to point at the same shared `scripts/video_intel.py`. No Python script split.

**Corpus Discovery**

- R5. When `video_intel.py` needs to resolve `output_dir`, it consults sources in this order, stopping at the first that yields a value:
  1. Plugin-repo `config.yaml` resolved via `SKILL_DIR` (existing behavior, unchanged)
  2. Environment variable `VIDEO_INTEL_OUTPUT_DIR`
  3. User-level config at `~/.video-intel/config.yaml`
  4. Hard error with an actionable message naming both the env var and the user-config path

  The env var and user config act as overrides for users who want their installed plugin to point at a different corpus without editing the cached plugin files.
- R6. The user-level config at `~/.video-intel/config.yaml` accepts a subset of the plugin's `config.yaml` schema: `output_dir` (required) and `vector_db_dir` (optional). It intentionally does not drive scan behavior (channels, model, prompts). The user-level file exists to point at a corpus from outside the plugin repo.
- R6a. Curate commands (`scan`, `index`, `process`, `concepts`, `taxonomy-build`, `dedupe`, `transcript`, `mindmap`) validate that `channels:` is present in the resolved config at entry. If absent, they exit 1 with an actionable message: "This command requires `channels:` in config.yaml. Run from the plugin repo, or set `VIDEO_INTEL_OUTPUT_DIR` to point at a checkout that has channels configured." Search commands (`search`, `nugget`, `status`) do not require `channels:` and do not run this check.
- R6b. When the user-level config contains keys outside the supported subset (e.g., `channels:`, `model:`, prompts), those keys are silently ignored and the script logs one INFO line naming the ignored keys: "Ignoring unsupported keys in ~/.video-intel/config.yaml: channels, model, ...". Friendly to the copy-paste case, still audible so users who expected a model override notice it did not apply.
- R7. The existing curate workflow in the plugin repo is unchanged. Running `scan`, `index`, `process`, `dedupe`, `concepts`, `taxonomy-build` continues to load the plugin-repo `config.yaml` via `SKILL_DIR` as it always did.

## Success Criteria

1. With the plugin registered at user level (documented install in R13), opening Claude Code in any project folder routes a fixed matrix of phrases to the correct skill. The matrix below is verified manually during implementation and recorded in the PR description. Every row must resolve to the `Expected` value.

   | Phrase | Expected |
   | --- | --- |
   | "search my videos for MCP" | video-intel-search |
   | "find videos about prompt caching" | video-intel-search |
   | "when did Nate talk about LightRAG" | video-intel-search |
   | "what do creators say about agent handoff" | video-intel-search |
   | "nugget brief on tool design" | video-intel-search |
   | "synthesize insights across creators about RAG" | video-intel-search |
   | "show corpus status" | video-intel-search |
   | "when was this last scanned" | video-intel-search |
   | "scan my channels for new videos" | video-intel |
   | "transcribe this video: URL" | video-intel |
   | "process this local MP4 through mindmap and transcript" | video-intel |
   | "rebuild the vector index" | video-intel |
   | "extract concepts from the latest mindmap" | video-intel |
   | "translate this YouTube video to Bosnian" | translate-bcs |
   | "scan nates channel and search for MCP videos" | either skill acceptable if a disambiguation prompt follows; NOT both silently |

   "Dual-routing" (both skills fire without disambiguation) counts as a failure for any row except the last. The last row's acceptable outcome is explicit disambiguation; covering a compound-intent phrase with a clean prompt is a better product than either skill guessing.
2. From a CWD that is not the plugin repo, with `VIDEO_INTEL_OUTPUT_DIR` pointing at the corpus and `VOYAGE_API_KEY` set, `python <path>/video_intel.py search --vector "query"` returns hybrid results without error. Verified by smoke test running the command from `c:\Users\danie\scratch` (or any non-plugin directory).
3. With the plugin-repo `config.yaml` present (normal checkout), every curate command (`scan`, `index`, `process`) resolves to that config regardless of `VIDEO_INTEL_OUTPUT_DIR` value. Verified by unit test that sets the env var to a sentinel path and asserts the loaded config is the plugin-repo one.
3a. A unit test exercises each step of the four-step resolution chain in R5: (a) absent plugin-repo config + env var set resolves `output_dir` from the env var; (b) absent plugin-repo config + absent env var + user config present resolves from the user config; (c) absent plugin-repo config + absent env var + absent user config errors with the message described in Success Criterion 4. This test is the authoritative coverage of the fallback chain itself, complementing the smoke test in criterion 2.
4. Running `search` with no plugin-repo config, no env var, no user config produces an error message that names both `VIDEO_INTEL_OUTPUT_DIR` and `~/.video-intel/config.yaml` and exits non-zero. Verified by unit test.
5. `ruff format . && ruff check . --fix && pytest -m "not integration" -q` passes.
6. CLAUDE.md Commands section is updated with the new env var and user-config lookup. New "User-level install" section or equivalent (see R13) is added or a clear pointer to it.
7. Both SKILL.md files ship in the same PR as the Python changes. No "SKILL.md update will follow" deferral.

**Documentation**

- R12. CLAUDE.md Architecture section gains a short subsection describing the corpus discovery precedence and the user-level config shape.
- R13. A user-level install procedure is documented in CLAUDE.md (or a pointed-to `INSTALL.md`) covering: (a) where to add the absolute-path `extraKnownMarketplaces` entry in the user-level `~/.claude/settings.json`, (b) where to set `VIDEO_INTEL_OUTPUT_DIR`, (c) how to write the optional `~/.video-intel/config.yaml`, and (d) a one-sentence note that curate operations still require running from the plugin repo.

## Scope Boundaries

- User-level install **automation** (a script that writes to `~/.claude/settings.json`) is out of scope. Documentation only in this PR; automation is a follow-up PR if desired.
- `${CLAUDE_SKILL_DIR}` placeholder cleanup in SKILL.md files is out of scope. The convention is kept because AI substitution is reliable in practice; a cosmetic rewrite is deferred.
- Python script split (splitting `video_intel.py` into a search module and a curate module) is out of scope. See Key Decision D2.
- **Graceful Voyage fallback is out of scope and deferred to a follow-up PR.** When `VOYAGE_API_KEY` is unset, `search --vector` continues to exit 1 as it does today. Rationale: doc review (2026-04-23) surfaced that the fallback is a meaningful refactor inside `hybrid_search()` (the vector and BM25 legs are not independently callable in LanceDB's `query_type="hybrid"` path), which does not belong bundled with portability work. Ship portability first; graceful fallback lands in its own PR where the refactor is the whole job.
- Local embedding models (sentence-transformers) or Gemini embeddings as Voyage replacements are out of scope. Future ADR if ever needed.
- New subcommands (`list`, `open`) to make the query skill richer are out of scope. They are worth considering later; today the portable skill fires on the existing `search` surface.
- Changes to `translate-bcs` skill surface, description, or script are out of scope.
- Per-user customization of `model` or prompt overrides in user-level config is out of scope. User config reads `output_dir` and `vector_db_dir` only.
- Backwards-compat naming for `VIDEO_INTEL_OUTPUT_DIR` (e.g. accepting an older name). New variable, no legacy aliases.
- Moving the translate-bcs skill to a global-install posture is out of scope. Its portability story is separate and has not shown friction.

## Key Decisions

- D1. **Two skills, not three.** `translate-bcs` stays as-is. Rationale: its surface is already dialed in and its problem shape is independent of the corpus. Touching it would be churn without user value.
- D2. **SKILL.md split only, shared Python script.** Both the search skill and the curate skill continue to invoke `scripts/video_intel.py`. Rationale: the two skills diverge at the description-routing layer (what intents should fire them) and at the CLI-surface layer (which subcommands they document). Under the hood they use the same config loading, the same meta.json format, the same chunking and rendering utilities. Splitting the script duplicates all of that for zero user-visible benefit and would require a second config loader to stay in sync.
- D3. **Plugin-repo config wins over env var and user config.** Rationale: preserves the existing behavior (one canonical `config.yaml` per plugin checkout). The env var and user config are overrides for users who want to point a user-installed plugin at a non-default corpus, not replacements for a committed `config.yaml`. Reverse ordering (env var first) would create a failure mode where a stale env var silently redirects a plugin-repo `scan` to the wrong corpus. That is a correctness risk (per `specs/agent-rules.md` §7: correctness outranks ergonomics).
- D4. **User-level config is a minimal subset.** Only `output_dir` and `vector_db_dir`. No `channels`, no `model`, no prompt overrides. Rationale: YAGNI. The requirement today is "point at the corpus from anywhere." If per-user model defaults are ever needed, a follow-up adds those keys with their own precedence and migrates users explicitly.

## Alternatives Considered

Two alternative shapes were weighed against the two-skill-plus-shared-script design in D2.

**Alternative A: One SKILL.md + config discovery only.** Keep a single `video-intel` skill with all 30+ trigger phrases. Add the env-var and user-config fallback so search works from any CWD. Rely on trigger-phrase discipline inside the one description and on curate commands failing fast (clear error naming the missing `channels:`) when invoked from a non-plugin CWD. Ships faster; one description to maintain. **Rejected because:** a user-level install would put curate verbs into the globally-available skill surface, where `scan`, `index`, `process`, `dedupe` would trigger on phrases intended for the user's other projects. The failure would be audible (fail-fast with a clear message) but repeated and noisy. The split lets the global install surface `video-intel-search` only, without curate ever appearing as an option. That is the concrete harm avoided.

**Alternative B: Two plugins instead of one.** Split `video-intel-search` and `video-intel-curate` into separate plugin repos, each with its own `plugin.json`. Maximum isolation. **Rejected because:** the two skills share `scripts/video_intel.py`, config parsing, meta.json format, chunking, and rendering utilities. Two plugins means two copies of the script, or a shared library in a third plugin the two depend on. Either way, the maintenance cost outweighs the isolation benefit for a single-user, single-machine tool.

**What the chosen split costs:** two SKILL.md descriptions that must stay mutually exclusive on trigger phrases. The CLAUDE.md skill-parity rule already requires any new CLI subcommand to update the right SKILL.md in the same PR. Extending that rule to "new subcommand picks exactly one skill, never both" is a small per-PR discipline, not a new workflow. See Open Questions for the forcing-function test that locks this.

## Visual Aid - Skill and Config Surface Before vs After

**Today**

```text
skills/
  video-intel/SKILL.md       (mixes scan + search + index + translate verbs)
  translate-bcs/SKILL.md     (translate only, clean)

config resolution: SKILL_DIR/config.yaml (plugin checkout) -> error if missing
query skill reach: only when CWD = plugin repo (skill discovery blocker, not config)
vector search with no Voyage key: hard error
```

**After this PR**

```text
skills/
  video-intel-search/SKILL.md   (search + query verbs only)
  video-intel/SKILL.md          (scan + transcribe + index + process verbs only)
  translate-bcs/SKILL.md        (unchanged)

config resolution: SKILL_DIR/config.yaml -> $VIDEO_INTEL_OUTPUT_DIR
                   -> ~/.video-intel/config.yaml -> error with hint
query skill reach: any CWD, once plugin is user-level registered
vector search with no Voyage key: still exits 1 today (graceful fallback deferred)
```

## Dependencies / Assumptions

- **Verified.** `output_dir` structure (per-channel subdirs, `.lancedb`, `taxonomy.json`, per-video `*.meta.json`, `*.transcript.md`) is stable. Search code reads only these paths; no writes on the query path. Grep-verified against `scripts/video_intel.py` on 2026-04-23.
- **Verified.** `.claude/settings.json`'s `extraKnownMarketplaces` accepts directory sources. Today the plugin uses `"path": "."`, which works for project-scoped install. User-level install needs an absolute path; this is a directory-source permutation already documented in Claude Code plugin docs.
- **Unverified assumption, labeled for planning.** The `plugin.json` manifest format supports declaring multiple skills from one plugin and pointing them at shared `scripts/`. The current repo already ships with two skills in one plugin, so this is the expected case; confirm during planning that no manifest change is required for the split.

## Outstanding Questions

### Resolve Before Planning

_None. All product decisions are locked._

### Deferred to Planning

- `[Affects R5]` `[Technical]` Where does `load_config()` (or its equivalent) live today in `scripts/video_intel.py:57-58`, and what is the minimum refactor to introduce the four-step precedence without leaking the new rule into every subcommand entry point?
- `[Affects R6a]` `[Technical]` The `channels:` presence check in R6a can be implemented either as a guard at each curate command's entry point, or via a shared `load_full_config()` helper called by curate paths (with `load_config()` keeping the minimal schema for search paths). Pick the shape during planning based on how many entry points exist vs how clean a shared helper reads.
- `[Affects R13]` `[Needs research]` Decide the exact doc home for the user-level install procedure: a new section in CLAUDE.md vs a separate `INSTALL.md` pointed to from the README / CLAUDE.md. Pick during planning based on doc length.
- `[Affects R13]` `[Needs research]` The Windows absolute path in `~/.claude/settings.json`'s `extraKnownMarketplaces` entry uses different separators than macOS/Linux. The install doc needs an OS-matrix. Worth naming once during planning so it is not rediscovered at install time.

## References

- `work/2026-04-23/07-skill-portability-test-and-three-way-split-proposal.md` - source thinking, including the original three-way-split proposal (this doc narrows it to two-way)
- `CLAUDE.md` - skill-parity rule (SKILL.md lands in same PR as CLI change); video-id-is-identity rule; probe-before-you-pay
- `specs/agent-rules.md` §1, §6, §7 - cognitive load, verify-don't-assume, stop-and-ask scope and priority ordering
- `docs/adr/ADR-0016-vector-db-path-config.md` - precedent for config override via env and config key (the `vector_db_dir` pattern this PR extends)
- `.claude/settings.json` - current project-scoped plugin registration pattern
- `.claude-plugin/plugin.json` - plugin manifest (shared `scripts/` dir already in place for the existing two-skill layout)
- `skills/video-intel/SKILL.md` - current bundled skill description (to be trimmed per R2)
- `skills/translate-bcs/SKILL.md` - reference for the right grain of SKILL.md description (unchanged per R3)
- `scripts/video_intel.py` - shared script (both SKILL.md files continue to point at it per R4)
- `docs/search-internals.md` - the math and pipeline mechanics of hybrid search; reference only (no changes to hybrid search in this PR)
- `docs/adr/ADR-0017-kb-layer-strategy.md` - current search/retrieval strategy context
- Memory: `project_output_dir_gdrive.md` - current `output_dir` lives on Google Drive, `vector_db_dir` on local NTFS (this PR preserves that separation)

## Next Steps

`Resolve Before Planning` is empty. All deferred items are technical or research questions that belong in `/ce-plan`.

-> `/ce-plan` for structured implementation planning
