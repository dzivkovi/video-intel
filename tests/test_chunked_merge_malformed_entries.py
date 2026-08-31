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
        # Full deterministic message, not a substring check (issue #171 P3
        # dual-review follow-up) - locks the count AND the caller prefix,
        # not just the presence of a chunk number somewhere in the text.
        assert transcript_warnings[0] == (
            "merge_chunked_transcripts: skipped 2 malformed transcripts entries: "
            "[chunk 3 entry 1] 'None', [chunk 6 entry 1] 'None'"
        )

    def test_healthy_multi_chunk_run_logs_no_warnings(self, caplog):
        chunk1 = _healthy_chunk([10, 20])
        chunk2 = _healthy_chunk([3010, 3020])
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merge_chunked_transcripts([(0, chunk1), (3000, chunk2)], chunk_duration_seconds=3000)
        assert caplog.records == []


class TestWholeTaskListDropsGetTheirOwnWarning:
    """Issue #171 P3, dual-review follow-up: a WHOLE task list being the
    wrong type is a categorically bigger loss than a per-entry skip - if
    `transcripts` comes back as a bare string, that chunk's entire dialogue
    is gone, not "1 malformed entry." This must be its own warning, naming
    the offending chunk(s), separate from `_log_skipped_entries`'s
    per-entry aggregate (which, after this fix, counts ONLY genuine
    per-entry skips).
    """

    def test_whole_list_drop_and_a_per_entry_skip_produce_two_separate_warnings(self, caplog):
        # Chunk 1: the WHOLE transcripts value is wrong-typed (a bare
        # string) - total content loss for that chunk. Chunk 2: transcripts
        # is a genuine list with one bad ENTRY inside it - a much smaller,
        # single-line loss. These must not be reported as the same kind of
        # event.
        chunk1 = {"transcripts": "garbage", "screen_content": [], "speakers": []}
        chunk2 = {
            "transcripts": [{"start": "00:01", "voice": 1, "text": "ok"}, "bad"],
            "screen_content": [],
            "speakers": [],
        }
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merge_chunked_transcripts([(0, chunk1), (3000, chunk2)], chunk_duration_seconds=3000)

        whole_list_warnings = [r.message for r in caplog.records if r.levelname == "WARNING" and "WHOLE" in r.message]
        per_entry_warnings = [
            r.message
            for r in caplog.records
            if r.levelname == "WARNING" and r.message.startswith("merge_chunked_transcripts: skipped")
        ]

        assert len(whole_list_warnings) == 1, "the whole-list drop must get its own warning"
        assert whole_list_warnings[0] == (
            "merge_chunked_transcripts: WHOLE transcripts task value was unusable (not a "
            "per-entry skip) in 1 chunk - that chunk's entire transcripts list is missing: "
            "[chunk 1] 'transcripts' task value is str, not a list - the whole task is "
            "unusable (not entry-by-entry malformed); ignoring it."
        )

        assert len(per_entry_warnings) == 1, "the per-entry skip must stay in its own separate aggregate"
        assert per_entry_warnings[0] == (
            "merge_chunked_transcripts: skipped 1 malformed transcripts entry: [chunk 2 entry 1] \"'bad'\""
        )
        # Chunk 1's whole-list loss must never be counted as "1 entry" in
        # the per-entry aggregate - that is exactly the under-reporting
        # this split exists to fix.
        assert "chunk 1" not in per_entry_warnings[0]

    def test_whole_list_drop_across_multiple_chunks_names_every_offending_chunk_in_one_warning(self, caplog):
        chunks = [
            (0, {"transcripts": "garbage-a", "screen_content": [], "speakers": []}),
            (3000, _healthy_chunk([3001])),
            (6000, {"transcripts": 42, "screen_content": [], "speakers": []}),
        ]
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merge_chunked_transcripts(chunks, chunk_duration_seconds=3000)

        whole_list_warnings = [r.message for r in caplog.records if r.levelname == "WARNING" and "WHOLE" in r.message]
        assert len(whole_list_warnings) == 1
        assert whole_list_warnings[0] == (
            "merge_chunked_transcripts: WHOLE transcripts task value was unusable (not a "
            "per-entry skip) in 2 chunks - that chunk's entire transcripts list is missing: "
            "[chunk 1] 'transcripts' task value is str, not a list - the whole task is "
            "unusable (not entry-by-entry malformed); ignoring it., [chunk 3] 'transcripts' "
            "task value is int, not a list - the whole task is unusable (not entry-by-entry "
            "malformed); ignoring it."
        )

    def test_healthy_multi_chunk_run_logs_no_whole_list_warning(self, caplog):
        chunk1 = _healthy_chunk([10, 20])
        chunk2 = _healthy_chunk([3010, 3020])
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merge_chunked_transcripts([(0, chunk1), (3000, chunk2)], chunk_duration_seconds=3000)
        assert [r.message for r in caplog.records if "WHOLE" in r.message] == []


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
        # Full deterministic message (issue #171 P3 dual-review follow-up),
        # not a "chunk 1 in message" substring check.
        assert (
            transcript_warnings[0]
            == "merge_chunked_transcripts: skipped 1 malformed transcripts entry: [chunk 1 entry 1] 'None'"
        )


