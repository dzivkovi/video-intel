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
  - "Usable" for a `start` timestamp means: key present AND value is a
    non-empty string. None, "", a dict, a list, or an int are all malformed.
  - A speaker missing a usable `voice` id is skipped from the name map only
    - it must never drop a transcript entry; those fall back to the
      existing voice_names.get(...) default path. `voice` is a Gemini
      integer ID (see prompts/transcript.md), not a string, so a legitimate
      voice value is an int - the "non-empty string" rule does not apply to
      it. A voice id is usable when present and not None/"".
  - One WARNING per task list (not per entry), naming the list, the count
    skipped, and the first few offending indices.
  - No signature change, no new meta.json field - the WARNING is the
    operator surface.
  - A response where every entry is malformed must not crash; it produces a
    transcript with no dialogue (the existing #157 guards flag that on
    their own).
"""

from __future__ import annotations

import copy
import logging

import pytest

from video_intel import merge_transcript_json

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
    def test_transcripts_warning_names_list_count_and_indices(self, caplog):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["transcripts"][0]["start"]
        payload["transcripts"][1]["start"] = None

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        transcript_warnings = [r.message for r in caplog.records if "transcripts" in r.message]
        assert len(transcript_warnings) == 1
        assert "2" in transcript_warnings[0]
        assert "0" in transcript_warnings[0]
        assert "1" in transcript_warnings[0]

    def test_screen_content_warning_names_list_and_count(self, caplog):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["screen_content"][0]["start"]

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        screen_warnings = [r.message for r in caplog.records if "screen_content" in r.message]
        assert len(screen_warnings) == 1
        assert "1" in screen_warnings[0]

    def test_speakers_warning_names_list_and_count(self, caplog):
        payload = copy.deepcopy(HEALTHY_RESPONSE)
        del payload["speakers"][0]["voice"]

        with caplog.at_level(logging.WARNING):
            merge_transcript_json(payload, {})

        speaker_warnings = [r.message for r in caplog.records if "speakers" in r.message]
        assert len(speaker_warnings) == 1
        assert "1" in speaker_warnings[0]

    def test_no_warning_when_everything_is_healthy(self, caplog):
        with caplog.at_level(logging.WARNING):
            merge_transcript_json(copy.deepcopy(HEALTHY_RESPONSE), {})

        assert not caplog.records
