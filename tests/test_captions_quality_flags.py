"""The captions failover owns every field that describes its transcript (#182).

`_try_captions_transcript` was the third transcript meta writer and the only one
that replaced the artifact while leaving another writer's metrics behind.
`update_meta` merges, so a captions recovery after a flagged Gemini attempt
produced a meta reading `transcript_status: complete`,
`transcript_source: youtube_captions` and
`transcript_quality_flags: ["monolithic_severe"]` at once - describing a
transcript that no longer existed on disk.

Measured cost on the real corpus: four recovered videos each paid for
mindmap-from-video (up to 411k prompt tokens) with a healthy transcript sitting
beside them, because `resolve_mindmap_source(..., transcript_severe=True)`
correctly treated the stale flag as real. Plus a permanent `EXIT_PARTIAL` and a
dedupe ranking penalty for each.

These tests drive the REAL writer and assert on the meta it actually produced -
a stub that hands the fields in would agree by construction.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest

import video_intel as vi


def _video(vid: str = "vid123") -> dict:
    return {
        "video_id": vid,
        "title": "A Talk",
        "url": f"https://www.youtube.com/watch?v={vid}",
        "published": "2026-01-01",
    }


def _captions(snippets, *, generated: bool = True):
    return vi.CaptionsResult(snippets, generated, "en")


def _healthy_snippets(n: int = 40, step: int = 30):
    return [(i * step, f"line number {i} with some real words in it") for i in range(n)]


@pytest.fixture
def paths(tmp_path):
    channel_dir = tmp_path / "somechannel"
    channel_dir.mkdir()
    prefix = "2026-01-01-a-talk"
    return SimpleNamespace(
        channel_dir=channel_dir,
        prefix=prefix,
        transcript=channel_dir / f"{prefix}.transcript.md",
        meta=channel_dir / f"{prefix}.meta.json",
    )


def _stub_captions(monkeypatch, snippets, *, generated: bool = True):
    monkeypatch.setattr(vi, "fetch_english_captions", lambda _vid: _captions(snippets, generated=generated))


def _write_flagged_gemini_meta(paths, **extra) -> None:
    """A meta as a FAILED, severe-flagged Gemini attempt would have left it."""
    meta = {
        "video_id": "vid123",
        "channel": "somechannel",
        "title": "A Talk",
        "published": "2026-01-01",
        "video_url": "https://www.youtube.com/watch?v=vid123",
        "processed": "2026-08-30T00:00:00+00:00",
        "modes_completed": ["transcript"],
        "transcript_status": "partial",
        "transcript_source": "gemini",
        "transcript_quality_flags": ["monolithic_severe", "density_mild"],
        "transcript_max_blind_gap_seconds": 2400,
        "transcript_blind_gap_at_seconds": 120,
        "transcript_last_dialogue_fraction": 0.02,
        "transcript_dialogue_entries": 1,
        "transcript_chunks": 3,
        "transcript_chunk_minutes": 30,
        "transcript_thin_chunks": 2,
        "transcript_confabulated_chunks": 1,
        "transcript_chunk_window_violations": 7,
        "transcript_output_tokens": 65522,
        "transcript_finish_reason": "MAX_TOKENS",
        **extra,
    }
    paths.meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")


class TestStaleFlagsDoNotSurviveARecovery:
    def test_a_captions_recovery_clears_the_previous_attempts_severe_flags(self, paths, monkeypatch) -> None:
        _write_flagged_gemini_meta(paths)
        _stub_captions(monkeypatch, _healthy_snippets())

        result = vi._try_captions_transcript(
            _video(), paths.transcript, paths.meta, paths.prefix, reason="gemini error: boom", duration_seconds=1200
        )
        assert result is not None

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert meta["transcript_source"] == vi.TRANSCRIPT_SOURCE_CAPTIONS
        assert meta["transcript_status"] == "complete"
        assert vi.transcript_quality_flags_are_severe(meta["transcript_quality_flags"]) is False

    def test_the_chunked_only_fields_are_retired_not_merged_forward(self, paths, monkeypatch) -> None:
        """These describe a Gemini attempt that no longer exists. `update_meta`
        merges, so setting the fields this writer owns is not enough - the rest
        have to be dropped."""
        _write_flagged_gemini_meta(paths)
        _stub_captions(monkeypatch, _healthy_snippets())

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=1200)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        for gone in (
            "transcript_chunks",
            "transcript_chunk_minutes",
            "transcript_thin_chunks",
            "transcript_confabulated_chunks",
            "transcript_chunk_window_violations",
            "transcript_output_tokens",
            "transcript_finish_reason",
        ):
            assert gone not in meta, f"{gone} still describes a transcript that is gone"

    def test_the_downstream_consequences_all_clear(self, paths, monkeypatch) -> None:
        """The three the issue measured: mindmap routing, the exit code, and
        dedupe's canonical ordering."""
        _write_flagged_gemini_meta(paths)
        _stub_captions(monkeypatch, _healthy_snippets())
        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=1200)

        severe = vi._transcript_quality_severe_from_meta(paths.meta)
        assert severe is False

        # resolve_mindmap_source: `auto` must now use the transcript, not fall
        # back to the ~10x more expensive mindmap-from-video.
        assert vi.resolve_mindmap_source({}, transcript_available=True, transcript_severe=severe) == "transcript"

        # missing_pipeline_artifacts: the transcript step is no longer a gap.
        steps = [
            {
                "name": "transcript",
                "requested": True,
                "path": paths.transcript,
                "status": "done (captions)",
                "quality_severe": severe,
            }
        ]
        assert vi.missing_pipeline_artifacts(steps) == []

        # dedupe: the healthy captions artifact is no longer ranked below a clean duplicate.
        assert vi._dedupe_meta_is_severe(json.loads(paths.meta.read_text(encoding="utf-8"))) is False

    def test_identity_and_operator_fields_survive(self, paths, monkeypatch) -> None:
        """Dropping is scoped to fields that DESCRIBE the artifact. Identity
        (issue #66), the operator's deliberate stage suppression (issue #42),
        title-rotation history, and a hand-written note are not that."""
        _write_flagged_gemini_meta(
            paths,
            alt_titles=["An Older Title"],
            skip_modes=["concepts"],
            transcript_quality_note="repaired by hand 2026-08-31",
        )
        _stub_captions(monkeypatch, _healthy_snippets())

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=1200)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert meta["video_id"] == "vid123"
        assert meta["alt_titles"] == ["An Older Title"]
        assert meta["skip_modes"] == ["concepts"]
        assert meta["transcript_quality_note"] == "repaired by hand 2026-08-31"


