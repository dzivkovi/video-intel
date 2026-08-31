"""Tests for video_id-based dedup: prevention in is_processed() and the
dedupe subcommand that cleans up historical duplicates.

Background: YouTube creators A/B test video titles for SEO. When a title
rotates, the slug in video_file_prefix() changes, so the previous
is_processed() slug-only check missed the match and the pipeline
re-processed the same video_id under a second prefix. Production sweep
on 2026-04-22 found 6 such groups across 4 channels.

These tests guard:
  - is_processed() now consults a per-channel video_id index first.
  - A dedupe subcommand picks a canonical meta, preserves the discarded
    SEO titles as alt_titles, and deletes the losers' artifacts.
  - A pre-scan alt_title recorder captures ongoing rotations without
    requiring a dedupe pass.
"""

import json
from argparse import Namespace
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


@pytest.fixture(autouse=True)
def _clear_video_id_cache():
    """Each test starts with an empty video_id index cache."""
    vi._invalidate_video_id_cache()
    yield
    vi._invalidate_video_id_cache()


# ---------------------------------------------------------------------------
# Prevention: is_processed() video_id lookup
# ---------------------------------------------------------------------------


def test_is_processed_returns_true_when_video_id_already_under_different_slug(tmp_path):
    """Core fix: creator rotated the title; video_id is the same; we must
    recognize the video as already processed even though the new slug
    doesn't exist under it yet."""
    ch = tmp_path / "natebjones"
    original_prefix = "2026-04-01-claude-mythos-changes-everything"
    _write_meta(
        ch,
        original_prefix,
        {
            "video_id": "vid123",
            "video_url": "https://www.youtube.com/watch?v=vid123",
            "channel": "natebjones",
            "title": "Claude Mythos Changes Everything.",
            "published": "2026-04-01",
            "processed": "2026-04-02T20:58:51+00:00",
            "modes_completed": ["scan"],
        },
    )
    _touch(ch / f"{original_prefix}.mindmap.md", "# mind map")

    rotated_video = {
        "video_id": "vid123",
        "title": "Your AI Stack Isn't Ready for Claude Mythos",
        "published": "2026-04-01",
    }
    assert vi.is_processed(tmp_path, "natebjones", rotated_video, "scan", any_variant=True) is True


def test_is_processed_falls_back_to_slug_when_video_id_not_in_index(tmp_path):
    """Brand-new video with no existing meta for its id: must still use the
    slug-based existence check (legacy path, and the new-video path)."""
    ch = tmp_path / "natebjones"
    ch.mkdir()

    brand_new = {"video_id": "new456", "title": "Brand New", "published": "2026-04-22"}
    assert vi.is_processed(tmp_path, "natebjones", brand_new, "scan", any_variant=True) is False


