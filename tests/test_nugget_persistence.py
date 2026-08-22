"""Tests for persisting nugget briefs as corpus artifacts (issue #147).

`nugget` synthesis used to be ephemeral - stdout only, re-paid every time the
same question was asked. This suite guards the new persistence contract:

- The brief is written to `_briefings/nuggets/<date>-<query-slug>.md`.
- Front matter uses `cited_video_ids` (NOT `video_ids`) - `load_seen_video_ids`
  only reads `video_ids`, so a nugget's evidence must never be silently
  promoted into the catch-up-briefing "seen" set (issue #80/#88 contract).
- Same-day re-runs of the same query get `-2`, `-3` suffixes, never a clobber
  (mirrors `cmd_briefings`'s convention).
- Every `&t=` deep link from the retrieved hits survives into a Sources
  section (timestamps are data, not decoration).
- `--no-save` skips the write entirely.

The "writer vs checker" test (PR #136 guardrail) computes expected output
paths as hardcoded literals, never by calling `nugget_brief_slug` /
`write_nugget_brief` a second time - so the test cannot silently agree with a
broken writer by construction.
"""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from video_intel import (
    load_seen_video_ids,
    render_nugget_brief_markdown,
    write_nugget_brief,
)


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
# render_nugget_brief_markdown - front matter
# ---------------------------------------------------------------------------


class TestFrontMatter:
    def test_uses_cited_video_ids_not_video_ids(self):
        hits = [_hit(video_id="v1"), _hit(video_id="v2")]
        content = render_nugget_brief_markdown("my query", "The brief body.", hits, today=date(2026, 8, 22))
        fm_block = content.split("---")[1]
        fm = yaml.safe_load(fm_block)
        assert fm["cited_video_ids"] == ["v1", "v2"]
        assert "video_ids" not in fm

    def test_front_matter_has_title_date_query_generator(self):
        hits = [_hit(video_id="v1")]
        content = render_nugget_brief_markdown("forward deployed engineering", "body", hits, today=date(2026, 8, 22))
        fm = yaml.safe_load(content.split("---")[1])
        assert fm["title"] == "Nugget brief - forward deployed engineering"
        assert fm["date"] == "2026-08-22"
        assert fm["query"] == "forward deployed engineering"  # verbatim, not slugified
        assert fm["generator"] == {"name": "nugget", "version": 1}

    def test_cited_video_ids_deduplicated_and_sorted(self):
        hits = [_hit(video_id="v2"), _hit(video_id="v1"), _hit(video_id="v2")]
        content = render_nugget_brief_markdown("q", "body", hits, today=date(2026, 8, 22))
        fm = yaml.safe_load(content.split("---")[1])
        assert fm["cited_video_ids"] == ["v1", "v2"]

    def test_hits_without_video_id_excluded_from_cited_ids(self):
        hits = [_hit(video_id="v1"), _hit(video_id="")]
        content = render_nugget_brief_markdown("q", "body", hits, today=date(2026, 8, 22))
        fm = yaml.safe_load(content.split("---")[1])
        assert fm["cited_video_ids"] == ["v1"]

    def test_no_hits_yields_empty_cited_video_ids(self):
        content = render_nugget_brief_markdown("q", "body", [], today=date(2026, 8, 22))
        fm = yaml.safe_load(content.split("---")[1])
        assert fm["cited_video_ids"] == []


# ---------------------------------------------------------------------------
# render_nugget_brief_markdown - body
# ---------------------------------------------------------------------------


