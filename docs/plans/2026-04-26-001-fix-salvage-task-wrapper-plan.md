# Fix: Harden transcript salvage against Pro's task-wrapper format

**Issue:** [#45](https://github.com/dzivkovi/video-intel/issues/45)
**Requirements:** [docs/brainstorms/2026-04-26-salvage-task-wrapper-requirements.md](../brainstorms/2026-04-26-salvage-task-wrapper-requirements.md)
**Status:** in progress

## Goal

Make `salvage_transcript_sections` recover ≥80 speech entries from the
`task-wrapper` malformation, while leaving its behavior on every other
input unchanged.

## Approach

One new private helper, one call-site change, one helper for the
narrowly-scoped Cyrillic strip.

```text
text
 │
 ▼
_normalize_task_wrapper(text)        ← NEW
 ├─ try parse as JSON
 │  ├─ list of {task, output} dicts? → rebuild flat envelope as JSON string
 │  └─ otherwise                     → return text unchanged
 ├─ on parse error: try once after _strip_cyrillic_for_structure(text)
 │  └─ same rebuild branch
 └─ on any other failure              → return text unchanged
 │
 ▼
existing regex+balanced-bracket salvage  ← UNCHANGED
```

`_strip_cyrillic_for_structure` exists only to give the parser a
chance at the wrapper shape. It is not called outside
`_normalize_task_wrapper`. The two regex patterns are the ones the
issue's reference repair script used:

```python
_CYRILLIC_BEFORE_TEXT_KEY = re.compile(r"\s*[Ѐ-ӿ]+\s*(\"text\"\s*:)")
_CYRILLIC_BEFORE_VALUE    = re.compile(r"\s*[Ѐ-ӿ]+\s+(\"[^\"]+\")", re.MULTILINE)
```

(Cyrillic block is `U+0400`-`U+04FF`; the issue used the literal
`Ѐ-ӿ` which is the same range. The escape form is more grep-friendly.)

## Tasks

### 1. RED — failing tests

Add to `tests/test_utils.py` under `TestSalvageTranscriptSections`:

- `test_recovers_from_pro_task_wrapper_format` — synthetic
  `[{"task": "transcripts", "output": [...]}]` input, asserts
  ≥`SALVAGE_MIN_SPEECH_ENTRIES` recovered.
- `test_recovers_from_task_wrapper_with_cyrillic_intrusion` —
  same wrapper plus a `минерал` token before a `"text":` key,
  asserts ≥1 entry recovered (per-entry parser still drops the
  one corrupted entry, by design).
- `test_task_wrapper_recovery_does_not_break_simple_format` —
  classic flat envelope still works (regression guard around the
  new normalization step).
- `test_malformed_wrapper_falls_through_to_legacy_salvage` —
  truncated wrapper that does not full-parse falls back to the
  existing regex salvage and recovers from there (or returns
  empty cleanly, no raise).
- `test_robotics_raw_sidecar_recovers_at_least_80_speech_entries`
  — uses the real raw file from the local corpus as a fixture, gated
  on environment so CI can skip cleanly when the file is absent.
- `test_bci_raw_sidecar_still_recovers_at_least_400_speech_entries`
  — same fixture pattern, regression check.

The two real-fixture tests use `pytest.skip` if the file is
unreadable, so the suite stays green on a fresh clone.

Real fixtures live at:

- `G:\My Drive\video-intel\ycombinator\2026-04-16-the-gpt-moment-for-robotics-is-here.transcript.raw.txt`
- `G:\My Drive\video-intel\ycombinator\2026-03-09-the-future-of-brain-computer-interfaces.transcript.raw.txt`

I will *not* commit these into `tests/fixtures/` because they are
~50KB-100KB each, copyrighted (Y Combinator), and the test value is
the empirical end-to-end check, not a unit-level invariant. The
synthetic tests cover the unit-level invariants. Future contributors
who want to rerun the smoke test are pointed at the issue body which
names the source URLs.

Run `pytest tests/test_utils.py::TestSalvageTranscriptSections -v`
expecting at least the new tests to fail.

### 2. GREEN — implementation

In `scripts/video_intel.py`, just before
`salvage_transcript_sections` (~line 1599):

```python
_KNOWN_TASK_KEYS = ("transcripts", "screen_content", "speakers")
_CYRILLIC_BEFORE_TEXT_KEY = re.compile(r"\s*[Ѐ-ӿ]+\s*(\"text\"\s*:)")
_CYRILLIC_BEFORE_VALUE = re.compile(r"\s*[Ѐ-ӿ]+\s+(\"[^\"]+\")")


def _strip_cyrillic_for_structure(text: str) -> str:
    """Strip Cyrillic-token intrusions that block JSON.loads of a wrapper.

    Scoped helper for `_normalize_task_wrapper` only - the issue rejects a
    global pre-strip because verbatim foreign content can be legitimate.
    """
    fixed, _ = _CYRILLIC_BEFORE_TEXT_KEY.subn(r' \1', text)
    fixed, _ = _CYRILLIC_BEFORE_VALUE.subn(r' "text": \1', fixed)
    return fixed


def _normalize_task_wrapper(text: str) -> str:
    """Detect Pro's `[{"task": ..., "output": [...]}, ...]` malformation
    and rewrite to the flat `{"transcripts": [...], ...}` envelope.

    Returns the input unchanged when the wrapper shape is absent or the
    rebuild fails for any reason. Composition with downstream salvage is
    monotone: wrapper-free inputs are unchanged; wrapper inputs are at
    least as recoverable as today (which is zero).
    """
    for candidate in (text, _strip_cyrillic_for_structure(text)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if not (isinstance(parsed, list) and parsed and isinstance(parsed[0], dict)
                and "task" in parsed[0]):
            return text
        envelope: dict[str, list] = {k: [] for k in _KNOWN_TASK_KEYS}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            task = item.get("task")
            if task in envelope:
                output = item.get("output", [])
                if isinstance(output, list):
                    envelope[task] = output
        return json.dumps(envelope)
    return text
```

Then in `salvage_transcript_sections`:

```python
def salvage_transcript_sections(text: str) -> tuple[dict, str | None]:
    """Try to recover valid JSON arrays for transcripts/screen_content/speakers."""
    text = _normalize_task_wrapper(text)
    result: dict[str, list] = {"transcripts": [], "screen_content": [], "speakers": []}
    ...  # rest unchanged
```

That is the entire diff in `video_intel.py`. ~30 lines including
docstrings.

### 3. Validate

```bash
ruff format . && ruff check . --fix
pytest tests/test_utils.py::TestSalvageTranscriptSections -v
pytest -m "not integration" -q
```

All must pass. The two real-fixture tests will pass on the user's
machine and skip on a fresh clone.

### 4. Documentation

- `CLAUDE.md` — add a short paragraph under "Architecture" describing
  the wrapper malformation, ratify the rule that the existing salvage
  is the right shape, and add a guardrail entry.
- `docs/solutions/integration-issues/gemini-pro-task-wrapper-and-cyrillic-intrusions-20260426.md`
  — the empirical-observation doc per Open Question #4.
- No `SKILL.md` change — the fix is purely internal to a salvage
  helper, no user-visible surface changes.

### 5. Self-review and Gate 1

- `/ce-code-review` on the diff. Address any P1.
- Run the actual `salvage_transcript_sections` on both real raw
  sidecars (Gate 1 smoke test). Show the recovery counts.
- Capture the smoke-test transcript in
  `docs/plans/gate1-evidence/issue-45-salvage-smoke.txt`.

### 6. Commit, push, PR

After Gate 1 passes and only after, commit with conventional message,
push, open PR. Wait for explicit user approval before rebase-merging.

## Risk

Low. The change is additive (one helper) plus one new line at the
top of `salvage_transcript_sections`. Composition is monotone: every
non-wrapper input passes through `_normalize_task_wrapper` unchanged
(both branches of the `for candidate in (text, ...)` loop fall to
`return text`).

The Cyrillic strip is the highest-risk piece because it touches
bytes. It is scoped to `_strip_cyrillic_for_structure` which is only
called from `_normalize_task_wrapper`, and its output is only used to
re-attempt a JSON parse — never to drive entry boundaries inside the
existing salvage. If the wrapper shape is absent, the strip's output
is discarded.
