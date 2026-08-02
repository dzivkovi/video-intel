"""Tests for the mindmap-side prompt=0 confabulation guard (issue #119).

Issue #60 shipped the guard on the transcript path only (locked by
`tests/test_captions_failover.py::TestConfabGuard`). The video-mindmap path
logged the same usage metadata but never inspected it, so a `prompt=0`
response - Gemini ingested zero video tokens and generated plausible content
from priors - was written as a healthy `.mindmap.md` and flowed on into
concepts extraction, `taxonomy.json`, and the vector index.

What is locked here:
- `source="video"` + `prompt == 0` writes NO artifact and records `last_error`.
- The guard is `== 0`, never falsy/missing: unreadable usage metadata (a `None`
  return from `log_usage_metadata`) must NOT trip it.
- A positive prompt count behaves exactly as before the guard existed.
- The temp file (`.md.tmp`) is never left behind on the guarded path.
- `source="transcript"` is deliberately out of scope (text-only call; issue #54's
  inversion means it is not the confabulation vector).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel
from video_intel import process_mindmap

MINDMAP_BODY = "## Topic\n\n* bullet (0:00)\n"


class _Usage:
    def __init__(self, prompt: int) -> None:
        self.prompt_token_count = prompt
        self.cached_content_token_count = 0
        self.thoughts_token_count = 0
        self.candidates_token_count = 100
        self.total_token_count = prompt + 100


class _Resp:
    """Gemini response whose usage_metadata reports `prompt` ingested tokens."""

    def __init__(self, prompt: int) -> None:
        self.usage_metadata = _Usage(prompt)


class _RespNoUsage:
    """Gemini response with unreadable usage metadata (log_usage_metadata -> None)."""

    usage_metadata = None


@pytest.fixture
def sample_video():
    return {
        "video_id": "9exWJmbKeMo",
        "url": "https://www.youtube.com/watch?v=9exWJmbKeMo",
        "title": "Live Compound Engineering Q and A",
        "published": "2026-07-24",
    }


@pytest.fixture
def fake_types():
    return SimpleNamespace(
        MediaResolution=SimpleNamespace(
            MEDIA_RESOLUTION_LOW="MEDIA_RESOLUTION_LOW",
            MEDIA_RESOLUTION_HIGH="MEDIA_RESOLUTION_HIGH",
        )
    )


def _stub_gemini_video(monkeypatch, response):
    """Stub call_gemini so it reports `response` through the on_response callback."""

    def fake_call_gemini(client, types, media_uri, prompt_text, model, **kw):
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(response)
        return MINDMAP_BODY

    monkeypatch.setattr(video_intel, "call_gemini", fake_call_gemini)


def _run(video, types, tmp_path, **kw):
    return process_mindmap(
        client=MagicMock(),
        types=types,
        video=video,
        prompt_text="MINDMAP-PROMPT",
        model="stub-model",
        output_dir=tmp_path,
        channel_name="demo",
        source="video",
        **kw,
    )


def _paths(tmp_path, prefix="2026-07-24-live-compound-engineering-q-and-a"):
    channel_dir = tmp_path / "demo"
    return (
        channel_dir / f"{prefix}.mindmap.md",
        channel_dir / f"{prefix}.meta.json",
        channel_dir / f"{prefix}.md.tmp",
    )


class TestVideoMindmapConfabGuard:
    def test_prompt_zero_writes_no_mindmap(self, sample_video, fake_types, tmp_path, monkeypatch):
        _stub_gemini_video(monkeypatch, _Resp(0))

        prefix, status = _run(sample_video, fake_types, tmp_path)

        mindmap_path, _, _ = _paths(tmp_path)
        assert not mindmap_path.exists(), "a prompt=0 confabulation must never land as a .mindmap.md"
        assert status.startswith("error:"), f"expected an error-shaped status, got {status!r}"
        assert "confabulation" in status
        assert "prompt=0" in status
        assert prefix

    def test_prompt_zero_records_error_in_meta(self, sample_video, fake_types, tmp_path, monkeypatch):
        _stub_gemini_video(monkeypatch, _Resp(0))

        _run(sample_video, fake_types, tmp_path)

        _, meta_path, _ = _paths(tmp_path)
        assert meta_path.exists(), "the guard must surface via meta.json so the video can retry"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "confabulation" in meta["last_error"]
        assert "prompt=0" in meta["last_error"]
        assert "scan" not in meta.get("modes_completed", []), "a guarded run is not a completed mindmap"

    def test_guarded_meta_carries_full_identity(self, sample_video, fake_types, tmp_path, monkeypatch):
        """The guard raises so the existing except handler stamps identity like any other failure.

        An early `return resolved_prefix, "error: ..."` would skip that handler and leave a
        meta without video_id / channel / title / published - identity-less metas are what
        issue #66 had to go back and repair.
        """
        _stub_gemini_video(monkeypatch, _Resp(0))

        _run(sample_video, fake_types, tmp_path)

        _, meta_path, _ = _paths(tmp_path)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["video_id"] == sample_video["video_id"]
        assert meta["video_url"] == sample_video["url"]
        assert meta["channel"] == "demo"
        assert meta["title"] == sample_video["title"]
        assert meta["published"] == sample_video["published"]
        assert meta["last_error"]

    def test_prompt_zero_keeps_a_raw_forensic_sidecar(self, sample_video, fake_types, tmp_path, monkeypatch):
        """Mirrors the transcript path's .transcript.raw.txt: keep the evidence, not the artifact."""
        _stub_gemini_video(monkeypatch, _Resp(0))

        _run(sample_video, fake_types, tmp_path)

        mindmap_path, _, _ = _paths(tmp_path)
        raw_path = mindmap_path.with_name(mindmap_path.name.replace(".mindmap.md", ".mindmap.raw.txt"))
        assert raw_path.exists(), "the discarded response must be kept for forensics"
        assert raw_path.read_text(encoding="utf-8") == MINDMAP_BODY
        assert not mindmap_path.exists(), "the sidecar is not a licence to also write the artifact"

    def test_prompt_zero_leaves_no_tmp_file(self, sample_video, fake_types, tmp_path, monkeypatch):
        _stub_gemini_video(monkeypatch, _Resp(0))

        _run(sample_video, fake_types, tmp_path)

        _, _, tmp_file = _paths(tmp_path)
        assert not tmp_file.exists(), "the guard must fire before the temp write, not after"
        leftovers = list((tmp_path / "demo").glob("*.tmp"))
        assert leftovers == [], f"stray temp artifacts left behind: {leftovers}"

    def test_prompt_zero_does_not_clobber_an_existing_mindmap(self, sample_video, fake_types, tmp_path, monkeypatch):
        """A --force regeneration that confabulates must leave the good artifact intact."""
        mindmap_path, _, _ = _paths(tmp_path)
        mindmap_path.parent.mkdir(parents=True, exist_ok=True)
        mindmap_path.write_text("KNOWN GOOD MINDMAP\n", encoding="utf-8")
        _stub_gemini_video(monkeypatch, _Resp(0))

        _, status = _run(sample_video, fake_types, tmp_path, force=True)

        assert status.startswith("error:")
        assert mindmap_path.read_text(encoding="utf-8") == "KNOWN GOOD MINDMAP\n"