def test_is_processed_cache_is_populated_once_per_channel(tmp_path, monkeypatch):
    """Regression guard: the video_id index should be globbed at most once
    per channel per run, not on every is_processed() call."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-01-a",
        {"video_id": "v1", "title": "A", "published": "2026-04-01", "processed": "2026-04-01T00:00:00+00:00"},
    )
    _touch(ch / "2026-04-01-a.mindmap.md")

    call_count = {"n": 0}
    original_glob = Path.glob

    def counting_glob(self, pattern):
        if pattern == "*.meta.json" and self == ch:
            call_count["n"] += 1
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", counting_glob)

    video = {"video_id": "v1", "title": "A", "published": "2026-04-01"}
    for _ in range(5):
        vi.is_processed(tmp_path, "ch", video, "scan", any_variant=True)

    assert call_count["n"] == 1, "meta.json glob must be cached after the first call"


# ---------------------------------------------------------------------------
# Cleanup: dedupe subcommand
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, channels: list[str]) -> dict:
    return {
        "output_dir": str(tmp_path),
        "channels": [{"name": name, "url": f"https://example.com/{name}"} for name in channels],
    }


def _make_dupe_group(ch: Path, prefix_a: str, prefix_b: str) -> None:
    """Produce a canonical shape: two metas with same video_id, two processed
    timestamps, each with its own mindmap/transcript/concepts sidecars."""
    _write_meta(
        ch,
        prefix_a,
        {
            "video_id": "dup1",
            "video_url": "https://www.youtube.com/watch?v=dup1",
            "channel": ch.name,
            "title": "Earlier Title",
            "published": "2026-04-15",
            "processed": "2026-04-18T04:04:43+00:00",
            "modes_completed": ["scan", "transcript", "concepts"],
        },
    )
    _touch(ch / f"{prefix_a}.mindmap.md", "A mindmap")
    _touch(ch / f"{prefix_a}.transcript.md", "A transcript")
    _touch(ch / f"{prefix_a}.concepts.json", "{}")

    _write_meta(
        ch,
        prefix_b,
        {
            "video_id": "dup1",
            "video_url": "https://www.youtube.com/watch?v=dup1",
            "channel": ch.name,
            "title": "Later Title",
            "published": "2026-04-15",
            "processed": "2026-04-18T04:05:17+00:00",
            "modes_completed": ["scan", "transcript", "concepts"],
        },
    )
    _touch(ch / f"{prefix_b}.mindmap.md", "B mindmap")
    _touch(ch / f"{prefix_b}.transcript.md", "B transcript")
    _touch(ch / f"{prefix_b}.concepts.json", "{}")


def test_dedupe_dry_run_reports_but_does_not_modify(tmp_path):
    ch = tmp_path / "samwitteveenai"
    _make_dupe_group(ch, "2026-04-15-earlier", "2026-04-15-later")
    before = {p.name: p.read_text() for p in ch.iterdir()}

    vi.cmd_dedupe(Namespace(channel=None, apply=False), _make_config(tmp_path, ["samwitteveenai"]))

    after = {p.name: p.read_text() for p in ch.iterdir()}
    assert before == after, "dry-run must not mutate any file"


def test_dedupe_apply_picks_latest_processed_as_canonical(tmp_path):
    ch = tmp_path / "samwitteveenai"
    _make_dupe_group(ch, "2026-04-15-earlier", "2026-04-15-later")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["samwitteveenai"]))

    # Later-processed prefix survives; earlier-processed prefix is gone
    assert (ch / "2026-04-15-later.meta.json").exists()
    assert not (ch / "2026-04-15-earlier.meta.json").exists()


def test_dedupe_apply_merges_alt_titles_ordered_by_processed(tmp_path):
    ch = tmp_path / "ch"
    _make_dupe_group(ch, "2026-04-15-earlier", "2026-04-15-later")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    canonical = json.loads((ch / "2026-04-15-later.meta.json").read_text())
    assert canonical["title"] == "Later Title"
    assert canonical["alt_titles"] == ["Earlier Title"]


def test_dedupe_apply_deletes_all_loser_siblings(tmp_path):
    ch = tmp_path / "ch"
    _make_dupe_group(ch, "2026-04-15-earlier", "2026-04-15-later")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    loser_siblings = list(ch.glob("2026-04-15-earlier.*"))
    assert loser_siblings == [], f"loser siblings should be deleted: {loser_siblings}"


def test_dedupe_apply_copies_mode_artifact_when_only_loser_has_it(tmp_path):
    """Canonical (latest processed) may be less complete than the loser.
    In that case, we must not lose content: copy the loser's artifact for
    the missing mode to the canonical prefix before deleting losers."""
    ch = tmp_path / "ch"
    # Canonical: latest processed, scan-only
    _write_meta(
        ch,
        "2026-04-15-b",
        {
            "video_id": "v1",
            "title": "B (canonical, scan-only)",
            "published": "2026-04-15",
            "processed": "2026-04-20T00:00:00+00:00",
            "modes_completed": ["scan"],
        },
    )
    _touch(ch / "2026-04-15-b.mindmap.md", "B mindmap")
    # Loser: older, but has transcript
    _write_meta(
        ch,
        "2026-04-15-a",
        {
            "video_id": "v1",
            "title": "A (loser, has transcript)",
            "published": "2026-04-15",
            "processed": "2026-04-18T00:00:00+00:00",
            "modes_completed": ["scan", "transcript"],
        },
    )
    _touch(ch / "2026-04-15-a.mindmap.md", "A mindmap")
    _touch(ch / "2026-04-15-a.transcript.md", "A transcript content")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    # Canonical survives and gained the transcript
    assert (ch / "2026-04-15-b.transcript.md").exists()
    assert (ch / "2026-04-15-b.transcript.md").read_text() == "A transcript content"

    canonical = json.loads((ch / "2026-04-15-b.meta.json").read_text())
    assert set(canonical["modes_completed"]) == {"scan", "transcript"}

    # Loser's siblings are gone
    assert not list(ch.glob("2026-04-15-a.*"))


def test_dedupe_on_clean_channel_is_noop(tmp_path):
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-unique",
        {"video_id": "solo", "title": "Solo", "published": "2026-04-15", "processed": "2026-04-18T00:00:00+00:00"},
    )
    _touch(ch / "2026-04-15-unique.mindmap.md")

    before = {p.name: p.read_text() for p in ch.iterdir()}
    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))
    after = {p.name: p.read_text() for p in ch.iterdir()}
    assert before == after


def test_dedupe_preserves_existing_alt_titles_on_canonical(tmp_path):
    """If canonical already has alt_titles from a prior pass, union don't overwrite."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-c",
        {
            "video_id": "v1",
            "title": "C",
            "published": "2026-04-15",
            "processed": "2026-04-20T00:00:00+00:00",
            "modes_completed": ["scan"],
            "alt_titles": ["PriorAlt"],
        },
    )
    _touch(ch / "2026-04-15-c.mindmap.md")
    _write_meta(
        ch,
        "2026-04-15-a",
        {
            "video_id": "v1",
            "title": "A",
            "published": "2026-04-15",
            "processed": "2026-04-18T00:00:00+00:00",
            "modes_completed": ["scan"],
        },
    )
    _touch(ch / "2026-04-15-a.mindmap.md")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    canonical = json.loads((ch / "2026-04-15-c.meta.json").read_text())
    assert "PriorAlt" in canonical["alt_titles"]
    assert "A" in canonical["alt_titles"]


