"""Issue #161: merge_transcript_json must degrade on one malformed entry,
never crash a whole (already-paid) Gemini transcript call.

Observed live in the 2026-08-30 A/B eval: a real gemini-3.7-flash response
returned a screen_content entry with no "start", and the unguarded
`sc["start"]` access raised KeyError, destroying the entire merged transcript
for a call that had already been billed.

Design decisions locked in by the issue (see CLAUDE.md Code Review
Guardrails and the issue body itself):
  - Skip the malformed entry. Never synthesize a timestamp (a fabricated
    "00:00" would sort to the top and corrupt deep-links).
  - "Usable" for a `start` timestamp means: the entry is a dict, key present,
    and value is a non-blank string. None, "", whitespace-only, a dict, a
    list, or an int are all malformed.
  - A speaker missing a usable `voice` id is skipped from the name map only
    - it must never drop a transcript entry; those fall back to the
      existing voice_names.get(...) default path. `voice` is a Gemini
      integer ID (see prompts/transcript.md), not a string, so a legitimate
      voice value is an int - the "non-blank string" rule does not apply to
      it. A voice id is usable when the speaker is a dict and its voice is
      a hashable (int or str) scalar, not None/"".
  - One WARNING per task list (not per entry), naming the list, the count
    skipped, and a truncated snippet of the first few offending entries.
  - No signature change, no new meta.json field - the WARNING is the
    operator surface.
  - A response where every entry is malformed must not crash; it produces a
    transcript with no dialogue (the existing #157 guards flag that on
    their own).

Second commit (dual-review follow-up, both layers found the same four
must-fix items - see the debrief for the full pass-by-pass breakdown):
  - P1: the writer (merge_transcript_json) and the quality assessor
    (assess_transcript_artifact) must agree on what counts as a usable
    entry - they now share ONE `_usable_timestamp` predicate. See
    TestWriterAndAssessorAgreeOnUsableEntries, and the caller-level tests
    in TestCallerLevelAllMalformedNeverStampsComplete which prove the
    consequence: an all-malformed response is now correctly flagged
    `monolithic_severe` and never stamped `transcript_status: "complete"`.
  - P1: `_usable_voice_id` requires a hashable (int/str) scalar - a dict or
    list `voice` used to pass the old `is not None` check and then raise
    `TypeError: unhashable type` on the very next line. Both predicates
    also guard `isinstance(entry, dict)` first, so a non-dict item inside a
    task list (`None`, a bare string) is skipped instead of raising
    AttributeError from `.get()`. `merge_transcript_json` also refuses a
    non-dict `raw_json` root after the list-unwrap.
  - P1: the chunked path (`merge_chunked_transcripts` ->
    `_classify_and_offset_timestamp`) used to crash on a non-string `start`
    BEFORE the merge-time guard ever got a chance to skip it - see
    TestChunkedPathDoesNotCrashOnNonStringStart.
  - P3: a whitespace-only `start` ("   ") used to pass a bare `!= ""` check
    and still sort to the top - now rejected via `.strip()`.

Third commit (adversarial re-verify PASSED all four second-commit items;
three small follow-ups before merge):
  - The load-bearing one: `_usable_voice_id` excluding `float` was a
    REGRESSION the second commit would have shipped knowingly - a float
    voice (hash(1.0) == hash(1)) worked before this whole fix landed. See
    TestFloatVoiceIdKeepsItsName.
  - A non-list task VALUE (e.g. `{"transcripts": "00:01 hello"}`) used to
    be walked character-by-character by `enumerate()`, producing a
    correct-but-misleading WARNING ("skipped 11 malformed entries: [0]
    '0', ..."). `_usable_task_list` now rejects the wrong-typed value
    itself with one clear line naming the real problem. See
    TestNonListTaskValueGuarded.
  - `_usable_timestamp`'s docstring is corrected: it checks SHAPE only and
    never parses timestamp content - a non-blank-but-unparseable string
    like "N/A" is deliberately admitted (both the writer and the assessor
    already agree on it, so it can no longer cause their divergence). See
    TestUnparseableButNonBlankStartIsAdmitted.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import video_intel as vi
from video_intel import merge_transcript_json, process_transcript

# A realistic three-task response shape, used as the base for both the
# healthy-response regression lock and the malformed-entry variants.
HEALTHY_RESPONSE = {
    "transcripts": [
        {"start": "00:05", "voice": 1, "text": "Hello and welcome."},
        {"start": "00:12", "voice": 2, "text": "Thanks for having me."},
        {"start": "01:30", "voice": 1, "text": "Let's get into it."},
    ],
    "screen_content": [
        {
            "start": "00:20",
            "end": "00:45",
            "type": "slide",
            "description": "Title slide",
        },
        {
            "start": "01:00",
            "end": "01:20",
            "type": "code",
            "description": "A code sample",
            "code": "print('hi')",
        },
    ],
    "speakers": [
        {"voice": 1, "name": "Alice", "role": "Host", "evidence": "Introduces the show"},
        {"voice": 2, "name": "Bob", "evidence": "Guest voice"},
    ],
}


def _expected_healthy_body() -> str:
    """Build the expected merged body independently of merge_transcript_json,
    so the regression-lock test does not just call the function twice.
    """
    # Entries are sorted by timestamp: 00:05, 00:12, 00:20, 01:00, 01:30.
    # Evidence lines use each speaker's name BEFORE the role suffix is
    # appended (the source loop appends evidence, then role, in that order),
    # so Alice's evidence line reads "Alice", not "Alice (Host)". Each list
    # element below mirrors exactly one `lines.append(...)` call the source
    # makes for each entry, joined with "\n" - reproducing the source's own
    # append sequence by hand, not by invoking it.
    lines = [
        '[00:05] Alice (Host): "Hello and welcome."\n',
        '[00:12] Bob: "Thanks for having me."\n',
        "\n  SCREEN [00:20-00:45] [slide]: Title slide",
        "",
        "\n  SCREEN [01:00-01:20] [code]: A code sample",
        "  ```\n  print('hi')\n  ```",
        "",
        '[01:30] Alice (Host): "Let\'s get into it."\n',
        "\n---\n## Speaker Identification Evidence\n",
        "- **Alice**: Introduces the show",
        "- **Bob**: Guest voice",
    ]
    return "\n".join(lines)


class TestHealthyResponseRegressionLock:
    def test_healthy_response_matches_independently_built_expectation(self):
        result = merge_transcript_json(copy.deepcopy(HEALTHY_RESPONSE), {})
        assert result == _expected_healthy_body()


class TestScreenContentMissingStart:
    """The observed real-world shape: a screen_content entry with no start."""

    def test_missing_start_is_skipped_other_entries_still_merge(self, caplog):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["screen_content"][0]["start"]

        with caplog.at_level(logging.WARNING):
            result = merge_transcript_json(payload, {})

        # The malformed screen entry (Title slide) is gone.
        assert "Title slide" not in result
        # Everything else survives: both dialogue lines and the other screen entry.
        assert "Hello and welcome" in result
        assert "Thanks for having me" in result
        assert "Let's get into it" in result
        assert "A code sample" in result

    def test_missing_start_does_not_raise(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["screen_content"][0]["start"]
        # Must not raise KeyError.
        merge_transcript_json(payload, {})


class TestTranscriptsMissingStart:
    def test_missing_start_is_skipped_other_entries_still_merge(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["transcripts"][1]["start"]

        result = merge_transcript_json(payload, {})

        assert "Thanks for having me" not in result
        assert "Hello and welcome" in result
        assert "Let's get into it" in result
        # screen_content entries are untouched.
        assert "Title slide" in result
        assert "A code sample" in result


class TestSpeakersMissingVoice:
    def test_missing_voice_skipped_from_name_map_only_no_transcript_entry_lost(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["speakers"][0]["voice"]

        result = merge_transcript_json(payload, {})

        # All three dialogue lines still render - none dropped.
        assert "Hello and welcome" in result
        assert "Thanks for having me" in result
        assert "Let's get into it" in result
        # Bob (voice 2) still maps normally.
        assert "Bob:" in result
        # Alice's voice=1 lines fall back to the default "Speaker 1" label
        # since her speaker record was unusable and never entered the map.
        assert "Speaker 1" in result
        assert "Alice" not in result


@pytest.mark.parametrize("bad_start", [None, "", {}])
class TestMalformedStartShapes:
    def test_none_empty_dict_all_skipped_from_transcripts(self, bad_start):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["transcripts"][0]["start"] = bad_start

        result = merge_transcript_json(payload, {})

        assert "Hello and welcome" not in result
        assert "Thanks for having me" in result

    def test_none_empty_dict_all_skipped_from_screen_content(self, bad_start):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["screen_content"][0]["start"] = bad_start

        result = merge_transcript_json(payload, {})

        assert "Title slide" not in result
        assert "A code sample" in result


class TestIntStartIsMalformed:
    """A bare number instead of a formatted timestamp string is malformed too."""

    def test_int_start_skipped_in_transcripts(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["transcripts"][0]["start"] = 5

        result = merge_transcript_json(payload, {})

        assert "Hello and welcome" not in result
        assert "Thanks for having me" in result

    def test_int_start_skipped_in_screen_content(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["screen_content"][0]["start"] = 20

        result = merge_transcript_json(payload, {})

        assert "Title slide" not in result
        assert "A code sample" in result


class TestListShapedStart:
    """A list `start` (e.g. Gemini emitting ["00", "05"]) is malformed too -
    isinstance(value, str) rejects it, same as dict and int shapes."""

    def test_list_start_skipped_in_transcripts(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["transcripts"][0]["start"] = ["00", "05"]

        result = merge_transcript_json(payload, {})

        assert "Hello and welcome" not in result
        assert "Thanks for having me" in result

    def test_list_start_skipped_in_screen_content(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["screen_content"][0]["start"] = ["00", "20"]

        result = merge_transcript_json(payload, {})

        assert "Title slide" not in result
        assert "A code sample" in result


class TestWhitespaceOnlyStartIsMalformed:
    """Issue #161 review, P3: a bare `!= ""` check lets a whitespace-only
    string through, and it would still sort to the top of the transcript -
    exactly the corruption the guard's docstring says it prevents."""

    @pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t \n "])
    def test_whitespace_only_start_skipped_in_transcripts(self, blank):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["transcripts"][0]["start"] = blank

        result = merge_transcript_json(payload, {})

        assert "Hello and welcome" not in result
        assert "Thanks for having me" in result

    def test_whitespace_only_start_skipped_in_screen_content(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["screen_content"][0]["start"] = "   "

        result = merge_transcript_json(payload, {})

        assert "Title slide" not in result
        assert "A code sample" in result


class TestUnhashableVoiceIsGuarded:
    """Issue #161 review, P1: a dict or list `voice` used to pass the old
    `is not None and value != ""` check and then raise
    `TypeError: unhashable type` at `voice_names[s["voice"]]` on the very
    next line - the crash this whole issue exists to prevent, reintroduced
    by a shape the first pass of the fix did not consider."""

    def test_dict_voice_skipped_no_crash(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["speakers"][0]["voice"] = {"nested": "shape"}

        # Must not raise TypeError: unhashable type.
        result = merge_transcript_json(payload, {})

        # All three dialogue lines still render; Alice's lines fall back to
        # the default label since her speaker record was unusable.
        assert "Hello and welcome" in result
        assert "Speaker 1" in result
        assert "Alice" not in result
        # Bob (voice 2, unaffected) still maps normally.
        assert "Bob:" in result

    def test_list_voice_skipped_no_crash(self):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        payload["speakers"][0]["voice"] = ["a", "list"]

        result = merge_transcript_json(payload, {})

        assert "Hello and welcome" in result
        assert "Speaker 1" in result
        assert "Alice" not in result


class TestUnhashableVoiceOnATranscriptsEntryIsGuarded:
    """Issue #171 P1, dual-review follow-up: `TestUnhashableVoiceIsGuarded`
    above only covers an unhashable `voice` on a SPEAKERS entry (guarded
    by `_usable_voice_id`, issue #161). A TRANSCRIPTS entry's `voice` is
    never validated by that predicate at all - it is gated only by
    `_usable_timestamp` (which looks at `start`, not `voice`) - so it sailed
    straight through into the render loop's `voice_names.get(entry["voice"],
    ...)` and raised `TypeError: unhashable type` there, one merge layer
    away from `TestUnhashableVoiceIsGuarded`'s coverage. This is the P1
    site the dual-review flagged as making the #161 "one malformed entry
    cannot crash a paid call" claim false at BOTH merge layers, not just
    the chunked one.
    """

    def test_dict_voice_on_a_transcripts_entry_renders_as_speaker_none_no_crash(self):
        payload = {
            "transcripts": [{"start": "00:01", "voice": {"nested": "shape"}, "text": "weird voice line"}],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        # Must not raise TypeError: unhashable type.
        result = merge_transcript_json(payload, {})
        assert "weird voice line" in result
        # Normalized to the pre-existing "no voice id" sentinel (None) at
        # copy time, so it renders through the existing default - no new
        # branch, no dropped entry.
        assert "Speaker None" in result

    def test_list_voice_on_a_transcripts_entry_renders_as_speaker_none_no_crash(self):
        payload = {
            "transcripts": [{"start": "00:01", "voice": ["a", "list"], "text": "another weird voice line"}],
            "screen_content": [],
            "speakers": [],
        }
        result = merge_transcript_json(payload, {})
        assert "another weird voice line" in result
        assert "Speaker None" in result

    def test_healthy_sibling_entry_with_a_normal_voice_is_unaffected(self):
        payload = {
            "transcripts": [
                {"start": "00:01", "voice": {"nested": "shape"}, "text": "weird voice line"},
                {"start": "00:02", "voice": 1, "text": "normal line"},
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        result = merge_transcript_json(payload, {})
        assert "Host:" in result
        assert '"normal line"' in result
        assert "Speaker None" in result


class TestNonDictListItemIsGuarded:
    """Issue #161 review, P1: a non-dict item inside any task list
    (e.g. `transcripts: [null, "foo"]`) used to raise AttributeError inside
    `entry.get(...)` before either predicate could even check the shape."""

    def test_none_and_bare_string_items_in_transcripts_skipped_no_crash(self):
        payload = {
            "transcripts": [None, "foo", {"start": "00:05", "voice": 1, "text": "hi"}],
            "screen_content": [],
            "speakers": [],
        }

        result = merge_transcript_json(payload, {})

        assert "hi" in result

    def test_none_and_bare_string_items_in_screen_content_skipped_no_crash(self):
        payload = {
            "transcripts": [],
            "screen_content": [None, "foo", {"start": "00:20", "type": "slide", "description": "x"}],
            "speakers": [],
        }

        result = merge_transcript_json(payload, {})

        assert "x" in result

    def test_none_and_bare_string_items_in_speakers_skipped_no_crash(self):
        payload = {
            "transcripts": [{"start": "00:05", "voice": 1, "text": "hi"}],
            "screen_content": [],
            "speakers": [None, "foo", {"voice": 1, "name": "Host"}],
        }

        result = merge_transcript_json(payload, {})

        assert "Host:" in result


class TestNonDictRawJsonRoot:
    """Issue #161 review, P1: `raw_json` itself can be non-dict after the
    list-unwrap (an empty list unwraps to {} already; a list of non-dicts,
    or a raw_json that was never an object at all, does not)."""

    def test_list_of_non_dicts_returns_empty_string_no_crash(self):
        assert merge_transcript_json([1, 2, 3], {}) == ""

    def test_bare_string_root_returns_empty_string_no_crash(self):
        assert merge_transcript_json("not a dict at all", {}) == ""

    def test_bare_int_root_returns_empty_string_no_crash(self):
        assert merge_transcript_json(5, {}) == ""


class TestFloatVoiceIdKeepsItsName:
    """Issue #161 second review round (P1, load-bearing): a float voice
    (e.g. 1.0) is a perfectly good, hashable dict key - hash(1.0) ==
    hash(1) - and worked before this whole fix landed. An earlier revision
    of _usable_voice_id excluded float from the accepted types, which would
    have shipped a REGRESSION: the speaker's NAME silently vanished (the
    transcript line still rendered via the Speaker <id> fallback, so this
    was not a crash - just a quieter, still-real loss of identity)."""

    def test_float_voice_keeps_its_mapped_name(self):
        payload = {
            "transcripts": [{"start": "00:01", "voice": 1.0, "text": "hi"}],
            "screen_content": [],
            "speakers": [{"voice": 1.0, "name": "Alice"}],
        }

        result = merge_transcript_json(payload, {})

        assert 'Alice: "hi"' in result
        assert "Speaker 1.0" not in result
        assert "Speaker" not in result  # never falls back to the default

    def test_float_voice_speaker_not_logged_as_skipped(self, caplog):
        payload = {
            "transcripts": [{"start": "00:01", "voice": 1.0, "text": "hi"}],
            "screen_content": [],
            "speakers": [{"voice": 1.0, "name": "Alice"}],
        }

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        assert not caplog.records


class TestNonListTaskValueGuarded:
    """Issue #161 second review round (P1): a task VALUE that is itself the
    wrong type (most concretely a string) is still iterable, so enumerate()
    used to walk it character by character. The OUTCOME was already right
    (nothing merges, no crash) but the diagnosis was actively misleading -
    an operator would see "skipped 11 malformed transcripts entries: [0]
    '0', [1] '0', [2] ':', ..." with no way to tell the real problem is the
    whole task's TYPE, not eleven malformed entries. One clear line naming
    the actual problem replaces that noise."""

    def test_string_transcripts_value_produces_one_clear_warning_not_per_character_noise(self, caplog):
        payload = {"transcripts": "00:01 hello", "screen_content": [], "speakers": []}

        with caplog.at_level(logging.WARNING):
            result = merge_transcript_json(payload, {})

        assert result == ""
        messages = [r.message for r in caplog.records]
        assert len(messages) == 1
        assert messages[0] == (
            "merge_transcript_json: 'transcripts' task value is str, not a list - the whole "
            "task is unusable (not entry-by-entry malformed); ignoring it."
        )
        # None of the per-character noise from the old behavior.
        assert not any("entries:" in m for m in messages)

    def test_dict_screen_content_value_produces_one_clear_warning(self, caplog):
        payload = {"transcripts": [], "screen_content": {"start": "00:05"}, "speakers": []}

        with caplog.at_level(logging.WARNING):
            result = merge_transcript_json(payload, {})

        assert result == ""
        messages = [r.message for r in caplog.records]
        assert len(messages) == 1
        assert "'screen_content' task value is dict, not a list" in messages[0]

    def test_int_speakers_value_produces_one_clear_warning(self, caplog):
        payload = {"transcripts": [], "screen_content": [], "speakers": 5}

        with caplog.at_level(logging.WARNING):
            result = merge_transcript_json(payload, {})

        assert result == ""
        messages = [r.message for r in caplog.records]
        assert len(messages) == 1
        assert "'speakers' task value is int, not a list" in messages[0]

    def test_non_list_task_value_does_not_suppress_other_healthy_lists(self):
        payload = {
            "transcripts": "garbage",
            "screen_content": [{"start": "00:20", "type": "slide", "description": "fine"}],
            "speakers": [],
        }

        result = merge_transcript_json(payload, {})

        assert "fine" in result

    def test_missing_task_key_still_defaults_to_empty_no_warning(self, caplog):
        """A key that is simply ABSENT (the normal shape when a task
        legitimately has nothing to report) must not be confused with a
        present-but-wrong-type value."""
        payload = {"transcripts": [{"start": "00:01", "voice": 1, "text": "hi"}]}

        with caplog.at_level(logging.WARNING):
            result = merge_transcript_json(payload, {})

        assert "hi" in result
        assert not [r for r in caplog.records if "not a list" in r.message]


class TestUnparseableButNonBlankStartIsAdmitted:
    """Issue #161 second review round (P3, docstring correction): locks the
    documented, deliberate behavior that _usable_timestamp checks SHAPE
    only, never parses timestamp content. A non-blank string that is not a
    real timestamp ("N/A") is admitted and rendered - it sorts wherever
    timestamp_to_seconds's own fallback-to-0 places it, which both the
    writer and the assessor already agree on, so it can no longer cause the
    writer/assessor divergence this predicate exists to prevent. This is
    NOT something to "fix" - see the docstring."""

    def test_non_timestamp_but_non_blank_string_is_admitted_and_rendered(self):
        payload = {
            "transcripts": [{"start": "N/A", "voice": 1, "text": "garbled stamp"}],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }

        result = merge_transcript_json(payload, {})

        assert "garbled stamp" in result
        assert "[N/A]" in result

    def test_writer_and_assessor_still_agree_on_the_admitted_na_entry(self):
        transcripts = [{"start": "N/A", "voice": 1, "text": "garbled stamp"}]
        payload = {"transcripts": transcripts, "screen_content": [], "speakers": []}

        rendered = merge_transcript_json(payload, {})
        assert "garbled stamp" in rendered

        metrics = vi.assess_transcript_artifact(transcripts, duration_seconds=None, window=None)
        assert metrics["dialogue_entries"] == 1


class TestAllEntriesMalformed:
    def test_all_malformed_produces_no_crash_and_empty_dialogue_body(self):
        payload = {
            "transcripts": [{"start": None, "voice": 1, "text": "ghost"}],
            "screen_content": [{"start": "", "type": "slide", "description": "ghost slide"}],
            "speakers": [{"name": "NoVoiceHere"}],
        }

        result = merge_transcript_json(payload, {})

        assert "ghost" not in result
        assert "ghost slide" not in result
        # No exception raised - that is the acceptance criterion.


class TestWarningsFireOncePerList:
    """Exact-message/regex assertions (issue #161 review): the earlier
    single-character substring checks (`"2" in msg`) were satisfied by
    accident - e.g. `indices [1, 0]` contains "2", "0", and "1" without the
    message actually saying what the test claimed."""

    def test_transcripts_warning_names_list_count_and_snippets(self, caplog):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["transcripts"][0]["start"]
        payload["transcripts"][1]["start"] = None

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        transcript_warnings = [r.message for r in caplog.records if "transcripts" in r.message]
        assert len(transcript_warnings) == 1
        assert transcript_warnings[0] == (
            "merge_transcript_json: skipped 2 malformed transcripts entries: "
            "[0] 'Hello and welcome.', [1] 'Thanks for having me.'"
        )

    def test_screen_content_warning_names_list_count_and_snippet(self, caplog):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["screen_content"][0]["start"]

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        screen_warnings = [r.message for r in caplog.records if "screen_content" in r.message]
        assert len(screen_warnings) == 1
        assert (
            screen_warnings[0] == "merge_transcript_json: skipped 1 malformed screen_content entry: [0] 'Title slide'"
        )

    def test_speakers_warning_names_list_count_and_snippet(self, caplog):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["speakers"][0]["voice"]

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        speaker_warnings = [r.message for r in caplog.records if "speakers" in r.message]
        assert len(speaker_warnings) == 1
        assert re.match(r"^merge_transcript_json: skipped 1 malformed speakers entry: \[0\] ", speaker_warnings[0])
        assert "Alice" in speaker_warnings[0]

    def test_no_warning_when_everything_is_healthy(self, caplog):
        with caplog.at_level(logging.WARNING):
            merge_transcript_json(copy.deepcopy(HEALTHY_RESPONSE), {})

        assert not caplog.records

    def test_truncation_branch_shows_first_five_and_plus_more(self, caplog):
        """Issue #161 review: the `(+more)` suffix, and the cap at 5 shown
        entries, on a list with more than 5 malformed entries."""
        payload = {
            "transcripts": [{"start": None, "voice": 1, "text": f"line {i}"} for i in range(8)],
            "screen_content": [],
            "speakers": [],
        }

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        transcript_warnings = [r.message for r in caplog.records if "transcripts" in r.message]
        assert len(transcript_warnings) == 1
        msg = transcript_warnings[0]
        assert msg.startswith("merge_transcript_json: skipped 8 malformed transcripts entries: ")
        assert msg.endswith(" (+more)")
        # Exactly the first 5 indices are shown, never entry 5, 6, or 7.
        for i in range(5):
            assert f"[{i}] 'line {i}'" in msg
        for i in range(5, 8):
            assert f"[{i}] 'line {i}'" not in msg


# ---------------------------------------------------------------------------
# Issue #161 review, P1 (the load-bearing item): the writer and the quality
# assessor must agree on what counts as a usable entry.
# ---------------------------------------------------------------------------


class TestWriterAndAssessorAgreeOnUsableEntries:
    """CLAUDE.md: "a verifier must use the WRITER's path, never re-derive
    its own" (PR #136). Before this fix, `assess_transcript_artifact`'s own
    `if not ts: continue` accepted a dict/list/int `start` as "present"
    (truthy), so a response `merge_transcript_json` rendered as ZERO
    dialogue lines was reported by the assessor as a healthy
    `dialogue_entries` count with `severe: []` - a silent, undetected empty
    transcript that would have been stamped `transcript_status: "complete"`.

    Both sides below are derived INDEPENDENTLY from the SAME raw payload -
    the writer's real merged markdown output, and the assessor's real
    `assess_transcript_artifact` call - and then compared. This is the one
    test shape that can catch a divergent re-derivation (see
    tests/test_transport_retry_and_partial_exit.py::TestCheckerAndWriterAgreeOnPaths
    for the reference shape this follows).
    """

    @staticmethod
    def _malformed_transcripts(n: int) -> list[dict]:
        shapes = [{"foo": "bar"}, ["a", "list"], 5]
        return [{"start": shapes[i % len(shapes)], "voice": 1, "text": f"line {i}"} for i in range(n)]

    @pytest.mark.parametrize("n", [8, 30, 60])
    def test_writer_renders_zero_dialogue_and_assessor_reports_zero_and_severe(self, n):
        malformed = self._malformed_transcripts(n)
        payload = {"transcripts": malformed, "screen_content": [], "speakers": []}

        # WRITER side: the real merged markdown, from the real function.
        rendered = merge_transcript_json(copy.deepcopy(payload), {})
        assert rendered == ""  # no speech lines, no screen entries, no evidence footer

        # ASSESSOR side: the real assess_transcript_artifact call, on the
        # SAME raw transcripts list process_transcript's real call site
        # passes it (a >5min window so the monolithic gate can fire).
        metrics = vi.assess_transcript_artifact(malformed, duration_seconds=3600, window=None)
        assert metrics["dialogue_entries"] == 0
        assert "monolithic_severe" in metrics["severe"]

    def test_mixed_healthy_and_malformed_entries_agree_on_the_surviving_count(self):
        """Not just the all-malformed extreme: 2 healthy + 3 malformed must
        have BOTH sides agree on exactly 2 usable entries."""
        payload = {
            "transcripts": [
                {"start": "00:05", "voice": 1, "text": "one"},
                {"start": {"bad": "shape"}, "voice": 1, "text": "two"},
                {"start": "05:00", "voice": 1, "text": "three"},
                {"start": None, "voice": 1, "text": "four"},
                {"start": ["x"], "voice": 1, "text": "five"},
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }

        rendered = merge_transcript_json(copy.deepcopy(payload), {})
        rendered_dialogue_count = rendered.count("Host:")
        assert rendered_dialogue_count == 2

        metrics = vi.assess_transcript_artifact(payload["transcripts"], duration_seconds=None, window=None)
        assert metrics["dialogue_entries"] == 2


# ---------------------------------------------------------------------------
# Caller-level: process_transcript (single-shot) and _run_chunked_transcript_url,
# executed for real (stubbed only at the Gemini call boundary), proving the
# writer/assessor agreement fix's real consequence end to end.
# ---------------------------------------------------------------------------


def _video(video_id: str = "abc12345678") -> dict:
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": "A Talk",
        "published": "2026-08-30",
    }


def _fake_gemini_response(prompt_tokens: int = 5000, candidates_tokens: int = 100, finish_reason: str = "STOP"):
    """A real (non-Mock) response shape - log_usage_metadata / hit_output_cap
    read concrete fields off this, and a MagicMock's auto-generated attribute
    chain is not JSON-serializable if it ever lands in a persisted field."""
    return SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=candidates_tokens,
            total_token_count=prompt_tokens + candidates_tokens,
        ),
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


def _stub_single_shot(monkeypatch, response_json: str):
    def fake_call_gemini(_client, _types, _uri, _prompt, _model, **kw):
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(_fake_gemini_response())
        return response_json

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    return MagicMock(), MagicMock()


class TestCallerLevelAllMalformedNeverStampsComplete:
    def test_single_shot_all_malformed_starts_is_not_stamped_complete(self, tmp_path, monkeypatch):
        payload = json.dumps(
            {
                "transcripts": [
                    {"start": {"bad": "shape"}, "voice": 1, "text": "line one"},
                    {"start": ["also", "bad"], "voice": 1, "text": "line two"},
                    {"start": 5, "voice": 1, "text": "line three"},
                ],
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )
        client, types = _stub_single_shot(monkeypatch, payload)
        video = _video("mno12345678")
        channel_dir = tmp_path / "demo"
        prefix, status = process_transcript(
            client, types, video, "prompt", "gemini-test", channel_dir, "2026-08-30-b-talk", duration_seconds=3600
        )

        meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        assert meta["transcript_status"] != "complete"
        assert "monolithic_severe" in meta["transcript_quality_flags"]
        assert meta["transcript_dialogue_entries"] == 0
        assert status == "partial (quality guard)"

    def test_chunked_all_malformed_starts_is_not_stamped_complete(self, tmp_path, monkeypatch):
        chunk = json.dumps(
            {
                "transcripts": [
                    {"start": {"bad": "shape"}, "voice": 1, "text": "line one"},
                    {"start": None, "voice": 1, "text": "line two"},
                ],
                "screen_content": [],
                "speakers": [{"voice": 1, "name": "Host"}],
            }
        )

        def fake_call_gemini(_client, _types, _uri, _prompt, _model, **kw):
            on_response = kw.get("on_response")
            if on_response is not None:
                on_response(_fake_gemini_response())
            return chunk

        monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda types, model: None)

        channel_dir = tmp_path / "demo"
        video = _video("pqr12345678")
        status = vi._run_chunked_transcript_url(
            client=MagicMock(),
            types=MagicMock(),
            video=video,
            prompt_text="PROMPT",
            model="stub-model",
            channel_dir=channel_dir,
            prefix="2026-08-30-c-talk",
            chunks=[(0, 3600)],
            duration_seconds=3600,
            chunk_minutes=60,
            force=False,
        )

        meta = json.loads((channel_dir / "2026-08-30-c-talk.meta.json").read_text(encoding="utf-8"))
        assert meta["transcript_status"] != "complete"
        assert status != "ok"


class TestChunkedPathDoesNotCrashOnNonStringStart:
    """Issue #161 review, P1: merge_chunked_transcripts calls
    _classify_and_offset_timestamp unconditionally whenever `"start" in
    new_t`, with no type check - so a non-string start used to crash
    INSIDE the classifier (`ts.strip()`), before the final merge guard in
    merge_transcript_json ever got a chance to skip the entry. This made
    the PR's "one malformed entry cannot kill a paid call" claim false on
    the chunked path, which most long videos take.
    """

    def test_classifier_passes_a_non_string_start_through_unchanged(self):
        # Direct proof at the classifier itself: must not raise, and must
        # return the value UNCHANGED (issue #158: classification decisions
        # stay where they are - this function does not drop or coerce).
        assert vi._classify_and_offset_timestamp({"bad": "shape"}, 0, 3000) == {"bad": "shape"}
        assert vi._classify_and_offset_timestamp(["a", "b"], 0, 3000) == ["a", "b"]
        assert vi._classify_and_offset_timestamp(None, 0, 3000) is None
        assert vi._classify_and_offset_timestamp(5, 0, 3000) == 5

    def test_merge_chunked_transcripts_does_not_crash_on_dict_or_list_start(self):
        chunk = {
            "transcripts": [{"start": {"bad": "shape"}, "voice": 1, "text": "x"}],
            "screen_content": [{"start": ["a", "b"], "type": "slide", "description": "y"}],
            "speakers": [{"voice": 1, "name": "Host"}],
        }

        # Must not raise AttributeError from inside the classifier.
        merged = vi.merge_chunked_transcripts([(0, chunk)], chunk_duration_seconds=3000)

        # The final merge-time guard is what actually skips these entries.
        fused = merge_transcript_json(merged, {})
        assert fused == ""

    def test_relativize_chunk_entries_does_not_crash_on_dict_start(self):
        transcripts = [{"start": {"bad": "shape"}, "voice": 1, "text": "x"}]
        # Must not raise; the malformed entry is simply excluded.
        assert vi._relativize_chunk_entries(transcripts, start_secs=0, allotted_span=3000) == []
