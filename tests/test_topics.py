"""Topic-following metadata layer (issue #146).

Topic membership is a VIDEO-level curation fact ("why did the operator pull this
in"), asserted from two sources - a briefing's folder and a meta.json `--topic`
stamp - and materialized into a derived `topics.json`. These tests lock the
contract rows T1..T22 from the normalized implementation contract.

The sharpest edges, and why each has a test rather than a comment:

* the topic writer is NOT `update_meta` (it would overwrite the list, append to
  `modes_completed` and clear `last_error`);
* every write stamps full identity, because a quarantined read returns `{}` and
  an identity-less meta re-queues a full re-transcribe (issue #66);
* the build joins through `video_id`, never a filename rebuilt from title and
  date parts, so title rotation cannot turn a present video into an unresolved
  one (PR #136).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

import video_intel
from video_intel import (
    CONFIG_BACKUP_COMMANDS,
    TOPICS_FILENAME,
    build_topics,
    cmd_search,
    cmd_status,
    cmd_topics_build,
    collect_briefing_topic_assertions,
    load_topics_artifact,
    normalize_meta_topics,
    normalize_topic_slug,
    stamp_video_topics,
    topic_from_briefing_path,
    topic_slug_arg,
    topic_video_ids,
    write_topics,
)

# Ids that genuinely sit in more than one topic folder in the live corpus.
FDE_AND_AI_ENGINEERING = ("APqXGyCoGW4", "Byv311hdoHE", "KwhgfwOSToQ")
ADHD_AND_FDE = "nUPQImuDUng"


@pytest.fixture(autouse=True)
def _clear_pending_topic_stamps():
    """Finding C2: `_PENDING_TOPIC_STAMPS` is a module global, so one test's
    queued-but-unflushed target can leak into the next test's assertions if
    tests happen to run in a particular order. Clear it on both sides of
    every test in this module so no test's ordering-dependent pass survives
    a reorder or a `pytest -k` subset run.
    """
    video_intel._PENDING_TOPIC_STAMPS.clear()
    yield
    video_intel._PENDING_TOPIC_STAMPS.clear()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def write_briefing(output_dir: Path, rel_path: str, *, front_matter: str, body: str = "# brief\n") -> Path:
    """Drop a briefing markdown file with raw front-matter text.

    Raw text, not a dict dumped through yaml, because half of what these tests
    are about is the SHAPE the operator's editor left behind: a bare YAML date
    versus a quoted string versus no date key at all.
    """
    path = output_dir / "_briefings" / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front_matter}---\n\n{body}", encoding="utf-8")
    return path


def write_meta(
    output_dir: Path,
    channel: str,
    prefix: str,
    *,
    video_id: str,
    title: str = "A talk",
    published: str = "2026-05-01",
    extra: dict | None = None,
) -> Path:
    """A corpus meta.json plus the concepts sibling that makes it 'complete'."""
    channel_dir = output_dir / channel
    channel_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "video_url": f"https://www.youtube.com/watch?v={video_id}",
        "video_id": video_id,
        "channel": channel,
        "title": title,
        "published": published,
        "processed": "2026-05-02T10:00:00+00:00",
        "modes_completed": ["transcript", "scan"],
        "last_error": None,
    }
    meta.update(extra or {})
    path = channel_dir / f"{prefix}.meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return path


def video_entry(topics: dict, slug: str, video_id: str) -> dict:
    entries = [v for v in topics["topics"][slug]["videos"] if v["video_id"] == video_id]
    assert len(entries) == 1, f"{video_id} appears {len(entries)} times in topic {slug}"
    return entries[0]


# ---------------------------------------------------------------------------
# T8 - one slug normalizer, shared by both intake paths
# ---------------------------------------------------------------------------


class TestSlugNormalization:
    """T8. Contract C1 / amendment 2.

    Note the precise claim: the normalizer turns a separator INTO a hyphen, it
    does not delete it. So `FDE`, `fde`, `fde/` are one topic and `F D E`,
    `f_d_e` are one topic (`f-d-e`), distinct from `fde`. The contract's prose
    row lists all five together; the algorithm both the amendment and C1 state
    is what is implemented here, since a rule that deleted separators would
    silently fuse `agent-os` and `agentos` too.
    """

    @pytest.mark.parametrize("raw", ["FDE", "fde", "fde/", "/fde/", "  FDE  ", "-fde-", "Fde"])
    def test_one_topic_from_case_slash_and_padding(self, raw):
        assert normalize_topic_slug(raw) == "fde"

    @pytest.mark.parametrize("raw", ["F D E", "f_d_e", "F__D  E", "f\\d\\e", "f--d--e"])
    def test_separators_collapse_to_single_hyphens(self, raw):
        assert normalize_topic_slug(raw) == "f-d-e"

    @pytest.mark.parametrize("raw", ["", "   ", "///", "___", "---", None])
    def test_empty_normalization(self, raw):
        assert normalize_topic_slug(raw) == ""

    def test_cli_rejects_a_value_that_normalizes_to_empty(self):
        assert topic_slug_arg("  FDE/ ") == "fde"
        with pytest.raises(argparse.ArgumentTypeError):
            topic_slug_arg("___")

    def test_parser_error_exits_two_on_an_empty_slug(self, monkeypatch, tmp_path):
        """The CLI path is `parser.error`, not a silent empty tag."""
        monkeypatch.setattr("sys.argv", ["video_intel.py", "transcript", "--url", "u", "--topic", "___"])
        with pytest.raises(SystemExit) as exc:
            video_intel.main()
        assert exc.value.code == 2

    def test_folder_that_normalizes_to_empty_is_skipped_with_a_warning(self, tmp_path, caplog):
        briefings = tmp_path / "_briefings"
        md = briefings / "___" / "2026-01-01-note.md"
        md.parent.mkdir(parents=True)
        md.write_text("---\nvideo_ids: [aaaaaaaaaaa]\n---\n", encoding="utf-8")
        with caplog.at_level("WARNING"):
            assert topic_from_briefing_path(md, briefings) is None
        assert "empty topic slug" in caplog.text

    def test_repeatable_cli_flag_normalizes_and_dedupes(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--topic", action="append", dest="topic", default=None, type=topic_slug_arg)
        args = parser.parse_args(["--topic", "FDE", "--topic", "fde/", "--topic", "Sales"])
        assert video_intel.resolve_cli_topics(args) == ["fde", "sales"]


# ---------------------------------------------------------------------------
# T2, T3, T7, T10, T11 - deriving assertions from the briefings tree
# ---------------------------------------------------------------------------


class TestBriefingDerivation:
    def test_first_path_segment_wins_in_a_nested_subfolder(self, tmp_path):
        """T3. `_briefings/fde/deep-dives/note.md` is `fde`, never `deep-dives`.

        Zero cases of this exist in the live corpus, which is exactly why it has
        to be synthetic: the rule would otherwise be untested until the day
        someone files a briefing one level deeper and silently invents a topic.
        """
        output_dir = tmp_path
        write_briefing(
            output_dir,
            "fde/deep-dives/2026-08-22-note.md",
            front_matter="date: 2026-08-22\nvideo_ids:\n  - APqXGyCoGW4\n",
        )
        assertions = collect_briefing_topic_assertions(output_dir / "_briefings")
        assert list(assertions) == ["fde"]
        assert list(assertions["fde"]) == ["fde/deep-dives/2026-08-22-note.md"]

    def test_briefing_in_the_briefings_root_asserts_no_topic(self, tmp_path):
        write_briefing(tmp_path, "2026-08-22-catch-up.md", front_matter="video_ids:\n  - APqXGyCoGW4\n")
        assert collect_briefing_topic_assertions(tmp_path / "_briefings") == {}

    def test_nuggets_is_reserved_and_never_becomes_a_topic(self, tmp_path):
        """T7. Nugget briefs are synthesis output, not a curation decision."""
        write_briefing(
            tmp_path,
            "nuggets/2026-08-22-fde-brief.md",
            front_matter="artifact_type: nugget_brief\ncited_video_ids:\n  - APqXGyCoGW4\n",
        )
        # Even under the key the topic layer DOES read, the folder stays excluded.
        write_briefing(
            tmp_path,
            "nuggets/2026-08-23-other.md",
            front_matter="video_ids:\n  - Byv311hdoHE\n",
        )
        write_briefing(tmp_path, "fde/2026-08-22-real.md", front_matter="video_ids:\n  - Byv311hdoHE\n")
        assert list(collect_briefing_topic_assertions(tmp_path / "_briefings")) == ["fde"]

    def test_unparseable_front_matter_contributes_nothing(self, tmp_path):
        """T10a."""
        write_briefing(tmp_path, "fde/2026-08-22-broken.md", front_matter="video_ids: [unclosed\n  bad: : :\n")
        assert collect_briefing_topic_assertions(tmp_path / "_briefings") == {}

    def test_scalar_video_ids_contributes_nothing_and_does_not_crash(self, tmp_path, caplog):
        """T10b. `video_ids: APqXGyCoGW4` (a string, not a list)."""
        write_briefing(tmp_path, "fde/2026-08-22-scalar.md", front_matter="video_ids: APqXGyCoGW4\n")
        with caplog.at_level("WARNING"):
            assert collect_briefing_topic_assertions(tmp_path / "_briefings") == {}
        assert "video_ids" in caplog.text

    def test_undecodable_briefing_bytes_contribute_nothing(self, tmp_path):
        """T10c. A torn write mid-multibyte-character is a real corpus shape.

        `read_text` raises `UnicodeDecodeError`, which subclasses `ValueError`
        and NOT `OSError` - the exact distinction issue #124 was bitten by
        twice. A narrower except here would abort a whole rebuild over one file.
        """
        md = tmp_path / "_briefings" / "fde" / "2026-08-22-torn.md"
        md.parent.mkdir(parents=True)
        md.write_bytes(b"---\nvideo_ids:\n  - \xff\xfe broken\n---\n")
        assert collect_briefing_topic_assertions(tmp_path / "_briefings") == {}

    def test_briefing_with_no_video_ids_key_contributes_nothing(self, tmp_path):
        """T11. 11 of 30 real topic briefings are prose plans in this state."""
        write_briefing(tmp_path, "operator-brain/2026-08-01-plan.md", front_matter="title: A prose plan\n")
        assert collect_briefing_topic_assertions(tmp_path / "_briefings") == {}

    def test_multi_topic_channel_keeps_membership_at_the_video_level(self, tmp_path):
        """T2. A channel serving two topics rolls up to both, per VIDEO."""
        write_meta(tmp_path, "demo", "2026-05-01-one", video_id="vid1111aaaa")
        write_meta(tmp_path, "demo", "2026-05-02-two", video_id="vid2222bbbb")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - vid1111aaaa\n")
        write_briefing(tmp_path, "sales/2026-08-22-b.md", front_matter="video_ids:\n  - vid2222bbbb\n")

        topics = build_topics(tmp_path)
        assert topics["channels"] == {"demo": ["fde", "sales"]}
        assert [v["video_id"] for v in topics["topics"]["fde"]["videos"]] == ["vid1111aaaa"]
        assert [v["video_id"] for v in topics["topics"]["sales"]["videos"]] == ["vid2222bbbb"]


# ---------------------------------------------------------------------------
# T1, T4, T5, T12, T14, T15 - the derived join
# ---------------------------------------------------------------------------


class TestBuildTopics:
    def test_one_video_keeps_both_topic_memberships(self, tmp_path):
        """T1. Real case: APqXGyCoGW4 sits in both `ai-engineering` and `fde`."""
        for vid in FDE_AND_AI_ENGINEERING:
            write_meta(tmp_path, "demo", f"2026-05-01-{vid}", video_id=vid)
        ids = "".join(f"  - {v}\n" for v in FDE_AND_AI_ENGINEERING)
        write_briefing(tmp_path, "fde/2026-08-22-fde.md", front_matter=f"video_ids:\n{ids}")
        write_briefing(tmp_path, "ai-engineering/2026-07-29-worlds-fair.md", front_matter=f"video_ids:\n{ids}")

        topics = build_topics(tmp_path)
        for vid in FDE_AND_AI_ENGINEERING:
            assert video_entry(topics, "fde", vid)["video_id"] == vid
            assert video_entry(topics, "ai-engineering", vid)["video_id"] == vid
        assert topics["channels"]["demo"] == ["ai-engineering", "fde"]

    def test_sources_carry_the_asserting_briefing_paths(self, tmp_path):
        """T4. Per-membership provenance (amendment 1) is what makes removal work."""
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id=ADHD_AND_FDE)
        write_briefing(tmp_path, "adhd/2026-08-22-adhd.md", front_matter=f"video_ids:\n  - {ADHD_AND_FDE}\n")
        write_briefing(tmp_path, "fde/2026-08-22-fde.md", front_matter=f"video_ids:\n  - {ADHD_AND_FDE}\n")

        topics = build_topics(tmp_path)
        assert video_entry(topics, "adhd", ADHD_AND_FDE)["sources"] == {
            "briefings": ["adhd/2026-08-22-adhd.md"],
            "meta": False,
        }
        assert video_entry(topics, "fde", ADHD_AND_FDE)["sources"] == {
            "briefings": ["fde/2026-08-22-fde.md"],
            "meta": False,
        }

    def test_meta_only_topic_is_a_membership_with_no_briefing(self, tmp_path):
        """T5. The `--topic` stamp covers the window before any briefing exists."""
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id="metaonly123", extra={"topics": ["fde"]})
        topics = build_topics(tmp_path)
        entry = video_entry(topics, "fde", "metaonly123")
        assert entry["sources"] == {"briefings": [], "meta": True}
        assert topics["topics"]["fde"]["briefings"] == []
        assert topics["built_from"]["metas"] == 1

    def test_union_of_both_sources_on_one_membership(self, tmp_path):
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id="bothsrc1234", extra={"topics": ["fde"]})
        write_briefing(tmp_path, "fde/2026-08-22-fde.md", front_matter="video_ids:\n  - bothsrc1234\n")
        topics = build_topics(tmp_path)
        assert video_entry(topics, "fde", "bothsrc1234")["sources"] == {
            "briefings": ["fde/2026-08-22-fde.md"],
            "meta": True,
        }
        assert topics["topics"]["fde"]["video_count"] == 1

    def test_empty_topic_is_omitted(self, tmp_path):
        """T12. Real case: `_briefings/plans/` is a dated prose doc with no ids."""
        write_briefing(tmp_path, "plans/2026-08-22-30-day-plan.md", front_matter="date: 2026-08-22\n")
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id="realvid1234")
        write_briefing(tmp_path, "fde/2026-08-22-fde.md", front_matter="video_ids:\n  - realvid1234\n")

        topics = build_topics(tmp_path)
        assert "plans" not in topics["topics"]
        assert list(topics["topics"]) == ["fde"]

    def test_unresolved_id_is_kept_flagged_and_counted_but_not_rolled_up(self, tmp_path):
        """T14. A briefing can name an id no corpus artifact backs."""
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id="present1234")
        write_briefing(
            tmp_path,
            "fde/2026-08-22-fde.md",
            front_matter="video_ids:\n  - present1234\n  - AD-EmZ3v6-g\n",
        )
        topics = build_topics(tmp_path)
        ghost = video_entry(topics, "fde", "AD-EmZ3v6-g")
        assert ghost["unresolved"] is True
        assert "channel" not in ghost
        assert topics["topics"]["fde"]["unresolved_count"] == 1
        assert topics["topics"]["fde"]["channels"] == ["demo"]  # ghost excluded from the rollup
        assert topics["summary"]["unresolved"] == 1

    def test_duplicate_ids_yield_one_membership(self, tmp_path):
        """T15. Repeated in one list, and repeated across two briefings."""
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id="dupvid12345")
        write_briefing(
            tmp_path,
            "fde/2026-08-22-a.md",
            front_matter="video_ids:\n  - dupvid12345\n  - dupvid12345\n",
        )
        write_briefing(tmp_path, "fde/2026-08-23-b.md", front_matter="video_ids:\n  - dupvid12345\n")
        topics = build_topics(tmp_path)
        assert topics["topics"]["fde"]["video_count"] == 1
        assert video_entry(topics, "fde", "dupvid12345")["sources"]["briefings"] == [
            "fde/2026-08-22-a.md",
            "fde/2026-08-23-b.md",
        ]

    def test_topics_json_never_touches_the_taxonomy(self, tmp_path):
        """T21. Curation intent and emergent concepts stay separate layers."""
        taxonomy = tmp_path / "taxonomy.json"
        taxonomy.write_text(json.dumps({"version": 1, "built_from": 3, "concepts": {}}), encoding="utf-8")
        before = taxonomy.read_bytes()
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id="taxvid12345", extra={"topics": ["fde"]})

        cmd_topics_build(argparse.Namespace(dry_run=False), {"output_dir": str(tmp_path)})

        assert taxonomy.read_bytes() == before
        assert "fde" not in taxonomy.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T13 - deterministic first_seen
# ---------------------------------------------------------------------------


class TestFirstSeen:
    """T13. Contract C3: date -> created_at -> filename -> nothing. Never a clock.

    All four shapes exist in the live corpus (9 bare YAML dates, 1 quoted
    string, 8 `created_at`, 12 with neither), which is why the chain is code
    rather than an assumption about how briefings are written.
    """

    def _first_seen(self, tmp_path, rel_path, front_matter):
        write_meta(tmp_path, "demo", "2026-05-01-x", video_id="fsvid123456")
        write_briefing(tmp_path, rel_path, front_matter=front_matter + "video_ids:\n  - fsvid123456\n")
        return build_topics(tmp_path)["topics"][rel_path.split("/")[0]]["first_seen"]

    def test_bare_yaml_date_object(self, tmp_path):
        assert self._first_seen(tmp_path, "t1/2026-12-31-note.md", "date: 2026-03-01\n") == "2026-03-01"

    def test_quoted_date_string(self, tmp_path):
        assert self._first_seen(tmp_path, "t2/2026-12-31-note.md", 'date: "2026-02-01"\n') == "2026-02-01"

    def test_created_at_when_date_is_absent(self, tmp_path):
        assert self._first_seen(tmp_path, "t3/2026-12-31-note.md", "created_at: 2026-01-15\n") == "2026-01-15"

    def test_filename_date_when_neither_key_is_present(self, tmp_path):
        assert self._first_seen(tmp_path, "t4/2026-01-05-note.md", "title: no dates\n") == "2026-01-05"

    def test_no_date_anywhere_yields_null_not_today(self, tmp_path):
        """The wall clock is banned: it would make every rebuild a diff."""
        got = self._first_seen(tmp_path, "t5/note.md", "title: no dates\n")
        assert got is None
        assert got != dt.date.today().isoformat()

    def test_earliest_evidence_wins_across_briefings_and_meta(self, tmp_path):
        write_meta(
            tmp_path,
            "demo",
            "2026-05-01-x",
            video_id="earliest123",
            extra={"topics": ["fde"], "processed": "2026-04-04T00:00:00+00:00"},
        )
        write_briefing(
            tmp_path, "fde/2026-09-09-late.md", front_matter="date: 2026-09-09\nvideo_ids:\n  - earliest123\n"
        )
        write_briefing(
            tmp_path, "fde/2026-06-06-mid.md", front_matter="date: 2026-06-06\nvideo_ids:\n  - earliest123\n"
        )
        assert build_topics(tmp_path)["topics"]["fde"]["first_seen"] == "2026-04-04"

    def test_unparseable_date_is_ignored_and_falls_through(self, tmp_path):
        assert self._first_seen(tmp_path, "t6/2026-07-07-note.md", 'date: "not a date"\n') == "2026-07-07"


# ---------------------------------------------------------------------------
# T9 - byte-stability
# ---------------------------------------------------------------------------


class TestByteStability:
    def test_two_builds_of_unchanged_inputs_are_byte_identical(self, tmp_path):
        """T9. Determinism is what makes `topics.json` a derived artifact.

        A wall-clock field, an unsorted set, or dict iteration order would each
        make every rebuild a spurious diff on a cloud-synced corpus.
        """
        for i, vid in enumerate(FDE_AND_AI_ENGINEERING):
            write_meta(tmp_path, f"ch{i}", f"2026-05-0{i + 1}-{vid}", video_id=vid, extra={"topics": ["fde", "Sales"]})
        ids = "".join(f"  - {v}\n" for v in (*FDE_AND_AI_ENGINEERING, "ghost123456"))
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter=f"date: 2026-08-22\nvideo_ids:\n{ids}")
        write_briefing(tmp_path, "ai-engineering/sub/2026-07-29-b.md", front_matter=f"video_ids:\n{ids}")

        first = write_topics(tmp_path, build_topics(tmp_path)).read_bytes()
        second = write_topics(tmp_path, build_topics(tmp_path)).read_bytes()
        assert first == second
        assert json.loads(first.decode("utf-8"))["version"] == 1


# ---------------------------------------------------------------------------
# T16, T18, T19 - the dedicated topic writer (contract C5)
# ---------------------------------------------------------------------------


class TestTopicWriter:
    def _video(self, video_id="wr1terid123"):
        return {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": "A Rotated Title",
            "published": "2026-05-01",
        }

    def test_merges_rather_than_overwrites_an_existing_list(self, tmp_path):
        meta_path = write_meta(tmp_path, "demo", "p", video_id="wr1terid123", extra={"topics": ["sales"]})
        merged = stamp_video_topics(meta_path, self._video(), "demo", ["fde"])
        assert merged == ["fde", "sales"]
        assert json.loads(meta_path.read_text(encoding="utf-8"))["topics"] == ["fde", "sales"]

    def test_does_not_route_through_update_meta(self, tmp_path, monkeypatch):
        """T19a. `update_meta` is the SUCCESS writer and is wrong here.

        It would `meta.update(fields)` (overwriting the list), append to
        `modes_completed`, and clear `last_error`. A `--topic` stamp is
        provenance, not stage completion.
        """
        meta_path = write_meta(tmp_path, "demo", "p", video_id="wr1terid123")

        def explode(*_a, **_kw):
            raise AssertionError("the topic writer must not route through update_meta")

        monkeypatch.setattr(video_intel, "update_meta", explode)
        stamp_video_topics(meta_path, self._video(), "demo", ["fde"])

    def test_leaves_modes_completed_and_last_error_untouched(self, tmp_path):
        """T19b."""
        meta_path = write_meta(
            tmp_path,
            "demo",
            "p",
            video_id="wr1terid123",
            extra={"modes_completed": ["transcript"], "last_error": "concepts: boom", "concepts_status": "error: boom"},
        )
        stamp_video_topics(meta_path, self._video(), "demo", ["fde"])
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["modes_completed"] == ["transcript"]
        assert meta["last_error"] == "concepts: boom"
        assert meta["concepts_status"] == "error: boom"

    def test_preserves_alt_titles_and_skip_modes(self, tmp_path):
        meta_path = write_meta(
            tmp_path,
            "demo",
            "p",
            video_id="wr1terid123",
            extra={"alt_titles": ["Old Title"], "skip_modes": ["transcript"]},
        )
        stamp_video_topics(meta_path, self._video(), "demo", ["fde"])
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["alt_titles"] == ["Old Title"]
        assert meta["skip_modes"] == ["transcript"]

    def test_identity_is_stamped_after_a_quarantining_read(self, tmp_path):
        """T18. Issue #66: a `{}` read makes these fields the WHOLE file.

        Without the identity stamp the result is a meta with only `topics`,
        which `_load_video_id_index` skips - re-queueing an expensive
        re-transcribe as the price of recording a free provenance tag.
        """
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True)
        meta_path = channel_dir / "p.meta.json"
        meta_path.write_text("{ this is not json", encoding="utf-8")

        stamp_video_topics(meta_path, self._video(), "demo", ["fde"])

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["video_id"] == "wr1terid123"
        assert meta["channel"] == "demo"
        assert meta["title"] == "A Rotated Title"
        assert meta["published"] == "2026-05-01"
        assert meta["video_url"].endswith("wr1terid123")
        assert meta["topics"] == ["fde"]
        assert list(channel_dir.glob("*.meta.corrupt.json")), "unusable content must be quarantined, not dropped"

    def test_os_error_propagates_on_this_success_path(self, tmp_path, monkeypatch):
        """Issue #124: a read we merely failed to PERFORM must not license a rewrite."""
        meta_path = write_meta(tmp_path, "demo", "p", video_id="wr1terid123", extra={"alt_titles": ["keep me"]})
        real_read = Path.read_bytes

        def flaky(self, *a, **kw):
            if self == meta_path:
                raise OSError("transient cloud-mount read error")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_bytes", flaky)
        with pytest.raises(OSError):
            stamp_video_topics(meta_path, self._video(), "demo", ["fde"])
        monkeypatch.undo()
        assert json.loads(meta_path.read_text(encoding="utf-8"))["alt_titles"] == ["keep me"]

    def test_never_creates_a_meta_that_did_not_exist(self, tmp_path, caplog):
        """A second meta claiming one video_id manufactures a phantom dedupe group."""
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True)
        meta_path = channel_dir / "p.meta.json"
        with caplog.at_level("WARNING"):
            assert stamp_video_topics(meta_path, self._video(), "demo", ["fde"]) is None
        assert not meta_path.exists()
        assert "no meta.json" in caplog.text

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("fde", ["fde"]),
            (["fde", 3, None, "", "Sales"], ["fde", "sales"]),
            (3, []),
            ({"fde": True}, []),
        ],
    )
    def test_malformed_topics_field_keeps_the_usable_entries(self, raw, expected, caplog):
        """T16. Never quarantine a whole meta over one bad OPTIONAL field."""
        with caplog.at_level("WARNING"):
            assert normalize_meta_topics(raw, label="p.meta.json") == expected
        assert "malformed `topics`" in caplog.text

    def test_malformed_topics_field_does_not_quarantine_the_meta(self, tmp_path):
        meta_path = write_meta(
            tmp_path,
            "demo",
            "p",
            video_id="wr1terid123",
            extra={"topics": "fde", "alt_titles": ["keep me"]},
        )
        stamp_video_topics(meta_path, self._video(), "demo", ["sales"])
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["topics"] == ["fde", "sales"]
        assert meta["alt_titles"] == ["keep me"]
        assert not list((tmp_path / "demo").glob("*.meta.corrupt.json"))

    def test_writer_normalizes_raw_slugs_from_a_non_argparse_caller(self, tmp_path):
        """Found by the Gate-1 smoke: argparse is not the only caller.

        A namespace built in code bypasses `topic_slug_arg`, and an unnormalized
        slug that reaches disk is permanent - `FDE`, `fde/` and `Founder Led
        Sales` became three topics for one curation decision.
        """
        meta_path = write_meta(tmp_path, "demo", "p", video_id="wr1terid123")
        merged = stamp_video_topics(meta_path, self._video(), "demo", ["FDE", "fde/", "Founder Led Sales"])
        assert merged == ["fde", "founder-led-sales"]
        assert json.loads(meta_path.read_text(encoding="utf-8"))["topics"] == ["fde", "founder-led-sales"]

    def test_resolve_cli_topics_normalizes_a_hand_built_namespace(self):
        args = argparse.Namespace(topic=["FDE", "fde/", "___", "Founder Led Sales"])
        assert video_intel.resolve_cli_topics(args) == ["fde", "founder-led-sales"]

    def test_no_topics_is_a_no_op(self, tmp_path):
        meta_path = write_meta(tmp_path, "demo", "p", video_id="wr1terid123")
        before = meta_path.read_bytes()
        assert stamp_video_topics(meta_path, self._video(), "demo", []) is None
        assert meta_path.read_bytes() == before


