---
title: "A defect is a shape, not a traceback line: inventory every site by grep, every entry path by an absurd-value smoke, and guard on the property the next line needs"
date: 2026-10-31
category: workflow-issues
module: video_intel.py
problem_type: runtime_error
component: tooling
symptoms:
  - A config-knob guard covered the hand-counted call sites and missed one inside a ThreadPoolExecutor task, where the exception re-raises through future.result()
  - "A fix hardened two readers of config[\"channels\"] while eleven other readers of the same key still crashed on the same malformed YAML"
  - A configured timeout was ignored by every command except the one that read the knob inline
  - A tightened type allowlist dropped float voice ids and a speaker's name silently fell back to a generic label
  - A stub-level test suite stayed green over a production line that crashed before reaching the hardened code
root_cause: missing_validation
resolution_type: workflow_improvement
severity: high
tags: [fix-scope, call-site-inventory, config-knobs, type-guards, malformed-input, real-caller, one-definition]
---

# A defect is a shape, not a traceback line: inventory every site by grep, every entry path by an absurd-value smoke, and guard on the property the next line needs

## Problem
Fixes scoped to the sites an issue or a traceback named kept leaving live copies of the same defect behind. The defect was a shape (an unguarded key read, a knob read in one place, a type check on the wrong property), and every unexamined instance still crashed or silently degraded.

## Symptoms
- The first pass of the `resolve_mindmap_source` guard (issue #135, PR #167) missed the per-video mindmap closure, the most severe site: it runs as a `ThreadPoolExecutor` task and `future.result()` re-raises with nothing above it, so one config typo took down the channel's mindmap stage after transcript work was paid for.
- Issue #205 hardened two readers of `config["channels"]`; its peer pass confirmed by execution that the other eleven still raised on the same four ordinary YAML mistakes. Issue #213 then routed all of them through `configured_channels`.
- Before issue #248, only `cmd_scan` resolved `transcript_timeout_seconds`, inline. `transcript --url` and `process --url` never read the knob, so a 2-second smoke budget was ignored and real chunks transcribed.
- During the issue #161 review, a revision of `_usable_voice_id` excluded float from its accepted types. A float voice id (hashable, and accepted before the fix) lost its speaker name, which fell back to the `Speaker <id>` label.
- In the first review round of issue #171, 76 stub-level tests passed while the real chunked caller crashed before it reached the hardened merge.

## What Didn't Work
- Hand-counting call sites in a brief, and fixing only the readers the traceback named.
- Treating `x.get(key) or []` as a type guard. It replaces only falsy values: a truthy scalar crashes the next `for` or `len()`, and a bare string iterates character by character and survives by luck, which is why the pattern reads as harmless.
- Unit tests that pass the knob value straight in: they cannot see a caller that never reads it.

## Solution
1. **Inventory by grep, publish the table.** Search for the key or the shape (not the function in the traceback) and put the site list in the PR as a table, so a reviewer can check that no further site exists. An identical site the issue did not name is flagged, never silently widened or skipped.
2. **Sweep every consumer of the hardened object**, diagnostic log lines included. The durable form is one reader ([core: one-definition]): `configured_channels` for the watchlist, `_usable_task_list` for Gemini task values, `resolve_transcript_timeout_seconds` for the hang budget.
3. **Smoke each entry path with an absurd value.** Run every command that makes the call once against the pre-fix code with a value no one would ship (a 2-second timeout). A path that ignores it never reads the knob.
4. **Resolve at a scope that dominates every reader, validate only where it is consumed.** Resolving inside one branch can leave a later reader without a value; validating before branching blocks paths that never read the knob. `mindmap --url` resolves the timeout on the video branch only, so a typo cannot block the uncapped transcript-source mindmap or a skip (issue #249).
5. **Guard on the property the next operation needs.** A dict key must be hashable, so `_is_unhashable_json_scalar` rejects exactly `dict` and `list`. When a guard tightens, run every newly excluded shape against pre-PR output.

## Why This Works
A traceback names one instance; a grep for the shape names all of them, and a published table makes the inventory reviewable. The absurd-value smoke finds the bug where it lives: in which callers read the value. Guarding on the needed property rejects exactly what will crash and nothing that worked before.

## Prevention
- Caller-level tests that drive the real command with only the paid boundary stubbed ([core: real-caller]).
- Drift guards that name readers rather than count them (#213 entry in `.claude/rules/scan-config.md`).
- For any tightened predicate, one regression test per previously accepted shape (`tests/test_merge_malformed_entries.py::TestFloatVoiceIdKeepsItsName`).

## Related Issues
- Issues #135, #161, #168, #171, #205, #213, #248, #249
- `.claude/rules/scan-config.md` (#135 with its #168 amendment, #205, #213 entries)
- `.claude/rules/transcript.md` (malformed-entry bullet for #161/#171, #249 item 3)
- `CLAUDE.md` [core: one-definition], [core: real-caller]
