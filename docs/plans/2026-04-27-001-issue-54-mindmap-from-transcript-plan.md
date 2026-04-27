# Issue #54 — Mindmap-from-transcript implementation plan

**Status:** active session plan, written 2026-04-27 after the empirical pilot
in `docs/plans/gate1-evidence/issue-54-empirical-pilot.md` confirmed quality.
**Issue:** https://github.com/dzivkovi/video-intel/issues/54
**Branch:** `feat/issue-54-mindmap-from-transcript` (worktree at `../video-intel-issue54`)
**Skip /ce-brainstorm:** issue body already encodes user-resolved edge cases.

## Empirical baseline (already established)

`gemini-2.5-flash`, 188 KB transcript (Lex/Steinberger 3h15m, chunked) →
49.2 s wall, 47k input + 5k output tokens, `finish_reason=STOP`, ~$0.01.
Quality matches `mindmap-knowledge.md` rules; visual content from SCREEN
blocks survives. Verdict: ship.

## What changes

### Files added

- `prompts/mindmap-from-transcript.md` — text-only prompt; already drafted and
  validated by the pilot. Ships as production prompt without further iteration.
- Tests covering: source resolver, no-transcript fallback rules, partial-transcript
  inheritance, existing-mindmap skip, scan/process ordering inversion, config
  knob parsing.
- `docs/plans/gate1-evidence/issue-54-empirical-pilot.md` (already written) +
  `docs/plans/gate1-evidence/issue-54-pilot-raw-mindmap.md` (already written) +
  `docs/plans/gate1-evidence/issue-54-cli-smoke.md` (Gate 1 CLI evidence, written
  during implementation).

### Files modified — `scripts/video_intel.py`

1. **`process_mindmap(...)`** — gain `source: Literal['video','transcript'] = 'video'`
   and `transcript_path: Path | None = None` keyword-only parameters. When `source='transcript'`,
   read `transcript_path` (or default `<channel_dir>/<prefix>.transcript.md`),
   call `call_gemini_text` with the new prompt, write `<prefix>.mindmap.md`
   with the same header comment + meta.json updates as the video path.
   On partial-transcript input (transcript_status == "partial" in source meta),
   write a `<!-- source: partial transcript -->` HTML comment after the existing
   `<!-- video: ... -->` header and set `mindmap_source_status: "partial"` in meta.

2. **`call_gemini_text(...)`** — gain optional `response_mime_type` (default kept
   as `"application/json"` for back-compat with the concepts caller). Pass
   `"text/plain"` from the new mindmap-from-transcript path so we get markdown
   back, not JSON-wrapped markdown.

3. **`resolve_mindmap_source(channel_config, *, transcript_available: bool) -> str`**
   — new pure function, returns one of `"video" | "transcript" | "skip"`. Encodes
   the issue's edge-case rules:
   - `mindmap_source: "none"` → `"skip"`.
   - `mindmap_source: "video"` → `"video"`.
   - `mindmap_source: "transcript"` + transcript exists → `"transcript"`.
   - `mindmap_source: "transcript"` + no transcript → raise `ValueError` with
     actionable message (caller logs as ERROR and skips this video, does not abort
     the run).
   - `mindmap_source: "auto"` (default) + transcript exists → `"transcript"`.
   - `mindmap_source: "auto"` + no transcript → `"video"` (legacy path), log INFO
     `"No transcript for {video_id}; mindmap-from-video fallback."`.

4. **`cmd_scan(...)`** — invert the loop order:
   - **OLD:** mindmap loop → transcript loop → concepts loop.
   - **NEW:** transcript loop → mindmap loop (reading from on-disk transcripts where
     resolver picks `"transcript"`) → concepts loop.
   - The mindmap loop now consults `resolve_mindmap_source(...)` per video. The
     legacy `auto_mindmap == "none"` short-circuit is preserved via the resolver's
     `"none"` → `"skip"` mapping (so users with existing notify-only configs are
     unaffected).

5. **`_cmd_process_url(...)`** — invert step order:
   - **OLD:** Step 1 mindmap (video) → Step 2 transcript → Step 3 concepts.
   - **NEW:** Step 1 transcript (chunked when needed, unchanged) → Step 2 mindmap
     (text from on-disk transcript) → Step 3 concepts.
   - The 10800-frame fps fallback in Step 1 (mindmap-from-video) is **deleted**,
     not preserved. Mindmap-from-transcript has no frame cap.
   - When transcript fails AND `mindmap_source` resolves to `"video"` (e.g. user
     explicitly set it), fall back to the legacy mindmap-from-video path. The
     `auto` default never reaches that branch on a fresh URL because transcript
     success is the precondition for the `"transcript"` resolution.

6. **`cmd_process(...)` (the `--file` branch)** — UNCHANGED. The local-file path
   keeps mindmap-from-video because chunking it would multiply the upload cost,
   defeating the "one upload" guarantee. Documented in the existing CLAUDE.md
   guardrail "process --url chunks; process --file does NOT" — this issue extends
   that guardrail with "process --url's mindmap reads on-disk transcript;
   process --file's mindmap watches the upload."

7. **`cmd_mindmap(...)`** — `--url` branch consults `resolve_mindmap_source(...)`
   identically to scan. `--file` branch unchanged (same reasoning as cmd_process).

### Files modified — `config.yaml.example`