class TestGuardIsExactlyZeroNotFalsy:
    def test_unreadable_usage_metadata_does_not_trip_the_guard(self, sample_video, fake_types, tmp_path, monkeypatch):
        """log_usage_metadata returns None when usage is unreadable: never flag on missing data."""
        _stub_gemini_video(monkeypatch, _RespNoUsage())

        _, status = _run(sample_video, fake_types, tmp_path)

        mindmap_path, meta_path, _ = _paths(tmp_path)
        assert status == "done", f"missing usage data must not be treated as a confabulation, got {status!r}"
        assert mindmap_path.exists()
        assert json.loads(meta_path.read_text(encoding="utf-8"))["mindmap_source"] == "video"

    def test_positive_prompt_count_writes_the_artifact_unchanged(self, sample_video, fake_types, tmp_path, monkeypatch):
        _stub_gemini_video(monkeypatch, _Resp(563000))

        _, status = _run(sample_video, fake_types, tmp_path)

        mindmap_path, meta_path, tmp_file = _paths(tmp_path)
        assert status == "done"
        text = mindmap_path.read_text(encoding="utf-8")
        assert text.startswith(f"<!-- video: {sample_video['url']} -->")
        assert MINDMAP_BODY in text
        assert not tmp_file.exists()
        raw_path = mindmap_path.with_name(mindmap_path.name.replace(".mindmap.md", ".mindmap.raw.txt"))
        assert not raw_path.exists(), "the forensic sidecar is for discarded responses only"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["mindmap_source"] == "video"
        assert meta["video_id"] == sample_video["video_id"]
        assert "scan" in meta["modes_completed"]
        assert meta["last_error"] is None

    def test_sidecar_is_in_the_prune_shorts_allowlist(self):
        """A pruned Short must not leave the guard's sidecar orphaned in the channel folder."""
        assert "{prefix}.mindmap.raw.txt" in video_intel.PRUNE_SHORTS_DELETION_PATTERNS


class TestTranscriptSourceStaysOutOfScope:
    """source='transcript' is a text-only call: prompt=0 means something else there.

    Issue #119 scopes the fix to the video path deliberately. This test exists so a
    later 'symmetry' refactor that extends the guard to the text path has to argue
    for it explicitly rather than doing it by accident.
    """

    def test_prompt_zero_on_transcript_source_still_writes(self, sample_video, fake_types, tmp_path, monkeypatch):
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True, exist_ok=True)
        prefix = "2026-07-24-live-compound-engineering-q-and-a"
        (channel_dir / f"{prefix}.transcript.md").write_text("[00:00] Hi\n", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(json.dumps({"transcript_status": "ok"}), encoding="utf-8")

        def fake_call_gemini_text(client, types, content, model, **kw):
            on_response = kw.get("on_response")
            if on_response is not None:
                on_response(_Resp(0))
            return MINDMAP_BODY

        monkeypatch.setattr(video_intel, "call_gemini_text", fake_call_gemini_text)
        monkeypatch.setattr(
            video_intel,
            "call_gemini",
            lambda *a, **kw: pytest.fail("call_gemini must not run on source='transcript'"),
        )

        _, status = process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=sample_video,
            prompt_text="MINDMAP-FROM-TRANSCRIPT-PROMPT",
            model="stub-model",
            output_dir=tmp_path,
            channel_name="demo",
            source="transcript",
        )

        assert status == "done"
        assert (channel_dir / f"{prefix}.mindmap.md").exists()
