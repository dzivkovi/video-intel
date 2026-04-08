---
title: "Preview model hang on long video translation — switch to GA model"
date: 2026-04-08
category: integration-issues
tags: [gemini, model-selection, translate, preview-vs-ga, long-video, chunking]
components: [scripts/translate_video.py, config.yaml]
severity: high
symptoms:
  - "First chunk of auto-chunked 2h18m video hangs indefinitely on gemini-3.1-pro-preview"
  - "Streaming iterator blocks at gRPC/HTTP layer, never yields first token"
  - "Heartbeat thread logs 'Waiting for Gemini...' for 43+ minutes with no progress"
root_cause: "Working hypothesis: Preview model gemini-3.1-pro-preview is unstable for long video; GA model gemini-2.5-pro resolved the hang. Timestamp drift resolved by clip-relative instructions + stitcher offset math."
---

# Preview Model Hang on Long Video Translation

## Problem

`translate_video.py` defaulted to `gemini-3.1-pro-preview` for BCS subtitle translation of long YouTube videos. When translating a 2h18m video with auto-chunking (first 60min + 4x20min), the first chunk hung indefinitely. Gemini's streaming iterator blocked at the gRPC/HTTP layer and never yielded the first token. The heartbeat thread logged "Waiting for Gemini..." for 43+ minutes.

## What We Tried

### Attempt 1: Timeout wrapper around blocking iterator (REJECTED)

A `_iter_with_timeout` helper using `threading.Thread` + `queue.Queue` to enforce deadlines on Gemini's streaming response iterator. ~40 lines of concurrency code that would have made hangs fail faster but did not address the root cause (unstable Preview model). Rejected as over-engineering that adds cognitive load without fixing the actual problem.

### Attempt 2: Switch to gemini-2.5-flash (REJECTED)

Fast and stable (GA), but translation quality was unacceptable:
- Wrong dialect: ekavica instead of the required ijekavica
- Inconsistent timestamp format: `MM:SS` mixed with `HH:MM:SS`
- Incomplete coverage: first 60-minute chunk only translated 25 minutes of content

## Working Solution

One-line default change in `scripts/translate_video.py` — swap the argparse default model from the unstable Preview model to the GA model with proven translation quality.

```python
# Before (hung for 43+ minutes on a 2h18m video)
parser.add_argument("--model", default="gemini-3.1-pro-preview")

# After (3 min to first chunk, correct dialect, full coverage)
parser.add_argument("--model", default="gemini-2.5-pro")
```

Supporting doc updates in `README.md`, `CLAUDE.md`, and the script's module docstring to reflect the new default.

**Key insight:** The problem was not missing timeout logic — it was using a Preview-tier model for a production translation workflow. Preview models can hang, change behavior without notice, and lack the instruction-following fidelity needed for complex prompts (dialect selection, timestamp formatting, full-coverage chunking). The fix is to pin to a GA model that meets quality requirements.

### Follow-up: Timestamp drift in 2.5-pro (resolved)

Even with `gemini-2.5-pro`, timestamps drifted when chunks were instructed to use absolute offsets ("Timestamps must start at 1h 0m 0s"). Gemini starts correct but can't sustain the math — drifts to `[MM:SS]`, echo patterns (`[18:42:42]`), or nonsensical jumps.

**Solution:** Let Gemini count from `[00:00:00]` (its natural mode) with a clip-relative instruction. The stitcher applies offsets mechanically using start time from the filename (`part-60-80` → +3600s). A chunk-aware classifier distinguishes relative, already-absolute, and implausible timestamps per-line, preventing double-offsetting or garbage propagation.

## Evidence

Empirical comparison on the same 2h18m YouTube video with the same prompt:

