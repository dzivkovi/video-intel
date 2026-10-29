"""The public-subset exporter (`scripts/atlas_export.py`, issue #262).

Every test drives the real `main` or the real projection over a synthetic private
atlas whose DENIED rows carry canary strings in every field a row has. The
load-bearing assertion is that no canary byte reaches either output file. The one
boundary stubbed is the YouTube status request (`_fetch_status_batch`); by default
the stub reports every id public, and a test overrides it to report otherwise.
"""

from __future__ import annotations

import hashlib
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
PRIVATE_ID = "CANARYID001"
LOCAL_ID = "goldcast-ab12cd34"
EXCLUDED_ID = "excl0000001"
PUBLIC_ID = "pub00000001"
UNLISTED_ID = "unl00000001"
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
    "CANARY_title_unlisted",
    "CANARY_topic_public",
    "CANARY_alias_public",
    "CANARY_other_folder",
    "CANARY_label_unreferenced",
    "CANARY_generated_from",
    "CANARY_generated_at",
    "CANARY_dl_private",
    OLD_ID,
    PRIVATE_ID,
    LOCAL_ID,
    EXCLUDED_ID,
    UNLISTED_ID,
    "1999-09-09",
    "777777",
    "7777",
    "9998",
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
        "generated_from": "CANARY_generated_from",
        "generated_at": "CANARY_generated_at",
        "summary": {"channels": {PUBLIC_CH: 4, PRIVATE_CH: 1}, "topics": {"CANARY_topic_private": 1}, "unthreaded": 0},
        "concepts": {
            "c.public": "Public concept",
            "c.private": "CANARY_label_private",
            "c.unref": "CANARY_label_unreferenced",
        },
        "rows": [
            row(OLD_ID, "CANARY_title_old", PUBLIC_CH, "2024-01-01"),
            row(PUBLIC_ID, "A public video & <b>title</b>", PUBLIC_CH, "2026-06-01"),
            row(UNLISTED_ID, "CANARY_title_unlisted", PUBLIC_CH, "2026-06-15"),
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
                "1999-09-09",
                duration_seconds=777777,
                transcript_quality="severe",
                transcript_flag_count=9998,
                synopsis=["CANARY_heading_private"],
                deep_links=[
                    {
                        "label": "CANARY_dl_private",
                        "seconds": 7777,
                        "url": f"https://www.youtube.com/watch?v={PRIVATE_ID}&t=7777",
                    }
                ],
                aliases=["CANARY_alias_private"],
                concept_ids=["c.private"],
                topics=["CANARY_topic_private"],
            ),
        ],
    }


POLICY = f"channels:\n  - {PUBLIC_CH}\nsince: 2025-01-01\nexclude_video_ids:\n  - {EXCLUDED_ID}\n"


@pytest.fixture(autouse=True)
def youtube_status(monkeypatch):
    """Stub the one network boundary: every id is public except the ones a test marks."""
    calls: list[str] = []
    state = {"unlisted": {UNLISTED_ID}, "missing": set()}

    def fake(url: str) -> dict:
        calls.append(url)
        ids = url.split("id=")[1].split("&")[0].split("%2C")
        items = []
        for vid in ids:
            if vid in state["missing"]:
                continue
            items.append({"id": vid, "status": {"privacyStatus": "unlisted" if vid in state["unlisted"] else "public"}})
        return {"items": items}

    monkeypatch.setattr(ax, "_fetch_status_batch", fake)
    monkeypatch.setenv(ax.YOUTUBE_API_KEY_ENV, "test-key")
    state["calls"] = calls
    return state


@pytest.fixture
def corpus(tmp_path: Path):
    private = tmp_path / "corpus" / "_atlas"
    private.mkdir(parents=True)
    (private / "atlas.json").write_text(json.dumps(private_atlas()), encoding="utf-8")
    policy = private / "export-policy.yaml"
    policy.write_text(POLICY, encoding="utf-8")
    (tmp_path / "corpus" / "no-terms.txt").write_text("", encoding="utf-8")
    return tmp_path / "corpus", private / "atlas.json", policy


def run(corpus, *extra):
    root, atlas, policy = corpus
    argv = ["--atlas", str(atlas), "--policy", str(policy)]
    if "--deny-terms" not in extra:
        argv += ["--deny-terms", str(root / "no-terms.txt")]
    return ax.main(argv + list(extra))


