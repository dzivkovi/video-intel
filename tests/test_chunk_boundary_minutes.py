"""Issue #195: the chunk-boundary regex must see cues past 99 minutes.

`chunk_transcript` decides where an indexed chunk starts. Its boundary regex
allowed one or two digits before the first colon, while the captions renderer
emits `MM:SS` with unbounded minutes (`[100:30]` for 1h40m30s). Every cue past
99:59 therefore failed the boundary match and was folded into the preceding
chunk: the content still reached the index, but it carried the timestamp of the
last cue before 100:00, so `search --vector`'s `&t=<seconds>` deep-link pointed
hours away from the hit.

Measured on the live corpus at the time of the fix: 25 transcripts across 10
channels, 27,798 cue lines, all `transcript_source: youtube_captions` - the
longest videos in the corpus, which is where an accurate deep-link matters most.

The tests below pin three separate things, because each can regress alone:

* the parser sees a wide minute field (literal fixtures, renderer-independent);
* the *renderer* still emits the shape the parser was widened for (a
  renderer-driven fixture, so this file cannot go vacuous if the format moves);
* no chunk silently swallows an unbounded span (the corpus-shaped guard that
  catches a renderer/parser divergence of any shape, not just this one).
"""

import re
from itertools import pairwise
from typing import ClassVar

import pytest

from video_intel import (
    ENTRY_TIMESTAMP_PATTERN,
    CaptionsResult,
    _build_captions_transcript_body,
    chunk_transcript,
)

HEADER = (
    "# Transcript: Long Video\n\n**Source:** https://www.youtube.com/watch?v=LONG\n**Published:** 2026-08-26\n\n---\n\n"
)


