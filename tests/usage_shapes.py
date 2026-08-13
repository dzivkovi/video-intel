"""Canonical table of Gemini usage-metadata shapes, shared across test modules.

Issue #125 established one rule at the ``gemini_common`` seam, and it splits on
**attribute presence**, not on which field is being read:

* the attribute is gone from the object (or raises ``AttributeError``) -> SDK
  drift -> ``None``, so a guard comparing ``== 0`` stays quiet;
* the attribute exists but holds ``None`` -> the wire omitted it, and
  protobuf-JSON omits an implicit-presence integer exactly when it is zero ->
  ``0``, so a genuine ``prompt == 0`` confabulation still trips the guard;
* a wrong *shape* (float, bool, string, negative, list) -> ``None``.

Every consumer of that rule - the ``log_usage_metadata`` renderer, the
transcript ``prompt == 0`` guard, the video-mindmap ``prompt == 0`` guard - has
to agree about which shapes land in which bucket, so the table lives here once
instead of being re-typed (and re-interpreted) in each test module.
"""

from __future__ import annotations

from types import SimpleNamespace

#: (id, raw value) -> must coerce to None. A wrong shape is never evidence of
#: anything, whichever field it turns up in.
UNREADABLE_SHAPES: list[tuple[str, object]] = [
    ("float", 1234.0),
    # A list in an AGGREGATE count field is drift: the documented type is
    # integer|None, and ModalityTokenCount lists live on the separate
    # *_tokens_details fields the helper never reads.
    ("drifted_list", [SimpleNamespace(modality="TEXT", token_count=100)]),
    ("empty_list", []),
    ("string", "1234"),
    ("bool_true", True),
    ("bool_false", False),
    ("negative", -1),
    ("object", SimpleNamespace(token_count=5)),
]

#: (id, raw value, expected int) -> must coerce to the int it carries.
READABLE_SHAPES: list[tuple[str, object, int]] = [
    ("zero", 0, 0),
    ("small", 1, 1),
    ("typical_prompt", 230741, 230741),
    ("near_output_cap", 65522, 65522),
]


class MissingAttr:
    """usage_metadata whose ``prompt_token_count`` attribute does not exist.

    Stands in for an SDK rename. This is the case that must read as unreadable:
    a guard that discards artifacts on a count of exactly 0 cannot be allowed to
    treat "the field moved" as "Gemini ingested nothing".
    """

    cached_content_token_count = 0
    thoughts_token_count = 0
    candidates_token_count = 1204
    total_token_count = 78516


class AttrErrorProperty:
    """usage_metadata whose ``prompt_token_count`` property raises AttributeError.

    ``getattr`` swallows AttributeError raised *inside* a property and returns
    the default (stdlib behavior), so this collapses to the same case as
    ``MissingAttr`` and must likewise read as unreadable.
    """

    @property
    def prompt_token_count(self):
        raise AttributeError("not available on this SDK version")

    cached_content_token_count = 0
    thoughts_token_count = 0
    candidates_token_count = 1204
    total_token_count = 78516


#: The two ways Gemini can report "no video was ingested". Both MUST trip the
#: confabulation guards. A literal 0 is the obvious one; ``None`` is what a
#: protobuf-JSON serializer emits for a zero-valued implicit-presence integer,
#: and treating it as unreadable would silently switch the guard off in exactly
#: the case it exists for.
CONFABULATION_PROMPT_VALUES = [0, None]


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
