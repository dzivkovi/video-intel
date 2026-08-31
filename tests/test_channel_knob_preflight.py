"""Issue #169: `scan --dry-run` cannot see channel config typos because it
returns before the per-video knob resolvers run.

`validate_channel_knobs` reuses the REAL resolvers (`resolve_transcript_source`,
`resolve_chunk_minutes`, `resolve_mindmap_source`) to preflight a channel's
knobs, and `cmd_scan` calls it at the top of the channel loop - before
`get_channel_id` and before the `--dry-run` early return - so a config typo
is visible on a dry run instead of only surfacing on the real (paid) run.
The preflight is report-only: it never changes where the actual skip happens.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

import pytest

import video_intel as vi
from video_intel import (
    KNOB_CONSEQUENCE_ABORTS_SCAN,
    KNOB_CONSEQUENCE_FAILS_MINDMAPS,
    KNOB_CONSEQUENCE_NOT_REACHED_BY_SCAN,
    KNOB_CONSEQUENCE_SKIPS_CHANNEL,
    load_prompt,
    resolve_prompt_path,
    validate_channel_knobs,
)


class TestValidateChannelKnobsHealthyConfigs:
    def test_empty_config_returns_no_problems(self):
        assert validate_channel_knobs({}, {}) == []

    @pytest.mark.parametrize("value", sorted(vi._VALID_TRANSCRIPT_SOURCE_VALUES))
    def test_every_valid_transcript_source_is_healthy(self, value):
        assert validate_channel_knobs({"transcript_source": value}, {}) == []

    @pytest.mark.parametrize("value", sorted(vi._VALID_MINDMAP_SOURCE_VALUES))
    def test_every_valid_mindmap_source_is_healthy(self, value):
        assert validate_channel_knobs({"mindmap_source": value}, {}) == []

    @pytest.mark.parametrize("value", [1, 5, 30, 120])
    def test_every_positive_chunk_minutes_is_healthy(self, value):
        assert validate_channel_knobs({"chunk_minutes": value}, {}) == []


class TestValidateChannelKnobsInvalidStrings:
    def test_invalid_transcript_source_string_names_that_knob(self):
        problems = validate_channel_knobs({"transcript_source": "webvtt"}, {})

        assert len(problems) == 1
        knob, message, _consequence = problems[0]
        assert knob == "transcript_source"
        assert "webvtt" in message

    def test_invalid_mindmap_source_string_names_that_knob(self):
        problems = validate_channel_knobs({"mindmap_source": "screenshots"}, {})

        assert len(problems) == 1
        knob, message, _consequence = problems[0]
        assert knob == "mindmap_source"
        assert "screenshots" in message


class TestValidateChannelKnobsNonStringYamlShapes:
    """Issue #135's finding, reapplied here: a mapping or sequence fails a
    resolver's `in` membership test with TypeError, not ValueError."""

    @pytest.mark.parametrize("bad_value", [{"mode": "auto"}, ["auto"]], ids=["yaml_mapping", "yaml_sequence"])
    def test_transcript_source_non_string_shape_is_flagged(self, bad_value):
        problems = validate_channel_knobs({"transcript_source": bad_value}, {})

        assert len(problems) == 1
        assert problems[0][0] == "transcript_source"

    @pytest.mark.parametrize("bad_value", [{"mode": "auto"}, ["auto"]], ids=["yaml_mapping", "yaml_sequence"])
    def test_mindmap_source_non_string_shape_is_flagged(self, bad_value):
        problems = validate_channel_knobs({"mindmap_source": bad_value}, {})

        assert len(problems) == 1
        assert problems[0][0] == "mindmap_source"

    @pytest.mark.parametrize("bad_value", [{"n": 30}, [30]], ids=["yaml_mapping", "yaml_sequence"])
    def test_chunk_minutes_non_string_shape_is_flagged(self, bad_value):
        problems = validate_channel_knobs({"chunk_minutes": bad_value}, {})

        assert len(problems) == 1
        assert problems[0][0] == "chunk_minutes"


class TestValidateChannelKnobsChunkMinutesPositivity:
    @pytest.mark.parametrize("bad_value", [0, -5])
    def test_non_positive_chunk_minutes_is_flagged(self, bad_value):
        problems = validate_channel_knobs({"chunk_minutes": bad_value}, {})

        assert len(problems) == 1
        knob, message, _consequence = problems[0]
        assert knob == "chunk_minutes"
        assert str(bad_value) in message