# ---------------------------------------------------------------------------
# T6 - checker and writer agree on the path
# ---------------------------------------------------------------------------


class TestCheckerAndWriterAgreeOnPaths:
    """T6. PR #136: a verifier that re-derives a path is a false-alarm generator.

    Both sides are derived INDEPENDENTLY here and then compared. A stub that
    handed the writer and the reader the same path would agree by construction
    and prove nothing - which is how three real defects sat live under a green
    suite in PR #136.
    """

    def test_build_joins_through_video_id_not_a_reconstructed_filename(self, tmp_path):
        channel_dir = tmp_path / "demo"
        rotated_prefix = "2026-01-01-old-seo-title"
        video = {
            "video_id": "rotated1234",
            "url": "https://www.youtube.com/watch?v=rotated1234",
            "title": "New Rotated Title",  # computes a DIFFERENT slug
            "published": "2026-01-01",
        }
        assert video_intel.video_file_prefix(video) != rotated_prefix

        meta_path = write_meta(tmp_path, "demo", rotated_prefix, video_id="rotated1234", title=video["title"])
        stamp_video_topics(meta_path, video, "demo", ["fde"])

        # The WRITER's real destination, discovered from the filesystem by the
        # id it carries - no path expression borrowed from the code under test.
        on_disk = [
            p
            for p in channel_dir.glob("*.meta.json")
            if json.loads(p.read_text(encoding="utf-8")).get("video_id") == "rotated1234"
        ]
        assert len(on_disk) == 1, f"tree = {sorted(tmp_path.rglob('*'))}"

        # The READER's expectation, derived from the id alone.
        topics = build_topics(tmp_path)
        entry = video_entry(topics, "fde", "rotated1234")
        assert "unresolved" not in entry, "a title rotation must not turn a present video into a ghost"
        assert entry["channel"] == on_disk[0].parent.name
        assert topics["channels"] == {"demo": ["fde"]}

        # And the path a filename reconstruction would have produced does not exist,
        # so this test would be vacuous if the join were rebuilt from title parts.
        assert not (channel_dir / f"{video_intel.video_file_prefix(video)}.meta.json").exists()

    def test_topics_json_is_written_where_the_readers_look(self, tmp_path):
        write_meta(tmp_path, "demo", "p", video_id="written1234", extra={"topics": ["fde"]})
        written_path = write_topics(tmp_path, build_topics(tmp_path))

        # Independent derivation: the only *.json at the corpus root the build produced.
        found = [p for p in tmp_path.glob("*.json") if p.name == TOPICS_FILENAME]
        assert found == [written_path]

        data, problem = load_topics_artifact(tmp_path)
        assert problem is None
        assert topic_video_ids(data, "fde") == {"written1234"}


