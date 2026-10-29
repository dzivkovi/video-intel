"""`assess-transcripts`: run the quality assessor over transcripts never assessed.

The command reads `.transcript.md` files already on disk (no Gemini call) and
writes the six quality fields the transcript writers persist, demoting a
healthy status to `partial` on a severe flag. These tests drive the real
`cmd_assess_transcripts` over a synthetic corpus, and the parser contract
through the real `main()`.
"""

import ast
import csv
import inspect
import json
import sys
import textwrap
import types as _types

import pytest

import video_intel as vi

QUALITY_FIELDS = (
    "transcript_quality_flags",
    "transcript_max_blind_gap_seconds",
    "transcript_blind_gap_at_seconds",
    "transcript_last_dialogue_fraction",
    "transcript_dialogue_entries",
    "transcript_timestamp_overrun_entries",
)


def _md(body_lines: list[str], title: str = "T") -> str:
    header = (
        f"# Transcript: {title}\n\n"
        "**Source:** https://www.youtube.com/watch?v=abc\n"
        "**Published:** 2026-06-01\n"
        "**Status:** Complete\n\n"
        "---\n\n"
    )
    return header + "\n\n".join(body_lines) + "\n"


def _healthy_body(duration: int, step: int = 20) -> list[str]:
    lines = []
    for secs in range(0, duration, step):
        lines.append(f'[{secs // 60:02d}:{secs % 60:02d}] Host (Speaker): "line at {secs}"')
        if secs % 120 == 0:
            lines.append(f"  SCREEN [{secs // 60:02d}:{secs % 60:02d}-{secs // 60:02d}:{secs % 60 + 5:02d}] slide")
    return lines


MONOLITHIC_BODY = ['[00:00] Host: "everything at once"', '[19:50] Host: "bye"']


def _write(chan, prefix, meta, transcript=None):
    chan.mkdir(exist_ok=True)
    meta_path = chan / f"{prefix}.meta.json"
    if isinstance(meta, bytes):
        meta_path.write_bytes(meta)
    else:
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if transcript is not None:
        (chan / f"{prefix}.transcript.md").write_text(transcript, encoding="utf-8")
    return meta_path


def _meta(**extra):
    base = {
        "video_id": "vid",
        "channel": "chan",
        "title": "T",
        "transcript_status": "complete",
        "duration_seconds": 1200,
        "last_error": "kept on purpose",
        "modes_completed": ["transcript"],
        "processed": "2026-01-01T00:00:00+00:00",
    }
    base.update(extra)
    return base


def _args(apply=False, force=False, channel=None, backup_dir=None):
    return _types.SimpleNamespace(apply=apply, force=force, channel=channel, backup_dir=backup_dir)


def _run(tmp_path, **kw):
    return vi.cmd_assess_transcripts(_args(**kw), {"output_dir": str(tmp_path / "corpus")})


def _snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    return root


class TestSelection:
    def test_default_selects_only_flagless_metas_and_force_selects_all(self, tmp_path, corpus):
        chan = corpus / "chan"
        _write(chan, "fresh", _meta(video_id="a"), _md(_healthy_body(1200)))
        _write(
            chan,
            "assessed",
            _meta(video_id="b", transcript_quality_flags=[]),
            _md(_healthy_body(1200)),
        )
        counts = _run(tmp_path)
        assert counts["candidates"] == 1
        counts = _run(tmp_path, force=True)
        assert counts["candidates"] == 2

    def test_channel_restricts(self, tmp_path, corpus):
        _write(corpus / "one", "v", _meta(), _md(_healthy_body(1200)))
        _write(corpus / "two", "v", _meta(), _md(_healthy_body(1200)))
        assert _run(tmp_path, channel="two")["candidates"] == 1

    def test_underscore_and_dot_folders_are_skipped(self, tmp_path, corpus):
        _write(corpus / "_standalone", "v", _meta(), _md(_healthy_body(1200)))
        _write(corpus / ".hidden", "v", _meta(), _md(_healthy_body(1200)))
        assert _run(tmp_path)["candidates"] == 0

    def test_skips_create_or_change_nothing(self, tmp_path, corpus):
        chan = corpus / "chan"
        _write(chan, "no-transcript", _meta())
        _write(chan, "unparseable", b'{"video_id": "x", "transcript_st', _md(_healthy_body(1200)))
        _write(chan, "torn-utf8", b'{"title": "\xd0', _md(_healthy_body(1200)))
        _write(chan, "identityless", {}, _md(_healthy_body(1200)))
        _write(chan, "errored", _meta(transcript_status="error: boom"), _md(_healthy_body(1200)))
        _write(chan, "odd-status", _meta(transcript_status=42), _md(_healthy_body(1200)))
        before = _snapshot(corpus)
        counts = _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        assert _snapshot(corpus) == before
        assert counts["candidates"] == 0
        assert counts["skipped_unparseable"] == 3
        assert not (tmp_path / "bk" / "chan").exists()

    def test_zero_entries_is_skipped_never_assessed(self, tmp_path, corpus):
        chan = corpus / "chan"
        meta_path = _write(chan, "v", _meta(), _md(["no stamps here"]))
        before = meta_path.read_bytes()
        counts = _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        assert counts["skipped_no_entries"] == 1
        assert counts["candidates"] == 0
        assert meta_path.read_bytes() == before


