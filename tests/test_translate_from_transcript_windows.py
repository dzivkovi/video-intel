"""`--from-transcript` windows, honors --start/--end, and tells the truth
about the partial it preserves (issue #206).

Three defects from one live run: a 1h58m interview (138 KB, 683 entries) sent
as ONE streamed request died at `503 UNAVAILABLE. The request timed out.` about
5.5 minutes in, having translated roughly the first 46 minutes. The log said
"Partial output preserved in .txt.tmp file" and no .txt.tmp existed anywhere.

  1. No chunking. The video path has auto-chunked at `--chunk-minutes` since
     the beginning, and the README sells the rich-transcript path as the way to
     do a 2-hour video - exactly the input that exceeds one call's deadline.
  2. `--start` / `--end` were accepted by the parser and silently dropped:
     `main()` never passed them and the function had no parameter to take them.
     Silent no-op is the worst of the three, because nothing tells the operator.
  3. The "partial preserved" error was FALSE on this path. `_stream_with_timeouts`
     logs it, but this writer opened its tmp only AFTER a successful return, so
     the billed partial was discarded and the log said the opposite.

These tests drive the REAL `_translate_from_transcript` with only the Gemini
stream stubbed, because the first two defects live in the caller's plumbing,
not in any helper - a helper-only suite would pass over all three.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import translate_video as tv


def make_transcript(minutes: int) -> str:
    head = "# A Long Interview\n\n**Source URL:** https://youtu.be/XXXX\n**Published:** 2026-08-27\n\n"
    body = "".join(
        f'[{m // 60:02d}:{m % 60:02d}:00] Host (Interviewer): "Line at minute {m}."\n'
        f"  SCREEN [{m // 60:02d}:{m % 60:02d}:00] [slide]: A slide.\n\n"
        for m in range(minutes)
    )
    return head + body


class _Types:
    class ThinkingConfig:
        def __init__(self, **kw):
            pass

    class GenerateContentConfig:
        def __init__(self, **kw):
            pass


class Recorder:
    """Stands in for the Gemini stream. Records every payload it was handed,
    and can fail on the Nth call while writing a partial to `tmp_file` exactly
    as the real `_stream_with_timeouts` does."""

    def __init__(self, fail_at: int | None = None, empty_windows: set[int] | None = None):
        self.payloads: list[str] = []
        self.fail_at = fail_at
        self.empty_windows = empty_windows or set()

    def __call__(self, client, model, contents, config, tmp_file=None):
        self.payloads.append(contents)
        n = len(self.payloads)
        # Write a UNIQUE marker on EVERY call, the way the real
        # `_stream_with_timeouts` streams each window's text as it arrives.
        # The first cut wrote only on the failing call, which made the tmp
        # handle untestable: reopening it per window (truncating away windows
        # 1..N-1) left the whole suite green while destroying two thirds of a
        # billed translation. A stub that only exercises the failure path
        # cannot detect a defect on the success path.
        if tmp_file:
            tmp_file.write(f"<<W{n}>>")
            tmp_file.flush()
        if self.fail_at is not None and n == self.fail_at:
            raise RuntimeError("503 UNAVAILABLE. The request timed out.")
        if n in self.empty_windows:
            return ("", {"total_token_count": 5}, "STOP")
        return (f"[BCS window {n}] 00:00", {"total_token_count": 1000}, "STOP")


@pytest.fixture
def harness(monkeypatch):
    def _run(
        tmp_path,
        *,
        minutes=118,
        chunk_minutes=20,
        start=None,
        end=None,
        fail_at=None,
        empty_windows=None,
    ):
        rec = Recorder(fail_at=fail_at, empty_windows=empty_windows)
        monkeypatch.setattr(tv, "_stream_with_timeouts", rec)
        monkeypatch.setattr(tv, "translate_title", lambda c, m, t: "BCS::" + t)
        monkeypatch.setattr(tv, "create_client", lambda k: object())
        monkeypatch.setattr(tv, "require_gemini", lambda: (None, _Types))
        monkeypatch.setattr(tv, "build_permissive_safety_settings", lambda types: [])
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        src = tmp_path / "long.transcript.md"
        src.write_text(make_transcript(minutes), encoding="utf-8")
        out = tmp_path / "out"
        error = None
        try:
            tv._translate_from_transcript(
                transcript_path=src,
                model_name="gemini-2.5-pro",
                output_dir=out,
                use_stdout=False,
                force=True,
                chunk_minutes=chunk_minutes,
                start_minutes=start,
                end_minutes=end,
            )
        except BaseException as e:
            error = e
        return rec, out, error

    return _run


class TestDefectOneNoChunking:
    def test_a_two_hour_transcript_becomes_many_calls_not_one(self, tmp_path, harness):
        """The whole point. Pre-fix this was a single streamed request over the
        entire 138 KB body, which is what exceeded the server deadline."""
        rec, out, error = harness(tmp_path, minutes=118, chunk_minutes=20)
        assert error is None
        assert len(rec.payloads) == 6, "a 118-minute transcript at 20-minute windows is 6 calls"
        assert (out / "long.translate-bcs.txt").exists()

    def test_a_short_transcript_still_takes_exactly_one_call(self, tmp_path, harness):
        """Windowing must not turn a 10-minute transcript into a multi-call run."""
        rec, _out, error = harness(tmp_path, minutes=10, chunk_minutes=20)
        assert error is None
        assert len(rec.payloads) == 1

    def test_the_preamble_is_translated_once_not_per_window(self, tmp_path, harness):
        """Feeding the title/source/date block to every window would stitch N
        translated headers into one file."""
        rec, _out, _error = harness(tmp_path, minutes=118, chunk_minutes=20)
        assert "Source URL" in rec.payloads[0]
        assert all("Source URL" not in p for p in rec.payloads[1:])

    def test_no_entry_is_lost_or_duplicated_across_windows(self, tmp_path, harness):
        """Every minute appears in exactly one payload. A chunker that dropped
        an entry at a boundary, or double-sent it, would still 'work'."""
        rec, _out, _error = harness(tmp_path, minutes=118, chunk_minutes=20)
        seen = [int(t.split()[0].rstrip('."')) for p in rec.payloads for t in p.split("Line at minute ")[1:]]
        assert sorted(seen) == list(range(118))
        assert len(seen) == len(set(seen)), "an entry was sent in two windows"

    def test_a_screen_block_stays_with_the_entry_above_it(self, tmp_path, harness):
        """Non-timestamped lines belong to the entry they follow. Splitting one
        away from its dialogue line would hand a window an orphan block."""
        rec, _out, _error = harness(tmp_path, minutes=118, chunk_minutes=20)
        for payload in rec.payloads:
            assert payload.count("Line at minute") == payload.count("SCREEN")


class TestDefectTwoStartEndIgnored:
    def test_start_and_end_actually_narrow_the_body(self, tmp_path, harness):
        rec, _out, error = harness(tmp_path, minutes=118, chunk_minutes=20, start=30, end=45)
        assert error is None
        seen = sorted({int(t.split()[0].rstrip('."')) for p in rec.payloads for t in p.split("Line at minute ")[1:]})
        # The end is EXCLUSIVE: `--end 45` stops before the 45:00 cue, so that
        # a following `--start 45` picks it up exactly once. Consecutive ranges
        # must tile, because the documented recovery for a missing window is a
        # series of adjacent ranges - an inclusive end would duplicate every
        # boundary cue across two runs.
        assert seen == list(range(30, 45)), "the range was ignored or applied off-by-one"

    def test_start_alone_runs_to_the_end(self, tmp_path, harness):
        rec, _out, _error = harness(tmp_path, minutes=60, chunk_minutes=20, start=50)
        seen = sorted({int(t.split()[0].rstrip('."')) for p in rec.payloads for t in p.split("Line at minute ")[1:]})
        assert seen == list(range(50, 60))

    def test_an_empty_range_refuses_loudly_instead_of_translating_nothing(self, tmp_path, harness):
        """Silently sending an empty body would bill for a call and produce a
        file with no content, which is the same class of quiet wrong as the
        no-op this defect was."""
        rec, _out, error = harness(tmp_path, minutes=60, chunk_minutes=20, start=200, end=210)
        assert isinstance(error, SystemExit) and error.code == 1
        assert rec.payloads == [], "an empty range must cost no Gemini call"

    def test_no_range_sends_the_whole_body(self, tmp_path, harness):
        rec, _out, _error = harness(tmp_path, minutes=40, chunk_minutes=20)
        seen = {int(t.split()[0].rstrip('."')) for p in rec.payloads for t in p.split("Line at minute ")[1:]}
        assert seen == set(range(40))


class TestDefectThreeThePartialMessageWasFalse:
    def test_a_mid_run_failure_leaves_the_tmp_file_the_log_promises(self, tmp_path, harness):
        """The headline lie. `_stream_with_timeouts` logs "Partial output
        preserved in .txt.tmp file"; pre-fix this writer opened its tmp only
        after a successful return, so nothing existed to preserve."""
        rec, out, error = harness(tmp_path, minutes=118, chunk_minutes=20, fail_at=3)
        assert isinstance(error, RuntimeError)
        assert len(rec.payloads) == 3, "the run should stop at the failing window"
        tmps = list(out.glob("*.txt.tmp"))
        assert tmps, "the log promised a .txt.tmp and there is none - the original defect"
        preserved = tmps[0].read_text(encoding="utf-8")
        # Not merely "non-empty" - that passes for a handle reopened per window,
        # which would hold ONLY the failing window's partial and have silently
        # thrown away two thirds of a billed translation. Name every window that
        # must still be there.
        assert "<<W1>>" in preserved and "<<W2>>" in preserved, (
            f"earlier windows were lost from the tmp file: {preserved!r}"
        )
        assert "<<W3>>" in preserved, "the failing window's own partial was not preserved"

    def test_the_stream_handler_receives_a_live_handle_on_every_window(self, tmp_path, harness):
        """Not just on the first call. A tmp opened per-window, or only for
        window 1, would preserve nothing when window 5 fails."""
        seen_handles = []

        def recorder(client, model, contents, config, tmp_file=None):
            # Record liveness AT CALL TIME. Checking `.closed` after the run
            # would always be True - the writer closes the handle in a finally
            # once every window is done, which is correct.
            seen_handles.append((tmp_file is not None, tmp_file is not None and not tmp_file.closed))
            return ("[BCS] 00:00", None, "STOP")

        import pytest as _pytest

        mp = _pytest.MonkeyPatch()
        try:
            mp.setattr(tv, "_stream_with_timeouts", recorder)
            mp.setattr(tv, "translate_title", lambda c, m, t: t)
            mp.setattr(tv, "create_client", lambda k: object())
            mp.setattr(tv, "require_gemini", lambda: (None, _Types))
            mp.setattr(tv, "build_permissive_safety_settings", lambda types: [])
            mp.setenv("GEMINI_API_KEY", "k")
            src = tmp_path / "x.transcript.md"
            src.write_text(make_transcript(80), encoding="utf-8")
            tv._translate_from_transcript(
                transcript_path=src,
                model_name="gemini-2.5-pro",
                output_dir=tmp_path / "o",
                use_stdout=False,
                force=True,
                chunk_minutes=20,
            )
        finally:
            mp.undo()
        assert len(seen_handles) >= 4, "an 80-minute transcript at 20-minute windows is 4+ calls"
        assert all(present for present, _live in seen_handles), (
            "a window was handed no tmp_file, so its partial would be discarded"
        )
        assert all(live for _present, live in seen_handles), "the handle was already closed when a later window ran"

    def test_stdout_mode_opens_no_tmp_file(self, tmp_path, monkeypatch):
        """`--stdout` writes nothing by contract, so it must not create a tmp
        as a side effect of the partial-preservation fix."""
        seen = []
        monkeypatch.setattr(
            tv,
            "_stream_with_timeouts",
            lambda c, m, contents, cfg, tmp=None: (seen.append(tmp), ("x 00:00", None, "STOP"))[1],
        )
        monkeypatch.setattr(tv, "translate_title", lambda c, m, t: t)
        monkeypatch.setattr(tv, "create_client", lambda k: object())
        monkeypatch.setattr(tv, "require_gemini", lambda: (None, _Types))
        monkeypatch.setattr(tv, "build_permissive_safety_settings", lambda types: [])
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        src = tmp_path / "s.transcript.md"
        src.write_text(make_transcript(10), encoding="utf-8")
        out = tmp_path / "o"
        tv._translate_from_transcript(
            transcript_path=src,
            model_name="gemini-2.5-pro",
            output_dir=out,
            use_stdout=True,
            force=True,
            chunk_minutes=20,
        )
        assert seen == [None]
        assert not list(out.glob("*.tmp")) if out.exists() else True


class TestWindowingHelpers:
    def test_a_window_boundary_never_splits_an_entry(self):
        body = "[00:00] a\n  detail\n[00:19] b\n  detail\n[00:21] c\n  detail\n"
        windows = tv.split_transcript_into_windows(body, 20)
        for _s, _e, text in windows:
            assert text.count("detail") == text.count("] ")

    def test_a_long_gap_does_not_start_a_new_window_per_later_cue(self):
        """The first cut asserted "no empty windows", which the splitter cannot
        produce (a window is only emitted when it has content), so the test
        guarded nothing - deleting the boundary-advance loop left it green. The
        real property is about COST: after a three-hour silence, the boundary
        must catch up in one step, or every subsequent cue starts its own
        window and a transcript with one break becomes N Gemini calls.
        """
        body = "[00:00:00] a\n\n[03:00:00] b\n\n[03:01:00] c\n\n[03:02:00] d\n\n"
        windows = tv.split_transcript_into_windows(body, 20)
        assert len(windows) == 2, (
            f"the three post-gap cues should share one window, got {len(windows)}: {[(w[0], w[1]) for w in windows]}"
        )
        # And nothing was lost putting them there.
        assert "".join(w[2] for w in windows) == body

    def test_a_body_with_no_timestamps_is_one_window(self):
        windows = tv.split_transcript_into_windows("just prose\n", 20)
        assert len(windows) == 1

    def test_a_non_positive_chunk_size_is_refused(self):
        """`chunk_minutes: 0` would divide the transcript into infinite windows."""
        with pytest.raises(ValueError):
            tv.split_transcript_into_windows("[00:00] a\n", 0)

    def test_windows_use_the_unbounded_minute_shape(self):
        """The chunker shares issue #197's constants, so a stamp past 99:59 is
        a real boundary rather than an unrecognized line folded into the window
        above it - which is exactly the #195/#197 defect one layer up."""
        body = "[00:00] a\n[100:30] b\n[140:00] c\n"
        windows = tv.split_transcript_into_windows(body, 30)
        assert len(windows) == 3, "a [100:30] stamp was not seen as a boundary"


