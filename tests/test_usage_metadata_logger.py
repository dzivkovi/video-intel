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
        assert records[0].getMessage() == "usage mindmap prompt=77312 cached=0 candidates=1204 total=78516"

    def test_all_fields_present_when_transcript_uses_transcript_label(self, caplog):
        response = SimpleNamespace(
            usage_metadata=_meta(prompt=77312, cached=77000, candidates=14807, total=169119),
        )
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "transcript")

        records = [r for r in caplog.records if r.name == "gemini_common"]
        assert len(records) == 1
        assert records[0].getMessage() == "usage transcript prompt=77312 cached=77000 candidates=14807 total=169119"


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

    def test_candidates_is_list_when_multimodal_shape_coerces_to_zero_without_raising(self, caplog):
        """Gemini 3+ may return candidates_token_count as a list of ModalityTokenCount.

        The helper must not crash on this shape — coerce unusable values to 0 so the
        log format stays machine-parseable.
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
        assert "candidates=0" in records[0].getMessage()
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

    def test_usage_metadata_attribute_error_from_property_falls_back_silently(self, caplog):
        """AttributeError raised inside a property is swallowed by getattr (stdlib behavior)."""

        class PropertyAttrErrMeta:
            @property
            def prompt_token_count(self):
                raise AttributeError("not available on this SDK version")

        response = SimpleNamespace(usage_metadata=PropertyAttrErrMeta())
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "mindmap")  # must not raise

        # Well-formed log line still emitted with the missing field defaulted to 0
        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO]
        assert len(records) == 1
        assert "prompt=0" in records[0].getMessage()

    def test_response_lacks_usage_metadata_attribute_when_missing_does_not_raise(self, caplog):
        response = SimpleNamespace()  # no usage_metadata attr at all
        with caplog.at_level(logging.WARNING, logger="gemini_common"):
            log_usage_metadata(response, "concepts")  # must not raise


class TestLogUsageMetadataLabel:
    def test_label_appears_verbatim_when_arbitrary_string_is_passed(self, caplog):
        response = SimpleNamespace(usage_metadata=_meta())
        with caplog.at_level(logging.INFO, logger="gemini_common"):
            log_usage_metadata(response, "my-custom-label")

        records = [r for r in caplog.records if r.name == "gemini_common" and r.levelno == logging.INFO]
        assert len(records) == 1
        assert "usage my-custom-label prompt=" in records[0].getMessage()
