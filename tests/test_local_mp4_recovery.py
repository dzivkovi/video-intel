"""Tests for the local-MP4 channel-scoped recovery feature.

Covers the helpers and command paths added by the revision-4 plan:
- infer_channel_from_file_path: derive channel name from parent folder
- resolve_local_file_identity: priority-ordered identity resolution with G2 dedup
- cmd_transcript --file --channel: in-place routing with media_uri split
- cmd_mindmap  --file --channel: parity with transcript
- cmd_scan concept enumeration: glob-based pickup of local-recovery artifacts
- update_meta(mode="identity"): sentinel that skips modes_completed append
"""

import argparse
import json
from unittest.mock import MagicMock

import pytest

from video_intel import (
    cmd_mindmap,
    cmd_scan,
    cmd_transcript,
    infer_channel_from_file_path,
    resolve_local_file_identity,
    update_meta,
)

# ---------------------------------------------------------------------------
# infer_channel_from_file_path
# ---------------------------------------------------------------------------


class TestInferChannelFromFilePath:
    """Parent-folder inference against a configured channel list."""

    def _config(self, channel_names):
        return {"channels": [{"name": n, "url": f"https://youtube.com/@{n}"} for n in channel_names]}

    def test_file_under_configured_channel_folder_returns_channel_name(self, tmp_path):
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "Compound Engineering Camp.mkv"
        mp4.write_bytes(b"x")

        result = infer_channel_from_file_path(mp4, output_dir, self._config(["everyinc", "natebjones"]))

        assert result == "everyinc"

    def test_file_under_unconfigured_channel_folder_returns_none(self, tmp_path):
        output_dir = tmp_path / "video-intel"
        ghost_dir = output_dir / "ghostchannel"
        ghost_dir.mkdir(parents=True)
        mp4 = ghost_dir / "foo.mp4"
        mp4.write_bytes(b"x")

        result = infer_channel_from_file_path(mp4, output_dir, self._config(["everyinc"]))

        assert result is None

    def test_file_outside_output_dir_returns_none(self, tmp_path):
        output_dir = tmp_path / "video-intel"
        output_dir.mkdir()
        elsewhere = tmp_path / "downloads"
        elsewhere.mkdir()
        mp4 = elsewhere / "foo.mp4"
        mp4.write_bytes(b"x")

        result = infer_channel_from_file_path(mp4, output_dir, self._config(["everyinc"]))

        assert result is None

    def test_file_directly_under_output_dir_returns_none(self, tmp_path):
        output_dir = tmp_path / "video-intel"
        output_dir.mkdir()
        mp4 = output_dir / "loose.mp4"
        mp4.write_bytes(b"x")

        result = infer_channel_from_file_path(mp4, output_dir, self._config(["everyinc"]))

        assert result is None

    def test_file_nested_below_channel_folder_returns_none(self, tmp_path):
        """Only direct children of output_dir/<channel>/ qualify. Nested subfolders do not."""
        output_dir = tmp_path / "video-intel"
        nested = output_dir / "everyinc" / "archive"
        nested.mkdir(parents=True)
        mp4 = nested / "foo.mp4"
        mp4.write_bytes(b"x")

        result = infer_channel_from_file_path(mp4, output_dir, self._config(["everyinc"]))

        assert result is None

    def test_empty_channel_config_returns_none(self, tmp_path):
        output_dir = tmp_path / "video-intel"
        (output_dir / "everyinc").mkdir(parents=True)
        mp4 = output_dir / "everyinc" / "foo.mp4"
        mp4.write_bytes(b"x")

        result = infer_channel_from_file_path(mp4, output_dir, {"channels": []})

        assert result is None


# ---------------------------------------------------------------------------
# resolve_local_file_identity
# ---------------------------------------------------------------------------


def _make_args(
    *,
    channel=None,
    video_id=None,
    title=None,
    date=None,
    start=None,
    end=None,
    force=False,
    file=None,
    url=None,
    model=None,
    prompt=None,
):
    """Build an argparse.Namespace matching the transcript/mindmap subparsers."""
    return argparse.Namespace(
        channel=channel,
        video_id=video_id,
        title=title,
        date=date,
        start=start,
        end=end,
        force=force,
        file=file,
        url=url,
        model=model,
        prompt=prompt,
    )


