# Harden transcript salvage against Pro's task-wrapper format

**Issue:** [#45](https://github.com/dzivkovi/video-intel/issues/45)
**Date:** 2026-04-26
**Status:** drafted autonomously on the user's overnight-run instruction.
The issue body already contains a tightly-argued Proposal 1; this doc
ratifies that proposal, names what we are NOT doing and why, and pins the
acceptance criteria to evidence already on disk.

## Problem

`salvage_transcript_sections` (`scripts/video_intel.py:1599`) is the
second-line defense when Gemini returns malformed JSON for the
three-task transcript prompt. It scans for the literal regex
`"transcripts"\s*:\s*\[` (and the same for `screen_content` /
`speakers`), then walks the array balanced-bracket-by-bracket and
recovers as many object entries as it can.

That regex is wrong against one of the malformations Pro produces. Pro
sometimes wraps each section in a `{task, output}` object:

```json
[
  {"task": "transcripts", "output": [...]},
  {"task": "screen_content", "output": [...]},
  {"task": "speakers",       "output": [...]}
]
```

The regex looks for `"transcripts":` but the wrapper says
`"task": "transcripts",` — the comma after the string instead of the
colon makes the match fail. The whole array is invisible to salvage.
Result on the 2026-04-25 robotics raw sidecar: **0** of ~93 speech
entries recovered. The video had to be repaired manually.

A second, lower-impact malformation also appears in the same Pro
responses: a single Cyrillic word (`минерал`, `хронический`, ...) is
injected at a structural position inside JSON, blowing up the full
parse. The per-object salvage already absorbs this with ~1 entry lost
per occurrence, so it is not the urgent gap.

## Evidence (sidecars on disk, verified 2026-04-26)

Rerun against the actual raw responses, no stubbing:

| Sidecar | Failure mode | Salvage today | Manual repair max |
|---|---|---|---|
| `2026-04-16-the-gpt-moment-for-robotics-is-here.transcript.raw.txt` | task-wrapper + Cyrillic | 0 / 0 / 0 | 93 / 14 / 5 |
| `2026-03-09-the-future-of-brain-computer-interfaces.transcript.raw.txt` | simple format + Cyrillic | 405 / 5 / 2 | 406 / 5 / 2 |

Counts are speech / screen / speakers. The BCI gap (405 vs 406) is one
entry lost to the Cyrillic intrusion at the per-object level — the
fail-soft we want.

The robotics gap (0 vs 93) is the bug we are fixing.

## Goal

A single `task-wrapper` malformation must degrade gracefully through
salvage, like every other malformation does today. The end state is
that re-running `transcript --url` on a video that produces the
task-wrapper shape lands a partial transcript instead of a hard error,
and does so without a second Gemini call when one re-upload would
cost extra tokens for nothing.

## Proposal (Proposal 1 from the issue, ratified)

Add a structural normalization step at the **top** of
`salvage_transcript_sections` that detects the wrapper shape and
rewrites the input into the flat envelope the rest of the function
already expects:

```python
def _normalize_task_wrapper(text: str) -> str:
    """If text parses as [{"task": ..., "output": [...]}, ...], rewrite
    to {"transcripts": [...], "screen_content": [...], "speakers": [...]}.
    Otherwise return text unchanged."""
```

Then `salvage_transcript_sections` calls
`text = _normalize_task_wrapper(text)` before the existing regex loop.
Everything downstream is unchanged.

The detection is structural (the parsed top-level value is a list of
dicts whose first dict has a `"task"` key naming a known section), not
heuristic. False positives require Gemini to spontaneously produce
`[{"task": "transcripts", ...}, ...]` for some other reason, which has
not been observed in any other malformation we have seen.

If `_normalize_task_wrapper` itself fails to parse the input, it
returns the input unchanged. The existing regex fallback then runs as
before. Composition is monotone: salvage on a wrapper-free input is
unchanged, salvage on a wrapper input is at least as good as today
(which is zero).

## Decisions

1. **Normalize, do not regex-rewrite.** We could try to substitute
   `"task": "transcripts",\n"output":` → `"transcripts":` with a regex.
   We will not. Parsing the wrapper as JSON and rebuilding the
   envelope by key is one branch with one failure mode; regex
   substitution multiplies edge cases (whitespace variants,
   reordering, embedded quotes in `output` strings).

2. **Apply Cyrillic stripping before parsing the wrapper, but only to
   recover the wrapper's own structure.** The wrapper itself can be
   corrupted by a Cyrillic intrusion at the same time it is wrapped
   (the robotics raw shows both). To make
   `_normalize_task_wrapper` succeed at extracting the `output` arrays,
   we strip Cyrillic tokens from the input *inside that helper* using
   the same two patterns the manual repair script used. The stripped
   text is only used to find the boundaries of each `output` array and
   to rebuild the envelope; the per-entry parser inside salvage still
   sees the original (Cyrillic-stripped) bytes for those bytes, so
   no entries are silently rewritten in a way that could change
   meaning. This is the only place we strip Cyrillic — Issue Open
   Question #2 explicitly rejects a global pre-strip.

   *Why this is not the rejected proposal:* the issue rejects a
   pre-strip applied to *all* salvage inputs because it would risk
   false positives on legitimate verbatim foreign content. The
   normalization helper applies the strip only to its own structural
   work — the existing per-entry parser still reads the raw bytes
   without modification and continues to absorb single-word
   intrusions as one lost entry. The contract from the rest of
   salvage's perspective is unchanged.

3. **No `response_schema` change.** Issue Open Question #2 invites a
   stricter Gemini output mode. That is a larger change with broader
   unknowns (does it raise the failure rate? does it cost more
   tokens? does it work on 2.5 Pro at all?). Out of scope. Recorded
   here as a future option, gated on an A/B against this fix.

4. **Add a `docs/solutions/` entry.** Issue Open Question #4 asks
   whether to capture the empirical observation. Yes — the
   `Pro injects single non-Latin tokens at structural positions, ~1
   in 23 long videos` finding is exactly the kind of fact that gets
   forgotten and rediscovered painfully. Lands as
   `docs/solutions/integration-issues/gemini-pro-task-wrapper-and-cyrillic-intrusions-20260426.md`.

5. **No retry-policy change.** The bounded one retry on salvage
   failure stays as-is. With the wrapper fix, one of the cases that
   used to need a retry will succeed on the first response, but the
   retry path remains as the catch-all.

6. **Skill parity is a no-op for this PR.** The fix lives entirely
   inside `salvage_transcript_sections`. No new CLI flag, no new
   subcommand, no user-visible surface. CLAUDE.md gets an updated
   guardrail entry for the salvage path; SKILL.md gets no change.
   This is documented to defuse a likely-false-positive review hit.

## Acceptance criteria

The criteria from the issue, made concrete:

1. `salvage_transcript_sections` on the robotics raw sidecar returns
   `≥80` speech entries (target: 93).
2. `salvage_transcript_sections` on the BCI raw sidecar returns
   `≥400` speech entries (regression check; today's number is 405).
3. New unit tests in `tests/` covering:
   - synthetic task-wrapper input → normalized
   - synthetic task-wrapper + Cyrillic injection → ≥1 entry recovered
   - non-wrapper input → unchanged behavior
   - malformed wrapper that fails to parse → falls back to existing
     salvage path without raising
4. No regression in existing salvage tests
   (`TestSalvageTranscriptSections`).
5. Local validation passes:
   `ruff format . && ruff check . --fix && pytest -m "not integration" -q`.
6. Gate 1 smoke test: end-to-end recovery counts on the two real
   sidecars match acceptance criterion 1 and 2 when run against the
   actual function in the actual repo.

## Out of scope

- `response_schema` / Pydantic-typed Gemini output (Open Question #2).
- A streaming JSON parser like `ijson` (Open Question #3). The current
  regex-first approach is intentional: it does not require a
  syntactically-recoverable input, only one with the right key and
  bracket layout. A tolerant parser would not have helped on the
  robotics case anyway, because the input is syntactically valid JSON
  in the wrong shape, not malformed JSON in the right shape. Recorded
  here so the next maintainer doesn't re-litigate it.
- Re-running the existing eval. This is a salvage-path correctness
  fix, not a retrieval-quality change.

## Open questions

None blocking implementation. The four questions in the issue are
either answered above (#2, #3, #4) or do not gate the fix (#1: rate is
"once across 23 videos in the YC backfill"; even if it stays at <1%
the cost of the fix is ~10 lines plus tests, which clears any
reasonable cost-benefit threshold).

## Post-review revision (2026-04-26, after `/ce-code-review`)

The first cut of this plan only touched `salvage_transcript_sections`.
The correctness reviewer flagged a P0 in that scope: a *clean*
task-wrapper response (no Cyrillic intrusion to break the parse)
would full-parse successfully, bypass salvage entirely, and reach
`merge_transcript_json` as a list whose `[0]` is `{"task": ...,
"output": [...]}` — a shape `merge_transcript_json` reduces to
`raw_json[0].get("transcripts", [])` = `[]`, producing an empty
transcript file with `transcript_status: "complete"`. Silent
regression once Gemini patches the Cyrillic intrusion but keeps the
wrapper drift.

Fix: extract the wrapper-to-envelope logic into a pure
`_wrapper_to_envelope_dict(parsed)` helper that operates on parsed
Python objects, and call it from both:

1. `try_parse_transcript_json` — after a successful direct or
   isolated parse, normalize a wrapper-shaped result into a flat dict
   so `merge_transcript_json` sees the expected schema.
2. `_normalize_task_wrapper` (text layer) — unchanged contract, now
   delegates to the same helper after parsing.

Same review also caught two P1s, addressed in the same revision:

- The `_KNOWN_TASK_KEYS` filter is enforced at envelope-rebuild time
  (a wrapper with an unknown `task` value returns `None` so callers
  pass the original through, preserving any legacy-salvage chance).
- The `test_malformed_wrapper_falls_through_to_legacy_salvage` test
  was renamed to `test_malformed_wrapper_does_not_raise` and now
  asserts the no-raise contract more sharply (well-shaped result
  with all three sections as lists), which is what the original test
  actually verified.

Acceptance criteria 1 and 2 are unchanged. New criterion 7: the
clean-wrapper case produces a non-empty fused transcript end-to-end.
Verified in `docs/plans/gate1-evidence/issue-45-salvage-smoke.txt`.
