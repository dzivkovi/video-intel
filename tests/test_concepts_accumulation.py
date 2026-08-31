"""Regression tests for issue #176: scan's auto_concepts loop must accumulate
newly-discovered concepts into the in-memory taxonomy between videos, like
cmd_concepts already does (ADR-0010).

Pre-fix, `cmd_scan`'s `auto_concepts` loop loaded `taxonomy = load_taxonomy(...)`
once (per channel) and never merged a just-written concepts.json back into it,
so video N in a multi-video scan normalized against the SAME snapshot video 1
saw - two videos in one scan could mint separate labels for the same concept.
`cmd_concepts` already merges after every video; this issue extracts that
block into a shared helper, `accumulate_concepts_into_taxonomy`, and calls it
from both loops, plus fixes the identical drift one scope wider: `cmd_scan`
used to reload `taxonomy` fresh INSIDE the per-channel loop, so channel 2
never saw channel 1's newly-minted concepts either.

Test layout:
  * TestAccumulateConceptsIntoTaxonomy - unit tests for the helper itself,
    including the paid-loop robustness guards (malformed JSON, invalid UTF-8
    bytes, non-list `concepts`, non-dict entries) that must degrade rather
    than crash a loop that has already paid for a Gemini call.
  * TestScanAccumulatesWithinOneChannel - the issue's own contract: drives
    the REAL cmd_scan with process_concepts stubbed to write a real
    concepts.json for video 1 and record the taxonomy dict it was handed for
    video 2. Asserts on accumulated CONTENT, not call count.
  * TestScanAccumulatesAcrossChannels - the cross-channel half: two channels,
    one video each; channel 2 must see channel 1's minted concept.
  * TestCmdConceptsAccumulationUnchanged - guards the extraction: an
    equivalent two-video cmd_concepts run still accumulates.
  * TestErrorStatusDoesNotAccumulate - an "error:"-prefixed status must not
    feed the in-memory taxonomy.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock

import pytest

import video_intel
from video_intel import accumulate_concepts_into_taxonomy, cmd_concepts, cmd_scan

# ---------------------------------------------------------------------------
# 1. Unit tests for the extracted helper.
# ---------------------------------------------------------------------------


class TestAccumulateConceptsIntoTaxonomy:
    def test_adds_a_new_concept_and_returns_one(self, tmp_path):
        taxonomy = {"concepts": {}}
        concepts_path = tmp_path / "video.concepts.json"
        concepts_path.write_text(
            json.dumps({"concepts": [{"concept_id": "ai.rag", "preferred_label": "RAG", "domain": "ai"}]}),
            encoding="utf-8",
        )

        added = accumulate_concepts_into_taxonomy(taxonomy, concepts_path)

        assert added == 1
        assert taxonomy["concepts"]["ai.rag"] == {
            "preferred_label": "RAG",
            "aliases": [],
            "domain": "ai",
        }

    def test_does_not_overwrite_an_existing_concept_id_and_returns_zero(self, tmp_path):
        taxonomy = {
            "concepts": {"ai.rag": {"preferred_label": "Original Label", "aliases": ["retrieval"], "domain": "ai"}}
        }
        concepts_path = tmp_path / "video.concepts.json"
        concepts_path.write_text(
            json.dumps({"concepts": [{"concept_id": "ai.rag", "preferred_label": "Renamed", "domain": "ai"}]}),
            encoding="utf-8",
        )

        added = accumulate_concepts_into_taxonomy(taxonomy, concepts_path)

        assert added == 0
        assert taxonomy["concepts"]["ai.rag"]["preferred_label"] == "Original Label"
        assert taxonomy["concepts"]["ai.rag"]["aliases"] == ["retrieval"]

    def test_missing_file_returns_zero(self, tmp_path):
        taxonomy = {"concepts": {}}
        added = accumulate_concepts_into_taxonomy(taxonomy, tmp_path / "does-not-exist.concepts.json")
        assert added == 0
        assert taxonomy["concepts"] == {}

    def test_malformed_json_returns_zero_and_does_not_raise(self, tmp_path, caplog):
        taxonomy = {"concepts": {}}
        concepts_path = tmp_path / "video.concepts.json"
        concepts_path.write_text("{not valid json", encoding="utf-8")

        with caplog.at_level("WARNING", logger="video_intel"):
            added = accumulate_concepts_into_taxonomy(taxonomy, concepts_path)

        assert added == 0
        assert taxonomy["concepts"] == {}
        assert any("malformed concepts.json" in r.message.lower() for r in caplog.records)

    def test_invalid_utf8_bytes_returns_zero_and_does_not_raise(self, tmp_path, caplog):
        """Proves the ValueError half of the (ValueError, OSError) tuple: a
        write torn mid-multibyte-character raises UnicodeDecodeError, which
        subclasses ValueError, not OSError. Real invalid bytes, not a mock."""
        taxonomy = {"concepts": {}}
        concepts_path = tmp_path / "video.concepts.json"
        concepts_path.write_bytes(b'{"concepts": [{"concept_id": "ai.rag"\xff\xfe')

        with caplog.at_level("WARNING", logger="video_intel"):
            added = accumulate_concepts_into_taxonomy(taxonomy, concepts_path)

        assert added == 0
        assert taxonomy["concepts"] == {}
        assert any("malformed concepts.json" in r.message.lower() for r in caplog.records)

    @pytest.mark.parametrize("bad_concepts_value", [5, "not-a-list"])
    def test_non_list_concepts_value_returns_zero_and_does_not_raise(self, tmp_path, bad_concepts_value):
        taxonomy = {"concepts": {}}
        concepts_path = tmp_path / "video.concepts.json"
        concepts_path.write_text(json.dumps({"concepts": bad_concepts_value}), encoding="utf-8")

        added = accumulate_concepts_into_taxonomy(taxonomy, concepts_path)

        assert added == 0
        assert taxonomy["concepts"] == {}

    def test_non_dict_entry_is_skipped_and_good_entries_still_process(self, tmp_path):
        taxonomy = {"concepts": {}}
        concepts_path = tmp_path / "video.concepts.json"
        concepts_path.write_text(
            json.dumps(
                {
                    "concepts": [
                        "not-a-dict",
                        {"concept_id": "ai.rag", "preferred_label": "RAG", "domain": "ai"},
                        123,
                        None,
                    ]
                }
            ),
            encoding="utf-8",
        )

        added = accumulate_concepts_into_taxonomy(taxonomy, concepts_path)

        assert added == 1
        assert "ai.rag" in taxonomy["concepts"]

    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("list", "[]"),
            ("nonempty_list", '[{"concept_id": "c1"}]'),
            ("string", '"hello"'),
            ("number", "42"),
            ("null", "null"),
            ("bool", "true"),
        ],
    )
    def test_a_successful_parse_with_a_non_object_top_level_does_not_crash(self, tmp_path, caplog, label, body):
        """A parse that SUCCEEDS can still hand back a non-dict.

        `json.loads` returns a list for `[]`, a str for `"hello"`, an int for
        `42` and None for `null`, and `.get` on any of them raises
        AttributeError - escaping the read guard immediately above it, inside a
        loop that has already paid for a Gemini call. All four crashed before
        this check. Same "the guard has a hole one layer in" shape as the
        issue #161/#171 family; caught by the Codex peer pass, not by the
        in-family review.
        """
        concepts_path = tmp_path / f"{label}.concepts.json"
        concepts_path.write_text(body, encoding="utf-8")
        taxonomy = {"concepts": {}}

        with caplog.at_level("WARNING"):
            added = accumulate_concepts_into_taxonomy(taxonomy, concepts_path)

        assert added == 0
        assert taxonomy == {"concepts": {}}, "a non-object top level must contribute nothing"
        assert any("top level" in r.message for r in caplog.records), (
            "a skipped artifact must say so; a guard that stops guarding silently is the failure mode"
        )


# ---------------------------------------------------------------------------
# Shared cmd_scan stubbing (mirrors tests/test_process_force_propagation.py's
# TestScanAutoConceptsForcePropagation idioms).
# ---------------------------------------------------------------------------


def _scan_args(*, channel=None, force=False):
    return argparse.Namespace(channel=channel, since=None, force=force, dry_run=False, model=None)


def _new_youtube_video(video_id, title, published="2026-08-30"):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "published": published,
        "duration_iso": "PT10M",
    }


def _stub_scan_env(monkeypatch, tmp_path, new_videos_by_channel):
    """new_videos_by_channel: {channel_name: [video, ...]}."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("YOUTUBE_API_KEY", "fake")
    monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
    monkeypatch.setattr(video_intel, "require_youtube", lambda: MagicMock())
    monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
    monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: tmp_path)
    monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-for-{name}")
    monkeypatch.setattr(video_intel, "get_channel_id", lambda _yt, url: (f"UC-{url}", url))
    monkeypatch.setattr(
        video_intel,
        "fetch_channel_videos",
        lambda _yt, channel_id, *_a, **_kw: [dict(v) for v in new_videos_by_channel.get(channel_id, [])],
    )

    def fake_enrich(_yt, video_ids):
        durations = {}
        for videos in new_videos_by_channel.values():
            for v in videos:
                if v["video_id"] in video_ids:
                    durations[v["video_id"]] = v["duration_iso"]
        return durations

    monkeypatch.setattr(video_intel, "enrich_with_durations", fake_enrich)
    monkeypatch.setattr(video_intel, "fetch_preflight_status", lambda *a, **kw: {})
    monkeypatch.setattr(video_intel, "is_short", lambda *a, **kw: False)
    monkeypatch.setattr(video_intel, "record_alt_title_if_rotated", lambda *a, **kw: None)