class TestResolveLocalFileIdentity:
    """Priority-ordered identity resolution.

    Priority from the plan:
      1. Sibling meta.json for the same filename stem
      2. G2: channel-wide video_id match against canonical scan metas (adopts canonical prefix)
      3. Explicit CLI flags
      4. Parent-folder inference fills channel
      5. Filename stem = title
      6. Filename stem = video_id only if ^[A-Za-z0-9_-]{11}$
      7. mtime fallback for published
      8. video_url derived from video_id
    """

    def test_step_1_sibling_meta_wins(self, tmp_path):
        """Sibling meta.json next to the MP4 provides all identity fields."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "Compound Engineering Camp.mkv"
        mp4.write_bytes(b"x")
        sibling = channel_dir / "Compound Engineering Camp.meta.json"
        sibling.write_text(
            json.dumps(
                {
                    "video_id": "abc123XYZ_-",
                    "video_url": "https://www.youtube.com/watch?v=abc123XYZ_-",
                    "title": "The Real Title",
                    "published": "2026-03-15",
                    "channel": "everyinc",
                }
            )
        )

        args = _make_args()
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["video_id"] == "abc123XYZ_-"
        assert identity["url"] == "https://www.youtube.com/watch?v=abc123XYZ_-"
        assert identity["title"] == "The Real Title"
        assert identity["published"] == "2026-03-15"
        assert identity["channel"] == "everyinc"
        assert identity["published_source"] == "sibling_meta"
        assert identity["prefix"] == "Compound Engineering Camp"
        assert identity["channel_dir"] == channel_dir
        assert identity["meta_path"] == sibling

    def test_step_1_sibling_meta_honors_explicit_flag_overrides(self, tmp_path):
        """Regression: stale sibling meta must NOT silently shadow explicit CLI flags.

        Triggered in the wild on 2026-04-17: user passed --video-id --force to
        re-stamp a prior run's meta that had empty video_id="", but the sibling-meta
        step adopted the empty field verbatim because step 1 didn't honor flag
        overrides. Step 2 (G2) already did; step 1 now matches.
        """
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "Compound Engineering Camp.mkv"
        mp4.write_bytes(b"x")
        # Stale sibling: empty video_id/video_url from a prior run without --video-id
        (channel_dir / "Compound Engineering Camp.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "",
                    "video_url": "",
                    "title": "Compound Engineering Camp",
                    "published": "2026-04-17",
                    "channel": "everyinc",
                    "published_source": "mtime",
                }
            )
        )

        args = _make_args(video_id="lfML5OJc-CM", date="2026-04-18")
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        # Flags won on their specific fields
        assert identity["video_id"] == "lfML5OJc-CM"
        assert identity["url"] == "https://www.youtube.com/watch?v=lfML5OJc-CM"
        assert identity["published"] == "2026-04-18"
        assert identity["published_source"] == "cli_flag"
        # Sibling-meta-derived fields still win for non-flagged fields
        assert identity["title"] == "Compound Engineering Camp"
        assert identity["channel"] == "everyinc"
        # Prefix stays stem-based (no G2 dedup available here)
        assert identity["prefix"] == "Compound Engineering Camp"

    def test_step_1_sibling_meta_without_flags_still_adopts_verbatim(self, tmp_path):
        """Regression guard: no-flag path must stay unchanged after the override rule."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "foo.mkv"
        mp4.write_bytes(b"x")
        (channel_dir / "foo.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "storedVidID",
                    "video_url": "https://www.youtube.com/watch?v=storedVidID",
                    "title": "Stored Title",
                    "published": "2026-03-01",
                    "channel": "everyinc",
                }
            )
        )

        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=_make_args())

        assert identity["video_id"] == "storedVidID"
        assert identity["url"] == "https://www.youtube.com/watch?v=storedVidID"
        assert identity["title"] == "Stored Title"
        assert identity["published"] == "2026-03-01"
        assert identity["published_source"] == "sibling_meta"

    def test_step_2_g2_dedup_adopts_canonical_prefix(self, tmp_path):
        """When stem is a videoId matching a canonical scan meta, adopt that meta's prefix."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        canonical = channel_dir / "2026-04-16-once-you-vibe-code.meta.json"
        canonical.write_text(
            json.dumps(
                {
                    "video_id": "abcDEF12345",
                    "video_url": "https://www.youtube.com/watch?v=abcDEF12345",
                    "title": "Once You Vibe Code",
                    "published": "2026-04-16",
                    "channel": "everyinc",
                    "published_source": "youtube_api",
                }
            )
        )
        mp4 = channel_dir / "abcDEF12345.mp4"
        mp4.write_bytes(b"x")

        args = _make_args()
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["video_id"] == "abcDEF12345"
        assert identity["title"] == "Once You Vibe Code"
        assert identity["published"] == "2026-04-16"
        assert identity["published_source"] == "youtube_api"
        assert identity["prefix"] == "2026-04-16-once-you-vibe-code"
        assert identity["meta_path"] == canonical

    def test_step_2_g2_via_explicit_video_id_flag(self, tmp_path):
        """--video-id overrides stem for G2 match when stem is not a videoId."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        canonical = channel_dir / "2026-04-16-talk-title.meta.json"
        canonical.write_text(
            json.dumps(
                {
                    "video_id": "lookUpByArg1",
                    "video_url": "https://www.youtube.com/watch?v=lookUpByArg1",
                    "title": "Talk Title",
                    "published": "2026-04-16",
                    "channel": "everyinc",
                    "published_source": "youtube_api",
                }
            )
        )
        mp4 = channel_dir / "some random filename.mkv"
        mp4.write_bytes(b"x")

        args = _make_args(video_id="lookUpByArg1")
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["video_id"] == "lookUpByArg1"
        assert identity["prefix"] == "2026-04-16-talk-title"
        assert identity["meta_path"] == canonical

    def test_step_2_g2_flag_overrides_content_in_canonical_meta(self, tmp_path):
        """Flag-override precedence within a G2 match: flags update content fields, prefix stays canonical."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        canonical = channel_dir / "2026-04-16-original-title.meta.json"
        canonical.write_text(
            json.dumps(
                {
                    "video_id": "flgOvrdXYZ1",
                    "video_url": "https://www.youtube.com/watch?v=flgOvrdXYZ1",
                    "title": "Original Title",
                    "published": "2026-04-16",
                    "channel": "everyinc",
                    "published_source": "youtube_api",
                }
            )
        )
        mp4 = channel_dir / "flgOvrdXYZ1.mp4"
        mp4.write_bytes(b"x")

        args = _make_args(title="New Title From Flag", date="2026-04-20")
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["title"] == "New Title From Flag"
        assert identity["published"] == "2026-04-20"
        assert identity["published_source"] == "cli_flag"
        # prefix and meta_path stay canonical (F11 uniqueness invariant)
        assert identity["prefix"] == "2026-04-16-original-title"
        assert identity["meta_path"] == canonical

    def test_step_3_explicit_flags_without_canonical_meta(self, tmp_path):
        """With all three flags, build identity without any meta.json lookup.

        Issue #186: with BOTH --title and --date asserted, the prefix follows
        the {date}-{slug} scan convention instead of the filename stem (the
        pre-#186 expectation here was `"anything"`).
        """
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "anything.mkv"
        mp4.write_bytes(b"x")

        args = _make_args(video_id="ab12CDef-_3", title="Explicit Title", date="2026-04-01")
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["video_id"] == "ab12CDef-_3"
        assert identity["title"] == "Explicit Title"
        assert identity["published"] == "2026-04-01"
        assert identity["published_source"] == "cli_flag"
        assert identity["url"] == "https://www.youtube.com/watch?v=ab12CDef-_3"
        assert identity["prefix"] == "2026-04-01-explicit-title"
        assert identity["channel"] == "everyinc"

    def test_step_5_stem_as_title_no_video_id(self, tmp_path):
        """When stem is not a videoId, stem becomes title and video_id stays empty."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "Compound Engineering Camp.mkv"
        mp4.write_bytes(b"x")

        args = _make_args()
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["title"] == "Compound Engineering Camp"
        assert identity["video_id"] == ""
        assert identity["url"] == ""
        assert identity["prefix"] == "Compound Engineering Camp"

    def test_step_6_stem_as_video_id_when_11_chars(self, tmp_path):
        """11-char stem matching the charset is adopted as video_id (without any canonical meta)."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "lfML5OJc-CM.mp4"
        mp4.write_bytes(b"x")

        args = _make_args()
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["video_id"] == "lfML5OJc-CM"
        assert identity["url"] == "https://www.youtube.com/watch?v=lfML5OJc-CM"

    def test_stem_not_exactly_11_chars_is_not_video_id(self, tmp_path):
        """Stems of length 10 or 12 with valid charset must NOT be treated as video_id."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4_10 = channel_dir / "ABCDEFGHIJ.mp4"  # 10 chars
        mp4_10.write_bytes(b"x")
        mp4_12 = channel_dir / "ABCDEFGHIJKL.mp4"  # 12 chars
        mp4_12.write_bytes(b"x")

        id_10 = resolve_local_file_identity(mp4_10, channel_name="everyinc", channel_dir=channel_dir, args=_make_args())
        id_12 = resolve_local_file_identity(mp4_12, channel_name="everyinc", channel_dir=channel_dir, args=_make_args())

        assert id_10["video_id"] == ""
        assert id_12["video_id"] == ""

    def test_step_7_mtime_fallback_for_published(self, tmp_path):
        """With no sibling meta and no --date, published equals the MP4's mtime formatted YYYY-MM-DD."""
        import os
        from datetime import datetime as _dt

        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "Compound Engineering Camp.mkv"
        mp4.write_bytes(b"x")

        # Pin mtime to a specific UTC moment and compute the expected local-date string
        # using the same formula the implementation uses (datetime.fromtimestamp(mtime)).
        ts = 1773000000  # deterministic epoch
        os.utime(mp4, (ts, ts))
        expected_date = _dt.fromtimestamp(ts).strftime("%Y-%m-%d")

        args = _make_args()
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["published_source"] == "mtime"
        assert identity["published"] == expected_date

    def test_cli_flag_video_url_derived_from_video_id(self, tmp_path):
        """Whenever video_id is known (any source), video_url is derivable."""
        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "unknown.mkv"
        mp4.write_bytes(b"x")

        args = _make_args(video_id="knownID12345")
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["url"] == "https://www.youtube.com/watch?v=knownID12345"

    def test_video_id_flag_without_title_flag_emits_warning(self, tmp_path, caplog):
        """If --video-id is given but --title is not, warn that stem is a low-confidence title."""
        import logging

        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        mp4 = channel_dir / "random filename here.mkv"
        mp4.write_bytes(b"x")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            args = _make_args(video_id="lookUpByArgX")
            identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert any("video-id given without --title" in record.message for record in caplog.records)
        # Even so, identity still resolves (stem becomes title as documented fallback)
        assert identity["title"] == "random filename here"

    def test_multiple_canonical_metas_with_same_video_id_logs_warning(self, tmp_path, caplog):
        """F11 invariant: multiple canonical metas with the same video_id is a data-integrity issue."""
        import logging

        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()
        # Simulate pre-existing corruption: two canonical metas both with video_id "dupVideoID1"
        (channel_dir / "2026-04-16-first.meta.json").write_text(
            json.dumps({"video_id": "dupVideoID1", "title": "First", "published": "2026-04-16"})
        )
        (channel_dir / "2026-04-17-second.meta.json").write_text(
            json.dumps({"video_id": "dupVideoID1", "title": "Second", "published": "2026-04-17"})
        )
        mp4 = channel_dir / "dupVideoID1.mp4"
        mp4.write_bytes(b"x")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            args = _make_args()
            identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert any("Multiple canonical meta.json" in record.message for record in caplog.records)
        # Deterministic pick: lexicographically first filename wins
        assert identity["prefix"] == "2026-04-16-first"


# ---------------------------------------------------------------------------
# update_meta sentinel mode
# ---------------------------------------------------------------------------


class TestUpdateMetaIdentityMode:
    """mode='identity' must not append to modes_completed."""

    def test_identity_mode_does_not_append_to_modes_completed(self, tmp_path):
        meta_path = tmp_path / "x.meta.json"

        update_meta(meta_path, {"video_id": "abc"}, mode="identity")

        meta = json.loads(meta_path.read_text())
        assert "identity" not in meta.get("modes_completed", [])
        assert meta["video_id"] == "abc"

    def test_identity_mode_preserves_existing_modes_completed(self, tmp_path):
        meta_path = tmp_path / "x.meta.json"
        meta_path.write_text(json.dumps({"modes_completed": ["scan", "transcript"]}))

        update_meta(meta_path, {"title": "New"}, mode="identity")

        meta = json.loads(meta_path.read_text())
        assert meta["modes_completed"] == ["scan", "transcript"]
        assert meta["title"] == "New"

    def test_non_identity_mode_still_appends(self, tmp_path):
        """Regression guard: normal modes still grow modes_completed."""
        meta_path = tmp_path / "x.meta.json"

        update_meta(meta_path, {"title": "T"}, mode="scan")

        meta = json.loads(meta_path.read_text())
        assert meta["modes_completed"] == ["scan"]


# ---------------------------------------------------------------------------
# cmd_transcript --file --channel  (Checkpoint B)
# ---------------------------------------------------------------------------


class TestCmdTranscriptFileChannel:
    """Integration: --file + --channel routes artifacts to canonical channel folder."""

    def _setup_common(self, monkeypatch, tmp_path):
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")
        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/gemini-uri")

    def test_channel_scoped_transcript_uses_canonical_video_url_not_file_uri(self, monkeypatch, tmp_path):
        """F8: video['url'] passed downstream is canonical YouTube URL; file_uri goes via media_uri."""
        self._setup_common(monkeypatch, tmp_path)
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "Compound Engineering Camp.mkv"
        mp4.write_bytes(b"x")

        captured = {}

        def fake_process(_client, _types, video, _prompt, _model, ch_dir, prefix, **kw):
            captured["video_url"] = video["url"]
            captured["channel_dir"] = ch_dir
            captured["prefix"] = prefix
            captured["media_uri"] = kw.get("media_uri")
            return prefix, "done"

        monkeypatch.setattr("video_intel.process_transcript", fake_process)

        args = _make_args(file=mp4, channel="everyinc")
        cmd_transcript(args, {"channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc"}]})

        assert captured["channel_dir"] == channel_dir
        assert captured["prefix"] == "Compound Engineering Camp"
        assert captured["media_uri"] == "files/gemini-uri"
        # No video_id known from stem => video_url stays empty string (never file_uri)
        assert captured["video_url"] == ""

    def test_channel_scoped_writes_identity_meta_before_gemini_call(self, monkeypatch, tmp_path):
        """F11: identity block lands in meta.json before transcript call."""
        self._setup_common(monkeypatch, tmp_path)
        output_dir = tmp_path / "video-intel"
        channel_dir = output_dir / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "talk.mkv"
        mp4.write_bytes(b"x")

        # Track ordering: process_transcript called AFTER update_meta(mode='identity')
        events: list = []
        orig_update_meta = __import__("video_intel").update_meta

        def logging_update_meta(meta_path, fields, mode):
            events.append(("update_meta", mode, dict(fields)))
            return orig_update_meta(meta_path, fields, mode)

        monkeypatch.setattr("video_intel.update_meta", logging_update_meta)
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (events.append(("process_transcript",)), ("talk", "done"))[1],
        )

        args = _make_args(file=mp4, channel="everyinc")
        cmd_transcript(args, {"channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc"}]})

        modes = [e for e in events if e[0] == "update_meta"]
        assert any(m[1] == "identity" for m in modes), f"Expected identity-mode write, got {events}"
        # Identity write must precede process_transcript call
        identity_idx = next(i for i, e in enumerate(events) if e[0] == "update_meta" and e[1] == "identity")
        process_idx = next(i for i, e in enumerate(events) if e[0] == "process_transcript")
        assert identity_idx < process_idx

    def test_channel_not_in_config_exits(self, monkeypatch, tmp_path):
        """F5: --channel must exist in config.yaml, else sys.exit."""
        self._setup_common(monkeypatch, tmp_path)
        mp4 = tmp_path / "video-intel" / "everyinc" / "x.mp4"
        mp4.parent.mkdir(parents=True)
        mp4.write_bytes(b"x")

        args = _make_args(file=mp4, channel="nonexistent")
        with pytest.raises(SystemExit):
            cmd_transcript(args, {"channels": [{"name": "everyinc", "url": "u"}]})

    def test_channel_scoped_honors_skip_flag_in_existing_meta(self, monkeypatch, tmp_path):
        """F7: skip=true in existing meta.json blocks upload and transcription."""
        self._setup_common(monkeypatch, tmp_path)
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "talk.mkv"
        mp4.write_bytes(b"x")
        # Pre-existing sibling meta with skip=true
        (channel_dir / "talk.meta.json").write_text(json.dumps({"video_id": "", "skip": True}))

        upload_called = {"count": 0}
        process_called = {"count": 0}

        def fake_upload(_c, _p):
            upload_called["count"] += 1
            return "files/should-not-happen"

        monkeypatch.setattr("video_intel.upload_local_video", fake_upload)
        monkeypatch.setattr(
            "video_intel.process_transcript",
            lambda *a, **kw: (process_called.__setitem__("count", process_called["count"] + 1), ("x", "done"))[1],
        )

        args = _make_args(file=mp4, channel="everyinc")
        cmd_transcript(args, {"channels": [{"name": "everyinc", "url": "u"}]})

        assert upload_called["count"] == 0, "Upload must NOT happen when skip=true"
        assert process_called["count"] == 0, "process_transcript must NOT run when skip=true"

    def test_channel_scoped_meta_json_contains_full_identity_block(self, monkeypatch, tmp_path):
        """End-to-end: after cmd_transcript --file --channel, meta.json carries every F10/F13 field."""
        self._setup_common(monkeypatch, tmp_path)
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "lfML5OJc-CM.mp4"  # 11-char YouTube id as stem
        mp4.write_bytes(b"x")
        # Let process_transcript succeed without actually calling Gemini
        monkeypatch.setattr("video_intel.process_transcript", lambda *a, **kw: ("lfML5OJc-CM", "done"))

        args = _make_args(file=mp4, channel="everyinc", title="Compound Engineering Camp", date="2026-04-17")
        cmd_transcript(args, {"channels": [{"name": "everyinc", "url": "u"}]})

        # Issue #186: with both --title and --date the artifacts land under the
        # {date}-{slug} scan convention, not the id stem (pre-#186: lfML5OJc-CM.meta.json).
        meta_path = channel_dir / "2026-04-17-compound-engineering-camp.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())

        # F8: canonical YouTube URL, never file_uri
        assert meta["video_url"] == "https://www.youtube.com/watch?v=lfML5OJc-CM"
        assert "files/" not in meta["video_url"]

        # F10 / F13: identity + provenance fields present
        assert meta["video_id"] == "lfML5OJc-CM"
        assert meta["channel"] == "everyinc"
        assert meta["title"] == "Compound Engineering Camp"
        assert meta["published"] == "2026-04-17"
        assert meta["published_source"] == "cli_flag"
        assert meta["transcript_source"] == "local_file"

        # Identity-mode write must NOT have appended "identity" to modes_completed
        assert "identity" not in meta.get("modes_completed", [])

    def test_no_channel_backward_compat_behavior_unchanged(self, monkeypatch, tmp_path):
        """Without --channel, existing behavior: output next to source, video['url']=file_uri."""
        self._setup_common(monkeypatch, tmp_path)
        mp4 = tmp_path / "loose.mp4"
        mp4.write_bytes(b"x")

        captured = {}

        def fake_process(_client, _types, video, _prompt, _model, ch_dir, prefix, **kw):
            captured["video_url"] = video["url"]
            captured["channel_dir"] = ch_dir
            captured["prefix"] = prefix
            return prefix, "done"

        monkeypatch.setattr("video_intel.process_transcript", fake_process)

        args = _make_args(file=mp4)
        cmd_transcript(args, {})

        # Backward-compat: file_uri IS video_url; artifacts land next to source
        assert captured["video_url"] == "files/gemini-uri"
        assert captured["channel_dir"] == mp4.parent
        assert captured["prefix"] == "loose"


