"""Pytest harness that runs hybrid_search against the golden dataset.

This is the eval gate: each golden query's dimensions become metrics, each
metric has a threshold, and pytest fails a case when any metric falls below
its threshold. Use `pytest tests/evals/ -v` to run.

Smoke mode: set VIDEO_INTEL_EVAL_SMOKE=1 to run only Q01 (useful when
debugging the harness itself without burning Voyage API calls for 25 queries).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

import video_intel as vi  # scripts/ is on sys.path via pyproject.toml pythonpath

from ._helpers import build_test_case
from .metrics import (
    ChannelCoverageMetric,
    MRRMetric,
    RecallAtKMetric,
    TimestampPrecisionMetric,
)

# --- load dataset once at module import so parametrize can see it -----------
GOLDEN_PATH = Path(__file__).parent / "golden_dataset.yaml"
_data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
_all_queries: list[dict[str, Any]] = _data["queries"]

if os.environ.get("VIDEO_INTEL_EVAL_SMOKE"):
    _all_queries = _all_queries[:1]


def _resolve_output_dir() -> tuple[Path, dict]:
    config = vi.load_config()
    return vi.resolve_output_dir(config), config


def _build_metrics(gold: dict[str, Any]) -> list:
    """Translate a gold entry's `dimensions` block into DeepEval metric instances."""
    dims = gold["dimensions"]
    metrics = []
    if "recall_at_k" in dims:
        d = dims["recall_at_k"]
        metrics.append(RecallAtKMetric(k=d["k"], threshold=d["threshold"]))
    if "channel_coverage" in dims:
        d = dims["channel_coverage"]
        metrics.append(ChannelCoverageMetric(min_channels=d["min_channels"], threshold=d["threshold"]))
    if "timestamp_precision" in dims:
        d = dims["timestamp_precision"]
        metrics.append(TimestampPrecisionMetric(tolerance_sec=d["tolerance_sec"], threshold=d["threshold"]))
    # Always add MRR as a secondary signal (non-gating)
    metrics.append(MRRMetric(threshold=0.25))
    return metrics


@pytest.fixture(scope="session")
def search_context() -> tuple[Path, dict]:
    return _resolve_output_dir()


@pytest.mark.parametrize("gold", _all_queries, ids=lambda g: g["id"])
def test_retrieval_quality(gold: dict[str, Any], search_context: tuple[Path, dict]) -> None:
    output_dir, config = search_context

    # Use the most-demanding k across the query's dimensions so we have enough
    # candidates for every metric (recall@15 needs 15 results; smaller k metrics
    # will only look at the prefix they need).
    limit = max(
        [gold["dimensions"].get("recall_at_k", {}).get("k", 10), 10],
        default=10,
    )

    hits = vi.hybrid_search(output_dir, gold["query"], limit=limit, config=config)

    test_case = build_test_case(gold, hits)
    metrics = _build_metrics(gold)

    failures = []
    scores = {}
    for metric in metrics:
        metric.measure(test_case)
        scores[metric.__name__] = (metric.score, metric.success, metric.reason)
        if not metric.success and metric.__name__ != "MRR":
            # MRR is informational (non-gating); all others are gates
            failures.append(f"{metric.__name__}: {metric.reason} (score={metric.score:.3f})")

    # Always print the full per-metric report for visibility
    header = f"\n[{gold['id']} / {gold['query_type']}] {gold['query'][:80]}"
    print(header)
    for name, (score, success, reason) in scores.items():
        flag = "PASS" if success else "FAIL"
        print(f"  {flag}  {name:<35} score={score:.3f}  {reason}")

    if failures:
        pytest.fail(f"{gold['id']}: {len(failures)} gating metric(s) failed:\n" + "\n".join(failures))
