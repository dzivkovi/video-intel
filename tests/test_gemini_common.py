"""Tests for gemini_common.py — shared Gemini API utilities."""

import logging
from types import SimpleNamespace

import pytest
from usage_shapes import (
    CONFABULATION_PROMPT_VALUES,
    READABLE_SHAPES,
    UNREADABLE_SHAPES,
    AttrErrorProperty,
    MissingAttr,
    usage_response,
)

from gemini_common import _MISSING, _coerce_token_count, get_retry_delay, log_usage_metadata


class TestGetRetryDelay:
    def test_server_error_503_gets_long_retry_budget(self, monkeypatch):
        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        exc = errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})

        result = get_retry_delay(exc, 0, max_retries_rate=3, max_retries_server=8)

        assert result == ("Server error", 60, 8)

    def test_rate_limit_429_gets_short_retry_budget(self, monkeypatch):
        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        exc = errors.APIError(429, {"error": {"message": "quota hit", "status": "RESOURCE_EXHAUSTED"}})

        result = get_retry_delay(exc, 1, max_retries_rate=3, max_retries_server=8)

        assert result == ("Rate limited (429)", 30, 3)

    def test_non_retryable_api_error_returns_none(self):
        from google.genai import errors

        exc = errors.APIError(400, {"error": {"message": "bad request", "status": "INVALID_ARGUMENT"}})

        assert get_retry_delay(exc, 0, max_retries_rate=3, max_retries_server=8) is None

    def test_non_api_error_returns_none(self):
        assert get_retry_delay(RuntimeError("503 overloaded"), 0, max_retries_rate=3, max_retries_server=8) is None

    def test_server_error_stops_retrying_after_budget(self):
        from google.genai import errors

        exc = errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})

        assert get_retry_delay(exc, 8, max_retries_rate=3, max_retries_server=8) is None

    def test_default_server_budget_is_8(self, monkeypatch):
        from google.genai import errors

        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        exc = errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})

        # Default max_retries_server=8 — attempt 7 should still retry
        result = get_retry_delay(exc, 7)
        assert result is not None
        assert result[2] == 8

        # Attempt 8 should exhaust budget
        assert get_retry_delay(exc, 8) is None


class TestCreateClient:
    def test_create_client_returns_client(self):
        from gemini_common import create_client

        client = create_client("test-api-key")
        assert client is not None

    def test_create_client_custom_read_timeout(self):
        from gemini_common import create_client

        # Verify it doesn't crash with a custom timeout
        client = create_client("test-api-key", read_timeout=60)
        assert client is not None


class TestBuildPermissiveSafetySettings:
    def test_returns_four_categories_all_block_none(self):
        from google.genai import types

        from gemini_common import build_permissive_safety_settings

        settings = build_permissive_safety_settings(types)
        assert len(settings) == 4
        categories = {str(s.category) for s in settings}
        # All four standard harm categories present
        assert any("HARASSMENT" in c for c in categories)
        assert any("HATE_SPEECH" in c for c in categories)
        assert any("SEXUALLY_EXPLICIT" in c for c in categories)
        assert any("DANGEROUS_CONTENT" in c for c in categories)
        # All thresholds are BLOCK_NONE
        for s in settings:
            assert "BLOCK_NONE" in str(s.threshold)

    def test_omits_civic_integrity(self):
        # CIVIC_INTEGRITY is intentionally excluded — not universally supported
        # on Gemini 2.x and unrelated to violence/war coverage.
        from google.genai import types

        from gemini_common import build_permissive_safety_settings

        settings = build_permissive_safety_settings(types)
        categories = {str(s.category) for s in settings}
        assert not any("CIVIC_INTEGRITY" in c for c in categories)


