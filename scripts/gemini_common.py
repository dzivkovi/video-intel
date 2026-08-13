"""Shared Gemini API utilities. Used by video_intel.py and translate_video.py."""

import logging
import random
import sys

log = logging.getLogger(__name__)

RETRYABLE_SERVER_CODES: set[int] = {408, 500, 502, 503, 504}
RETRYABLE_RATE_CODES: set[int] = {429}


#: Rendered in the usage log line for a count the SDK did not report readably.
#: Deliberately not "0": see _coerce_token_count for why the two must not be
#: confused, and issue #128 for the operator who reads these lines by eye.
UNREADABLE_COUNT_DISPLAY = "?"


def _coerce_token_count(value: object, *, absent_means_zero: bool) -> int | None:
    """Coerce a Gemini usage_metadata field to a non-negative int, or None.

    ``None`` means "the SDK did not report a readable count, draw no
    conclusions". The split is load-bearing, not cosmetic (issue #125): both
    ``prompt == 0`` confabulation guards treat an integer zero as proof that
    Gemini ingested no video and DISCARD the artifact, so a shape they cannot
    read must never arrive dressed as a reported zero.

    Two distinct causes of a ``None`` raw value have to be told apart, because
    the REST API omits a count for two different reasons:

    * **Omitted because it is zero.** ``cachedContentTokenCount`` is absent
      when nothing was served from cache; ``thoughtsTokenCount`` is absent on
      a non-thinking model. Pydantic materializes both as ``None``. Verified
      against a live gemini-2.5-flash call: an uncached request really does
      return ``cached_content_token_count=None``. These are genuine zeros, and
      callers pass ``absent_means_zero=True`` so the log keeps printing
      ``cached=0``, a signal the chunked-transcript path documents and the
      operator reads to audit implicit-cache hits across chunks.
    * **Omitted because something is wrong.** ``promptTokenCount`` and
      ``totalTokenCount`` are always present on a successful response, so their
      absence is drift (a rename, a shape change), not a zero. Callers pass
      ``absent_means_zero=False`` and get ``None``.

    A *wrong shape* is unreadable either way and always yields ``None``: a
    float, a bool, a string, a negative number, or a list.

    On that last one, be precise about WHY. Every aggregate count here is
    documented as ``integer | None``; the ``ModalityTokenCount`` lists live on
    the separate ``*_tokens_details`` fields (``candidates_tokens_details``,
    ``prompt_tokens_details``), which this helper never reads. So a list
    arriving in an aggregate field is drift or a malformed response, NOT an
    expected multimodal shape. Rejecting it is still the right defensive move -
    coercing it to 0 would make a response look like it emitted no output
    tokens at all, blinding the output-cap check - but nobody should build on
    it as though gemini-3 routinely reports counts that way.
    """
    if value is None:
        return 0 if absent_means_zero else None
    if isinstance(value, bool):  # bool is a subclass of int; not a token count
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    return None


def _fmt_token_count(value: int | None) -> str:
    """Render a coerced count for the usage log line."""
    return UNREADABLE_COUNT_DISPLAY if value is None else str(value)