# ---------------------------------------------------------------------------
# T17 - the lazy-skip stamp
# ---------------------------------------------------------------------------


class TestLazySkipStamping:
    """T17 / amendment 4: provenance is the flag's ENTIRE purpose.

    A run where every stage skips because the artifacts already exist is exactly
    the run an operator uses to backfill a topic onto a video they curated
    months ago. If the stamp only fired when work happened, the flag would be
    useless for the case it exists for.
    """

    @pytest.fixture
    def stub_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("video_intel.require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr("video_intel.create_client", lambda _key: MagicMock())
        monkeypatch.setattr("video_intel.resolve_model", lambda _a, _c: "stub-model")
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path / "corpus")
        monkeypatch.setattr("video_intel.load_prompt", lambda name: f"prompt-{name}")
        monkeypatch.setattr("video_intel.load_taxonomy", lambda _d: {"concepts": {}})
        return tmp_path

    def test_process_file_stamps_when_every_stage_skips(self, stub_env, monkeypatch, tmp_path):
        output_dir = tmp_path / "corpus"
        channel_dir = output_dir / "everyinc"
        channel_dir.mkdir(parents=True)
        mp4 = channel_dir / "video.mp4"
        mp4.write_bytes(b"fake")
        prefix = "video"
        for suffix, body in ((".mindmap.md", "m"), (".transcript.md", "t"), (".concepts.json", "{}")):
            (channel_dir / f"{prefix}{suffix}").write_text(body, encoding="utf-8")
        meta_path = channel_dir / f"{prefix}.meta.json"
        meta_path.write_text(
            json.dumps(
                {
                    "video_id": "lazyvid1234",
                    "video_url": "https://www.youtube.com/watch?v=lazyvid1234",
                    "title": "video",
                    "published": "2026-04-23",
                    "channel": "everyinc",
                    "modes_completed": ["scan", "transcript", "concepts"],
                    "last_error": None,
                }
            ),
            encoding="utf-8",
        )
        before = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "topics" not in before

        uploads: list = []
        monkeypatch.setattr("video_intel.upload_local_video", lambda _c, p: uploads.append(p) or "files/nope")
        monkeypatch.setattr(
            "video_intel.process_mindmap", lambda *a, **kw: (kw.get("prefix") or prefix, "skipped (exists)")
        )
        monkeypatch.setattr(
            "video_intel.process_transcript", lambda *a, **kw: (kw.get("prefix") or prefix, "skipped (exists)")
        )
        monkeypatch.setattr(
            "video_intel.process_concepts", lambda *a, **kw: (kw.get("prefix") or prefix, "skipped (exists)")
        )

        args = argparse.Namespace(
            file=mp4,
            channel="everyinc",
            video_id=None,
            title=None,
            date=None,
            start=None,
            end=None,
            force=False,
            model=None,
            prompt=None,
            topic=["fde", "sales"],
        )
        video_intel.cmd_process(args, {"channels": [{"name": "everyinc", "url": "u"}]})

        assert uploads == [], "the lazy-skip path must not have done any work"
        after = json.loads(meta_path.read_text(encoding="utf-8"))
        assert after["topics"] == ["fde", "sales"]
        assert after["modes_completed"] == before["modes_completed"]
        assert after["video_id"] == "lazyvid1234"

    def test_pending_stamp_is_flushed_even_when_the_command_exits(self, tmp_path, monkeypatch):
        """A stamp registered before a `sys.exit` still lands (main()'s finally)."""
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True)
        video = {"video_id": "pending1234", "url": "u", "title": "T", "published": "2026-05-01"}
        args = argparse.Namespace(topic=["fde"])

        video_intel.register_topic_stamp_target(args, video, channel_dir, "p", "demo")
        assert video_intel._PENDING_TOPIC_STAMPS, "no meta yet, so the stamp must be pending"

        write_meta(tmp_path, "demo", "p", video_id="pending1234")
        video_intel.flush_topic_stamps()

        assert json.loads((channel_dir / "p.meta.json").read_text(encoding="utf-8"))["topics"] == ["fde"]
        assert video_intel._PENDING_TOPIC_STAMPS == []

    def test_no_topic_flag_registers_nothing(self, tmp_path):
        video_intel.register_topic_stamp_target(
            argparse.Namespace(topic=None), {"video_id": "x"}, tmp_path, "p", "demo"
        )
        assert video_intel._PENDING_TOPIC_STAMPS == []


# ---------------------------------------------------------------------------
# Finding C1 (review of issue #146) - a direct cmd_* caller must flush too
# ---------------------------------------------------------------------------


class TestDirectCmdCallerFlushesPendingStamp:
    """A `cmd_*` function called directly - bypassing main() entirely, the way
    a test or a library caller does - used to silently drop a pending stamp on
    a brand-new video: only main()'s `finally` drained `_PENDING_TOPIC_STAMPS`.
    `cmd_mindmap` (and `cmd_transcript` / `cmd_process`) now flush from their
    own `finally`, so the stamp lands even when main() never runs.
    """

    def test_cmd_mindmap_file_flushes_a_pending_stamp_without_going_through_main(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), MagicMock()))
        monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
        monkeypatch.setattr(video_intel, "resolve_model", lambda _a, _c: "stub-model")
        monkeypatch.setattr(video_intel, "load_prompt", lambda _name: "prompt")
        monkeypatch.setattr(video_intel, "upload_local_video", lambda _c, _p: "files/nope")

        output_dir = tmp_path / "corpus"
        channel_dir = output_dir / "demo"
        channel_dir.mkdir(parents=True)
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: output_dir)

        mp4 = tmp_path / "source" / "brandnew.mp4"
        mp4.parent.mkdir(parents=True)
        mp4.write_bytes(b"fake")

        def fake_process_mindmap(*_a, **kw):
            # Stands in for the real writer: the meta.json is created HERE,
            # mid-pipeline - strictly after register_topic_stamp_target already
            # ran and found nothing on disk, which is what makes the stamp
            # pending rather than landing immediately.
            prefix = kw["prefix"]
            channel_dir_override = kw["channel_dir_override"]
            channel_dir_override.mkdir(parents=True, exist_ok=True)
            (channel_dir_override / f"{prefix}.meta.json").write_text(
                json.dumps(
                    {
                        "video_id": "brandnewvid1",
                        "video_url": "https://www.youtube.com/watch?v=brandnewvid1",
                        "channel": "demo",
                        "title": "Brand New Talk",
                        "published": "2026-08-22",
                        "processed": "2026-08-22T00:00:00+00:00",
                        "modes_completed": ["mindmap"],
                        "last_error": None,
                    }
                ),
                encoding="utf-8",
            )
            return prefix, "done"

        monkeypatch.setattr(video_intel, "process_mindmap", fake_process_mindmap)

        args = argparse.Namespace(
            file=mp4,
            channel="demo",
            video_id="brandnewvid1",
            title="Brand New Talk",
            date="2026-08-22",
            force=False,
            model=None,
            prompt=None,
            media_resolution="low",
            topic=["fde"],
        )

        # Calls cmd_mindmap DIRECTLY. main() never runs here, so its finally's
        # flush_topic_stamps() call never fires - only the fix inside
        # cmd_mindmap itself can land this stamp.
        video_intel.cmd_mindmap(args, {"channels": [{"name": "demo", "url": "u"}]})

        # Issue #186: with both --title and --date the writer's prefix is the
        # {date}-{slug} convention, and the pending stamp follows the writer's
        # own (channel_dir, prefix) pair - so the stamp lands there too.
        meta_path = channel_dir / "2026-08-22-brand-new-talk.meta.json"
        assert json.loads(meta_path.read_text(encoding="utf-8"))["topics"] == ["fde"]
        assert video_intel._PENDING_TOPIC_STAMPS == [], "the direct call must have drained the pending queue itself"


# ---------------------------------------------------------------------------
# T20 - the read surfaces fail soft
# ---------------------------------------------------------------------------