class TestCoerceTokenCount:
    """Issue #125: the split is on ATTRIBUTE PRESENCE, not on which field it is.

    Both ``prompt == 0`` confabulation guards discard the artifact when they see
    an integer zero, so the helper has to get two opposite things right at once:
    an SDK rename must NOT read as "every video is a confabulation", and a
    genuine zero must still trip the guard however Gemini chose to encode it.
    """

    @pytest.mark.parametrize("value", [pytest.param(v, id=name) for name, v in UNREADABLE_SHAPES])
    def test_unreadable_shape_coerces_to_none(self, value):
        """A wrong shape is never evidence of anything."""
        assert _coerce_token_count(value) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [pytest.param(v, exp, id=name) for name, v, exp in READABLE_SHAPES],
    )
    def test_sdk_reported_int_coerces_to_itself(self, value, expected):
        assert _coerce_token_count(value) == expected

    def test_absent_attribute_is_unreadable(self):
        """The rename case: the guard must stay quiet, not fire on everything."""
        assert _coerce_token_count(_MISSING) is None

    def test_present_but_none_is_a_reported_zero(self):
        """The wire omits an implicit-presence integer exactly when it is zero.

        Reading this as unreadable would silently switch OFF the confabulation
        guard in precisely the case it exists for.
        """
        assert _coerce_token_count(None) == 0

    def test_the_two_absences_are_not_the_same_value(self):
        assert _coerce_token_count(_MISSING) is not _coerce_token_count(None)


class TestLogUsageMetadataUnreadableCounts:
    """The dict values, and the log line, both carry the presence split."""

    @pytest.mark.parametrize("value", [pytest.param(v, id=name) for name, v in UNREADABLE_SHAPES])
    def test_unreadable_prompt_returns_none_so_guards_do_not_trip(self, value):
        counts = log_usage_metadata(usage_response(prompt_token_count=value), "transcript")

        assert counts is not None, "the call itself still succeeded"
        assert counts["prompt"] is None
        assert counts["prompt"] != 0, "an unreadable prompt must never compare equal to a reported zero"

    @pytest.mark.parametrize("meta_cls", [MissingAttr, AttrErrorProperty], ids=["missing_attr", "attr_error_property"])
    def test_absent_prompt_attribute_reads_as_unreadable_not_zero(self, meta_cls):
        """An SDK rename must not masquerade as 'Gemini ingested nothing'."""
        counts = log_usage_metadata(SimpleNamespace(usage_metadata=meta_cls()), "mindmap")

        assert counts is not None
        assert counts["prompt"] is None

    def test_unreadable_prompt_warns_that_the_guard_cannot_run(self, caplog):
        """A guard that stops guarding must never do it silently."""
        with caplog.at_level(logging.WARNING, logger="gemini_common"):
            log_usage_metadata(SimpleNamespace(usage_metadata=MissingAttr()), "mindmap")

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("confabulation guard cannot run" in m for m in warnings)

    @pytest.mark.parametrize("value", CONFABULATION_PROMPT_VALUES)
    def test_both_encodings_of_zero_prompt_still_trip_the_guard(self, value):
        """A literal 0 and an omitted-because-zero None must be indistinguishable here.

        This is the whole reason the split is on attribute presence: the guard
        stays correct without anyone having to settle which encoding Gemini uses.
        """
        counts = log_usage_metadata(usage_response(prompt_token_count=value), "transcript")

        assert counts is not None
        assert counts["prompt"] == 0

    @pytest.mark.parametrize(
        "field",
        ["cached", "thoughts", "candidates", "total"],
    )
    def test_omitted_observability_field_reads_as_zero(self, field):
        """Live-verified: an uncached call reports cached_content_token_count=None.

        Rendering that as ? would print on nearly every line and destroy the
        cached=0 vs cached>0 signal the chunked path tells operators to read.
        """
        attr = {"cached": "cached_content_token_count"}.get(field, f"{field}_token_count")
        counts = log_usage_metadata(usage_response(**{attr: None}), "transcript")

        assert counts is not None
        assert counts[field] == 0

    def test_drifted_candidates_list_is_unreadable_not_zero(self):
        """A list coerced to 0 would look exactly like a truncated response."""
        counts = log_usage_metadata(
            usage_response(candidates_token_count=[SimpleNamespace(modality="TEXT", token_count=100)]),
            "transcript",
        )

        assert counts is not None
        assert counts["candidates"] is None

    def test_unreadable_counts_render_as_question_mark_in_the_log_line(self, caplog):
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(usage_response(prompt_token_count="oops", candidates_token_count=1204), "mindmap")

        line = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO][0].getMessage()
        assert "prompt=?" in line
        assert "candidates=1204" in line, "readable neighbours still render as numbers"

    def test_healthy_line_is_all_numbers(self):
        """No churn on the common case."""
        counts = log_usage_metadata(usage_response(), "transcript")

        assert counts is not None
        assert all(v is not None for v in counts.values())