def exported(corpus) -> dict:
    root, _, _ = corpus
    return json.loads((root / "_atlas-export" / "atlas.json").read_text(encoding="utf-8"))


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
        assert "rows=1 " in line and "dropped_channel=1" in line and "dropped_date=1" in line
        assert "dropped_url=1" in line and "dropped_excluded=1" in line and "dropped_visibility=1" in line
        assert "3369" not in text and "of 6" not in line

    def test_public_row_carries_only_the_public_keys(self, corpus):
        assert run(corpus) == 0
        subset = exported(corpus)
        assert subset["generator"] == ax.GENERATOR_NAME
        (r,) = subset["rows"]
        assert tuple(r) == ax.PUBLIC_ROW_KEYS
        assert r["video_id"] == PUBLIC_ID and r["title"] == "A public video & <b>title</b>"
        assert [dl["seconds"] for dl in r["deep_links"]] == [5]
        assert subset["concepts"] == {"c.public": "Public concept"}
        assert subset["summary"] == {"channels": {PUBLIC_CH: 1}, "topics": {}, "unthreaded": 0, "rows": 1}
        assert subset["generated_from"] == "public subset" and subset["generated_at"] == "2026-06-01"
        assert subset["export"] == {
            "policy_sha256_16": hashlib.sha256(corpus[2].read_bytes()).hexdigest()[:16],
            "channels": [PUBLIC_CH],
            "since": "2025-01-01",
        }

    def test_the_fingerprint_follows_the_policy_bytes(self, corpus):
        _, _, policy = corpus
        assert run(corpus) == 0
        first = exported(corpus)["export"]["policy_sha256_16"]
        policy.write_text(POLICY + "# a comment changes the bytes\n", encoding="utf-8")
        assert run(corpus) == 0
        assert exported(corpus)["export"]["policy_sha256_16"] != first

    def test_the_rendered_page_opens_from_the_subset(self, corpus):
        root, _, _ = corpus
        assert run(corpus) == 0
        page = (root / "_atlas-export" / "catalog.html").read_text(encoding="utf-8")
        assert ar.GENERATOR_META in page and '"generator":"atlas_export.py"' in page


class TestCanonicalUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://www.youtube.com/watch?v=pub00000001",
            "https://youtu.be/pub00000001",
            "https://m.youtube.com/watch?v=pub00000001",
            "https://www.youtube.com/watch?v=pub00000001&list=CANARY_list",
            "https://www.youtube.com/watch?v=pub00000001&t=5",
            "https://example.com/watch?v=pub00000001",
            "https://www.youtube.com/watch?v=OTHERVIDEO1",
            "",
            None,
            42,
        ],
    )
    def test_anything_but_the_canonical_watch_url_is_dropped(self, corpus, capsys, url):
        root, atlas, _ = corpus
        doc = private_atlas()
        doc["rows"][1]["url"] = url
        doc["rows"][1]["title"] = "CANARY_title_badurl"
        atlas.write_text(json.dumps(doc), encoding="utf-8")
        assert run(corpus) == ax.EXIT_REFUSED  # the only public row is gone, so the channel has no video
        assert "no exportable video" in capsys.readouterr().err
        assert not (root / "_atlas-export").exists()

    @pytest.mark.parametrize("vid", ["short", "", None, "twelve-chars1", "pub00000001\n"])
    def test_a_non_id_shaped_video_id_is_dropped(self, vid):
        assert ax.canonical_url({"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}"}) is None

    def test_deep_links_keep_only_the_rows_own_url_plus_seconds(self):
        url = "https://www.youtube.com/watch?v=pub00000001"
        links = [
            {"label": "ok", "seconds": 5, "url": f"{url}&t=5"},
            {"label": "float", "seconds": 5.0, "url": f"{url}&t=5"},
            {"label": "bool", "seconds": True, "url": f"{url}&t=True"},
            {"label": 7, "seconds": 7, "url": f"{url}&t=7"},
            {"label": "extra", "seconds": 8, "url": f"{url}&t=8&list=x"},
            {"label": "other", "seconds": 9, "url": "https://www.youtube.com/watch?v=OTHERVIDEO1&t=9"},
            "not a dict",
        ]
        assert ax._public_deep_links(links, url) == [{"label": "ok", "seconds": 5, "url": f"{url}&t=5"}]
        assert ax._public_deep_links("not a list", url) == []