# ---------------------------------------------------------------------------
# Quality-aware canonical selection (issue #159)
# ---------------------------------------------------------------------------
#
# Background: dedupe used to pick canonical by latest `processed` timestamp
# alone. A rerun whose transcript tripped the #157/#158 severe quality guards
# (e.g. a monolithic-collapse transcript) is newer but worse, and used to beat
# a healthy older duplicate - the healthy artifacts became the deleted losers.
# The fix: a meta WITHOUT severe transcript_quality_flags always outranks one
# WITH severe flags, via the existing transcript_quality_flags_are_severe()
# helper (never a re-implemented severity test). Within one severity bucket,
# the pre-#159 tie-break (latest processed, modes_completed size, prefix) is
# unchanged.


def _meta_tuple(prefix: str, **fields) -> tuple[Path, dict]:
    """Build a (Path, dict) pair shaped like `_pick_canonical` expects,
    without touching disk - these tests exercise the pure selection logic."""
    return Path(f"{prefix}.meta.json"), fields


def test_pick_canonical_prefers_clean_over_severe_regardless_of_recency():
    """The headline case: a newer, severe-flagged rerun must never beat an
    older, clean duplicate."""
    older_clean = _meta_tuple(
        "2026-04-15-older",
        processed="2026-04-18T00:00:00+00:00",
        modes_completed=["scan", "transcript"],
    )
    newer_severe = _meta_tuple(
        "2026-04-20-newer",
        processed="2026-04-20T00:00:00+00:00",
        modes_completed=["scan", "transcript"],
        transcript_quality_flags=["monolithic_severe"],
    )
    canonical_path, _ = vi._pick_canonical([older_clean, newer_severe])
    assert canonical_path.name == "2026-04-15-older.meta.json"


def test_pick_canonical_both_clean_newer_wins_regression_lock():
    """Regression lock: with neither meta severe, behavior is unchanged from
    pre-#159 - latest processed wins."""
    older = _meta_tuple("2026-04-15-a", processed="2026-04-18T00:00:00+00:00", modes_completed=["scan"])
    newer = _meta_tuple("2026-04-15-b", processed="2026-04-20T00:00:00+00:00", modes_completed=["scan"])
    canonical_path, _ = vi._pick_canonical([older, newer])
    assert canonical_path.name == "2026-04-15-b.meta.json"


def test_pick_canonical_both_severe_newer_wins():
    """With both metas severe, the pre-#159 tie-break still applies: latest
    processed wins even though neither is clean."""
    older_severe = _meta_tuple(
        "2026-04-15-a",
        processed="2026-04-18T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags=["blind_gap_severe"],
    )
    newer_severe = _meta_tuple(
        "2026-04-15-b",
        processed="2026-04-20T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags=["monolithic_severe"],
    )
    canonical_path, _ = vi._pick_canonical([older_severe, newer_severe])
    assert canonical_path.name == "2026-04-15-b.meta.json"


def test_pick_canonical_absent_flags_field_treated_clean():
    """No transcript_quality_flags key at all - must not be mistaken for
    severe; the meta with the field absent still wins over a severe one even
    if it is older."""
    older_absent = _meta_tuple("2026-04-15-a", processed="2026-04-18T00:00:00+00:00", modes_completed=["scan"])
    newer_severe = _meta_tuple(
        "2026-04-15-b",
        processed="2026-04-20T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags=["monolithic_severe"],
    )
    canonical_path, _ = vi._pick_canonical([older_absent, newer_severe])
    assert canonical_path.name == "2026-04-15-a.meta.json"


def test_pick_canonical_malformed_flags_scalar_string_treated_clean_without_crash():
    """A malformed transcript_quality_flags value that is a bare string
    (not a list) must degrade to 'not severe' rather than crashing the
    dedupe sort - and rather than being scanned character-by-character."""
    older_malformed = _meta_tuple(
        "2026-04-15-a",
        processed="2026-04-18T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags="monolithic_severe",
    )
    newer_severe = _meta_tuple(
        "2026-04-15-b",
        processed="2026-04-20T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags=["monolithic_severe"],
    )
    canonical_path, _ = vi._pick_canonical([older_malformed, newer_severe])
    assert canonical_path.name == "2026-04-15-a.meta.json"


def test_pick_canonical_malformed_flags_int_entries_treated_clean_without_crash():
    """A transcript_quality_flags list with non-string (int) entries must
    degrade to 'not severe' without raising."""
    older_malformed = _meta_tuple(
        "2026-04-15-a",
        processed="2026-04-18T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags=[1, 2, 3],
    )
    newer_severe = _meta_tuple(
        "2026-04-15-b",
        processed="2026-04-20T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags=["monolithic_severe"],
    )
    canonical_path, _ = vi._pick_canonical([older_malformed, newer_severe])
    assert canonical_path.name == "2026-04-15-a.meta.json"


