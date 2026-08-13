"""Tests for log_usage_metadata — observability helper for Gemini usage tokens."""

import logging
from types import SimpleNamespace

from gemini_common import log_usage_metadata


def _meta(
    prompt: int | None = 77312,
    cached: int | None = 0,
    candidates: int | None = 1204,
    total: int | None = 78516,
):
    """Build a SimpleNamespace that mimics Gemini's usage_metadata shape."""
    return SimpleNamespace(
        prompt_token_count=prompt,
        cached_content_token_count=cached,
        candidates_token_count=candidates,
        total_token_count=total,
    )


class TestLogUsageMetadataHappyPath:
    def test_all_fields_present_when_mindmap_emits_formatted_info_line(self, caplog):
        response = SimpleNamespace(usage_metadata=_meta())
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "mindmap")

        records = [r for r in caplog.records if r.name == "gemini_common"]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert records[0].getMessage() == "usage mindmap prompt=77312 cached=0 thoughts=0 candidates=1204 total=78516"

    def test_all_fields_present_when_transcript_uses_transcript_label(self, caplog):
        response = SimpleNamespace(
            usage_metadata=_meta(prompt=77312, cached=77000, candidates=14807, total=169119),
        )
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "transcript")

        records = [r for r in caplog.records if r.name == "gemini_common"]
        assert len(records) == 1
        assert (
            records[0].getMessage()
            == "usage transcript prompt=77312 cached=77000 thoughts=0 candidates=14807 total=169119"
        )


class TestLogUsageMetadataEdgeCases:
    def test_missing_cached_field_when_small_prompt_falls_back_to_zero(self, caplog):
        meta = SimpleNamespace(
            prompt_token_count=500,
            candidates_token_count=200,
            total_token_count=700,
        )
        response = SimpleNamespace(usage_metadata=meta)
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "concepts")

        records = [r for r in caplog.records if r.name == "gemini_common"]
        assert len(records) == 1
        assert "cached=0" in records[0].getMessage()
        assert "prompt=500" in records[0].getMessage()

    def test_usage_metadata_is_none_when_response_lacks_it_logs_warning_with_zeros(self, caplog):
        response = SimpleNamespace(usage_metadata=None)
        with caplog.at_level(logging.WARNING, logger="gemini_common"):
            log_usage_metadata(response, "mindmap")

        records = [r for r in caplog.records if r.name == "gemini_common"]
        # One warning line is emitted; no info line with the usage format
        assert any(r.levelno == logging.WARNING for r in records)

    def test_candidates_is_list_when_multimodal_shape_reads_as_unreadable_not_zero(self, caplog):
        """Gemini 3+ may return candidates_token_count as a list of ModalityTokenCount.

        The helper must not crash on this shape, and (issue #125) must not report
        it as ``0`` either: a candidates count of zero is what a truncated or
        blocked response looks like, so a multimodal list dressed as 0 would
        blind the output-cap check. It renders as ``?`` — still a well-formed,
        machine-parseable line, but an honest one.
        """
        multimodal_counts = [SimpleNamespace(modality="TEXT", token_count=100)]
        meta = SimpleNamespace(
            prompt_token_count=1000,
            cached_content_token_count=0,
            candidates_token_count=multimodal_counts,
            total_token_count=1100,
        )
        response = SimpleNamespace(usage_metadata=meta)
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "mindmap")

        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO]
        assert len(records) == 1
        assert "candidates=?" in records[0].getMessage()
        assert "candidates=0" not in records[0].getMessage()
        # Log line is still well-formed — machine-parseable
        assert records[0].getMessage().startswith("usage mindmap prompt=")

    def test_usage_metadata_attribute_access_raises_non_attribute_error_does_not_propagate(self, caplog):
        """If a future SDK quirk makes attribute access raise a non-AttributeError, observability must never break the caller."""

        class ExplodingMeta:
            @property
            def prompt_token_count(self):
                raise RuntimeError("SDK quirk")

        response = SimpleNamespace(usage_metadata=ExplodingMeta())
        with caplog.at_level(logging.WARNING, logger="gemini_common"):
            log_usage_metadata(response, "mindmap")  # must not raise

        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.WARNING]
        assert len(records) >= 1

    def test_usage_metadata_attribute_error_from_property_reads_as_unreadable_prompt(self, caplog):
        """AttributeError raised inside a property is swallowed by getattr (stdlib behavior).

        Issue #125: the swallowed value must surface as ``prompt=?`` / ``None``,
        never as ``prompt=0``. ``promptTokenCount`` is always present on a healthy
        response, so its absence means SDK drift — and both confabulation guards
        discard the artifact on a prompt of exactly 0. Reporting drift as a zero
        would turn one field rename into "every video is a confabulation".
        """

        class PropertyAttrErrMeta:
            @property
            def prompt_token_count(self):
                raise AttributeError("not available on this SDK version")

        response = SimpleNamespace(usage_metadata=PropertyAttrErrMeta())
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            counts = log_usage_metadata(response, "mindmap")  # must not raise

        # Well-formed log line still emitted, with the unreadable field marked
        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO]
        assert len(records) == 1
        assert "prompt=?" in records[0].getMessage()
        assert "prompt=0" not in records[0].getMessage()
        assert counts is not None and counts["prompt"] is None

    def test_response_lacks_usage_metadata_attribute_when_missing_does_not_raise(self, caplog):
        response = SimpleNamespace()  # no usage_metadata attr at all
        with caplog.at_level(logging.WARNING, logger="gemini_common"):
            log_usage_metadata(response, "concepts")  # must not raise


class TestLogUsageMetadataGemini3Thoughts:
    def test_thoughts_token_count_present_when_gemini_3_thinking_budget_active_appears_in_log_line(self, caplog):
        """Gemini 3.x thinking-enabled responses include thoughts_token_count.

        Per ai.google.dev/gemini-api/docs/tokens: when thinking is active,
        total_token_count > prompt + cached + candidates; the gap is thoughts.
        """
        meta = SimpleNamespace(
            prompt_token_count=50000,
            cached_content_token_count=0,
            thoughts_token_count=3200,  # gemini-3-flash-preview with thinking
            candidates_token_count=1200,
            total_token_count=54400,
        )
        response = SimpleNamespace(usage_metadata=meta)
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "transcript")

        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO]
        assert len(records) == 1
        assert "thoughts=3200" in records[0].getMessage()

    def test_thoughts_field_missing_when_legacy_gemini_2_response_defaults_to_zero(self, caplog):
        """Gemini 2.x responses have no thoughts_token_count attr — must default to 0, not crash."""
        meta = SimpleNamespace(
            prompt_token_count=1000,
            cached_content_token_count=0,
            candidates_token_count=200,
            total_token_count=1200,
            # thoughts_token_count intentionally absent
        )
        response = SimpleNamespace(usage_metadata=meta)
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "mindmap")

        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO]
        assert len(records) == 1
        assert "thoughts=0" in records[0].getMessage()


class TestLogUsageMetadataLabel:
    def test_label_appears_verbatim_when_arbitrary_string_is_passed(self, caplog):
        response = SimpleNamespace(usage_metadata=_meta())
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "my-custom-label")

        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO]
        assert len(records) == 1
        assert "usage my-custom-label prompt=" in records[0].getMessage()
