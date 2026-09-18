"""The description must reach DISK, from every writer (#224, review of PR #229).

`tests/test_video_description.py` proves the field reaches the processing
helpers. That is not the property the feature needs, and the gap was not
academic: two live writers hand-rolled their identity block and dropped the
description entirely, while 28 tests passed.

- `_run_chunked_transcript_url` serves every video longer than `chunk_minutes`
  (default 30), which is 278 of 2,785 metas in the live corpus.
- `process_mindmap` is the ONLY writer on the `mindmap --url` path, the
  documented cherry-pick recipe for notify-only channels and the members-only
  403 recovery flow. It fetched the description and threw it away.

Combined, roughly 18% of corpus shapes would never have received one from a
scan, making `backfill-descriptions` a permanent post-scan step rather than the
one-time migration it is documented as.

So this file drives the REAL writers with only the Gemini call stubbed, reads
the meta.json back off disk, and adds the source-walk drift guard this repo
uses for exactly this class of bug.
"""

from __future__ import annotations

import json
import sys
import types as _types
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from unittest.mock import MagicMock

from youtube_captions import CaptionsResult

import video_intel as vi

DESC = "Link to Resources: https://example.test/notes\n\nLinks featured:\n- Archify - https://github.com/tt-ali/archify"

_PAYLOAD = {
    "transcripts": [{"start": "00:00", "voice": 1, "text": "hi"}],
    "screen_content": [],
    "speakers": [{"voice": 1, "name": "A"}],
}

_IDENTITY_MARKER = '"video_url"'
_DESCRIPTION_MARKER = '"description"'


def _usage(prompt_tokens: int = 5000):
    return _types.SimpleNamespace(
        usage_metadata=_types.SimpleNamespace(
            prompt_token_count=prompt_tokens,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=10,
            total_token_count=prompt_tokens + 10,
        )
    )


def _video(desc: str | None = DESC) -> dict:
    v = {
        "video_id": "vid123",
        "url": "https://www.youtube.com/watch?v=vid123",
        "title": "Test Title",
        "published": "2026-09-11",
    }
    if desc is not None:
        v["description"] = desc
    return v


def _meta(chan: Path, prefix: str) -> dict:
    return json.loads((chan / f"{prefix}.meta.json").read_text(encoding="utf-8"))


class TestEveryWriterPersistsItToDisk:
    def test_single_shot_transcript_writer(self, tmp_path, monkeypatch):
        def fake(client, types, media_uri, prompt, model, response_json=False, **kw):
            kw["on_response"](_usage())
            return json.dumps(_PAYLOAD)

        monkeypatch.setattr(vi, "call_gemini", fake)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda t, m: None)
        chan = tmp_path / "chan"
        chan.mkdir()
        vi.process_transcript(
            object(),
            None,
            _video(),
            "p",
            "stub-model",
            chan,
            "2026-09-11-test",
            transcript_source="gemini",
            media_resolution="LOW",
        )
        assert _meta(chan, "2026-09-11-test")["description"] == DESC

    def test_captions_writer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "fetch_english_captions", lambda vid: CaptionsResult([(0.0, "hi")], True, "en"))
        chan = tmp_path / "chan"
        chan.mkdir()
        vi.process_transcript(
            object(),
            None,
            _video(),
            "p",
            "stub-model",
            chan,
            "2026-09-11-test",
            transcript_source="yt-captions",
            media_resolution="LOW",
        )
        assert _meta(chan, "2026-09-11-test")["description"] == DESC

    def test_chunked_transcript_writer(self, tmp_path, monkeypatch):
        def fake(client, types, media_uri, prompt, model, response_json=False, **kw):
            cb = kw.get("on_response")
            if cb:
                cb(_usage())
            return json.dumps(
                {
                    "transcripts": [{"start": "00:10", "voice": 1, "text": "hi"}],
                    "screen_content": [],
                    "speakers": [{"voice": 1, "name": "A"}],
                }
            )

        monkeypatch.setattr(vi, "call_gemini", fake)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda t, m: None)
        chan = tmp_path / "chan"
        vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=_video(),
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=chan,
            prefix="2026-09-11-long",
            chunks=[(0, 240), (240, 480)],
            duration_seconds=480,
            chunk_minutes=4,
            force=False,
        )
        assert _meta(chan, "2026-09-11-long")["description"] == DESC

    def test_mindmap_writer(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "call_gemini_text", lambda *a, **k: "## Theme\n\n* **Sub**\n  - point (0:10)")
        chan = tmp_path / "chan"
        chan.mkdir()
        (chan / "2026-09-11-test.transcript.md").write_text("[00:10] A: hi", encoding="utf-8")
        vi.process_mindmap(
            MagicMock(),
            MagicMock(),
            _video(),
            "PROMPT",
            "stub-model",
            tmp_path,
            "chan",
            prefix="2026-09-11-test",
            source="transcript",
            transcript_path=chan / "2026-09-11-test.transcript.md",
        )
        assert _meta(chan, "2026-09-11-test")["description"] == DESC

    def test_the_mindmap_writer_must_not_clobber_when_the_video_has_none(self, tmp_path, monkeypatch):
        """Its meta_fields has no falsy-drop, so the write has to be GUARDED.
        An unconditional `video.get("description")` would write None here and
        erase what the transcript writer stored: the `--file` path carries no
        description at all."""
        monkeypatch.setattr(vi, "call_gemini_text", lambda *a, **k: "## Theme\n\n* **Sub**\n  - point (0:10)")
        chan = tmp_path / "chan"
        chan.mkdir()
        (chan / "2026-09-11-test.transcript.md").write_text("[00:10] A: hi", encoding="utf-8")
        (chan / "2026-09-11-test.meta.json").write_text(
            json.dumps({"video_id": "vid123", "description": DESC}), encoding="utf-8"
        )
        vi.process_mindmap(
            MagicMock(),
            MagicMock(),
            _video(desc=None),
            "PROMPT",
            "stub-model",
            tmp_path,
            "chan",
            prefix="2026-09-11-test",
            source="transcript",
            transcript_path=chan / "2026-09-11-test.transcript.md",
        )
        assert _meta(chan, "2026-09-11-test")["description"] == DESC, (
            "the mindmap writer erased a description it did not produce"
        )


