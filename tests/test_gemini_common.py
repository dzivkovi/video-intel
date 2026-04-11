"""Tests for gemini_common.py — shared Gemini API utilities."""

from gemini_common import get_retry_delay


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