# ---------------------------------------------------------------------------
# cmd_mindmap --file --channel  (parity)
# ---------------------------------------------------------------------------


class TestCmdMindmapFileChannel:
    """Parity: mindmap --file --channel routes to canonical channel folder."""

    def _setup_common(self, monkeypatch, tmp_path):
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")
        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/gemini-uri")

    def test_mindmap_file_channel_routes_and_uses_media_uri(self, monkeypatch, tmp_path):
        self._setup_common(monkeypatch, tmp_path)
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "lfML5OJc-CM.mp4"
        mp4.write_bytes(b"x")

        captured = {}

        def fake_process_mindmap(_client, _types, video, _prompt, _model, out_dir, ch_name, **kw):
            captured["video_url"] = video["url"]
            captured["video_id"] = video["video_id"]
            captured["out_dir"] = out_dir
            captured["channel_name"] = ch_name
            captured["media_uri"] = kw.get("media_uri")
            return "lfML5OJc-CM", "done"

        monkeypatch.setattr("video_intel.process_mindmap", fake_process_mindmap)

        args = _make_args(file=mp4, channel="everyinc")
        cmd_mindmap(args, {"channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc"}]})

        assert captured["video_id"] == "lfML5OJc-CM"
        assert captured["video_url"] == "https://www.youtube.com/watch?v=lfML5OJc-CM"
        assert captured["media_uri"] == "files/gemini-uri"
        assert captured["channel_name"] == "everyinc"


