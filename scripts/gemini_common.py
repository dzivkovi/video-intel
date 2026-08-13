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


#: Sentinel for "this attribute is not on the usage object at all", which is a
#: different thing from an attribute that exists and holds ``None``.
_MISSING = object()


def _coerce_token_count(value: object) -> int | None:
    """Coerce a Gemini usage_metadata field to a non-negative int, or None.

    ``None`` means "the SDK did not report a readable count, draw no
    conclusions". The split is load-bearing, not cosmetic (issue #125): both
    ``prompt == 0`` confabulation guards treat an integer zero as proof that
    Gemini ingested no video and DISCARD the artifact, so a shape they cannot
    read must never arrive dressed as a reported zero.

    The decisive distinction is **attribute presence**, not which field it is:

    * **Attribute absent** (``_MISSING`` - the name is gone from the object, or
      accessing it raised ``AttributeError``) means SDK drift: a rename, a
      restructure. Yields ``None``, so a guard reading ``== 0`` stays quiet
      rather than declaring every video a confabulation. This is what issue
      #125 exists to fix.
    * **Attribute present, value ``None``** means the count was omitted on the
      wire, and protobuf-JSON omits an implicit-presence integer exactly when
      it is **zero**. Yields ``0``. Verified live: an uncached
      ``gemini-2.5-flash`` call returns ``cached_content_token_count=None``
      while genuinely having cached nothing.

    Splitting on presence rather than on a per-field guess is what makes this
    safe. An earlier revision hard-coded "``promptTokenCount`` is always sent,
    so its absence is drift" - but the SDK declares all five counts identically
    (``Optional[int]``, default ``None``), and if the serializer omits zeros
    then a genuine ``prompt == 0`` confabulation would arrive as ``None`` and
    SILENTLY MUTE both guards. Under the presence rule the guard fires whether
    Gemini sends a literal ``0`` or omits the field, so the behavior is correct
    without needing to settle which one it does.

    A *wrong shape* always yields ``None``: a float, a bool, a string, a
    negative number, or a list. On the list, be precise about why. Every
    aggregate count here is documented ``integer | None``; the
    ``ModalityTokenCount`` lists live on the separate ``*_tokens_details``
    fields, which this helper never reads. A list in an aggregate field is
    drift or a malformed response, not an expected multimodal shape. Rejecting
    it is still right - coercing it to 0 would make a response look like it
    emitted no output tokens at all - but nobody should build on it as though
    gemini-3 routinely reports counts that way.
    """
    if value is _MISSING:
        return None
    if value is None:
        return 0
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
        # The getattr default is the _MISSING sentinel, never 0 and never None:
        # the coercion step has to be able to tell "the SDK renamed this away"
        # from "the wire omitted a zero" (issue #125).
        prompt = _coerce_token_count(getattr(usage, "prompt_token_count", _MISSING))
        total = _coerce_token_count(getattr(usage, "total_token_count", _MISSING))
        cached = _coerce_token_count(getattr(usage, "cached_content_token_count", _MISSING))
        thoughts = _coerce_token_count(getattr(usage, "thoughts_token_count", _MISSING))
        candidates = _coerce_token_count(getattr(usage, "candidates_token_count", _MISSING))
        if prompt is None:
            # The confabulation guards read this field and can only fail open.
            # Say so out loud: a silent fail-open is how a guard stops guarding
            # without anyone noticing.
            log.warning(
                "usage %s: prompt_token_count is unreadable - the prompt==0 confabulation guard cannot run",
                label,
            )
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