def _base_healthy_chunk_json(dialogue_text: str, speaker_name: str, start: str = "00:05") -> dict:
    return {
        "transcripts": [{"start": start, "voice": 1, "text": dialogue_text}],
        "screen_content": [{"start": start, "end": start, "type": "slide", "description": f"{speaker_name} slide"}],
        "speakers": [{"voice": 1, "name": speaker_name}],
    }


class TestCallerLevelCoverageTableSpeakerReadDoesNotCrash:
    """Issue #171 P0 (dual-review follow-up): `_run_chunked_transcript_url`
    builds its coverage-table `speaker_names` list INSIDE the per-chunk
    loop, roughly 30 lines BEFORE it ever calls `merge_chunked_transcripts`
    - a separate, earlier consumer of the exact same malformed shapes the
    merge layer guards. This drives all SIX combinations (three task
    lists x {whole-list wrong type, non-dict entry inside a real list})
    through the REAL caller (only the Gemini call stubbed), because a
    caller-level crash here means one paid chunk call is made, zero files
    are written, and the exception propagates all the way out of
    `cmd_transcript`'s `try`/`finally` with no captions failover - a
    function-level-only test cannot see any of that (issue #161's review
    made exactly this point).
    """

    @staticmethod
    def _malformed_chunk1_json(task_key: str, mode: str) -> dict:
        chunk = _base_healthy_chunk_json("chunk1 dialogue", "Chunk1 Speaker")
        if mode == "whole_list":
            chunk[task_key] = "garbage-not-a-list"
        elif mode == "entry":
            chunk[task_key] = [None]
        else:  # pragma: no cover - guards the parametrize table itself
            raise ValueError(f"unknown mode {mode!r}")
        return chunk

    @staticmethod
    def _run_and_assert_survives(tmp_path, monkeypatch, task_key: str, mode: str, prefix: str):
        chunk1_json = TestCallerLevelCoverageTableSpeakerReadDoesNotCrash._malformed_chunk1_json(task_key, mode)
        chunk2_json = _base_healthy_chunk_json("healthy line two", "Chunk2 Speaker", start="50:10")
        responses = [json.dumps(chunk1_json), json.dumps(chunk2_json)]
        _stub_chunked_call_gemini(monkeypatch, responses)

        channel_dir = tmp_path / "demo"
        video = _video()

        # The load-bearing assertion IS that this call does not raise -
        # pytest fails the test loudly on an uncaught exception, which is
        # exactly the P0 crash shape (AttributeError from `s.get("voice")`
        # on a non-dict "entry" produced by iterating a bare string
        # character by character, or on a genuine non-dict list entry).
        status = vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix=prefix,
            chunks=[(0, 3000), (3000, 6000)],
            duration_seconds=6000,
            chunk_minutes=50,
            force=False,
        )

        transcript_path = channel_dir / f"{prefix}.transcript.md"
        assert transcript_path.exists(), "a real file must be written - a crash here writes nothing"
        body = transcript_path.read_text(encoding="utf-8")
        assert "healthy line two" in body, "the healthy sibling chunk's content must survive intact"
        assert status in ("done", "partial")
        return body

    @pytest.mark.parametrize("task_key", ["speakers", "transcripts", "screen_content"])
    def test_whole_list_wrong_type_in_chunk_1_does_not_crash_the_real_caller(self, tmp_path, monkeypatch, task_key):
        self._run_and_assert_survives(tmp_path, monkeypatch, task_key, "whole_list", f"2026-08-31-p0-whole-{task_key}")

    @pytest.mark.parametrize("task_key", ["speakers", "transcripts", "screen_content"])
    def test_non_dict_entry_in_chunk_1_does_not_crash_the_real_caller(self, tmp_path, monkeypatch, task_key):
        self._run_and_assert_survives(tmp_path, monkeypatch, task_key, "entry", f"2026-08-31-p0-entry-{task_key}")


