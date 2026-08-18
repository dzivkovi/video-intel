"""repair-metas rebuilds artifacts that have NO meta.json at all.

The first pass of `repair-metas` walks existing `*.meta.json` files, so a
transcript/mindmap pair with no meta beside it is invisible to it - and to every
other command. That combination is worse than an identity-less meta:

  - `scan` sees `<prefix>.transcript.md` on disk and calls the video processed,
    so it is never re-queued;
  - `concepts` iterates `*.meta.json`, so it cannot see the video at all and
    reports "All concepts up to date" - a false success;
  - the video therefore never reaches `taxonomy.json` or the search index.

Observed on 7 artifacts in one channel from a single failed batch run, stranded
for ~2 months. Recovery is free: the artifact headers carry the source URL,
title and date, so no Gemini call is needed.
"""

from __future__ import annotations

import argparse
import json

import pytest

import video_intel as v

TRANSCRIPT = """# Transcript: Some Real Title

**Source:** https://www.youtube.com/watch?v=abcDEF12345
**Published:** 2026-06-18
**Processed:** 2026-06-21 13:43 UTC
**Status:** Complete

[00:00] Speaker: "hello"
"""

MINDMAP = """<!-- video: https://www.youtube.com/watch?v=abcDEF12345 -->
<!-- title: Some Real Title -->
<!-- published: 2026-06-18 -->

## A Branch

* **Thing**
  - point (0:00)
"""


def _args(apply=False, channel=None):
    return argparse.Namespace(apply=apply, channel=channel)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    out = tmp_path / "corpus"
    (out / "chan").mkdir(parents=True)
    monkeypatch.setattr(v, "resolve_output_dir", lambda cfg, **kw: out)
    monkeypatch.setattr(v, "_invalidate_video_id_cache", lambda: None)
    return out / "chan"