def _write(tmp_path, body, name="long.transcript.md"):
    path = tmp_path / name
    path.write_text(HEADER + body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The defect: a cue past 99:59 must start its own chunk, with its own timestamp
# ---------------------------------------------------------------------------


class TestCuesPastNinetyNineMinutes:
    # Cues either side of the 100-minute line. Written as literals rather than
    # generated, so the expectation does not inherit the code's own arithmetic.
    BODY = (
        '[99:50] "before the boundary"\n'
        "\n"
        '[99:55] "still before"\n'
        "\n"
        '[100:00] "the first cue past ninety-nine minutes"\n'
        "\n"
        '[100:30] "and the one after it"\n'
        "\n"
        '[125:30] "much later"\n'
    )

    def test_every_cue_either_side_of_the_boundary_starts_its_own_chunk(self, tmp_path):
        path = _write(tmp_path, self.BODY)
        chunks = chunk_transcript(path, chunk_size=1)
        assert len(chunks) == 5, "each of the five cues is one entry, so chunk_size=1 gives five chunks"

    def test_each_chunk_carries_its_own_cues_seconds_not_an_earlier_ones(self, tmp_path):
        path = _write(tmp_path, self.BODY)
        chunks = chunk_transcript(path, chunk_size=1)
        # 99:50 -> 5990, 99:55 -> 5995, 100:00 -> 6000, 100:30 -> 6030, 125:30 -> 7530
        assert [c["timestamp_seconds"] for c in chunks] == [5990, 5995, 6000, 6030, 7530]

    def test_each_chunk_holds_only_its_own_cue_text(self, tmp_path):
        path = _write(tmp_path, self.BODY)
        chunks = chunk_transcript(path, chunk_size=1)
        assert "the first cue past ninety-nine minutes" in chunks[2]["text"]
        assert "and the one after it" not in chunks[2]["text"], (
            "a following cue folded into this chunk means the boundary was missed"
        )

    def test_the_tail_is_not_one_giant_chunk(self, tmp_path):
        """The user-visible half of the defect, stated independently of counts."""
        path = _write(tmp_path, self.BODY)
        chunks = chunk_transcript(path, chunk_size=2)
        last = chunks[-1]
        assert "much later" in last["text"]
        assert last["timestamp_seconds"] == 7530


# ---------------------------------------------------------------------------
# The adjacent SCREEN-block regex had the identical shape
# ---------------------------------------------------------------------------


class TestScreenBlocksPastNinetyNineMinutes:
    def test_screen_block_past_ninety_nine_minutes_starts_its_own_chunk(self, tmp_path):
        body = (
            '[99:00] Alice: "speech before"\n'
            "\n"
            "  SCREEN [100:10-100:40] [slide]: the slide past the boundary\n"
            "\n"
            '[101:00] Alice: "speech after"\n'
        )
        path = _write(tmp_path, body)
        chunks = chunk_transcript(path, chunk_size=1)
        assert len(chunks) == 3
        assert [c["timestamp_seconds"] for c in chunks] == [5940, 6010, 6060]

    def test_screen_block_under_the_boundary_is_unchanged(self, tmp_path):
        body = '[01:00] Alice: "hi"\n\n  SCREEN [01:20-01:30] [slide]: a slide\n'
        path = _write(tmp_path, body)
        chunks = chunk_transcript(path, chunk_size=1)
        assert [c["timestamp_seconds"] for c in chunks] == [60, 80]


# ---------------------------------------------------------------------------
# The pre-existing shapes must stay byte-identical
# ---------------------------------------------------------------------------


class TestExistingShapesUnchanged:
    @pytest.mark.parametrize(
        ("line", "expected_seconds"),
        [
            ('[00:00] Alice: "zero"', 0),
            ('[07:05] Alice: "mm ss"', 425),
            ('[59:59] Alice: "last two-digit minute"', 3599),
            ('[1:15:30] Alice: "h mm ss"', 4530),
            ('[01:15:30] Alice: "hh mm ss"', 4530),
            ('[10:15:30] Alice: "ten hours"', 36930),
        ],
    )
    def test_known_shapes_still_match_and_parse(self, tmp_path, line, expected_seconds):
        path = _write(tmp_path, line + "\n")
        chunks = chunk_transcript(path, chunk_size=1)
        assert len(chunks) == 1
        assert chunks[0]["timestamp_seconds"] == expected_seconds

    def test_a_non_timestamp_bracket_line_is_not_a_boundary(self, tmp_path):
        """Widening the minute field must not turn prose into a chunk start."""
        body = '[00:00] Alice: "opening"\n\n[not a timestamp] still the same entry\n\n[12] neither is this\n'
        path = _write(tmp_path, body)
        chunks = chunk_transcript(path, chunk_size=10)
        assert len(chunks) == 1
        assert "neither is this" in chunks[0]["text"]


# ---------------------------------------------------------------------------
# Renderer-driven: the parser is widened for what the writer actually emits
# ---------------------------------------------------------------------------


class TestParserMatchesTheCaptionsRenderer:
    """Drive the fixture through the real renderer, not a hand-typed guess.

    Same rule as the checker-must-use-the-writer's-path family: a hand-written
    fixture can agree with the parser while disagreeing with the writer, which
    is precisely the divergence issue #195 reports.
    """

    SNIPPETS: ClassVar[list[tuple[float, str]]] = [
        (5990.0, "before the boundary"),
        (6000.0, "the first cue past ninety-nine minutes"),
        (6030.0, "and the one after it"),
        (7530.0, "much later"),
    ]

    def _body(self):
        return _build_captions_transcript_body(CaptionsResult(self.SNIPPETS, True, "en"))

    def test_the_renderer_still_emits_unbounded_minutes(self):
        """Companion guard: this file must not pass vacuously.

        If this fails because `_captions_timestamp` moved to `HH:MM:SS`, that is
        the renderer change issue #195 raised as an option - the literal
        fixtures above still cover the parser, but this class's premise is gone
        and it should be retired deliberately rather than left green and inert.
        """
        body = self._body()
        assert "[100:00]" in body, f"expected an unbounded-minutes cue in the rendered body, got: {body!r}"

    def test_rendered_captions_past_the_boundary_chunk_one_cue_at_a_time(self, tmp_path):
        path = _write(tmp_path, self._body())
        chunks = chunk_transcript(path, chunk_size=1)
        assert [c["timestamp_seconds"] for c in chunks] == [5990, 6000, 6030, 7530]

    def test_rendered_cue_text_lands_in_the_chunk_whose_timestamp_it_owns(self, tmp_path):
        path = _write(tmp_path, self._body())
        chunks = chunk_transcript(path, chunk_size=1)
        by_seconds = {c["timestamp_seconds"]: c["text"] for c in chunks}
        assert "much later" in by_seconds[7530]
        assert "much later" not in by_seconds[5990]


# ---------------------------------------------------------------------------
# Corpus-shaped guard: no chunk may swallow an unbounded span
# ---------------------------------------------------------------------------


class TestNoChunkSwallowsAnUnboundedSpan:
    """Catches a renderer/parser divergence of ANY shape, not just this one.

    A three-hour caption track at one cue every five seconds. If any boundary
    shape stops matching, the chunks after it collapse into one enormous entry
    and its span blows past the bound - which is the symptom, independent of
    whichever regex caused it.
    """

    CUE_SECONDS = 5
    DURATION_SECONDS = 3 * 60 * 60

    def _rendered_path(self, tmp_path):
        snippets = [(float(t), f"cue at {t} seconds") for t in range(0, self.DURATION_SECONDS, self.CUE_SECONDS)]
        body = _build_captions_transcript_body(CaptionsResult(snippets, True, "en"))
        return _write(tmp_path, body, name="three-hours.transcript.md"), len(snippets)

    def test_every_chunk_spans_at_most_its_own_entries(self, tmp_path):
        chunk_size = 5
        path, cue_count = self._rendered_path(tmp_path)
        chunks = chunk_transcript(path, chunk_size=chunk_size)
        assert len(chunks) == -(-cue_count // chunk_size), "one chunk per group of cues, none folded away"

        starts = [c["timestamp_seconds"] for c in chunks]
        assert starts == sorted(starts), "chunk starts must advance monotonically"
        # A chunk covers `chunk_size` cues, so its start may not sit more than
        # chunk_size * CUE_SECONDS behind the next chunk's start.
        max_span = self.CUE_SECONDS * chunk_size
        spans = [b - a for a, b in pairwise(starts)]
        assert max(spans) <= max_span, f"a chunk spans {max(spans)}s, above the {max_span}s bound"

    def test_the_last_chunk_reaches_the_end_of_the_video(self, tmp_path):
        path, _ = self._rendered_path(tmp_path)
        chunks = chunk_transcript(path, chunk_size=5)
        last_cue = self.DURATION_SECONDS - self.CUE_SECONDS
        assert chunks[-1]["timestamp_seconds"] >= last_cue - self.CUE_SECONDS * 5


# ---------------------------------------------------------------------------
# One shared pattern, so the two boundaries cannot drift apart again
# ---------------------------------------------------------------------------


class TestOneSharedBoundaryPattern:
    def test_the_walk_finds_both_boundary_matches(self):
        """Companion guard: the walk below must actually see the hard instances.

        Without this, a rename or a refactor that hides one boundary from the
        walk leaves the next test comparing an empty list against itself.
        """
        lines = _boundary_match_lines()
        assert len(lines) == 2, f"expected the speech and screen boundary matches, found {len(lines)}: {lines}"
        assert any("SCREEN" in line for line in lines), "the screen boundary is not among the matched lines"

    def test_every_boundary_match_is_built_from_the_shared_pattern(self):
        offenders = [line for line in _boundary_match_lines() if "ENTRY_TIMESTAMP_PATTERN" not in line]
        assert offenders == [], f"a boundary regex is hard-coded instead of shared: {offenders}"

    def test_chunk_transcript_hard_codes_no_narrow_minute_field(self):
        source = _chunk_transcript_source()
        narrow = re.findall(r"\\d\{1,2\}:", source)
        assert narrow == [], f"chunk_transcript still hard-codes a narrow minute field: {narrow}"

    def test_the_shared_pattern_accepts_wide_and_narrow_minutes(self):
        compiled = re.compile(rf"^\[{ENTRY_TIMESTAMP_PATTERN}\]")
        assert compiled.match("[00:00]")
        assert compiled.match("[100:30]")
        assert compiled.match("[1234:56]")
        assert compiled.match("[01:15:30]")
        assert not compiled.match("[abc]")


def _chunk_transcript_source() -> str:
    """The source text of `chunk_transcript` alone, so the walk cannot go wide."""
    import inspect

    import video_intel

    return inspect.getsource(video_intel.chunk_transcript)


def _boundary_match_lines() -> list[str]:
    """The lines inside `chunk_transcript` that build an entry-boundary regex.

    Keyed on `re.match(` rather than on the constant's name, so a boundary that
    stopped using the shared pattern is still SEEN by the walk and reported,
    instead of quietly dropping out of the sample.
    """
    return [line.strip() for line in _chunk_transcript_source().splitlines() if "re.match(" in line]