class TestAssessment:
    def test_healthy_transcript_gets_six_fields_and_keeps_status(self, tmp_path, corpus):
        meta_path = _write(corpus / "chan", "v", _meta(), _md(_healthy_body(1200)))
        _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for field in QUALITY_FIELDS:
            assert field in meta, field
        assert meta["transcript_quality_flags"] == []
        assert meta["transcript_status"] == "complete"
        # No other key is touched: not the success-path writer.
        assert meta["last_error"] == "kept on purpose"
        assert meta["modes_completed"] == ["transcript"]
        assert meta["processed"] == "2026-01-01T00:00:00+00:00"

    def test_monolithic_transcript_is_severe_and_demoted_to_partial(self, tmp_path, corpus):
        meta_path = _write(corpus / "chan", "v", _meta(), _md(MONOLITHIC_BODY))
        counts = _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert vi.QUALITY_FLAG_MONOLITHIC_SEVERE in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "partial"
        assert counts["severe"] == 1
        assert counts["demoted"] == 1

    def test_a_meta_with_no_status_at_all_is_assessed_and_gets_its_first_status(self, tmp_path, corpus):
        """The oldest writer left transcripts with no transcript_status; the catalog shows them as not assessed."""
        meta = _meta()
        del meta["transcript_status"]
        healthy = _write(corpus / "chan", "p-old", meta, _md(_healthy_body(1200)))
        meta2 = _meta()
        del meta2["transcript_status"]
        collapsed = _write(corpus / "chan", "p-old-bad", meta2, _md(MONOLITHIC_BODY))
        _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        got = json.loads(healthy.read_text(encoding="utf-8"))
        assert got["transcript_status"] == "complete" and all(k in got for k in QUALITY_FIELDS)
        bad = json.loads(collapsed.read_text(encoding="utf-8"))
        assert bad["transcript_status"] == "partial" and "monolithic_severe" in bad["transcript_quality_flags"]
        assert got["last_error"] == "kept on purpose" and "processed" in got

    def test_severe_on_a_non_healthy_status_keeps_that_status(self, tmp_path, corpus):
        meta_path = _write(
            corpus / "chan",
            "v",
            _meta(transcript_status=vi.TRANSCRIPT_STATUS_TRUNCATED),
            _md(MONOLITHIC_BODY),
        )
        counts = _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["transcript_status"] == vi.TRANSCRIPT_STATUS_TRUNCATED
        assert counts["demoted"] == 0

    @pytest.mark.parametrize(
        ("body", "duration"),
        [
            (_healthy_body(1200), 1200),
            (MONOLITHIC_BODY, 1200),
            (_healthy_body(600, step=5) + ['[05:00] Host: "rewind"'], 900),
            (_healthy_body(1200), None),
            (_healthy_body(1200), True),
        ],
        ids=["healthy", "monolithic", "backward-jump", "unknown-duration", "bool-duration"],
    )
    def test_persisted_values_equal_an_independent_assessment(self, tmp_path, corpus, body, duration):
        text = _md(body)
        meta_path = _write(corpus / "chan", "v", _meta(duration_seconds=duration), text)
        _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expected_duration = duration if isinstance(duration, int) and not isinstance(duration, bool) else None
        metrics = vi.assess_transcript_artifact(vi.dialogue_entries_from_transcript_markdown(text), expected_duration)
        assert meta["transcript_quality_flags"] == sorted(set(metrics["severe"]) | set(metrics["mild"]))
        assert meta["transcript_max_blind_gap_seconds"] == metrics["max_blind_gap_seconds"]
        assert meta["transcript_blind_gap_at_seconds"] == metrics["blind_gap_at_seconds"]
        assert meta["transcript_last_dialogue_fraction"] == metrics["last_dialogue_fraction"]
        assert meta["transcript_dialogue_entries"] == metrics["dialogue_entries"]
        assert meta["transcript_timestamp_overrun_entries"] == metrics["timestamp_overrun_entries"]

    def test_captions_leading_gap_gets_the_captions_writer_demotion(self, tmp_path, corpus):
        body = [f'[{m:02d}:00] "cue {m}"' for m in range(12, 20)]
        meta_path = _write(
            corpus / "chan",
            "v",
            _meta(transcript_source=vi.TRANSCRIPT_SOURCE_CAPTIONS, duration_seconds=1200),
            _md(body),
        )
        _run(tmp_path, apply=True, backup_dir=str(tmp_path / "bk"))
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert vi.QUALITY_FLAG_BLIND_GAP_SEVERE not in meta["transcript_quality_flags"]
        assert vi.QUALITY_FLAG_CAPTIONS_LEADING_GAP_MILD in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "complete"