class TestMindmapSourceTranscriptAvailabilityIsolation:
    def test_mindmap_source_transcript_is_not_a_false_alarm_before_transcripts_exist(self):
        """The load-bearing guard: `transcript_available=True` isolates the
        enum check from the availability conflict. A channel legitimately
        configured `mindmap_source: transcript` whose transcripts have not
        been written yet on this run must never be reported as broken -
        that would be a permanent false alarm on a healthy config.
        """
        assert validate_channel_knobs({"mindmap_source": "transcript"}, {}) == []


class TestValidateChannelKnobsConsequenceStrings:
    def test_bad_transcript_source_with_auto_transcript_all_skips_the_channel(self):
        problems = validate_channel_knobs({"transcript_source": "bogus", "auto_transcript": "all"}, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_SKIPS_CHANNEL

    @pytest.mark.parametrize("auto_transcript", [None, "none"], ids=["absent", "explicit_none"])
    def test_bad_transcript_source_without_auto_transcript_all_is_inert(self, auto_transcript):
        cfg = {"transcript_source": "bogus"}
        if auto_transcript is not None:
            cfg["auto_transcript"] = auto_transcript

        problems = validate_channel_knobs(cfg, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_NOT_REACHED_BY_SCAN

    def test_bad_chunk_minutes_with_auto_transcript_all_skips_the_channel(self):
        problems = validate_channel_knobs({"chunk_minutes": 0, "auto_transcript": "all"}, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_SKIPS_CHANNEL

    def test_bad_chunk_minutes_without_auto_transcript_all_is_inert(self):
        problems = validate_channel_knobs({"chunk_minutes": 0}, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_NOT_REACHED_BY_SCAN

    def test_bad_mindmap_source_with_auto_mindmap_none_is_inert(self):
        problems = validate_channel_knobs({"mindmap_source": "screenshots", "auto_mindmap": "none"}, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_NOT_REACHED_BY_SCAN

    def test_bad_mindmap_source_with_default_auto_mindmap_fails_every_mindmap(self):
        # auto_mindmap defaults to "all" when the key is absent.
        problems = validate_channel_knobs({"mindmap_source": "screenshots"}, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_FAILS_MINDMAPS

    def test_bad_mindmap_source_alone_still_fails_every_mindmap_composite_fix_did_not_flatten_the_normal_case(self):
        """Round 2: the composite downgrade (below) must only fire when a
        SKIPS_CHANNEL problem is genuinely present. A lone bad mindmap_source,
        with no transcript/chunk_minutes problem at all, must keep reporting
        FAILS_MINDMAPS - proving the composite fix did not flatten this case."""
        problems = validate_channel_knobs({"mindmap_source": "screenshots", "auto_mindmap": "all"}, {})

        assert len(problems) == 1
        assert problems[0][0] == "mindmap_source"
        assert problems[0][2] == KNOB_CONSEQUENCE_FAILS_MINDMAPS

    def test_skips_channel_problem_downgrades_a_coexisting_mindmap_problem_to_not_reached_by_scan(self):
        """Round 2 composite-contradiction fix: a skipped channel never reaches
        the mindmap loop, so a bad mindmap_source alongside a channel-skipping
        transcript_source problem must report NOT_REACHED_BY_SCAN, never
        FAILS_MINDMAPS - claiming both would contradict the runtime."""
        problems = validate_channel_knobs(
            {
                "auto_transcript": "all",
                "transcript_source": "bogus",
                "mindmap_source": "screenshots",
            },
            {},
        )

        by_knob = {p[0]: p[2] for p in problems}
        assert by_knob["transcript_source"] == KNOB_CONSEQUENCE_SKIPS_CHANNEL
        assert by_knob["mindmap_source"] == KNOB_CONSEQUENCE_NOT_REACHED_BY_SCAN


class TestValidateChannelKnobsPrompt:
    """Round 2: the reviewer's first finding. `prompt` resolves BEFORE any
    per-mode gate (`load_prompt` runs unconditionally), so a bad prompt name
    does not skip a channel - it ABORTS the whole scan mid-run, after every
    earlier channel's Gemini spend already happened."""

    def test_missing_prompt_file_is_flagged_aborts_scan(self):
        problems = validate_channel_knobs({"prompt": "does-not-exist-xyz"}, {})

        assert len(problems) == 1
        knob, message, consequence = problems[0]
        assert knob == "prompt"
        assert "does-not-exist-xyz" in message
        assert consequence == KNOB_CONSEQUENCE_ABORTS_SCAN

    def test_existing_prompt_file_is_healthy(self):
        # mindmap-light.md is a real, committed prompt file.
        assert validate_channel_knobs({"prompt": "mindmap-light"}, {}) == []

    def test_missing_default_prompt_fallback_is_flagged(self):
        """No per-channel `prompt` - the top-level `default_prompt` fallback
        must be checked too, not just the per-channel override."""
        problems = validate_channel_knobs({}, {"default_prompt": "does-not-exist-xyz"})

        assert len(problems) == 1
        assert problems[0][0] == "prompt"
        assert "does-not-exist-xyz" in problems[0][1]
        assert problems[0][2] == KNOB_CONSEQUENCE_ABORTS_SCAN

    def test_no_prompt_and_no_default_prompt_resolves_to_the_builtin_default(self):
        """Neither the channel nor the top-level config names a prompt - the
        hardcoded 'mindmap-light' fallback inside validate_channel_knobs
        itself must resolve to a real file, not merely 'no problem by luck'."""
        assert validate_channel_knobs({}, {}) == []

    @pytest.mark.parametrize("bad_value", [{"mode": "auto"}, ["a", "b"]], ids=["yaml_mapping", "yaml_sequence"])
    def test_non_string_prompt_is_flagged_rather_than_raising(self, bad_value):
        problems = validate_channel_knobs({"prompt": bad_value}, {})

        assert len(problems) == 1
        assert problems[0][0] == "prompt"
        assert problems[0][2] == KNOB_CONSEQUENCE_ABORTS_SCAN


class TestValidateChannelKnobsNumericTranscriptKnobs:
    """Round 2: `transcript_max_duration_seconds` / `transcript_timeout_seconds`
    have no resolver - scan reads them straight off the dict and uses them
    numerically AFTER the dry-run early return, so a bad value raises an
    uncaught TypeError on a real run instead of failing gracefully."""

    @pytest.mark.parametrize("knob_name", ["transcript_max_duration_seconds", "transcript_timeout_seconds"])
    def test_string_value_is_flagged(self, knob_name):
        problems = validate_channel_knobs({knob_name: "not-a-number"}, {})

        assert len(problems) == 1
        assert problems[0][0] == knob_name

    @pytest.mark.parametrize("knob_name", ["transcript_max_duration_seconds", "transcript_timeout_seconds"])
    def test_bool_value_is_flagged_even_though_bool_subclasses_int(self, knob_name):
        # An unquoted YAML `yes` parses as Python True; True seconds is not a
        # timeout anyone meant.
        problems = validate_channel_knobs({knob_name: True}, {})

        assert len(problems) == 1
        assert problems[0][0] == knob_name

    @pytest.mark.parametrize("knob_name", ["transcript_max_duration_seconds", "transcript_timeout_seconds"])
    @pytest.mark.parametrize("good_value", [7200, 600.0])
    def test_int_and_float_values_are_healthy(self, knob_name, good_value):
        assert validate_channel_knobs({knob_name: good_value}, {}) == []

    @pytest.mark.parametrize("knob_name", ["transcript_max_duration_seconds", "transcript_timeout_seconds"])
    def test_absent_key_is_healthy(self, knob_name):
        assert validate_channel_knobs({}, {}) == []

    @pytest.mark.parametrize("knob_name", ["transcript_max_duration_seconds", "transcript_timeout_seconds"])
    def test_consequence_is_aborts_scan_when_auto_transcript_is_all(self, knob_name):
        problems = validate_channel_knobs({knob_name: "bad", "auto_transcript": "all"}, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_ABORTS_SCAN

    @pytest.mark.parametrize("knob_name", ["transcript_max_duration_seconds", "transcript_timeout_seconds"])
    def test_consequence_is_not_reached_by_scan_when_auto_transcript_is_not_all(self, knob_name):
        problems = validate_channel_knobs({knob_name: "bad"}, {})

        assert problems[0][2] == KNOB_CONSEQUENCE_NOT_REACHED_BY_SCAN


class TestResolvePromptPathMatchesLoadPrompt:
    """Load-bearing: `resolve_prompt_path` was split out of `load_prompt` so
    the preflight could ask "would this resolve?" without the answer being
    sys.exit(1). If the two ever disagree about where a prompt file lives,
    the preflight is worse than no preflight (the PR #136 checker/writer-path
    -drift class). Both halves derived independently and then compared.
    """

    def test_resolve_prompt_path_points_at_the_exact_file_load_prompt_reads(self):
        path = resolve_prompt_path("mindmap-light")
        assert path.exists()
        assert path.read_text(encoding="utf-8") == load_prompt("mindmap-light")

    def test_load_prompt_exits_1_for_a_name_resolve_prompt_path_reports_as_missing(self):
        name = "does-not-exist-xyz"
        assert not resolve_prompt_path(name).exists()

        with pytest.raises(SystemExit) as excinfo:
            load_prompt(name)
        assert excinfo.value.code == 1


class TestValidateChannelKnobsDeterministicOrder:
    def test_all_three_bad_knobs_report_in_transcript_chunk_mindmap_order(self):
        problems = validate_channel_knobs({"transcript_source": "x", "chunk_minutes": 0, "mindmap_source": "y"}, {})

        assert [p[0] for p in problems] == ["transcript_source", "chunk_minutes", "mindmap_source"]


class TestValidateChannelKnobsNeverRaises:
    def test_never_raises_and_never_mutates_inputs(self):
        channel_config = {"transcript_source": "x", "chunk_minutes": {"bad": 1}, "mindmap_source": ["y"]}
        config = {}
        before = dict(channel_config)

        problems = validate_channel_knobs(channel_config, config)

        assert len(problems) == 3
        assert channel_config == before, "must never mutate the channel config it inspects"


# ---------------------------------------------------------------------------
# Caller-level: drive the real cmd_scan so the preflight's wiring (placement,
# logging, and non-interference with runtime routing) is proven end-to-end,
# not just at the helper.
# ---------------------------------------------------------------------------


def _scan_args(**overrides):
    base = {"dry_run": False, "channel": None, "force": False, "since": None, "model": None}
    base.update(overrides)
    return SimpleNamespace(**base)


class _RaisesIfCalled:
    """Stand-in for process_transcript/process_mindmap: fails the test loudly
    if a dry run ever reaches a paid Gemini call."""

    def __init__(self, label):
        self.label = label

    def __call__(self, *args, **kwargs):
        raise AssertionError(f"{self.label} must never be called on a --dry-run scan")


class _ScanEnvironment:
    """Shared stubbing for a two-channel scan: one healthy, one config-typoed.

    Mirrors the pattern in tests/test_manual_url_transcript_source.py's
    TestScanChannelConfigTypoSkipsOnlyThatChannel - only the network/paid
    boundary is stubbed, cmd_scan itself runs for real.
    """

    good_video: ClassVar[dict] = {
        "video_id": "good1",
        "title": "Good video",
        "published": "2026-04-15",
        "url": "https://www.youtube.com/watch?v=good1",
    }
    typo_video: ClassVar[dict] = {
        "video_id": "typo1",
        "title": "Typo channel video",
        "published": "2026-04-15",
        "url": "https://www.youtube.com/watch?v=typo1",
    }

    def wire(self, monkeypatch, *, dry_run):
        videos_by_channel_url = {
            "https://example.com/typo": [self.typo_video],
            "https://example.com/good": [self.good_video],
        }

        monkeypatch.setenv("GEMINI_API_KEY", "test")
        monkeypatch.setenv("YOUTUBE_API_KEY", "test")
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **kw: None)
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)

        self.get_channel_id_calls: list[str] = []

        def fake_get_channel_id(_yt, url):
            self.get_channel_id_calls.append(url)
            return (url, url)

        monkeypatch.setattr(vi, "get_channel_id", fake_get_channel_id)
        monkeypatch.setattr(vi, "fetch_channel_videos", lambda yt, cid, since: list(videos_by_channel_url.get(cid, [])))
        monkeypatch.setattr(vi, "enrich_with_durations", lambda _yt, ids: dict.fromkeys(ids))
        monkeypatch.setattr(vi, "fetch_preflight_status", lambda _yt, ids: {vid: {} for vid in ids})
        monkeypatch.setattr(vi, "_is_youtube_short_url", lambda video_id: False)
        monkeypatch.setattr(
            vi, "process_transcript", _RaisesIfCalled("process_transcript") if dry_run else self._fake_transcript
        )
        monkeypatch.setattr(
            vi, "process_mindmap", _RaisesIfCalled("process_mindmap") if dry_run else self._fake_mindmap
        )

        self.transcripts_seen: list[str] = []
        self.mindmaps_seen: list[str] = []

    def _fake_transcript(self, *args, **kwargs):
        video = args[2] if len(args) > 2 else kwargs.get("video")
        self.transcripts_seen.append(video["video_id"])
        return (video.get("video_id", "prefix"), "done")

    def _fake_mindmap(self, *args, **kwargs):
        video = args[2] if len(args) > 2 else kwargs.get("video")
        self.mindmaps_seen.append(video["video_id"])
        return (video.get("video_id", "prefix"), "done")

    def config(self, tmp_path):
        return {
            "output_dir": str(tmp_path),
            "channels": [
                {
                    "name": "typo",
                    "url": "https://example.com/typo",
                    "auto_transcript": "all",
                    "transcript_source": "captions",
                },
                {"name": "good", "url": "https://example.com/good", "auto_transcript": "all"},
            ],
        }


class TestScanDryRunPreflight:
    def test_dry_run_reports_the_typo_and_makes_no_gemini_call(self, tmp_path, monkeypatch, caplog):
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=True)

        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(dry_run=True), env.config(tmp_path))

        error_messages = [
            r.message for r in caplog.records if r.levelname == "ERROR" and "transcript_source" in r.message
        ]
        assert len(error_messages) == 1, f"expected one transcript_source preflight error, got: {error_messages}"
        assert "typo" in error_messages[0]
        assert "captions" in error_messages[0]
        # No AssertionError from _RaisesIfCalled means no paid call happened;
        # this is the direct behavioral proof the dry run never reached one.

    def test_dry_run_still_previews_the_healthy_channel_alongside_the_typoed_one(self, tmp_path, monkeypatch, caplog):
        """One bad channel must not suppress the preview of the others -
        --dry-run's original job (listing new videos) keeps working."""
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=True)

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True), env.config(tmp_path))

        info_messages = [r.message for r in caplog.records if r.levelname == "INFO"]
        assert any("Good video" in m for m in info_messages), (
            f"expected the healthy channel's video previewed in dry-run output, got: {info_messages}"
        )

        error_messages = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert any("typo" in m and "transcript_source" in m for m in error_messages)

    def test_dry_run_note_says_the_listed_videos_would_not_be_processed(self, tmp_path, monkeypatch, caplog):
        """Reporting the typo is only half the fix: without this NOTE the preview
        contradicts itself, announcing "Found N videos, N new" and listing them
        for a channel a real run skips outright. Found by executing Gate 1 on the
        real CLI, not by reading the diff."""
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=True)

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True), env.config(tmp_path))

        notes = [r.message for r in caplog.records if "would NOT be processed" in r.message]
        assert len(notes) == 1, f"expected exactly one dry-run skip NOTE, got: {notes}"
        assert "typo1ch" in notes[0] or "typo" in notes[0], notes[0]
        assert "SKIPS this channel" in notes[0]
        # The NOTE is an ERROR, not an INFO: it corrects a number the operator
        # would otherwise act on.
        note_records = [r for r in caplog.records if "would NOT be processed" in r.message]
        assert note_records[0].levelname == "ERROR"

    def test_dry_run_note_is_absent_for_a_non_blocking_knob_problem(self, tmp_path, monkeypatch, caplog):
        """A bad mindmap_source fails each video's mindmap but does NOT skip the
        channel, so claiming "a real run SKIPS this channel" would be a false
        alarm - the exact failure class this repo refuses to ship."""
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=True)
        config = env.config(tmp_path)
        for ch in config["channels"]:
            ch.pop("transcript_source", None)
            ch.pop("chunk_minutes", None)
            ch["mindmap_source"] = "transcrpt"

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True), config)

        assert any("mindmap_source" in r.message for r in caplog.records if r.levelname == "ERROR")
        assert not [r for r in caplog.records if "would NOT be processed" in r.message], (
            "a non-blocking knob problem must not claim the channel is skipped"
        )

    def test_dry_run_note_says_aborts_and_no_later_channel_for_a_typoed_prompt(self, tmp_path, monkeypatch, caplog):
        """Round 2 finding 1: a bad `prompt` does not skip a channel, it kills
        the whole run mid-scan. The dry-run NOTE must say so explicitly and
        must not use the SKIPS wording."""
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=True)
        config = env.config(tmp_path)
        typo_ch = next(c for c in config["channels"] if c["name"] == "typo")
        typo_ch.pop("transcript_source", None)
        typo_ch["prompt"] = "does-not-exist-xyz"

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True), config)

        notes = [r.message for r in caplog.records if "would NOT be processed" in r.message]
        assert len(notes) == 1, f"expected exactly one dry-run NOTE, got: {notes}"
        assert "ABORTS" in notes[0]
        assert "no later channel would run" in notes[0]
        assert "SKIPS" not in notes[0]

    def test_aborts_note_takes_precedence_over_skips_note_when_a_channel_has_both_problems(
        self, tmp_path, monkeypatch, caplog
    ):
        """A channel with BOTH a typoed prompt (ABORTS) and a typoed
        transcript_source (SKIPS) must emit exactly one NOTE line, and it must
        be the ABORTS one - aborting the scan is strictly worse than (and
        subsumes) skipping the one channel."""
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=True)
        config = env.config(tmp_path)
        typo_ch = next(c for c in config["channels"] if c["name"] == "typo")
        # transcript_source stays "captions" (invalid) from env.config(), and
        # auto_transcript stays "all", so the SKIPS consequence also fires.
        typo_ch["prompt"] = "does-not-exist-xyz"

        with caplog.at_level("INFO"):
            vi.cmd_scan(_scan_args(dry_run=True), config)

        notes = [r.message for r in caplog.records if "would NOT be processed" in r.message]
        assert len(notes) == 1, f"expected exactly one dry-run NOTE, got: {notes}"
        assert "ABORTS" in notes[0]
        assert "SKIPS" not in notes[0]

    def test_typoed_transcript_source_without_auto_transcript_logs_warning_with_manual_commands_wording(
        self, tmp_path, monkeypatch, caplog
    ):
        """Round 2 finding 2 (the rename): the preflight must never call this
        knob 'inert'. A channel with no auto_transcript still fails the
        manual transcript/mindmap/process --channel commands (issue #127
        resolves the same channel dict), so the log line must say so, and it
        must be a WARNING (not an ERROR - a real scan is unaffected)."""
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=True)
        config = env.config(tmp_path)
        typo_ch = next(c for c in config["channels"] if c["name"] == "typo")
        typo_ch.pop("auto_transcript", None)
        # transcript_source stays "captions" (invalid) from env.config().

        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(dry_run=True), config)

        warning_records = [r for r in caplog.records if r.levelname == "WARNING" and "transcript_source" in r.message]
        assert len(warning_records) == 1, f"expected one WARNING-level transcript_source line, got: {warning_records}"
        assert "still fails the manual" in warning_records[0].message
        assert "inert" not in warning_records[0].message

        error_records = [r for r in caplog.records if r.levelname == "ERROR" and "transcript_source" in r.message]
        assert error_records == [], f"a NOT_REACHED_BY_SCAN problem must log at WARNING, not ERROR: {error_records}"


