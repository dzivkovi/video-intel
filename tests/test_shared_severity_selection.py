"""Issue #165: `meta_transcript_is_severe` is the ONE shared severity
predicate for FOUR duplicate-selection sites - `_pick_canonical` (dedupe,
unchanged, renamed only), `_load_video_id_index` (the processed-state
cache), `collect_corpus_videos` (briefing-record provenance), and
`_find_canonical_meta_by_video_id` (local-file identity resolution).

Scope decision (deliberately narrower than the issue's literal wording):
only the SEVERITY component is shared. Each site keeps its OWN pre-existing
secondary ordering underneath it - `_pick_canonical`'s processed/modes/prefix
tie-break, `_load_video_id_index`'s first-wins-on-sorted-order,
`collect_corpus_videos`'s `_artifact_count`, and
`_find_canonical_meta_by_video_id`'s lexicographically-first filename are
all UNCHANGED within one severity bucket. A blanket switch to dedupe's
exact ordering (in particular `modes_completed` instead of
`_artifact_count`) would regress `collect_corpus_videos`: `modes_completed`
is a claim about what RAN, not what is on disk (issue #159 documented an
operator deleting an artifact by hand while the meta still claimed the
mode), and briefing ranking scores a record with no concepts.json on disk
at zero regardless of what its meta claims. See the "shared severity, not
shared ordering" guardrail in CLAUDE.md.
"""

import json
from pathlib import Path

import pytest

import video_intel as vi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_meta(channel_dir: Path, prefix: str, data: dict) -> Path:
    """Write a meta.json sidecar and return its path."""
    channel_dir.mkdir(parents=True, exist_ok=True)
    path = channel_dir / f"{prefix}.meta.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _meta_tuple(prefix: str, **fields) -> tuple[Path, dict]:
    """Build a (Path, dict) pair shaped like `_pick_canonical` expects,
    without touching disk."""
    return Path(f"{prefix}.meta.json"), fields


@pytest.fixture(autouse=True)
def _clear_video_id_cache():
    """`_load_video_id_index` caches per channel dir; start and end clean."""
    vi._invalidate_video_id_cache()
    yield
    vi._invalidate_video_id_cache()


# ---------------------------------------------------------------------------
# Anti-drift: all four sites route through the SAME predicate
# ---------------------------------------------------------------------------


