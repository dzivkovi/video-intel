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
