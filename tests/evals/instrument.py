"""Measurability audit for the golden dataset — is the ruler itself intact?

Issue #190: the retrieval eval reported N/25 for over a year without anyone
being able to tell a RETRIEVAL failure from an INSTRUMENT failure. Two defects
were hiding inside that one number:

1. `hybrid_search` returned one chunk per video, so a golden query expecting
   several timestamp windows inside a single video was capped at
   (distinct videos / expected hits) on `timestamp_precision` — below its own
   threshold for 5 of the 25 queries, which therefore could never pass.
2. A golden `video_id` left the corpus (creator re-upload), making one query's
   `recall_at_k` threshold unreachable against any index.

Neither is a retrieval result, but both scored exactly like one. The functions
here compute what each gating metric can achieve AT BEST given the harness
configuration and the index actually on disk, so the harness can report a
mechanically-unreachable threshold as a broken ruler mark instead of silently
folding it into the retrieval score.

Everything here is a pure function of its arguments — no network, no Voyage
spend, no LanceDB query. `IndexView` is the small read-only projection the
caller supplies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# scripts/ is on sys.path via pyproject.toml pythonpath. Importing the shared
# stdlib-only helper rather than `.metrics` keeps this audit free of deepeval,
# so a measurability question can be answered without the eval stack.
from timestamp_utils import parse_time_to_seconds as _parse_ts

GOLDEN_PATH = Path(__file__).parent / "golden_dataset.yaml"

# The harness reads every retrieved window inside a selected video, not just
# each video's single best chunk (issue #190). The video SET and ORDER are
# identical either way - `dedup_by_video` only decides how many chunks per video
# come back - so this measures the same videos the product surface would have
# shown, with the multi-window expectations in golden_dataset.yaml actually
# reachable. Product callers keep the `dedup_by_video=True` default.
HARNESS_DEDUP_BY_VIDEO = False

# Both constants live HERE, in the deepeval-free module, so `test_instrument.py`
# can audit measurability without importing the eval stack it is auditing.


def harness_limit(gold: dict[str, Any]) -> int:
    """How many videos the harness asks `hybrid_search` for, for this query.

    One definition, used by the harness itself AND by the ceiling math, because
    a ceiling computed against a separately re-derived limit is the
    checker-disagrees-with-writer failure class: the audit would be predicting a
    run that never happens.
    """
    k = gold["dimensions"].get("recall_at_k", {}).get("k")
    if isinstance(k, int) and not isinstance(k, bool) and k >= 1:
        return max(k, 10)
    return 10


@dataclass(frozen=True)
class IndexView:
    """What the audit needs to know about the index, and nothing more.

    `chunk_seconds_by_video` maps a video_id to the timestamps of every chunk
    indexed for it. A video absent from the mapping is absent from the index.

    `channels` is tracked separately on purpose: `ChannelCoverageMetric` is
    satisfied by ANY retrieved video from an expected channel, not specifically
    by the expected video, so a dead `video_id` does not by itself make its
    channel unreachable.
    """

    chunk_seconds_by_video: dict[str, list[int]] = field(default_factory=dict)
    channels: frozenset[str] | None = None

    def has_video(self, video_id: str) -> bool:
        return video_id in self.chunk_seconds_by_video

    def has_channel(self, channel: str) -> bool:
        # `None` means the caller supplied no channel projection at all, so no
        # channel judgment is possible and every channel is treated as
        # reachable. An EMPTY frozenset is a different fact - the projection ran
        # and found nothing - and must report every channel unreachable. Folding
        # the two into one falsy test makes real channel-data loss look
        # identical to an absent projection and silently false-passes the audit.
        return self.channels is None or channel in self.channels

    def has_chunk_in_window(self, video_id: str, start: int, end: int) -> bool:
        return any(start <= ts <= end for ts in self.chunk_seconds_by_video.get(video_id, []))


def _max_windows_one_chunk_can_satisfy(windows: list[tuple[int, int]]) -> int:
    """How many of one video's expected windows a SINGLE chunk can satisfy.

    `TimestampPrecisionMetric` lets one retrieved chunk satisfy every expected
    window it falls inside, so overlapping windows are not independent. Q24, for
    example, expects `00:24-00:56` and `00:39-01:11` in the same video: a chunk
    at `00:45` satisfies both. Assuming one window per video would under-report
    the ceiling and could flag a genuinely measurable query as unmeasurable -
    the audit crying wolf, which is the one failure mode it cannot afford.

    This is the classic maximum-overlap sweep: sort the endpoints and take the
    deepest point of coverage.
    """
    if not windows:
        return 0
    events: list[tuple[int, int]] = []
    for start, end in windows:
        events.append((start, 1))
        events.append((end + 1, -1))  # inclusive ranges, so the window ends after `end`
    events.sort()
    depth = best = 0
    for _, delta in events:
        depth += delta
        best = max(best, depth)
    return best


def timestamp_precision_ceiling(gold: dict[str, Any], *, dedup_by_video: bool) -> float:
    """Best achievable `timestamp_precision`, ignoring retrieval quality entirely.

    Without dedup every window is independently reachable, so the ceiling is 1.0.
    With one chunk per video, each video contributes at most the number of its
    own windows that a single well-placed chunk could satisfy at once.
    """
    hits = gold["expected_hits"]
    if not hits:
        return 1.0
    if not dedup_by_video:
        return 1.0

    tolerance = _tolerance_of(gold)
    windows_by_video: dict[str, list[tuple[int, int]]] = {}
    for hit in hits:
        start = _parse_ts(hit["timestamp_range"][0]) - tolerance
        end = _parse_ts(hit["timestamp_range"][1]) + tolerance
        windows_by_video.setdefault(hit["video_id"], []).append((start, end))
    satisfiable = sum(_max_windows_one_chunk_can_satisfy(w) for w in windows_by_video.values())
    return satisfiable / len(hits)


def recall_ceiling(gold: dict[str, Any], index: IndexView) -> float:
    """Best achievable `recall_at_k` given the index AND the query's own `k`.

    `RecallAtKMetric` only inspects the top-`k` VIDEOS, so a query expecting
    more distinct videos than its own `k` is capped at `k / expected` even with
    a perfect index. Ignoring `k` here would report 1.0 and declare measurable a
    query that can never pass - the exact failure this audit exists to catch,
    reproduced inside the audit itself.
    """
    expected = {h["video_id"] for h in gold["expected_hits"]}
    if not expected:
        return 1.0
    reachable = len({v for v in expected if index.has_video(v)})
    k = gold["dimensions"].get("recall_at_k", {}).get("k")
    if isinstance(k, int) and not isinstance(k, bool) and k >= 1:
        reachable = min(reachable, k)
    return reachable / len(expected)


def _tolerance_of(gold: dict[str, Any]) -> int:
    tolerance = gold["dimensions"].get("timestamp_precision", {}).get("tolerance_sec", 0)
    return tolerance if isinstance(tolerance, int) and not isinstance(tolerance, bool) else 0


def unaddressable_hits(gold: dict[str, Any], index: IndexView) -> list[dict[str, Any]]:
    """Expected hits no index lookup could ever satisfy.

    An expected hit is unaddressable when its video is missing from the index,
    or when the video is present but holds no chunk inside the expected window
    (± the query's own tolerance). Either way no ranking change can satisfy it,
    so counting it as a retrieval miss is a lie about the retriever.
    """
    tolerance = _tolerance_of(gold)
    out = []
    for hit in gold["expected_hits"]:
        vid = hit["video_id"]
        if not index.has_video(vid):
            out.append({"hit": hit, "reason": f"video_id {vid} is not in the index"})
            continue
        start = _parse_ts(hit["timestamp_range"][0]) - tolerance
        end = _parse_ts(hit["timestamp_range"][1]) + tolerance
        if not index.has_chunk_in_window(vid, start, end):
            out.append(
                {
                    "hit": hit,
                    "reason": (
                        f"{vid} has no indexed chunk inside "
                        f"{hit['timestamp_range'][0]}-{hit['timestamp_range'][1]} (±{tolerance}s)"
                    ),
                }
            )
    return out


# Every dimension name the harness knows how to build a metric for.
# `position_diversity` is declared in the dataset for a deferred G-Eval metric
# and is deliberately not gated by anything today.
KNOWN_DIMENSIONS = frozenset({"recall_at_k", "channel_coverage", "timestamp_precision", "position_diversity"})


def malformed_dimensions(gold: dict[str, Any]) -> list[str]:
    """Dimension declarations neither the metric builder nor this audit can read.

    `_build_metrics` reads dimensions with `.get`, so a misspelled key silently
    drops a GATING metric - the query then passes on the metrics that remain,
    for a reason invisible in the report. That is the same shape as the defect
    this whole audit exists to catch, one layer up, so it is reported rather
    than tolerated. Out-of-range values get the same treatment: a threshold
    outside [0, 1] or a `k` below 1 cannot describe a real target.
    """
    problems: list[str] = []
    dims = gold["dimensions"]

    for name in sorted(set(dims) - KNOWN_DIMENSIONS):
        problems.append(
            f"unknown dimension {name!r}: no metric is built for it, so its "
            f"threshold is silently ignored by both the harness and this audit"
        )

    for name in sorted(set(dims) & KNOWN_DIMENSIONS):
        spec = dims[name]
        if not isinstance(spec, dict):
            problems.append(f"dimension {name!r} is not a mapping: {spec!r}")
            continue
        threshold = spec.get("threshold")
        if threshold is not None and not (isinstance(threshold, int | float) and 0.0 <= threshold <= 1.0):
            problems.append(f"dimension {name!r} has an out-of-range threshold {threshold!r}; expected 0..1")

    rk = dims.get("recall_at_k")
    if isinstance(rk, dict):
        k = rk.get("k")
        if not (isinstance(k, int) and not isinstance(k, bool) and k >= 1):
            problems.append(f"recall_at_k.k must be an integer >= 1, got {k!r}")

    cc = dims.get("channel_coverage")
    if isinstance(cc, dict):
        mc = cc.get("min_channels")
        if not (isinstance(mc, int) and not isinstance(mc, bool) and mc >= 1):
            problems.append(f"channel_coverage.min_channels must be an integer >= 1, got {mc!r}")

    return problems


def unreachable_thresholds(gold: dict[str, Any], index: IndexView, *, dedup_by_video: bool) -> list[str]:
    """Gating thresholds this query cannot reach no matter how good retrieval is.

    An empty list means every gating threshold is at least theoretically
    achievable, so a failure is a real retrieval result and can be read as one.
    """
    problems: list[str] = malformed_dimensions(gold)
    dims = gold["dimensions"]

    tp = dims.get("timestamp_precision")
    if tp is not None:
        ceiling = timestamp_precision_ceiling(gold, dedup_by_video=dedup_by_video)
        if ceiling < tp["threshold"] - 1e-9:
            problems.append(
                f"timestamp_precision threshold {tp['threshold']} exceeds the "
                f"{ceiling:.3f} ceiling imposed by dedup_by_video={dedup_by_video} "
                f"({len({h['video_id'] for h in gold['expected_hits']})} distinct videos "
                f"across {len(gold['expected_hits'])} expected hits)"
            )
        unaddressable = unaddressable_hits(gold, index)
        addressable = len(gold["expected_hits"]) - len(unaddressable)
        index_ceiling = addressable / len(gold["expected_hits"]) if gold["expected_hits"] else 1.0
        if index_ceiling < tp["threshold"] - 1e-9:
            # Several expected hits often share one cause (a whole video gone),
            # so report each distinct reason once.
            reasons = list(dict.fromkeys(u["reason"] for u in unaddressable))
            problems.append(
                f"timestamp_precision threshold {tp['threshold']} exceeds the "
                f"{index_ceiling:.3f} ceiling imposed by the index: " + "; ".join(reasons)
            )

    rk = dims.get("recall_at_k")
    if rk is not None:
        ceiling = recall_ceiling(gold, index)
        if ceiling < rk["threshold"] - 1e-9:
            missing = sorted(v for v in {h["video_id"] for h in gold["expected_hits"]} if not index.has_video(v))
            expected_videos = len({h["video_id"] for h in gold["expected_hits"]})
            cause = (
                f"missing video_ids: {missing}"
                if missing
                else f"k={rk['k']} is below the {expected_videos} distinct videos this query expects"
            )
            problems.append(f"recall_at_k threshold {rk['threshold']} exceeds the {ceiling:.3f} ceiling; {cause}")

    cc = dims.get("channel_coverage")
    if cc is not None:
        expected_channels = {h["channel"] for h in gold["expected_hits"]}
        reachable = {c for c in expected_channels if index.has_channel(c)}
        # The harness only ever sees `limit` videos, so it cannot show more than
        # `limit` distinct channels however many exist in the index.
        showable = harness_limit(gold)
        attainable = min(len(reachable), showable)
        ceiling = attainable / len(expected_channels) if expected_channels else 1.0
        if ceiling < cc["threshold"] - 1e-9 or attainable < cc["min_channels"]:
            problems.append(
                f"channel_coverage needs {cc['min_channels']} channels at threshold "
                f"{cc['threshold']}, but at most {attainable}/{len(expected_channels)} "
                f"expected channels are attainable ({len(reachable)} reachable in the index, "
                f"at most {showable} videos returned)"
            )

    return problems
