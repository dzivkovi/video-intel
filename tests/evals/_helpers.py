"""Helpers for the retrieval eval harness.

Separated from conftest.py because conftest modules cannot be imported
cross-package by pytest — only auto-discovered.
"""

from __future__ import annotations

from typing import Any

from deepeval.test_case import LLMTestCase


def build_test_case(gold: dict[str, Any], hits: list[dict]) -> LLMTestCase:
    """Convert a gold entry + hybrid_search hits into a DeepEval LLMTestCase.

    The `additional_metadata` field is our side-channel for retrieval-level
    data that doesn't fit DeepEval's LLM-output-centric model.
    """
    actual_output_lines = [
        f"- [{h['channel']}] {h.get('title', '')} @ {h.get('timestamp', '?')} (score={h['relevance']:.3f})"
        for h in hits
    ]

    return LLMTestCase(
        input=gold["query"],
        actual_output="\n".join(actual_output_lines) or "(no results)",
        expected_output=gold["known_good_answer"],
        retrieval_context=[h.get("text", "")[:500] for h in hits],
        additional_metadata={
            "query_id": gold["id"],
            "query_type": gold["query_type"],
            "retrieved_video_ids": [h.get("video_id", "") for h in hits],
            "retrieved_channels": [h.get("channel", "") for h in hits],
            "retrieved_timestamps": [h.get("timestamp_seconds", 0) for h in hits],
            "expected_hits": gold["expected_hits"],
        },
    )
