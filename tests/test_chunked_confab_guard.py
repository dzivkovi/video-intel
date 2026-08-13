"""Issue #123: the chunked transcript path must run the prompt==0 guard too.

`_run_chunked_transcript_url` passed `log_usage_metadata` as its per-chunk
`on_response` but only LOGGED the counts. A chunk where Gemini reported
`prompt=0` - the confabulation signature, meaning it ingested no video and wrote
from priors - was parsed and stitched into the final `.transcript.md` with
`transcript_status: ok`.

Chunking auto-triggers above `--chunk-minutes` (default 50) on both `--url` and
`--file`, so this was reachable on any long-form video. It is the last member of
the family closed by #60 (single-shot transcript) and #119 (video mindmap).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import video_intel as vi


def _usage(prompt):
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=5000,
            total_token_count=(prompt + 5000) if isinstance(prompt, int) and not isinstance(prompt, bool) else 5000,
        )
    )


def _hms(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _payload(marker: str, start_secs: int, end_secs: int):
    """A well-formed chunk envelope whose text is identifiable in the output.

    Timestamps are spread densely across the chunk's own window so the
    pre-existing `_assess_chunk_coverage` thin-chunk detector stays quiet -
    otherwise every chunk would be flagged thin and `transcript_status` would
    read `partial` for reasons that have nothing to do with this guard.
    """
    span = end_secs - start_secs
    marks = [start_secs + int(span * f) for f in (0.02, 0.25, 0.5, 0.75, 0.97)]
    return json.dumps(
        {
            "transcripts": [
                {"start": _hms(s), "voice": 1, "text": f"{marker} spoken content at {i}"} for i, s in enumerate(marks)
            ],
            "screen_content": [{"start": _hms(marks[0]), "text": f"{marker} on screen"}],
            "speakers": [{"voice": 1, "name": f"Speaker {marker}"}],
        }
    )


@pytest.fixture
def fake_types():
    return SimpleNamespace(MediaResolution=SimpleNamespace(MEDIA_RESOLUTION_LOW="LOW", MEDIA_RESOLUTION_HIGH="HIGH"))


@pytest.fixture
def video():
    return {
        "video_id": "vid123",
        "url": "https://www.youtube.com/watch?v=vid123",
        "title": "A Long Talk",
        "published": "2026-08-12",
    }


def _run(tmp_path: Path, video, fake_types, monkeypatch, prompts):
    """Drive the real chunked path over len(prompts) chunks.

    `prompts[i]` is the prompt-token count Gemini reports for chunk i+1.
    """
    calls = {"n": 0}

    def fake_call_gemini(client, types, media_uri, prompt_text, model, response_json=False, **kw):
        idx = calls["n"]
        calls["n"] += 1
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(_usage(prompts[idx]))
        return _payload(f"CHUNK{idx + 1}", idx * 3000, (idx + 1) * 3000)

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)

    channel_dir = tmp_path / "demo"
    chunks = [(i * 3000, (i + 1) * 3000) for i in range(len(prompts))]
    status = vi._run_chunked_transcript_url(
        client=object(),
        types=fake_types,
        video=video,
        prompt_text="PROMPT",
        model="stub-model",
        channel_dir=channel_dir,
        prefix="2026-08-12-a-long-talk",
        chunks=chunks,
        duration_seconds=3000 * len(prompts),
        chunk_minutes=50,
        force=False,
    )
    body = (channel_dir / "2026-08-12-a-long-talk.transcript.md").read_text(encoding="utf-8")
    meta = json.loads((channel_dir / "2026-08-12-a-long-talk.meta.json").read_text(encoding="utf-8"))
    return status, body, meta, channel_dir


class TestChunkedConfabulationGuard:
    """The issue's stated contract: chunk 2 of 3 reports prompt=0."""

    def test_fabricated_chunk_is_absent_from_the_stitched_body(self, tmp_path, video, fake_types, monkeypatch):
        _, body, _, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, 0, 260000])

        assert "CHUNK2" not in body, "a prompt=0 chunk must never be stitched in"

    def test_surrounding_chunks_survive_intact(self, tmp_path, video, fake_types, monkeypatch):
        _, body, _, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, 0, 260000])

        assert "CHUNK1" in body
        assert "CHUNK3" in body, "one bad chunk must not cost the whole video"

    def test_coverage_table_records_the_window_as_failed(self, tmp_path, video, fake_types, monkeypatch):
        _, body, _, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, 0, 260000])

        assert "FAILED" in body
        assert "confabulation" in body, (
            "the coverage table is the operator's audit surface - a discarded window must say WHY"
        )

    def test_transcript_status_is_partial(self, tmp_path, video, fake_types, monkeypatch):
        status, _, meta, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, 0, 260000])

        assert meta["transcript_status"] == "partial"
        assert status == "partial"

    def test_identity_is_still_stamped(self, tmp_path, video, fake_types, monkeypatch):
        """Issue #66 contract holds on the guarded path too."""
        _, _, meta, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, 0, 260000])

        assert meta["video_id"] == "vid123"

    def test_raw_sidecar_is_kept_for_forensics(self, tmp_path, video, fake_types, monkeypatch):
        """Mirrors the single-shot guard: the discarded text stays inspectable."""
        _, _, _, channel_dir = _run(tmp_path, video, fake_types, monkeypatch, [250000, 0, 260000])

        sidecars = list(channel_dir.glob("*.transcript.raw.chunk*.txt"))
        assert sidecars, "the discarded chunk text must be recoverable"
        assert "CHUNK2" in sidecars[0].read_text(encoding="utf-8")