class TestDryRunAndBackups:
    def test_dry_run_writes_nothing(self, tmp_path, corpus):
        _write(corpus / "chan", "a", _meta(), _md(_healthy_body(1200)))
        _write(corpus / "chan", "b", _meta(), _md(MONOLITHIC_BODY))
        before = _snapshot(tmp_path)
        counts = _run(tmp_path)
        assert _snapshot(tmp_path) == before
        assert counts["candidates"] == 2
        assert counts["demoted"] == 1

    def test_backup_and_manifest_exist_before_the_original_changes(self, tmp_path, corpus, monkeypatch):
        chan = corpus / "chan"
        a = _write(chan, "a", _meta(video_id="ida"), _md(MONOLITHIC_BODY))
        b = _write(chan, "b", _meta(video_id="idb"), _md(_healthy_body(1200)))
        unchanged = _write(
            chan,
            "c",
            _meta(video_id="idc", transcript_quality_flags=["stale"]),
            _md(_healthy_body(1200)),
        )
        originals = {p: p.read_bytes() for p in (a, b, unchanged)}
        backup_dir = tmp_path / "bk"
        real_replace = vi.os.replace
        seen = []

        def checking_replace(src, dst):
            from pathlib import Path

            dst = Path(dst)
            if dst in originals:
                backup = backup_dir / "chan" / dst.name
                assert backup.read_bytes() == originals[dst]
                assert dst.read_bytes() == originals[dst], "original changed before its backup was verified"
                rows = list(csv.reader((backup_dir / "manifest.csv").open(encoding="utf-8")))
                assert dst.name in [r[1] for r in rows[1:]]
                seen.append(dst.name)
            return real_replace(src, dst)

        monkeypatch.setattr(vi.os, "replace", checking_replace)
        _run(tmp_path, apply=True, backup_dir=str(backup_dir))
        assert sorted(seen) == ["a.meta.json", "b.meta.json"]
        assert a.read_bytes() != originals[a]
        assert unchanged.read_bytes() == originals[unchanged]
        assert not (backup_dir / "chan" / "c.meta.json").exists()
        rows = list(csv.reader((backup_dir / "manifest.csv").open(encoding="utf-8")))
        assert rows[0] == ["channel", "meta_file", "video_id", "flags", "transcript_status"]
        by_file = {r[1]: r for r in rows[1:]}
        assert by_file["a.meta.json"][0] == "chan"
        assert by_file["a.meta.json"][2] == "ida"
        assert vi.QUALITY_FLAG_MONOLITHIC_SEVERE in by_file["a.meta.json"][3].split(";")
        assert by_file["a.meta.json"][4] == "partial"
        assert by_file["b.meta.json"][4] == "complete"

    def test_failed_backup_verification_stops_before_the_write(self, tmp_path, corpus, monkeypatch):
        meta_path = _write(corpus / "chan", "a", _meta(), _md(MONOLITHIC_BODY))
        before = meta_path.read_bytes()
        backup_dir = tmp_path / "bk"
        (backup_dir / "chan").mkdir(parents=True)
        (backup_dir / "chan" / "a.meta.json").write_bytes(b"an older, different original")
        with pytest.raises(SystemExit) as exc:
            _run(tmp_path, apply=True, backup_dir=str(backup_dir))
        assert exc.value.code == 1
        assert meta_path.read_bytes() == before
        assert (backup_dir / "chan" / "a.meta.json").read_bytes() == b"an older, different original"