class TestReadSurfacesFailSoft:
    """T20 / contract C8. One actionable message naming `topics-build`.

    Never a traceback, and never silent emptiness: an empty result and a missing
    index are indistinguishable to a reader, and only one of them is fixed by a
    rebuild.
    """

    def test_absent_artifact_message_names_the_rebuild_command(self, tmp_path):
        data, problem = load_topics_artifact(tmp_path)
        assert data is None
        assert "topics-build" in problem

    @pytest.mark.parametrize("content", ["{ not json", '["a list"]', '{"topics": "not a dict"}'])
    def test_malformed_artifact_message_names_the_rebuild_command(self, tmp_path, content):
        (tmp_path / TOPICS_FILENAME).write_text(content, encoding="utf-8")
        data, problem = load_topics_artifact(tmp_path)
        assert data is None
        assert "topics-build" in problem

    def test_status_prints_one_message_and_does_not_raise(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_status(argparse.Namespace(), {"channels": [{"name": "demo"}]})
        out = capsys.readouterr().out
        assert out.count("topics-build") == 1
        assert "Traceback" not in out

    def test_status_shows_the_per_channel_rollup(self, tmp_path, capsys, monkeypatch):
        write_meta(tmp_path, "demo", "p", video_id="statusvid12", extra={"topics": ["fde", "sales"]})
        write_topics(tmp_path, build_topics(tmp_path))
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_status(argparse.Namespace(), {"channels": [{"name": "demo"}]})
        out = capsys.readouterr().out
        assert "topics: fde, sales" in out
        assert "2 topics" in out

    def test_status_surfaces_a_topic_channel_missing_from_config(self, tmp_path, capsys, monkeypatch):
        write_meta(tmp_path, "orphan", "p", video_id="orphanvid12", extra={"topics": ["fde"]})
        write_topics(tmp_path, build_topics(tmp_path))
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_status(argparse.Namespace(), {"channels": []})
        out = capsys.readouterr().out
        assert "orphan: fde" in out

    def test_search_topic_without_an_index_prints_one_message(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        args = argparse.Namespace(query="anything", channel=None, limit=None, vector=False, since=None, topic="fde")
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert out.count("topics-build") == 1
        assert "Traceback" not in out

    def test_search_unknown_topic_lists_the_known_ones(self, tmp_path, capsys, monkeypatch):
        write_meta(tmp_path, "demo", "p", video_id="knownvid123", extra={"topics": ["fde"]})
        write_topics(tmp_path, build_topics(tmp_path))
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        args = argparse.Namespace(query="anything", channel=None, limit=None, vector=False, since=None, topic="sales")
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert "No topic 'sales'" in out
        assert "known: fde" in out


# ---------------------------------------------------------------------------
# search --topic filtering
# ---------------------------------------------------------------------------


class TestSearchTopicFilter:
    def _corpus(self, tmp_path):
        (tmp_path / "taxonomy.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "built_from": 2,
                    "concepts": {"c.agents": {"preferred_label": "agents", "aliases": [], "video_count": 2}},
                }
            ),
            encoding="utf-8",
        )
        for channel, prefix, vid in (("demo", "p1", "insidevid12"), ("demo", "p2", "outsidevid1")):
            write_meta(tmp_path, channel, prefix, video_id=vid, title=f"talk {vid}")
            (tmp_path / channel / f"{prefix}.concepts.json").write_text(
                json.dumps({"video_id": vid, "concepts": [{"concept_id": "c.agents"}]}), encoding="utf-8"
            )
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - insidevid12\n")
        write_topics(tmp_path, build_topics(tmp_path))

    def test_concept_mode_returns_only_the_topics_videos(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        args = argparse.Namespace(query="agents", channel=None, limit=None, vector=False, since=None, topic="fde")
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert "insidevid12" in out
        assert "outsidevid1" not in out

    def test_without_the_flag_both_videos_come_back(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        args = argparse.Namespace(query="agents", channel=None, limit=None, vector=False, since=None, topic=None)
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert "insidevid12" in out
        assert "outsidevid1" in out


# ---------------------------------------------------------------------------
# Issue #203 - the topic scope is EXACT. `TOPIC_FILTER_OVERFETCH` is retired.
# ---------------------------------------------------------------------------


class TestTopicScopeIsExactNotOverfetched:
    """Replaces `TestTopicFilterOverfetchIsLoadBearing`, retired with the
    constant it guarded (issue #203).

    That class proved the old mechanism was load-bearing: with the multiplier
    mutated to 1, a topic member ranked 6th unfiltered vanished from a
    `--limit 3` search. It could not prove the mechanism was SUFFICIENT, and it
    was not - the multiplier is a probability improvement, so a member ranked
    below `limit * 5` was still lost. On the live corpus that is exactly what
    happened: a 19-member topic had to out-rank 2,300+ other videos for 25
    pool slots and the scoped query returned nothing at all.

    These tests assert the strictly stronger property the replacement gives:
    the member's UNFILTERED rank does not matter at any depth, because the
    scope is applied to retrieval itself rather than to a globally ranked pool.
    Each fixture ranks the member DEAD LAST behind enough competitors that no
    surviving multiplier could have reached it, so a diff that reintroduces an
    over-fetch post-filter fails here rather than passing quietly.
    """

    def test_the_retired_constant_is_gone_on_purpose(self):
        """The constant existed only to compensate for a post-filter that no
        longer exists. Leaving it behind would invite a future edit to
        re-plumb the post-filter around it, which is the defect #203 fixed."""
        assert not hasattr(video_intel, "TOPIC_FILTER_OVERFETCH")

    def test_concept_mode_surfaces_a_member_ranked_dead_last(self, tmp_path, capsys, monkeypatch):
        """40 out-of-topic videos each match 3 concepts; the member matches 1,
        so the `(-matched_concepts, published)` sort puts it 41st. At
        `--limit 1` the old `limit * 5` window reached rank 5. The scope is
        applied before the cap now, so rank is irrelevant."""
        concepts = {
            "c.a0": {"preferred_label": "agents overview", "aliases": [], "video_count": 1},
            "c.a1": {"preferred_label": "agents basics", "aliases": [], "video_count": 40},
            "c.a2": {"preferred_label": "agents patterns", "aliases": [], "video_count": 40},
            "c.a3": {"preferred_label": "agents tooling", "aliases": [], "video_count": 40},
        }
        (tmp_path / "taxonomy.json").write_text(
            json.dumps({"version": 1, "built_from": 41, "concepts": concepts}), encoding="utf-8"
        )

        for i in range(40):
            vid = f"outsidevid{i:02d}"
            write_meta(
                tmp_path,
                "demo",
                f"p-out{i:02d}",
                video_id=vid,
                title=f"outside talk {vid}",
                published="2026-01-01",
            )
            (tmp_path / "demo" / f"p-out{i:02d}.concepts.json").write_text(
                json.dumps({"video_id": vid, "concepts": [{"concept_id": c} for c in ("c.a1", "c.a2", "c.a3")]}),
                encoding="utf-8",
            )

        member_id = "membervid01"
        write_meta(tmp_path, "demo", "p-member", video_id=member_id, title="topic talk", published="2026-02-01")
        (tmp_path / "demo" / "p-member.concepts.json").write_text(
            json.dumps({"video_id": member_id, "concepts": [{"concept_id": "c.a0"}]}), encoding="utf-8"
        )
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter=f"video_ids:\n  - {member_id}\n")
        write_topics(tmp_path, build_topics(tmp_path))

        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        args = argparse.Namespace(query="agents", channel=None, limit=1, vector=False, since=None, topic="fde")
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert "topic talk" in out, "an exact scope must reach a member at any unfiltered rank"
        assert "outside talk" not in out

    def test_the_partial_concept_cut_is_the_remaining_cliff(self, tmp_path, capsys, monkeypatch):
        """Scope honesty, in the shape of `test_does_not_claim_the_monolithic_early_stop_shape`.

        Issue #203 makes the VIDEO list exact. It does NOT widen concept
        SELECTION: on the partial-match path `search_corpus` still feeds video
        lookup from `matching_concepts[:5]` (the issue #189 rule, deliberately
        untouched), and that cut runs BEFORE the topic scope. So a member whose
        only matching concept ranks sixth stays unreachable at any `--limit`.

        This test asserts the LIMITATION, not a fix. It exists so a future
        reader cannot mistake "the scope is exact" for "concept search can
        always reach a member", and so that widening the concept cut is a
        deliberate decision against issue #189's frozen contract rather than an
        accident. Found by an executing reviewer, not by reading the diff.
        """
        # The cut lives on the PARTIAL path only: when concepts match the query
        # exactly, every exact match feeds video lookup and there is no cut at
        # all. So the query carries a third term no concept has, making all six
        # matches partial. Five of them are tighter labels (2 of 4 tokens are
        # query terms) than the member's (2 of 5), so #189's tightness
        # tie-break ranks the member's concept sixth, just past the cut.
        concepts = {
            f"c.d{i}": {
                "preferred_label": f"agent design pattern {i}",
                "aliases": [],
                "video_count": 100 - i,
            }
            for i in range(5)
        }
        concepts["c.d9"] = {"preferred_label": "agent orchestration for solo builders", "aliases": [], "video_count": 1}
        (tmp_path / "taxonomy.json").write_text(
            json.dumps({"version": 1, "built_from": 6, "concepts": concepts}), encoding="utf-8"
        )

        for i in range(5):
            vid = f"outsidevid{i:02d}"
            write_meta(tmp_path, "demo", f"p-out{i}", video_id=vid, title=f"outside {vid}", published="2026-01-01")
            (tmp_path / "demo" / f"p-out{i}.concepts.json").write_text(
                json.dumps({"video_id": vid, "concepts": [{"concept_id": f"c.d{i}"}]}), encoding="utf-8"
            )

        member_id = "membervid01"
        write_meta(tmp_path, "demo", "p-member", video_id=member_id, title="topic talk", published="2026-02-01")
        (tmp_path / "demo" / "p-member.concepts.json").write_text(
            json.dumps({"video_id": member_id, "concepts": [{"concept_id": "c.d9"}]}), encoding="utf-8"
        )
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter=f"video_ids:\n  - {member_id}\n")
        write_topics(tmp_path, build_topics(tmp_path))

        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        # Confirm the premise first: the member's concept really is outside the
        # top five that feed video lookup. Without this the test could pass for
        # the wrong reason if ranking ever promoted it.
        unscoped = video_intel.search_corpus(tmp_path, "agent design orchestration", limit=50)
        assert len(unscoped["concepts"]) > 5
        assert not any(c["_match_score"] == 1.0 for c in unscoped["concepts"]), (
            "the fixture must exercise the PARTIAL path - an exact match feeds every "
            "matching concept to video lookup and there is no cut to demonstrate"
        )
        assert "c.d9" not in {c["concept_id"] for c in unscoped["concepts"][:5]}

        args = argparse.Namespace(
            query="agent design orchestration", channel=None, limit=50, vector=False, since=None, topic="fde"
        )
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert "topic talk" not in out, (
            "If this now passes, concept SELECTION was widened - that rewrites issue #189's "
            "exact-vs-partial contract and needs its own decision, not a silent change here."
        )

    def test_concept_mode_scope_filters_without_reordering(self, tmp_path, capsys, monkeypatch):
        """The #146 contract survives the mechanism change: two members keep
        their relative `(-matched_concepts, published)` order under the scope.

        The dates are deliberately set so that concept count and publish date
        DISAGREE about the order. With the strong member also being the older
        one, a stray ascending re-sort by `published` would produce the same
        output as the correct key and the test would pass on a real regression
        (found by an executing test reviewer). Here only the concept-count key
        yields this order, so both a reversal and a date-only re-sort fail.
        """
        concepts = {
            "c.a0": {"preferred_label": "agents overview", "aliases": [], "video_count": 2},
            "c.a1": {"preferred_label": "agents basics", "aliases": [], "video_count": 2},
        }
        (tmp_path / "taxonomy.json").write_text(
            json.dumps({"version": 1, "built_from": 2, "concepts": concepts}), encoding="utf-8"
        )
        # `strongvid001` matches two concepts, `weakvid00001` one, so the
        # relevance sort must keep strong first inside the scope too - even
        # though strong is the NEWER of the two.
        write_meta(tmp_path, "demo", "p-strong", video_id="strongvid001", title="strong member", published="2026-09-01")
        (tmp_path / "demo" / "p-strong.concepts.json").write_text(
            json.dumps({"video_id": "strongvid001", "concepts": [{"concept_id": "c.a0"}, {"concept_id": "c.a1"}]}),
            encoding="utf-8",
        )
        write_meta(tmp_path, "demo", "p-weak", video_id="weakvid00001", title="weak member", published="2026-01-01")
        (tmp_path / "demo" / "p-weak.concepts.json").write_text(
            json.dumps({"video_id": "weakvid00001", "concepts": [{"concept_id": "c.a0"}]}), encoding="utf-8"
        )
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - strongvid001\n  - weakvid00001\n")
        write_topics(tmp_path, build_topics(tmp_path))

        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        args = argparse.Namespace(query="agents", channel=None, limit=10, vector=False, since=None, topic="fde")
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert out.index("strong member") < out.index("weak member")

    def test_vector_mode_pushes_the_scope_into_the_index_predicate(self, tmp_path, capsys, monkeypatch):
        """Driven through the real `hybrid_search` with only the LanceDB
        connection and the Voyage embed stubbed, so the `where` clause the
        production code builds is the thing asserted - not a pre-canned return
        from a stubbed `hybrid_search`.

        The stub table returns the member row ONLY when the predicate names it,
        which is what the real index does. A post-filter implementation would
        pass no predicate, get every row back, and rank the member 41st behind
        the out-of-topic chunks - failing at `--limit 1`.
        """
        member_id = "vecmember01"
        write_meta(tmp_path, "demo", "p-member", video_id=member_id, title="topic talk", published="2026-02-01")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter=f"video_ids:\n  - {member_id}\n")
        write_topics(tmp_path, build_topics(tmp_path))

        def _row(vid, title, relevance):
            return {
                "text": f"chunk for {vid}",
                "timestamp": "00:00:00",
                "timestamp_seconds": 0,
                "video_id": vid,
                "channel": "demo",
                "title": title,
                "published": "2026-01-01",
                "source_file": f"{vid}.transcript.md",
                "concept_ids": "[]",
                "_relevance_score": relevance,
            }

        # Every outside chunk outranks the member, and there are far more of
        # them than any plausible multiplier would have covered.
        all_rows = [_row(f"vecoutside{i:02d}", f"outside talk {i}", 1.0 - i * 0.001) for i in range(40)]
        all_rows.append(_row(member_id, "topic talk", 0.5))
        captured = {}

        class _RowBuilder:
            def vector(self, _v):
                return self

            def text(self, _q):
                return self

            def limit(self, n):
                captured["limit"] = n
                return self

            def where(self, clause):
                captured["where"] = clause
                return self

            def to_pandas(self):
                clause = captured.get("where")
                if clause and "video_id IN" in clause:
                    ids = {frag.strip().strip("'") for frag in clause.split("IN (", 1)[1].rstrip(")").split(",")}
                    return pd.DataFrame([r for r in all_rows if r["video_id"] in ids])
                return pd.DataFrame(all_rows)

        class _RowTable:
            def search(self, **_kw):
                return _RowBuilder()

        class _RowDB:
            def list_tables(self):
                return SimpleNamespace(tables=[video_intel.LANCEDB_TABLE])

            def open_table(self, _n):
                return _RowTable()

        monkeypatch.setattr(
            video_intel, "require_lancedb", lambda: SimpleNamespace(connect=lambda *_a, **_kw: _RowDB())
        )
        monkeypatch.setattr(
            video_intel,
            "require_voyageai",
            lambda: SimpleNamespace(
                Client=lambda: SimpleNamespace(embed=lambda *_a, **_kw: SimpleNamespace(embeddings=[[0.0]]))
            ),
        )
        monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)

        args = argparse.Namespace(
            query="agents",
            channel=None,
            limit=1,
            vector=True,
            since=None,
            topic="fde",
            preview=False,
            min_relevance=0.0,
            no_expand=True,
        )
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert f"video_id IN ('{member_id}')" in captured["where"], "the scope must reach the index, not a post-filter"
        assert "topic talk" in out
        assert "outside talk" not in out

    def test_vector_mode_passes_the_users_own_limit_with_no_multiplier(self, tmp_path, capsys, monkeypatch):
        """R4: the limit reaching `hybrid_search` is the user's. Depth on the
        scoped path comes from `hybrid_search`'s own scope-sized candidate pool
        (issue #188), never from a caller-side multiplier."""
        write_meta(tmp_path, "demo", "p1", video_id="insidevid12", title="Anchor talk", published="2026-08-01")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - insidevid12\n")
        write_topics(tmp_path, build_topics(tmp_path))
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        captured = {}

        def fake_hybrid(_out, _query, **kw):
            captured.update(kw)
            return [
                {
                    "video_id": "insidevid12",
                    "channel": "demo",
                    "title": "Anchor talk",
                    "published": "2026-08-01",
                    "timestamp": "01:00",
                    "timestamp_seconds": 60,
                    "relevance": 0.5,
                    "text": "anchor chunk",
                    "source_file": "demo/p1.transcript.md",
                }
            ]

        monkeypatch.setattr(video_intel, "hybrid_search", fake_hybrid)
        args = argparse.Namespace(
            query="agents",
            channel=None,
            limit=7,
            vector=True,
            since=None,
            topic="fde",
            preview=False,
            min_relevance=0.0,
            no_expand=True,
        )
        cmd_search(args, {})
        assert captured["limit"] == 7
        assert captured["video_ids_filter"] == {"insidevid12"}

    def test_without_a_topic_neither_mode_passes_a_filter(self, tmp_path, capsys, monkeypatch):
        """The unscoped path must be byte-identical to pre-#203: no predicate,
        no multiplier, the user's own limit."""
        write_meta(tmp_path, "demo", "p1", video_id="insidevid12", title="Anchor talk", published="2026-08-01")
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        captured = {}

        def fake_hybrid(_out, _query, **kw):
            captured.update(kw)
            return []

        monkeypatch.setattr(video_intel, "hybrid_search", fake_hybrid)
        args = argparse.Namespace(
            query="agents",
            channel=None,
            limit=6,
            vector=True,
            since=None,
            topic=None,
            preview=False,
            min_relevance=0.0,
            no_expand=True,
        )
        cmd_search(args, {})
        assert captured["limit"] == 6
        assert captured["video_ids_filter"] is None
        assert "Is the index built?" in capsys.readouterr().out