def _make_fake_mindmap(channel_dir_by_name):
    def fake_mindmap(*args, **kwargs):
        # process_mindmap(client, types, video, prompt_text, model,
        # output_dir, channel_name, *, prefix=None, ...) - args[2] is video,
        # args[6] is channel_name (see the real call sites in cmd_scan).
        video = args[2] if len(args) > 2 else kwargs.get("video")
        channel = args[6] if len(args) > 6 else kwargs.get("channel_name")
        prefix = kwargs.get("prefix") or video_intel.video_file_prefix(video)
        channel_dir = channel_dir_by_name[channel]
        channel_dir.mkdir(parents=True, exist_ok=True)
        (channel_dir / f"{prefix}.mindmap.md").write_text("# mindmap\n", encoding="utf-8")
        (channel_dir / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": video.get("video_id", ""),
                    "video_url": video.get("url", ""),
                    "title": video.get("title", prefix),
                    "published": video.get("published", ""),
                    "channel": channel,
                    "modes_completed": ["scan", "mindmap"],
                }
            ),
            encoding="utf-8",
        )
        return prefix, "done"

    return fake_mindmap

    # ---------------------------------------------------------------------------
    # 2. The issue's own test contract: within one channel, video 2 must see
    #    video 1's newly-minted concept id.
    # ---------------------------------------------------------------------------