# ---------------------------------------------------------------------------
# F7 leak-guard: no Gemini file_uri in any persisted artifact
# ---------------------------------------------------------------------------


class TestFileUriLeakGuard:
    """F7/F8: Gemini files/... URIs must never appear in persisted artifacts on --channel path."""

    def test_no_file_uri_in_meta_json_after_channel_scoped_run(self, monkeypatch, tmp_path):
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path / "video-intel")
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")
        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, _p: "files/LEAKY_URI_42")
        monkeypatch.setattr("video_intel.process_transcript", lambda *a, **kw: ("talk", "done"))

        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "talk.mkv"
        mp4.write_bytes(b"x")

        args = _make_args(file=mp4, channel="everyinc")
        cmd_transcript(args, {"channels": [{"name": "everyinc", "url": "u"}]})

        # Scan all produced meta.json files for a Gemini Files API URI pattern.
        # Pattern matches literal "files/" followed by at least one alphanumeric
        # character, which is what Gemini returns (e.g. "files/abc123"); this
        # avoids false positives on unrelated path substrings that happen to
        # contain the word "files".
        import re as _re

        gemini_uri_re = _re.compile(r"files/[A-Za-z0-9_-]+")
        for meta_file in channel_dir.glob("*.meta.json"):
            content = meta_file.read_text()
            assert "LEAKY_URI_42" not in content, f"file_uri leaked into {meta_file}"
            match = gemini_uri_re.search(content)
            assert match is None, f"Gemini URI {match.group(0)!r} leaked into {meta_file}: {content}"


