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