def test_all_four_sites_respond_to_meta_transcript_is_severe_being_monkeypatched(tmp_path, monkeypatch):
    """The ticket's real point: a site that re-implemented severity locally
    would not respond to this monkeypatch, and this test would catch it.

    Sets up, for each of the four sites, a duplicate pair where meta A is
    genuinely clean and meta B carries a severe flag - so under the REAL
    predicate, A wins every site. Then monkeypatches
    `vi.meta_transcript_is_severe` to a sentinel that INVERTS the verdict
    (severe becomes "not severe" and vice versa) and asserts all four sites
    now pick B instead - proving each one actually calls the shared name
    at selection time, not a local re-derivation.
    """
    # --- _pick_canonical ---
    clean = _meta_tuple(
        "2026-04-15-clean",
        processed="2026-04-15T00:00:00+00:00",
        modes_completed=["scan", "transcript"],
    )
    severe = _meta_tuple(
        "2026-04-20-severe",
        processed="2026-04-20T00:00:00+00:00",
        modes_completed=["scan", "transcript"],
        transcript_quality_flags=["monolithic_severe"],
    )
    picked_path, _ = vi._pick_canonical([clean, severe])
    assert picked_path.name == "2026-04-15-clean.meta.json"

    # --- _load_video_id_index ---
    ch_a = tmp_path / "channel_a"
    _write_meta(
        ch_a,
        "2026-04-15-clean",
        {"video_id": "vidA", "processed": "2026-04-15T00:00:00+00:00"},
    )
    _write_meta(
        ch_a,
        "2026-04-20-severe",
        {
            "video_id": "vidA",
            "processed": "2026-04-20T00:00:00+00:00",
            "transcript_quality_flags": ["monolithic_severe"],
        },
    )
    index = vi._load_video_id_index(ch_a)
    assert index["vidA"] == "2026-04-15-clean"

    # --- collect_corpus_videos ---
    ch_b = tmp_path / "corpus" / "channel_b"
    _write_meta(
        ch_b,
        "2026-04-15-clean",
        {"video_id": "vidB", "processed": "2026-04-15T00:00:00+00:00", "topics": []},
    )
    _touch(ch_b / "2026-04-15-clean.mindmap.md")
    _touch(ch_b / "2026-04-15-clean.concepts.json", "{}")
    _write_meta(
        ch_b,
        "2026-04-20-severe",
        {
            "video_id": "vidB",
            "processed": "2026-04-20T00:00:00+00:00",
            "transcript_quality_flags": ["monolithic_severe"],
            "topics": [],
        },
    )
    _touch(ch_b / "2026-04-20-severe.mindmap.md")
    _touch(ch_b / "2026-04-20-severe.concepts.json", "{}")
    records = vi.collect_corpus_videos(tmp_path / "corpus")
    assert len(records) == 1
    assert records[0]["title"] == "2026-04-15-clean"

    # --- _find_canonical_meta_by_video_id ---
    ch_c = tmp_path / "channel_c"
    _write_meta(ch_c, "2026-04-15-clean", {"video_id": "vidC"})
    _write_meta(
        ch_c,
        "2026-04-20-severe",
        {"video_id": "vidC", "transcript_quality_flags": ["monolithic_severe"]},
    )
    found = vi._find_canonical_meta_by_video_id(ch_c, "vidC")
    assert found.name == "2026-04-15-clean.meta.json"

    # --- now invert the predicate and re-run every site ---
    def _inverted(meta_data: dict) -> bool:
        return not vi.transcript_quality_flags_are_severe(
            meta_data.get("transcript_quality_flags")
            if isinstance(meta_data.get("transcript_quality_flags"), list)
            else None
        )

    monkeypatch.setattr(vi, "meta_transcript_is_severe", _inverted)

    picked_path, _ = vi._pick_canonical([clean, severe])
    assert picked_path.name == "2026-04-20-severe.meta.json", "_pick_canonical did not respond to the patched predicate"

    vi._invalidate_video_id_cache(ch_a)
    index = vi._load_video_id_index(ch_a)
    assert index["vidA"] == "2026-04-20-severe", "_load_video_id_index did not respond to the patched predicate"

    records = vi.collect_corpus_videos(tmp_path / "corpus")
    assert records[0]["title"] == "2026-04-20-severe", "collect_corpus_videos did not respond to the patched predicate"

    found = vi._find_canonical_meta_by_video_id(ch_c, "vidC")
    assert found.name == "2026-04-20-severe.meta.json", (
        "_find_canonical_meta_by_video_id did not respond to the patched predicate"
    )


# ---------------------------------------------------------------------------
# _load_video_id_index
# ---------------------------------------------------------------------------


def test_load_video_id_index_prefers_clean_over_severe(tmp_path):
    """The severe meta's prefix sorts ALPHABETICALLY FIRST - pre-#165
    first-wins-on-glob-order would have picked it. Post-#165 the clean
    meta wins despite sorting second."""
    ch = tmp_path / "channel"
    _write_meta(
        ch,
        "2026-04-01-severe",
        {
            "video_id": "vid1",
            "processed": "2026-04-01T00:00:00+00:00",
            "transcript_quality_flags": ["blind_gap_severe"],
        },
    )
    _write_meta(ch, "2026-04-20-clean", {"video_id": "vid1", "processed": "2026-04-20T00:00:00+00:00"})
    index = vi._load_video_id_index(ch)
    assert index["vid1"] == "2026-04-20-clean"


def test_load_video_id_index_same_severity_bucket_keeps_first_wins_on_sorted_order(tmp_path):
    """Both clean (or both severe): secondary ordering is UNCHANGED -
    first-wins on the sorted glob order, not e.g. latest processed."""
    ch = tmp_path / "channel"
    _write_meta(ch, "2026-04-01-a", {"video_id": "vid1", "processed": "2026-04-01T00:00:00+00:00"})
    _write_meta(ch, "2026-04-30-b", {"video_id": "vid1", "processed": "2026-04-30T00:00:00+00:00"})
    index = vi._load_video_id_index(ch)
    # "2026-04-01-a" sorts before "2026-04-30-b" lexicographically.
    assert index["vid1"] == "2026-04-01-a"

    vi._invalidate_video_id_cache(ch)
    ch2 = tmp_path / "channel2"
    flags = ["monolithic_severe"]
    _write_meta(ch2, "2026-04-01-a", {"video_id": "vid1", "transcript_quality_flags": flags})
    _write_meta(ch2, "2026-04-30-b", {"video_id": "vid1", "transcript_quality_flags": flags})
    index2 = vi._load_video_id_index(ch2)
    assert index2["vid1"] == "2026-04-01-a"


