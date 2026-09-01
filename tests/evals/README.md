# Retrieval Eval Harness

Grounded-golden-dataset evaluation of `video_intel search --vector` (hybrid
BM25 + vector + RRF). Run as pytest.

For the full explainer — why DeepEval, what the 25-query dataset covers,
how to interpret the 1/25 baseline, and how to add queries — see
[`docs/testing.md`](../../docs/testing.md). For the staged KB-layer
strategy this eval gates, see
[ADR-0017](../../docs/adr/ADR-0017-kb-layer-strategy.md).

## Quick Start

```bash
# one-time
pip install deepeval
export VOYAGE_API_KEY=...
export GEMINI_API_KEY=...          # reserved for future G-Eval metrics
python scripts/video_intel.py index   # if LanceDB index doesn't exist

# full eval, ~1 min and a few cents of Voyage tokens
pytest tests/evals/ -v -s

# smoke (Q01 only, ~3s, for iterating on the harness)
VIDEO_INTEL_EVAL_SMOKE=1 pytest tests/evals/ -v -s
```

The `-s` flag matters — the harness prints per-metric diagnostics that
pytest otherwise hides.

## Privacy: DeepEval telemetry is opted out

`tests/evals/__init__.py` sets `DEEPEVAL_TELEMETRY_OPT_OUT=YES` before
any submodule triggers the `deepeval` import. Without that, DeepEval's
telemetry layer would ship each `metric.measure()` call's metric name
plus the developer's public IP and an anonymous unique ID to PostHog
and a Sentry heartbeat — on the order of 100 outbound events per
`pytest tests/evals/` run. The opt-out is load-bearing; do not remove
it. `.gitignore` also excludes the `.deepeval*` caches that DeepEval
writes to the repo root.

## Files

| File | Role |
| ---- | ---- |
| `golden_dataset.yaml` | 25 grounded queries, 90 timestamped expected hits across 7 channels. **Frozen contract** — edits require ADR-grade justification per ADR-0017. |
| `metrics.py` | Four DeepEval `BaseMetric` subclasses: `RecallAtKMetric`, `MRRMetric` (non-gating), `ChannelCoverageMetric`, `TimestampPrecisionMetric`. All deterministic, no LLM judge. |
| `_helpers.py` | `build_test_case()` — converts a gold entry + hybrid_search hits into a DeepEval `LLMTestCase`. Kept separate from `conftest.py` because pytest can't import conftest cross-package. |
| `conftest.py` | Fixture loading the YAML once at session scope. |
| `test_search_quality.py` | Parametrized pytest harness; one test per query. Gating metrics fail the test; `MRR` is informational. Runs `hybrid_search(dedup_by_video=False)` so multi-window expectations are reachable — same videos, all their windows. |
| `instrument.py` | Ceiling math for the measurability audit: what each gating metric can achieve at best, ignoring retrieval quality. Pure functions, no deepeval and no network. |
| `test_instrument.py` | One test per query: fails when a gating threshold is mechanically unreachable. A failure here means the RULER is broken, not the retriever. |

## Baseline

As of 2026-09-01 (post-#190, 2,360 videos / 80,297 chunks): **1 of 25 queries
passes** (Q11). The same corpus scored 0/25 immediately before, with the
instrument defect below still in place. Primary failure mode is now recall —
19 of 25 queries retrieve none of their expected videos, and only 1 of the 22
distinct golden videos is missing from the index, so this is genuine retrieval
failure rather than corpus coverage.

**Do not compare this number to the 2026-04-19 1/25.** Different corpus,
different passing query, and a different instrument. See
[`docs/testing.md`](../../docs/testing.md) → "Is the ruler intact?".

## Run the measurability audit first

```bash
pytest tests/evals/test_instrument.py -v      # free: no Voyage call
```

`test_instrument.py` asks whether each golden query's gating thresholds are
reachable **at all** given the harness configuration and the index on disk. A
failure there is a broken ruler mark, not a retrieval result, and any N/25 read
without checking it can silently contain instrument defects — which is exactly
what happened between 2026-04-19 and 2026-09-01 (issue #190).
