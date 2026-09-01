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
from typing import Any

# scripts/ is on sys.path via pyproject.toml pythonpath. Importing the shared
# stdlib-only helper rather than `.metrics` keeps this audit free of deepeval,
# so a measurability question can be answered without the eval stack.
from timestamp_utils import parse_time_to_seconds as _parse_ts


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
    channels: frozenset[str] = frozenset()

    def has_video(self, video_id: str) -> bool:
        return video_id in self.chunk_seconds_by_video

    def has_channel(self, channel: str) -> bool:
        # An empty channel set means the caller supplied no channel projection;
        # treat every channel as reachable rather than inventing a failure.
        return not self.channels or channel in self.channels

    def has_chunk_in_window(self, video_id: str, start: int, end: int) -> bool:
        return any(start <= ts <= end for ts in self.chunk_seconds_by_video.get(video_id, []))


def timestamp_precision_ceiling(gold: dict[str, Any], *, dedup_by_video: bool) -> float:
    """Best achievable `timestamp_precision`, ignoring retrieval quality entirely.

    With one chunk per video the ceiling is (distinct expected videos /
    expected hits): a query expecting three windows inside one video can have
    at most one of them satisfied. Without dedup every window is reachable and
    the ceiling is 1.0.
    """
    hits = gold["expected_hits"]
    if not hits:
        return 1.0
    if not dedup_by_video:
        return 1.0
    return len({h["video_id"] for h in hits}) / len(hits)


def recall_ceiling(gold: dict[str, Any], index: IndexView) -> float:
    """Best achievable `recall_at_k` given which expected videos the index holds."""
    expected = {h["video_id"] for h in gold["expected_hits"]}
    if not expected:
        return 1.0
    return len({v for v in expected if index.has_video(v)}) / len(expected)


def unaddressable_hits(gold: dict[str, Any], index: IndexView) -> list[dict[str, Any]]:
    """Expected hits no index lookup could ever satisfy.

    An expected hit is unaddressable when its video is missing from the index,
    or when the video is present but holds no chunk inside the expected window
    (± the query's own tolerance). Either way no ranking change can satisfy it,
    so counting it as a retrieval miss is a lie about the retriever.
    """
    tolerance = gold["dimensions"].get("timestamp_precision", {}).get("tolerance_sec", 0)
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


def unreachable_thresholds(gold: dict[str, Any], index: IndexView, *, dedup_by_video: bool) -> list[str]:
    """Gating thresholds this query cannot reach no matter how good retrieval is.

    An empty list means every gating threshold is at least theoretically
    achievable, so a failure is a real retrieval result and can be read as one.
    """
    problems: list[str] = []
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
            problems.append(
                f"recall_at_k threshold {rk['threshold']} exceeds the {ceiling:.3f} "
                f"ceiling imposed by the index; missing video_ids: {missing}"
            )

    cc = dims.get("channel_coverage")
    if cc is not None:
        expected_channels = {h["channel"] for h in gold["expected_hits"]}
        reachable = {c for c in expected_channels if index.has_channel(c)}
        ceiling = len(reachable) / len(expected_channels) if expected_channels else 1.0
        if ceiling < cc["threshold"] - 1e-9 or len(reachable) < cc["min_channels"]:
            problems.append(
                f"channel_coverage needs {cc['min_channels']} channels at threshold "
                f"{cc['threshold']}, but only {len(reachable)}/{len(expected_channels)} "
                f"expected channels are reachable in the index"
            )

    return problems