def log_usage_metadata(response: object, label: str) -> dict | None:
    """Log a single info-level line summarizing Gemini token usage.

    Emits: ``usage {label} prompt=N cached=N thoughts=N candidates=N total=N``.

    Returns the parsed counts as a dict (keys: prompt, cached, thoughts,
    candidates, total) so callers that need the numbers - e.g. the issue #60
    confabulation guard, which treats ``prompt == 0`` as "Gemini ingested no
    video tokens" - can read them off the same call that logs them. Returns
    ``None`` when usage_metadata is missing or unreadable, so a None return
    means "could not confirm" (never flag a confabulation on missing data).
    Existing callers ignore the return value, so this is backward compatible.

    Individual dict values are ``int | None`` on the same principle (issue
    #125): a count the SDK did not report readably comes back as ``None`` and
    renders as ``?`` in the log line, never as ``0``. A guard comparing
    ``counts["prompt"] == 0`` therefore stays quiet on SDK drift and fires
    only on a count Gemini genuinely reported as zero. See
    ``_coerce_token_count`` for which fields treat an omitted value as zero
    (``cached``, ``thoughts``, ``candidates``) and which treat it as drift
    (``prompt``, ``total``).

    ``thoughts_token_count`` is Gemini 3.x-specific (the thinking process);
    legacy 2.x responses omit it, which reads as ``0`` here because the API
    omits that field exactly when no thinking happened. Per Gemini
    usage_metadata docs (ai.google.dev/gemini-api/docs/tokens), when
    thoughts > 0 the ``total_token_count`` will exceed
    ``prompt + cached + candidates``.

    Observability must never break the caller. Any unexpected shape
    produces a warning log and returns ``None`` — it does not raise.
    """
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            log.warning("usage %s: response.usage_metadata missing or None", label)
            return None
        # The getattr defaults are None, not 0: an attribute the SDK renamed
        # away must reach the coercion step as "absent" so the per-field rule
        # below decides what absence means (issue #125). prompt and total are
        # always present on a healthy response, so absence is drift; the other
        # three are omitted precisely when they are zero.
        prompt = _coerce_token_count(getattr(usage, "prompt_token_count", None), absent_means_zero=False)
        total = _coerce_token_count(getattr(usage, "total_token_count", None), absent_means_zero=False)
        cached = _coerce_token_count(getattr(usage, "cached_content_token_count", None), absent_means_zero=True)
        thoughts = _coerce_token_count(getattr(usage, "thoughts_token_count", None), absent_means_zero=True)
        candidates = _coerce_token_count(getattr(usage, "candidates_token_count", None), absent_means_zero=True)
        log.info(
            "usage %s prompt=%s cached=%s thoughts=%s candidates=%s total=%s",
            label,
            _fmt_token_count(prompt),
            _fmt_token_count(cached),
            _fmt_token_count(thoughts),
            _fmt_token_count(candidates),
            _fmt_token_count(total),
        )
        return {
            "prompt": prompt,
            "cached": cached,
            "thoughts": thoughts,
            "candidates": candidates,
            "total": total,
        }
    except Exception as exc:
        log.warning("usage %s: failed to read usage_metadata (%s)", label, exc)
        return None


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------


def require_gemini():
    """Lazy import google.genai with a clear error message."""
    try:
        from google import genai
        from google.genai import types

        return genai, types
    except ImportError:
        log.error("google-genai not installed. Run: pip install google-genai")
        sys.exit(1)


def require_youtube():
    """Lazy import googleapiclient with a clear error message."""
    try:
        from googleapiclient.discovery import build

        return build
    except ImportError:
        log.error("google-api-python-client not installed. Run: pip install google-api-python-client")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def create_client(api_key: str, *, read_timeout: int = 1200):
    """Create a Gemini client with httpx timeout config.

    read_timeout: seconds of server silence before aborting (default 20 min).
    Does NOT limit total request time — only the gap between data on the wire.
    """
    import httpx

    genai, types = require_gemini()
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            client_args={"timeout": httpx.Timeout(connect=30, read=read_timeout, write=30, pool=30)},
        ),
    )


# ---------------------------------------------------------------------------
# Safety settings
# ---------------------------------------------------------------------------


def build_permissive_safety_settings(types):
    """Return safety_settings list that disables all filters we can safely disable.

    Used by transcription, translation, and mind-map generation across this
    skill. The pipeline is a faithful-reporting tool: it transcribes what was
    said on-screen, not content it generated itself. Filter-induced silent
    truncation produces broken subtitles mid-sentence for ordinary news
    coverage (war, politics, violence). Users rely on file-level coverage
    diagnostics and finish_reason annotations to see when a model refused
    to continue — blocking content here only hides the problem.

    CIVIC_INTEGRITY is intentionally omitted: it is not universally supported
    on Gemini 2.x models and is unrelated to the violence/politics cases
    that trigger silent truncations in practice.
    """
    return [
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ]


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------


def get_retry_delay(
    exc: Exception,
    attempt: int,
    *,
    max_retries_rate: int = 3,
    max_retries_server: int = 8,
) -> tuple[str, float, int] | None:
    """Return (kind, wait_seconds, max_for_type) for retryable Gemini API errors, or None."""
    from google.genai import errors

    if not isinstance(exc, errors.APIError):
        return None

    if exc.code in RETRYABLE_SERVER_CODES:
        if attempt >= max_retries_server:
            return None
        base_wait = 60 * (2 ** min(attempt, 3))  # 60s, 120s, 240s, 480s cap
        return "Server error", base_wait + random.uniform(0, 10), max_retries_server

    if exc.code in RETRYABLE_RATE_CODES:
        if attempt >= max_retries_rate:
            return None
        base_wait = 15 * (2**attempt)  # 15s, 30s, 60s
        return "Rate limited (429)", base_wait + random.uniform(0, 10), max_retries_rate

    return None