def test_load_video_id_index_is_deterministic_regardless_of_glob_order(tmp_path, monkeypatch):
    """The glob is now sorted, so the result must not depend on the order
    Path.glob happens to hand files back in. Simulate a reversed glob order
    directly to prove sorting inside the function - not incidental OS
    ordering - is what makes this deterministic."""
    ch = tmp_path / "channel"
    a = _write_meta(ch, "2026-04-01-a", {"video_id": "vid1"})
    b = _write_meta(ch, "2026-04-30-b", {"video_id": "vid1", "transcript_quality_flags": ["monolithic_severe"]})

    real_glob = Path.glob

    def _reversed_glob(self, pattern):
        return list(reversed(list(real_glob(self, pattern))))

    monkeypatch.setattr(Path, "glob", _reversed_glob)
    index = vi._load_video_id_index(ch)
    assert index["vid1"] == "2026-04-01-a"
    assert {a.name, b.name} == {"2026-04-01-a.meta.json", "2026-04-30-b.meta.json"}


# ---------------------------------------------------------------------------
# collect_corpus_videos
# ---------------------------------------------------------------------------


def test_collect_corpus_videos_prefers_clean_over_more_complete_severe(tmp_path):
    """Pre-#165 the severe meta (more artifacts on disk) would have won
    on _artifact_count alone. Post-#165, severity leads: the clean meta
    wins even though it has FEWER artifacts."""
    output_dir = tmp_path / "corpus"
    ch = output_dir / "channel"
    _write_meta(
        ch,
        "2026-04-15-clean",
        {"video_id": "vid1", "processed": "2026-04-15T00:00:00+00:00", "topics": []},
    )
    # Clean meta: only a mindmap on disk (artifact_count=1).
    _touch(ch / "2026-04-15-clean.mindmap.md")

    _write_meta(
        ch,
        "2026-04-20-severe",
        {
            "video_id": "vid1",
            "processed": "2026-04-20T00:00:00+00:00",
            "transcript_quality_flags": ["monolithic_severe"],
            "topics": [],
        },
    )
    # Severe meta: both mindmap and concepts on disk (artifact_count=2).
    _touch(ch / "2026-04-20-severe.mindmap.md")
    _touch(ch / "2026-04-20-severe.concepts.json", "{}")

    records = vi.collect_corpus_videos(output_dir)
    assert len(records) == 1
    assert records[0]["title"] == "2026-04-15-clean"


def test_collect_corpus_videos_same_severity_bucket_artifact_count_still_decides(tmp_path):
    """Both clean (or both severe): secondary ordering is UNCHANGED -
    _artifact_count decides, matching pre-#165 behavior exactly."""
    output_dir = tmp_path / "corpus"
    ch = output_dir / "channel"
    _write_meta(
        ch,
        "2026-04-15-fewer",
        {"video_id": "vid1", "processed": "2026-04-15T00:00:00+00:00", "topics": []},
    )
    _touch(ch / "2026-04-15-fewer.mindmap.md")

    _write_meta(
        ch,
        "2026-04-10-more",
        {"video_id": "vid1", "processed": "2026-04-10T00:00:00+00:00", "topics": []},
    )
    _touch(ch / "2026-04-10-more.mindmap.md")
    _touch(ch / "2026-04-10-more.concepts.json", "{}")

    records = vi.collect_corpus_videos(output_dir)
    assert len(records) == 1
    # "more" has 2 artifacts vs "fewer"'s 1, despite being processed earlier.
    assert records[0]["title"] == "2026-04-10-more"


def test_collect_corpus_videos_unions_topics_even_when_severity_flips_the_winner(tmp_path):
    """Highest-risk regression (issue #146): topics UNION across duplicate
    metas must stay independent of who wins the selection. The severe meta
    (loser) carries a topic tag the clean meta (winner) does not - it must
    still appear on the surfaced record."""
    output_dir = tmp_path / "corpus"
    ch = output_dir / "channel"
    _write_meta(
        ch,
        "2026-04-15-clean",
        {"video_id": "vid1", "processed": "2026-04-15T00:00:00+00:00", "topics": ["fde"]},
    )
    _touch(ch / "2026-04-15-clean.mindmap.md")

    _write_meta(
        ch,
        "2026-04-20-severe",
        {
            "video_id": "vid1",
            "processed": "2026-04-20T00:00:00+00:00",
            "transcript_quality_flags": ["monolithic_severe"],
            "topics": ["sales"],
        },
    )
    _touch(ch / "2026-04-20-severe.mindmap.md")
    _touch(ch / "2026-04-20-severe.concepts.json", "{}")

    records = vi.collect_corpus_videos(output_dir)
    assert len(records) == 1
    record = records[0]
    assert record["title"] == "2026-04-15-clean"
    assert record["topics"] == ["fde", "sales"]


