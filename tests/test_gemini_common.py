"""Tests for gemini_common.py — shared Gemini API utilities."""

import logging
from types import SimpleNamespace

import pytest
from usage_shapes import (
    ABSENT_MEANS_UNREADABLE_FIELDS,
    ABSENT_MEANS_ZERO_FIELDS,
    READABLE_SHAPES,
    UNREADABLE_SHAPES,
    AttrErrorProperty,
    MissingAttr,
    usage_response,
)

from gemini_common import _coerce_token_count, get_retry_delay, log_usage_metadata


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
    """Issue #125: unreadable shapes must read as None, never as a reported zero.

    Both ``prompt == 0`` confabulation guards discard the artifact when they see
    an integer zero. If an unreadable shape coerced to 0, one SDK field rename
    would read as "every video is a confabulation" and destroy healthy output.
    """

    @pytest.mark.parametrize(
        "value",
        [pytest.param(v, id=name) for name, v in UNREADABLE_SHAPES],
    )
    @pytest.mark.parametrize("absent_means_zero", [True, False], ids=["omit_is_zero", "omit_is_drift"])
    def test_unreadable_shape_coerces_to_none_for_every_field(self, value, absent_means_zero):
        """A wrong shape is never evidence, regardless of what absence means for that field."""
        assert _coerce_token_count(value, absent_means_zero=absent_means_zero) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [pytest.param(v, exp, id=name) for name, v, exp in READABLE_SHAPES],
    )
    @pytest.mark.parametrize("absent_means_zero", [True, False], ids=["omit_is_zero", "omit_is_drift"])
    def test_sdk_reported_int_coerces_to_itself(self, value, expected, absent_means_zero):
        assert _coerce_token_count(value, absent_means_zero=absent_means_zero) == expected

    def test_sdk_reported_zero_is_not_none(self):
        """The distinction the guards depend on: a real 0 is evidence, None is not."""
        assert _coerce_token_count(0, absent_means_zero=False) == 0
        assert _coerce_token_count(0, absent_means_zero=False) is not None

    def test_absent_value_is_zero_only_where_the_api_omits_to_mean_zero(self):
        assert _coerce_token_count(None, absent_means_zero=True) == 0
        assert _coerce_token_count(None, absent_means_zero=False) is None


class TestLogUsageMetadataUnreadableCounts:
    """The dict values, and the log line, both carry the None-vs-zero split."""

    @pytest.mark.parametrize(
        "value",
        [pytest.param(v, id=name) for name, v in UNREADABLE_SHAPES] + [pytest.param(None, id="none")],
    )
    def test_unreadable_prompt_returns_none_so_guards_do_not_trip(self, value):
        counts = log_usage_metadata(usage_response(prompt_token_count=value), "transcript")

        assert counts is not None, "the call itself still succeeded"
        assert counts["prompt"] is None
        assert counts["prompt"] != 0, "an unreadable prompt must never compare equal to a reported zero"

    @pytest.mark.parametrize("field", ABSENT_MEANS_ZERO_FIELDS)
    def test_omitted_observability_field_still_reads_as_zero(self, field):
        """Live-verified: an uncached call reports cached_content_token_count=None.

        Rendering that as ? would print on nearly every call and destroy the
        cached=0 vs cached>0 signal the chunked path relies on.
        """
        attr = {"cached": "cached_content_token_count", "thoughts": "thoughts_token_count"}.get(
            field, f"{field}_token_count"
        )
        counts = log_usage_metadata(usage_response(**{attr: None}), "transcript")

        assert counts is not None
        assert counts[field] == 0

    @pytest.mark.parametrize("field", ABSENT_MEANS_UNREADABLE_FIELDS)
    def test_omitted_always_present_field_reads_as_unreadable(self, field):
        counts = log_usage_metadata(usage_response(**{f"{field}_token_count": None}), "transcript")

        assert counts is not None
        assert counts[field] is None

    def test_drifted_candidates_list_is_unreadable_not_zero(self):
        """Issue #128 needs this: a list coerced to 0 hides a truncated response."""
        counts = log_usage_metadata(
            usage_response(candidates_token_count=[SimpleNamespace(modality="TEXT", token_count=100)]),
            "transcript",
        )

        assert counts is not None
        assert counts["candidates"] is None

    def test_sdk_reported_zero_prompt_survives_as_zero(self):
        counts = log_usage_metadata(usage_response(prompt_token_count=0), "transcript")

        assert counts is not None
        assert counts["prompt"] == 0

    @pytest.mark.parametrize("meta_cls", [MissingAttr, AttrErrorProperty], ids=["missing_attr", "attr_error_property"])
    def test_absent_prompt_attribute_reads_as_unreadable_not_zero(self, meta_cls):
        """An SDK rename must not masquerade as 'Gemini ingested nothing'."""
        response = SimpleNamespace(usage_metadata=meta_cls())

        counts = log_usage_metadata(response, "mindmap")

        assert counts is not None
        assert counts["prompt"] is None

    def test_unreadable_counts_render_as_question_mark_in_the_log_line(self, caplog):
        response = usage_response(prompt_token_count=None, candidates_token_count=1204)

        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "mindmap")

        line = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO][0].getMessage()
        assert "prompt=?" in line
        assert "candidates=1204" in line, "readable neighbours still render as numbers"

    def test_candidates_stays_readable_for_the_output_cap_check(self):
        """Issue #128 compares candidates against MAX_OUTPUT_TOKENS; it needs a real int."""
        counts = log_usage_metadata(usage_response(candidates_token_count=65522), "transcript")

        assert counts is not None
        assert counts["candidates"] == 65522