class TestParserContract:
    def test_apply_without_backup_dir_is_a_parser_error(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(vi, "load_config", lambda: {"output_dir": str(tmp_path)})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
        monkeypatch.setattr(vi, "cmd_assess_transcripts", lambda *a: called.append(a))
        monkeypatch.setattr(sys, "argv", ["video_intel.py", "assess-transcripts", "--apply"])
        with pytest.raises(SystemExit) as exc:
            vi.main()
        assert exc.value.code == 2
        assert called == []

    def test_main_dispatches_without_channels_in_config(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(vi, "load_config", lambda: {"output_dir": str(tmp_path)})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_k: None)
        monkeypatch.setattr(vi, "cmd_assess_transcripts", lambda args, config: called.append(args))
        monkeypatch.setattr(
            sys, "argv", ["video_intel.py", "assess-transcripts", "--apply", "--backup-dir", str(tmp_path / "bk")]
        )
        vi.main()
        assert len(called) == 1
        assert called[0].apply is True

    def test_it_snapshots_the_config_like_other_corpus_writers(self):
        assert "assess-transcripts" in vi.CONFIG_BACKUP_COMMANDS


class TestDialogueEntryParser:
    def test_captions_and_gemini_lines_are_entries_and_screen_lines_are_not(self):
        text = _md(
            [
                '[00:02] "Hello"',
                '[00:05] Dan Shipper (Host): "text"',
                "  SCREEN [00:05-00:09] slide title",
                "SCREEN [00:10-00:12] flush-left screen",
                '[100:30] "past ninety-nine minutes"',
                '[1:01:33] Guest: "hour form"',
                "[not a timestamp] prose",
            ]
        )
        assert vi.dialogue_entries_from_transcript_markdown(text) == [
            {"start": "00:02", "text": '"Hello"'},
            {"start": "00:05", "text": 'Dan Shipper (Host): "text"'},
            {"start": "100:30", "text": '"past ninety-nine minutes"'},
            {"start": "1:01:33", "text": 'Guest: "hour form"'},
        ]

    def test_file_order_is_kept(self):
        text = _md(['[05:00] "b"', '[01:00] "a"'])
        assert [e["start"] for e in vi.dialogue_entries_from_transcript_markdown(text)] == ["05:00", "01:00"]

    def test_header_lines_are_never_entries(self):
        text = textwrap.dedent(
            """\
            # Transcript: [00:01] a title that looks like a stamp
            [00:02] header line before the rule
            ---
            [00:03] "body"
            """
        )
        assert vi.dialogue_entries_from_transcript_markdown(text) == [{"start": "00:03", "text": '"body"'}]

    def test_a_title_with_no_rule_is_all_header(self):
        assert vi.dialogue_entries_from_transcript_markdown('# Transcript: T\n[00:01] "x"\n') == []


def _parser_regex_calls() -> list[str]:
    """Every `re.<fn>(` call inside the parser, keyed on the call, not the constant.

    A regex that stopped using the shared pattern is still SEEN here and
    reported, instead of quietly dropping out of the sample.
    """
    source = textwrap.dedent(inspect.getsource(vi.dialogue_entries_from_transcript_markdown))
    calls = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
        ):
            calls.append(ast.unparse(node))
    return calls


class TestParserUsesTheSharedBoundaryPattern:
    def test_the_walk_finds_the_parser_regex(self):
        calls = _parser_regex_calls()
        assert len(calls) == 1, f"expected exactly one regex in the parser, found {calls}"

    def test_the_parser_regex_is_built_from_the_shared_pattern(self):
        offenders = [c for c in _parser_regex_calls() if "ENTRY_TIMESTAMP_PATTERN" not in c]
        assert offenders == [], f"the parser hard-codes its timestamp regex: {offenders}"