- Document `mindmap_source` per-channel, with all four values explained
  (`auto` default, `transcript`, `video`, `none`). Comment block notes the
  interaction with `auto_mindmap` (legacy boolean-style knob, kept working).

### Files modified — `CLAUDE.md`, `skills/video-intel/SKILL.md`, `skills/video-intel-search/SKILL.md`

- CLAUDE.md: update "Architecture" + "Key Design Decisions" sections to reflect
  the inversion. Add a guardrail under "Code Review Guardrails":
  `process --url generates mindmap from on-disk transcript text by default; process --file still watches the upload`.
- SKILL.md (curate): clarify that `process --url` now produces transcript first,
  mindmap from transcript second, concepts third.

## What does NOT change

- `prompts/mindmap-knowledge.md`, `mindmap-light.md`, `mindmap-heavy.md` — kept as-is
  for `mindmap_source: video` and the `auto`-fallback path.
- `prompts/transcript.md` — unchanged. Transcript is the input, not the output here.
- `prompts/concepts.md` — unchanged. Mindmap output shape is identical, so the
  downstream concepts call needs no adjustment.
- `process_concepts` — unchanged.
- `process_transcript` — unchanged in shape; the mindmap-from-transcript path is a
  new consumer of its on-disk artifact.
- Local-file (`--file`) paths in cmd_process and cmd_mindmap — unchanged.
- Search index, eval harness, dedup, prune-shorts, taxonomy-build — unchanged.

## Edge cases covered (from issue body)

| Case | Handling |
|---|---|
| Channel `auto_transcript: none` (notify-only) | No new behavior. Manual `process --url --channel <name>` is the cherry-pick path; the new ordering generates transcript first, then mindmap-from-transcript, then concepts. |
| Video has `skip_modes: ["transcript"]`, `mindmap_source: auto` | Resolver returns `"video"` (no transcript available), logs INFO `"No transcript for ... ; mindmap-from-video fallback."`. |
| Video has `skip_modes: ["transcript"]`, `mindmap_source: transcript` | Resolver raises `ValueError`. Caller logs ERROR with actionable message naming both `skip_modes` and `mindmap_source`. Video is skipped; run continues for other videos. |
| Existing on-disk `<prefix>.mindmap.md` | `process_mindmap` already short-circuits with `"skipped (exists)"` when `not force`. No change needed; price optimization preserved by reusing existing logic. |
| Transcript with `transcript_status: "partial"` | Resolver still returns `"transcript"` (we feed partial anyway). Mindmap output gains `<!-- source: partial transcript -->` header line and meta gains `mindmap_source_status: "partial"`. |

## TDD plan (RED first)

New test file: `tests/test_mindmap_from_transcript.py`. Each test starts as a
failing test against the unchanged code, then passes once the corresponding
implementation lands.

1. `test_resolve_mindmap_source_default_auto_uses_transcript_when_available`
2. `test_resolve_mindmap_source_default_auto_falls_back_to_video_when_no_transcript`
3. `test_resolve_mindmap_source_explicit_video_keeps_video`
4. `test_resolve_mindmap_source_explicit_transcript_with_transcript_returns_transcript`
5. `test_resolve_mindmap_source_explicit_transcript_without_transcript_raises`
6. `test_resolve_mindmap_source_none_returns_skip`
7. `test_process_mindmap_from_transcript_writes_expected_artifacts` — uses a
   fake Gemini client that returns canned markdown; asserts the on-disk
   `<prefix>.mindmap.md` has the `<!-- video: -->` header AND meta.json has
   `prompt: "mindmap-from-transcript"`.
8. `test_process_mindmap_from_transcript_partial_marks_meta_and_header` — fake
   transcript meta has `transcript_status: "partial"`; asserts mindmap meta
   gains `mindmap_source_status: "partial"` and the markdown carries the
   `<!-- source: partial transcript -->` HTML comment.
9. `test_process_mindmap_from_transcript_skips_when_artifact_exists` — pre-create
   the mindmap.md, assert no Gemini call is made.
10. `test_call_gemini_text_text_plain_response_mime` — verify the new optional
    `response_mime_type` parameter actually changes the GenerateContentConfig
    passed to `generate_content`.

Existing tests must continue to pass — no regression. Specifically the local-file
process and cmd_mindmap paths must remain on the video-input behavior.

## GATE 1 plan

After GREEN tests + ruff clean, run a real-Gemini smoke through the new CLI path
(not the pilot script) on a SECOND on-disk transcript that the user has not seen
mindmap output from before. Capture:

- Command line invoked.
- Wall clock + token counts (echo from `log_usage_metadata`).
- Diff between meta.json fields before and after (`mindmap_source` should go
  from absent to `"transcript"`, `prompt` to `"mindmap-from-transcript"`).
- First 60 lines of the resulting mindmap.

Save to `docs/plans/gate1-evidence/issue-54-cli-smoke.md`. PR description links
this evidence file.

## Out of scope

- Translation-from-transcript for `translate_video.py`: file follow-up issue.
- Bulk regeneration of legacy mindmaps under the new architecture: separate
  cleanup PR if user opts in.
- Modifying chunked-transcript "partial" semantics: the chunked path's "some chunks
  failed" shape is treated identically to the single-call "partial" shape for
  this issue's purposes (both surface as `transcript_status` ≠ `"ok"` upstream
  of the mindmap call).