| Model | Stability | Time to first chunk | Dialect | Timestamp format | Coverage (60-min chunk) |
|---|---|---|---|---|---|
| `gemini-3.1-pro-preview` | Preview | **HUNG (43+ min)** | N/A | N/A | N/A |
| `gemini-2.5-flash` | GA | ~30s | Ekavica (wrong) | MM:SS mixed with HH:MM:SS | 25/60 min |
| `gemini-2.5-pro` | GA | ~3 min | Ijekavica (correct) | HH:MM:SS (minor drift) | Full 60 min |

`gemini-2.5-pro` is the WMT25 translation quality leader among Gemini models, which aligns with the empirical results showing it is the only model that follows the complex multi-constraint translation prompt faithfully.

## Prevention Strategies

### Model Selection: Preview vs GA, Flash vs Pro

- **Never default to Preview models in tools intended for repeated use.** Preview models exist for evaluation, not production. They may be withdrawn, throttled, or behaviorally unstable without notice. The default model string in any script should always be a GA release.
- **Treat the model default as a reliability contract.** The hardcoded default is what runs when the user does not think about models. It must be the most boring, stable option available. Users who want to experiment can pass `--model` explicitly.
- **Use Flash models only for tasks with simple, structured output.** Flash models are optimized for speed and cost on straightforward extraction, classification, and short-form generation. They are not suitable for tasks requiring sustained adherence to complex prompt instructions (multi-step translation, dialect fidelity, timestamp preservation across long documents).
- **Pro models are the correct tier when the prompt has multiple interacting constraints.** Translation that must simultaneously maintain BCS dialect, preserve timestamps, and respect subtitle formatting is a complex-instruction task. Pro GA is the right default.
- **When a new model generation ships, wait for GA before updating defaults.** Pin to the current GA, note the new preview in a comment or issue, and only bump the default after GA promotion and local validation.

### Test Before You Engineer

- **When something breaks, the first response should be to change one variable, not to add code.** Before writing a timeout wrapper, retry decorator, or any other compensating mechanism, ask: "Can I fix this by changing a configuration value?" Model name, temperature, max tokens, and API endpoint are all zero-code changes that should be tested first.
- **Rank hypotheses by simplicity and test in that order.** For a hanging API call, the hypothesis list is: (1) wrong model, (2) request too large, (3) API bug, (4) network issue. A model swap takes 10 seconds to test. A timeout wrapper takes an hour to write and debug. Always test the 10-second hypothesis first.
- **Workaround code is tech debt from the moment it is written.** A timeout wrapper around an API call that should not need one is a sign you are compensating for the wrong root cause. Fix the cause; do not insulate against the symptom.

### Testing Protocol for Model Changes

Before changing the model for any command, validate along these independent dimensions:

1. **Completion:** Does the model finish the request without hanging, timing out, or truncating? Test on the longest realistic input.
2. **Instruction adherence:** Does the output follow the prompt structure? Timestamps present, format correct, speaker labels preserved?
3. **Quality / domain fidelity:** For translation, is the dialect correct? For mind maps, are technical terms accurate? Human spot-check on at least 3 representative segments (beginning, middle, end).
4. **Coverage:** Compare subtitle blocks against video duration at an expected density. Missing blocks mean the model truncated or skipped sections.
5. **Latency and cost:** Measure wall-clock time and token counts. This is the last dimension to evaluate, not the first.

## Related Documentation

- `work/2026-04-08/01-stitch-cleanup-pattern.md` — Chunking workflow implementation (default 20m, timestamp normalization, stitcher)
- `work/2026-04-08/02-test-run-timeout-terminal-next.md` — Test run context and expected behavior per chunk
- `docs/adr/ADR-0003-single-model-replaces-pipeline.md` — Prior model selection reasoning for the main pipeline
- `docs/adr/ADR-0001-gemini-as-multimodal-proxy.md` — Architectural context: Gemini as multimodal proxy
- `work/2026-04-02/01-gemini-api-cost-audit.md` — API cost context ($17 for 340 videos)
- Commit `2d16784` — clip-relative timestamps, chunk-aware stitcher, model default change
