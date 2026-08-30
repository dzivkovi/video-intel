"""Gate 1 real-input smoke for issue #158 (cross-chunk timestamp
misclassification is invisible to every existing check).

The four forensic sidecars found in the read-only corpus
(`G:/My Drive/video-intel/**/*.transcript.raw.chunk*.txt`) are all 0 bytes
(timeout/confabulation failures with an empty raw response) - none parse to
JSON. Per the issue's Gate 1 fallback, this harness instead takes REAL
dialogue entries from a real chunked transcript already in the corpus
(saminyasar's "Hermes Agent" course, 4 chunks at 50-minute/3000s nominal
size) and MECHANICALLY double-offsets one chunk's entries by
+2*chunk_duration_seconds (see the in-function comment for why 2x is the
literal "double offset") to construct the failure shape the issue names.
Explicitly: the INPUT is real content, the CORRUPTION is synthetic.

Run from the repo root:
    python docs/plans/gate1-evidence/issue-158-chunk-window-mismatch-smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import video_intel as vi

# Real, corpus-verified absolute timestamps (already-classified/rendered
# form) from:
#   G:/My Drive/video-intel/saminyasar/
#     2026-07-10-hermes-agent-full-course-3-hours-build-sell-2026.transcript.md
# Chunk 1 window (nominal [0, 3000)): seven dialogue lines between 47:11 and
# 49:49 (2831s-2989s), well inside chunk 1's real window.
CHUNK1_REAL_SECONDS = [2831, 2852, 2873, 2901, 2922, 2952, 2989]
# Chunk 2 window (nominal [3000, 6000)): ten dialogue lines between 50:00
# and 58:04 (3000s-3484s), well inside chunk 2's real window.
CHUNK2_REAL_SECONDS = [3000, 3028, 3070, 3099, 3165, 3217, 3300, 3361, 3414, 3484]


def _fmt(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _chunk_json(seconds: list[int]) -> dict:
    return {
        "transcripts": [{"start": _fmt(s), "voice": 1, "text": f"real line at {s}s"} for s in seconds],
        "screen_content": [],
        "speakers": [{"voice": 1, "name": "Samin Yasar"}],
    }


def main() -> None:
    chunk_duration_seconds = 3000  # matches the real video's chunk_minutes=50
    chunk1 = _chunk_json(CHUNK1_REAL_SECONDS)

    # Mechanical double-offset (synthetic corruption applied to real
    # content): chunk 2's genuinely-correct absolute positions need +0
    # offset (they are already absolute). A "double-offset" misclassification
    # mistakenly adds chunk_start_secs to an already-absolute stamp - and
    # doing that TWICE (once from an initial wrong relative read, again from
    # a second erroneous pass) lands each stamp at
    # original_absolute + 2 * chunk_start_secs. With chunk_start_secs=3000
    # that shift (6000s) pushes every one of chunk 2's real positions
    # (3000-3484) to 9000-9484 - past even chunk 3's window, comfortably
    # outside the classifier's own absolute-band tolerance for chunk 2, so
    # every stamp falls to the implausible-passthrough branch unchanged and
    # lands squarely outside chunk 2's real window.
    corrupted_chunk2_seconds = [s + 2 * chunk_duration_seconds for s in CHUNK2_REAL_SECONDS]
    chunk2_corrupted = _chunk_json(corrupted_chunk2_seconds)

    chunks = [(0, chunk1), (chunk_duration_seconds, chunk2_corrupted)]
    chunk_bounds = [(0, chunk_duration_seconds), (chunk_duration_seconds, 2 * chunk_duration_seconds)]

    print("=" * 78)
    print("PRE-FIX equivalent: merge_chunked_transcripts() with no chunk_bounds")
    print("(this is byte-identical to the code path before issue #158)")
    print("=" * 78)
    pre_fix_merged = vi.merge_chunked_transcripts(chunks, chunk_duration_seconds=chunk_duration_seconds)
    print(f"Returned keys: {sorted(pre_fix_merged.keys())}")
    print(f"'_chunk_window_violations' present: {'_chunk_window_violations' in pre_fix_merged}")
    classified_chunk2_starts = [
        t["start"]
        for t in pre_fix_merged["transcripts"]
        if vi.timestamp_to_seconds(t["start"]) >= chunk_duration_seconds
    ]
    print(f"Chunk 2's classified (final, persisted) timestamps: {classified_chunk2_starts}")
    print(
        "-> These all landed inside chunk 4's real window (2:30:00-3:02:48) "
        "instead of chunk 2's real window (50:00-1:40:00) - silently wrong, "
        "and NOTHING in the pre-fix return value flags it."
    )

    print()
    print("=" * 78)
    print("POST-FIX: merge_chunked_transcripts() with chunk_bounds supplied")
    print("=" * 78)
    post_fix_merged = vi.merge_chunked_transcripts(
        chunks, chunk_duration_seconds=chunk_duration_seconds, chunk_bounds=chunk_bounds
    )
    violations = post_fix_merged["_chunk_window_violations"]
    print(f"'_chunk_window_violations': {violations}")
    result = vi._classify_chunk_window_violations(violations)
    print(f"_classify_chunk_window_violations(): {result}")
    print(
        f"transcript_quality_flags_are_severe(result['severe']): {vi.transcript_quality_flags_are_severe(result['severe'])}"
    )

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    chunk2_result = violations[1]
    assert chunk2_result["out_of_window"] == len(CHUNK2_REAL_SECONDS), "expected every corrupted stamp flagged"
    assert vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE in result["severe"], "expected a SEVERE flag"
    print(
        f"Chunk 2: {chunk2_result['out_of_window']}/{chunk2_result['classified_dialogue']} "
        "classified dialogue entries flagged out-of-window; SEVERE."
    )
    print("Pre-fix: silent (no signal). Post-fix: reported. Gate 1 PASSED.")


if __name__ == "__main__":
    main()