# ---------------------------------------------------------------------------
# cmd_scan concept enumeration pivot (G1)
# ---------------------------------------------------------------------------


class TestProcessMindmapExceptionPath:
    """Regression guard: process_mindmap must record failure without NameError
    when call_gemini raises (e.g. 403 PERMISSION_DENIED). Caught in the wild on
    2026-04-17 when the resolved_channel_dir / resolved_prefix refactor left
    the except block still referencing the old `channel_dir` / `prefix` names."""

    def test_exception_path_writes_meta_and_returns_error(self, tmp_path, monkeypatch):
        from video_intel import process_mindmap

        types = MagicMock()
        client = MagicMock()

        def boom(*a, **kw):
            raise RuntimeError("403 PERMISSION_DENIED")

        monkeypatch.setattr("video_intel.call_gemini", boom)

        output_dir = tmp_path / "video-intel"
        video = {
            "video_id": "failID12345",
            "url": "https://www.youtube.com/watch?v=failID12345",
            "title": "Fails Hard",
            "published": "2026-04-17",
        }

        # Call WITHOUT prefix/channel_dir_override kwargs (the scan-path usage
        # that originally triggered the NameError)
        prefix, status = process_mindmap(client, types, video, "prompt", "gemini-test", output_dir, "everyinc")

        assert status.startswith("error: 403 PERMISSION_DENIED"), f"Got: {status}"
        assert prefix == "2026-04-17-fails-hard"  # video_file_prefix(video)

        meta_path = output_dir / "everyinc" / "2026-04-17-fails-hard.meta.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["last_error"] == "403 PERMISSION_DENIED"
        assert meta["video_id"] == "failID12345"


