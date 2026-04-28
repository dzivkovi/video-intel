# Issue #58 Gate 1 evidence — Tucker chunk-3 timestamp normalization

## Context

PR #51 ported `apply_timestamp_offset`'s classifier from `scripts/translate_video.py` into `scripts/video_intel.py:_classify_and_offset_timestamp` for chunked transcripts, but did not port the `normalize_timestamp` helper that runs first as a malformation pre-pass.

A 2026-04-27 live run of `python scripts/video_intel.py transcript --url "https://www.youtube.com/watch?v=sFow6dOMfgQ" --channel tuckercarlson` (Tucker Carlson / Jeffrey Sachs, 2h2m35s, 3 chunks of 50 minutes each) surfaced the regression: chunk 3 produced 89 `Implausible timestamp [100:XX:XX]` warnings, and 49 corrupted timestamps were preserved in the saved transcript.

The malformation: Gemini packed total minutes into the HH field. `[100:08:57]` means 1h48m57s (100 minutes from start = 1h40m chunk start + 8 min, 57 sec). Without normalization the classifier saw 100 hours, fell into the implausible branch, logged a warning, and passed the corruption through.

## Fix

Extract `normalize_timestamp` (and three sibling helpers) from `scripts/translate_video.py` into a new shared module `scripts/timestamp_utils.py`. Call `normalize_timestamp` at the top of `scripts/video_intel.py:_classify_and_offset_timestamp` via a defensive wrap-and-strip pattern:

```python
ts_clean = ts.strip().lstrip("[").rstrip("]")
normalized = normalize_timestamp(f"[{ts_clean}]")
if normalized.startswith("[") and "]" in normalized:
    ts = normalized[1 : normalized.index("]")]
else:
    ts = ts_clean
```

## Gate 1 results

Re-ran the same Tucker URL with `--force` after applying the fix. Same 3-chunk layout, same content.

| Metric | Before fix (2026-04-27 19:14) | After fix (2026-04-27 20:41) |
|---|---:|---:|
| `Implausible timestamp` warnings | 89 | **0** |
| `[100:XX:XX]` lines in transcript | 49 | **0** |
| Chunk 3 timestamps render correctly | no | yes |
| Transcript ends at | corrupted | `[2:02:32]` (matches 2h2m35s duration) |

Sample chunk-3 region from the fixed transcript:

```
[1:58:32] Jeffrey Sachs (Economist): "We have one overwhelming delusion..."

  SCREEN [1:58:30-2:02:19] [other]: Jeffrey Sachs responding.

[2:02:20] Tucker Carlson (Host): "Or ends."
[2:02:21] Jeffrey Sachs (Economist): "Or ends. Exactly. Thank you."
[2:02:24] Tucker Carlson (Host): "Jeffrey Sachs, thank you very much for that."
[2:02:26] Jeffrey Sachs (Economist): "Oh, great to be with you, Tucker. Thanks."

  SCREEN [2:02:29-2:02:32] [text_overlay]: TCN logo and website address.
```

All timestamps in `[H:MM:SS]` normalized form. Identical content shape to chunks 1 and 2.

## Why PR #51's Gate 1 missed this

PR #51's smoke ran on Lex Fridman / Steinberger #491 (3h15m52s, 4 chunks). All four chunks produced clean absolute timestamps. Tucker chunk 3 happened to trip the `[100:XX:XX]` malformation that Lex's chunks did not. **Stochastic Gemini output** — a single Gate 1 video can't characterize the full output-format space.

Mitigation: the new shared module concentrates Gemini-quirk normalization in one place. Future malformations get one fix, not N copies.

## Cost

3 chunks at Gemini 3 Flash. Tokens: ~273k prompt × 2 chunks + ~123k for the shorter chunk 3, ~10-15k output per chunk. Approximately $0.30-$0.50.

## Files

- Run log: [issue-58-tucker-chunk3-FIXED.log](issue-58-tucker-chunk3-FIXED.log)
- Corrupted baseline (gitignored): `work/2026-04-27/07-tucker-chunk3-corruption-baseline.transcript.md`

## Gate 2 (content completeness) — second iteration

After Gate 1 confirmed timestamps were normalized, a semantic-coverage comparison of the new transcript vs YouTube's English SRT surfaced a *separate* failure mode in the same code path: chunk 2 transcribed only 3 minutes of its 50-minute window, missing ~47 minutes of content (Danny Danon UN exchange, Palestinian historical narrative, Trump's military budget, swap lines anecdote).

Root cause: Gemini's dynamic-thinking default stochastically consumed the chunk's output budget. Run-log evidence:

| Run | Chunk 2 candidates (output) | Chunk 2 thoughts (thinking) |
|---|---:|---:|
| Before Gate 1 fix (corrupted) | 15,817 | 0 |
| After Gate 1 fix only (thin) | **1,484** | **15,013** |
| After Gate 1+2 fix (clean) | **7,834** | **0** |

Same prompt, same input, three different outcomes. The fix mirrors translate-bcs's `SRT_DEFAULT_THINKING_BUDGET=128` mitigation but model-aware:

- Gemini 3 Flash Preview: `thinking_level="minimal"` (Flash-exclusive level, lowest available)
- Gemini 3 Pro: `thinking_level="low"`
- Gemini 2.5 Flash: `thinking_budget=0` (disable entirely)
- Gemini 2.5 Pro: `thinking_budget=128` (Pro can't disable; 128 is documented minimum)

Plus a defense-in-depth `_assess_chunk_coverage` sanity check that flags chunks with <50% coverage of their allotted window as `thin`, propagating to `transcript_status: partial`.

### Gate 2 metrics on Tucker re-run (2026-04-27 21:10):

| Metric | Result |
|---|---|
| `[100:XX:XX]` corrupted lines | 0 |
| `Implausible timestamp` warnings | 0 |
| `flagged as thin` warnings | 0 |
| `transcript_status` | ok |
| `transcript_thin_chunks` | 0 |
| Transcript line count | 330 (was 237 in thin run, +39%) |
| Anchor phrases recovered | "Danny Danon" `[54:54]`, "7th century" `[59:00]`, "half a trillion" `[1:26:37]` — all in chunk-2 region |

Run log: [issue-58-tucker-gate2-FIXED.log](issue-58-tucker-gate2-FIXED.log)

### Why this nests under issue #58 instead of being filed separately

Both failure modes manifest in the same chunked-transcript path and share the same conceptual fix: "chunked transcription must constrain Gemini's stochastic output behavior." Splitting into two issues would have created bureaucratic overhead without engineering benefit. The user explicitly asked for one root-cause fix instead of a paper trail of follow-up tickets.
