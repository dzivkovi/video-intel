"""Canonical table of Gemini usage-metadata shapes, shared across test modules.

Issue #125 established one rule at the ``gemini_common`` seam: a count the SDK
did not report readably coerces to ``None`` ("draw no conclusions"), and only a
count Gemini genuinely reported as int ``0`` coerces to ``0``. Every consumer of
that rule - the ``log_usage_metadata`` renderer, the transcript ``prompt == 0``
guard, the video-mindmap ``prompt == 0`` guard - must agree about which shapes
land in which bucket, so the table lives here once instead of being re-typed
(and re-interpreted) in each test module.

``UNREADABLE_SHAPES`` are the values that must yield ``None``. ``READABLE_SHAPES``
are the values that must yield the int they carry - including the zero that is
allowed to trip a confabulation guard.
"""

from __future__ import annotations

from types import SimpleNamespace

#: (id, raw value) -> must coerce to None whatever the field, because the shape
#: itself is unreadable. A wrong shape is never evidence of anything.
UNREADABLE_SHAPES: list[tuple[str, object]] = [
    ("float", 1234.0),
    ("modality_token_count_list", [SimpleNamespace(modality="TEXT", token_count=100)]),
    ("empty_list", []),
    ("string", "1234"),
    ("bool_true", True),
    ("bool_false", False),
    ("negative", -1),
    ("object", SimpleNamespace(token_count=5)),
]

#: A raw ``None`` is the one shape whose meaning is field-dependent: the REST
#: API omits ``cachedContentTokenCount`` / ``thoughtsTokenCount`` /
#: ``candidatesTokenCount`` to mean zero, but always sends ``promptTokenCount``
#: and ``totalTokenCount``, so their absence is drift. Verified live: an
#: uncached gemini-2.5-flash call returns ``cached_content_token_count=None``.
ABSENT_MEANS_ZERO_FIELDS = ("cached", "thoughts", "candidates")
ABSENT_MEANS_UNREADABLE_FIELDS = ("prompt", "total")

#: (id, raw value, expected int) -> must coerce to the int it carries.
READABLE_SHAPES: list[tuple[str, object, int]] = [
    ("zero", 0, 0),
    ("small", 1, 1),
    ("typical_prompt", 230741, 230741),
    ("near_output_cap", 65522, 65522),
]


class MissingAttr:
    """usage_metadata whose ``prompt_token_count`` attribute does not exist.

    Stands in for an SDK rename. ``getattr(usage, "prompt_token_count", <default>)``
    returns the default here, which is exactly why that default must be ``None``
    and not ``0``.
    """

    cached_content_token_count = 0
    thoughts_token_count = 0
    candidates_token_count = 1204
    total_token_count = 78516


class AttrErrorProperty:
    """usage_metadata whose ``prompt_token_count`` property raises AttributeError.

    ``getattr`` swallows AttributeError raised *inside* a property and returns
    the default (stdlib behavior), so this collapses to the same case as
    ``MissingAttr`` - and must likewise read as unreadable, not as zero.
    """

    @property
    def prompt_token_count(self):
        raise AttributeError("not available on this SDK version")

    cached_content_token_count = 0
    thoughts_token_count = 0
    candidates_token_count = 1204
    total_token_count = 78516


def usage_response(**counts):
    """Build a response object carrying a usage_metadata with the given counts.

    Unspecified fields default to plausible healthy values so a test can vary
    one field at a time.
    """
    defaults = {
        "prompt_token_count": 77312,
        "cached_content_token_count": 0,
        "thoughts_token_count": 0,
        "candidates_token_count": 1204,
        "total_token_count": 78516,
    }
    defaults.update(counts)
    return SimpleNamespace(usage_metadata=SimpleNamespace(**defaults))