class TestSearchVectorTopicBeltAndComposition:
    """Caller-level coverage for the `search --vector --topic` surface.

    Issue #203's KTD3 claims one belt shared with `nugget` so the two surfaces
    cannot drift. `nugget` had a caller-level leak test since #188; `search`
    did not, and an executing test reviewer proved the gap by DELETING the
    `drop_topic_leaks` call from `cmd_search`'s vector branch and watching all
    148 tests stay green. A helper that is unit-tested but never proven to be
    CALLED is the same blind spot as a stub agreeing with its own assertion.
    """

    def _corpus(self, tmp_path):
        write_meta(tmp_path, "demo", "p1", video_id="insidevid12", title="Anchor talk", published="2026-08-01")
        write_meta(tmp_path, "other", "p2", video_id="outsidevid1", title="Louder talk", published="2026-07-01")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - insidevid12\n")
        write_topics(tmp_path, build_topics(tmp_path))

    def _hit(self, vid, channel, title, text, published="2026-08-01"):
        return {
            "video_id": vid,
            "channel": channel,
            "title": title,
            "published": published,
            "timestamp": "01:00",
            "timestamp_seconds": 60,
            "relevance": 0.5,
            "text": text,
            "source_file": f"{channel}/x.transcript.md",
        }

    def _args(self, **over):
        base = dict(
            query="thinking partner",
            channel=None,
            limit=5,
            vector=True,
            since=None,
            topic="fde",
            preview=True,
            min_relevance=0.0,
            no_expand=True,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def test_a_leak_past_the_index_filter_is_dropped_with_a_warning(self, tmp_path, capsys, monkeypatch, caplog):
        """Mirror of `TestNuggetTopicScoping::test_a_leak_past_the_index_filter_is_dropped_with_a_warning`.

        The predicate owns membership, but if it ever silently stops filtering
        the leak must not reach a result list the operator will read as "my
        topic said this". Falsified by deleting the `drop_topic_leaks` call
        from the vector branch: this test fails, the rest stay green.
        """
        self._corpus(tmp_path)
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setattr(
            video_intel,
            "hybrid_search",
            lambda *_a, **_kw: [
                self._hit("insidevid12", "demo", "Anchor talk", "anchor chunk"),
                self._hit("outsidevid1", "other", "Louder talk", "leaked chunk"),
            ],
        )
        with caplog.at_level("WARNING"):
            cmd_search(self._args(), {})
        out = capsys.readouterr().out
        assert "Anchor talk" in out
        assert "Louder talk" not in out, "a hit outside the topic must never render under a --topic search"
        assert "leaked chunk" not in out
        assert "outside the topic despite the index-level filter" in caplog.text

    def test_a_total_leak_leaves_nothing_and_says_so_without_blaming_the_index(
        self, tmp_path, capsys, monkeypatch, caplog
    ):
        """The belt runs BEFORE the empty check, so a fully-leaking retrieval
        ends as an empty scoped result, not as a list of out-of-topic hits."""
        self._corpus(tmp_path)
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setattr(
            video_intel,
            "hybrid_search",
            lambda *_a, **_kw: [self._hit("outsidevid1", "other", "Louder talk", "leaked chunk")],
        )
        with caplog.at_level("WARNING"):
            cmd_search(self._args(), {})
        out = capsys.readouterr().out
        assert "Louder talk" not in out
        assert "No excerpts in topic 'fde'" in out
        assert "Is the index built?" not in out
        assert "outside the topic despite the index-level filter" in caplog.text

    def test_topic_composes_with_channel_and_since_in_vector_mode(self, tmp_path, capsys, monkeypatch):
        """SKILL.md promises `--topic` composes with `--channel` and `--since`.
        The #203 refactor routes the topic scope through the same builder that
        carries those two, so pin that all three reach `hybrid_search` together
        rather than one silently replacing another."""
        self._corpus(tmp_path)
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        captured = {}

        def fake_hybrid(_out, _query, **kw):
            captured.update(kw)
            return [self._hit("insidevid12", "demo", "Anchor talk", "anchor chunk")]

        monkeypatch.setattr(video_intel, "hybrid_search", fake_hybrid)
        cmd_search(self._args(channel="demo", since="2026-01-01"), {})
        assert captured["video_ids_filter"] == {"insidevid12"}
        assert captured["channel_filter"] == "demo"
        assert captured["since_iso"] == "2026-01-01"
        assert captured["limit"] == 5

    def test_topic_composes_with_channel_and_since_in_concept_mode(self, tmp_path, capsys, monkeypatch):
        """Same promise on the concept surface, driven through the real
        `search_corpus` rather than a stub: `--channel` and `--since` must
        still narrow the scoped set, and `--since` must be applied before the
        pre-scope count the emptied message reports."""
        (tmp_path / "taxonomy.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "built_from": 3,
                    "concepts": {"c.agents": {"preferred_label": "agents", "aliases": [], "video_count": 3}},
                }
            ),
            encoding="utf-8",
        )
        members = [
            ("demo", "p-old", "oldmembervid", "old member", "2026-01-01"),
            ("demo", "p-new", "newmembervid", "new member", "2026-08-01"),
            ("other", "p-off", "offchannelvid", "other channel member", "2026-08-01"),
        ]
        for channel, prefix, vid, title, published in members:
            write_meta(tmp_path, channel, prefix, video_id=vid, title=title, published=published)
            (tmp_path / channel / f"{prefix}.concepts.json").write_text(
                json.dumps({"video_id": vid, "concepts": [{"concept_id": "c.agents"}]}), encoding="utf-8"
            )
        ids = "".join(f"  - {m[2]}\n" for m in members)
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter=f"video_ids:\n{ids}")
        write_topics(tmp_path, build_topics(tmp_path))
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)

        cmd_search(
            argparse.Namespace(query="agents", channel="demo", limit=10, vector=False, since="2026-06-01", topic="fde"),
            {},
        )
        out = capsys.readouterr().out
        assert "new member" in out
        assert "old member" not in out, "--since must still narrow inside the topic scope"
        assert "other channel member" not in out, "--channel must still narrow inside the topic scope"


class TestSearchCorpusVideoIdsFilter:
    """The concept-mode half of the #203 scope, at the function boundary."""

    def _corpus(self, tmp_path):
        (tmp_path / "taxonomy.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "built_from": 2,
                    "concepts": {"c.agents": {"preferred_label": "agents", "aliases": [], "video_count": 2}},
                }
            ),
            encoding="utf-8",
        )
        for prefix, vid in (("p1", "insidevid12"), ("p2", "outsidevid1")):
            write_meta(tmp_path, "demo", prefix, video_id=vid, title=f"talk {vid}", published="2026-05-01")
            (tmp_path / "demo" / f"{prefix}.concepts.json").write_text(
                json.dumps({"video_id": vid, "concepts": [{"concept_id": "c.agents"}]}), encoding="utf-8"
            )

    def test_none_means_no_filter(self, tmp_path):
        self._corpus(tmp_path)
        res = video_intel.search_corpus(tmp_path, "agents", video_ids_filter=None)
        assert {v["video_id"] for v in res["videos"]} == {"insidevid12", "outsidevid1"}

    def test_a_set_narrows_to_its_members(self, tmp_path):
        self._corpus(tmp_path)
        res = video_intel.search_corpus(tmp_path, "agents", video_ids_filter={"insidevid12"})
        assert [v["video_id"] for v in res["videos"]] == ["insidevid12"]

    def test_an_empty_set_matches_nothing_never_everything(self, tmp_path):
        """Same convention as `video_ids_predicate`: "filter to no videos" must
        never silently become "no filter"."""
        self._corpus(tmp_path)
        res = video_intel.search_corpus(tmp_path, "agents", video_ids_filter=set())
        assert res["videos"] == []

    def test_the_pre_scope_count_is_reported_for_the_emptied_message(self, tmp_path):
        self._corpus(tmp_path)
        res = video_intel.search_corpus(tmp_path, "agents", video_ids_filter={"absentvid12"})
        assert res["videos"] == []
        assert res["videos_before_topic_filter"] == 2

    def test_the_scope_is_applied_before_the_limit_cap(self, tmp_path):
        """The load-bearing ordering. With the cap applied first, a member
        outside the top-`limit` would be gone before the scope ever ran - the
        pre-#203 defect that the over-fetch multiplier only papered over."""
        self._corpus(tmp_path)
        capped = video_intel.search_corpus(tmp_path, "agents", limit=1)
        assert [v["video_id"] for v in capped["videos"]] == ["insidevid12"]
        scoped = video_intel.search_corpus(tmp_path, "agents", limit=1, video_ids_filter={"outsidevid1"})
        assert [v["video_id"] for v in scoped["videos"]] == ["outsidevid1"]


class TestSharedTopicScopeHelpers:
    """One belt and one no-match message, shared by `search --vector` and
    `nugget` (issue #203), for the same reason `resolve_topic_filter` is one
    resolver: two copies drift."""

    def _hit(self, vid):
        return {"video_id": vid, "text": f"chunk {vid}"}

    def test_no_leak_returns_the_hits_unchanged(self, caplog):
        hits = [self._hit("insidevid12")]
        with caplog.at_level("WARNING"):
            assert video_intel.drop_topic_leaks(hits, {"insidevid12"}, "fde") == hits
        assert "outside the topic" not in caplog.text

    def test_a_leak_is_dropped_and_warned_about(self, caplog):
        hits = [self._hit("insidevid12"), self._hit("outsidevid1")]
        with caplog.at_level("WARNING"):
            kept = video_intel.drop_topic_leaks(hits, {"insidevid12"}, "fde")
        assert [h["video_id"] for h in kept] == ["insidevid12"]
        assert (
            "topic 'fde': 1 retrieved chunks were outside the topic despite the index-level filter; dropping them"
            in caplog.text
        )

    def test_the_no_match_message_names_a_remedy_that_can_work(self):
        msg = video_intel.topic_no_match_message("thinking partner", "fde")
        assert "No excerpts in topic 'fde'" in msg
        assert "search --topic fde" in msg
        # An exact scope is applied before the cap, so a bigger cap cannot
        # recover a member that did not match. Naming it would be false advice.
        assert "--limit" not in msg
        assert "index" not in msg


# ---------------------------------------------------------------------------
# T22 - classification, and the corpus-level shape
# ---------------------------------------------------------------------------


