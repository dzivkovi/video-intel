"""`concepts` must write on the ON-DISK stem, not a recomputed title slug.

`process_concepts` derives its filename from `video_file_prefix(video)` unless a
`prefix=` is supplied. That recomputes `{date}-{slug(title)}`, which matches the
filename stem for scan-created artifacts and diverges for every locally-recovered
one - where the stem comes from the source file or a sibling meta instead.

When they diverge, `cmd_concepts` wrote the concepts file (and a stub meta) under
a prefix none of the video's other artifacts use. Observed in a real corpus:
`2026-04-22-claude-design-system-prompt-leak-tips.concepts.json` orphaned beside
the genuine, complete `Claude Design System Prompt Leak + Tips.*` set, plus a
124-byte meta carrying no `video_id` at all.

Note what this bug did NOT do, because assuming otherwise overstates it: there is
no repeated Gemini spend. `cmd_concepts` re-selects the video every run (its skip
check reads the stem and never sees the drifted file), but `process_concepts`
then short-circuits on its own computed prefix. The cost is corpus consistency.

Issue #144. `cmd_scan`'s concepts loop already threaded `prefix=` through; this
is the same contract applied to the standalone subcommand.
"""

from __future__ import annotations

import argparse
import json

import pytest

import video_intel as v

MINDMAP = "## Topic\n\n* **Thing**\n  - a point (0:00)\n"
DRIFTING_TITLE = "Andrew Ng AI stack masterclass (unverified rip)"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    out = tmp_path / "corpus"
    (out / "mychan").mkdir(parents=True)
    monkeypatch.setattr(v, "resolve_output_dir", lambda cfg, **kw: out)
    monkeypatch.setattr(v, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(v, "create_client", lambda *a, **k: object())
    monkeypatch.setattr(v, "load_taxonomy", lambda *a, **k: {"concepts": {}})
    monkeypatch.setattr(v, "load_prompt", lambda n: "PROMPT {{taxonomy}}")
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    return out / "mychan"


def _seed(channel_dir, stem, *, title, published="2026-08-10"):
    (channel_dir / f"{stem}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    (channel_dir / f"{stem}.meta.json").write_text(
        json.dumps(
            {
                "video_id": "abcDEF12345",
                "video_url": "https://youtu.be/abcDEF12345",
                "title": title,
                "published": published,
                "channel": "mychan",
            }
        ),
        encoding="utf-8",
    )


def _cfg(out):
    return {"output_dir": str(out), "channels": [{"name": "mychan", "url": "https://youtube.com/@m"}]}


def _args(**kw):
    base = {"channel": "mychan", "force": False, "model": None, "dry_run": False, "log_level": "info"}
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def stub_gemini(monkeypatch):
    calls = []

    def fake(client, types, text, model, **k):
        calls.append(text)
        return json.dumps({"concepts": [{"concept_id": "c1", "preferred_label": "Thing"}]})

    monkeypatch.setattr(v, "call_gemini_text", fake)
    return calls


class TestWritesOnTheOnDiskStem:
    def test_drifting_stem_still_gets_its_own_concepts_file(self, corpus, stub_gemini):
        """The regression: stem and recomputed prefix disagree."""
        _seed(corpus, "AngrewNG", title=DRIFTING_TITLE)
        recomputed = v.video_file_prefix(
            {"title": DRIFTING_TITLE, "published": "2026-08-10", "video_id": "abcDEF12345"}
        )
        assert recomputed != "AngrewNG", "fixture no longer exercises the drift it exists to test"

        v.cmd_concepts(_args(), _cfg(corpus.parent))

        assert (corpus / "AngrewNG.concepts.json").exists(), (
            "concepts did not land on the on-disk stem; it is orphaned from the "
            "mindmap/transcript/meta that share that stem"
        )
        assert not (corpus / f"{recomputed}.concepts.json").exists(), (
            f"concepts written under the recomputed prefix {recomputed!r} instead"
        )

    def test_no_stub_meta_is_created_under_the_drifted_prefix(self, corpus, stub_gemini):
        """The orphan meta is the more damaging half: it carries no video_id, so
        `_load_video_id_index` skips it and dedupe cannot see it either."""
        _seed(corpus, "AngrewNG", title=DRIFTING_TITLE)
        recomputed = v.video_file_prefix(
            {"title": DRIFTING_TITLE, "published": "2026-08-10", "video_id": "abcDEF12345"}
        )
        v.cmd_concepts(_args(), _cfg(corpus.parent))
        assert not (corpus / f"{recomputed}.meta.json").exists()
        metas = sorted(p.name for p in corpus.glob("*.meta.json"))
        assert metas == ["AngrewNG.meta.json"], f"unexpected meta files: {metas}"

    def test_normal_scan_shaped_stem_is_unaffected(self, corpus, stub_gemini):
        """The common case, where stem and recomputed prefix coincide."""
        title = "A Normal Video"
        stem = v.video_file_prefix({"title": title, "published": "2026-08-10", "video_id": "abcDEF12345"})
        _seed(corpus, stem, title=title)
        v.cmd_concepts(_args(), _cfg(corpus.parent))
        assert (corpus / f"{stem}.concepts.json").exists()
        assert sorted(p.name for p in corpus.glob("*.concepts.json")) == [f"{stem}.concepts.json"]


class TestLoopPlumbingStillHolds:
    """The work tuple gained a field; both consumers must unpack it."""

    def test_dry_run_does_not_crash_and_writes_nothing(self, corpus, stub_gemini):
        _seed(corpus, "AngrewNG", title=DRIFTING_TITLE)
        v.cmd_concepts(_args(dry_run=True), _cfg(corpus.parent))
        assert not list(corpus.glob("*.concepts.json")), "dry run wrote an artifact"
        assert not stub_gemini, "dry run called Gemini"

    def test_already_complete_channel_is_skipped(self, corpus, stub_gemini):
        _seed(corpus, "AngrewNG", title=DRIFTING_TITLE)
        (corpus / "AngrewNG.concepts.json").write_text('{"concepts": []}', encoding="utf-8")
        v.cmd_concepts(_args(), _cfg(corpus.parent))
        assert not stub_gemini, "re-ran a video whose concepts already exist on its stem"

    def test_second_run_is_a_no_op(self, corpus, stub_gemini):
        """After the fix the skip check and the writer agree, so a re-run is free."""
        _seed(corpus, "AngrewNG", title=DRIFTING_TITLE)
        v.cmd_concepts(_args(), _cfg(corpus.parent))
        first = len(stub_gemini)
        v.cmd_concepts(_args(), _cfg(corpus.parent))
        assert len(stub_gemini) == first, "second run re-called Gemini for the same video"
