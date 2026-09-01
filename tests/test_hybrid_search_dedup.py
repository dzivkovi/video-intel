"""Contract for `_select_hits_by_video` and the `dedup_by_video` switch (issue #190).

The load-bearing property is that `dedup_by_video` changes HOW MANY chunks per
video come back and nothing else: the video set and the video order are the
same in both modes. That is what lets the retrieval eval run un-deduped and
still measure the videos the product surface would have shown.

`test_dedup_true_matches_an_independently_written_pre_190_algorithm` derives the
expected answer from a separate implementation of the old code rather than from
the new one, so the two can actually disagree. A test that fed the new
function's own output back as the expectation would agree by construction — the
PR #136 failure class this repo has been burned by.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pandas as pd
import pytest

import video_intel as vi


def _hit(video_id: str, relevance: float, seconds: int, *, source_file: str = "", channel: str = "ch") -> dict:
    return {
        "text": f"{video_id}@{seconds}",
        "timestamp": f"{seconds // 60:02d}:{seconds % 60:02d}",
        "timestamp_seconds": seconds,
        "video_id": video_id,
        "channel": channel,
        "title": f"title-{video_id}",
        "published": "2026-01-01",
        "source_file": source_file,
        "concept_ids": "[]",
        "relevance": relevance,
    }


def _pre_190_dedup(hits: list[dict], limit: int) -> list[dict]:
    """The algorithm as it stood before issue #190, transcribed independently."""
    best_per_video: dict[str, dict] = {}
    for hit in hits:
        vid = hit.get("video_id", "")
        if not vid:
            vid = hit.get("source_file", "")
        score = hit["relevance"]
        if vid not in best_per_video or score > best_per_video[vid]["relevance"]:
            best_per_video[vid] = hit
    deduped = sorted(best_per_video.values(), key=lambda h: h["relevance"], reverse=True)
    return deduped[:limit]


def _video_order(hits: list[dict]) -> list[str]:
    seen: list[str] = []
    for h in hits:
        vid = h.get("video_id", "") or h.get("source_file", "")
        if vid not in seen:
            seen.append(vid)
    return seen


# ---------------------------------------------------------------------------
# The selector
# ---------------------------------------------------------------------------


class TestSelectorPreservesTheVideoFrontier:
    def test_dedup_true_matches_an_independently_written_pre_190_algorithm(self) -> None:
        rng = random.Random(190)
        for _ in range(200):
            hits = [
                _hit(f"v{rng.randrange(6)}", round(rng.uniform(0.0, 1.0), 3), rng.randrange(0, 3600))
                for _ in range(rng.randrange(1, 25))
            ]
            limit = rng.randrange(1, 8)
            assert vi._select_hits_by_video(hits, limit, dedup=True) == _pre_190_dedup(hits, limit)

    def test_both_modes_select_the_same_videos_in_the_same_order(self) -> None:
        rng = random.Random(1901)
        for _ in range(200):
            hits = [
                _hit(f"v{rng.randrange(6)}", round(rng.uniform(0.0, 1.0), 3), rng.randrange(0, 3600))
                for _ in range(rng.randrange(1, 25))
            ]
            limit = rng.randrange(1, 8)
            deduped = vi._select_hits_by_video(hits, limit, dedup=True)
            expanded = vi._select_hits_by_video(hits, limit, dedup=False)
            assert _video_order(expanded) == _video_order(deduped)

    def test_unsorted_input_rows_do_not_change_the_video_frontier(self) -> None:
        """The selector must not lean on LanceDB returning rows pre-sorted."""
        hits = [
            _hit("v1", 0.20, 10),
            _hit("v2", 0.90, 20),
            _hit("v1", 0.95, 30),
            _hit("v3", 0.50, 40),
        ]
        shuffled = [hits[3], hits[0], hits[2], hits[1]]
        for mode in (True, False):
            assert _video_order(vi._select_hits_by_video(hits, 3, dedup=mode)) == ["v1", "v2", "v3"]
            assert _video_order(vi._select_hits_by_video(shuffled, 3, dedup=mode)) == ["v1", "v2", "v3"]

    def test_equal_score_ties_break_on_first_appearance_deterministically(self) -> None:
        hits = [_hit("v1", 0.5, 10), _hit("v2", 0.5, 20), _hit("v3", 0.5, 30)]
        assert _video_order(vi._select_hits_by_video(hits, 3, dedup=True)) == ["v1", "v2", "v3"]
        assert _video_order(vi._select_hits_by_video(hits, 3, dedup=False)) == ["v1", "v2", "v3"]

    def test_source_file_is_the_fallback_identity_when_video_id_is_blank(self) -> None:
        hits = [
            _hit("", 0.9, 10, source_file="a.md"),
            _hit("", 0.4, 20, source_file="a.md"),
            _hit("", 0.6, 30, source_file="b.md"),
        ]
        deduped = vi._select_hits_by_video(hits, 5, dedup=True)
        assert [h["source_file"] for h in deduped] == ["a.md", "b.md"]
        expanded = vi._select_hits_by_video(hits, 5, dedup=False)
        assert len(expanded) == 3