def _make_dupe_group_quality_aware(
    ch: Path,
    prefix_older_clean: str,
    prefix_newer_severe: str,
) -> None:
    """Same shape as `_make_dupe_group`, but the newer meta carries a severe
    transcript_quality_flags entry and the older is clean."""
    _write_meta(
        ch,
        prefix_older_clean,
        {
            "video_id": "dup1",
            "video_url": "https://www.youtube.com/watch?v=dup1",
            "channel": ch.name,
            "title": "Healthy Older Title",
            "published": "2026-04-15",
            "processed": "2026-04-18T04:04:43+00:00",
            "modes_completed": ["scan", "transcript", "concepts"],
            "topics": ["ai-agents"],
        },
    )
    _touch(ch / f"{prefix_older_clean}.mindmap.md", "older mindmap")
    _touch(ch / f"{prefix_older_clean}.transcript.md", "older transcript, healthy")
    _touch(ch / f"{prefix_older_clean}.concepts.json", "{}")

    _write_meta(
        ch,
        prefix_newer_severe,
        {
            "video_id": "dup1",
            "video_url": "https://www.youtube.com/watch?v=dup1",
            "channel": ch.name,
            "title": "Severe Rerun Title",
            "published": "2026-04-15",
            "processed": "2026-04-20T04:05:17+00:00",
            "modes_completed": ["scan", "transcript"],
            "transcript_quality_flags": ["monolithic_severe"],
            "topics": ["safety"],
        },
    )
    _touch(ch / f"{prefix_newer_severe}.mindmap.md", "newer mindmap")
    _touch(ch / f"{prefix_newer_severe}.transcript.md", "newer transcript, collapsed")


def test_dedupe_apply_severe_flagged_newer_loses_to_clean_older_headline_case(tmp_path):
    """End-to-end: `dedupe --apply` must keep the older, clean duplicate as
    canonical and delete the newer, severe-flagged rerun's artifacts - the
    exact defect issue #159 reports."""
    ch = tmp_path / "ch"
    _make_dupe_group_quality_aware(ch, "2026-04-15-older", "2026-04-20-newer")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    assert (ch / "2026-04-15-older.meta.json").exists()
    assert not (ch / "2026-04-20-newer.meta.json").exists()
    # The healthy transcript survives untouched under the canonical prefix.
    assert (ch / "2026-04-15-older.transcript.md").read_text() == "older transcript, healthy"


def test_dedupe_apply_flipped_winner_still_merges_alt_titles_and_topics(tmp_path):
    """The severity-based flip must not disturb the surrounding mechanics:
    alt_titles merge and topics union still work with the clean-older meta
    as canonical."""
    ch = tmp_path / "ch"
    _make_dupe_group_quality_aware(ch, "2026-04-15-older", "2026-04-20-newer")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    canonical = json.loads((ch / "2026-04-15-older.meta.json").read_text())
    assert canonical["title"] == "Healthy Older Title"
    assert canonical["alt_titles"] == ["Severe Rerun Title"]
    # concepts mode: only the (now-canonical) older meta had it, so it's
    # already present and no artifact move was needed for it; the union of
    # modes_completed still reflects both losers' completed modes.
    assert set(canonical["modes_completed"]) == {"scan", "transcript", "concepts"}
    # topics union (issue #146 mechanics, unaffected by the #159 flip): each
    # meta's own topic is present, not overwritten by the other's.
    assert set(canonical["topics"]) == {"ai-agents", "safety"}