class TestCommandClassification:
    def test_topics_build_snapshots_the_config(self):
        """T22. It writes topics.json at the corpus root, so it takes the
        default reading of amendment 6 rather than the WRITES_BUT_EXEMPT escape.
        `tests/test_config_backup.py::TestEveryMutatingCommandSnapshots` fails
        on any unclassified subcommand, so this is belt-and-braces on intent.
        """
        assert "topics-build" in CONFIG_BACKUP_COMMANDS

    def test_dry_run_writes_nothing(self, tmp_path, capsys):
        write_meta(tmp_path, "demo", "p", video_id="dryvid12345", extra={"topics": ["fde"]})
        cmd_topics_build(argparse.Namespace(dry_run=True), {"output_dir": str(tmp_path)})
        assert not (tmp_path / TOPICS_FILENAME).exists()
        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "fde" in out

    def test_build_reports_topics_memberships_and_unresolved(self, tmp_path, capsys):
        write_meta(tmp_path, "demo", "p", video_id="realvid1234")
        write_briefing(
            tmp_path,
            "fde/2026-08-22-a.md",
            front_matter="video_ids:\n  - realvid1234\n  - ghostvid123\n",
        )
        cmd_topics_build(argparse.Namespace(dry_run=False), {"output_dir": str(tmp_path)})
        out = capsys.readouterr().out
        assert "1 topics, 2 memberships, 1 unresolved video ids" in out
        assert (tmp_path / TOPICS_FILENAME).exists()

    def test_topics_build_needs_no_channels_config(self, tmp_path):
        """A derived rebuild over corpus artifacts, like `taxonomy-build`."""
        write_meta(tmp_path, "demo", "p", video_id="nochannel12", extra={"topics": ["fde"]})
        cmd_topics_build(argparse.Namespace(dry_run=False), {"output_dir": str(tmp_path)})
        assert (tmp_path / TOPICS_FILENAME).exists()


class TestDuplicateMetaTopicUnion:
    def test_a_title_rotation_loser_does_not_lose_its_topic(self, tmp_path):
        """The completeness tie-break must not delete a membership.

        An operator can stamp `--topic` before a retitle, leaving the tag on the
        meta that later loses `collect_corpus_videos`' most-complete tie-break.
        """
        write_meta(tmp_path, "demo", "old-title", video_id="dupmeta1234", extra={"topics": ["fde"]})
        write_meta(tmp_path, "demo", "new-title", video_id="dupmeta1234", extra={"topics": ["sales"]})
        (tmp_path / "demo" / "new-title.concepts.json").write_text("{}", encoding="utf-8")  # the winner

        topics = build_topics(tmp_path)
        assert sorted(topics["topics"]) == ["fde", "sales"]
        assert topics["channels"] == {"demo": ["fde", "sales"]}


# ---------------------------------------------------------------------------
# Second review round (correctness + adversarial), findings 1-7
# ---------------------------------------------------------------------------


class TestStampFillsAbsentIdentityWithoutDowngrading:
    """Finding 1. `meta.update(identity)` REPLACED healthy fields.

    The caller's `video` dict is not authoritative about a video already on
    disk: on a rotated title it carries the CURRENT title while the meta
    records the one its artifacts are named after, and a canonical `video_url`
    the meta already resolved can differ from the one the caller built. The
    falsy-drop inside `_transcript_identity_fields` only blocks an EMPTY value
    from clobbering, never a different non-empty one, so a free provenance tag
    silently downgraded real identity data.
    """

    def _rotated_video(self):
        return {
            "video_id": "downgrade12",
            "url": "https://www.youtube.com/watch?v=downgrade12",
            "title": "The Current Rotated Title",
            "published": "2026-09-09",
        }

    def test_a_healthy_on_disk_identity_survives_a_stamp(self, tmp_path):
        meta_path = write_meta(
            tmp_path,
            "demo",
            "p",
            video_id="downgrade12",
            title="The Original Title",
            published="2026-05-01",
        )
        stamp_video_topics(meta_path, self._rotated_video(), "demo", ["fde"])

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["title"] == "The Original Title"
        assert meta["published"] == "2026-05-01"
        assert meta["topics"] == ["fde"]

    def test_an_absent_identity_key_is_still_filled(self, tmp_path):
        """The gap-filling half: a partial meta gains what it is missing."""
        meta_path = write_meta(tmp_path, "demo", "p", video_id="downgrade12")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        del meta["published"]
        meta["title"] = ""
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        stamp_video_topics(meta_path, self._rotated_video(), "demo", ["fde"])

        after = json.loads(meta_path.read_text(encoding="utf-8"))
        assert after["published"] == "2026-09-09"
        assert after["title"] == "The Current Rotated Title"

    def test_quarantined_read_still_gets_full_identity(self, tmp_path):
        """Issue #66 regression guard. A `{}` read makes these fields the WHOLE
        file, so gap-filling must be indistinguishable from a full stamp there.
        """
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True)
        meta_path = channel_dir / "p.meta.json"
        meta_path.write_text("{ this is not json", encoding="utf-8")

        stamp_video_topics(meta_path, self._rotated_video(), "demo", ["fde"])

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["video_id"] == "downgrade12"
        assert meta["channel"] == "demo"
        assert meta["title"] == "The Current Rotated Title"
        assert meta["published"] == "2026-09-09"
        assert meta["video_url"].endswith("downgrade12")


class TestTornMetaDoesNotKillTheBuild:
    """Finding 2. `UnicodeDecodeError` subclasses ValueError, not OSError.

    A meta truncated mid-multibyte-character is the normal shape of a torn
    write on a corpus carrying Cyrillic/BCS titles, and it escaped the
    `(json.JSONDecodeError, OSError)` handler in the corpus walker - killing
    `topics-build` (and every other caller) on one bad file. Issue #124.
    """

    def _plant_torn_meta(self, tmp_path):
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True, exist_ok=True)
        cyrillic = "Интервју"
        raw = json.dumps(
            {"video_id": "tornvid1234", "title": cyrillic, "topics": ["fde"]},
            ensure_ascii=False,
        ).encode("utf-8")
        # Slice one byte into a multibyte character so the file is undecodable
        # rather than merely unparseable - that is what a torn write looks like.
        torn = raw[: raw.index(cyrillic.encode("utf-8")) + 5]
        (channel_dir / "torn.meta.json").write_bytes(torn)
        with pytest.raises(UnicodeDecodeError):
            torn.decode("utf-8")

    def test_build_completes_and_still_reports_the_other_videos(self, tmp_path):
        self._plant_torn_meta(tmp_path)
        write_meta(tmp_path, "demo", "healthy", video_id="healthyvid1", extra={"topics": ["sales"]})

        topics = build_topics(tmp_path)

        assert sorted(topics["topics"]) == ["sales"]
        assert video_entry(topics, "sales", "healthyvid1")["channel"] == "demo"

    def test_topics_build_command_does_not_traceback(self, tmp_path, capsys):
        self._plant_torn_meta(tmp_path)
        write_meta(tmp_path, "demo", "healthy", video_id="healthyvid1", extra={"topics": ["sales"]})

        cmd_topics_build(argparse.Namespace(dry_run=False), {"output_dir": str(tmp_path)})

        assert (tmp_path / TOPICS_FILENAME).exists()
        assert "sales" in capsys.readouterr().out


class TestDedupeApplyPreservesTopics:
    """Finding 3. `_apply_dedupe_group` merged `alt_titles` and
    `modes_completed` but not `topics`, so a topic stamped before a retitle was
    deleted along with the loser meta - permanently, on the next `dedupe
    --apply`.
    """

    def _group(self, tmp_path, *, loser_topics, canonical_topics=None):
        channel_dir = tmp_path / "ch"
        channel_dir.mkdir(parents=True, exist_ok=True)
        for prefix, processed, topics in (
            ("2026-04-15-earlier", "2026-04-16T00:00:00+00:00", loser_topics),
            ("2026-04-15-later", "2026-04-18T00:00:00+00:00", canonical_topics),
        ):
            extra = {"processed": processed, "modes_completed": ["scan", "transcript"]}
            if topics is not None:
                extra["topics"] = topics
            write_meta(tmp_path, "ch", prefix, video_id="dedupevid12", title=prefix, extra=extra)
        return channel_dir

    def _config(self, tmp_path):
        return {"output_dir": str(tmp_path), "channels": [{"name": "ch", "url": "https://example.com/ch"}]}

    def test_a_loser_only_topic_survives_on_the_canonical(self, tmp_path):
        channel_dir = self._group(tmp_path, loser_topics=["fde"])

        video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), self._config(tmp_path))

        assert not (channel_dir / "2026-04-15-earlier.meta.json").exists()
        canonical = json.loads((channel_dir / "2026-04-15-later.meta.json").read_text(encoding="utf-8"))
        assert canonical["topics"] == ["fde"]

    def test_both_sides_union_and_normalize(self, tmp_path):
        channel_dir = self._group(tmp_path, loser_topics="FDE", canonical_topics=["sales"])

        video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), self._config(tmp_path))

        canonical = json.loads((channel_dir / "2026-04-15-later.meta.json").read_text(encoding="utf-8"))
        assert canonical["topics"] == ["fde", "sales"]

    def test_a_group_with_no_topics_gains_no_key(self, tmp_path):
        channel_dir = self._group(tmp_path, loser_topics=None)

        video_intel.cmd_dedupe(argparse.Namespace(channel=None, apply=True), self._config(tmp_path))

        canonical = json.loads((channel_dir / "2026-04-15-later.meta.json").read_text(encoding="utf-8"))
        assert "topics" not in canonical


class TestTransientStampErrorDefersInsteadOfAborting:
    """Finding 4. The immediate stamp had no handler, so a transient read on a
    cloud-synced mount propagated out of `register_topic_stamp_target` and
    killed the command - for a provenance tag. `stamp_video_topics` keeps
    raising (that contract is about not overwriting a file we failed to read);
    the recovery is to defer to the pending queue and retry at flush.
    """

    def test_an_os_error_defers_to_the_pending_queue(self, tmp_path, monkeypatch, caplog):
        meta_path = write_meta(tmp_path, "demo", "p", video_id="transient12")
        real_read = Path.read_bytes
        failures = []

        def flaky(self, *a, **kw):
            if self == meta_path and not failures:
                failures.append(self)
                raise OSError("transient cloud-mount read error")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_bytes", flaky)
        video = {"video_id": "transient12", "url": "u", "title": "T", "published": "2026-05-01"}
        with caplog.at_level("WARNING"):
            video_intel.register_topic_stamp_target(
                argparse.Namespace(topic=["fde"]), video, tmp_path / "demo", "p", "demo"
            )

        assert failures, "the immediate stamp must actually have hit the injected error"
        assert video_intel._PENDING_TOPIC_STAMPS, "a failed immediate stamp must stay pending"
        assert "deferring" in caplog.text

        video_intel.flush_topic_stamps()
        assert json.loads(meta_path.read_text(encoding="utf-8"))["topics"] == ["fde"]

    def test_the_writer_itself_still_raises_on_an_os_error(self, tmp_path, monkeypatch):
        """The guard belongs at the call site, not in the writer: a read we
        merely failed to PERFORM must not license overwriting a healthy meta.
        """
        meta_path = write_meta(tmp_path, "demo", "p", video_id="transient12", extra={"alt_titles": ["keep me"]})
        real_read = Path.read_bytes

        def always_fails(self, *a, **kw):
            if self == meta_path:
                raise OSError("transient cloud-mount read error")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_bytes", always_fails)
        with pytest.raises(OSError):
            stamp_video_topics(meta_path, {"video_id": "transient12"}, "demo", ["fde"])


class TestManualTranscriptUrlStampsTheIndexedPrefix:
    """Finding 5. `cmd_transcript --url` handed `video_file_prefix(video)`
    straight to the stamp, so on a title-rotated video the target meta path did
    not exist, the stamp deferred, and it then failed at flush. video_id is the
    identity, slug is decoration - `cmd_mindmap` already makes this lookup.
    """

    def test_a_rotated_title_stamps_the_existing_meta(self, tmp_path, monkeypatch):
        output_dir = tmp_path / "corpus"
        video_id = "rotatedurl1"
        rotated_prefix = "2026-01-01-old-seo-title"
        meta_path = write_meta(
            output_dir,
            "demo",
            rotated_prefix,
            video_id=video_id,
            title="Old SEO Title",
            published="2026-01-01",
        )
        current = {
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": "The Brand New Title",
            "published": "2026-01-01",
        }
        # Derived independently of the writer above (PR #136): the checker's
        # path and the writer's are compared, never shared.
        assert video_intel.video_file_prefix(current) != rotated_prefix

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(video_intel, "require_gemini", lambda: (MagicMock(), None))
        monkeypatch.setattr(video_intel, "create_client", lambda _key: MagicMock())
        monkeypatch.setattr(video_intel, "resolve_model", lambda _a, _c: "stub-model")
        monkeypatch.setattr(video_intel, "load_prompt", lambda name: f"prompt-{name}")
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: output_dir)

        class _Stop(Exception):
            pass

        def stop(_video_id):
            raise _Stop()

        # The first call after the stamp registration, so the run ends without
        # a Gemini call while the real path up to the stamp has executed.
        monkeypatch.setattr(video_intel, "_lookup_was_livestream", stop)

        args = argparse.Namespace(
            file=None,
            url=f"https://www.youtube.com/watch?v={video_id}",
            channel="demo",
            title="The Brand New Title",
            date="2026-01-01",
            start=None,
            end=None,
            force=False,
            model=None,
            media_resolution="low",
            transcript_source=None,
            topic=["fde"],
        )
        with pytest.raises(_Stop):
            video_intel.cmd_transcript(args, {})

        assert json.loads(meta_path.read_text(encoding="utf-8"))["topics"] == ["fde"]
        phantom = output_dir / "demo" / f"{video_intel.video_file_prefix(current)}.meta.json"
        assert not phantom.exists(), "the stamp must never manufacture a second meta for one video_id"