class TestOperationalSeparation:
    def test_no_video_intel_import_was_added(self):
        import ast

        source = (Path(__file__).resolve().parent.parent / "scripts" / "translate_video.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert "video_intel" not in imported
        assert "timestamp_utils" in imported, "the AST walk found no imports at all"


class TestTheCliActuallyReachesTheFeature:
    """The P0 the first cut shipped, and the reason the other 18 tests could not
    see it: every one of them called `_translate_from_transcript` DIRECTLY.

    The function honored `--chunk-minutes` / `--start` / `--end` perfectly while
    `main()` still carried three `parser.error(...)` refusals for those exact
    flags, left over from when this path had nowhere to apply them. So the whole
    feature was unreachable from the CLI and the suite was green.

    Same class as the standing "a helper tested but never proven called" rule,
    one layer up: there the wiring was a missing call, here it was an argument
    parser rejecting the call before it happened. These tests drive `main()`.
    """

    def _run_main(self, monkeypatch, tmp_path, argv, recorder=None):
        rec = recorder or Recorder()
        monkeypatch.setattr(tv, "_stream_with_timeouts", rec)
        monkeypatch.setattr(tv, "translate_title", lambda c, m, t: "BCS::" + t)
        monkeypatch.setattr(tv, "create_client", lambda k: object())
        monkeypatch.setattr(tv, "require_gemini", lambda: (None, _Types))
        monkeypatch.setattr(tv, "build_permissive_safety_settings", lambda types: [])
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        src = tmp_path / "long.transcript.md"
        src.write_text(make_transcript(118), encoding="utf-8")
        out = tmp_path / "out"
        full = ["translate_video.py", "--from-transcript", str(src), "--output-dir", str(out), "--force"]
        monkeypatch.setattr(sys, "argv", full + argv)
        code = 0
        try:
            tv.main()
        except SystemExit as e:
            code = e.code if e.code is not None else 0
        return rec, out, code

    def test_chunk_minutes_is_not_refused(self, monkeypatch, tmp_path):
        """Pre-fix: exit 2, zero Gemini calls, "mutually exclusive"."""
        rec, _out, code = self._run_main(monkeypatch, tmp_path, ["--chunk-minutes", "30"])
        assert code == 0, f"the CLI refused --chunk-minutes (exit {code})"
        # 118 minutes at 30-minute windows is 4, not the default 20's 6.
        assert len(rec.payloads) == 4, f"--chunk-minutes did not take effect: {len(rec.payloads)} calls"

    def test_start_and_end_are_not_refused_and_actually_narrow_the_work(self, monkeypatch, tmp_path):
        rec, _out, code = self._run_main(monkeypatch, tmp_path, ["--start", "30", "--end", "45"])
        assert code == 0, f"the CLI refused --start/--end (exit {code})"
        joined = "".join(rec.payloads)
        minutes = sorted({int(t.split()[0].rstrip('."')) for t in joined.split("Line at minute ")[1:]})
        assert minutes and min(minutes) >= 30 and max(minutes) < 45, (
            f"the range was accepted but not applied: {minutes[:3]}..{minutes[-3:]}"
        )

    def test_a_nonpositive_chunk_minutes_is_refused_before_any_gemini_call(self, monkeypatch, tmp_path):
        """Rejected at the PARSER, not inside the splitter. The splitter runs
        after `translate_title`, which is a paid call - so a library caller
        would get a traceback AND a bill. Probe before you pay.
        """
        rec, _out, code = self._run_main(monkeypatch, tmp_path, ["--chunk-minutes", "0"])
        assert code == 2, f"expected argparse exit 2, got {code}"
        assert rec.payloads == [], "a bad --chunk-minutes reached Gemini"


class TestAnEmptyWindowIsNeverSilent:
    """A window that returns nothing is a HOLE. The first cut logged it and
    carried on to exit 0 with a "Written to ..." line, so a 20-minute gap in the
    middle of an interview left no trace in the file, its header, or the exit
    code - a NEW failure mode this feature introduced (pre-#206 a single empty
    response was `sys.exit(1)`).

    Continuing is still right: the other windows are real, billed translation.
    But the gap must be visible on every surface a reader or a batch driver
    looks at.
    """

    def test_the_body_carries_a_marker_where_the_content_should_be(self, tmp_path, harness):
        _rec, out, error = harness(tmp_path, minutes=118, chunk_minutes=20, empty_windows={3})
        assert isinstance(error, SystemExit), f"expected a non-zero exit, got {error!r}"
        written = list(out.glob("*.translate-bcs.txt"))
        assert written, "the surviving windows must still be written"
        text = written[0].read_text(encoding="utf-8")
        assert "MISSING: window 3/6" in text, "the gap left no trace in the body"
        assert "[BCS window 2]" in text and "[BCS window 4]" in text, "the windows either side of the gap were lost"

    def test_the_exit_code_says_incomplete_not_success(self, tmp_path, harness):
        _rec, _out, error = harness(tmp_path, minutes=118, chunk_minutes=20, empty_windows={3})
        assert isinstance(error, SystemExit)
        assert error.code == tv.EXIT_INCOMPLETE, f"a file with a hole in it exited {error.code}; pre-fix this was 0"

    def test_the_header_records_it_and_a_later_good_window_cannot_erase_it(self, tmp_path, harness):
        """Window 3 is empty and windows 4-6 succeed with STOP. If the reason
        were simply overwritten by the last window, the header would claim a
        clean run over a file with a hole in it."""
        _rec, out, error = harness(tmp_path, minutes=118, chunk_minutes=20, empty_windows={3})
        assert isinstance(error, SystemExit)
        text = list(out.glob("*.translate-bcs.txt"))[0].read_text(encoding="utf-8")
        header = text.split("[BCS window", 1)[0]
        assert "Incomplete translation" in header, (
            f"the header claims a clean run over a file with a hole in it:{chr(10)}{header}"
        )
        assert "window 3/6" in header, "the header does not say WHICH window is missing"
        # The pre-existing truncation notice keys on an observed-vs-requested
        # END ratio and structurally cannot see a hole in the MIDDLE - this run
        # ends exactly where it should. That is why the gap gets its own notice
        # rather than being folded into that one.
        assert "--start" in header, "the header names no recovery"

    def test_a_clean_run_is_still_exit_zero_with_no_marker(self, tmp_path, harness):
        """The companion that keeps the three tests above from being satisfied
        by a writer that always reports incomplete."""
        _rec, out, error = harness(tmp_path, minutes=118, chunk_minutes=20)
        assert error is None, f"a healthy run must not exit non-zero: {error!r}"
        text = list(out.glob("*.translate-bcs.txt"))[0].read_text(encoding="utf-8")
        assert "MISSING: window" not in text
        assert tv.FINISH_REASON_INCOMPLETE_WINDOWS not in text


class TestAnEmptyRangeCostsNoGeminiCallAtAll:
    """The claim in the rules file is "costs no Gemini call". The first cut's
    test asserted only that the STREAM stub was unused - but `translate_title`
    is a separate Gemini call, was separately monkeypatched, and was never
    counted. Measured: an empty range fired the title call and THEN exited 1.
    """

    def test_neither_the_stream_nor_the_title_call_fires(self, monkeypatch, tmp_path):
        calls = {"stream": 0, "title": 0}

        def stream(*a, **k):
            calls["stream"] += 1
            return ("x", None, "STOP")

        def title(*a, **k):
            calls["title"] += 1
            return "BCS::x"

        monkeypatch.setattr(tv, "_stream_with_timeouts", stream)
        monkeypatch.setattr(tv, "translate_title", title)
        monkeypatch.setattr(tv, "create_client", lambda k: object())
        monkeypatch.setattr(tv, "require_gemini", lambda: (None, _Types))
        monkeypatch.setattr(tv, "build_permissive_safety_settings", lambda types: [])
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")

        src = tmp_path / "long.transcript.md"
        src.write_text(make_transcript(60), encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            tv._translate_from_transcript(
                transcript_path=src,
                model_name="gemini-2.5-pro",
                output_dir=tmp_path / "out",
                use_stdout=False,
                force=True,
                chunk_minutes=20,
                start_minutes=200,
                end_minutes=210,
            )
        assert exc.value.code == 1
        assert calls == {"stream": 0, "title": 0}, f"an empty range still paid: {calls}"


class TestMergeUsage:
    """Zero coverage in the first cut. The review mutated it to last-window-wins
    - the exact under-report-by-N the rules file forbids - and 244 tests stayed
    green, because the harness returns a token count nothing ever reads back.
    """

    def test_counts_from_every_window_are_summed(self):
        total = None
        for _ in range(3):
            total = tv._merge_usage(total, {"total_token_count": 100, "prompt_token_count": 10})
        assert total["total_token_count"] == 300, f"windows were not summed: {total}"
        assert total["prompt_token_count"] == 30

    def test_a_none_part_does_not_wipe_the_running_total(self):
        total = tv._merge_usage(None, {"total_token_count": 100})
        assert tv._merge_usage(total, None)["total_token_count"] == 100

    def test_a_none_total_adopts_the_first_part(self):
        assert tv._merge_usage(None, {"total_token_count": 7})["total_token_count"] == 7

    def test_a_key_only_some_windows_report_still_accumulates(self):
        total = tv._merge_usage({"total_token_count": 5}, {"total_token_count": 5, "thoughts_token_count": 2})
        assert total["thoughts_token_count"] == 2
        assert total["total_token_count"] == 10