class TestTheCaptionTrackIsActuallyJudged:
    """Clearing alone would have left captions transcripts never assessed. A
    genuinely bad caption track is exactly what the #157 machinery is for."""

    def test_a_monolithic_caption_track_is_flagged_severe(self, paths, monkeypatch) -> None:
        _stub_captions(monkeypatch, [(5, "the entire three hour talk in one cue")])

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=10800)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert vi.transcript_quality_flags_are_severe(meta["transcript_quality_flags"]) is True
        assert meta["transcript_status"] == "partial"

    def test_a_healthy_caption_track_is_not_flagged(self, paths, monkeypatch) -> None:
        _stub_captions(monkeypatch, _healthy_snippets(n=60, step=20))

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=1200)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert meta["transcript_quality_flags"] == []
        assert meta["transcript_status"] == "complete"
        assert meta["transcript_dialogue_entries"] == 60

    def test_an_unknown_duration_degrades_instead_of_manufacturing_a_flag(self, paths, monkeypatch) -> None:
        """`duration_seconds=None` genuinely means unknown. Gap and density
        cannot be computed, and the assessor must not invent a verdict."""
        _stub_captions(monkeypatch, _healthy_snippets(n=5, step=600))

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=None)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert vi.transcript_quality_flags_are_severe(meta["transcript_quality_flags"]) is False

    def test_a_clipped_segment_is_assessed_against_its_own_span(self, paths, monkeypatch) -> None:
        """Otherwise every --start/--end segment reads as one enormous blind
        gap against the full video's duration."""
        snippets = [(600 + i * 20, f"segment line {i}") for i in range(30)]
        _stub_captions(monkeypatch, snippets)

        vi._try_captions_transcript(
            _video(),
            paths.transcript,
            paths.meta,
            paths.prefix,
            start_offset=600,
            end_offset=1200,
            duration_seconds=10800,
        )

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert vi.transcript_quality_flags_are_severe(meta["transcript_quality_flags"]) is False

    def test_the_assessment_describes_the_cues_that_reached_the_transcript(self, paths, monkeypatch) -> None:
        """Rolling-window ASR repeats cues; the body collapses them to one per
        second. Assessing the RAW track would count entries the transcript does
        not contain."""
        snippets = [(0, "short"), (0, "a much longer version of the same cue"), (30, "second line")]
        _stub_captions(monkeypatch, snippets)

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=120)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert meta["transcript_dialogue_entries"] == 2
        body = paths.transcript.read_text(encoding="utf-8")
        assert body.count("[00:00]") == 1


