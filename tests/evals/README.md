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
| `test_search_quality.py` | Parametrized pytest harness; one test per query. Gating metrics fail the test; `MRR` is informational. |

## Baseline

As of 2026-04-19: **1 of 25 queries passes.** Primary failure mode is
vocabulary mismatch — see ADR-0017 for diagnosis and the staged-KB-layer
plan that re-runs this harness at each stage.