def test_collect_corpus_videos_unions_topics_when_clean_meta_seen_first(tmp_path):
    """Same as above but with the winner already installed as `existing`
    when the loser (severe, but with a distinct topic) is encountered -
    exercises the OTHER union branch (`elif record["topics"]`)."""
    output_dir = tmp_path / "corpus"
    ch = output_dir / "channel"
    _write_meta(
        ch,
        "2026-04-10-clean",
        {"video_id": "vid1", "processed": "2026-04-10T00:00:00+00:00", "topics": ["fde"]},
    )
    _touch(ch / "2026-04-10-clean.mindmap.md")

    _write_meta(
        ch,
        "2026-04-20-severe",
        {
            "video_id": "vid1",
            "processed": "2026-04-20T00:00:00+00:00",
            "transcript_quality_flags": ["monolithic_severe"],
            "topics": ["sales"],
        },
    )
    _touch(ch / "2026-04-20-severe.mindmap.md")
    _touch(ch / "2026-04-20-severe.concepts.json", "{}")

    records = vi.collect_corpus_videos(output_dir)
    assert len(records) == 1
    record = records[0]
    assert record["title"] == "2026-04-10-clean"
    assert record["topics"] == ["fde", "sales"]


# ---------------------------------------------------------------------------
# _find_canonical_meta_by_video_id
# ---------------------------------------------------------------------------


def test_find_canonical_meta_prefers_clean_over_severe(tmp_path):
    """Pre-#165 this returned the lexicographically-first filename
    unconditionally; the severe meta sorts first here (its prefix name is
    alphabetically earlier), so it must still lose."""
    ch = tmp_path / "channel"
    _write_meta(ch, "2026-04-01-aaa-severe", {"video_id": "vid1", "transcript_quality_flags": ["monolithic_severe"]})
    _write_meta(ch, "2026-04-20-zzz-clean", {"video_id": "vid1"})
    found = vi._find_canonical_meta_by_video_id(ch, "vid1")
    assert found.name == "2026-04-20-zzz-clean.meta.json"


def test_find_canonical_meta_same_severity_bucket_lexicographically_first_wins(tmp_path):
    """Both clean (or both severe): secondary ordering is UNCHANGED -
    lexicographically-first filename decides."""
    ch = tmp_path / "channel"
    _write_meta(ch, "2026-04-01-a", {"video_id": "vid1"})
    _write_meta(ch, "2026-04-20-b", {"video_id": "vid1"})
    found = vi._find_canonical_meta_by_video_id(ch, "vid1")
    assert found.name == "2026-04-01-a.meta.json"

    ch2 = tmp_path / "channel2"
    flags = ["blind_gap_severe"]
    _write_meta(ch2, "2026-04-01-a", {"video_id": "vid1", "transcript_quality_flags": flags})
    _write_meta(ch2, "2026-04-20-b", {"video_id": "vid1", "transcript_quality_flags": flags})
    found2 = vi._find_canonical_meta_by_video_id(ch2, "vid1")
    assert found2.name == "2026-04-01-a.meta.json"


def test_find_canonical_meta_warns_naming_the_chosen_file(tmp_path, caplog):
    """The WARNING on >1 match must still fire and name the file actually
    picked - now the severity-preferred one, not just 'the first'."""
    ch = tmp_path / "channel"
    _write_meta(ch, "2026-04-01-aaa-severe", {"video_id": "vid1", "transcript_quality_flags": ["monolithic_severe"]})
    _write_meta(ch, "2026-04-20-zzz-clean", {"video_id": "vid1"})
    with caplog.at_level("WARNING"):
        found = vi._find_canonical_meta_by_video_id(ch, "vid1")
    assert found.name == "2026-04-20-zzz-clean.meta.json"
    assert any("2026-04-20-zzz-clean.meta.json" in rec.message for rec in caplog.records)