def test_dedupe_apply_flipped_winner_moves_missing_mode_artifact_from_severe_loser(tmp_path):
    """If the severe-flagged (and thus non-canonical) meta is the only one
    with a given mode's artifact, that artifact must still be moved onto the
    canonical prefix rather than deleted with the rest of the loser's
    siblings - the artifact-move mechanics are untouched by the flip.

    Issue #159 dual-review item 1 ("flag laundering"): the moved transcript
    IS the severe loser's transcript, so canonical's meta must inherit its
    per-mode provenance - both the fixed fields and an arbitrary extra
    transcript_* field, proving the generic prefix sweep in
    `_mode_provenance_fields` (not just the hand-enumerated list)."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-older",
        {
            "video_id": "v1",
            "title": "Older, clean, scan-only",
            "published": "2026-04-15",
            "processed": "2026-04-18T00:00:00+00:00",
            "modes_completed": ["scan"],
        },
    )
    _touch(ch / "2026-04-15-older.mindmap.md", "older mindmap")
    _write_meta(
        ch,
        "2026-04-20-newer-severe",
        {
            "video_id": "v1",
            "title": "Newer, severe, has transcript",
            "published": "2026-04-15",
            "processed": "2026-04-20T00:00:00+00:00",
            "modes_completed": ["scan", "transcript"],
            "transcript_status": "partial",
            "transcript_quality_flags": ["blind_gap_severe"],
            "transcript_max_blind_gap_seconds": 340,
            "transcript_provenance_note": "recovered via captions fallback",
        },
    )
    _touch(ch / "2026-04-20-newer-severe.mindmap.md", "newer mindmap")
    _touch(ch / "2026-04-20-newer-severe.transcript.md", "newer transcript content")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    # Older/clean survives as canonical, despite starting less complete.
    assert (ch / "2026-04-15-older.meta.json").exists()
    assert not (ch / "2026-04-20-newer-severe.meta.json").exists()
    # But the transcript artifact only the severe loser had is preserved by
    # moving it onto the canonical prefix - content is not lost.
    assert (ch / "2026-04-15-older.transcript.md").read_text() == "newer transcript content"
    canonical = json.loads((ch / "2026-04-15-older.meta.json").read_text())
    assert set(canonical["modes_completed"]) == {"scan", "transcript"}
    # The moved artifact's provenance must land on canonical - a severe
    # transcript must stay severe-labeled, never laundered clean by the flip.
    assert canonical["transcript_status"] == "partial"
    assert canonical["transcript_quality_flags"] == ["blind_gap_severe"]
    assert canonical["transcript_max_blind_gap_seconds"] == 340
    assert canonical["transcript_provenance_note"] == "recovered via captions fallback"


def test_dedupe_apply_disk_verifies_canonical_claim_before_computing_missing_modes(tmp_path):
    """Issue #159 dual-review item 2 ("empty-shell winner"): the clean-older
    meta claims a transcript mode whose .transcript.md was since deleted by
    hand (the exact #159 remediation flow - an operator manually removing a
    bad artifact without also editing modes_completed). The flip still picks
    the clean meta as canonical by its meta-level standing (no
    transcript_quality_flags key at all), but before computing which modes
    are "missing" from the loser, dedupe must verify the canonical's claim
    against disk - otherwise the loser's genuine (severe) transcript is
    deleted outright and the meta still claims a complete transcript, so the
    video is never re-queued."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-older-shell",
        {
            "video_id": "v1",
            "title": "Older, clean-labeled, but transcript file is gone",
            "published": "2026-04-15",
            "processed": "2026-04-18T00:00:00+00:00",
            "modes_completed": ["scan", "transcript"],
            # No transcript_quality_flags -> "clean" at the meta level, even
            # though the artifact it claims no longer exists on disk.
        },
    )
    _touch(ch / "2026-04-15-older-shell.mindmap.md", "older mindmap")
    # Deliberately NO 2026-04-15-older-shell.transcript.md on disk.

    _write_meta(
        ch,
        "2026-04-20-newer-severe",
        {
            "video_id": "v1",
            "title": "Newer, severe, has the only real transcript",
            "published": "2026-04-15",
            "processed": "2026-04-20T00:00:00+00:00",
            "modes_completed": ["scan", "transcript"],
            "transcript_status": "partial",
            "transcript_quality_flags": ["blind_gap_severe"],
        },
    )
    _touch(ch / "2026-04-20-newer-severe.mindmap.md", "newer mindmap")
    _touch(ch / "2026-04-20-newer-severe.transcript.md", "the only real transcript content")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    # Clean-labeled shell still wins the top-level pick...
    assert (ch / "2026-04-15-older-shell.meta.json").exists()
    assert not (ch / "2026-04-20-newer-severe.meta.json").exists()

    # ...but the loser's REAL transcript survives - it is not deleted with
    # the rest of the loser's siblings just because the canonical falsely
    # claimed to already have one.
    assert (ch / "2026-04-15-older-shell.transcript.md").read_text() == "the only real transcript content"

    # And the canonical meta now honestly records the severe provenance of
    # the transcript it actually has on disk (item 1 + item 2 together).
    canonical = json.loads((ch / "2026-04-15-older-shell.meta.json").read_text())
    assert canonical["transcript_status"] == "partial"
    assert canonical["transcript_quality_flags"] == ["blind_gap_severe"]
    assert set(canonical["modes_completed"]) == {"scan", "transcript"}