class TestDedupFalseExposesEveryWindow:
    def test_returns_all_chunks_of_the_selected_videos_grouped_best_first(self) -> None:
        hits = [
            _hit("v1", 0.90, 100),
            _hit("v1", 0.40, 500),
            _hit("v1", 0.70, 900),
            _hit("v2", 0.80, 200),
            _hit("v3", 0.10, 300),
        ]
        expanded = vi._select_hits_by_video(hits, 2, dedup=False)
        # v3 is outside the top-2 video frontier and must not appear.
        assert [(h["video_id"], h["relevance"]) for h in expanded] == [
            ("v1", 0.90),
            ("v1", 0.70),
            ("v1", 0.40),
            ("v2", 0.80),
        ]

    def test_this_is_what_lifts_the_multi_window_cap(self) -> None:
        """The defect in one assertion: three windows in one video, dedup keeps one."""
        hits = [_hit("v1", 0.9, 100), _hit("v1", 0.8, 600), _hit("v1", 0.7, 1200)]
        assert len(vi._select_hits_by_video(hits, 10, dedup=True)) == 1
        assert len(vi._select_hits_by_video(hits, 10, dedup=False)) == 3

    def test_dedup_true_is_still_one_chunk_per_video(self) -> None:
        hits = [_hit("v1", 0.9, 100), _hit("v1", 0.8, 600), _hit("v2", 0.7, 1200)]
        deduped = vi._select_hits_by_video(hits, 10, dedup=True)
        assert [h["video_id"] for h in deduped] == ["v1", "v2"]

    def test_limit_still_counts_videos_not_chunks_in_either_mode(self) -> None:
        hits = [_hit("v1", 0.9, 1), _hit("v1", 0.8, 2), _hit("v2", 0.7, 3), _hit("v3", 0.6, 4)]
        assert len(_video_order(vi._select_hits_by_video(hits, 2, dedup=True))) == 2
        assert len(_video_order(vi._select_hits_by_video(hits, 2, dedup=False))) == 2


# ---------------------------------------------------------------------------
# The switch, through the real hybrid_search
# ---------------------------------------------------------------------------


@pytest.fixture
def lancedb_rows(monkeypatch):
    """Stub only the two network boundaries (LanceDB + Voyage), return the rows knob."""
    rows: list[dict] = []

    class _Builder:
        def vector(self, _v):
            return self

        def text(self, _q):
            return self

        def limit(self, _n):
            return self

        def where(self, _c):
            return self

        def to_pandas(self):
            return pd.DataFrame(rows)

    class _Table:
        def search(self, **_kw):
            return _Builder()

    class _DB:
        def list_tables(self):
            return SimpleNamespace(tables=[vi.LANCEDB_TABLE])

        def open_table(self, _n):
            return _Table()

    monkeypatch.setattr(vi, "require_lancedb", lambda: SimpleNamespace(connect=lambda *_a, **_k: _DB()))
    monkeypatch.setattr(
        vi,
        "require_voyageai",
        lambda: SimpleNamespace(
            Client=lambda: SimpleNamespace(embed=lambda *_a, **_k: SimpleNamespace(embeddings=[[0.0] * 8]))
        ),
    )
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")
    return rows


def _row(video_id: str, score: float, seconds: int) -> dict:
    return {
        "text": f"{video_id}@{seconds}",
        "timestamp": "00:01",
        "timestamp_seconds": seconds,
        "video_id": video_id,
        "channel": "ch",
        "title": "t",
        "published": "2026-01-01",
        "source_file": "f.md",
        "concept_ids": "[]",
        "_relevance_score": score,
    }


class TestHybridSearchSwitch:
    def test_default_is_dedup_by_video_true(self, lancedb_rows, tmp_path) -> None:
        lancedb_rows.extend([_row("v1", 0.9, 10), _row("v1", 0.8, 600), _row("v2", 0.7, 20)])
        hits = vi.hybrid_search(tmp_path, "q", config={}, expand=False)
        assert [h["video_id"] for h in hits] == ["v1", "v2"]

    def test_dedup_false_returns_every_window_of_the_same_videos(self, lancedb_rows, tmp_path) -> None:
        lancedb_rows.extend([_row("v1", 0.9, 10), _row("v1", 0.8, 600), _row("v2", 0.7, 20)])
        hits = vi.hybrid_search(tmp_path, "q", config={}, expand=False, dedup_by_video=False)
        assert [(h["video_id"], h["timestamp_seconds"]) for h in hits] == [
            ("v1", 10),
            ("v1", 600),
            ("v2", 20),
        ]

    def test_the_two_modes_agree_on_the_video_set_through_the_real_function(self, lancedb_rows, tmp_path) -> None:
        lancedb_rows.extend([_row("v1", 0.9, 10), _row("v3", 0.2, 5), _row("v1", 0.85, 600), _row("v2", 0.7, 20)])
        deduped = vi.hybrid_search(tmp_path, "q", config={}, expand=False, limit=2)
        expanded = vi.hybrid_search(tmp_path, "q", config={}, expand=False, limit=2, dedup_by_video=False)
        assert _video_order(deduped) == _video_order(expanded) == ["v1", "v2"]


class TestProductionCallSitesKeepDedup:
    """`search --vector` prints one line per video and `nugget` bills Gemini per
    chunk, so both are length-sensitive. Neither may pass dedup_by_video=False,
    and flipping the default would silently change both."""

    def test_default_in_the_signature_is_true(self) -> None:
        import inspect

        sig = inspect.signature(vi.hybrid_search)
        assert sig.parameters["dedup_by_video"].default is True

    def test_no_production_call_site_disables_dedup(self) -> None:
        import ast
        import inspect

        source = inspect.getsource(vi)
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "hybrid_search":
                continue
            for kw in node.keywords:
                if kw.arg == "dedup_by_video":
                    offenders.append(ast.dump(kw))
        assert not offenders, (
            "a production call site passes dedup_by_video explicitly; "
            f"the eval harness is the only intended non-default caller: {offenders}"
        )
