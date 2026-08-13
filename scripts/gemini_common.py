"""Shared Gemini API utilities. Used by video_intel.py and translate_video.py."""

import logging
import random
import sys

log = logging.getLogger(__name__)

RETRYABLE_SERVER_CODES: set[int] = {408, 500, 502, 503, 504}
RETRYABLE_RATE_CODES: set[int] = {429}

#: Retries allowed for a transport-layer failure (issue #129). ONE, deliberately:
#: far below the server-error budget, because a server error carries a verdict
#: from Gemini while a dropped socket costs a full re-billed call with no
#: evidence the next one will land. One retry covered every occurrence observed
#: in the 2026-08-11/12 bulk ingest (7 drops, each fixed by a single re-run).
#:
#: This is a per-CALL budget on purpose - there is no run-level cap on top. A
#: chunked transcript of N chunks can therefore spend up to N extra calls worst
#: case, which is bounded and proportional: each chunk is an independent unit of
#: work, and a shared run-level budget would let chunk 1's bad luck starve
#: chunk 8 of the retry that would have saved it.
MAX_RETRIES_TRANSPORT: int = 1

#: Seconds before a transport retry (plus 0-3s jitter). Kept small ON PURPOSE.
#: Transcript calls run inside ``_run_with_timeout``'s 600s wall-clock cap
#: (issue #74) which wraps the whole ``call_gemini`` invocation including this
#: sleep, so the backoff competes with the real call for that budget. The
#: server-error ladder's 60-480s waits would consume it on their own.
TRANSPORT_RETRY_BASE_WAIT: int = 2


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
    only on a count Gemini genuinely reported as zero. The split is on
    ATTRIBUTE PRESENCE and is identical for all five fields: a missing
    attribute is drift (``None``), an attribute holding ``None`` is a zero the
    wire omitted (``0``). See ``_coerce_token_count`` - and do not reintroduce
    a per-field rule, which would mute the confabulation guards.

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


def is_transient_transport_error(exc: Exception) -> bool:
    """True for an httpx failure where no server verdict was ever delivered.

    Issue #129. ``Server disconnected without sending a response.`` is
    ``httpx.RemoteProtocolError``, not a ``google.genai.errors.APIError``, so
    before this existed ``get_retry_delay`` returned ``None`` for it and one
    dropped socket killed the whole pipeline step. The SDK does not wrap httpx
    exceptions (``google.genai._api_client`` has no ``except httpx...`` clause),
    so they arrive here raw.

    ``httpx.TransportError`` is the right net because it is defined by the
    request never completing: connect/read/write/pool timeouts, connection and
    protocol drops, proxy failures. Its two client-side members are excluded,
    because retrying either fails identically and only burns time:

    * ``LocalProtocolError`` - we built a malformed request.
    * ``UnsupportedProtocol`` - the URL scheme is wrong.

    ``HTTPStatusError`` is deliberately NOT here: it is not a ``TransportError``
    at all, and a status carrying a real server verdict is the ``APIError``
    branch's business. That separation is what keeps ``PERMISSION_DENIED`` (403)
    and ``INVALID_ARGUMENT`` (400) failing fast.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx ships with google-genai
        return False
    if isinstance(exc, httpx.LocalProtocolError | httpx.UnsupportedProtocol):
        return False
    return isinstance(exc, httpx.TransportError)


def get_retry_delay(
    exc: Exception,
    attempt: int,
    *,
    max_retries_rate: int = 3,
    max_retries_server: int = 8,
    max_retries_transport: int = 0,
) -> tuple[str, float, int] | None:
    """Return (kind, wait_seconds, max_for_type) for retryable Gemini failures, or None.

    Three disjoint classes, checked in this order:

    1. ``APIError`` - the server answered. Retryable only for the 5xx/408 and
       429 code sets; **every other code returns None from inside this branch**,
       which is what makes ``PERMISSION_DENIED`` and ``INVALID_ARGUMENT`` fail
       fast. That early return is load-bearing: an ``APIError`` must never fall
       through to the transport check below (issue #129).
    2. Transport failure - the request never completed, so there is no verdict
       to respect. Small budget, short backoff, and **off unless the caller asks
       for it**.
    3. Anything else - not retryable.

    ``max_retries_transport`` defaults to ``0`` so this stays a pure addition:
    ``translate_video.py`` shares this helper and is operationally separate from
    the video-intel pipeline, so it must not inherit a new retry policy as a
    side effect of a video-intel ticket. video-intel's call sites opt in
    explicitly with ``MAX_RETRIES_TRANSPORT``. Enabling it for the translator
    later is a one-argument change, and should be its own deliberate diff with
    its own smoke test rather than a silent inheritance.

    The ``prompt == 0`` confabulation guards (issues #60/#119/#123) are
    untouched by class 2 and cannot be: they inspect usage metadata on a
    response that arrived successfully, so they run *after* this function's
    caller has already returned. A refusal there is not an exception this loop
    ever sees.
    """
    from google.genai import errors

    if isinstance(exc, errors.APIError):
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

    if is_transient_transport_error(exc):
        if attempt >= max_retries_transport:
            return None
        base_wait = TRANSPORT_RETRY_BASE_WAIT * (2**attempt)
        return "Transport error", base_wait + random.uniform(0, 3), max_retries_transport

    return None
