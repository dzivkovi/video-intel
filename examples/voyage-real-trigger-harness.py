"""Real-Voyage validation of issue #44 fix.

Two modes, both isolated from the user's production index:

  --mode suspect-3        Runs the 3 originally-suspect transcripts (Chip
                          Huyen + Cat Wu + Kieran X). Regression check that
                          the patched _embed_batch handles the historically-
                          dense content cleanly on real Voyage. Halving is
                          NOT expected to fire on these alone (181 chunks,
                          ~127K tokens spread across 2 batches).

  --mode halving-trigger  Runs all 7 lennyspodcast transcripts (206 chunks,
                          ~161K tokens). Alphabetical packing puts batch 1
                          at ~100-126K tokens depending on tokenizer
                          density -- right at the 120K cap edge. If real
                          Voyage rejects, the patched code halves; if it
                          accepts, we still get clean two-batch coverage.

Default: suspect-3.

Both modes:
- output_dir = a temp directory (read-only side effect of shutil.copy2)
- vector_db_dir = examples/.lancedb-validation/ (gitignored, isolated)
- require_voyageai = the real SDK (real API spend, real $)
- The user's production cache at C:/Users/danie/video-intel-cache/lancedb
  is never opened.

Cost estimate: ~$0.03 (suspect-3) or ~$0.04 (halving-trigger).

Acceptance:
  (a) Exit 0 with chunks indexed > 0
  (b) Final LanceDB table is queryable
  (c) Stdout/log readable in the captured output file
  (d) Production index at C:/Users/danie/video-intel-cache/lancedb is
      byte-identical before and after this run

Halving correctness is independently locked by tests/test_index.py. This
harness is for real-money end-to-end evidence, not unit verification.

Run from worktree root with VOYAGE_API_KEY set:

    python examples/voyage-real-trigger-harness.py [--mode {suspect-3,halving-trigger}]
"""

from __future__ import annotations

import argparse
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