class TestFieldInventoryCannotDrift:
    """A new quality field added to the Gemini writers must not silently go
    stale on this path. The inventory is derived from the module, not restated,
    so the two cannot disagree."""

    # Keys that are NOT meta fields describing a transcript artifact, and so
    # are correctly absent from the inventory. Every entry needs a reason -
    # this is the conscious-classification list a new field has to pass
    # through, and padding it is how the guarantee would be lost.
    NOT_ARTIFACT_FIELDS: ClassVar[set[str]] = {
        # The operator's own hand-written annotation. Dropping it is data loss.
        "transcript_quality_note",
        # Config knobs and function parameters, never persisted to meta.json.
        "transcript_source_cli",
        "transcript_timeout_seconds",
        "transcript_max_duration_seconds",
    }

    def test_every_transcript_meta_key_in_the_module_is_classified(self) -> None:
        """The inventory is derived from the module, not restated beside it.

        An earlier version of this test walked only `transcript_*` constants
        appearing SYNTACTICALLY INSIDE an `update_meta(...)` call node. The
        chunked and salvage writers build `meta_fields = {...}` as a variable
        and pass the name, so their keys were invisible: the walk saw 7 fields,
        all from the two inline dict literals - the captions writer compared
        against itself. It was a tautology, and it is why four real fields
        (`transcript_confabulation_note`, `transcript_recovery`,
        `transcript_parse_error`, `transcript_warning`) were missing from the
        inventory while the suite stayed green.

        This version collects every `transcript_*`/`captions_*` string used as
        a DICT KEY anywhere in the module, which no writer can dodge, and
        requires each to be either in the inventory or consciously classified
        as not-an-artifact-field.
        """
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(vi))
        keys: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key in node.keys:
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and (key.value.startswith("transcript_") or key.value.startswith("captions_"))
                ):
                    keys.add(key.value)

        unaccounted = sorted(keys - set(vi.TRANSCRIPT_ARTIFACT_FIELDS) - self.NOT_ARTIFACT_FIELDS)
        assert not unaccounted, (
            "these transcript_*/captions_* meta keys are written somewhere in the module "
            "but are neither in TRANSCRIPT_ARTIFACT_FIELDS nor classified as "
            "not-an-artifact-field, so they can go stale on a captions recovery: "
            f"{unaccounted}"
        )

    def test_the_walk_actually_sees_the_writers_it_claims_to_watch(self) -> None:
        """Guard against the tautology returning. If the extraction stops
        seeing the chunked and salvage writers' variable-built dicts, this
        test goes red BEFORE the inventory test can quietly start passing for
        the wrong reason."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(vi))
        keys: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)

        # Written only by the chunked writer, and only inside a variable-built dict.
        assert "transcript_chunks" in keys
        assert "transcript_confabulated_chunks" in keys
        # Written only by the salvage writer, likewise.
        assert "transcript_recovery" in keys
        assert "transcript_parse_error" in keys

    def test_no_inventory_field_keeps_the_previous_attempts_value(self, paths, monkeypatch) -> None:
        """Written-or-dropped, with nothing falling between the two. The
        expectation is read off the pre-recovery meta on disk rather than
        restated, so a change to the fixture cannot quietly weaken it."""
        _write_flagged_gemini_meta(paths)
        before = json.loads(paths.meta.read_text(encoding="utf-8"))
        _stub_captions(monkeypatch, _healthy_snippets())

        vi._try_captions_transcript(
            _video(), paths.transcript, paths.meta, paths.prefix, reason="r", duration_seconds=1200
        )
        after = json.loads(paths.meta.read_text(encoding="utf-8"))

        stale = [f for f in vi.TRANSCRIPT_ARTIFACT_FIELDS if f in before and f in after and before[f] == after[f]]
        assert not stale, f"these kept the previous attempt's value: {stale}"


class TestEveryCallSiteThreadsTheDuration:
    """A call site that drops `duration_seconds` silently downgrades that path's
    assessment to metrics-only - gap, density and monolithic are all skipped, so
    a five-cues-over-three-hours track is stamped `complete`, `flags: []`.

    The first cut of this PR missed one of the TEN sites (the `_cmd_process_url`
    chunked-failover one) while claiming nine, and nothing caught it - every
    other test calls the writer directly and hands the duration in. Deriving the
    inventory from the source is what closes that."""

    def test_no_call_site_omits_duration_seconds(self) -> None:
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(vi))
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "_try_captions_transcript":
                continue
            if not any(kw.arg == "duration_seconds" for kw in node.keywords):
                missing.append(node.lineno)
        assert not missing, (
            "these _try_captions_transcript call sites do not pass duration_seconds, so the "
            f"quality assessment silently degrades to metrics-only on those paths: lines {missing}"
        )

    def test_the_walk_finds_the_call_sites_it_claims_to_check(self) -> None:
        """Guard the guard: if the extraction stops finding call sites, the test
        above passes vacuously."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(vi))
        sites = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None))
            == "_try_captions_transcript"
        ]
        assert len(sites) >= 9, f"expected the module's captions call sites, found {len(sites)}"


