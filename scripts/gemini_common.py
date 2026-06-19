"""Shared Gemini API utilities. Used by video_intel.py and translate_video.py."""

import logging
import random
import sys

log = logging.getLogger(__name__)

RETRYABLE_SERVER_CODES: set[int] = {408, 500, 502, 503, 504}
RETRYABLE_RATE_CODES: set[int] = {429}


def _coerce_token_count(value: object) -> int:
    """Coerce a Gemini usage_metadata field to a non-negative int.

    Gemini SDK may return a bare int, None, or (on gemini-3 multimodal
    responses) a list of ModalityTokenCount submodels. Anything that
    isn't a plain numeric int is treated as 0 so the log format stays
    machine-parseable across SDK shape drift.
    """
    if isinstance(value, bool):  # bool is a subclass of int; reject it
        return 0
    if isinstance(value, int):
        return max(value, 0)
    return 0


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

    ``thoughts_token_count`` is Gemini 3.x-specific (the thinking process);
    legacy 2.x responses omit it and we default to 0. Per Gemini usage_metadata
    docs (ai.google.dev/gemini-api/docs/tokens), when thoughts > 0 the
    ``total_token_count`` will exceed ``prompt + cached + candidates``.

    Observability must never break the caller. Any unexpected shape
    produces a warning log and returns ``None`` — it does not raise.
    """
    try:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            log.warning("usage %s: response.usage_metadata missing or None", label)
            return None
        prompt = _coerce_token_count(getattr(usage, "prompt_token_count", 0))
        cached = _coerce_token_count(getattr(usage, "cached_content_token_count", 0))
        thoughts = _coerce_token_count(getattr(usage, "thoughts_token_count", 0))
        candidates = _coerce_token_count(getattr(usage, "candidates_token_count", 0))
        total = _coerce_token_count(getattr(usage, "total_token_count", 0))
        log.info(
            "usage %s prompt=%d cached=%d thoughts=%d candidates=%d total=%d",
            label,
            prompt,
            cached,
            thoughts,
            candidates,
            total,
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
