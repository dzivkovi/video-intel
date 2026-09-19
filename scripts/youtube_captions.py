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

# ---------------------------------------------------------------------------
# Why a caption fetch failed (issue #231)
# ---------------------------------------------------------------------------
#
# `youtube-transcript-api` ALREADY distinguishes these cases - `RequestBlocked`
# and `IpBlocked` are distinct classes from `NoTranscriptFound` and
# `TranscriptsDisabled`. The pre-#231 code caught their shared base class
# `CouldNotRetrieveTranscript` and reported every one as "No English captions
# available", discarding a distinction the library had already made.
#
# That is not cosmetic. Under `transcript_source: yt-captions` a block fails
# EVERY video in the scan, and the operator reads it as "this creator has no
# captions" - false, and it trains them to disbelieve the message.

CAPTIONS_FAILURE_ABSENT = "absent"
CAPTIONS_FAILURE_BLOCKED = "blocked"
CAPTIONS_FAILURE_VIDEO = "video_unavailable"
CAPTIONS_FAILURE_EMPTY = "empty"
CAPTIONS_FAILURE_NO_LIBRARY = "no_library"
CAPTIONS_FAILURE_OTHER = "other"

#: The sink is a diagnostic, not a transcript of the library.
_REASON_MESSAGE_MAX_CHARS = 300

#: Stated in the log and in the meta error so the operator is never told to run
#: a recovery that cannot work. MEASURED 2026-09-18: a block observed at 01:38
#: was still refusing the same three video ids at 19:30 - eighteen hours, not
#: the "wait a bit" this was first assumed to be. So the message names waiting
#: as uncertain and names the switch to Gemini as the certain route.
CAPTIONS_BLOCK_RECOVERY = (
    "YouTube is refusing caption requests from this IP (not a video without captions). "
    "A block observed on 2026-09-18 persisted at least 18 hours, so re-running shortly "
    "may not help; the certain route is transcript_source: gemini (or auto) for this run."
)

#: Exception class NAMES, resolved defensively at call time. A name is used
#: rather than a direct import because `RequestBlocked`/`IpBlocked` do not
#: exist in older `youtube-transcript-api` releases, and importing a missing
#: name would break the whole captions path rather than degrade one branch.
#: `YouTubeRequestFailed` is deliberately NOT here. Read the installed library:
#: `_transcripts.py::_raise_http_errors` raises `IpBlocked` for **429 only** and
#: `YouTubeRequestFailed` as the catch-all for everything else
#: `raise_for_status()` can throw - 403, 404, 500, 502, 503, a transient blip -
#: on any of three call sites. The 2026-09-18 measurement behind this feature is
#: entirely about `IpBlocked`; extending "YouTube is refusing you, switch to
#: gemini" to a one-off 5xx would be an unmeasured generalization, and the
#: operator might flip a channel off captions permanently over a blip. It falls
#: through to `other` and keeps the old, weaker wording, which is correct for an
#: unknown failure. `FailedToCreateConsentCookie` is left in `other` for the
#: same reason - refusal-adjacent, but unmeasured here.
_BLOCKED_EXC_NAMES = ("RequestBlocked", "IpBlocked", "PoTokenRequired")
_ABSENT_EXC_NAMES = ("NoTranscriptFound", "TranscriptsDisabled", "NotTranslatable")
_VIDEO_EXC_NAMES = ("VideoUnavailable", "VideoUnplayable", "AgeRestricted", "InvalidVideoId")


def _exception_kind(exc: BaseException) -> str:
    """Classify a youtube-transcript-api exception by its own class name.

    Walks the real MRO rather than matching one name, so `IpBlocked` is
    recognised through its `RequestBlocked` base without listing every
    subclass a future release might add.
    """
    names = {klass.__name__ for klass in type(exc).__mro__}
    if names & set(_BLOCKED_EXC_NAMES):
        return CAPTIONS_FAILURE_BLOCKED
    if names & set(_ABSENT_EXC_NAMES):
        return CAPTIONS_FAILURE_ABSENT
    if names & set(_VIDEO_EXC_NAMES):
        return CAPTIONS_FAILURE_VIDEO
    return CAPTIONS_FAILURE_OTHER