def test_dedupe_apply_partial_collision_never_credits_mode_on_sidecar_alone(tmp_path):
    """Issue #159 dual-review ROUND 2, P1 (the adversarial re-verify's own
    executed repro): a PARTIAL destination collision must not credit a mode.

    The canonical prefix has a STALE, unclaimed `.transcript.md` sitting on
    disk (an orphan from some earlier run - canonical's own meta never
    claims "transcript"). The severe loser has BOTH the real
    `.transcript.md` primary artifact AND a `.transcript.raw.txt` sidecar.
    When dedupe tries to move the loser's "transcript" mode onto canonical:
    the PRIMARY move is blocked (stale file already occupies the
    destination), but the SIDECAR moves cleanly (no collision there). Before
    this fix, `moved_any` flipped True on the sidecar alone, crediting the
    mode, copying the loser's severe provenance onto a meta whose actual
    transcript is the unrelated stale file, and the end-of-group sweep would
    then delete the loser's real transcript outright. None of that may
    happen now: the mode must not be credited, no provenance may be copied,
    and the loser's real transcript.md must survive on disk (preserved from
    the sweep, since it never actually moved)."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-canonical",
        {
            "video_id": "v1",
            "title": "Canonical, clean, does not claim transcript",
            "published": "2026-04-15",
            "processed": "2026-04-18T00:00:00+00:00",
            "modes_completed": ["scan"],
        },
    )
    _touch(ch / "2026-04-15-canonical.mindmap.md", "canonical mindmap")
    # A stale, unclaimed leftover .transcript.md already sits at the
    # canonical prefix - not referenced by modes_completed at all.
    _touch(ch / "2026-04-15-canonical.transcript.md", "STALE unrelated content, not a real transcript")

    _write_meta(
        ch,
        "2026-04-20-severe-loser",
        {
            "video_id": "v1",
            "title": "Severe loser with the real transcript and a raw sidecar",
            "published": "2026-04-15",
            "processed": "2026-04-20T00:00:00+00:00",
            "modes_completed": ["scan", "transcript"],
            "transcript_status": "partial",
            "transcript_quality_flags": ["monolithic_severe"],
        },
    )
    _touch(ch / "2026-04-20-severe-loser.mindmap.md", "loser mindmap")
    _touch(ch / "2026-04-20-severe-loser.transcript.md", "the loser's real transcript content")
    _touch(ch / "2026-04-20-severe-loser.transcript.raw.txt", "raw sidecar content")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    # Canonical survives (it was already clean and the group's pick).
    assert (ch / "2026-04-15-canonical.meta.json").exists()
    assert not (ch / "2026-04-20-severe-loser.meta.json").exists()

    # The stale canonical file is untouched - the collision that blocked
    # the primary move must not have overwritten it.
    assert (ch / "2026-04-15-canonical.transcript.md").read_text() == "STALE unrelated content, not a real transcript"

    # The loser's REAL transcript survives on disk (not deleted by the
    # sweep) precisely because it never actually moved.
    assert (ch / "2026-04-20-severe-loser.transcript.md").read_text() == "the loser's real transcript content"

    # No provenance was laundered onto canonical, and the mode was not
    # credited: canonical's meta must not claim "transcript" nor carry any
    # of the loser's transcript_* fields.
    canonical = json.loads((ch / "2026-04-15-canonical.meta.json").read_text())
    assert "transcript" not in canonical["modes_completed"]
    assert "transcript_status" not in canonical
    assert "transcript_quality_flags" not in canonical


def test_move_missing_mode_artifacts_does_not_credit_mode_when_only_sidecar_moves(tmp_path):
    """Unit-level companion, isolating `_move_missing_mode_artifacts` itself
    from the rest of the dedupe pipeline: gate `moved_modes` on the
    PRIMARY pattern moving, never a sidecar alone."""
    ch = tmp_path / "ch"
    ch.mkdir()
    _touch(ch / "canonical.transcript.md", "stale canonical content, blocks the primary move")
    _touch(ch / "loser.transcript.md", "loser's real primary transcript")
    _touch(ch / "loser.transcript.raw.txt", "loser's sidecar, no collision here")

    moved, blocked = vi._move_missing_mode_artifacts(ch, "canonical", "loser", {"transcript"})

    assert moved == set(), "the primary was blocked, so the mode must not be credited"
    assert (ch / "loser.transcript.md") in blocked
    # Primary stayed in place (blocked); sidecar moved cleanly (no collision).
    assert (ch / "loser.transcript.md").read_text() == "loser's real primary transcript"
    assert not (ch / "loser.transcript.raw.txt").exists(), "the sidecar, having no collision, still moves"
    assert (ch / "canonical.transcript.raw.txt").exists()
    assert (ch / "canonical.transcript.md").read_text() == "stale canonical content, blocks the primary move"


def test_dedupe_apply_three_meta_mixed_severity_group_best_clean_wins(tmp_path):
    """Three-way group: two clean metas and one severe. The severe meta must
    never win regardless of recency, and among the two clean metas the
    pre-#159 tie-break (latest processed) still decides."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-10-clean-oldest",
        {
            "video_id": "v1",
            "title": "Clean, oldest",
            "published": "2026-04-10",
            "processed": "2026-04-11T00:00:00+00:00",
            "modes_completed": ["scan"],
        },
    )
    _touch(ch / "2026-04-10-clean-oldest.mindmap.md", "oldest clean mindmap")
    _write_meta(
        ch,
        "2026-04-15-clean-best",
        {
            "video_id": "v1",
            "title": "Clean, best (latest of the two clean)",
            "published": "2026-04-10",
            "processed": "2026-04-16T00:00:00+00:00",
            "modes_completed": ["scan"],
        },
    )
    _touch(ch / "2026-04-15-clean-best.mindmap.md", "best clean mindmap")
    _write_meta(
        ch,
        "2026-04-20-severe-newest",
        {
            "video_id": "v1",
            "title": "Severe, newest overall",
            "published": "2026-04-10",
            "processed": "2026-04-20T00:00:00+00:00",
            "modes_completed": ["scan"],
            "transcript_quality_flags": ["monolithic_severe"],
        },
    )
    _touch(ch / "2026-04-20-severe-newest.mindmap.md", "severe mindmap")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    # The best CLEAN meta wins, even though the severe meta is newer overall.
    assert (ch / "2026-04-15-clean-best.meta.json").exists()
    assert not (ch / "2026-04-10-clean-oldest.meta.json").exists()
    assert not (ch / "2026-04-20-severe-newest.meta.json").exists()