class TestBody:
    def test_body_contains_stdout_brief_verbatim(self):
        body_text = "## Consensus\n- Everyone agreed on X.\n"
        content = render_nugget_brief_markdown("q", body_text, [_hit()], today=date(2026, 8, 22))
        assert "Everyone agreed on X." in content

    def test_sources_section_preserves_t_deep_links(self):
        hits = [_hit(video_id="xyz789", timestamp_seconds=600)]
        content = render_nugget_brief_markdown("q", "body", hits, today=date(2026, 8, 22))
        assert "https://www.youtube.com/watch?v=xyz789&t=600" in content

    def test_sources_section_omits_link_for_hit_with_no_video_id(self):
        hits = [_hit(video_id="")]
        content = render_nugget_brief_markdown("q", "body", hits, today=date(2026, 8, 22))
        assert "## Sources" not in content

    def test_sources_deduplicates_repeated_video_urls(self):
        hits = [_hit(video_id="v1", timestamp_seconds=10), _hit(video_id="v1", timestamp_seconds=10)]
        content = render_nugget_brief_markdown("q", "body", hits, today=date(2026, 8, 22))
        assert content.count("https://www.youtube.com/watch?v=v1&t=10") == 1

    def test_sources_url_omits_t_param_when_timestamp_seconds_zero(self):
        hits = [_hit(video_id="v1", timestamp_seconds=0)]
        content = render_nugget_brief_markdown("q", "body", hits, today=date(2026, 8, 22))
        assert "https://www.youtube.com/watch?v=v1)" in content
        assert "&t=" not in content

    def test_title_brackets_escaped_in_sources(self):
        hits = [_hit(video_id="v1", title="free tips ](evil)")]
        content = render_nugget_brief_markdown("q", "body", hits, today=date(2026, 8, 22))
        assert "\\[evil\\)" not in content  # sanity: only brackets are escaped
        assert "free tips \\]" in content


# ---------------------------------------------------------------------------
# write_nugget_brief - filesystem behavior
# ---------------------------------------------------------------------------


class TestWriteNuggetBrief:
    def test_writes_under_briefings_nuggets_dir(self, tmp_path):
        out_dir = tmp_path / "corpus"
        path = write_nugget_brief(out_dir, "forward deployed engineering", "body", [_hit()], today=date(2026, 8, 22))
        # Expected path is a hardcoded literal (PR #136 guardrail): never
        # derived by calling nugget_brief_slug/write_nugget_brief again, so
        # this test can't agree with a broken writer by construction.
        expected = out_dir / "_briefings" / "nuggets" / "2026-08-22-forward-deployed-engineering.md"
        assert path == expected
        assert path.exists()
        assert path.read_text(encoding="utf-8").startswith("---")

    def test_same_day_rerun_gets_dash2_suffix_never_clobbers(self, tmp_path):
        out_dir = tmp_path / "corpus"
        p1 = write_nugget_brief(out_dir, "same query", "first brief", [_hit(video_id="v1")], today=date(2026, 8, 22))
        p2 = write_nugget_brief(out_dir, "same query", "second brief", [_hit(video_id="v2")], today=date(2026, 8, 22))
        expected1 = out_dir / "_briefings" / "nuggets" / "2026-08-22-same-query.md"
        expected2 = out_dir / "_briefings" / "nuggets" / "2026-08-22-same-query-2.md"
        assert p1 == expected1
        assert p2 == expected2
        assert p1.exists() and p2.exists()
        assert "first brief" in p1.read_text(encoding="utf-8")
        assert "second brief" in p2.read_text(encoding="utf-8")  # p1 untouched, not clobbered

    def test_third_same_day_rerun_gets_dash3_suffix(self, tmp_path):
        out_dir = tmp_path / "corpus"
        write_nugget_brief(out_dir, "q", "b1", [_hit()], today=date(2026, 8, 22))
        write_nugget_brief(out_dir, "q", "b2", [_hit()], today=date(2026, 8, 22))
        p3 = write_nugget_brief(out_dir, "q", "b3", [_hit()], today=date(2026, 8, 22))
        assert p3 == out_dir / "_briefings" / "nuggets" / "2026-08-22-q-3.md"

    def test_different_day_reuses_the_base_name(self, tmp_path):
        out_dir = tmp_path / "corpus"
        write_nugget_brief(out_dir, "q", "b1", [_hit()], today=date(2026, 8, 22))
        p2 = write_nugget_brief(out_dir, "q", "b2", [_hit()], today=date(2026, 8, 23))
        assert p2 == out_dir / "_briefings" / "nuggets" / "2026-08-23-q.md"


# ---------------------------------------------------------------------------
# load_seen_video_ids must be unaffected by nugget artifacts (issue #80/#88)
# ---------------------------------------------------------------------------