class TestAssessChunkCoverageNonIterableTranscriptsScalar:
    """Issue #171 P0 follow-up, found while proving the coverage-table fix
    at the real-caller level: `_assess_chunk_coverage` read `parsed.get(
    "transcripts") or []`, which only substitutes `[]` for a FALSY value -
    a truthy non-list SCALAR (int, float, `True`) sailed through and
    crashed `for entry in transcripts` with `TypeError: <type> object is
    not iterable` a few lines later, inside `assess_transcript_artifact`.
    This is a DIFFERENT crash shape from the `speaker_names` P0 (that one
    iterated a STRING character by character and crashed later on
    `.get()`; a bare int/float/bool cannot even start iterating) on the
    SAME real path, reached BEFORE `merge_chunked_transcripts` for every
    chunk in `_run_chunked_transcript_url`.
    """

    @pytest.mark.parametrize("bad_value", [42, 3.14, True])
    def test_assess_chunk_coverage_does_not_crash_on_a_non_iterable_transcripts_scalar(self, bad_value):
        parsed = {"transcripts": bad_value, "screen_content": [], "speakers": []}
        # Must not raise TypeError - the load-bearing assertion.
        status, metrics = vi._assess_chunk_coverage(parsed, 0, 3000, 6000, 1)
        assert status in ("ok", "thin")
        assert isinstance(metrics, dict)

    def test_real_caller_survives_an_int_transcripts_value_in_chunk_1(self, tmp_path, monkeypatch):
        chunk1_json = {"transcripts": 42, "screen_content": [], "speakers": [{"voice": 1, "name": "Chunk1 Speaker"}]}
        chunk2_json = _base_healthy_chunk_json("healthy line two", "Chunk2 Speaker", start="50:10")
        responses = [json.dumps(chunk1_json), json.dumps(chunk2_json)]
        _stub_chunked_call_gemini(monkeypatch, responses)

        channel_dir = tmp_path / "demo"
        video = _video()

        status = vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix="2026-08-31-p0-int-transcripts",
            chunks=[(0, 3000), (3000, 6000)],
            duration_seconds=6000,
            chunk_minutes=50,
            force=False,
        )

        transcript_path = channel_dir / "2026-08-31-p0-int-transcripts.transcript.md"
        assert transcript_path.exists()
        body = transcript_path.read_text(encoding="utf-8")
        assert "healthy line two" in body
        assert status in ("done", "partial")