class TestEmptyTopicResultNamesTheRightRemedy:
    """Finding 6. An emptied `--topic` post-filter reported itself as a missing
    index, sending the operator to rebuild an index that had just returned
    results - and training them to distrust the message.
    """

    def _corpus(self, tmp_path):
        (tmp_path / "taxonomy.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "built_from": 1,
                    "concepts": {"c.agents": {"preferred_label": "agents", "aliases": [], "video_count": 1}},
                }
            ),
            encoding="utf-8",
        )
        write_meta(tmp_path, "demo", "p1", video_id="outsidevid1", title="talk outsidevid1")
        (tmp_path / "demo" / "p1.concepts.json").write_text(
            json.dumps({"video_id": "outsidevid1", "concepts": [{"concept_id": "c.agents"}]}), encoding="utf-8"
        )
        # A topic whose only member is not in the corpus, so the post-filter
        # empties a genuinely non-empty ranking.
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - absentvid12\n")
        write_topics(tmp_path, build_topics(tmp_path))

    def _run(self, tmp_path, monkeypatch, capsys, *, topic):
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        args = argparse.Namespace(query="agents", channel=None, limit=None, vector=False, since=None, topic=topic)
        cmd_search(args, {})
        return capsys.readouterr().out

    def test_concept_mode_distinguishes_the_two_empties(self, tmp_path, monkeypatch, capsys):
        """Concept mode keeps this message after issue #203: it still matches
        the whole corpus by concept and THEN narrows, so "the ranking found N,
        none in this topic" remains both true and diagnostic there."""
        self._corpus(tmp_path)
        filtered = self._run(tmp_path, monkeypatch, capsys, topic="fde")
        assert "the topic filter removed all of them" in filtered
        assert "search --topic fde" in filtered
        assert "topics-build" in filtered

        unfiltered = self._run(tmp_path, monkeypatch, capsys, topic=None)
        assert "the topic filter removed all of them" not in unfiltered
        assert filtered != unfiltered

    def test_the_emptied_message_no_longer_offers_the_limit_remedy(self, tmp_path, monkeypatch, capsys):
        """Retired with the over-fetch (issue #203). The scope is applied
        before the cap now, so a bigger `--limit` cannot recover a member that
        the scope already includes and the query did not match. Naming it would
        be false advice - the same "trains them to distrust the message"
        failure this whole class exists to prevent."""
        self._corpus(tmp_path)
        filtered = self._run(tmp_path, monkeypatch, capsys, topic="fde")
        assert "--limit" not in filtered

    def test_vector_mode_does_not_blame_the_index(self, tmp_path, monkeypatch, capsys):
        """Issue #203 changed WHAT an empty scoped vector search means, not
        whether it blames the index. Retrieval is scoped at the index query
        now, so there is no post-filter to report - an empty result means
        nothing inside the topic matched. Both branches must stay
        distinguishable and neither may point at a rebuild of the index that
        just answered for the rest of the corpus.
        """
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        write_meta(tmp_path, "demo", "p1", video_id="outsidevid1", title="talk outsidevid1")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - absentvid12\n")
        write_topics(tmp_path, build_topics(tmp_path))

        def _args(topic):
            return argparse.Namespace(
                query="agents",
                channel=None,
                limit=None,
                vector=True,
                since=None,
                topic=topic,
                preview=False,
                min_relevance=0.0,
                no_expand=True,
            )

        monkeypatch.setattr(video_intel, "hybrid_search", lambda *_a, **_kw: [])
        cmd_search(_args("fde"), {})
        scoped = capsys.readouterr().out
        assert "Is the index built?" not in scoped
        assert "the topic filter removed all of them" not in scoped
        assert "No excerpts in topic 'fde'" in scoped

        cmd_search(_args(None), {})
        unscoped = capsys.readouterr().out
        assert "Is the index built?" in unscoped
        assert unscoped != scoped

    def test_the_helper_returns_none_for_a_genuinely_empty_ranking(self):
        assert video_intel.topic_filter_emptied_message("q", "fde", 0) is None
        assert video_intel.topic_filter_emptied_message("q", None, 5) is None
        assert video_intel.topic_filter_emptied_message("q", "fde", 5) is not None


class TestStandaloneStampWarnsItIsInvisible:
    """Finding 7. `collect_corpus_videos` skips `_`-prefixed dirs, which
    `_briefings` / `_headlines` depend on, so a `--topic` stamp on a
    `_standalone` video lands in the meta and can never reach `topics.json`.
    Loosening the underscore rule was rejected as out of scope, so the operator
    gets the one thing the meta cannot give them: a signal.
    """

    def test_the_stamp_is_written_and_the_warning_names_the_recovery(self, tmp_path, caplog):
        meta_path = write_meta(tmp_path, "loose", "p", video_id="standalone1")
        video = {"video_id": "standalone1", "url": "u", "title": "T", "published": "2026-05-01"}

        with caplog.at_level("WARNING"):
            merged = stamp_video_topics(meta_path, video, video_intel.STANDALONE_CHANNEL, ["fde"])

        assert merged == ["fde"]
        assert json.loads(meta_path.read_text(encoding="utf-8"))["topics"] == ["fde"]
        assert "topics-build" in caplog.text
        assert "--channel" in caplog.text

    def test_a_real_channel_stamp_is_silent(self, tmp_path, caplog):
        meta_path = write_meta(tmp_path, "demo", "p", video_id="standalone1")
        video = {"video_id": "standalone1", "url": "u", "title": "T", "published": "2026-05-01"}

        with caplog.at_level("WARNING"):
            stamp_video_topics(meta_path, video, "demo", ["fde"])

        assert "not visible to topics-build" not in caplog.text


# ---------------------------------------------------------------------------
# Peer-review finding 1 - a stamp with no video_id is invisible to the join
# ---------------------------------------------------------------------------


class TestStampWithoutAVideoIdWarns:
    """`resolve_local_file_identity` can resolve no `video_id` at all.

    A plain `ordinary-name.mp4 --channel demo` has no sibling meta, no dedup
    hit and no `--video-id`, so the caller's video dict carries none.
    `_transcript_identity_fields` drops the falsy value (correctly - a stamp
    must never downgrade a healthy field), and the resulting meta reaches disk
    with no `video_id`. `build_topics` joins on that id, so the tag is
    permanently invisible and nothing downstream can repair it. The write still
    happens (the meta may hold other state) but the operator gets a signal.
    """

    def test_stamp_writes_the_topic_and_warns_that_the_join_cannot_see_it(self, tmp_path, caplog):
        channel_dir = tmp_path / "demo"
        channel_dir.mkdir(parents=True)
        meta_path = channel_dir / "ordinary-name.meta.json"
        meta_path.write_text("{ this is not json", encoding="utf-8")
        video = {"title": "ordinary-name", "published": "2026-08-22"}

        with caplog.at_level("WARNING"):
            merged = stamp_video_topics(meta_path, video, "demo", ["fde"])

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert merged == ["fde"]
        assert meta["topics"] == ["fde"], "the tag is still recorded - refusing would lose it outright"
        assert "video_id" not in meta
        assert "topics-build" in caplog.text
        assert "--video-id" in caplog.text

    def test_a_resolved_video_id_stamps_silently(self, tmp_path, caplog):
        meta_path = write_meta(tmp_path, "demo", "p", video_id="haveanid123")
        video = {"video_id": "haveanid123", "url": "u", "title": "T", "published": "2026-05-01"}

        with caplog.at_level("WARNING"):
            stamp_video_topics(meta_path, video, "demo", ["fde"])

        assert "--video-id" not in caplog.text

    def test_an_on_disk_video_id_is_enough_even_when_the_caller_has_none(self, tmp_path, caplog):
        """The check reads the MERGED meta, not the caller's dict.

        A backfill stamp on an existing healthy meta supplies no identity of
        its own, and warning there would be a false alarm on the flag's most
        common use.
        """
        meta_path = write_meta(tmp_path, "demo", "p", video_id="ondiskid123")

        with caplog.at_level("WARNING"):
            stamp_video_topics(meta_path, {"title": "T"}, "demo", ["fde"])

        assert "--video-id" not in caplog.text


# ---------------------------------------------------------------------------
# Peer-review finding 2 - one numeric video_id must not abort the build
# ---------------------------------------------------------------------------


class TestMalformedVideoIdDoesNotAbortTheBuild:
    """`sorted()` refuses to order an int against a str.

    One meta carrying `"video_id": 123` used to raise TypeError out of
    `build_topics`, taking every healthy video with it. The id is skipped with
    a WARNING naming the file - never coerced, because `str(123)` would merge a
    malformed meta into a real `"123"` identity.
    """

    def test_a_numeric_id_is_skipped_and_the_rest_of_the_corpus_builds(self, tmp_path, caplog):
        write_meta(tmp_path, "demo", "good", video_id="abc123", extra={"topics": ["fde"]})
        write_meta(tmp_path, "demo", "bad", video_id=123, extra={"topics": ["fde"]})

        with caplog.at_level("WARNING"):
            topics = build_topics(tmp_path)

        assert [v["video_id"] for v in topics["topics"]["fde"]["videos"]] == ["abc123"]
        assert topics["built_from"]["metas"] == 1
        assert "bad.meta.json" in caplog.text
        assert "video_id" in caplog.text

    def test_a_non_string_channel_falls_back_to_the_folder_name(self, tmp_path, caplog):
        """`channels` is sorted and rolled up too, so it needs the same guard."""
        write_meta(tmp_path, "demo", "good", video_id="abc123", extra={"topics": ["fde"], "channel": 7})

        with caplog.at_level("WARNING"):
            topics = build_topics(tmp_path)

        assert topics["topics"]["fde"]["channels"] == ["demo"]
        assert topics["channels"] == {"demo": ["fde"]}
        assert "channel" in caplog.text


# ---------------------------------------------------------------------------
# Peer-review finding 3 - YAML scalars are typed by the parser, not the operator
# ---------------------------------------------------------------------------


class TestYamlScalarVideoIdsAreNotVideoIds:
    """Unquoted `yes` parses to `True`, and `str(True)` invented a member.

    PyYAML returns bool / int / datetime.date / str for
    `[yes, 123, 2026-08-22, AbC_123-xYz]`. Coercing the first three produced
    ids like `"True"`, which then surfaced in `topics.json` as unresolved
    members - videos that never existed. `bool` subclasses `int`, so one
    `isinstance(..., str)` check covers all three shapes.
    """

    def test_only_the_real_string_id_survives_and_one_warning_names_the_file(self, tmp_path, caplog):
        write_briefing(
            tmp_path,
            "fde/2026-08-22-yaml.md",
            front_matter="video_ids: [yes, 123, 2026-08-22, AbC_123-xYz]\n",
        )

        with caplog.at_level("WARNING"):
            assertions = collect_briefing_topic_assertions(tmp_path / "_briefings")

        assert assertions["fde"]["fde/2026-08-22-yaml.md"]["video_ids"] == ["AbC_123-xYz"]
        warnings = [r for r in caplog.records if "video_ids" in r.getMessage()]
        assert len(warnings) == 1, "one line per file, not one per dropped entry"
        assert "fde/2026-08-22-yaml.md" in warnings[0].getMessage()
        assert "3" in warnings[0].getMessage()

    def test_no_phantom_member_reaches_topics_json(self, tmp_path):
        write_briefing(tmp_path, "fde/2026-08-22-yaml.md", front_matter="video_ids: [yes, AbC_123-xYz]\n")
        topics = build_topics(tmp_path)
        assert [v["video_id"] for v in topics["topics"]["fde"]["videos"]] == ["AbC_123-xYz"]

    def test_a_clean_list_logs_nothing(self, tmp_path, caplog):
        write_briefing(tmp_path, "fde/2026-08-22-clean.md", front_matter='video_ids: ["AbC_123-xYz"]\n')

        with caplog.at_level("WARNING"):
            collect_briefing_topic_assertions(tmp_path / "_briefings")

        assert caplog.text == ""


# ---------------------------------------------------------------------------
# Issue #188: query-less `search --topic` listing + `nugget --topic` scoping
# ---------------------------------------------------------------------------