def _write(d, prefix, *, transcript=True, mindmap=True, concepts=False, meta=None):
    if transcript:
        (d / f"{prefix}.transcript.md").write_text(TRANSCRIPT, encoding="utf-8")
    if mindmap:
        (d / f"{prefix}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    if concepts:
        (d / f"{prefix}.concepts.json").write_text('{"concepts": []}', encoding="utf-8")
    if meta is not None:
        (d / f"{prefix}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


class TestRebuildsOrphanedArtifacts:
    def test_meta_is_created_with_full_identity(self, corpus):
        _write(corpus, "2026-06-18-x")
        v.cmd_repair_metas(_args(apply=True), {})
        meta = json.loads((corpus / "2026-06-18-x.meta.json").read_text(encoding="utf-8"))
        # Issue #66: an identity-less meta is skipped by _load_video_id_index,
        # so writing one would re-create the invisibility this pass cures.
        for field in ("video_url", "video_id", "channel", "title", "published"):
            assert meta.get(field), f"rebuilt meta is missing {field}: {meta}"
        assert meta["video_id"] == "abcDEF12345"
        assert meta["channel"] == "chan"

    def test_modes_completed_reflects_only_what_is_on_disk(self, corpus):
        _write(corpus, "a", transcript=True, mindmap=True, concepts=False)
        _write(corpus, "b", transcript=True, mindmap=True, concepts=True)
        v.cmd_repair_metas(_args(apply=True), {})
        a = json.loads((corpus / "a.meta.json").read_text(encoding="utf-8"))
        b = json.loads((corpus / "b.meta.json").read_text(encoding="utf-8"))
        assert a["modes_completed"] == ["transcript", "mindmap"], (
            "claimed a mode with no artifact on disk; a meta asserting concepts that "
            "do not exist would hide the gap it was meant to expose"
        )
        assert b["modes_completed"] == ["transcript", "mindmap", "concepts"]

    def test_provenance_is_recorded(self, corpus):
        _write(corpus, "x")
        v.cmd_repair_metas(_args(apply=True), {})
        meta = json.loads((corpus / "x.meta.json").read_text(encoding="utf-8"))
        assert meta.get("meta_reconstructed") is True
        assert meta.get("meta_reconstructed_from") == "transcript_header"

    def test_falls_back_to_mindmap_header_when_no_transcript(self, corpus):
        _write(corpus, "mm-only", transcript=False, mindmap=True)
        v.cmd_repair_metas(_args(apply=True), {})
        meta = json.loads((corpus / "mm-only.meta.json").read_text(encoding="utf-8"))
        assert meta["video_id"] == "abcDEF12345"
        assert meta["meta_reconstructed_from"] == "mindmap_header"


class TestSafety:
    def test_dry_run_writes_nothing(self, corpus):
        _write(corpus, "x")
        v.cmd_repair_metas(_args(apply=False), {})
        assert not (corpus / "x.meta.json").exists(), "dry run must not write"

    def test_existing_meta_is_never_overwritten(self, corpus):
        original = {"video_id": "KEEPME1234x", "alt_titles": ["rotated title"], "skip_modes": ["transcript"]}
        _write(corpus, "x", meta=original)
        v.cmd_repair_metas(_args(apply=True), {})
        after = json.loads((corpus / "x.meta.json").read_text(encoding="utf-8"))
        assert after == original, (
            "an existing meta was overwritten. alt_titles (title-rotation history) and "
            "skip_modes (deliberate operator suppression) exist nowhere else."
        )

    def test_refuses_to_write_without_a_recoverable_video_id(self, corpus):
        (corpus / "bad.transcript.md").write_text("# Transcript: No source line here\n", encoding="utf-8")
        (corpus / "bad.mindmap.md").write_text("## Branch\n\n* thing\n", encoding="utf-8")
        v.cmd_repair_metas(_args(apply=True), {})
        assert not (corpus / "bad.meta.json").exists(), (
            "wrote a meta with no video_id; _load_video_id_index skips those, so the "
            "video stays invisible AND now looks repaired"
        )

    def test_channel_filter_is_honored(self, corpus):
        other = corpus.parent / "other"
        other.mkdir()
        _write(corpus, "mine")
        _write(other, "theirs")
        v.cmd_repair_metas(_args(apply=True, channel="chan"), {})
        assert (corpus / "mine.meta.json").exists()
        assert not (other / "theirs.meta.json").exists()

    def test_underscore_dirs_are_skipped(self, corpus):
        special = corpus.parent / "_briefings"
        special.mkdir()
        _write(special, "not-a-video")
        v.cmd_repair_metas(_args(apply=True), {})
        assert not (special / "not-a-video.meta.json").exists(), (
            "_-prefixed dirs are not channels; writing corpus metas into _briefings/ "
            "or _reports/ would corrupt those surfaces"
        )


class TestMindmapHeaderParser:
    def test_parses_the_three_header_comments(self, tmp_path):
        p = tmp_path / "x.mindmap.md"
        p.write_text(MINDMAP, encoding="utf-8")
        got = v._identity_from_mindmap_header(p)
        assert got["video_id"] == "abcDEF12345"
        assert got["title"] == "Some Real Title"
        assert got["published"] == "2026-06-18"

    def test_returns_none_without_a_parseable_video_id(self, tmp_path):
        p = tmp_path / "x.mindmap.md"
        p.write_text("<!-- video: https://example.com/not-youtube -->\n## B\n", encoding="utf-8")
        assert v._identity_from_mindmap_header(p) is None

    def test_over_long_id_is_rejected_rather_than_truncated(self, tmp_path):
        """Mirrors the transcript parser: a non-canonical URL must fail, not
        silently yield a wrong 11-char id."""
        p = tmp_path / "x.mindmap.md"
        p.write_text("<!-- video: https://www.youtube.com/watch?v=TOOLONGIDENTIFIER123 -->\n", encoding="utf-8")
        assert v._identity_from_mindmap_header(p) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert v._identity_from_mindmap_header(tmp_path / "nope.mindmap.md") is None
