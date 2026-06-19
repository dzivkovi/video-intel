#!/usr/bin/env python3
"""Shared YouTube-caption helpers.

Pure, dependency-light primitives for fetching and formatting a video's
English caption track via ``youtube-transcript-api``. Extracted from
``translate_video.py`` (issue #60) so both ``translate_video.py`` (BCS
translation, SRT-first path) and ``video_intel.py`` (captions failover /
lite-index for the curate pipeline) can share one implementation instead
of duplicating it.

This mirrors the ``timestamp_utils.py`` shared-module precedent: a small
module of pure functions imported by both consumers, so neither script
depends on the other (the operational-separation rule in CLAUDE.md stays
intact - the dependency flows both -> shared module, never script-to-script).

Only ``fetch_english_captions`` touches the network; everything else is a
pure function over ``(start_seconds, text)`` snippet tuples and is trivially
unit-testable.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptionsResult:
    """English caption track fetched from YouTube.

    snippets: list of (start_seconds, text) tuples in source order.
    is_generated: True for auto-generated ASR captions, False for
        manually authored tracks. Affects the prompt sent to Gemini.
    language: BCP-47 language tag reported by YouTube (typically "en").
    durations: parallel list of per-snippet durations in seconds.
        Kept as a separate list (rather than extending the snippet
        tuple) so existing consumers that unpack `(start, text)` keep
        working. Empty by default — test fixtures that construct
        CaptionsResult directly without duration data trigger no SRT
        sibling generation, which is the desired behavior.
    """

    snippets: list[tuple[float, str]]
    is_generated: bool
    language: str
    durations: tuple[float, ...] = ()


def fetch_english_captions(video_id: str) -> CaptionsResult | None:
    """Fetch the English caption track from YouTube, preferring manual over auto-generated.

    Returns a CaptionsResult on success, or None when no captions are
    available for any reason the caller should treat as "fall back to
    the video-understanding path." Any unexpected exception propagates
    so we do not silently swallow real problems.

    The library's default behavior for `find_transcript(['en'])` is to
    return a manually authored track when one exists, falling back to
    the auto-generated track only if no manual track is present. We
    rely on that default instead of re-implementing preference logic.
    """
    try:
        from youtube_transcript_api import (
            CouldNotRetrieveTranscript,
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
            YouTubeTranscriptApi,
        )
    except ImportError:
        log.debug("youtube-transcript-api not installed, skipping captions fetch")
        return None

    try:
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        transcript = transcript_list.find_transcript(["en"])
        fetched = transcript.fetch()
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, CouldNotRetrieveTranscript) as e:
        log.info("No English captions available (%s) - falling back to video path", type(e).__name__)
        return None

    snippets = [(float(s.start), s.text) for s in fetched]
    durations = tuple(float(getattr(s, "duration", 0.0) or 0.0) for s in fetched)
    if not snippets:
        log.info("Captions list returned empty - falling back to video path")
        return None

    return CaptionsResult(
        snippets=snippets,
        is_generated=bool(transcript.is_generated),
        language=str(transcript.language_code or "en"),
        durations=durations,
    )


def format_captions_for_translation(snippets: list[tuple[float, str]]) -> str:
    """Render caption snippets as a [HH:MM:SS]-prefixed text block.

    The output is the exact format we want Gemini to echo back in the
    translation, so preservation is trivial: "translate each line, keep
    the timestamp prefix unchanged." Pure function for easy testing.
    Line-internal newlines in snippet text are flattened to spaces so
    the one-line-per-snippet invariant holds.
    """
    lines = []
    for start_seconds, text in snippets:
        # Round to the nearest whole second. The library returns start times
        # as floats with millisecond precision (e.g. 4.59, 2121.11); rounding
        # minimizes absolute error versus truncating.
        start = round(start_seconds)
        h, remainder = divmod(start, 3600)
        m, s = divmod(remainder, 60)
        clean = " ".join(text.split())  # collapse any whitespace, incl. newlines
        lines.append(f"[{h:02d}:{m:02d}:{s:02d}] {clean}")
    return "\n".join(lines)


def format_captions_as_srt(
    snippets: list[tuple[float, str]],
    durations: tuple[float, ...] | list[float],
) -> str:
    """Render (start, text) snippets + durations as standard SRT text.

    Produces real SRT format loadable by VLC, MPV, mkvtoolnix, Aegisub,
    etc. — 1-indexed sequence numbers, `HH:MM:SS,mmm --> HH:MM:SS,mmm`
    timestamp lines (comma decimal, not period), flattened text, blank
    line separators. Pure function for easy testing.

    If `durations` is shorter than `snippets` (or empty), missing entries
    fall back to a 2-second default so the output is still a valid SRT
    file rather than a crash. YouTube normally provides duration for
    every snippet; the fallback is belt-and-suspenders for exotic tracks.
    """
    if not snippets:
        return ""

    def _hms_ms(total_seconds: float) -> str:
        total_ms = max(0, round(total_seconds * 1000))
        h, remainder_ms = divmod(total_ms, 3600 * 1000)
        m, remainder_ms = divmod(remainder_ms, 60 * 1000)
        s, ms = divmod(remainder_ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    entries = []
    for idx, (start, text) in enumerate(snippets, start=1):
        duration = durations[idx - 1] if idx - 1 < len(durations) else 0.0
        if duration <= 0:
            duration = 2.0  # sane fallback for rare missing-duration case
        end = start + duration
        clean_text = " ".join(text.split())  # flatten internal newlines
        entries.append(f"{idx}\n{_hms_ms(start)} --> {_hms_ms(end)}\n{clean_text}\n")
    return "\n".join(entries)


def filter_snippets_by_range(
    snippets: list[tuple[float, str]],
    start_minutes: int | None,
    end_minutes: int | None,
) -> list[tuple[float, str]]:
    """Return snippets whose start time falls within [start_min, end_min).

    Mirrors the --start/--end semantics used by the video path. A None
    boundary means "no limit" on that side. start_minutes is inclusive,
    end_minutes is exclusive.
    """
    if start_minutes is None and end_minutes is None:
        return snippets
    lo = (start_minutes or 0) * 60
    hi = end_minutes * 60 if end_minutes is not None else None
    return [(start, text) for start, text in snippets if start >= lo and (hi is None or start < hi)]