class TestGuardDoesNotOverreach:
    def test_all_healthy_chunks_stay_ok(self, tmp_path, video, fake_types, monkeypatch):
        status, body, meta, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, 240000, 260000])

        assert meta["transcript_status"] == "ok"
        assert status == "done"
        for marker in ("CHUNK1", "CHUNK2", "CHUNK3"):
            assert marker in body

    @pytest.mark.parametrize(
        "unreadable",
        ["oops", 12.5, [], True, -1],
        ids=["string", "float", "empty_list", "bool", "negative"],
    )
    def test_unreadable_usage_does_not_trip_the_guard(self, tmp_path, video, fake_types, monkeypatch, unreadable):
        """Unreadable is not proof of confabulation (the #125 contract)."""
        _, body, meta, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, unreadable, 260000])

        assert "CHUNK2" in body, "a count we could not read must not discard a real chunk"
        assert meta["transcript_status"] == "ok"

    def test_prompt_omitted_on_the_wire_still_trips(self, tmp_path, video, fake_types, monkeypatch):
        """Issue #125: a zero omitted by the serializer arrives as 0, not None."""
        _, body, meta, _ = _run(tmp_path, video, fake_types, monkeypatch, [250000, None, 260000])

        assert "CHUNK2" not in body
        assert meta["transcript_status"] == "partial"

    def test_every_chunk_confabulated_is_not_written_as_a_transcript(self, tmp_path, video, fake_types, monkeypatch):
        """Nothing real was ingested, so there is no transcript to keep."""

        def fake_call_gemini(client, types, media_uri, prompt_text, model, response_json=False, **kw):
            on_response = kw.get("on_response")
            if on_response is not None:
                on_response(_usage(0))
            return _payload("FAKE", 0, 3000)

        monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)

        channel_dir = tmp_path / "demo"
        status = vi._run_chunked_transcript_url(
            client=object(),
            types=fake_types,
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix="2026-08-12-a-long-talk",
            chunks=[(0, 3000), (3000, 6000)],
            duration_seconds=6000,
            chunk_minutes=50,
            force=False,
        )

        assert status.startswith("error:")
        assert not (channel_dir / "2026-08-12-a-long-talk.transcript.md").exists()
