"""Issue #171: merge_chunked_transcripts must degrade on a malformed
per-chunk entry, never crash a whole (already-paid) chunked Gemini
transcript run.

Issue #161 hardened `merge_transcript_json` - the FINAL merge-time guard -
so one malformed entry in a Gemini response is skipped instead of crashing
the whole (already-paid) call. `merge_chunked_transcripts`, which runs
BEFORE `merge_transcript_json` on the chunked path, never got the same
treatment. Three reachable crash sites on real chunked output:

  - `speakers`: a non-LIST value iterates character by character, and
    `s.get("voice")` raises AttributeError on the resulting non-dict
    "entries" (single characters); a non-dict entry inside a real list
    raises the same way.
  - `transcripts`: `dict(t)` on a non-dict `t` raises ValueError (a string)
    or TypeError (None) before `_usable_timestamp` in the final merge guard
    ever gets a chance to look at it.
  - `screen_content`: same shape as `transcripts`.

The `chunk_json` ROOT is already guarded (`if not isinstance(chunk_json,
dict): continue`) by issue #158/#161 work - untouched here.

Design decisions locked in by issue #171 (see CLAUDE.md Code Review
Guardrails, "A verifier/writer must use ... a single convention" pattern):
  - Reuse `_usable_task_list` (generalized with an optional `note_sink`) for
    the whole-list-wrong-type case, and an `isinstance(entry, dict)` guard
    mirroring the one already inside `_usable_timestamp`/`_usable_voice_id`
    for the per-entry case - one convention, two call sites.
  - Skip the malformed entry. Unlike `merge_transcript_json`, there is no
    "pass it through" option here: `dict(t)` IS the copy step, so an
    unusable entry cannot be carried forward.
  - ONE warning per task list for the WHOLE `merge_chunked_transcripts`
    call (not once per chunk), aggregated across chunks - each reported
    entry carries its CHUNK index too (e.g. "chunk 3 entry 7"), since a
    bare list index is meaningless once chunks are aggregated.
    `_log_skipped_entries` is generalized (a `caller` prefix + a composite
    label) rather than duplicated, mirroring #161's `[idx] 'snippet'`
    format.
  - Skipped entries never touch the issue #158 window-violation counters
    (`classified_dialogue`/`out_of_window`/`unparseable`) - a skipped entry
    was never classified, so it must not enter any of the three counters.
  - A malformed `speakers` entry loses only its name mapping (existing
    #161 rule, reused as-is here) - it must never drop a transcript entry;
    those fall back to the existing `voice_names.get(...)` default.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel as vi
from video_intel import merge_chunked_transcripts, merge_transcript_json


def _mmss(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _healthy_chunk(seconds: list[int], speaker_name: str = "Host", voice: int = 1) -> dict:
    return {
        "transcripts": [{"start": _mmss(s), "voice": voice, "text": f"line at {s}"} for s in seconds],
        "screen_content": [
            {"start": _mmss(seconds[0]), "end": _mmss(seconds[0] + 5), "type": "slide", "description": "healthy slide"}
        ],
        "speakers": [{"voice": voice, "name": speaker_name}],
    }


# ---------------------------------------------------------------------------
# 1. Non-list task VALUE per chunk (bare string) - the character-by-character
#    iteration crash.
# ---------------------------------------------------------------------------


class TestNonListTaskValuePerChunk:
    def test_non_list_speakers_does_not_crash(self):
        chunk = {"transcripts": [], "screen_content": [], "speakers": "garbage"}
        merged = merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        assert merged["speakers"] == []

    def test_non_list_transcripts_does_not_crash(self):
        chunk = {"transcripts": "00:01 hello", "screen_content": [], "speakers": []}
        merged = merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        assert merged["transcripts"] == []

    def test_non_list_screen_content_does_not_crash(self):
        chunk = {"transcripts": [], "screen_content": {"start": "00:05"}, "speakers": []}
        merged = merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        assert merged["screen_content"] == []

    def test_non_list_values_do_not_suppress_a_healthy_sibling_task_in_the_same_chunk(self):
        chunk = {
            "transcripts": "garbage",
            "screen_content": [{"start": "00:20", "type": "slide", "description": "fine"}],
            "speakers": [{"voice": 1, "name": "Alice"}],
        }
        merged = merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        assert merged["transcripts"] == []
        assert len(merged["screen_content"]) == 1
        assert merged["screen_content"][0]["description"] == "fine"
        assert len(merged["speakers"]) == 1


# ---------------------------------------------------------------------------
# 2. Non-dict ENTRY inside an otherwise-real list - the dict(t) / .get()
#    crash.
# ---------------------------------------------------------------------------


class TestNonDictEntryInsideList:
    @pytest.mark.parametrize("bad_entry", [None, "a bare string", 5, ["nested", "list"]])
    def test_bad_transcript_entry_does_not_crash(self, bad_entry):
        chunk = {
            "transcripts": [{"start": "00:01", "voice": 1, "text": "good"}, bad_entry],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        merged = merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        # The healthy sibling entry survives; the bad one is skipped, not
        # crashed on and not fabricated into a dict.
        assert len(merged["transcripts"]) == 1
        assert merged["transcripts"][0]["text"] == "good"

    @pytest.mark.parametrize("bad_entry", [None, "a bare string", 5, ["nested", "list"]])
    def test_bad_screen_content_entry_does_not_crash(self, bad_entry):
        chunk = {
            "transcripts": [],
            "screen_content": [
                {"start": "00:05", "end": "00:10", "type": "slide", "description": "good"},
                bad_entry,
            ],
            "speakers": [],
        }
        merged = merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        assert len(merged["screen_content"]) == 1
        assert merged["screen_content"][0]["description"] == "good"

    @pytest.mark.parametrize("bad_entry", [None, "a bare string", 5, ["nested", "list"]])
    def test_bad_speaker_entry_does_not_crash(self, bad_entry):
        chunk = {
            "transcripts": [{"start": "00:01", "voice": 1, "text": "hi"}],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}, bad_entry],
        }
        merged = merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        assert len(merged["speakers"]) == 1
        assert merged["speakers"][0]["name"] == "Host"
        # A malformed speaker entry must never drop a transcript entry.
        assert len(merged["transcripts"]) == 1


# ---------------------------------------------------------------------------
# 3. LOAD-BEARING: an entirely-malformed chunk must not poison a healthy
#    sibling chunk's content.
# ---------------------------------------------------------------------------


class TestSiblingChunkSurvivesAnEntirelyMalformedChunk:
    def test_healthy_chunk_2_content_survives_intact_when_chunk_1_is_entirely_malformed(self):
        malformed_chunk = {
            "transcripts": "not even a list",
            "screen_content": [None, "garbage", 5],
            "speakers": None,
        }
        healthy_chunk = _healthy_chunk([3010, 3020, 3030], speaker_name="Guest", voice=1)

        merged = merge_chunked_transcripts(
            [(0, malformed_chunk), (3000, healthy_chunk)],
            chunk_duration_seconds=3000,
        )

        # Only one speaker (Guest) is ever seen, so the global voice id is
        # deterministically 1 - asserted separately below, not baked into
        # this expectation via a self-reference into `merged`.
        assert merged["transcripts"] == [
            {"start": _mmss(s), "voice": 1, "text": f"line at {s}"} for s in [3010, 3020, 3030]
        ]
        assert len(merged["screen_content"]) == 1
        assert merged["screen_content"][0]["description"] == "healthy slide"
        assert len(merged["speakers"]) == 1
        assert merged["speakers"][0]["name"] == "Guest"

        # The whole thing must still be renderable by the final merge guard.
        fused = merge_transcript_json(merged, {})
        assert "line at 3010" in fused
        assert "Guest" in fused


# ---------------------------------------------------------------------------
# 4. Skipped entries must not touch the issue #158 window-violation math.
# ---------------------------------------------------------------------------


class TestSkipsDoNotTouchWindowViolationCounters:
    def test_malformed_entries_are_excluded_from_all_three_counters(self):
        chunk = {
            "transcripts": [
                {"start": _mmss(10), "voice": 1, "text": "a"},
                {"start": _mmss(20), "voice": 1, "text": "b"},
                None,  # non-dict entry: must not be counted anywhere
                "bad",  # non-dict entry: must not be counted anywhere
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        merged = merge_chunked_transcripts(
            [(0, chunk)],
            chunk_duration_seconds=3000,
            chunk_bounds=[(0, 3000)],
        )
        violations = merged["_chunk_window_violations"]
        assert len(violations) == 1
        # Exactly the two healthy entries were classified; the two skipped
        # entries contribute to none of the three counters.
        assert violations[0]["classified_dialogue"] == 2
        assert violations[0]["out_of_window"] == 0
        assert violations[0]["unparseable"] == 0

    def test_whole_list_wrong_type_produces_zero_classified_not_a_false_severe(self):
        chunk = {"transcripts": "garbage", "screen_content": [], "speakers": []}
        merged = merge_chunked_transcripts(
            [(0, chunk)],
            chunk_duration_seconds=3000,
            chunk_bounds=[(0, 3000)],
        )
        violations = merged["_chunk_window_violations"]
        assert violations[0]["classified_dialogue"] == 0
        assert violations[0]["out_of_window"] == 0
        assert violations[0]["unparseable"] == 0


# ---------------------------------------------------------------------------
# 5. Healthy multi-chunk input stays byte-identical to pre-change behavior.
#    Expectation built independently - never by calling the function twice.
# ---------------------------------------------------------------------------


class TestHealthyMultiChunkInputUnchanged:
    def test_two_healthy_chunks_merge_exactly_as_before(self):
        chunk1 = _healthy_chunk([10, 20], speaker_name="Alice", voice=1)
        chunk2 = _healthy_chunk([3010, 3020], speaker_name="Bob", voice=1)

        merged = merge_chunked_transcripts([(0, chunk1), (3000, chunk2)], chunk_duration_seconds=3000)

        # Independently hand-built expectation - global voice ids assigned
        # in first-seen order (Alice=1, Bob=2), transcripts/screen_content
        # preserved per chunk in order, timestamps unchanged (all values
        # already sit inside their own chunk's plausible relative window so
        # the classifier makes no offset).
        expected_transcripts = [
            {"start": "00:10", "voice": 1, "text": "line at 10"},
            {"start": "00:20", "voice": 1, "text": "line at 20"},
            {"start": "50:10", "voice": 2, "text": "line at 3010"},
            {"start": "50:20", "voice": 2, "text": "line at 3020"},
        ]
        expected_screen_content = [
            {"start": "00:10", "end": "00:15", "type": "slide", "description": "healthy slide"},
            {"start": "50:10", "end": "50:15", "type": "slide", "description": "healthy slide"},
        ]
        expected_speakers = [
            {"voice": 1, "name": "Alice"},
            {"voice": 2, "name": "Bob"},
        ]

        assert merged["transcripts"] == expected_transcripts
        assert merged["screen_content"] == expected_screen_content
        assert merged["speakers"] == expected_speakers


# ---------------------------------------------------------------------------
# 6. Aggregated warning: ONE per task list, naming every offending chunk.
# ---------------------------------------------------------------------------


class TestAggregatedWarningNamesChunkIndices:
    def test_one_warning_per_task_list_across_all_chunks_not_one_per_chunk(self, caplog):
        chunks = []
        for i in range(8):
            if i in (2, 5):
                # Malformed transcript entry in chunks 3 and 6 (1-based).
                bad_chunk = {
                    "transcripts": [{"start": _mmss(i * 3000 + 1), "voice": 1, "text": "ok"}, None],
                    "screen_content": [],
                    "speakers": [{"voice": 1, "name": "Host"}],
                }
                chunks.append((i * 3000, bad_chunk))
            else:
                chunks.append((i * 3000, _healthy_chunk([i * 3000 + 1])))

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merge_chunked_transcripts(chunks, chunk_duration_seconds=3000)

        transcript_warnings = [
            r.message for r in caplog.records if r.levelname == "WARNING" and "transcripts entr" in r.message
        ]
        assert len(transcript_warnings) == 1, "must be aggregated into exactly one warning, not one per chunk"
        assert "chunk 3" in transcript_warnings[0]
        assert "chunk 6" in transcript_warnings[0]

    def test_whole_list_wrong_type_and_per_entry_skips_share_the_same_aggregated_warning(self, caplog):
        chunk1 = {"transcripts": "garbage", "screen_content": [], "speakers": []}
        chunk2 = {
            "transcripts": [{"start": "00:01", "voice": 1, "text": "ok"}, "bad"],
            "screen_content": [],
            "speakers": [],
        }
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merge_chunked_transcripts([(0, chunk1), (3000, chunk2)], chunk_duration_seconds=3000)

        transcript_warnings = [
            r.message for r in caplog.records if r.levelname == "WARNING" and "transcripts entr" in r.message
        ]
        assert len(transcript_warnings) == 1
        assert "chunk 1" in transcript_warnings[0]
        assert "chunk 2" in transcript_warnings[0]

    def test_healthy_multi_chunk_run_logs_no_warnings(self, caplog):
        chunk1 = _healthy_chunk([10, 20])
        chunk2 = _healthy_chunk([3010, 3020])
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merge_chunked_transcripts([(0, chunk1), (3000, chunk2)], chunk_duration_seconds=3000)
        assert caplog.records == []


# ---------------------------------------------------------------------------
# 7. Caller-level: the real `_run_chunked_transcript_url` path, stubbed
#    Gemini client only (issue #161's review specifically faulted
#    function-only coverage - the caller-level path is required, not
#    optional).
# ---------------------------------------------------------------------------


def _video(video_id: str = "malformed00001") -> dict:
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "A Chunked Talk With A Malformed Entry",
        "published": "2026-08-31",
    }


def _fake_gemini_response(prompt_tokens: int = 5000, candidates_tokens: int = 100):
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=candidates_tokens,
            total_token_count=prompt_tokens + candidates_tokens,
        ),
        candidates=[SimpleNamespace(finish_reason="STOP")],
    )


def _stub_chunked_call_gemini(monkeypatch, responses: list[str]):
    def fake_call_gemini(_client, _types, _uri, _prompt, _model, **kw):
        idx = fake_call_gemini.n
        fake_call_gemini.n += 1
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(_fake_gemini_response())
        return responses[idx]

    fake_call_gemini.n = 0
    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)


class TestCallerLevelDoesNotCrashOnMalformedChunkEntry:
    def test_real_caller_survives_a_non_dict_entry_in_one_chunk_and_keeps_the_healthy_chunk(
        self, tmp_path, monkeypatch, caplog
    ):
        chunk1_json = {
            "transcripts": [{"start": "00:10", "voice": 1, "text": "healthy line one"}, None],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        chunk2_json = {
            "transcripts": [{"start": "50:10", "voice": 1, "text": "healthy line two"}],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        responses = [json.dumps(chunk1_json), json.dumps(chunk2_json)]
        _stub_chunked_call_gemini(monkeypatch, responses)

        channel_dir = tmp_path / "demo"
        video = _video()

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            status = vi._run_chunked_transcript_url(
                client=MagicMock(),
                types=MagicMock(),
                video=video,
                prompt_text="PROMPT",
                model="stub-model",
                channel_dir=channel_dir,
                prefix="2026-08-31-malformed-entry-talk",
                chunks=[(0, 3000), (3000, 6000)],
                duration_seconds=6000,
                chunk_minutes=50,
                force=False,
            )

        transcript_path = channel_dir / "2026-08-31-malformed-entry-talk.transcript.md"
        assert transcript_path.exists()
        body = transcript_path.read_text(encoding="utf-8")
        assert "healthy line one" in body
        assert "healthy line two" in body
        assert status in ("done", "partial")

        transcript_warnings = [
            r.message for r in caplog.records if r.levelname == "WARNING" and "transcripts entr" in r.message
        ]
        assert len(transcript_warnings) == 1
        assert "chunk 1" in transcript_warnings[0]
