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
    TOPIC_FILTER_OVERFETCH,
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
# Finding B (review of issue #146) - TOPIC_FILTER_OVERFETCH had zero coverage
# ---------------------------------------------------------------------------


class TestTopicFilterOverfetchIsLoadBearing:
    """Proved by mutation: setting `TOPIC_FILTER_OVERFETCH = 1` (disabling the
    over-fetch multiplier) left every prior test in this module green. That
    is possible because none of them ranked a topic member BELOW the plain
    result cap - `_corpus` above only ever has one video per side, so a cap
    of even 1 never truncates anything.

    Each fixture here instead ranks several out-of-topic videos strictly
    ABOVE the one topic member, far enough that a small `--limit` (applied
    before the topic filter runs) drops the member from the unfiltered
    candidate set entirely. Only over-fetching a wider candidate pool before
    filtering - and THEN capping to the requested limit - can still surface
    it. `TOPIC_FILTER_OVERFETCH` must stay large enough to matter for this
    fixture to be meaningful; if a future edit shrinks it toward 1 this
    assertion is the first thing that will need revisiting alongside these
    tests.
    """

    def test_overfetch_constant_still_multiplies_meaningfully(self):
        assert TOPIC_FILTER_OVERFETCH >= 2

    def test_concept_mode_needs_overfetch_to_surface_a_low_ranked_member(self, tmp_path, capsys, monkeypatch):
        # Four "agents" concepts: three (c.a1..c.a3) are shared by every
        # out-of-topic video, so each of those ranks with 3 matched concepts;
        # the topic member carries only c.a0, one matched concept. That gives
        # the plain (-count, published) sort a hard cliff between the two
        # groups regardless of published-date tie-breaking.
        concepts = {
            "c.a0": {"preferred_label": "agents overview", "aliases": [], "video_count": 1},
            "c.a1": {"preferred_label": "agents basics", "aliases": [], "video_count": 5},
            "c.a2": {"preferred_label": "agents patterns", "aliases": [], "video_count": 5},
            "c.a3": {"preferred_label": "agents tooling", "aliases": [], "video_count": 5},
        }
        (tmp_path / "taxonomy.json").write_text(
            json.dumps({"version": 1, "built_from": 6, "concepts": concepts}), encoding="utf-8"
        )

        for i in range(5):
            vid = f"outsidevid{i}"
            write_meta(
                tmp_path,
                "demo",
                f"p-out{i}",
                video_id=vid,
                title=f"outside talk {vid}",
                published=f"2026-01-0{i + 1}",
            )
            (tmp_path / "demo" / f"p-out{i}.concepts.json").write_text(
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
        # limit=3 unfiltered keeps only the five (tied, count=3) outside
        # videos' first three by published date - the member (count=1) never
        # reaches the candidate set without over-fetch.
        args = argparse.Namespace(query="agents", channel=None, limit=3, vector=False, since=None, topic="fde")
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert "topic talk" in out, "the topic member must survive even though it ranks 6th unfiltered"

    def test_vector_mode_needs_overfetch_to_surface_a_low_ranked_member(self, tmp_path, capsys, monkeypatch):
        """Same shape, mirrored onto the LanceDB hybrid path.

        `hybrid_search` is exercised for real (only the LanceDB connection and
        the Voyage embed call are stubbed, same as `conftest.fake_lancedb`),
        so this drives the actual `_dedup_by_video` ranking-then-truncation
        logic that `TOPIC_FILTER_OVERFETCH` feeds `limit=` into - not a
        pre-canned return from `hybrid_search` itself.
        """
        member_id = "vecmember01"
        write_meta(tmp_path, "demo", "p-member", video_id=member_id, title="topic talk", published="2026-02-01")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter=f"video_ids:\n  - {member_id}\n")
        write_topics(tmp_path, build_topics(tmp_path))

        rows = [
            {
                "text": f"outside chunk {i}",
                "timestamp": "00:00:00",
                "timestamp_seconds": 0,
                "video_id": f"vecoutside{i}",
                "channel": "demo",
                "title": f"outside talk {i}",
                "published": "2026-01-01",
                "source_file": f"vecoutside{i}.transcript.md",
                "concept_ids": "[]",
                "_relevance_score": 1.0 - i * 0.01,  # every outside chunk outranks the member below
            }
            for i in range(5)
        ]
        rows.append(
            {
                "text": "member chunk",
                "timestamp": "00:00:00",
                "timestamp_seconds": 0,
                "video_id": member_id,
                "channel": "demo",
                "title": "topic talk",
                "published": "2026-02-01",
                "source_file": "p-member.transcript.md",
                "concept_ids": "[]",
                "_relevance_score": 0.5,
            }
        )
        df = pd.DataFrame(rows)

        class _RowBuilder:
            def vector(self, _v):
                return self

            def text(self, _q):
                return self

            def limit(self, _n):
                return self

            def where(self, _c):
                return self

            def to_pandas(self):
                return df

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
            limit=3,
            vector=True,
            since=None,
            topic="fde",
            preview=False,
            min_relevance=0.0,
            no_expand=True,
        )
        cmd_search(args, {})
        out = capsys.readouterr().out
        assert "topic talk" in out, "the topic member must survive even though it ranks 6th unfiltered"


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
        self._corpus(tmp_path)
        filtered = self._run(tmp_path, monkeypatch, capsys, topic="fde")
        assert "the topic filter removed all of them" in filtered
        assert "--limit" in filtered
        assert "topics-build" in filtered

        unfiltered = self._run(tmp_path, monkeypatch, capsys, topic=None)
        assert "the topic filter removed all of them" not in unfiltered
        assert filtered != unfiltered

    def test_vector_mode_does_not_blame_the_index(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(video_intel, "resolve_output_dir", lambda _c, **_kw: tmp_path)
        write_meta(tmp_path, "demo", "p1", video_id="outsidevid1", title="talk outsidevid1")
        write_briefing(tmp_path, "fde/2026-08-22-a.md", front_matter="video_ids:\n  - absentvid12\n")
        write_topics(tmp_path, build_topics(tmp_path))

        hit = {
            "video_id": "outsidevid1",
            "channel": "demo",
            "title": "talk outsidevid1",
            "published": "2026-05-01",
            "timestamp": "00:00:00",
            "timestamp_seconds": 0,
            "relevance": 0.9,
            "text": "chunk",
            "source_file": "p1.transcript.md",
        }
        args = argparse.Namespace(
            query="agents",
            channel=None,
            limit=None,
            vector=True,
            since=None,
            topic="fde",
            preview=False,
            min_relevance=0.0,
            no_expand=True,
        )

        monkeypatch.setattr(video_intel, "hybrid_search", lambda *_a, **_kw: [dict(hit)])
        cmd_search(args, {})
        filtered = capsys.readouterr().out
        assert "Is the index built?" not in filtered
        assert "the topic filter removed all of them" in filtered

        monkeypatch.setattr(video_intel, "hybrid_search", lambda *_a, **_kw: [])
        cmd_search(args, {})
        empty = capsys.readouterr().out
        assert "Is the index built?" in empty
        assert empty != filtered

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
