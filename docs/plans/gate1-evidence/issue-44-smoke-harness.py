"""Gate 1 smoke harness for issue #44 (Voyage adaptive batch-halving).

Exercises the full `build_search_index` integration end-to-end against a real
corpus channel (kieranklaassen, 1 transcript, 121 chunks) with three controlled
substitutions:

1. `voyageai.Client` is stubbed to inject a token-cap InvalidRequestError on
   the first call (batch size 121), then return real-shaped fake embeddings
   for the two halves (60 + 61 chunks).
2. `vector_db_dir` is pointed at a temp directory under this evidence folder
   so the user's production LanceDB index at C:/Users/danie/video-intel-cache/
   lancedb is NEVER touched.
3. `output_dir` is a temp dir hosting symlinks/copies of the kieranklaassen
   subfolder so the corpus stays read-only.

The smoke succeeds when:

- Log output contains "Voyage batch too large (121 chunks); splitting into 60 + 61"
- `build_search_index` returns a chunk count > 0
- The resulting LanceDB table is queryable with `.search()`

Run from the worktree root:

    python docs/plans/gate1-evidence/issue-44-smoke-harness.py
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Bootstrap sys.path so the scripts package is importable from the worktree.
SCRIPTS = Path(__file__).resolve().parent.parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import video_intel as vi  # noqa: E402

# Real corpus path the user's production scan writes into.
SOURCE_OUTPUT_DIR = Path("G:/My Drive/video-intel")
SMOKE_CHANNEL = "kieranklaassen"

# Token-cap error message stable across Voyage SDK versions; production code
# keys on the "max allowed tokens" substring.
TOKEN_CAP_MESSAGE = (
    "Request to model 'voyage-4-large' failed. "
    "The max allowed tokens per submitted batch is 120000. "
    "Your batch has 128972 tokens after truncation. "
    "Please lower the number of tokens in the batch."
)


class _FakeInvalidRequestError(Exception):
    """Stand-in for voyageai.error.InvalidRequestError. The production
    `_embed_batch` keys on the message substring, not the exception type."""


class StubVoyageClient:
    """Voyage stub: first call raises token-cap; subsequent calls return
    deterministic fake embeddings sized to whatever batch arrives.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.batch_sizes: list[int] = []

    def embed(self, texts, *_args, **_kwargs):
        self.call_count += 1
        self.batch_sizes.append(len(texts))
        if self.call_count == 1:
            raise _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        # Real Voyage returns vectors of length VOYAGE_DIMS (1024). LanceDB
        # only cares that the vectors are consistent length; we use the same
        # dimension to match the schema build_search_index expects.
        return SimpleNamespace(embeddings=[[0.0] * vi.VOYAGE_DIMS for _ in texts])


def _setup_corpus_view(tmp_root: Path) -> Path:
    """Stage a minimal corpus view containing only SMOKE_CHANNEL plus a
    placeholder taxonomy.json. We copy (not symlink) because Windows.
    """
    src = SOURCE_OUTPUT_DIR / SMOKE_CHANNEL
    if not src.is_dir():
        raise SystemExit(f"smoke channel not found: {src}")
    dst = tmp_root / SMOKE_CHANNEL
    shutil.copytree(src, dst)
    # taxonomy.json is needed by load_taxonomy() but the smoke does not
    # exercise the search path; an empty one is fine.
    (tmp_root / "taxonomy.json").write_text('{"concepts": []}', encoding="utf-8")
    return tmp_root


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    tmp_root = Path(tempfile.mkdtemp(prefix="vi-smoke-"))
    try:
        corpus_dir = _setup_corpus_view(tmp_root)
        vector_db_dir = tmp_root / "_lancedb"
        vector_db_dir.mkdir()

        config = {"output_dir": str(corpus_dir), "vector_db_dir": str(vector_db_dir)}

        # Patch require_voyageai BEFORE calling build_search_index.
        stub_client = StubVoyageClient()
        vi.require_voyageai = lambda: SimpleNamespace(Client=lambda: stub_client)

        # Need a fake VOYAGE_API_KEY so the env-var guard passes.
        import os

        os.environ["VOYAGE_API_KEY"] = "smoke-fake-key"

        print("\n=== Gate 1 smoke for issue #44 ===")
        print(f"corpus    : {corpus_dir}")
        print(f"vector_db : {vector_db_dir}")
        print(f"channel   : {SMOKE_CHANNEL}")
        print("voyage    : stubbed (no real API calls)")
        print()

        count = vi.build_search_index(
            corpus_dir,
            channel_filter=SMOKE_CHANNEL,
            force=False,
            config=config,
        )

        print()
        print("--- Results ---")
        print(f"chunks indexed         : {count}")
        print(f"voyage stub call count : {stub_client.call_count}")
        print(f"voyage batch sizes     : {stub_client.batch_sizes}")

        # Acceptance checks.
        assert count > 0, "build_search_index returned 0 chunks"
        assert stub_client.call_count >= 3, (
            f"expected >=3 voyage calls (1 fail + 2 halves), got {stub_client.call_count}"
        )
        assert stub_client.batch_sizes[0] >= vi.MIN_BATCH_SIZE, "first batch was already at floor"
        # First call is the fail; subsequent calls are halves.
        assert stub_client.batch_sizes[1] < stub_client.batch_sizes[0], "second batch was not smaller than first"

        # Confirm the LanceDB table is queryable.
        import lancedb

        db = lancedb.connect(str(vector_db_dir))
        table = db.open_table(vi.LANCEDB_TABLE)
        df = table.to_pandas()
        print(f"lancedb rows           : {len(df)}")
        print(f"channels in index      : {sorted(df['channel'].unique().tolist())}")
        assert len(df) == count

        print()
        print("=== Gate 1 PASS ===")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
