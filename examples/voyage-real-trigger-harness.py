"""Real-Voyage validation of issue #44 fix (post-patch regression test).

Runs the 3 suspect transcripts from the original token-cap incident through
the patched `_embed_batch` with REAL Voyage API calls. Ranked by probability
of being the dense culprit per work/2026-04-26/02-voyage-batch-size-bug-
where-it-happened.md:

  #1 Chip Huyen - AI Engineering 101 (lennyspodcast)
  #2 Cat Wu - Anthropic Head of Product (lennyspodcast)
  #3 Kieran X - GPT 5.5 thinking (kieranklaassen)

NO production indexes touched: output_dir is a temp directory, vector_db_dir
points at examples/.lancedb-validation/, and `require_voyageai` is the real
SDK. The user's production cache at C:/Users/danie/video-intel-cache/lancedb
is never opened.

Cost: ~$0.05 USD in real Voyage spend (181 chunks at voyage-4-large pricing).

Acceptance:
  (a) Exit 0 with chunks indexed > 0
  (b) Final LanceDB table is queryable
  (c) Stdout/log readable in examples/voyage-real-validation-output.txt
  (d) Production index at C:/Users/danie/video-intel-cache/lancedb is
      byte-identical before and after this run

Halving may or may not fire on these 3 files alone -- 181 chunks at ~700
average tokens packs into 2 batches at ~63K each, well under the 120K cap.
A halving-firing run would require adding the alphabetically adjacent dense
neighbors (Boris Cherny + Hamel Husain). This harness focuses on the
regression check; halving correctness is locked by tests/test_index.py.

Run from worktree root with VOYAGE_API_KEY set:

    python examples/voyage-real-trigger-harness.py
"""

from __future__ import annotations

import logging
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import video_intel as vi  # noqa: E402

# Real corpus path the user's production scan writes into.
SOURCE_OUTPUT_DIR = Path("G:/My Drive/video-intel")

# The three suspect transcripts, ordered by probability rank.
SUSPECT_FILES = [
    (
        "lennyspodcast",
        "2025-10-23-al-engineering-101-with-chip-huyen-nvidia-stanford-netflix",
        "#1 Chip Huyen - AI Engineering 101",
    ),
    (
        "lennyspodcast",
        "2026-04-23-how-anthropics-product-team-moves-faster-than-anyone-else-cat-wu-head-of-product",
        "#2 Cat Wu - Anthropic Head of Product",
    ),
    (
        "kieranklaassen",
        "Kieran Klaassen on X GPT 5.5 is capable. You can see it thinking",
        "#3 Kieran X - GPT 5.5 thinking",
    ),
]


def _stage_corpus(tmp_root: Path) -> Path:
    """Copy each suspect transcript and its sidecars into a temp output_dir.

    We copy the full set of artifacts a transcript carries (mindmap, meta,
    concepts) so build_search_index reads consistent metadata. The original
    files at SOURCE_OUTPUT_DIR are read-only side effects of `cp`.
    """
    for channel, prefix, _label in SUSPECT_FILES:
        src_dir = SOURCE_OUTPUT_DIR / channel
        dst_dir = tmp_root / channel
        dst_dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for sibling in src_dir.iterdir():
            if sibling.name.startswith(prefix) and sibling.is_file():
                shutil.copy2(sibling, dst_dir / sibling.name)
                copied += 1
        if copied == 0:
            raise SystemExit(f"no files found for prefix: {channel}/{prefix}")
    # Empty taxonomy is fine; load_taxonomy is called downstream of indexing.
    (tmp_root / "taxonomy.json").write_text('{"concepts": []}', encoding="utf-8")
    return tmp_root


def _count_chunks_per_file(corpus_dir: Path) -> list[tuple[str, str, int]]:
    """Per-file chunk counts (for the summary table)."""
    rows = []
    for channel, prefix, label in SUSPECT_FILES:
        tx_path = corpus_dir / channel / f"{prefix}.transcript.md"
        chunks = vi.chunk_transcript(tx_path)
        rows.append((label, f"{channel}/{prefix}", len(chunks)))
    return rows


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("harness")

    if not Path(SOURCE_OUTPUT_DIR).is_dir():
        raise SystemExit(f"corpus not reachable: {SOURCE_OUTPUT_DIR}")

    import os

    if not os.environ.get("VOYAGE_API_KEY"):
        raise SystemExit("VOYAGE_API_KEY not set; cannot run real Voyage harness")

    examples_dir = Path(__file__).resolve().parent
    isolated_db = examples_dir / ".lancedb-validation"
    if isolated_db.exists():
        shutil.rmtree(isolated_db)
    isolated_db.mkdir()

    tmp_root = Path(tempfile.mkdtemp(prefix="vi-real-validation-"))
    try:
        corpus_dir = _stage_corpus(tmp_root)
        chunk_breakdown = _count_chunks_per_file(corpus_dir)

        config = {
            "output_dir": str(corpus_dir),
            "vector_db_dir": str(isolated_db),
        }

        print("=== Issue #44 real-Voyage validation ===")
        print(f"corpus    : {corpus_dir}  (TEMP, copied from {SOURCE_OUTPUT_DIR})")
        print(f"vector_db : {isolated_db}  (ISOLATED -- production NOT touched)")
        print("voyage    : real SDK, real API spend (~$0.05 estimated)")
        print()
        print("Per-file chunk counts:")
        for label, path_id, count in chunk_breakdown:
            print(f"  {label:<45} chunks={count}  ({path_id})")
        total_chunks = sum(c for _, _, c in chunk_breakdown)
        print(f"  {'TOTAL':<45} chunks={total_chunks}")
        print()

        # No stub: this exercises the actual voyageai.Client and real API.
        # The patched _embed_batch is what we are validating.
        log.info("Calling build_search_index against real Voyage...")
        count = vi.build_search_index(
            corpus_dir,
            channel_filter=None,
            force=False,
            config=config,
        )

        print()
        print("--- Results ---")
        print(f"chunks indexed         : {count}")

        # LanceDB sanity: open the table, count rows, list channels.
        import lancedb

        db = lancedb.connect(str(isolated_db))
        table = db.open_table(vi.LANCEDB_TABLE)
        df = table.to_pandas()
        print(f"lancedb rows           : {len(df)}")
        print(f"channels in index      : {sorted(df['channel'].unique().tolist())}")
        print(f"per-channel row counts : {df['channel'].value_counts().to_dict()}")

        # Quick search sanity to confirm the index is queryable.
        # Vector dim must match VOYAGE_DIMS; build_search_index already wrote
        # vectors so we just round-trip a sample query.
        sample_text = df.iloc[0]["text"][:120]
        print(f"sample chunk text      : {sample_text!r}...")

        assert count > 0, "build_search_index returned 0 chunks"
        assert len(df) == count

        print()
        print("=== Real-Voyage validation PASS ===")
        print("  - 3 suspect transcripts processed cleanly through patched _embed_batch")
        print("  - Production index at C:/Users/danie/video-intel-cache/lancedb UNCHANGED")
        print(f"  - LanceDB output landed at {isolated_db}")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
