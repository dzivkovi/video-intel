"""The public-subset exporter (`scripts/atlas_export.py`, issue #262).

Every test drives the real `main` or the real projection over a synthetic private
atlas whose DENIED rows carry canary strings in every field a row has. The
load-bearing assertion is that no canary byte reaches either output file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import atlas_export as ax
import atlas_render as ar

PUBLIC_CH = "publicchan"
PRIVATE_CH = "CANARY_channel_zeta"
OLD_ID = "old00000001"
PRIVATE_ID = "priv0000001"
LOCAL_ID = "goldcast-ab12cd34"
EXCLUDED_ID = "excl0000001"
PUBLIC_ID = "pub00000001"
CANARIES = [
    PRIVATE_CH,
    "CANARY_title_private",
    "CANARY_heading_private",
    "CANARY_alias_private",
    "CANARY_topic_private",
    "CANARY_label_private",
    "CANARY_wiki_private",
    "CANARY_title_old",
    "CANARY_title_local",
    "CANARY_title_excluded",
    "CANARY_topic_public",
    "CANARY_alias_public",
    "CANARY_other_folder",
    "CANARY_label_unreferenced",
]


def row(vid, title, channel, published, url=None, **over):
    base = {
        "video_id": vid,
        "title": title,
        "channel": channel,
        "channels": [channel, "CANARY_other_folder"],
        "published": published,
        "duration_seconds": 100,
        "url": url if url is not None else f"https://www.youtube.com/watch?v={vid}",
        "transcript_quality": "complete",
        "transcript_flag_count": 0,
        "synopsis": ["Heading one", "Heading two"],
        "deep_links": [
            {"label": "Heading one", "seconds": 5, "url": f"https://www.youtube.com/watch?v={vid}&t=5"},
            {"label": "Heading two", "seconds": 9, "url": "https://www.youtube.com/watch?v=OTHERVIDEO1&t=9"},
        ],
        "concept_ids": ["c.public"],
        "topics": ["CANARY_topic_public"],
        "in_briefings": True,
        "briefing_count": 3,
        "wiki": {"creator": "CANARY_wiki_private", "concepts": ["c.public"]},
        "aliases": ["CANARY_alias_public"],
    }
    base.update(over)
    return base


def private_atlas() -> dict:
    return {
        "schema_version": 1,
        "generator": "atlas_build.py",
        "generator_version": "1",
        "generated_from": "corpus",
        "generated_at": "2026-10-28T00:00:00+00:00",
        "summary": {"channels": {PUBLIC_CH: 3, PRIVATE_CH: 1}, "topics": {"CANARY_topic_private": 1}, "unthreaded": 0},
        "concepts": {
            "c.public": "Public concept",
            "c.private": "CANARY_label_private",
            "c.unref": "CANARY_label_unreferenced",
        },
        "rows": [
            row(OLD_ID, "CANARY_title_old", PUBLIC_CH, "2024-01-01"),
            row(PUBLIC_ID, "A public video & <b>title</b>", PUBLIC_CH, "2026-06-01"),
            row(EXCLUDED_ID, "CANARY_title_excluded", PUBLIC_CH, "2026-07-01"),
            row(
                LOCAL_ID,
                "CANARY_title_local",
                PUBLIC_CH,
                "2026-08-01",
                url=f"https://www.youtube.com/watch?v={LOCAL_ID}",
            ),
            row(
                PRIVATE_ID,
                "CANARY_title_private",
                PRIVATE_CH,
                "2026-09-01",
                synopsis=["CANARY_heading_private"],
                aliases=["CANARY_alias_private"],
                concept_ids=["c.private"],
                topics=["CANARY_topic_private"],
            ),
        ],
    }


POLICY = f"channels:\n  - {PUBLIC_CH}\nsince: 2025-01-01\nexclude_video_ids:\n  - {EXCLUDED_ID}\n"


@pytest.fixture
def corpus(tmp_path: Path):
    private = tmp_path / "corpus" / "_atlas"
    private.mkdir(parents=True)
    (private / "atlas.json").write_text(json.dumps(private_atlas()), encoding="utf-8")
    policy = private / "export-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    return tmp_path / "corpus", private / "atlas.json", policy


def run(corpus, *extra):
    _, atlas, policy = corpus
    return ax.main(["--atlas", str(atlas), "--policy", str(policy), *extra])


class TestCanaries:
    def test_no_canary_byte_reaches_either_output(self, corpus, capsys):
        root, _, _ = corpus
        assert run(corpus) == 0
        out = root / "_atlas-export"
        for name in ("atlas.json", "catalog.html"):
            text = (out / name).read_text(encoding="utf-8")
            for canary in CANARIES:
                assert canary not in text, f"{canary} leaked into {name}"
        line = capsys.readouterr().out
        assert "rows=1 of 5" in line and "dropped_channel=1" in line and "dropped_date=1" in line
        assert "dropped_url=1" in line and "dropped_excluded=1" in line

    def test_public_row_carries_only_the_public_keys(self, corpus):
        root, _, _ = corpus
        assert run(corpus) == 0
        subset = json.loads((root / "_atlas-export" / "atlas.json").read_text(encoding="utf-8"))
        assert subset["generator"] == ax.GENERATOR_NAME
        (r,) = subset["rows"]
        assert tuple(r) == ax.PUBLIC_ROW_KEYS
        assert r["video_id"] == PUBLIC_ID and r["title"] == "A public video & <b>title</b>"
        # the foreign-video deep link is gone; the row's own stays
        assert [dl["seconds"] for dl in r["deep_links"]] == [5]
        assert subset["concepts"] == {"c.public": "Public concept"}
        assert subset["summary"] == {"channels": {PUBLIC_CH: 1}, "topics": {}, "unthreaded": 0, "rows": 1}
        assert subset["export"]["channels"] == [PUBLIC_CH] and subset["export"]["since"] == "2025-01-01"

    def test_the_rendered_page_opens_from_the_subset(self, corpus):
        root, _, _ = corpus
        assert run(corpus) == 0
        page = (root / "_atlas-export" / "catalog.html").read_text(encoding="utf-8")
        assert ar.GENERATOR_META in page and '"generator":"atlas_export.py"' in page


class TestDenyTerms:
    def test_a_deny_term_in_a_kept_row_aborts_before_any_write(self, corpus, tmp_path, capsys):
        root, _, _ = corpus
        terms = tmp_path / "terms.txt"
        terms.write_text("# comment\nshort\nPUBLIC VIDEO\n", encoding="utf-8")
        assert run(corpus, "--deny-terms", str(terms)) == ax.EXIT_REFUSED
        assert not (root / "_atlas-export").exists()
        err = capsys.readouterr().err
        assert "1 deny term(s)" in err and "public video" in err

    def test_terms_only_in_denied_rows_do_not_fire(self, corpus, tmp_path):
        root, _, _ = corpus
        terms = tmp_path / "terms.txt"
        terms.write_text("\n".join(CANARIES) + "\n", encoding="utf-8")
        assert run(corpus, "--deny-terms", str(terms)) == 0
        assert (root / "_atlas-export" / "atlas.json").exists()


class TestPolicy:
    @pytest.mark.parametrize(
        "text, message",
        [
            ("since: 2025-01-01\n", "`channels` must be a non-empty list"),
            ("channels: []\n", "`channels` must be a non-empty list"),
            ("channels: [a, 3]\n", "entries must be non-empty strings"),
            ("channels: [a, a]\n", "repeats an entry"),
            ("channels: [a]\nsince: yesterday\n", "`since` must be a YYYY-MM-DD date"),
            ("channels: [a]\nextra: 1\n", "unknown keys"),
            ("channels: [a]\nchannels: [b]\n", "duplicate policy key"),
            ("channels: [a]\n1: x\n", "keys must be strings"),
            ("channels: [a]\nexclude_video_ids: [nope]\n", "non-id entries"),
            ("- a\n", "must be a mapping"),
        ],
    )
    def test_a_policy_that_cannot_be_trusted_is_refused_and_nothing_is_written(self, corpus, capsys, text, message):
        root, atlas, policy = corpus
        policy.write_text(text, encoding="utf-8")
        assert run(corpus) == ax.EXIT_REFUSED
        assert message in capsys.readouterr().err
        assert not (root / "_atlas-export").exists()

    def test_an_unquoted_date_parses_as_a_date_and_is_accepted(self, corpus):
        assert run(corpus) == 0


class TestOutputGuards:
    def test_exporting_into_the_private_folder_is_refused(self, corpus, capsys):
        _, atlas, _ = corpus
        assert run(corpus, "--out", str(atlas.parent)) == ax.EXIT_REFUSED
        assert "private catalog folder" in capsys.readouterr().err
        assert json.loads(atlas.read_text(encoding="utf-8"))["generator"] == "atlas_build.py"

    def test_a_foreign_atlas_json_in_the_out_folder_is_refused(self, corpus, tmp_path, capsys):
        out = tmp_path / "elsewhere"
        out.mkdir()
        foreign = json.dumps({"generator": "atlas_build.py", "rows": []})
        (out / "atlas.json").write_text(foreign, encoding="utf-8")
        assert run(corpus, "--out", str(out)) == ax.EXIT_REFUSED
        assert (out / "atlas.json").read_text(encoding="utf-8") == foreign
        assert "not written by atlas_export.py" in capsys.readouterr().err

    def test_a_private_atlas_is_required_as_input(self, corpus, capsys):
        root, atlas, _ = corpus
        assert run(corpus) == 0
        exported = root / "_atlas-export" / "atlas.json"
        assert ax.main(["--atlas", str(exported), "--policy", str(corpus[2])]) == ax.EXIT_REFUSED
        assert "not a private atlas" in capsys.readouterr().err

    def test_a_failed_render_removes_the_half_written_bundle(self, corpus, monkeypatch, capsys):
        root, _, _ = corpus
        monkeypatch.setattr(ax.atlas_render, "main", lambda argv: 2)
        assert run(corpus) == ax.EXIT_REFUSED
        out = root / "_atlas-export"
        assert not (out / "atlas.json").exists() and not (out / "catalog.html").exists()
        assert "half-written bundle was removed" in capsys.readouterr().err


class TestDeterminism:
    def test_two_exports_are_byte_identical(self, corpus):
        root, _, _ = corpus
        assert run(corpus) == 0
        first = (root / "_atlas-export" / "atlas.json").read_bytes()
        assert run(corpus) == 0
        assert (root / "_atlas-export" / "atlas.json").read_bytes() == first