class TestScan403RecoveryRecipe:
    """Scan must print an actionable recovery recipe when a member-gated video returns 403."""

    def test_403_permission_denied_triggers_recovery_log_lines(self, monkeypatch, tmp_path, caplog):
        import logging

        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.get_channel_id", lambda *a, **kw: ("UCfake", "Every"))
        monkeypatch.setattr(
            "video_intel.fetch_channel_videos",
            lambda *a, **kw: [
                {
                    "video_id": "gated123XYZ",
                    "title": "Members Only Video",
                    "published": "2026-04-16",
                    "url": "https://www.youtube.com/watch?v=gated123XYZ",
                }
            ],
        )
        monkeypatch.setattr("video_intel.load_prompt", lambda _name: "prompt text")

        def fake_process_mindmap(*a, **kw):
            return "2026-04-16-members-only-video", ("error: 403 PERMISSION_DENIED. Caller does not have permission.")

        monkeypatch.setattr("video_intel.process_mindmap", fake_process_mindmap)

        args = argparse.Namespace(channel="everyinc", since=None, force=False, dry_run=False, model=None)
        config = {
            "channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc"}],
            "output_dir": str(tmp_path),
        }

        with caplog.at_level(logging.INFO, logger="video_intel"):
            cmd_scan(args, config)

        messages = [r.message for r in caplog.records]
        assert any("Likely members-only" in m for m in messages), f"Missing recovery hint in logs: {messages}"
        assert any("gated123XYZ.mp4" in m for m in messages), "videoId not interpolated in recipe"
        assert any("--channel everyinc" in m for m in messages), "channel name not in recipe"
        assert any("mindmap    --file" in m for m in messages), "mindmap command missing"
        assert any("transcript --file" in m for m in messages), "transcript command missing"


class TestScanConceptsGlobEnumeration:
    """F12: concept extraction enumerates via glob, picking up both naming conventions."""

    def test_concepts_enumerates_both_canonical_and_stem_named_metas(self, monkeypatch, tmp_path):
        """A channel folder with mixed canonical + stem-named metas must yield concepts for both."""
        # This test validates that the pivot works in principle by stubbing the concept work
        # and checking which prefixes get processed.
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.require_youtube", lambda: MagicMock())
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr("video_intel.get_channel_id", lambda *a, **kw: ("UCfake", "Every"))
        monkeypatch.setattr("video_intel.fetch_channel_videos", lambda *a, **kw: [])  # No YouTube videos

        channel_dir = tmp_path / "everyinc"
        channel_dir.mkdir()

        # Canonical scan-generated meta + mindmap
        (channel_dir / "2026-04-10-canonical-talk.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "canonicalID1",
                    "title": "Canonical Talk",
                    "published": "2026-04-10",
                    "channel": "everyinc",
                }
            )
        )
        (channel_dir / "2026-04-10-canonical-talk.mindmap.md").write_text("# canonical mindmap")

        # Stem-named local-recovery meta + mindmap
        (channel_dir / "Compound Engineering Camp.meta.json").write_text(
            json.dumps(
                {
                    "video_id": "",
                    "title": "Compound Engineering Camp",
                    "published": "2026-04-17",
                    "channel": "everyinc",
                }
            )
        )
        (channel_dir / "Compound Engineering Camp.mindmap.md").write_text("# stem mindmap")

        processed_prefixes: list = []

        def fake_process_concepts(_c, _t, _v, _text, _tax, _m, _od, _ch, **kw):
            # Prefix is computed from video dict fields inside process_concepts in real code,
            # but in this test we track invocation by title
            processed_prefixes.append(_v.get("title"))
            return _v.get("title", ""), "done"

        monkeypatch.setattr("video_intel.process_concepts", fake_process_concepts)
        monkeypatch.setattr("video_intel.load_taxonomy", lambda _od: {"version": 1, "concepts": {}})

        args = argparse.Namespace(
            channel="everyinc",
            since=None,
            force=False,
            dry_run=False,
            model=None,
        )
        config = {
            "channels": [{"name": "everyinc", "url": "https://youtube.com/@everyinc", "auto_concepts": True}],
            "output_dir": str(tmp_path),
            "auto_concepts": True,
        }
        cmd_scan(args, config)

        assert "Canonical Talk" in processed_prefixes
        assert "Compound Engineering Camp" in processed_prefixes


