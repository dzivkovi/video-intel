"""Unit tests for metric-class contracts (not per-query behavior).

Per-query scoring is tested in `test_search_quality.py`. This module
locks contracts that the parametrized harness can only verify
indirectly — in particular, which metrics are gating vs. informational.
"""

from __future__ import annotations

from .metrics import (
    ChannelCoverageMetric,
    MRRMetric,
    RecallAtKMetric,
    TimestampPrecisionMetric,
)


def test_gating_contract() -> None:
    """Only MRR is non-gating. A stray flip would silently start failing
    tests on an informational signal — guard the contract explicitly."""
    assert RecallAtKMetric(k=10, threshold=0.5).gating is True
    assert ChannelCoverageMetric(min_channels=1, threshold=0.5).gating is True
    assert TimestampPrecisionMetric(tolerance_sec=30, threshold=0.5).gating is True
    assert MRRMetric(threshold=0.25).gating is False