class TestScanNonDryRunRoutingUnchanged:
    def test_typo_channel_still_skipped_at_the_existing_site_healthy_channel_still_processed(
        self, tmp_path, monkeypatch, caplog
    ):
        """The preflight is report-only: a real scan's routing (who gets
        skipped, who gets processed, who lands in the failure summary) must
        be byte-identical to pre-#169 behavior. Only the diagnostics change.
        """
        env = _ScanEnvironment()
        env.wire(monkeypatch, dry_run=False)

        with caplog.at_level("WARNING"):
            vi.cmd_scan(_scan_args(dry_run=False), env.config(tmp_path))

        assert "good1" in env.transcripts_seen, "the healthy channel's transcript must still run"
        assert "typo1" not in env.transcripts_seen, "the typo channel is still skipped at the existing site"
        assert "good1" in env.mindmaps_seen
        assert "typo1" not in env.mindmaps_seen

        # get_channel_id still ran for the typo channel too - the preflight
        # never skips anything, it only reports.
        assert "https://example.com/typo" in env.get_channel_id_calls

        error_messages = [
            r.message for r in caplog.records if r.levelname == "ERROR" and "transcript_source" in r.message
        ]
        # The preflight logs the problem once at the top of the loop, and the
        # existing runtime skip site logs it again when the skip actually
        # happens - two ERROR lines about the same channel, by design.
        assert len(error_messages) == 2, f"expected preflight + skip-site error lines, got: {error_messages}"
        assert all("typo" in m for m in error_messages)

        summary_lines = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("FAILED" in line for line in summary_lines), (
            f"expected the typo channel in the end-of-scan failure summary, got: {summary_lines}"
        )
        assert any("typo" in line for line in summary_lines)
