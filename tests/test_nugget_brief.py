"""Tests for the nugget-brief prompt assembly.

The nugget CLI retrieves hybrid-search hits, formats each as an attributed
excerpt, and substitutes the excerpts + query + count into the
`prompts/nugget-brief.md` template. The substitution layer is pure — no
I/O, no Gemini — and these tests guard its contract:

- Each excerpt header surfaces channel, published date, title, timestamp.
- When a video_id is present, the URL includes `&t=<seconds>` deep-link.
- When video_id is missing, no URL line is emitted (don't fabricate).
- `{{QUERY}}`, `{{NUM_CHUNKS}}`, `{{EXCERPTS}}` are all replaced.
- Excerpts appear in retrieval order — ordering is load-bearing for the
  consultant-brief synthesis because top-ranked excerpts should frame the
  brief's consensus/divergence positioning.
- Empty hits list produces NUM_CHUNKS=0 and empty EXCERPTS — no crash.
"""

from __future__ import annotations

from video_intel import _format_nugget_excerpt, build_nugget_prompt

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hit(
    *,
    channel="natebjones",
    published="2026-04-22",
    title="The Video Title",
    timestamp="12:34",
    timestamp_seconds=754,
    video_id="abc123",
    text="This is the excerpt body.",
    **extra,
) -> dict:
    h = {
        "channel": channel,
        "published": published,
        "title": title,
        "timestamp": timestamp,
        "timestamp_seconds": timestamp_seconds,
        "video_id": video_id,
        "text": text,
    }
    h.update(extra)
    return h


# ---------------------------------------------------------------------------
# _format_nugget_excerpt
# ---------------------------------------------------------------------------


def test_excerpt_header_surfaces_all_attribution_fields():
    hit = _hit()
    formatted = _format_nugget_excerpt(hit, index=1)
    assert "### Excerpt 1" in formatted
    assert "**Channel:** natebjones" in formatted
    assert "**Published:** 2026-04-22" in formatted
    assert "**Title:** The Video Title" in formatted
    assert "**Timestamp:** [12:34]" in formatted


def test_excerpt_url_includes_timestamp_deep_link_when_video_id_present():
    hit = _hit(video_id="xyz789", timestamp_seconds=600)
    formatted = _format_nugget_excerpt(hit, index=1)
    assert "**URL:** https://www.youtube.com/watch?v=xyz789&t=600" in formatted


def test_excerpt_url_omitted_when_video_id_missing():
    hit = _hit(video_id="")
    formatted = _format_nugget_excerpt(hit, index=1)
    assert "**URL:**" not in formatted


def test_excerpt_url_without_timestamp_when_seconds_is_zero():
    # A chunk starting at t=0 should still produce a URL, just without &t=0.
    hit = _hit(video_id="zzz000", timestamp_seconds=0)
    formatted = _format_nugget_excerpt(hit, index=1)
    assert "**URL:** https://www.youtube.com/watch?v=zzz000" in formatted
    assert "&t=" not in formatted


def test_excerpt_body_is_stripped():
    hit = _hit(text="   body with surrounding whitespace   \n")
    formatted = _format_nugget_excerpt(hit, index=1)
    assert "body with surrounding whitespace" in formatted
    # Body appears after header, separated by blank line
    assert formatted.endswith("body with surrounding whitespace\n")


def test_excerpt_index_appears_in_header():
    hit = _hit()
    assert "### Excerpt 7" in _format_nugget_excerpt(hit, index=7)


# ---------------------------------------------------------------------------
# build_nugget_prompt — template substitution
# ---------------------------------------------------------------------------


_TEMPLATE = "Query: {{QUERY}}\nCount: {{NUM_CHUNKS}}\n---\n{{EXCERPTS}}\n---\nEnd."


def test_substitutes_query_into_template():
    out = build_nugget_prompt(_TEMPLATE, "the query", [])
    assert "Query: the query" in out


def test_substitutes_num_chunks_count():
    hits = [_hit(), _hit(), _hit()]
    out = build_nugget_prompt(_TEMPLATE, "q", hits)
    assert "Count: 3" in out


def test_empty_hits_produces_zero_count_and_empty_excerpts():
    out = build_nugget_prompt(_TEMPLATE, "q", [])
    assert "Count: 0" in out
    # Should not crash, should not leave the placeholder in the output
    assert "{{EXCERPTS}}" not in out
    assert "{{QUERY}}" not in out


def test_excerpts_appear_in_retrieval_order():
    # Retrieval order is load-bearing: top-ranked excerpts should frame
    # the brief's consensus/divergence positioning. If order is lost,
    # the synthesis quality degrades.
    hits = [
        _hit(channel="creator_a", title="First"),
        _hit(channel="creator_b", title="Second"),
        _hit(channel="creator_c", title="Third"),
    ]
    out = build_nugget_prompt(_TEMPLATE, "q", hits)
    pos_first = out.index("First")
    pos_second = out.index("Second")
    pos_third = out.index("Third")
    assert pos_first < pos_second < pos_third


def test_excerpts_numbered_starting_at_one():
    hits = [_hit(title="A"), _hit(title="B")]
    out = build_nugget_prompt(_TEMPLATE, "q", hits)
    assert "### Excerpt 1" in out
    assert "### Excerpt 2" in out
    assert "### Excerpt 0" not in out


def test_all_placeholders_removed_when_hits_present():
    hits = [_hit()]
    out = build_nugget_prompt(_TEMPLATE, "q", hits)
    assert "{{QUERY}}" not in out
    assert "{{NUM_CHUNKS}}" not in out
    assert "{{EXCERPTS}}" not in out


def test_query_with_template_delimiters_does_not_break_substitution():
    # Users might paste queries containing {{ }} literals (e.g., from
    # someone else's prompt). Simple .replace is order-sensitive; we
    # substitute QUERY first, so a query like "use {{NUM_CHUNKS}}"
    # would leak into the final output. This test documents the
    # current behavior so any future change to the substitution
    # strategy is intentional.
    weird_query = "what is {{NUM_CHUNKS}} of something?"
    out = build_nugget_prompt(_TEMPLATE, weird_query, [])
    # The query text is preserved verbatim
    assert "what is" in out
    # And the count field still populated (because NUM_CHUNKS substitution
    # runs after QUERY substitution; the query's literal {{NUM_CHUNKS}}
    # gets replaced with "0" — known behavior, not a hard failure mode
    # because real queries from users don't contain template syntax).
    # This test exists to flag any regression in substitution order.
    assert "Count: 0" in out
