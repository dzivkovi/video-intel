"""Both prompt==0 guards must agree with the gemini_common seam (issue #125).

The #125 fix moved the None-vs-zero decision into `_coerce_token_count` so both
confabulation guards would inherit one interpretation. That only compounds if
the guards are actually asserted against the same shape table - otherwise the
seam can be changed and the guard suites keep passing while the guards silently
stop guarding.

Two properties, driven through the REAL `process_mindmap` and `process_transcript`:

* an unreadable shape must NOT trip either guard (that is the drift-hardening
  half of #125: a rename must not read as "every video is a confabulation");
* BOTH encodings of a genuine zero - a literal `0` and the `None` a
  protobuf-JSON serializer emits for a zero-valued integer - MUST trip both
  guards. This is the half that a per-field "prompt is always sent" assumption
  would have broken, muting the guard in exactly the case it exists for.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from usage_shapes import CONFABULATION_PROMPT_VALUES, UNREADABLE_SHAPES, AttrErrorProperty, MissingAttr

import video_intel as vi

MINDMAP_BODY = "## Topic\n\n* bullet (0:00)\n"

_TRANSCRIPT_PAYLOAD = {
    "transcripts": [{"start": "00:00:01", "voice": 1, "text": "fabricated line"}],
    "screen_content": [],
    "speakers": [{"voice": 1, "name": "Host"}],
}


def _usage(prompt):
    """A usage_metadata carrying `prompt`, with everything else healthy."""
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=100,
            total_token_count=100,
        )
    )


#: Shapes that must leave both guards quiet. The two classes stand in for an SDK
#: rename (attribute gone / attribute raising AttributeError).
QUIET_SHAPES = [pytest.param(_usage(v), id=name) for name, v in UNREADABLE_SHAPES] + [
    pytest.param(SimpleNamespace(usage_metadata=MissingAttr()), id="missing_attr"),
    pytest.param(SimpleNamespace(usage_metadata=AttrErrorProperty()), id="attr_error_property"),
    pytest.param(SimpleNamespace(usage_metadata=None), id="usage_metadata_none"),
]

#: Shapes that must trip both guards.
TRIP_SHAPES = [pytest.param(_usage(v), id=f"prompt_{v!r}") for v in CONFABULATION_PROMPT_VALUES]


@pytest.fixture
def video():
    return {
        "video_id": "9exWJmbKeMo",
        "url": "https://www.youtube.com/watch?v=9exWJmbKeMo",
        "title": "A Talk",
        "published": "2026-08-12",
    }


@pytest.fixture
def fake_types():
    return SimpleNamespace(MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH"))


def _stub(monkeypatch, response, body):
    def fake_call_gemini(client, types, media_uri, prompt_text, model, response_json=False, **kw):
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(response)
        return body

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)


class TestVideoMindmapGuardMatchesTheSeam:
    @pytest.mark.parametrize("response", QUIET_SHAPES)
    def test_unreadable_prompt_does_not_trip_the_mindmap_guard(
        self, response, video, fake_types, tmp_path, monkeypatch
    ):
        _stub(monkeypatch, response, MINDMAP_BODY)

        prefix, status = vi.process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=video,
            prompt_text="P",
            model="stub-model",
            output_dir=tmp_path,
            channel_name="demo",
            source="video",
        )

        assert "confabulation" not in status, "unreadable is not proof of confabulation"
        assert (tmp_path / "demo" / f"{prefix}.mindmap.md").exists()

    @pytest.mark.parametrize("response", TRIP_SHAPES)
    def test_every_encoding_of_zero_trips_the_mindmap_guard(self, response, video, fake_types, tmp_path, monkeypatch):
        _stub(monkeypatch, response, MINDMAP_BODY)

        prefix, status = vi.process_mindmap(
            client=MagicMock(),
            types=fake_types,
            video=video,
            prompt_text="P",
            model="stub-model",
            output_dir=tmp_path,
            channel_name="demo",
            source="video",
        )

        assert "confabulation" in status, f"a zero-token prompt must be refused, got {status!r}"
        assert not (tmp_path / "demo" / f"{prefix}.mindmap.md").exists()


class TestTranscriptGuardMatchesTheSeam:
    @staticmethod
    def _run(tmp_path, fake_types):
        return vi.process_transcript(
            object(),
            fake_types,
            {
                "video_id": "vid123",
                "url": "https://www.youtube.com/watch?v=vid123",
                "title": "T",
                "published": "2026-06-13",
            },
            "PROMPT",
            "stub-model",
            tmp_path / "demo",
            "2026-06-13-t",
            transcript_source="gemini",
        )

    @pytest.mark.parametrize("response", QUIET_SHAPES)
    def test_unreadable_prompt_does_not_trip_the_transcript_guard(self, response, tmp_path, fake_types, monkeypatch):
        _stub(monkeypatch, response, json.dumps(_TRANSCRIPT_PAYLOAD))

        _, status = self._run(tmp_path, fake_types)

        assert "confabulation" not in status, "unreadable is not proof of confabulation"

    @pytest.mark.parametrize("response", TRIP_SHAPES)
    def test_every_encoding_of_zero_trips_the_transcript_guard(self, response, tmp_path, fake_types, monkeypatch):
        _stub(monkeypatch, response, json.dumps(_TRANSCRIPT_PAYLOAD))

        _, status = self._run(tmp_path, fake_types)

        assert "confabulation" in status, f"a zero-token prompt must be refused, got {status!r}"


class TestChunkedTranscriptGuardMatchesTheSeam:
    """The third guard (issue #123) reads the same seam and must agree with it.

    Added because this harness's own reason for existing - "otherwise the seam
    can be changed and the guard suites keep passing while the guards silently
    stop guarding" - applied to it too, and it was exempt.
    """

    @staticmethod
    def _run_one_chunk(tmp_path, fake_types, monkeypatch, response):
        payload = json.dumps(
            {
                "transcripts": [
                    {"start": f"00:{m:02d}:00", "voice": 1, "text": f"line {m}"} for m in (1, 12, 25, 38, 49)
                ],
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )

        def fake_call_gemini(client, types, media_uri, prompt_text, model, response_json=False, **kw):
            kw["on_response"](response)
            return payload

        monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)

        return vi._run_chunked_transcript_url(
            client=object(),
            types=fake_types,
            video={
                "video_id": "vid123",
                "url": "https://www.youtube.com/watch?v=vid123",
                "title": "T",
                "published": "2026-08-12",
            },
            prompt_text="P",
            model="m",
            channel_dir=tmp_path / "demo",
            prefix="p",
            chunks=[(0, 3000)],
            duration_seconds=3000,
            chunk_minutes=50,
            force=False,
        )

    @pytest.mark.parametrize("response", QUIET_SHAPES)
    def test_unreadable_prompt_does_not_discard_a_real_chunk(self, response, tmp_path, fake_types, monkeypatch):
        status = self._run_one_chunk(tmp_path, fake_types, monkeypatch, response)

        assert "confabulated" not in status, "unreadable is not proof of confabulation"

    @pytest.mark.parametrize("response", TRIP_SHAPES)
    def test_every_encoding_of_zero_discards_the_chunk(self, response, tmp_path, fake_types, monkeypatch):
        status = self._run_one_chunk(tmp_path, fake_types, monkeypatch, response)

        assert "confabulated" in status, f"a zero-token prompt must be refused, got {status!r}"