class TestSeenSetUnaffectedByNuggets:
    def test_load_seen_video_ids_ignores_cited_video_ids(self, tmp_path):
        out_dir = tmp_path / "corpus"
        write_nugget_brief(
            out_dir,
            "forward deployed engineering",
            "brief citing evidence",
            [_hit(video_id="cited-only-1"), _hit(video_id="cited-only-2")],
            today=date(2026, 8, 22),
        )
        briefings_dir = out_dir / "_briefings"
        assert load_seen_video_ids(briefings_dir) == set()

    def test_real_briefing_video_ids_still_counted_alongside_nugget_files(self, tmp_path):
        out_dir = tmp_path / "corpus"
        briefings_dir = out_dir / "_briefings"
        briefings_dir.mkdir(parents=True)
        front = yaml.safe_dump({"artifact_type": "viewing_guide", "video_ids": ["v1", "v2"]})
        (briefings_dir / "2026-08-20-catch-up-unseen.md").write_text(f"---\n{front}---\n\n# guide\n", encoding="utf-8")
        write_nugget_brief(out_dir, "q", "body", [_hit(video_id="v3")], today=date(2026, 8, 22))

        # v3 is only ever cited, never curated into a briefing - must stay unseen.
        assert load_seen_video_ids(briefings_dir) == {"v1", "v2"}


# ---------------------------------------------------------------------------
# cmd_nugget integration - persistence wired into the command
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, text):
        self._text = text

    def generate_content(self, **kwargs):
        return _FakeResponse(self._text)


class _FakeClient:
    def __init__(self, text):
        self.models = _FakeModels(text)


@pytest.fixture
def nugget_env(tmp_path, monkeypatch):
    import video_intel as vi

    output_dir = tmp_path / "corpus"
    output_dir.mkdir()
    config = {"output_dir": str(output_dir)}

    hits = [
        _hit(video_id="v1", channel="creatorA", timestamp_seconds=120, relevance=0.9),
        _hit(video_id="v2", channel="creatorB", timestamp_seconds=300, relevance=0.8),
    ]
    monkeypatch.setattr(vi, "hybrid_search", lambda *a, **k: hits)
    monkeypatch.setattr(vi, "create_client", lambda *a, **k: _FakeClient("## Consensus\n- They agree.\n"))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
    return vi, config, output_dir


def _args(**overrides):
    from types import SimpleNamespace

    base = dict(
        query="forward deployed engineering",
        channel=None,
        limit=15,
        since=None,
        min_relevance=0.0,
        no_expand=False,
        output=None,
        no_save=False,
        model=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestCmdNuggetPersistence:
    def test_default_run_persists_and_prints_brief_unchanged(self, nugget_env, capsys):
        vi, config, output_dir = nugget_env
        vi.cmd_nugget(_args(), config)

        out = capsys.readouterr().out
        assert "They agree." in out  # stdout brief unchanged

        nuggets_dir = output_dir / "_briefings" / "nuggets"
        files = list(nuggets_dir.glob("*.md"))
        assert len(files) == 1
        fm = yaml.safe_load(files[0].read_text(encoding="utf-8").split("---")[1])
        assert fm["cited_video_ids"] == ["v1", "v2"]
        assert fm["query"] == "forward deployed engineering"

    def test_no_save_writes_nothing(self, nugget_env, capsys):
        vi, config, output_dir = nugget_env
        vi.cmd_nugget(_args(no_save=True), config)

        out = capsys.readouterr().out
        assert "They agree." in out  # stdout still unchanged
        nuggets_dir = output_dir / "_briefings" / "nuggets"
        assert not nuggets_dir.exists() or not list(nuggets_dir.glob("*.md"))

    def test_two_runs_same_day_do_not_clobber(self, nugget_env):
        vi, config, output_dir = nugget_env
        vi.cmd_nugget(_args(), config)
        vi.cmd_nugget(_args(), config)

        nuggets_dir = output_dir / "_briefings" / "nuggets"
        files = sorted(p.name for p in nuggets_dir.glob("*.md"))
        assert len(files) == 2
        assert any(name.endswith("-2.md") for name in files)
