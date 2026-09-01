"""Pytest conftest for the golden-dataset retrieval evaluation.

`build_test_case` lives in _helpers.py (conftest can't be imported cross-package
by pytest). This file only holds fixtures the test modules depend on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from .instrument import IndexView

GOLDEN_PATH = Path(__file__).parent / "golden_dataset.yaml"


@pytest.fixture(scope="session")
def golden_queries() -> list[dict[str, Any]]:
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    return data["queries"]


@pytest.fixture(scope="session")
def index_view() -> IndexView:
    """Project the live LanceDB index down to what the measurability audit needs.

    Read-only and free: one column scan, no embeddings and no Voyage call. The
    audit needs the real index because a golden expectation can only be judged
    unreachable against the corpus that actually exists (issue #190).
    """
    import video_intel as vi  # scripts/ is on sys.path via pyproject.toml pythonpath

    lancedb = vi.require_lancedb()
    config = vi.load_config()
    output_dir = vi.resolve_output_dir(config)
    db = lancedb.connect(str(vi.resolve_vector_db_dir(config, output_dir)))
    if vi.LANCEDB_TABLE not in db.list_tables().tables:
        pytest.skip("no LanceDB index on disk; run `video_intel.py index` first")

    table = db.open_table(vi.LANCEDB_TABLE)
    expected_rows = table.count_rows()
    arrow = table.search().select(["video_id", "timestamp_seconds", "channel"]).limit(0).to_arrow()
    # `limit(0)` means "no limit" on lancedb 0.30.2 and returns the whole table
    # (verified: 80,297 rows == count_rows()). If a future version reinterprets
    # it as a page size, a partial projection would make the audit report
    # healthy videos as "not in the index" - a false UNMEASURABLE verdict, the
    # worst outcome this feature has. Fail loudly instead of quietly lying.
    if arrow.num_rows != expected_rows:
        pytest.fail(
            f"index projection returned {arrow.num_rows} of {expected_rows} rows; "
            "the measurability audit cannot run on a truncated view of the index"
        )

    by_video: dict[str, list[int]] = {}
    channels: set[str] = set()
    for vid, secs, channel in zip(
        arrow.column("video_id").to_pylist(),
        arrow.column("timestamp_seconds").to_pylist(),
        arrow.column("channel").to_pylist(),
        strict=True,
    ):
        by_video.setdefault(vid, []).append(int(secs or 0))
        if channel:
            channels.add(channel)
    # A projected-but-empty channel set is a real fact (channel data lost), not
    # the same as "no projection supplied" - IndexView keeps them apart, so pass
    # the frozenset unconditionally rather than defaulting to None.
    return IndexView(chunk_seconds_by_video=by_video, channels=frozenset(channels))
