"""A mindmap-less channel must not report every video as new (#226).

`scan` writes no artifact of its own. Its completion marker is the MINDMAP,
which is why `is_processed(..., "scan", any_variant=True)` resolves to a
`{prefix}.mindmap*.md` check. A channel configured `mindmap_source: none`
never writes one, so every video inside the window came back as new on every
scan. Observed live on the first zero-Gemini ingest of `thenextnewthingai`:
`Found 33 videos, 33 new` on a run where all 33 transcripts already existed.

Zero cost, but the "new" count is the operator's only signal that a feed
moved, and here it was always wrong: `--dry-run` previewed 33 videos of work
that would not happen, and the mindmap executor was handed 33 closures that
could only return a skip string.

The risk in the fix is over-reach, so most of this file pins what must NOT
change: an ordinary channel keeps the mindmap marker, and a channel that
produces no artifact at all keeps the old meaning of "new".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi

FULL = {"auto_transcript": "all"}


class TestTheReportedCase:
    def test_mindmap_source_none_switches_the_marker_to_the_transcript(self):
        assert vi.scan_completion_mode({**FULL, "mindmap_source": "none"}) == "transcript"

    def test_auto_mindmap_none_does_the_same(self):
        """The legacy top-level gate reaches the identical dead end."""
        assert vi.scan_completion_mode({**FULL, "auto_mindmap": "none"}) == "transcript"


class TestWhatMustNotChange:
    @pytest.mark.parametrize("source", [None, "auto", "video", "transcript"])
    def test_an_ordinary_channel_keeps_the_mindmap_marker(self, source):
        cfg = dict(FULL)
        if source is not None:
            cfg["mindmap_source"] = source
        assert vi.scan_completion_mode(cfg) == "scan"

    def test_a_channel_that_writes_nothing_keeps_the_old_meaning(self):
        """Notify-only mode: both steps off, so no artifact can ever mark a
        video done. Returning "transcript" there would look for a file that
        is never written - the same bug, moved."""
        assert vi.scan_completion_mode({"auto_mindmap": "none"}) == "scan"
        assert vi.scan_completion_mode({"mindmap_source": "none", "auto_transcript": "none"}) == "scan"

    @pytest.mark.parametrize("bad", ["mindmaps", "", 0, ["none"], {"mode": "none"}, True])
    def test_a_malformed_mindmap_source_does_not_raise_here(self, bad):
        """Reporting a bad knob is validate_channel_knobs's job (#169). A
        counting helper must not become a new place for a config typo to
        abort a scan (#135) - note `{"mode": "none"}` fails the resolver's
        membership test with TypeError, not ValueError."""
        assert vi.scan_completion_mode({**FULL, "mindmap_source": bad}) == "scan"


class TestTheMarkerActuallyChangesTheCount:
    """Function-level agreement is not enough: `is_processed` has to accept
    the mode and resolve it to the right file."""

    def _channel(self, tmp_path, *, mindmap: bool, transcript: bool):
        chan = tmp_path / "chan"
        chan.mkdir(exist_ok=True)
        prefix = "2026-09-11-a-video"
        if mindmap:
            (chan / f"{prefix}.mindmap.md").write_text("m", encoding="utf-8")
        if transcript:
            (chan / f"{prefix}.transcript.md").write_text("t", encoding="utf-8")
        return {"video_id": "vid123", "title": "A Video", "published": "2026-09-11"}

    def test_transcript_only_video_reads_as_processed_under_the_new_marker(self, tmp_path):
        video = self._channel(tmp_path, mindmap=False, transcript=True)
        assert vi.is_processed(tmp_path, "chan", video, "transcript", any_variant=True) is True

    def test_and_read_as_unprocessed_under_the_old_one(self, tmp_path):
        """The bug, pinned. Without the fix this is what the scan asked."""
        video = self._channel(tmp_path, mindmap=False, transcript=True)
        assert vi.is_processed(tmp_path, "chan", video, "scan", any_variant=True) is False

    def test_a_genuinely_new_video_is_still_new(self, tmp_path):
        video = self._channel(tmp_path, mindmap=False, transcript=False)
        assert vi.is_processed(tmp_path, "chan", video, "transcript", any_variant=True) is False


class TestCallerLevel:
    """Drives the real `cmd_scan`. A helper that is correct but never reached
    is the same blind spot as a stub agreeing with its own assertion."""

    @pytest.fixture
    def scan(self, monkeypatch, tmp_path):
        def _run(channel_cfg, *, on_disk):
            chan = tmp_path / "chan"
            chan.mkdir(exist_ok=True)
            prefix = "2026-09-11-a-video"
            for suffix in on_disk:
                (chan / f"{prefix}.{suffix}").write_text("x", encoding="utf-8")

            videos = [
                {
                    "video_id": "vid123",
                    "title": "A Video",
                    "published": "2026-09-11",
                    "url": "https://www.youtube.com/watch?v=vid123",
                }
            ]
            submitted: list = []

            monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_k: tmp_path)
            monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **k: object())
            monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
            monkeypatch.setattr(vi, "create_client", lambda *_a, **_k: object())
            monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
            monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_k: "stub-model")
            monkeypatch.setattr(vi, "get_channel_id", lambda *_a, **_k: ("UCabc", "u"))
            monkeypatch.setattr(vi, "fetch_channel_videos", lambda *_a, **_k: list(videos))
            monkeypatch.setattr(vi, "enrich_with_durations", lambda _y, _ids: {})
            monkeypatch.setattr(vi, "fetch_preflight_status", lambda *_a, **_k: {})
            monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
            monkeypatch.setattr(vi, "record_alt_title_if_rotated", lambda *_a, **_k: False)
            monkeypatch.setattr(vi, "render_headline_digest", lambda *_a, **_k: None)

            def record_mindmap(*a, **k):
                submitted.append(k.get("prefix") or "called")
                return ("2026-09-11-a-video", "done")

            monkeypatch.setattr(vi, "process_mindmap", record_mindmap)
            monkeypatch.setattr(vi, "process_transcript", lambda *a, **k: ("2026-09-11-a-video", "done"))
            monkeypatch.setenv("GEMINI_API_KEY", "k")
            monkeypatch.setenv("YOUTUBE_API_KEY", "k")

            from types import SimpleNamespace

            args = SimpleNamespace(
                channel="chan",
                since=None,
                dry_run=False,
                force=False,
                model=None,
                prompt=None,
                media_resolution="low",
                chunk_minutes=None,
                transcript_source=None,
                topic=None,
            )
            config = {"channels": [{"name": "chan", "url": "https://youtube.com/@chan", **channel_cfg}]}
            vi.cmd_scan(args, config)
            return submitted

        return _run

    def test_a_transcript_only_video_is_not_re_reported_as_new(self, scan, caplog):
        """The headline. Pre-fix this logged 'Found 1 videos, 1 new'."""
        import logging

        with caplog.at_level(logging.INFO):
            submitted = scan(
                {"auto_transcript": "all", "mindmap_source": "none", "transcript_source": "yt-captions"},
                on_disk=["transcript.md"],
            )
        assert "Found 1 videos, 0 new" in caplog.text, caplog.text
        assert submitted == [], "the mindmap executor was handed work it can only skip"
