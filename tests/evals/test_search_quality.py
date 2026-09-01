"""Pytest harness that runs hybrid_search against the golden dataset.

This is the eval gate: each golden query's dimensions become metrics, each
metric has a threshold, and pytest fails a case when any metric falls below
its threshold. Use `pytest tests/evals/ -v` to run.

Smoke mode: set VIDEO_INTEL_EVAL_SMOKE=1 to run only Q01 (useful when
debugging the harness itself without burning Voyage API calls for 25 queries).

Stage-1 query expansion toggle: set VIDEO_INTEL_EVAL_EXPAND=0 to run the
baseline (no taxonomy expansion). Default behaviour is expansion ON.
Per-query expansion decisions are written to
`tests/evals/results/<run_tag>-expansion.jsonl` regardless of toggle, so the
two files can be diffed in the PR description. See
`docs/plans/2026-04-20-feat-kb-stage1-query-expansion-plan.md`.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

import video_intel as vi  # scripts/ is on sys.path via pyproject.toml pythonpath

from ._helpers import build_test_case
from .instrument import (
    GOLDEN_PATH,
    HARNESS_DEDUP_BY_VIDEO,
    harness_limit,
    timestamp_precision_ceiling,
)
from .metrics import (
    ChannelCoverageMetric,
    MRRMetric,
    RecallAtKMetric,
    TimestampPrecisionMetric,
)

# --- load dataset once at module import so parametrize can see it -----------
_data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
_all_queries: list[dict[str, Any]] = _data["queries"]

if os.environ.get("VIDEO_INTEL_EVAL_SMOKE"):
    _all_queries = _all_queries[:1]

_EXPAND_ENABLED = os.environ.get("VIDEO_INTEL_EVAL_EXPAND", "1") != "0"
_RESULTS_DIR = Path(__file__).parent / "results"


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


@pytest.fixture(scope="session")
def expansion_log_path() -> Path:
    """Compute run tag, truncate the JSONL file once per session, return the path.

    Tag shape: `YYYY-MM-DD-stage1` when expansion is on, `YYYY-MM-DD-baseline`
    when off. Running both back-to-back produces two siblings the PR can diff.
    """
    date = datetime.now(UTC).date().isoformat()
    tag = f"{date}-stage1" if _EXPAND_ENABLED else f"{date}-baseline"
    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _RESULTS_DIR / f"{tag}-expansion.jsonl"
    # Truncate at session start so re-runs do not double-append
    path.write_text("", encoding="utf-8")
    return path


@pytest.mark.parametrize("gold", _all_queries, ids=lambda g: g["id"])
def test_retrieval_quality(
    gold: dict[str, Any],
    search_context: tuple[Path, dict],
    expansion_log_path: Path,
) -> None:
    output_dir, config = search_context

    # Use the most-demanding k across the query's dimensions so we have enough
    # candidates for every metric (recall@15 needs 15 results; smaller k metrics
    # will only look at the prefix they need). Shared with the measurability
    # audit's ceiling math so the two cannot drift.
    limit = harness_limit(gold)

    hits, diagnostics = vi.hybrid_search(
        output_dir,
        gold["query"],
        limit=limit,
        config=config,
        expand=_EXPAND_ENABLED,
        return_diagnostics=True,
        dedup_by_video=HARNESS_DEDUP_BY_VIDEO,
    )

    # Append the per-query expansion record to the JSONL run log. This is the
    # authoritative audit trail independent of logging configuration.
    with expansion_log_path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "query_id": gold["id"],
                    "query_type": gold["query_type"],
                    "query": gold["query"],
                    "expand_enabled": diagnostics["expand_enabled"],
                    "expanded_query": diagnostics["expanded_query"],
                    "matches": diagnostics["matches"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    test_case = build_test_case(gold, hits)
    metrics = _build_metrics(gold)

    failures = []
    scores = {}
    for metric in metrics:
        metric.measure(test_case)
        scores[metric.__name__] = (metric.score, metric.success, metric.reason)
        if not metric.success and metric.gating:
            # Non-gating metrics (e.g. MRR) are informational only — their
            # score prints in the diagnostic report but never fails the test.
            failures.append(f"{metric.__name__}: {metric.reason} (score={metric.score:.3f})")

    # Always print the full per-metric report for visibility
    header = f"\n[{gold['id']} / {gold['query_type']}] {gold['query'][:80]}"
    print(header)
    n_videos = len({v for v in test_case.additional_metadata["retrieved_video_ids"]})
    print(
        f"  ..    {len(hits)} chunks across {n_videos} videos "
        f"(dedup_by_video={HARNESS_DEDUP_BY_VIDEO}); "
        f"timestamp_precision ceiling {timestamp_precision_ceiling(gold, dedup_by_video=HARNESS_DEDUP_BY_VIDEO):.3f}"
    )
    for name, (score, success, reason) in scores.items():
        flag = "PASS" if success else "FAIL"
        print(f"  {flag}  {name:<35} score={score:.3f}  {reason}")

    if failures:
        pytest.fail(f"{gold['id']}: {len(failures)} gating metric(s) failed:\n" + "\n".join(failures))