class TestALeadingGapInACaptionTrackIsNotSevere:
    """A caption track is ASR ground truth about SPEECH, so silence before the
    first cue means nobody had spoken - not that a model skipped content. A
    livestream pre-show is the common shape, and #120 routes that population
    here on purpose. Measured: the two largest leading gaps in the corpus are
    576s and 558s, both within 4% of the 600s severe threshold."""

    def test_a_long_pre_show_silence_is_mild_not_severe(self, paths, monkeypatch) -> None:
        # Nothing said for the first 15 minutes, then a healthy hour.
        snippets = [(900 + i * 20, f"line {i}") for i in range(180)]
        _stub_captions(monkeypatch, snippets)

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=4500)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert vi.QUALITY_FLAG_CAPTIONS_LEADING_GAP_MILD in meta["transcript_quality_flags"]
        assert vi.transcript_quality_flags_are_severe(meta["transcript_quality_flags"]) is False
        assert meta["transcript_status"] == "complete"

    def test_an_internal_hole_is_still_severe(self, monkeypatch, paths) -> None:
        """Only LEADING is demoted. An internal hole could be a music segment
        or a genuine caption failure, and the evidence does not separate them."""
        snippets = [(i * 20, f"early {i}") for i in range(20)] + [(1400 + i * 20, f"late {i}") for i in range(20)]
        _stub_captions(monkeypatch, snippets)

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=2000)

        meta = json.loads(paths.meta.read_text(encoding="utf-8"))
        assert vi.transcript_quality_flags_are_severe(meta["transcript_quality_flags"]) is True