# ---------------------------------------------------------------------------
# Issue #186: explicit --title/--date derive the canonical {date}-{slug} prefix
# ---------------------------------------------------------------------------


class TestPrefixFromExplicitFlags:
    """When the operator supplies BOTH --title and --date on the fallback path
    (no sibling meta, no G2 canonical match), the artifact prefix follows the
    same {date}-{slug} convention every scanned artifact uses, instead of the
    filename stem. One flag alone changes nothing: a prefix derived from an
    mtime-fallback date would drift when the file is copied (new mtime, new
    prefix, duplicate artifacts), so the derived prefix requires the operator
    to have asserted both halves explicitly.
    """

    def _mp4(self, tmp_path):
        channel_dir = tmp_path / "neo4j"
        channel_dir.mkdir()
        mp4 = channel_dir / "gc720-hybrid-rag.mp4"
        mp4.write_bytes(b"x")
        return channel_dir, mp4

    def test_both_flags_derive_the_scan_convention_prefix(self, tmp_path):
        channel_dir, mp4 = self._mp4(tmp_path)
        args = _make_args(title="Hybrid RAG with Neo4j", date="2026-08-31")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        # Independent literal, not derived through slugify/video_file_prefix.
        assert identity["prefix"] == "2026-08-31-hybrid-rag-with-neo4j"
        assert identity["title"] == "Hybrid RAG with Neo4j"
        assert identity["published"] == "2026-08-31"
        assert identity["published_source"] == "cli_flag"

    def test_meta_path_follows_the_derived_prefix(self, tmp_path):
        channel_dir, mp4 = self._mp4(tmp_path)
        args = _make_args(title="Hybrid RAG with Neo4j", date="2026-08-31")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["meta_path"] == identity["channel_dir"] / f"{identity['prefix']}.meta.json"

    def test_title_alone_keeps_the_stem_prefix(self, tmp_path):
        channel_dir, mp4 = self._mp4(tmp_path)
        args = _make_args(title="Hybrid RAG with Neo4j")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "gc720-hybrid-rag"

    def test_date_alone_keeps_the_stem_prefix(self, tmp_path):
        channel_dir, mp4 = self._mp4(tmp_path)
        args = _make_args(date="2026-08-31")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "gc720-hybrid-rag"

    def test_no_flags_keeps_the_stem_prefix(self, tmp_path):
        channel_dir, mp4 = self._mp4(tmp_path)
        args = _make_args()

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "gc720-hybrid-rag"

    def test_sibling_meta_prefix_is_untouched_by_the_flags(self, tmp_path):
        """Artifacts already exist under the sibling's stem; renaming them is
        the manual follow-up issue #186 explicitly scopes OUT."""
        channel_dir, mp4 = self._mp4(tmp_path)
        (channel_dir / "gc720-hybrid-rag.meta.json").write_text(
            json.dumps({"video_id": "abc123XYZ_-", "title": "Old", "published": "2026-08-30", "channel": "neo4j"})
        )
        args = _make_args(title="Hybrid RAG with Neo4j", date="2026-08-31")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "gc720-hybrid-rag"
        assert identity["title"] == "Hybrid RAG with Neo4j"  # flags still override fields

    def test_g2_canonical_prefix_is_untouched_by_the_flags(self, tmp_path):
        """F11 uniqueness: a canonical scan meta's prefix always wins."""
        channel_dir = tmp_path / "neo4j"
        channel_dir.mkdir()
        (channel_dir / "2026-04-17-real-scan-title.meta.json").write_text(
            json.dumps({"video_id": "abc123XYZ_-", "title": "Real Scan Title", "published": "2026-04-17"})
        )
        mp4 = channel_dir / "abc123XYZ_-.mp4"
        mp4.write_bytes(b"x")
        args = _make_args(title="Hybrid RAG with Neo4j", date="2026-08-31")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "2026-04-17-real-scan-title"

    def test_synthetic_video_id_with_both_flags_takes_the_derived_prefix(self, tmp_path):
        """The Goldcast shape: --video-id names a synthetic id with no canonical
        meta to match, so G2 falls through and the flags own the prefix."""
        channel_dir, mp4 = self._mp4(tmp_path)
        args = _make_args(video_id="gc720hybrid", title="Hybrid RAG with Neo4j", date="2026-08-31")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "2026-08-31-hybrid-rag-with-neo4j"
        assert identity["video_id"] == "gc720hybrid"

    def test_derived_prefix_matches_the_scan_writers_own_rule(self, tmp_path):
        """Checker-uses-writer's-path: the derived prefix must equal what
        video_file_prefix produces for the same identity, compared against the
        REAL helper so the two rules cannot drift apart silently."""
        from video_intel import video_file_prefix

        channel_dir, mp4 = self._mp4(tmp_path)
        args = _make_args(title="C++ & .NET: A (Weird) Pairing!", date="2026-07-04")

        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == video_file_prefix({"title": args.title, "published": args.date})


# ---------------------------------------------------------------------------
# Issue #186 review round: date validation, channel-dir stem meta, belts
# ---------------------------------------------------------------------------