class TestVisibility:
    def test_an_unlisted_video_never_reaches_the_bundle(self, corpus, capsys):
        assert run(corpus) == 0
        assert [r["video_id"] for r in exported(corpus)["rows"]] == [PUBLIC_ID]
        assert f"not public, dropped: {UNLISTED_ID}" in capsys.readouterr().err

    def test_a_video_the_api_does_not_return_is_dropped_too(self, corpus, youtube_status, capsys):
        root, _, _ = corpus
        youtube_status["missing"].add(PUBLIC_ID)
        # The only public row is gone, so the channel has nothing exportable and the run refuses.
        assert run(corpus) == ax.EXIT_REFUSED
        err = capsys.readouterr().err
        assert f"not public, dropped: {PUBLIC_ID}" in err and "no exportable video" in err
        assert not (root / "_atlas-export").exists()

    def test_without_a_key_the_export_refuses_unless_told_to_skip(self, corpus, monkeypatch, capsys, youtube_status):
        monkeypatch.delenv(ax.YOUTUBE_API_KEY_ENV)
        assert run(corpus) == ax.EXIT_REFUSED
        assert ax.YOUTUBE_API_KEY_ENV in capsys.readouterr().err
        assert youtube_status["calls"] == []
        assert run(corpus, "--no-verify-public") == 0
        assert youtube_status["calls"] == []
        assert "visibility check skipped" in capsys.readouterr().err
        assert UNLISTED_ID in {r["video_id"] for r in exported(corpus)["rows"]}

    def test_a_failed_status_request_writes_nothing(self, corpus, monkeypatch, capsys):
        root, _, _ = corpus

        def boom(url):
            raise OSError("network down")

        monkeypatch.setattr(ax, "_fetch_status_batch", boom)
        assert run(corpus) == ax.EXIT_REFUSED
        assert "visibility check failed" in capsys.readouterr().err
        assert not (root / "_atlas-export").exists()

    def test_ids_are_asked_in_batches_of_fifty(self, monkeypatch):
        seen = []
        monkeypatch.setattr(ax, "_fetch_status_batch", lambda url: (seen.append(url), {"items": []})[1])
        ids = [f"id{i:09d}" for i in range(120)]
        assert ax.fetch_privacy_status(ids, "k") == {}
        assert len(seen) == 3 and all("part=status" in u and "key=k" in u for u in seen)


class TestDenyTerms:
    def test_a_deny_term_in_a_kept_row_aborts_before_any_write(self, corpus, tmp_path, capsys):
        root, _, _ = corpus
        terms = tmp_path / "terms.txt"
        terms.write_text("# public video\ntitl\nPUBLIC VIDEO\n", encoding="utf-8")
        assert run(corpus, "--deny-terms", str(terms)) == ax.EXIT_REFUSED
        assert not (root / "_atlas-export").exists()
        err = capsys.readouterr().err
        assert "1 deny term(s) found" in err and "public video" in err
        assert "1 deny term(s) under 5 characters ignored" in err

    def test_terms_only_in_denied_rows_do_not_fire(self, corpus, tmp_path):
        root, _, _ = corpus
        terms = tmp_path / "terms.txt"
        terms.write_text("\n".join(CANARIES) + "\n", encoding="utf-8")
        assert run(corpus, "--deny-terms", str(terms)) == 0
        assert (root / "_atlas-export" / "atlas.json").exists()

    def test_the_term_file_is_mandatory(self, corpus):
        _, atlas, policy = corpus
        with pytest.raises(SystemExit) as exc:
            ax.main(["--atlas", str(atlas), "--policy", str(policy)])
        assert exc.value.code == 2

    def test_unicode_equivalent_and_differently_cased_terms_still_hit(self, corpus, tmp_path, capsys):
        root, atlas, _ = corpus
        doc = private_atlas()
        doc["rows"][1]["title"] = "Visite de l'e\u0301cole"  # decomposed accent in the kept title
        atlas.write_text(json.dumps(doc), encoding="utf-8")
        terms = tmp_path / "terms.txt"
        terms.write_text("\u00c9COLE\n", encoding="utf-8")  # precomposed, upper case
        assert run(corpus, "--deny-terms", str(terms)) == ax.EXIT_REFUSED
        assert "1 deny term(s) found" in capsys.readouterr().err
        assert not (root / "_atlas-export").exists()

    def test_loader_rules(self, tmp_path):
        f = tmp_path / "t.txt"
        f.write_text("# Comment line\n\n   \nAbCdE\nabcd\nlonger term\n", encoding="utf-8")
        assert ax.load_deny_terms(f) == (["abcde", "longer term"], 1)