def test_pick_canonical_three_metas_mixed_severity_excludes_severe():
    """Unit-level companion: `_pick_canonical` itself, not just the
    end-to-end command, must exclude the severe entry from contention."""
    clean_oldest = _meta_tuple("2026-04-10-a", processed="2026-04-11T00:00:00+00:00", modes_completed=["scan"])
    clean_best = _meta_tuple("2026-04-15-b", processed="2026-04-16T00:00:00+00:00", modes_completed=["scan"])
    severe_newest = _meta_tuple(
        "2026-04-20-c",
        processed="2026-04-20T00:00:00+00:00",
        modes_completed=["scan"],
        transcript_quality_flags=["monolithic_severe"],
    )
    canonical_path, _ = vi._pick_canonical([clean_oldest, clean_best, severe_newest])
    assert canonical_path.name == "2026-04-15-b.meta.json"


def test_dedupe_apply_both_severe_group_end_to_end_newer_wins(tmp_path):
    """End-to-end companion to `test_pick_canonical_both_severe_newer_wins`:
    when every meta in the group is severe, the pre-#159 tie-break (latest
    processed) still governs which one survives `dedupe --apply`."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-severe-older",
        {
            "video_id": "v1",
            "title": "Severe, older",
            "published": "2026-04-15",
            "processed": "2026-04-18T00:00:00+00:00",
            "modes_completed": ["scan"],
            "transcript_quality_flags": ["blind_gap_severe"],
        },
    )
    _touch(ch / "2026-04-15-severe-older.mindmap.md", "older severe mindmap")
    _write_meta(
        ch,
        "2026-04-20-severe-newer",
        {
            "video_id": "v1",
            "title": "Severe, newer",
            "published": "2026-04-15",
            "processed": "2026-04-20T00:00:00+00:00",
            "modes_completed": ["scan"],
            "transcript_quality_flags": ["monolithic_severe"],
        },
    )
    _touch(ch / "2026-04-20-severe-newer.mindmap.md", "newer severe mindmap")

    vi.cmd_dedupe(Namespace(channel=None, apply=True), _make_config(tmp_path, ["ch"]))

    assert (ch / "2026-04-20-severe-newer.meta.json").exists()
    assert not (ch / "2026-04-15-severe-older.meta.json").exists()
    canonical = json.loads((ch / "2026-04-20-severe-newer.meta.json").read_text())
    assert canonical["transcript_quality_flags"] == ["monolithic_severe"]


def test_move_missing_mode_artifacts_warns_and_skips_when_destination_already_exists(tmp_path, caplog):
    """Issue #159 dual-review item 4: a stale file blocking a move used to
    fail silently. It must now log a WARNING naming both paths, leave the
    pre-existing destination content untouched, and not credit the mode as
    moved."""
    ch = tmp_path / "ch"
    ch.mkdir()
    _touch(ch / "loser.mindmap.md", "loser mindmap content")
    _touch(ch / "canonical.mindmap.md", "canonical's own stale mindmap")

    with caplog.at_level("WARNING"):
        moved, blocked = vi._move_missing_mode_artifacts(ch, "canonical", "loser", {"scan"})

    assert moved == set(), "a move that hit an existing destination must not be credited as moved"
    assert blocked == {ch / "loser.mindmap.md"}, "the blocked source must be reported so the sweep can preserve it"
    assert (ch / "canonical.mindmap.md").read_text() == "canonical's own stale mindmap"
    assert (ch / "loser.mindmap.md").exists(), "source is left in place when the move is skipped"
    assert any("already exists" in rec.message for rec in caplog.records)


def test_cmd_dedupe_dry_run_logs_severity_standing(tmp_path, caplog):
    """Issue #159 dual-review item 5: a quality flip must be auditable from
    the dry-run log before a destructive --apply, so the per-meta lines
    carry a visible clean/severe standing."""
    ch = tmp_path / "ch"
    _make_dupe_group_quality_aware(ch, "2026-04-15-older", "2026-04-20-newer")

    with caplog.at_level("INFO"):
        vi.cmd_dedupe(Namespace(channel=None, apply=False), _make_config(tmp_path, ["ch"]))

    messages = "\n".join(rec.message for rec in caplog.records)
    assert "[clean]" in messages
    assert "[severe]" in messages


# ---------------------------------------------------------------------------
# Pre-scan alt_title rotation recorder
# ---------------------------------------------------------------------------


def test_record_alt_title_on_rotation_writes_new_title_to_existing_meta(tmp_path):
    """When scan sees video_id=X with a title that differs from the existing
    meta's title, append the new title to alt_titles (first occurrence)."""
    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-first",
        {
            "video_id": "v1",
            "title": "First Title",
            "published": "2026-04-15",
            "processed": "2026-04-15T00:00:00+00:00",
            "modes_completed": ["scan"],
        },
    )

    rotated = {"video_id": "v1", "title": "Rotated Title", "published": "2026-04-15"}
    vi.record_alt_title_if_rotated(tmp_path, "ch", rotated)

    meta = json.loads((ch / "2026-04-15-first.meta.json").read_text())
    assert meta["alt_titles"] == ["Rotated Title"]
    assert meta["title"] == "First Title"  # canonical title untouched


