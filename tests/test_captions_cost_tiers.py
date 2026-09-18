"""The free-captions tiers cost what the docs say they cost (#225).

`skills/video-intel/SKILL.md` used to say that `transcript_source: yt-captions`
"Skips Gemini entirely". It skips the TRANSCRIPT call only: with the default
`mindmap_source: auto` the caption-built transcript still feeds a text-only
mindmap call, and the top-level `auto_concepts: true` still runs a concepts
call. An operator following that sentence paid two Gemini calls per video
believing they paid none.

The first correction over-corrected the other way, presenting a four-knob
"zero-Gemini recipe" as if it were the way to run such a channel. That is also
wrong: the expensive call is the one that watches the video, the two text calls
are cents, and turning them off costs the mindmap triage surface, `concepts.json`
and therefore concept-mode `search` and the taxonomy.

So the docs describe a SPECTRUM, and this file pins the actual call count at
each tier by driving the real `cmd_scan` against a counting stub. A test that
asserted zero at every tier would re-encode the original mistake.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi

TIERS = {
    # (label, extra channel config)
    "free-captions": {},
    "no-mindmap": {"mindmap_source": "none"},
    "no-concepts": {"auto_concepts": False},
    "zero-gemini": {"mindmap_source": "none", "auto_concepts": False},
}


@pytest.fixture
def count_gemini_calls(monkeypatch, tmp_path):
    """Drive a real one-video scan and count every Gemini entry point."""

    def _run(extra_cfg):
        calls: list[str] = []

        def fake_text(*_a, **_k):
            calls.append("text")
            return "## Theme\n\n* **Sub**\n  - a point (0:10)"

        def fake_video(*_a, **_k):
            calls.append("video")
            return "## Theme\n\n* **Sub**\n  - a point (0:10)"

        monkeypatch.setattr(vi, "call_gemini_text", fake_text)
        monkeypatch.setattr(vi, "call_gemini", fake_video)
        # A REALISTIC track. A one-cue stub trips the issue #157 quality guard
        # (monolithic_severe), which correctly makes resolve_mindmap_source treat
        # the transcript as unavailable and fall back to the expensive
        # source="video" path - so the harness would measure the wrong tier.
        cues = [(float(i * 12), f"sentence number {i} about the topic") for i in range(60)]
        monkeypatch.setattr(
            vi,
            "fetch_english_captions",
            lambda vid, **_kw: __import__("youtube_captions").CaptionsResult(cues, True, "en"),
        )
        monkeypatch.setattr(vi, "resolve_output_dir", lambda _c, **_k: tmp_path)
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **k: object())
        from unittest.mock import MagicMock

        monkeypatch.setattr(vi, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(vi, "create_client", lambda *_a, **_k: object())
        monkeypatch.setattr(vi, "load_prompt", lambda _n: "PROMPT")
        monkeypatch.setattr(vi, "resolve_model", lambda *_a, **_k: "stub-model")
        monkeypatch.setattr(vi, "get_channel_id", lambda *_a, **_k: ("UCabc", "u"))
        monkeypatch.setattr(vi, "is_short", lambda *_a, **_k: False)
        monkeypatch.setattr(
            vi,
            "fetch_channel_videos",
            lambda *_a, **_k: [
                {
                    "video_id": "vid00000001",
                    "title": "A Video",
                    "published": "2026-09-11",
                    "url": "https://www.youtube.com/watch?v=vid00000001",
                    "duration_iso": "PT12M",
                }
            ],
        )
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _y, _ids: {"vid00000001": "PT12M"})
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda *_a, **_k: {})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
        monkeypatch.setattr(vi, "record_alt_title_if_rotated", lambda *_a, **_k: False)
        monkeypatch.setattr(vi, "render_headline_digest", lambda *_a, **_k: None)
        monkeypatch.setattr(vi, "load_taxonomy", lambda _d: {"concepts": {}})
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        monkeypatch.setenv("YOUTUBE_API_KEY", "k")

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
        config = {
            "auto_concepts": True,
            "channels": [
                {
                    "name": "chan",
                    "url": "https://youtube.com/@chan",
                    "auto_transcript": "all",
                    "transcript_source": "yt-captions",
                    "skip_shorts": False,
                    **extra_cfg,
                }
            ],
        }
        vi.cmd_scan(args, config)
        return calls

    return _run


class TestTheTiersCostWhatTheDocsSay:
    def test_free_captions_alone_still_makes_gemini_calls(self, count_gemini_calls):
        """The headline correction. `yt-captions` alone is NOT zero-Gemini."""
        calls = count_gemini_calls(TIERS["free-captions"])
        assert len(calls) > 0, (
            "transcript_source: yt-captions made no Gemini call - if this is genuinely true now, "
            "the docs and this test both need rewriting, because they say it makes two"
        )

    def test_the_transcript_itself_costs_nothing(self, count_gemini_calls):
        """What the knob DOES buy: no multimodal call watches the video."""
        calls = count_gemini_calls(TIERS["free-captions"])
        assert "video" not in calls, "a multimodal Gemini call ran on a yt-captions channel"

    def test_turning_off_the_mindmap_reduces_the_count(self, count_gemini_calls):
        base = len(count_gemini_calls(TIERS["free-captions"]))
        fewer = len(count_gemini_calls(TIERS["no-mindmap"]))
        assert fewer < base, f"mindmap_source: none did not reduce the call count ({base} -> {fewer})"

    def test_the_zero_tier_makes_no_gemini_call_at_all(self, count_gemini_calls):
        calls = count_gemini_calls(TIERS["zero-gemini"])
        assert calls == [], f"the zero-Gemini recipe still called Gemini: {calls}"

    def test_the_tiers_are_monotonic(self, count_gemini_calls):
        """Each subtraction may only remove calls, never add them. This is the
        property the docs' table asserts, stated as an ordering rather than as
        four magic numbers that would need updating on any unrelated change."""
        counts = {name: len(count_gemini_calls(cfg)) for name, cfg in TIERS.items()}
        assert counts["zero-gemini"] <= counts["no-mindmap"] <= counts["free-captions"]
        assert counts["zero-gemini"] <= counts["no-concepts"] <= counts["free-captions"]
        assert counts["zero-gemini"] == 0


class TestTheDocsDoNotClaimZeroForTheOneKnobCase:
    """The sentence that started this. A docs guard, because the claim is what
    caused the loss, not the code."""

    def _living_docs(self):
        root = Path(__file__).resolve().parent.parent
        return [
            root / "README.md",
            root / "skills" / "video-intel" / "SKILL.md",
            root / "skills" / "video-intel-search" / "SKILL.md",
            root / "config.yaml.example",
        ]

    def test_no_living_doc_says_yt_captions_skips_gemini_entirely(self):
        offenders = []
        for doc in self._living_docs():
            if not doc.exists():
                continue
            text = doc.read_text(encoding="utf-8")
            for line in text.splitlines():
                low = line.lower()
                if "yt-captions" in low and "skips gemini entirely" in low:
                    offenders.append(f"{doc.name}: {line.strip()[:110]}")
        assert not offenders, "a living doc claims yt-captions skips Gemini entirely:\n" + "\n".join(offenders)

    def test_the_four_knob_recipe_is_documented_somewhere(self):
        """The zero tier has to remain reachable from the docs, or the
        correction swings back to 'you cannot turn it off'."""
        joined = "\n".join(d.read_text(encoding="utf-8") for d in self._living_docs() if d.exists())
        for knob in ("transcript_source: yt-captions", "mindmap_source: none", "auto_concepts: false"):
            assert knob in joined, f"the zero-Gemini recipe no longer documents {knob}"