class TestPolicy:
    @pytest.mark.parametrize(
        "text, message",
        [
            ("since: 2025-01-01\n", "`channels` must be a non-empty list"),
            ("channels: []\n", "`channels` must be a non-empty list"),
            ("channels: [a, 3]\n", "entries must be non-empty strings"),
            ("channels: [a, a]\n", "repeats an entry"),
            ("channels: [a]\nsince: yesterday\n", "`since` must be a YYYY-MM-DD date"),
            ("channels: [a]\nsince: '2025-13-45'\n", "`since` must be a YYYY-MM-DD date"),
            ("channels: [a]\nsince: '2025-02-30'\n", "not a calendar date"),
            ("channels: [a]\nextra: 1\n", "unknown keys"),
            ("channels: [a]\nchannels: [b]\n", "duplicate policy key"),
            ("channels: [a]\n1: x\n", "keys must be strings"),
            ("channels: [a]\nexclude_video_ids: [nope]\n", "non-id entries"),
            ("- a\n", "must be a mapping"),
            ("channels: [a\n", "not valid YAML"),
        ],
    )
    def test_a_policy_that_cannot_be_trusted_is_refused_and_nothing_is_written(self, corpus, capsys, text, message):
        root, atlas, policy = corpus
        policy.write_text(text, encoding="utf-8")
        assert run(corpus) == ax.EXIT_REFUSED
        assert message in capsys.readouterr().err
        assert not (root / "_atlas-export").exists()

    def test_an_unquoted_date_and_an_empty_exclusion_list_are_accepted(self, corpus):
        _, _, policy = corpus
        policy.write_text(f"channels: [{PUBLIC_CH}]\nsince: 2025-01-01\nexclude_video_ids: []\n", encoding="utf-8")
        assert run(corpus) == 0

    def test_a_policy_channel_with_no_exportable_video_is_refused(self, corpus, capsys):
        root, _, policy = corpus
        policy.write_text(f"channels: [{PUBLIC_CH}, typo-channel]\n", encoding="utf-8")
        assert run(corpus) == ax.EXIT_REFUSED
        assert "typo-channel" in capsys.readouterr().err
        assert not (root / "_atlas-export").exists()


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

    def test_a_foreign_catalog_html_in_the_out_folder_survives_byte_identical(self, corpus, tmp_path, capsys):
        out = tmp_path / "elsewhere"
        out.mkdir()
        mine = "<!doctype html><title>mine</title><p>hand-written</p>\n"
        (out / "catalog.html").write_text(mine, encoding="utf-8")
        assert run(corpus, "--out", str(out)) == ax.EXIT_REFUSED
        assert (out / "catalog.html").read_text(encoding="utf-8") == mine
        assert not (out / "atlas.json").exists()
        assert "refusing to overwrite" in capsys.readouterr().err

    def test_a_private_atlas_is_required_as_input(self, corpus, capsys):
        root, _, policy = corpus
        assert run(corpus) == 0
        exported_path = root / "_atlas-export" / "atlas.json"
        argv = ["--atlas", str(exported_path), "--policy", str(policy), "--deny-terms", str(root / "no-terms.txt")]
        assert ax.main(argv) == ax.EXIT_REFUSED
        assert "not a private atlas" in capsys.readouterr().err

    def test_a_failed_render_writes_nothing_into_an_empty_folder(self, corpus, monkeypatch, capsys):
        root, _, _ = corpus
        monkeypatch.setattr(ax.atlas_render, "main", lambda argv: 2)
        assert run(corpus) == ax.EXIT_REFUSED
        out = root / "_atlas-export"
        assert not (out / "atlas.json").exists() and not (out / "catalog.html").exists()
        assert not list(out.glob("*.tmp"))
        assert "nothing in the output folder was changed" in capsys.readouterr().err

    def test_a_failed_render_leaves_the_previous_bundle_byte_identical(self, corpus, monkeypatch):
        root, _, _ = corpus
        assert run(corpus) == 0
        out = root / "_atlas-export"
        before = ((out / "atlas.json").read_bytes(), (out / "catalog.html").read_bytes())
        monkeypatch.setattr(ax.atlas_render, "main", lambda argv: 2)
        assert run(corpus) == ax.EXIT_REFUSED
        assert ((out / "atlas.json").read_bytes(), (out / "catalog.html").read_bytes()) == before
        assert not list(out.glob("*.tmp"))


class TestDeterminism:
    def test_two_exports_are_byte_identical(self, corpus):
        root, _, _ = corpus
        assert run(corpus) == 0
        first = (
            (root / "_atlas-export" / "atlas.json").read_bytes(),
            (root / "_atlas-export" / "catalog.html").read_bytes(),
        )
        assert run(corpus) == 0
        second = (
            (root / "_atlas-export" / "atlas.json").read_bytes(),
            (root / "_atlas-export" / "catalog.html").read_bytes(),
        )
        assert first == second