class TestQueryLessTopicListing:
    """`search --topic <slug>` with no query lists the topic's members from
    topics.json alone - a pure read + render, no retrieval, no index needed."""

    def _corpus(self, tmp_path):
        write_meta(tmp_path, "demo", "p1", video_id="insidevid12", title="Newer member", published="2026-08-01")
        write_meta(tmp_path, "demo", "p2", video_id="insidevid34", title="Older member", published="2026-05-01")
        write_meta(tmp_path, "other", "p3", video_id="outsidevid1", title="Not in topic", published="2026-07-01")
        write_briefing(
            tmp_path,
            "fde/2026-08-22-a.md",
            front_matter="video_ids:\n  - insidevid12\n  - insidevid34\n  - ghostvid9999\n",
        )
        write_topics(tmp_path, build_topics(tmp_path))

    def _args(self, **over):
        base = dict(query=None, channel=None, limit=None, vector=False, since=None, topic="fde", preview=False)
        base.update(over)
        return argparse.Namespace(**base)

    def test_lists_every_member_newest_first_and_nothing_else(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_search(self._args(), {})
        out = capsys.readouterr().out
        assert "Newer member" in out
        assert "Older member" in out
        assert "Not in topic" not in out
        assert out.index("Newer member") < out.index("Older member")
        assert "https://www.youtube.com/watch?v=insidevid12" in out

    def test_unresolved_member_is_shown_as_unresolved(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_search(self._args(), {})
        out = capsys.readouterr().out
        assert "[unresolved] ghostvid9999" in out

    def test_listing_makes_no_retrieval_call(self, tmp_path, capsys, monkeypatch):
        """The listing is a read + render; a Voyage/LanceDB call would regress
        exactly the no-index reachability the issue asks for."""
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)

        def _boom(*_a, **_kw):
            raise AssertionError("retrieval must not run for a query-less listing")

        monkeypatch.setattr("video_intel.hybrid_search", _boom)
        monkeypatch.setattr("video_intel.search_corpus", _boom)
        cmd_search(self._args(), {})
        assert "Newer member" in capsys.readouterr().out

    def test_no_query_and_no_topic_exits_2(self, tmp_path, capsys, monkeypatch):
        """The pre-#188 missing-positional exit code was 2; assert the CODE,
        not just SystemExit, per the issue #185 lesson."""
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        with pytest.raises(SystemExit) as exc:
            cmd_search(self._args(topic=None), {})
        assert exc.value.code == 2
        assert "--topic" in capsys.readouterr().out

    def test_vector_without_query_exits_2_before_any_retrieval(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)

        def _boom(*_a, **_kw):
            raise AssertionError("hybrid_search must not run without a query")

        monkeypatch.setattr("video_intel.hybrid_search", _boom)
        with pytest.raises(SystemExit) as exc:
            cmd_search(self._args(vector=True), {})
        assert exc.value.code == 2

    def test_channel_since_and_limit_compose_as_filters(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_search(self._args(channel="demo", since="2026-06-01", limit=5), {})
        out = capsys.readouterr().out
        assert "Newer member" in out
        assert "Older member" not in out  # published before --since
        assert "[unresolved]" not in out  # unresolved has no channel; --channel filters it

    def test_limit_caps_and_names_the_remainder(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_search(self._args(limit=1), {})
        out = capsys.readouterr().out
        assert "Newer member" in out
        assert "Older member" not in out
        assert "more; raise --limit" in out

    def test_unknown_topic_fails_soft_with_the_known_slugs(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_search(self._args(topic="nope"), {})
        out = capsys.readouterr().out
        assert "No topic 'nope'" in out
        assert "known: fde" in out
        assert "Traceback" not in out

    def test_missing_topics_json_fails_soft_with_one_actionable_message(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        cmd_search(self._args(), {})
        out = capsys.readouterr().out
        assert out.count("topics-build") == 1
        assert "Traceback" not in out


class TestVideoIdsPredicate:
    """The SQL fragment scoping retrieval to a video-id set (issue #188).
    Same escaping rule as the channel predicate (issue #183 invariant 6)."""

    def test_ids_are_sorted_and_quoted(self):
        assert video_intel.video_ids_predicate({"b2", "a1"}) == "video_id IN ('a1', 'b2')"

    def test_a_quote_in_an_id_is_escaped_not_a_breakout(self):
        pred = video_intel.video_ids_predicate({"o'brien"})
        assert pred == "video_id IN ('o''brien')"

    def test_empty_and_non_string_ids_are_dropped(self):
        assert video_intel.video_ids_predicate({"", None, 123, "realvid1234"}) == "video_id IN ('realvid1234')"

    def test_an_empty_usable_set_matches_nothing_never_everything(self):
        """ "Filter to no videos" must never silently become "no filter" - and
        never "the blank-id rows" either: the live index carries ~750 chunks
        with a BLANK video_id, so the tempting `video_id IN ('')` matches all
        of THEM (executed proof, #188 review P2)."""
        assert video_intel.video_ids_predicate(set()) == "1 = 0"
        assert video_intel.video_ids_predicate({None, 5}) == "1 = 0"
        assert "IN ('')" not in video_intel.video_ids_predicate(set())


class TestNuggetTopicScoping:
    """`nugget --topic <slug>` narrows retrieval to the topic's members with
    the SAME resolver, belt check and no-match message as `search --vector
    --topic` (issue #146: a topic surface filters, never reorders). Neither
    surface has an over-fetch multiplier: nugget never did, and search's was
    retired with the post-filter it compensated for (issue #203)."""

    def _corpus(self, tmp_path):
        write_meta(tmp_path, "demo", "p1", video_id="insidevid12", title="Anchor talk", published="2026-08-01")
        write_meta(tmp_path, "other", "p2", video_id="outsidevid1", title="Louder talk", published="2026-07-01")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - insidevid12\n")
        write_topics(tmp_path, build_topics(tmp_path))

    def _hit(self, vid, channel, text, relevance):
        return {
            "video_id": vid,
            "channel": channel,
            "title": f"title {vid}",
            "published": "2026-08-01",
            "timestamp": "01:00",
            "timestamp_seconds": 60,
            "relevance": relevance,
            "text": text,
            "source_file": f"{channel}/x.transcript.md",
        }

    def _args(self, **over):
        base = dict(
            query="thinking partner",
            channel=None,
            limit=4,
            since=None,
            min_relevance=0.0,
            no_expand=False,
            output=None,
            no_save=False,
            topic="fde",
            model=None,
        )
        base.update(over)
        return argparse.Namespace(**base)

    def _run(self, tmp_path, monkeypatch, hits, args):
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        captured = {}

        def fake_hybrid(_out, _query, **kw):
            captured["limit"] = kw.get("limit")
            captured["video_ids_filter"] = kw.get("video_ids_filter")
            ids = kw.get("video_ids_filter")
            # Faithful to the real index-level predicate: only members return.
            return hits if ids is None else [h for h in hits if h["video_id"] in ids]

        monkeypatch.setattr("video_intel.hybrid_search", fake_hybrid)

        response = MagicMock()
        response.text = "synthesized brief body"
        client = MagicMock()
        client.models.generate_content.return_value = response
        monkeypatch.setattr("video_intel.create_client", lambda _key: client)

        video_intel.cmd_nugget(args, {})
        if client.models.generate_content.called:
            captured["prompt"] = client.models.generate_content.call_args.kwargs["contents"].parts[0].text
        return captured

    def test_scopes_retrieval_at_the_index_query_with_the_users_own_limit(self, tmp_path, capsys, monkeypatch):
        """The filter is an index-level predicate, not a post-filter over the
        global pool - Gate 1 measured the post-filter shape starving a
        19-video topic down to 2 surviving excerpts. The limit passed through
        is the USER's, untouched: no over-fetch multiplier on this surface."""
        self._corpus(tmp_path)
        hits = [
            self._hit("outsidevid1", "other", "louder chunk", 0.9),
            self._hit("insidevid12", "demo", "anchor chunk", 0.5),
        ]
        captured = self._run(tmp_path, monkeypatch, hits, self._args())
        assert captured["limit"] == 4
        assert captured["video_ids_filter"] == {"insidevid12"}
        assert "anchor chunk" in captured["prompt"]
        assert "louder chunk" not in captured["prompt"]

    def test_a_leak_past_the_index_filter_is_dropped_with_a_warning(self, tmp_path, capsys, monkeypatch, caplog):
        """Belt: the predicate owns membership, but if it ever silently stops
        filtering, the leak must not reach the synthesis unnoticed."""
        self._corpus(tmp_path)

        def leaky_hybrid(_out, _query, **kw):
            return [
                self._hit("insidevid12", "demo", "anchor chunk", 0.5),
                self._hit("outsidevid1", "other", "leaked chunk", 0.9),
            ]

        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr("video_intel.hybrid_search", leaky_hybrid)
        response = MagicMock()
        response.text = "brief"
        client = MagicMock()
        client.models.generate_content.return_value = response
        monkeypatch.setattr("video_intel.create_client", lambda _key: client)
        with caplog.at_level("WARNING"):
            video_intel.cmd_nugget(self._args(no_save=True), {})
        prompt = client.models.generate_content.call_args.kwargs["contents"].parts[0].text
        assert "anchor chunk" in prompt
        assert "leaked chunk" not in prompt
        assert "outside the topic despite the index-level filter" in caplog.text

    def test_without_the_flag_retrieval_is_byte_identical_to_pre_188(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        hits = [
            self._hit("outsidevid1", "other", "louder chunk", 0.9),
            self._hit("insidevid12", "demo", "anchor chunk", 0.5),
        ]
        captured = self._run(tmp_path, monkeypatch, hits, self._args(topic=None))
        assert captured["limit"] == 4  # no over-fetch without a topic
        assert "louder chunk" in captured["prompt"]
        assert "anchor chunk" in captured["prompt"]

    def test_scoped_brief_front_matter_records_the_topic(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        hits = [self._hit("insidevid12", "demo", "anchor chunk", 0.5)]
        self._run(tmp_path, monkeypatch, hits, self._args())
        briefs = list((tmp_path / "_briefings" / "nuggets").glob("*.md"))
        assert len(briefs) == 1
        front = briefs[0].read_text(encoding="utf-8").split("---")[1]
        assert "topic: fde" in front

    def test_unscoped_brief_front_matter_has_no_topic_key(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        hits = [self._hit("insidevid12", "demo", "anchor chunk", 0.5)]
        self._run(tmp_path, monkeypatch, hits, self._args(topic=None))
        briefs = list((tmp_path / "_briefings" / "nuggets").glob("*.md"))
        assert len(briefs) == 1
        assert "topic:" not in briefs[0].read_text(encoding="utf-8").split("---")[1]

    def test_unknown_topic_is_refused_before_any_retrieval(self, tmp_path, capsys, monkeypatch):
        """Probe before you pay: an unknown slug must not cost a Voyage call."""
        self._corpus(tmp_path)
        monkeypatch.setattr("video_intel.resolve_output_dir", lambda _c, **_kw: tmp_path)

        def _boom(*_a, **_kw):
            raise AssertionError("hybrid_search must not run for an unknown topic")

        monkeypatch.setattr("video_intel.hybrid_search", _boom)
        video_intel.cmd_nugget(self._args(topic="nope"), {})
        out = capsys.readouterr().out
        assert "No topic 'nope'" in out
        assert "known: fde" in out

    def test_no_matching_excerpts_prints_a_topic_aware_message(self, tmp_path, capsys, monkeypatch):
        self._corpus(tmp_path)
        hits = [self._hit("outsidevid1", "other", "louder chunk", 0.9)]
        self._run(tmp_path, monkeypatch, hits, self._args())
        out = capsys.readouterr().out
        assert "No excerpts in topic 'fde'" in out
        assert "search --topic fde" in out


class TestHybridSearchVideoIdsFilterWiring:
    """Issue #188 review P1: the stubbed nugget tests prove cmd_nugget's
    kwargs, not that the REAL hybrid_search honors them - both the WHERE
    wiring and the scoped pool sizing were deletable with every topic test
    green. These drive the real function through the capturing LanceDB fake."""

    def _run(self, tmp_path, fake_lancedb, monkeypatch, *, ids, limit):
        monkeypatch.setattr(video_intel, "load_taxonomy", lambda _d: {"concepts": {}})
        video_intel.hybrid_search(tmp_path, "some query", limit=limit, video_ids_filter=ids)
        return fake_lancedb

    def test_the_predicate_reaches_the_real_where_clause(self, tmp_path, fake_lancedb, monkeypatch):
        builder = self._run(tmp_path, fake_lancedb, monkeypatch, ids={"vidA", "vidB"}, limit=5)
        joined = " AND ".join(builder.where_clauses)
        assert video_intel.video_ids_predicate({"vidA", "vidB"}) in joined

    def test_no_filter_means_no_video_id_predicate(self, tmp_path, fake_lancedb, monkeypatch):
        monkeypatch.setattr(video_intel, "load_taxonomy", lambda _d: {"concepts": {}})
        video_intel.hybrid_search(tmp_path, "some query", limit=5)
        assert all("video_id IN" not in c for c in fake_lancedb.where_clauses)

    def test_scoped_pool_is_sized_to_the_scope(self, tmp_path, fake_lancedb, monkeypatch):
        ids = {f"vid{i:08d}" for i in range(19)}
        builder = self._run(tmp_path, fake_lancedb, monkeypatch, ids=ids, limit=15)
        # max(max(50, 15*5), min(1000, 19*15)) = max(75, 285) = 285
        assert builder.limit_calls == [285]

    def test_scoped_pool_is_capped_at_one_thousand(self, tmp_path, fake_lancedb, monkeypatch):
        ids = {f"vid{i:08d}" for i in range(120)}
        builder = self._run(tmp_path, fake_lancedb, monkeypatch, ids=ids, limit=10)
        assert builder.limit_calls == [1000]

    def test_unscoped_pool_is_unchanged(self, tmp_path, fake_lancedb, monkeypatch):
        monkeypatch.setattr(video_intel, "load_taxonomy", lambda _d: {"concepts": {}})
        video_intel.hybrid_search(tmp_path, "some query", limit=10)
        assert fake_lancedb.limit_calls == [50]


class TestTopicListingOrderPinsUnresolvedLast:
    def test_unresolved_and_undated_members_sort_after_every_dated_member(self):
        topics_data = {
            "topics": {
                "fde": {
                    "video_count": 4,
                    "channels": ["demo"],
                    "first_seen": "2026-08-01",
                    "briefings": [],
                    "videos": [
                        {"video_id": "ghost9999999", "unresolved": True},
                        {"video_id": "old456789012", "channel": "demo", "title": "Old", "published": "2026-01-01"},
                        {"video_id": "new456789012", "channel": "demo", "title": "New", "published": "2026-08-01"},
                        {"video_id": "undated12345", "channel": "demo", "title": "Undated", "published": None},
                    ],
                }
            }
        }
        lines = video_intel.render_topic_listing(topics_data, "fde")
        text = "\n".join(lines)
        # Dated members newest-first; the undated/unresolved tail comes after
        # EVERY dated member, ordered among itself by video_id (deterministic).
        assert text.index("New") < text.index("Old")
        for tail in ("Undated", "ghost9999999"):
            assert text.index("Old") < text.index(tail)
        assert text.index("ghost9999999") < text.index("Undated")  # id order within the tail