class TestDateFlagValidation:
    """A malformed --date used to reach the artifact path unvalidated - and on
    `mindmap --file` the Gemini upload was paid BEFORE the crash. `iso_date_arg`
    rejects it at the PARSER, before any work (probe before you pay)."""

    @pytest.mark.parametrize("bad", ["2026/08/31", "Aug 31, 2026", "0", "2026-8-31", "20260831"])
    def test_parser_rejects_non_iso_dates(self, bad):
        from video_intel import iso_date_arg

        with pytest.raises(argparse.ArgumentTypeError):
            iso_date_arg(bad)

    def test_parser_accepts_iso(self):
        from video_intel import iso_date_arg

        assert iso_date_arg("2026-08-31") == "2026-08-31"

    def test_every_date_flag_in_the_source_carries_the_validator(self):
        """Walk the module source for every "--date" add_argument call and
        require type=iso_date_arg on each - so a NEW subcommand adding the
        flag cannot silently skip validation."""
        import inspect

        import video_intel as vi

        source = inspect.getsource(vi)
        blocks = source.split('"--date"')[1:]
        assert len(blocks) == 3, f"expected the three --date parser sites, found {len(blocks)}"
        for i, block in enumerate(blocks):
            head = block[:120]
            assert "type=iso_date_arg" in head, f"--date site {i + 1} is missing type=iso_date_arg: {head!r}"

    def test_resolver_belt_keeps_stem_for_a_library_caller_with_a_bad_date(self, tmp_path, caplog):
        channel_dir = tmp_path / "neo4j"
        channel_dir.mkdir()
        mp4 = channel_dir / "gc720-hybrid-rag.mp4"
        mp4.write_bytes(b"x")
        args = _make_args(title="Hybrid RAG with Neo4j", date="2026/08/31")
        with caplog.at_level("WARNING"):
            identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)
        assert identity["prefix"] == "gc720-hybrid-rag"
        assert "not YYYY-MM-DD" in caplog.text

    def test_resolver_belt_keeps_stem_for_an_empty_slug_title(self, tmp_path, caplog):
        channel_dir = tmp_path / "neo4j"
        channel_dir.mkdir()
        mp4 = channel_dir / "gc720-hybrid-rag.mp4"
        mp4.write_bytes(b"x")
        args = _make_args(title="!!!", date="2026-08-31")
        with caplog.at_level("WARNING"):
            identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)
        assert identity["prefix"] == "gc720-hybrid-rag"
        assert "slugifies to nothing" in caplog.text


class TestChannelDirStemMetaAdoption:
    """Issue #186 review P2: a pre-#186 ingest from outside the corpus wrote
    `<channel>/<stem>.meta.json`; a flags re-run must adopt it instead of
    deriving a fresh prefix and re-billing Gemini for a duplicate ingest."""

    def test_flags_rerun_adopts_the_existing_channel_dir_stem_meta(self, tmp_path):
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        mp4 = downloads / "session-two.mp4"
        mp4.write_bytes(b"x")
        (channel_dir / "session-two.meta.json").write_text(
            json.dumps({"title": "Session Two", "published": "2026-08-30", "channel": "everyinc"}),
            encoding="utf-8",
        )

        args = _make_args(title="Session Two Talk", date="2026-08-31")
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "session-two"
        assert identity["meta_path"] == channel_dir / "session-two.meta.json"
        assert identity["channel_dir"] == channel_dir
        assert identity["title"] == "Session Two Talk"  # flags still override fields

    def test_true_sibling_still_wins_over_the_channel_dir_copy(self, tmp_path):
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        mp4 = downloads / "session-two.mp4"
        mp4.write_bytes(b"x")
        true_sibling = downloads / "session-two.meta.json"
        true_sibling.write_text(json.dumps({"title": "Beside the file", "published": "2026-08-29"}), encoding="utf-8")
        (channel_dir / "session-two.meta.json").write_text(
            json.dumps({"title": "In the channel dir", "published": "2026-08-30"}), encoding="utf-8"
        )

        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=_make_args())

        assert identity["meta_path"] == true_sibling
        assert identity["channel_dir"] == downloads
        assert identity["title"] == "Beside the file"

    def test_no_meta_anywhere_still_derives_from_the_flags(self, tmp_path):
        channel_dir = tmp_path / "video-intel" / "everyinc"
        channel_dir.mkdir(parents=True)
        downloads = tmp_path / "Downloads"
        downloads.mkdir()
        mp4 = downloads / "session-two.mp4"
        mp4.write_bytes(b"x")

        args = _make_args(title="Session Two Talk", date="2026-08-31")
        identity = resolve_local_file_identity(mp4, channel_name="everyinc", channel_dir=channel_dir, args=args)

        assert identity["prefix"] == "2026-08-31-session-two-talk"


class TestPrefixDerivationRequiresBothFlagsEvenWithVideoId:
    def test_video_id_plus_title_alone_keeps_the_stem(self, tmp_path):
        channel_dir = tmp_path / "neo4j"
        channel_dir.mkdir()
        mp4 = channel_dir / "gc720-hybrid-rag.mp4"
        mp4.write_bytes(b"x")
        args = _make_args(video_id="gc720hybrid", title="Hybrid RAG with Neo4j")
        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)
        assert identity["prefix"] == "gc720-hybrid-rag"

    def test_video_id_plus_date_alone_keeps_the_stem(self, tmp_path):
        channel_dir = tmp_path / "neo4j"
        channel_dir.mkdir()
        mp4 = channel_dir / "gc720-hybrid-rag.mp4"
        mp4.write_bytes(b"x")
        args = _make_args(video_id="gc720hybrid", date="2026-08-31")
        identity = resolve_local_file_identity(mp4, channel_name="neo4j", channel_dir=channel_dir, args=args)
        assert identity["prefix"] == "gc720-hybrid-rag"