class TestNoWriterDriftsFromTheSeam:
    """The "one seam" claim is only true if no writer hand-rolls identity.

    Scoped to sites that WRITE the key, not the many places that read it back
    with meta.get("video_url"). A walk that flags reads gets its exclusions
    widened until it guards nothing.
    """

    #: Sites that build identity where no description can exist, with the
    #: reason each is exempt. Keyed by enclosing function so the guard
    #: survives line-number churn.
    EXEMPT: ClassVar[dict[str, str]] = {
        "_transcript_identity_fields": "defines the seam",
        "_identity_from_mindmap_header": "reconstructs from a header; no description exists",
        "_identity_from_transcript_header": "reconstructs from a header; no description exists",
        "_cmd_transcript_impl": "local --file path; there is no snippet to read",
        "_cmd_process_impl": "local --file path; there is no snippet to read",
    }

    def _lines(self):
        src = Path(__file__).resolve().parent.parent / "scripts" / "video_intel.py"
        return src.read_text(encoding="utf-8").splitlines()

    def _write_sites(self):
        """(line number, enclosing function) for each identity WRITE."""
        import re

        lines = self._lines()
        out = []
        for i, line in enumerate(lines):
            if _IDENTITY_MARKER + ":" not in line:
                continue
            fn = "?"
            for j in range(i, -1, -1):
                m = re.match(r"^def (\w+)", lines[j])
                if m:
                    fn = m.group(1)
                    break
            out.append((i + 1, fn))
        return out

    def test_every_writer_either_uses_the_seam_or_handles_the_description(self):
        lines = self._lines()
        offenders = []
        for lineno, fn in self._write_sites():
            if fn in self.EXEMPT:
                continue
            window = chr(10).join(lines[max(0, lineno - 15) : lineno + 20])
            if "_transcript_identity_fields" in window or _DESCRIPTION_MARKER in window:
                continue
            offenders.append((lineno, fn))
        assert not offenders, "writer hand-rolls identity without handling the description: " + repr(offenders)

    def test_the_chunked_writer_no_longer_hand_rolls_identity(self):
        """It did, which is how 278 corpus metas would have missed the field.
        Pinned by NAME so a future edit cannot quietly reintroduce the copy."""
        assert all(fn != "_run_chunked_transcript_url" for _, fn in self._write_sites()), (
            "_run_chunked_transcript_url is building its own identity block again"
        )

    def test_the_walk_is_not_vacuous(self):
        """A walk whose pattern stops matching passes forever against nothing,
        and an allowlist covering every site is the same thing."""
        sites = self._write_sites()
        assert len(sites) >= 4, f"the walk found only {len(sites)} write sites; the pattern stopped matching"
        non_exempt = [fn for _, fn in sites if fn not in self.EXEMPT]
        assert non_exempt, "every write site is exempt; this guard no longer guards anything"