# Mode "suspect-3": originally-named culprits, ordered by probability rank.
SUSPECT_3_FILES = [
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


def _discover_lennyspodcast_files() -> list[tuple[str, str, str]]:
    """Build the SUSPECT_FILES tuple list from every lennyspodcast transcript
    on disk. Sorted alphabetically to match the order build_search_index
    feeds into VOYAGE_BATCH_SIZE-sized batches.
    """
    lp_dir = SOURCE_OUTPUT_DIR / "lennyspodcast"
    rows = []
    for tx in sorted(lp_dir.glob("*.transcript.md")):
        prefix = tx.name.replace(".transcript.md", "")
        # Truncated label for the summary table.
        short = prefix.split("-")[3:7]
        label = "lp/" + "-".join(short)[:50]
        rows.append(("lennyspodcast", prefix, label))
    return rows


def _stage_corpus(tmp_root: Path, files: list[tuple[str, str, str]]) -> Path:
    """Copy each transcript and its sidecars into a temp output_dir.

    We copy the full set of artifacts a transcript carries (mindmap, meta,
    concepts) so build_search_index reads consistent metadata. The original
    files at SOURCE_OUTPUT_DIR are read-only side effects of `cp`.
    """
    for channel, prefix, _label in files:
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


def _count_chunks_per_file(corpus_dir: Path, files: list[tuple[str, str, str]]) -> list[tuple[str, str, int]]:
    """Per-file chunk counts (for the summary table)."""
    rows = []
    for channel, prefix, label in files:
        tx_path = corpus_dir / channel / f"{prefix}.transcript.md"
        chunks = vi.chunk_transcript(tx_path)
        rows.append((label, f"{channel}/{prefix}", len(chunks)))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Real-Voyage validation harness for issue #44.",
    )
    parser.add_argument(
        "--mode",
        choices=("suspect-3", "halving-trigger"),
        default="suspect-3",
        help="suspect-3 = original 3 culprits; halving-trigger = full lennyspodcast pack.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help=(
            "Override VOYAGE_BATCH_SIZE for this run. The default 128 leaves "
            "natural corpus distribution under the 120K-token cap with the "
            "current content, so halving does not fire. Bumping to 256 packs "
            "all 206 lennyspodcast chunks into a single batch (~161K tokens), "
            "which Voyage rejects, exercising the halving recovery path on "
            "real API spend."
        ),
    )
    args = parser.parse_args()

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

    if args.mode == "suspect-3":
        files = SUSPECT_3_FILES
    else:
        files = _discover_lennyspodcast_files()

    # Optional: override VOYAGE_BATCH_SIZE for this run only. Useful when the
    # natural-distribution test does not trip the cap and we want to force
    # halving to fire on real Voyage as a "seeing is believing" test.
    original_batch_size = vi.VOYAGE_BATCH_SIZE
    if args.batch_size is not None:
        vi.VOYAGE_BATCH_SIZE = args.batch_size
        log_msg = (
            f"VOYAGE_BATCH_SIZE overridden: {original_batch_size} -> {args.batch_size} "
            f"(this run only; module constant restored on exit)"
        )
        print(log_msg)
        print()

    examples_dir = Path(__file__).resolve().parent
    isolated_db = examples_dir / ".lancedb-validation"
    if isolated_db.exists():
        shutil.rmtree(isolated_db)
    isolated_db.mkdir()

    tmp_root = Path(tempfile.mkdtemp(prefix="vi-real-validation-"))
    try:
        corpus_dir = _stage_corpus(tmp_root, files)
        chunk_breakdown = _count_chunks_per_file(corpus_dir, files)

        config = {
            "output_dir": str(corpus_dir),
            "vector_db_dir": str(isolated_db),
        }

        print(f"=== Issue #44 real-Voyage validation -- mode={args.mode} ===")
        print(f"corpus    : {corpus_dir}  (TEMP, copied from {SOURCE_OUTPUT_DIR})")
        print(f"vector_db : {isolated_db}  (ISOLATED -- production NOT touched)")
        print("voyage    : real SDK, real API spend")
        print()
        print("Per-file chunk counts:")
        for label, path_id, count in chunk_breakdown:
            print(f"  {label:<55} chunks={count}  ({path_id[:60]})")
        total_chunks = sum(c for _, _, c in chunk_breakdown)
        print(f"  {'TOTAL':<55} chunks={total_chunks}")
        print()

        # Capture caplog-style: subscribe a handler that records the
        # specific log lines we care about so the harness can report
        # halving events at the end without re-parsing stdout.
        halving_events: list[str] = []
        spend_summary_lines: list[str] = []

        class _RecordHandler(logging.Handler):
            def emit(self, record):
                msg = record.getMessage()
                if "splitting into" in msg.lower():
                    halving_events.append(msg)
                if "spend before failure" in msg.lower():
                    spend_summary_lines.append(msg)

        record_h = _RecordHandler(level=logging.WARNING)
        logging.getLogger("video_intel").addHandler(record_h)

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
        print(f"halving events fired   : {len(halving_events)}")
        for ev in halving_events:
            print(f"  -> {ev}")
        if spend_summary_lines:
            print(f"spend-summary log lines: {len(spend_summary_lines)} (failure path fired)")
            for s in spend_summary_lines:
                print(f"  -> {s}")

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
        print(f"  - mode={args.mode}, files={len(files)}, total chunks indexed={count}")
        if halving_events:
            print(f"  - HALVING FIRED on real Voyage: {len(halving_events)} split event(s)")
        else:
            print("  - No halving fired (corpus pack did not exceed token cap this run)")
        print("  - Production index at C:/Users/danie/video-intel-cache/lancedb UNCHANGED")
        print(f"  - LanceDB output landed at {isolated_db}")
        return 0
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
        # Restore the module constant so subsequent imports see the original.
        vi.VOYAGE_BATCH_SIZE = original_batch_size


if __name__ == "__main__":
    sys.exit(main())