def captions_failure_is_refusal(kind: str | None) -> bool:
    """True when the fetch failed because we were refused, not because the
    video has no English track. Callers use this to decide whether their
    error message should claim anything about the video at all."""
    return kind == CAPTIONS_FAILURE_BLOCKED


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


def fetch_english_captions(video_id: str, *, reason_sink: dict | None = None) -> CaptionsResult | None:
    """Fetch the English caption track from YouTube, preferring manual over auto-generated.

    Returns a CaptionsResult on success, or None when no captions are
    available for any reason the caller should treat as "fall back to
    the video-understanding path." Any unexpected exception propagates
    so we do not silently swallow real problems.

    The library's default behavior for `find_transcript(['en'])` is to
    return a manually authored track when one exists, falling back to
    the auto-generated track only if no manual track is present. We
    rely on that default instead of re-implementing preference logic.

    ``reason_sink`` (issue #231): pass a FRESH dict per call to learn WHY a
    ``None`` came back - it is filled with ``{"kind", "exception", "message"}``
    where ``kind`` is one of the ``CAPTIONS_FAILURE_*`` constants. Omit it and
    behaviour is byte-identical to pre-#231, which is what keeps
    ``translate_video.py`` (operationally separate, same shared module)
    untouched by this change.

    The dict must be per-call, never one shared across a scan: the captions
    path runs under ``max_parallel`` threads, and a shared sink would let one
    video inherit another's failure reason - the same mistake the per-chunk
    ``usage_capture`` guardrail in CLAUDE.md exists to prevent.
    """

    def _note(kind: str, exc: BaseException | None = None) -> None:
        if reason_sink is None:
            return
        reason_sink["kind"] = kind
        reason_sink["exception"] = type(exc).__name__ if exc is not None else None
        # Truncated: the library's IpBlocked/RequestBlocked text is a ~1100-char
        # multi-paragraph essay about proxies and Webshare with README links.
        # Nothing reads this field today, but an unbounded blob sitting in a
        # dict that a future change might log or persist into meta.json is a
        # trap worth closing while it costs one line.
        raw_message = str(exc) if exc is not None else ""
        reason_sink["message"] = raw_message[:_REASON_MESSAGE_MAX_CHARS]

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
        _note(CAPTIONS_FAILURE_NO_LIBRARY)
        return None

    try:
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        transcript = transcript_list.find_transcript(["en"])
        fetched = transcript.fetch()
    except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable, CouldNotRetrieveTranscript) as e:
        # The CATCH is deliberately unchanged from pre-#231, including the three
        # names that are redundant with the base class in the real library
        # (`TranscriptsDisabled` and friends all derive from
        # `CouldNotRetrieveTranscript`). Narrowing it to the base alone would be
        # a real behaviour change against any version - or any test fixture -
        # where they are siblings rather than subclasses, and an escaping
        # exception is a far worse regression than the message this fixes.
        #
        # What changed is that the branch now CLASSIFIES before it speaks.
        kind = _exception_kind(e)
        _note(kind, e)
        if kind == CAPTIONS_FAILURE_BLOCKED:
            # WARNING, not info: under transcript_source: yt-captions this fails
            # every video in the scan, and it says nothing about the video.
            log.warning(
                "Caption request REFUSED for %s (%s). %s",
                video_id,
                type(e).__name__,
                CAPTIONS_BLOCK_RECOVERY,
            )
        else:
            log.info("No English captions available (%s) - falling back to video path", type(e).__name__)
        return None

    snippets = [(float(s.start), s.text) for s in fetched]
    durations = tuple(float(getattr(s, "duration", 0.0) or 0.0) for s in fetched)
    if not snippets:
        log.info("Captions list returned empty - falling back to video path")
        _note(CAPTIONS_FAILURE_EMPTY)
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
