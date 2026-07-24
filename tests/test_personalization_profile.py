"""Tests for the first-class personalization profile (issue #115).

Two files are authored by the user - `_briefings/audience.md` (prose, hand-written)
and `_briefings/profile.yaml` (machine ranking weights) - and ONE compiled interest
model is what both ranking surfaces read: `rank_unseen` (briefings, concept
evidence) and `rank_headlines` (headline digest, title/metadata evidence). The
contract guarded here: a single profile edit reorders BOTH surfaces, hand-edited
files are never overwritten, `profile show` writes nothing, and personalization
reorders rather than deletes.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
import yaml

TAXONOMY = {
    "concepts": {
        "ai-engineering.agents": {
            "preferred_label": "agents",
            "aliases": ["agentic workflows"],
            "domain": "ai-engineering",
            "video_count": 9,
        },
        "business.pricing": {
            "preferred_label": "pricing",
            "aliases": [],
            "domain": "business",
            "video_count": 4,
        },
    }
}


def _corpus(tmp_path):
    """A tiny corpus: taxonomy + two concept-extracted videos, one per concept."""
    (tmp_path / "taxonomy.json").write_text(json.dumps(TAXONOMY), encoding="utf-8")
    channel = tmp_path / "natebjones"
    channel.mkdir(parents=True, exist_ok=True)
    for prefix, video_id, cid, label in (
        ("2026-06-20-a", "vid-agents", "ai-engineering.agents", "agents"),
        ("2026-06-19-b", "vid-pricing", "business.pricing", "pricing"),
    ):
        (channel / f"{prefix}.meta.json").write_text(
            json.dumps(
                {
                    "video_id": video_id,
                    "channel": "natebjones",
                    "title": prefix,
                    "published": prefix[:10],
                    "video_url": f"https://www.youtube.com/watch?v={video_id}",
                }
            ),
            encoding="utf-8",
        )
        (channel / f"{prefix}.concepts.json").write_text(
            json.dumps(
                {
                    "video_id": video_id,
                    "concepts": [{"concept_id": cid, "preferred_label": label, "domain": cid.split(".")[0]}],
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


def _snapshot(root):
    """Path -> (size, mtime_ns) for every file under `root`, for write assertions."""
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*")) if p.is_file()}


def _profile(agents_weight, pricing_weight):
    return {
        "id": "test-profile",
        "interest_concepts": {
            "ai-engineering.agents": agents_weight,
            "business.pricing": pricing_weight,
        },
        "interest_domains": [],
    }


# --------------------------------------------------------------------------
# The compiled interest model - one model, both rankers
# --------------------------------------------------------------------------
def test_one_profile_edit_reorders_both_surfaces(tmp_path):
    """The load-bearing invariant: a single weight flip must reorder `rank_unseen`
    AND `rank_headlines`. If the two surfaces ever load interest data through
    different paths, one of these orders stops moving."""
    import video_intel as vi

    _corpus(tmp_path)
    unseen = vi.collect_corpus_videos(tmp_path)
    headlines = [
        {"video_id": "h1", "title": "Everything new in agents this week", "published": "2026-07-01"},
        {"video_id": "h2", "title": "How I fixed my pricing", "published": "2026-07-02"},
    ]

    def order(agents_weight, pricing_weight):
        model = vi.compile_interest_model(_profile(agents_weight, pricing_weight), TAXONOMY)
        corpus_order = [v["video_id"] for v in vi.rank_unseen(unseen, model)]
        headline_order = [v["video_id"] for v in vi.rank_headlines(headlines, model)]
        return corpus_order, headline_order

    agents_first = order(10, 1)
    pricing_first = order(1, 10)

    assert agents_first == (["vid-agents", "vid-pricing"], ["h1", "h2"])
    assert pricing_first == (["vid-pricing", "vid-agents"], ["h2", "h1"])


def test_rank_headlines_scores_metadata_only_items_via_compiled_model(tmp_path):
    """Headline videos have no concepts.json; the model's label phrases + domains
    are the only evidence available, and they must not all score zero."""
    import video_intel as vi

    profile = {"interest_concepts": {"ai-engineering.agents": 3}, "interest_domains": ["business"]}
    model = vi.compile_interest_model(profile, TAXONOMY)
    ranked = vi.rank_headlines(
        [
            {"video_id": "h1", "title": "Agentic workflows in production", "published": "2026-07-01"},
            {"video_id": "h2", "title": "The business of shipping software", "published": "2026-07-02"},
            {"video_id": "h3", "title": "A totally unrelated vlog", "published": "2026-07-03"},
        ],
        model,
    )
    scores = {v["video_id"]: v["score"] for v in ranked}
    assert scores["h1"] == 3  # alias match against the taxonomy
    assert scores["h2"] == vi.HEADLINE_DOMAIN_MATCH_WEIGHT  # domain match, weaker
    assert scores["h3"] == 0


def test_rankers_still_accept_a_raw_profile_dict(tmp_path):
    """Back-compat: callers that pass the raw profile dict get it compiled for them,
    so there is still exactly one compiler and no second interpretation of weights."""
    import video_intel as vi

    _corpus(tmp_path)
    unseen = vi.collect_corpus_videos(tmp_path)
    raw = _profile(10, 1)

    from_dict = [v["video_id"] for v in vi.rank_unseen(unseen, raw)]
    from_model = [v["video_id"] for v in vi.rank_unseen(unseen, vi.compile_interest_model(raw, TAXONOMY))]
    assert from_dict == from_model


def test_both_consumers_load_through_the_shared_loader(tmp_path, monkeypatch):
    """Neither surface may load interest data through any other path."""
    import video_intel as vi

    _corpus(tmp_path)
    calls: list[str] = []
    real = vi.load_interest_model

    def spy(output_dir, config=None, **kwargs):
        calls.append("load")
        return real(output_dir, config, **kwargs)

    monkeypatch.setattr(vi, "load_interest_model", spy)

    vi.cmd_briefings(
        SimpleNamespace(unseen=True, dry_run=True, since=None, until=None, limit=30, pdf=False),
        {"output_dir": str(tmp_path)},
    )
    assert calls == ["load"]

    monkeypatch.setattr(vi, "collect_headline_channels", lambda _cfg: [])
    vi.render_headline_digest(object(), {"channels": []}, tmp_path, dry_run=True)
    # No eligible channels -> the digest returns before loading; the assertion that
    # matters is the briefings call above plus the digest path below.
    monkeypatch.setattr(
        vi,
        "collect_headline_channels",
        lambda _cfg: [{"name": "x", "url": "https://www.youtube.com/@x"}],
    )
    monkeypatch.setattr(vi, "get_channel_id", lambda *_a, **_kw: ("UC1", "X"))
    monkeypatch.setattr(vi, "fetch_channel_videos", lambda *_a, **_kw: [])
    vi.render_headline_digest(object(), {"channels": []}, tmp_path, dry_run=True)
    assert calls == ["load", "load"]


# --------------------------------------------------------------------------
# Serendipity floor - personalization reorders, never deletes
# --------------------------------------------------------------------------
def test_zero_score_items_are_ranked_last_but_kept_on_both_surfaces(tmp_path):
    import video_intel as vi

    _corpus(tmp_path)
    model = vi.load_interest_model(tmp_path, {"channels": []}, today=date(2026, 7, 1))

    unseen = vi.collect_corpus_videos(tmp_path)
    unseen.append(
        {
            "video_id": "no-concepts",
            "title": "Unrelated",
            "published": "2026-06-01",
            "channel": "natebjones",
            "url": "https://www.youtube.com/watch?v=no-concepts",
            "mindmap_path": None,
            "concepts_path": None,
        }
    )
    ranked = vi.rank_unseen(unseen, model)
    assert len(ranked) == 3  # nothing dropped
    assert ranked[-1]["video_id"] == "no-concepts"

    headlines = vi.rank_headlines(
        [
            {"video_id": "h-zero", "title": "Unrelated vlog", "published": "2026-07-01"},
            {"video_id": "h-hit", "title": "agents everywhere", "published": "2026-06-01"},
        ],
        model,
    )
    assert {v["video_id"] for v in headlines} == {"h-zero", "h-hit"}
    positive, zero = vi._select_headline_items(headlines)
    assert [v["video_id"] for v in positive + zero] == ["h-hit", "h-zero"]


# --------------------------------------------------------------------------
# profile show - reports the resolved model, writes nothing
# --------------------------------------------------------------------------
def test_profile_show_reports_inferred_source_and_writes_nothing(tmp_path, capsys):
    import video_intel as vi

    _corpus(tmp_path)
    before = _snapshot(tmp_path)

    vi.cmd_profile(SimpleNamespace(profile_action="show"), {"output_dir": str(tmp_path)})

    out = capsys.readouterr().out
    assert "inferred" in out.lower()
    assert "profile.yaml" in out
    assert "audience.md" in out
    assert _snapshot(tmp_path) == before  # zero filesystem writes
    assert not (tmp_path / "_briefings").exists()


def test_profile_show_reports_persisted_source(tmp_path, capsys):
    import video_intel as vi

    _corpus(tmp_path)
    briefings = tmp_path / "_briefings"
    briefings.mkdir()
    (briefings / "profile.yaml").write_text(
        yaml.safe_dump({"id": "hand-tuned", "interest_concepts": {"ai-engineering.agents": 42}}),
        encoding="utf-8",
    )
    before = _snapshot(tmp_path)

    vi.cmd_profile(SimpleNamespace(profile_action="show"), {"output_dir": str(tmp_path)})

    out = capsys.readouterr().out
    assert "persisted" in out.lower()
    assert "hand-tuned" in out
    assert "agents" in out
    assert _snapshot(tmp_path) == before


# --------------------------------------------------------------------------
# profile init - persists once, never overwrites
# --------------------------------------------------------------------------
def test_profile_init_persists_inferred_profile_and_scaffolds_audience(tmp_path, capsys):
    import video_intel as vi

    _corpus(tmp_path)
    vi.cmd_profile(SimpleNamespace(profile_action="init"), {"output_dir": str(tmp_path)})

    profile_path = tmp_path / "_briefings" / "profile.yaml"
    audience_path = tmp_path / "_briefings" / "audience.md"
    assert profile_path.exists()
    data = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
    assert data["interest_concepts"]["ai-engineering.agents"] == 9
    assert audience_path.exists()
    assert "Standing pillars" in audience_path.read_text(encoding="utf-8")
    assert "profile.yaml" in capsys.readouterr().out


@pytest.mark.parametrize(
    "existing",
    [
        "id: hand\ninterest_concepts:\n  my.custom: 99\n",  # valid
        "id: hand\n",  # partial - no interests
        "",  # empty
        "interest_concepts: [oops\n",  # malformed YAML
    ],
    ids=["valid", "partial", "empty", "malformed"],
)
def test_profile_init_never_overwrites_an_existing_profile(tmp_path, existing, capsys):
    """A broken file is still the user's file - hand-editing is the retune path."""
    import video_intel as vi

    _corpus(tmp_path)
    briefings = tmp_path / "_briefings"
    briefings.mkdir()
    profile_path = briefings / "profile.yaml"
    profile_path.write_text(existing, encoding="utf-8")

    vi.cmd_profile(SimpleNamespace(profile_action="init"), {"output_dir": str(tmp_path)})

    assert profile_path.read_text(encoding="utf-8") == existing
    assert "kept" in capsys.readouterr().out.lower()