class TestASevereCaptionTrackIsVisibleToAReader:
    def test_the_transcript_markdown_carries_the_warning(self, paths, monkeypatch) -> None:
        """A status that lives only in meta.json is invisible to the person who
        opens the transcript, who is the primary consumer."""
        _stub_captions(monkeypatch, [(5, "the whole three hour talk in one cue")])

        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=10800)

        body = paths.transcript.read_text(encoding="utf-8")
        assert "Quality guard flagged this caption track" in body

    def test_a_healthy_track_gets_no_warning_block(self, paths, monkeypatch) -> None:
        _stub_captions(monkeypatch, _healthy_snippets())
        vi._try_captions_transcript(_video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=1200)
        assert "Quality guard flagged" not in paths.transcript.read_text(encoding="utf-8")

    def test_the_returned_status_distinguishes_a_demotion_from_a_clean_success(self, paths, monkeypatch) -> None:
        """#157 invariant 6: a caller reading the status string, not the meta,
        must be able to tell the two apart."""
        _stub_captions(monkeypatch, [(5, "one cue for everything")])
        _, status = vi._try_captions_transcript(
            _video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=10800
        )
        assert status == "partial (captions quality guard)"

        paths.transcript.unlink()
        _stub_captions(monkeypatch, _healthy_snippets())
        _, ok_status = vi._try_captions_transcript(
            _video(), paths.transcript, paths.meta, paths.prefix, duration_seconds=1200
        )
        assert ok_status == "done (captions)"


class TestTheRuleAppliesToEveryTranscriptWriter:
    """CLAUDE.md states the rule universally, so it must be universally applied
    - not just on the captions path that prompted it."""

    def test_retired_transcript_fields_is_the_inventory_minus_what_is_written(self) -> None:
        written = {"transcript_status": "complete", "transcript_source": "gemini"}
        retired = vi.retired_transcript_fields(written)
        assert "transcript_status" not in retired
        assert "transcript_chunks" in retired
        assert set(retired) | set(written) >= set(vi.TRANSCRIPT_ARTIFACT_FIELDS)

    def test_every_transcript_writer_retires_what_it_does_not_write(self) -> None:
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(vi))
        unguarded = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "update_meta":
                continue
            args = [ast.unparse(a) for a in node.args] + [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords]
            joined = " ".join(args)
            is_transcript_write = '"transcript"' in joined or "'transcript'" in joined
            if is_transcript_write and "drop_fields" not in joined:
                unguarded.append(node.lineno)
        assert not unguarded, (
            "these update_meta(..., 'transcript') writers do not retire the artifact fields "
            f"they are not setting, so a predecessor's metrics can survive: lines {unguarded}"
        )


class TestUpdateMetaDropFields:
    def test_drop_fields_removes_keys_and_defaults_to_a_no_op(self, tmp_path) -> None:
        meta_path = tmp_path / "x.meta.json"
        meta_path.write_text(json.dumps({"a": 1, "b": 2, "modes_completed": []}), encoding="utf-8")

        vi.update_meta(meta_path, {"c": 3}, "transcript")
        assert json.loads(meta_path.read_text(encoding="utf-8"))["a"] == 1

        vi.update_meta(meta_path, {"c": 4}, "transcript", drop_fields=("a",))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "a" not in meta
        assert meta["b"] == 2 and meta["c"] == 4

    def test_a_dropped_key_that_is_also_written_keeps_the_new_value(self, tmp_path) -> None:
        """Drop runs before merge, so a field cannot be dropped out from under
        the value the caller just set."""
        meta_path = tmp_path / "x.meta.json"
        meta_path.write_text(json.dumps({"a": "old", "modes_completed": []}), encoding="utf-8")
        vi.update_meta(meta_path, {"a": "new"}, "transcript", drop_fields=("a",))
        assert json.loads(meta_path.read_text(encoding="utf-8"))["a"] == "new"

    def test_dropping_an_absent_key_is_not_an_error(self, tmp_path) -> None:
        meta_path = tmp_path / "x.meta.json"
        meta_path.write_text(json.dumps({"modes_completed": []}), encoding="utf-8")
        vi.update_meta(meta_path, {}, "transcript", drop_fields=("nope",))
        assert "nope" not in json.loads(meta_path.read_text(encoding="utf-8"))