def test_record_alt_title_noop_when_title_unchanged(tmp_path):
    ch = tmp_path / "ch"
    meta_path = _write_meta(
        ch,
        "2026-04-15-first",
        {
            "video_id": "v1",
            "title": "First Title",
            "published": "2026-04-15",
            "processed": "2026-04-15T00:00:00+00:00",
        },
    )
    before = meta_path.read_text()

    same = {"video_id": "v1", "title": "First Title", "published": "2026-04-15"}
    vi.record_alt_title_if_rotated(tmp_path, "ch", same)

    assert meta_path.read_text() == before


def test_record_alt_title_noop_when_new_title_already_in_alts(tmp_path):
    ch = tmp_path / "ch"
    meta_path = _write_meta(
        ch,
        "2026-04-15-first",
        {
            "video_id": "v1",
            "title": "First Title",
            "published": "2026-04-15",
            "processed": "2026-04-15T00:00:00+00:00",
            "alt_titles": ["Rotated Title"],
        },
    )
    before = meta_path.read_text()

    seen_again = {"video_id": "v1", "title": "Rotated Title", "published": "2026-04-15"}
    vi.record_alt_title_if_rotated(tmp_path, "ch", seen_again)

    assert meta_path.read_text() == before


# ---------------------------------------------------------------------------
# cmd_scan integration: --dry-run must not mutate any meta.json
# ---------------------------------------------------------------------------


def test_scan_dry_run_does_not_call_alt_title_recorder(tmp_path, monkeypatch):
    """P1 guard (PR #31 review): dry-run promises preview-only semantics.
    The pre-scan alt_title recorder mutates meta.json when it fires, which
    would silently break that contract for users who expect --dry-run to
    make no disk changes. This test locks the gate in place."""
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")

    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
    monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
    # Unit 3 (skip-shorts) added an enrich_with_durations call inside cmd_scan
    # that runs regardless of dry_run. Stub it so these tests don't crash on
    # the None youtube client. Returning all-None durations means is_short()
    # falls through to the URL check, which is also stubbed below.
    monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _vid: False)

    rotated = [{"video_id": "v1", "title": "Rotated", "published": "2026-04-15"}]
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: rotated)

    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-original",
        {
            "video_id": "v1",
            "title": "Original",
            "published": "2026-04-15",
            "processed": "2026-04-15T00:00:00+00:00",
        },
    )
    _touch(ch / "2026-04-15-original.mindmap.md")

    call_count = {"n": 0}
    original_recorder = vi.record_alt_title_if_rotated

    def counting_recorder(*a, **kw):
        call_count["n"] += 1
        return original_recorder(*a, **kw)

    monkeypatch.setattr(vi, "record_alt_title_if_rotated", counting_recorder)

    args = Namespace(
        dry_run=True,
        channel=None,
        force=False,
        since=None,
        model=None,
    )
    config = {
        "output_dir": str(tmp_path),
        "channels": [{"name": "ch", "url": "https://example.com/ch"}],
    }

    vi.cmd_scan(args, config)

    assert call_count["n"] == 0, "record_alt_title_if_rotated must not fire on --dry-run"


def test_scan_without_dry_run_calls_alt_title_recorder_on_rotation(tmp_path, monkeypatch):
    """Positive pair: when not in dry-run and the incoming title has rotated,
    the recorder fires so the alt_title capture is wired. Gemini work is
    mocked out to avoid network; we only assert the pre-scan hook runs."""
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    monkeypatch.setenv("YOUTUBE_API_KEY", "test")

    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
    monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
    monkeypatch.setattr(vi, "get_channel_id", lambda yt, url: ("chid", "ChTitle"))
    # Unit 3 (skip-shorts) added an enrich_with_durations call inside cmd_scan
    # that runs regardless of dry_run. Stub it so these tests don't crash on
    # the None youtube client. Returning all-None durations means is_short()
    # falls through to the URL check, which is also stubbed below.
    monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
    monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
    monkeypatch.setattr(vi, "_is_youtube_short_url", lambda _vid: False)

    rotated = [{"video_id": "v1", "title": "Rotated", "published": "2026-04-15"}]
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: rotated)

    ch = tmp_path / "ch"
    _write_meta(
        ch,
        "2026-04-15-original",
        {
            "video_id": "v1",
            "title": "Original",
            "published": "2026-04-15",
            "processed": "2026-04-15T00:00:00+00:00",
        },
    )
    _touch(ch / "2026-04-15-original.mindmap.md")

    call_count = {"n": 0}
    original_recorder = vi.record_alt_title_if_rotated

    def counting_recorder(*a, **kw):
        call_count["n"] += 1
        return original_recorder(*a, **kw)

    monkeypatch.setattr(vi, "record_alt_title_if_rotated", counting_recorder)

    args = Namespace(
        dry_run=False,
        channel=None,
        force=False,
        since=None,
        model=None,
    )
    config = {
        "output_dir": str(tmp_path),
        "channels": [{"name": "ch", "url": "https://example.com/ch"}],
    }

    vi.cmd_scan(args, config)

    assert call_count["n"] == 1, "recorder must fire in a non-dry-run scan"
    meta = json.loads((ch / "2026-04-15-original.meta.json").read_text())
    assert meta["alt_titles"] == ["Rotated"]