def test_profile_init_never_overwrites_an_existing_audience_file(tmp_path):
    import video_intel as vi

    _corpus(tmp_path)
    briefings = tmp_path / "_briefings"
    briefings.mkdir()
    audience_path = briefings / "audience.md"
    audience_path.write_text("# my own notes\n", encoding="utf-8")

    vi.cmd_profile(SimpleNamespace(profile_action="init"), {"output_dir": str(tmp_path)})

    assert audience_path.read_text(encoding="utf-8") == "# my own notes\n"


def test_profile_init_is_idempotent(tmp_path):
    import video_intel as vi

    _corpus(tmp_path)
    args = SimpleNamespace(profile_action="init")
    vi.cmd_profile(args, {"output_dir": str(tmp_path)})
    first = _snapshot(tmp_path / "_briefings")
    vi.cmd_profile(args, {"output_dir": str(tmp_path)})
    assert _snapshot(tmp_path / "_briefings") == first


# --------------------------------------------------------------------------
# briefings stays read-only about the profile; `profile init` is the write surface
# --------------------------------------------------------------------------
def test_briefings_without_persisted_profile_ranks_but_persists_nothing(tmp_path):
    import video_intel as vi

    _corpus(tmp_path)
    vi.cmd_briefings(
        SimpleNamespace(unseen=True, dry_run=False, since=None, until=None, limit=30, pdf=False),
        {"output_dir": str(tmp_path)},
    )

    briefings = tmp_path / "_briefings"
    written = list(briefings.glob("*-catch-up-unseen.md"))
    assert len(written) == 1  # the briefing itself is still produced...
    assert not (briefings / "profile.yaml").exists()  # ...but the profile is not persisted
    body = written[0].read_text(encoding="utf-8")
    assert "vid-agents" in body or "2026-06-20-a" in body