class TestDroppedMalformedEntriesShrinkTheSeverityDenominatorOnPurpose:
    """Issue #171 P3, dual-review follow-up: dropping malformed entries
    shrinks `classified_dialogue` (the issue #158 severity denominator),
    so a chunk that lost most of its entries to malformed-entry skipping
    can reach `chunk_window_mismatch_severe` on very few SURVIVING
    stamps. This is judged CORRECT and INTENTIONAL, not a bug to guard
    against: a chunk that lost 20 of 22 entries to malformation is
    genuinely degraded, and the 2 real stamps that did survive are exactly
    what the severity math is supposed to see - see the CLAUDE.md
    guardrail entry for this file. This test locks the shape in as a
    documented decision, not an emergent, unasserted side effect.
    """

    def test_two_surviving_out_of_window_stamps_among_twenty_dropped_entries_flags_severe(self):
        # 20 malformed (non-dict) transcript entries, dropped before they
        # are ever classified - per TestSkipsDoNotTouchWindowViolationCounters,
        # none of them touch classified_dialogue/out_of_window/unparseable.
        malformed_entries = [None] * 20
        # 2 genuine entries whose classified timestamps land OUTSIDE this
        # chunk's real [600, 1200] window (the same double-offset shape
        # tests/test_chunk_window_mismatch.py uses) - both survivors are
        # out of window, so the UNANIMOUS rule
        # (CHUNK_WINDOW_MISMATCH_UNANIMOUS_MIN_ENTRIES = 2) fires SEVERE
        # even though classified_dialogue (2) is below the majority rule's
        # own 4-entry floor.
        surviving_entries = [
            {"start": "21:40", "voice": 1, "text": "survivor one"},
            {"start": "22:30", "voice": 1, "text": "survivor two"},
        ]
        chunk2_json = {
            "transcripts": malformed_entries + surviving_entries,
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        chunk1_json = {
            "transcripts": [{"start": "00:10", "voice": 1, "text": "healthy"}],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }

        merged = vi.merge_chunked_transcripts(
            [(0, chunk1_json), (600, chunk2_json)],
            chunk_duration_seconds=600,
            chunk_bounds=[(0, 600), (600, 1200)],
        )

        chunk2_violations = merged["_chunk_window_violations"][1]
        assert chunk2_violations["chunk_index"] == 2
        # Exactly 2 of the 22 raw entries were ever classified - the 20
        # malformed ones never reached the classifier at all.
        assert chunk2_violations["classified_dialogue"] == 2
        assert chunk2_violations["out_of_window"] == 2
        assert chunk2_violations["unparseable"] == 0

        result = vi._classify_chunk_window_violations(merged["_chunk_window_violations"])
        assert vi.QUALITY_FLAG_CHUNK_WINDOW_MISMATCH_SEVERE in result["severe"]
        assert vi.transcript_quality_flags_are_severe(result["severe"]) is True


class TestUnhashableVoiceAndNameFieldsDoNotCrashTheChunkedMerger:
    """Issue #171 P1, dual-review follow-up: `_is_unhashable_json_scalar`
    guards every place `merge_chunked_transcripts` uses a raw JSON `voice`
    or `name` value as a dict key or membership-test operand. JSON has
    exactly two unhashable shapes - `dict` and `list` - and Gemini can, in
    principle, emit either where an int/str/None was expected. Three
    distinct call sites, all in this function:
      1. `voice_remap[voice] = ...` - an unhashable `voice` on an
         otherwise-usable speaker.
      2. `name not in name_to_global` / `name_to_global[name] = ...` - an
         unhashable `name`.
      3. `t.get("voice") in voice_remap` - an unhashable `voice` on a
         TRANSCRIPTS entry (never validated by `_usable_voice_id`, which
         only ever runs on SPEAKERS entries).
    """

    def test_unhashable_voice_on_a_speaker_does_not_crash_and_keeps_the_speaker_by_name(self):
        chunk = {
            "transcripts": [{"start": "00:01", "voice": 1, "text": "hi"}],
            "screen_content": [],
            "speakers": [{"voice": {"nested": "shape"}, "name": "Weird Voice Host"}],
        }
        merged = vi.merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        # The speaker is kept under its (hashable) name - exactly like the
        # pre-existing `voice is None` case already behaves - never
        # dropped just because its voice id could not be used as a key.
        assert len(merged["speakers"]) == 1
        assert merged["speakers"][0]["name"] == "Weird Voice Host"

    def test_unhashable_name_on_a_speaker_does_not_crash_and_is_skipped(self, caplog):
        chunk = {
            "transcripts": [{"start": "00:01", "voice": 1, "text": "hi"}],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": ["a", "list", "name"]}],
        }
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            merged = vi.merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        # No usable key to store this speaker under at all - skipped, not
        # crashed on, and reported through the same aggregate as any other
        # unusable speaker entry.
        assert merged["speakers"] == []
        speaker_warnings = [
            r.message for r in caplog.records if r.levelname == "WARNING" and "speakers entr" in r.message
        ]
        assert len(speaker_warnings) == 1
        # Expected snippet computed independently (repr()[:40], mirroring
        # `_entry_snippet`'s own truncation rule) rather than hand-typed,
        # since the raw text mixes single and double quotes in a way that
        # is error-prone to escape by hand - the point of this assertion
        # is locking count + chunk/entry label + caller prefix, which a
        # hand-typo in the snippet body would not actually test.
        malformed_speaker = {"voice": 1, "name": ["a", "list", "name"]}
        expected_snippet = repr(malformed_speaker)[:40]
        assert speaker_warnings[0] == (
            f"merge_chunked_transcripts: skipped 1 malformed speakers entry: [chunk 1 entry 0] {expected_snippet!r}"
        )

    def test_unhashable_voice_on_a_transcripts_entry_does_not_crash(self):
        chunk = {
            "transcripts": [
                {"start": "00:01", "voice": {"nested": "shape"}, "text": "weird voice line"},
                {"start": "00:02", "voice": 1, "text": "normal line"},
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        # Must not raise TypeError: unhashable type at `t.get("voice") in voice_remap`.
        merged = vi.merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)
        assert len(merged["transcripts"]) == 2
        texts = [t["text"] for t in merged["transcripts"]]
        assert "weird voice line" in texts
        assert "normal line" in texts
        # The entry with the unhashable voice keeps its raw (unremapped)
        # voice value - it simply never matched any (necessarily hashable)
        # voice_remap key.
        weird_entry = next(t for t in merged["transcripts"] if t["text"] == "weird voice line")
        assert weird_entry["voice"] == {"nested": "shape"}

    def test_real_caller_survives_an_unhashable_voice_on_a_transcripts_entry(self, tmp_path, monkeypatch):
        chunk1_json = {
            "transcripts": [
                {"start": "00:10", "voice": ["a", "list"], "text": "chunk1 weird voice line"},
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Chunk1 Speaker"}],
        }
        chunk2_json = _base_healthy_chunk_json("healthy line two", "Chunk2 Speaker", start="50:10")
        responses = [json.dumps(chunk1_json), json.dumps(chunk2_json)]
        _stub_chunked_call_gemini(monkeypatch, responses)

        channel_dir = tmp_path / "demo"
        video = _video()

        status = vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix="2026-08-31-p1-unhashable-voice",
            chunks=[(0, 3000), (3000, 6000)],
            duration_seconds=6000,
            chunk_minutes=50,
            force=False,
        )

        transcript_path = channel_dir / "2026-08-31-p1-unhashable-voice.transcript.md"
        assert transcript_path.exists()
        body = transcript_path.read_text(encoding="utf-8")
        assert "healthy line two" in body
        # The unhashable-voice entry survives too, just unrendered by name
        # - falls back to the "Speaker None" default (issue #171 P1
        # normalization in merge_transcript_json).
        assert "chunk1 weird voice line" in body
        assert status in ("done", "partial")
