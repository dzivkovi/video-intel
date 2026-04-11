"""Shared Gemini API utilities. Used by video_intel.py and translate_video.py."""

import logging
import random
import sys

log = logging.getLogger(__name__)

RETRYABLE_SERVER_CODES: set[int] = {408, 500, 502, 503, 504}
RETRYABLE_RATE_CODES: set[int] = {429}


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