class TestScanAccumulatesWithinOneChannel:
    def test_second_video_sees_first_videos_minted_concept(self, tmp_path, monkeypatch):
        channel = "everyinc"
        channel_dir = tmp_path / channel
        v1 = _new_youtube_video("VID00001", "First Video")
        v2 = _new_youtube_video("VID00002", "Second Video")
        _stub_scan_env(monkeypatch, tmp_path, {f"UC-https://youtube.com/@{channel}": [v1, v2]})
        monkeypatch.setattr(video_intel, "process_mindmap", _make_fake_mindmap({channel: channel_dir}))

        seen_taxonomies: list[dict] = []
        call_count = {"n": 0}

        def fake_process_concepts(client, types, video, mindmap_text, taxonomy, model, output_dir, ch_name, **kw):
            call_count["n"] += 1
            # Record a SNAPSHOT of what this call was handed, before this
            # call's own write could influence the recording.
            seen_taxonomies.append(json.loads(json.dumps(taxonomy)))
            prefix = kw.get("prefix")
            if call_count["n"] == 1:
                # Video 1 mints a brand-new concept id.
                (output_dir / ch_name / f"{prefix}.concepts.json").write_text(
                    json.dumps(
                        {
                            "concepts": [
                                {"concept_id": "ai.agentic_orchestration", "preferred_label": "Agentic Orchestration"}
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_concepts", fake_process_concepts)

        config = {
            "channels": [{"name": channel, "url": f"https://youtube.com/@{channel}", "auto_concepts": True}],
            "output_dir": str(tmp_path),
            "auto_concepts": True,
        }
        cmd_scan(_scan_args(channel=channel), config)

        assert call_count["n"] == 2
        # RED against the pre-fix code: the second call's taxonomy snapshot
        # would NOT contain the concept video 1 just minted.
        assert "ai.agentic_orchestration" not in seen_taxonomies[0].get("concepts", {})
        assert "ai.agentic_orchestration" in seen_taxonomies[1].get("concepts", {})


# ---------------------------------------------------------------------------
# 3. Cross-channel accumulation: channel 2 must see channel 1's minted
#    concept, proving the per-channel reload was removed.
# ---------------------------------------------------------------------------


class TestScanAccumulatesAcrossChannels:
    def test_second_channel_sees_first_channels_minted_concept(self, tmp_path, monkeypatch):
        ch_a, ch_b = "channel_a", "channel_b"
        channel_dir_by_name = {ch_a: tmp_path / ch_a, ch_b: tmp_path / ch_b}
        v_a = _new_youtube_video("VIDA0001", "Channel A Video")
        v_b = _new_youtube_video("VIDB0001", "Channel B Video")
        _stub_scan_env(
            monkeypatch,
            tmp_path,
            {
                f"UC-https://youtube.com/@{ch_a}": [v_a],
                f"UC-https://youtube.com/@{ch_b}": [v_b],
            },
        )
        monkeypatch.setattr(video_intel, "process_mindmap", _make_fake_mindmap(channel_dir_by_name))

        seen_taxonomies: list[dict] = []
        call_count = {"n": 0}

        def fake_process_concepts(client, types, video, mindmap_text, taxonomy, model, output_dir, ch_name, **kw):
            call_count["n"] += 1
            seen_taxonomies.append(json.loads(json.dumps(taxonomy)))
            prefix = kw.get("prefix")
            if call_count["n"] == 1:
                (output_dir / ch_name / f"{prefix}.concepts.json").write_text(
                    json.dumps({"concepts": [{"concept_id": "ai.cross_channel", "preferred_label": "Cross Channel"}]}),
                    encoding="utf-8",
                )
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_concepts", fake_process_concepts)

        config = {
            "channels": [
                {"name": ch_a, "url": f"https://youtube.com/@{ch_a}", "auto_concepts": True},
                {"name": ch_b, "url": f"https://youtube.com/@{ch_b}", "auto_concepts": True},
            ],
            "output_dir": str(tmp_path),
            "auto_concepts": True,
        }
        cmd_scan(_scan_args(), config)

        assert call_count["n"] == 2
        assert "ai.cross_channel" not in seen_taxonomies[0].get("concepts", {})
        assert "ai.cross_channel" in seen_taxonomies[1].get("concepts", {})


# ---------------------------------------------------------------------------
# 4. cmd_concepts behavior is unchanged after the extraction.
# ---------------------------------------------------------------------------


class TestCmdConceptsAccumulationUnchanged:
    def test_second_video_still_sees_first_videos_minted_concept(self, tmp_path, monkeypatch):
        channel = "everyinc"
        channel_dir = tmp_path / channel
        channel_dir.mkdir(parents=True, exist_ok=True)
        for i, name in enumerate(("video1", "video2"), 1):
            (channel_dir / f"{name}.mindmap.md").write_text(f"# mindmap {i}\n", encoding="utf-8")
            (channel_dir / f"{name}.meta.json").write_text(
                json.dumps(
                    {
                        "video_id": f"VID{i:04d}",
                        "video_url": f"https://www.youtube.com/watch?v=VID{i:04d}",
                        "title": name,
                        "published": f"2026-08-{i:02d}",
                        "channel": channel,
                    }
                ),
                encoding="utf-8",
            )

        monkeypatch.setenv("GEMINI_API_KEY", "fake")
        monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _cfg: tmp_path)
        monkeypatch.setattr(video_intel, "resolve_model", lambda *_a, **_kw: "stub-model")
        monkeypatch.setattr(video_intel, "load_taxonomy", lambda _od: {"version": 1, "concepts": {}})

        seen_taxonomies: list[dict] = []
        call_count = {"n": 0}

        def fake_process_concepts(client, types, video, mindmap_text, taxonomy, model, output_dir, ch_name, **kw):
            call_count["n"] += 1
            seen_taxonomies.append(json.loads(json.dumps(taxonomy)))
            prefix = kw.get("prefix")
            if call_count["n"] == 1:
                (output_dir / ch_name / f"{prefix}.concepts.json").write_text(
                    json.dumps({"concepts": [{"concept_id": "ai.concepts_unchanged", "preferred_label": "X"}]}),
                    encoding="utf-8",
                )
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_concepts", fake_process_concepts)

        config = {
            "channels": [{"name": channel, "url": f"https://youtube.com/@{channel}"}],
            "output_dir": str(tmp_path),
        }
        args = argparse.Namespace(channel=None, force=False, dry_run=False, model=None)
        cmd_concepts(args, config)

        assert call_count["n"] == 2
        assert "ai.concepts_unchanged" not in seen_taxonomies[0].get("concepts", {})
        assert "ai.concepts_unchanged" in seen_taxonomies[1].get("concepts", {})


# ---------------------------------------------------------------------------
# 5. An "error:" status must not accumulate.
# ---------------------------------------------------------------------------


class TestErrorStatusDoesNotAccumulate:
    def test_scan_skips_accumulation_on_error_status(self, tmp_path, monkeypatch):
        channel = "everyinc"
        channel_dir = tmp_path / channel
        v1 = _new_youtube_video("VIDE0001", "Erroring Video")
        v2 = _new_youtube_video("VIDE0002", "Second Video")
        _stub_scan_env(monkeypatch, tmp_path, {f"UC-https://youtube.com/@{channel}": [v1, v2]})
        monkeypatch.setattr(video_intel, "process_mindmap", _make_fake_mindmap({channel: channel_dir}))

        seen_taxonomies: list[dict] = []
        call_count = {"n": 0}

        def fake_process_concepts(client, types, video, mindmap_text, taxonomy, model, output_dir, ch_name, **kw):
            call_count["n"] += 1
            seen_taxonomies.append(json.loads(json.dumps(taxonomy)))
            prefix = kw.get("prefix")
            if call_count["n"] == 1:
                # Even though a concepts.json happens to exist on disk (e.g.
                # a stale leftover), an "error:" status must not accumulate
                # from it.
                (output_dir / ch_name / f"{prefix}.concepts.json").write_text(
                    json.dumps({"concepts": [{"concept_id": "ai.should_not_accumulate", "preferred_label": "X"}]}),
                    encoding="utf-8",
                )
                return prefix, "error: simulated Gemini failure"
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_concepts", fake_process_concepts)

        config = {
            "channels": [{"name": channel, "url": f"https://youtube.com/@{channel}", "auto_concepts": True}],
            "output_dir": str(tmp_path),
            "auto_concepts": True,
        }
        cmd_scan(_scan_args(channel=channel), config)

        assert call_count["n"] == 2
        assert "ai.should_not_accumulate" not in seen_taxonomies[1].get("concepts", {})
