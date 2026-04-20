"""Custom retrieval metrics for video-intel golden-dataset evaluation.

All metrics are deterministic (no LLM judge) and subclass DeepEval's `BaseMetric`
so they fit `assert_test()` and pytest-deepeval reporting. G-Eval (LLM-judge)
metrics for `position_diversity` and `essay_coverage` come in a later round.

Test-case contract (see conftest.py `build_test_case`):
    test_case.additional_metadata = {
        "retrieved_video_ids": list[str],       # ordered by relevance
        "retrieved_channels":  list[str],       # per-hit channel
        "retrieved_timestamps": list[int],      # timestamp_seconds per hit
        "expected_hits": list[dict],            # from golden_dataset.yaml
    }
"""

from __future__ import annotations

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase


def _parse_ts(ts: str) -> int:
    """Convert '04:56' or '1:23:45' to seconds."""
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Unexpected timestamp format: {ts!r}")


class RecallAtKMetric(BaseMetric):
    """Fraction of expected video_ids that appear in top-k retrieved results.

    Does not require uniqueness — an expected video that doesn't appear drops
    the score; extra retrieved videos beyond the expected set do not penalize.
    """

    gating: bool = True

    def __init__(self, k: int, threshold: float):
        self.k = k
        self.threshold = threshold
        self.score: float = 0.0
        self.success: bool = False
        self.reason: str = ""

    @property
    def __name__(self) -> str:
        return f"RecallAt{self.k}"

    def measure(self, test_case: LLMTestCase) -> float:
        meta = test_case.additional_metadata or {}
        retrieved = meta.get("retrieved_video_ids", [])[: self.k]
        expected = {h["video_id"] for h in meta.get("expected_hits", [])}

        if not expected:
            self.score = 1.0
            self.success = True
            self.reason = "No expected hits — vacuous pass"
            return self.score

        found = sum(1 for v in set(retrieved) if v in expected)
        self.score = found / len(expected)
        self.success = self.score >= self.threshold
        self.reason = f"Found {found}/{len(expected)} expected video_ids in top-{self.k}"
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.success


class MRRMetric(BaseMetric):
    """Mean Reciprocal Rank of the first expected video_id hit.

    Score = 1/rank of the first expected hit (1.0 if at rank 1, 0.5 at rank 2,
    etc.). Score = 0.0 if no expected hit appears in results at all.
    """

    # Non-gating: informational signal only. A query can legitimately pass
    # RecallAtK while MRR looks weak (expected hits present but not at top
    # ranks), and vice versa — so MRR score never fails the test.
    gating: bool = False

    def __init__(self, threshold: float):
        self.threshold = threshold
        self.score: float = 0.0
        self.success: bool = False
        self.reason: str = ""

    @property
    def __name__(self) -> str:
        return "MRR"

    def measure(self, test_case: LLMTestCase) -> float:
        meta = test_case.additional_metadata or {}
        retrieved = meta.get("retrieved_video_ids", [])
        expected = {h["video_id"] for h in meta.get("expected_hits", [])}

        if not expected:
            self.score = 1.0
            self.success = True
            self.reason = "No expected hits — vacuous pass"
            return self.score

        for rank, vid in enumerate(retrieved, start=1):
            if vid in expected:
                self.score = 1.0 / rank
                self.success = self.score >= self.threshold
                self.reason = f"First expected video_id at rank {rank} → 1/{rank} = {self.score:.3f}"
                return self.score

        self.score = 0.0
        self.success = False
        self.reason = f"No expected video_id found in {len(retrieved)} retrieved results"
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.success


class ChannelCoverageMetric(BaseMetric):
    """Fraction of expected channels that appear in retrieved results.

    Cross-channel queries need this: if Q01 expects ramjad + natebjones +
    mark_kashef but only ramjad shows up, recall@k may pass (most expected
    videos found) while channel_coverage fails (diversity missing).
    """

    gating: bool = True

    def __init__(self, min_channels: int, threshold: float):
        self.min_channels = min_channels
        self.threshold = threshold
        self.score: float = 0.0
        self.success: bool = False
        self.reason: str = ""

    @property
    def __name__(self) -> str:
        return f"ChannelCoverage>={self.min_channels}"

    def measure(self, test_case: LLMTestCase) -> float:
        meta = test_case.additional_metadata or {}
        retrieved_channels = set(meta.get("retrieved_channels", []))
        expected_channels = {h["channel"] for h in meta.get("expected_hits", [])}

        if not expected_channels:
            self.score = 1.0
            self.success = True
            self.reason = "No expected channels — vacuous pass"
            return self.score

        overlap = retrieved_channels & expected_channels
        self.score = len(overlap) / len(expected_channels) if expected_channels else 0.0
        # success needs BOTH: threshold-fraction of expected channels AND the absolute min_channels floor
        self.success = self.score >= self.threshold and len(overlap) >= self.min_channels
        self.reason = (
            f"{len(overlap)}/{len(expected_channels)} expected channels found (min required: {self.min_channels})"
        )
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.success


class TimestampPrecisionMetric(BaseMetric):
    """Fraction of expected hits where a retrieved chunk's timestamp falls
    within the expected window (± tolerance_sec).

    A retrieved hit counts if its video_id matches an expected hit AND its
    timestamp_seconds falls within [start - tol, end + tol] of that hit's
    expected range. One retrieved hit can satisfy multiple expected hits
    for the same video if their ranges overlap.
    """

    gating: bool = True

    def __init__(self, tolerance_sec: int, threshold: float):
        self.tolerance = tolerance_sec
        self.threshold = threshold
        self.score: float = 0.0
        self.success: bool = False
        self.reason: str = ""

    @property
    def __name__(self) -> str:
        return f"TimestampPrecision(tol={self.tolerance}s)"

    def measure(self, test_case: LLMTestCase) -> float:
        meta = test_case.additional_metadata or {}
        retrieved_video_ids = meta.get("retrieved_video_ids", [])
        retrieved_timestamps = meta.get("retrieved_timestamps", [])
        expected_hits = meta.get("expected_hits", [])

        if not expected_hits:
            self.score = 1.0
            self.success = True
            self.reason = "No expected hits — vacuous pass"
            return self.score

        # For each expected hit, check if any retrieved chunk for that video lands in range
        satisfied = 0
        for exp in expected_hits:
            exp_vid = exp["video_id"]
            exp_start = _parse_ts(exp["timestamp_range"][0]) - self.tolerance
            exp_end = _parse_ts(exp["timestamp_range"][1]) + self.tolerance
            for ret_vid, ret_ts in zip(retrieved_video_ids, retrieved_timestamps, strict=False):
                if ret_vid == exp_vid and exp_start <= ret_ts <= exp_end:
                    satisfied += 1
                    break  # this expected hit is satisfied; move to the next one

        self.score = satisfied / len(expected_hits)
        self.success = self.score >= self.threshold
        self.reason = f"{satisfied}/{len(expected_hits)} expected hits had a retrieved chunk within ±{self.tolerance}s"
        return self.score

    async def a_measure(self, test_case: LLMTestCase) -> float:
        return self.measure(test_case)

    def is_successful(self) -> bool:
        return self.success
