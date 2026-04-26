"""Compare embeddings: production LanceDB vs halving-trigger test LanceDB.

The production index at C:/Users/danie/video-intel-cache/lancedb was built
in some prior `index --force` run that succeeded WITHOUT halving (either by
luck-of-distribution or with the legacy VOYAGE_BATCH_SIZE=64 workaround).

The test index at examples/.lancedb-validation/ was just built via the
halving recovery path (one batch tripped Voyage's 120K cap, was split
206 -> 103 + 103, both halves succeeded, all 206 chunks indexed).

If the patched halving path is semantically transparent -- i.e., produces
the same embedding for a given chunk as the un-halved path -- the vectors
for matching chunks should be identical (cosine similarity = 1.0).

Caveat: even with deterministic Voyage embedding, server-side model updates
between the production build and now would shift vectors. Cosine ~ 0.999+
indicates 'halving is transparent, model has minor drift'. Cosine = 1.0
indicates 'halving is transparent AND model unchanged'.

Match key: (channel, video_id, timestamp_seconds) is unique within a
single channel's chunks.

Run from worktree root:

    python examples/voyage-embedding-equivalence-check.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PROD_DB_PATH = Path("C:/Users/danie/video-intel-cache/lancedb")
TEST_DB_PATH = Path(__file__).resolve().parent / ".lancedb-validation"
TABLE_NAME = "transcript_chunks"


def _load(db_path: Path):
    import lancedb

    db = lancedb.connect(str(db_path))
    return db.open_table(TABLE_NAME).to_pandas()


def main() -> int:
    if not PROD_DB_PATH.is_dir():
        raise SystemExit(f"production LanceDB not found at {PROD_DB_PATH}")
    if not TEST_DB_PATH.is_dir():
        raise SystemExit(
            f"test LanceDB not found at {TEST_DB_PATH}; "
            "run `python examples/voyage-real-trigger-harness.py "
            "--mode halving-trigger --batch-size 256` first"
        )

    print("=== Embedding equivalence check ===")
    print(f"Production : {PROD_DB_PATH}  (built without halving)")
    print(f"Test       : {TEST_DB_PATH}  (built via halving recovery)")
    print()

    prod = _load(PROD_DB_PATH)
    test = _load(TEST_DB_PATH)

    # Scope production to just lennyspodcast (the test index's only channel).
    test_channels = sorted(test["channel"].unique().tolist())
    prod_scoped = prod[prod["channel"].isin(test_channels)].copy()

    print(f"Production rows in scope ({test_channels}): {len(prod_scoped)}")
    print(f"Test rows                                   : {len(test)}")

    if len(prod_scoped) == 0:
        raise SystemExit("no production rows in scope to compare")

    # Build a unique key per chunk and pivot for matching.
    def _key(row):
        return (row["channel"], row["video_id"], row["timestamp_seconds"])

    prod_by_key = {_key(r): r for _, r in prod_scoped.iterrows()}
    test_by_key = {_key(r): r for _, r in test.iterrows()}

    common = sorted(set(prod_by_key) & set(test_by_key))
    only_prod = sorted(set(prod_by_key) - set(test_by_key))
    only_test = sorted(set(test_by_key) - set(prod_by_key))

    print(f"Matched on (channel, video_id, timestamp_seconds): {len(common)}")
    if only_prod:
        print(f"Only in production: {len(only_prod)}  (e.g., {only_prod[:3]})")
    if only_test:
        print(f"Only in test     : {len(only_test)}  (e.g., {only_test[:3]})")

    if not common:
        raise SystemExit("no chunks matched between the two indexes")

    # Compute pairwise cosine similarity + bit-equality counts.
    cosines = []
    bit_equal = 0
    text_diff = 0
    for k in common:
        p = prod_by_key[k]
        t = test_by_key[k]
        pv = np.asarray(p["vector"], dtype=np.float64)
        tv = np.asarray(t["vector"], dtype=np.float64)
        # Cosine similarity.
        denom = (np.linalg.norm(pv) * np.linalg.norm(tv)) or 1.0
        cos = float(np.dot(pv, tv) / denom)
        cosines.append(cos)
        if np.array_equal(p["vector"], t["vector"]):
            bit_equal += 1
        # Sanity: same text underneath?
        if p["text"] != t["text"]:
            text_diff += 1

    cosines = np.asarray(cosines)

    print()
    print("--- Vector comparison ---")
    print(f"chunks compared             : {len(common)}")
    print(f"bit-identical vectors       : {bit_equal} / {len(common)}")
    print(f"chunks with differing text  : {text_diff} (production text != test text)")
    print(f"cosine similarity min       : {cosines.min():.6f}")
    print(f"cosine similarity max       : {cosines.max():.6f}")
    print(f"cosine similarity mean      : {cosines.mean():.6f}")
    print(f"cosine similarity median    : {np.median(cosines):.6f}")
    print(f"chunks with cosine == 1.0   : {(cosines == 1.0).sum()}")
    print(f"chunks with cosine >= 0.9999: {(cosines >= 0.9999).sum()}")
    print(f"chunks with cosine < 0.99   : {(cosines < 0.99).sum()}")

    print()
    print("--- Verdict ---")
    if bit_equal == len(common):
        print("PERFECT: Every vector is bit-identical. Halving is semantically")
        print("transparent AND Voyage's model output is byte-stable for our content.")
    elif cosines.min() >= 0.9999:
        print("EQUIVALENT: All vectors agree to >= 0.9999 cosine similarity.")
        print("Halving is semantically transparent. Tiny float drift only.")
    elif cosines.min() >= 0.99:
        print("CLOSE: All vectors agree to >= 0.99 cosine similarity. Likely")
        print("halving is transparent and Voyage's model has minor server-side")
        print("drift between the production build and this test run.")
    else:
        print("DIVERGENT: Some vectors disagree by more than 0.01 cosine.")
        print(f"min cosine = {cosines.min():.6f}. Investigate matching key or model drift.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