# --------------------------------------------------------------------------
# Portability - both files resolve under output_dir, not a machine-specific path
# --------------------------------------------------------------------------
def test_profile_paths_resolve_under_output_dir(tmp_path):
    import video_intel as vi

    corpus = tmp_path / "elsewhere" / "corpus"
    corpus.mkdir(parents=True)
    _corpus(corpus)

    model = vi.load_interest_model(corpus, {"channels": []}, today=date(2026, 7, 1))
    assert model.profile_path == corpus / "_briefings" / "profile.yaml"
    assert model.audience_path == corpus / "_briefings" / "audience.md"


def test_profile_init_writes_under_overridden_output_dir(tmp_path):
    import video_intel as vi

    corpus = tmp_path / "elsewhere" / "corpus"
    corpus.mkdir(parents=True)
    _corpus(corpus)

    vi.cmd_profile(SimpleNamespace(profile_action="init"), {"output_dir": str(corpus)})

    assert (corpus / "_briefings" / "profile.yaml").exists()
    assert (corpus / "_briefings" / "audience.md").exists()


# --------------------------------------------------------------------------
# Robustness - a hand-edited or broken corpus must not crash the compiler
# --------------------------------------------------------------------------
def test_compiler_tolerates_hand_edited_shapes():
    import video_intel as vi

    model = vi.compile_interest_model(
        {
            "interest_concepts": ["not", "a", "mapping"],
            "interest_domains": "business",
        },
        TAXONOMY,
    )
    assert model.weights == {}
    assert model.domains == frozenset({"business"})
    assert vi.rank_unseen([], model) == []
    assert [v["score"] for v in vi.rank_headlines([{"title": "business talk", "published": ""}], model)] == [
        vi.HEADLINE_DOMAIN_MATCH_WEIGHT
    ]


def test_load_interest_model_tolerates_unreadable_taxonomy(tmp_path):
    import video_intel as vi

    (tmp_path / "taxonomy.json").write_text("{not json", encoding="utf-8")
    model = vi.load_interest_model(tmp_path, {"channels": []}, today=date(2026, 7, 1))
    assert model.source == "inferred"
    assert model.weights == {}
