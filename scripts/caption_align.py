"""Map a transcript's dialogue words onto a YouTube caption track's cue timings.

A caption track never forms a wall of text: each cue carries a start time from
speech timing. A Gemini transcript entry sometimes does - one entry holding
most of a talk under one stamp. This module lines the entry's words up with
the caption words and reports, per word, which cue holds it. It never edits a
transcript and never computes a time inside a cue: every time it reports is
the ``start`` of a real cue.

Standard library only, and it never imports ``video_intel``, so a benchmark
that measures timestamp accuracy can reuse ``entry_offsets`` without the
curate stack (the import-weight firebreak ``timestamp_utils`` keeps too).

Public surface: ``Cue``, ``normalize_words``, ``align_words_to_cues``,
``align_entries``, ``entry_offsets``, ``plan_entry_split`` and the four
``REPAIR_*`` thresholds.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

#: A dialogue entry whose aligned words span more than this many seconds of
#: caption time is a split candidate.
REPAIR_MIN_SPAN_SECONDS = 60
#: Pieces are cut at the sentence boundary that brings them closest to this
#: many seconds of caption time.
REPAIR_TARGET_PIECE_SECONDS = 25
#: A new piece needs an aligned word among its first this-many words; the cue
#: holding that word gives the piece its stamp.
REPAIR_ANCHOR_WORDS = 3
#: Below this share of an entry's words aligned, the entry is left whole.
REPAIR_MIN_ENTRY_COVERAGE = 0.6

_APOSTROPHES = re.compile(r"['\u2019\u2018`]")
_WORD = re.compile(r"[^\W_]+")
#: A sentence ends after `.`, `?` or `!` followed by whitespace.
_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+")


@dataclass(frozen=True)
class Cue:
    """One caption cue: a start time and duration in seconds, and its text."""

    start: float
    duration: float
    text: str


@dataclass(frozen=True)
class EntryOffset:
    """How far one dialogue entry's stamp sits from the caption time of its opening words.

    ``caption_seconds`` is the start of the cue holding the first aligned word
    among the entry's first ``REPAIR_ANCHOR_WORDS`` words, or None when none
    of them aligned; ``offset_seconds`` is ``start_seconds - caption_seconds``.
    """

    start_seconds: float
    caption_seconds: float | None
    offset_seconds: float | None
    coverage: float


@dataclass(frozen=True)
class Piece:
    """One piece of a split entry.

    The first piece of a plan keeps the entry's original stamp
    (``cue_index`` None). Every later piece is stamped with the start of cue
    ``cue_index``, truncated to a whole second.
    """

    text: str
    stamp_seconds: int
    cue_index: int | None


@dataclass(frozen=True)
class SplitPlan:
    """The split plan for one entry. ``repairable`` means: split into two or more pieces."""

    coverage: float
    span_seconds: float
    candidate: bool
    pieces: list[Piece] = field(default_factory=list)
    reason: str | None = None

    @property
    def repairable(self) -> bool:
        return len(self.pieces) > 1


def normalize_words(text: str) -> list[str]:
    """Lowercase word tokens with punctuation stripped (apostrophes dropped, not split on)."""
    return _WORD.findall(_APOSTROPHES.sub("", text.lower()))


def _caption_words(cues: list[Cue]) -> tuple[list[str], list[int]]:
    words: list[str] = []
    owner: list[int] = []
    for index, cue in enumerate(cues):
        cue_words = normalize_words(cue.text)
        words.extend(cue_words)
        owner.extend([index] * len(cue_words))
    return words, owner


def align_words_to_cues(words: list[str], cues: list[Cue]) -> list[int | None]:
    """For each word, the index of the cue holding the caption word it matched, or None.

    Order-preserving: difflib matching blocks between the caption word stream
    and ``words``, so the returned cue indices never decrease. ``autojunk`` is
    off on purpose: with it, every word that fills more than 1% of a long
    input ("the", "is", "and") can no longer seed a match, and in speech those
    words sit between nearly all the others, so a synthetic talk aligned only
    4% of its words. Off, a 14-minute talk aligns in a tenth of a second; two
    hours of speech (about 20,000 words) took about 17 seconds when measured.
    """
    caption, owner = _caption_words(cues)
    result: list[int | None] = [None] * len(words)
    matcher = difflib.SequenceMatcher(None, caption, words, autojunk=False)
    for a, b, size in matcher.get_matching_blocks():
        for k in range(size):
            result[b + k] = owner[a + k]
    return result


def align_entries(texts: list[str], cues: list[Cue]) -> list[list[int | None]]:
    """One alignment of every entry's words, in order, sliced back per entry.

    Aligning all entries together keeps a mid-length entry from matching an
    unrelated stretch of the track.
    """
    per_entry = [normalize_words(t) for t in texts]
    flat = align_words_to_cues([w for ws in per_entry for w in ws], cues)
    slices: list[list[int | None]] = []
    pos = 0
    for ws in per_entry:
        slices.append(flat[pos : pos + len(ws)])
        pos += len(ws)
    return slices


def _anchor(word_cues: list[int | None], first: int, anchor_words: int) -> int | None:
    for cue_index in word_cues[first : first + anchor_words]:
        if cue_index is not None:
            return cue_index
    return None


def _coverage(word_cues: list[int | None]) -> float:
    if not word_cues:
        return 0.0
    return sum(1 for c in word_cues if c is not None) / len(word_cues)


def entry_offsets(
    entries: list[tuple[float, str]], cues: list[Cue], *, anchor_words: int = REPAIR_ANCHOR_WORDS
) -> list[EntryOffset]:
    """Per entry ``(start_seconds, text)``: the caption time of its opening words and the signed offset.

    Reports only; nothing is edited.
    """
    slices = align_entries([text for _, text in entries], cues)
    out: list[EntryOffset] = []
    for (start, _), word_cues in zip(entries, slices, strict=True):
        anchor = _anchor(word_cues, 0, anchor_words)
        caption = cues[anchor].start if anchor is not None else None
        out.append(
            EntryOffset(
                start_seconds=start,
                caption_seconds=caption,
                offset_seconds=start - caption if caption is not None else None,
                coverage=_coverage(word_cues),
            )
        )
    return out


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of each sentence in ``text``; the whitespace between them belongs to neither."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _SENTENCE_BREAK.finditer(text):
        spans.append((pos, match.start()))
        pos = match.end()
    spans.append((pos, len(text)))
    return spans


def plan_entry_split(
    text: str,
    cues: list[Cue],
    *,
    original_seconds: int,
    word_cues: list[int | None] | None = None,
    min_span: float = REPAIR_MIN_SPAN_SECONDS,
    target: float = REPAIR_TARGET_PIECE_SECONDS,
    anchor_words: int = REPAIR_ANCHOR_WORDS,
    min_coverage: float = REPAIR_MIN_ENTRY_COVERAGE,
) -> SplitPlan:
    """The split plan for one dialogue entry.

    ``word_cues`` is this entry's slice of ``align_entries`` (aligned alone
    when omitted). The entry is a candidate when its aligned words span more
    than ``min_span`` seconds of caption time. A candidate below
    ``min_coverage`` is left whole. Otherwise it is cut only at its own
    sentence boundaries: a boundary is usable when one of the next sentence's
    first ``anchor_words`` words aligned and that cue's whole second is
    strictly later than the current piece's stamp. From each piece, the cut is
    the first usable boundary whose caption time reaches ``target`` seconds,
    or the usable one just before it when that is closer. The first piece
    keeps ``original_seconds``, always: it is the one stamp the transcript
    itself gave.
    """
    words = normalize_words(text)
    if word_cues is None:
        word_cues = align_words_to_cues(words, cues)
    if len(word_cues) != len(words):
        raise ValueError(f"word_cues has {len(word_cues)} entries for {len(words)} words")
    whole = [Piece(text=text, stamp_seconds=original_seconds, cue_index=None)]
    coverage = _coverage(word_cues)
    matched = [c for c in word_cues if c is not None]
    span = cues[matched[-1]].start - cues[matched[0]].start if matched else 0.0
    if span <= min_span:
        return SplitPlan(coverage, span, candidate=False, pieces=whole)
    if coverage < min_coverage:
        return SplitPlan(
            coverage, span, candidate=True, pieces=whole, reason=f"coverage {coverage:.3f} below {min_coverage}"
        )

    spans = _sentence_spans(text)
    # Usable boundaries in sentence order: (sentence index, cue index, cue start).
    boundaries: list[tuple[int, int, float]] = []
    word_pos = len(normalize_words(text[spans[0][0] : spans[0][1]]))
    for k in range(1, len(spans)):
        anchor = _anchor(word_cues, word_pos, anchor_words)
        if anchor is not None:
            boundaries.append((k, anchor, cues[anchor].start))
        word_pos += len(normalize_words(text[spans[k][0] : spans[k][1]]))

    cuts: list[tuple[int, int, int]] = []  # (sentence index, cue index, stamp)
    cur_k, cur_time, cur_stamp = 0, float(original_seconds), original_seconds
    while True:
        usable = [b for b in boundaries if b[0] > cur_k and int(b[2]) > cur_stamp]
        crossing = next((i for i, b in enumerate(usable) if b[2] - cur_time >= target), None)
        if crossing is None:
            break
        chosen = usable[crossing]
        if crossing > 0:
            before = usable[crossing - 1]
            if abs((before[2] - cur_time) - target) < abs((chosen[2] - cur_time) - target):
                chosen = before
        k, cue_index, start = chosen
        cuts.append((k, cue_index, int(start)))
        cur_k, cur_time, cur_stamp = k, start, int(start)

    if not cuts:
        return SplitPlan(coverage, span, candidate=True, pieces=whole, reason="no usable sentence boundary")
    pieces: list[Piece] = []
    starts = [(0, None, original_seconds)] + cuts
    for i, (k, cue_index, stamp) in enumerate(starts):
        end_k = starts[i + 1][0] if i + 1 < len(starts) else len(spans)
        piece_text = text[spans[k][0] : spans[end_k - 1][1]]
        pieces.append(Piece(text=piece_text, stamp_seconds=stamp, cue_index=cue_index))
    return SplitPlan(coverage, span, candidate=True, pieces=pieces)
