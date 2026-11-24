"""`repair-timestamps`: re-stamp a wall-of-text transcript from caption cue timings.

The real `cmd_repair_timestamps` runs over a synthetic corpus under `tmp_path`
with only `fetch_english_captions` stubbed. Transcripts are built through the
real writers (`merge_transcript_json`, `_write_transcript_md`, the chunked
coverage block, the captions body), so the parser is tested against what the
writers emit, not against hand-typed text. Every caption track is synthetic.
"""

import collections
import json
import logging
import shlex
import sys
import types as _types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import caption_align
import intel_graph
import pytest
from youtube_captions import CAPTIONS_BLOCK_RECOVERY, CaptionsResult

import video_intel as vi

VIDEO_ID = "vid00000001"
URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
PREFIX = "2026-06-01-a-talk"
WORDS = [
    "agents", "pods", "slack", "factory", "inference", "android", "tickets", "reviews",
    "builds", "queues", "budgets", "latency", "testing", "staging", "rollouts", "metrics",
    "kernels", "schedulers", "caches", "tokens", "prompts", "screens", "laptops", "ownership",
    "hiring", "pricing", "security", "networks", "dashboards", "incidents", "sprints",
]  # fmt: skip


# ---------------------------------------------------------------------------
# Synthetic talk: transcript text, caption track, real writers
# ---------------------------------------------------------------------------


def _talk(n: int = 50, first: float = 12.0, step: float = 11.0):
    """(sentences, cues): two sentences per beat, two caption cues per beat at fractional starts.

    Captions are lowercase and unpunctuated, and drop the "Um," fillers the
    transcript keeps, the way auto-captions differ from a Gemini transcript.
    """
    sentences: list[str] = []
    cues: list[tuple[float, str]] = []
    for i in range(n):
        a, b, c = WORDS[i % len(WORDS)], WORDS[(i * 7 + 3) % len(WORDS)], WORDS[(i * 3 + 1) % len(WORDS)]
        filler = "Um, " if i % 5 == 0 else ""
        sentences.append(f"{filler}Point {i} is about {a} and {b}.")
        sentences.append(f"We keep {c} in mind.")
        t = first + i * step
        cues.append((t + 0.4, f"point {i} is about {a} and {b}"))
        cues.append((t + 5.9, f"we keep {c} in mind"))
    return sentences, cues


SCREENS = [
    {"start": "00:00", "end": "00:11", "type": "other", "description": "Opening titles.", "transcribed_text": "A TALK"},
    {
        "start": "01:40",
        "end": "02:00",
        "type": "slide",
        "description": "Slide with numbers.",
        "transcribed_text": "Line one\n344 lines\n98.5% done",
    },
    {"start": "05:00", "type": "slide", "description": "A single-stamp slide."},
    {"start": "09:00", "end": "09:50", "type": "other", "description": "Closing slide."},
]


def _raw_json(text: str, stamp: str = "00:12") -> dict:
    return {
        "transcripts": [{"start": stamp, "voice": 1, "text": text}],
        "screen_content": SCREENS,
        "speakers": [{"voice": 1, "name": "Host", "role": "Speaker", "evidence": "Named on a slide."}],
    }


def _video() -> dict:
    return {"video_id": VIDEO_ID, "url": URL, "title": "A Talk", "published": "2026-06-01"}


def _single_shot_markdown(path: Path, text: str, stamp: str = "00:12") -> None:
    """The monolithic transcript as the single-shot writer renders it (header says Complete, pre-fix)."""
    vi._write_transcript_md(
        path, _video(), vi.merge_transcript_json(_raw_json(text, stamp), {}), transcript_source="gemini"
    )


def _chunked_markdown(path: Path, text: str, *, failed_row: bool = True) -> None:
    body = vi.merge_transcript_json(_raw_json(text), {})
    rows = [
        {"range": "00:00 - 05:00", "status": "ok", "speakers": ["Host"]},
        {"range": "05:00 - 10:00", "status": "FAILED (timeout)" if failed_row else "ok", "speakers": []},
    ]
    coverage = vi._build_chunked_transcript_coverage_block(600, 5, rows)
    path.write_text(coverage + "\n" + body, encoding="utf-8")


def _meta(**extra) -> dict:
    base = {
        "video_url": URL,
        "video_id": VIDEO_ID,
        "channel": "chan",
        "title": "A Talk",
        "published": "2026-06-01",
        "processed": "2026-01-01T00:00:00+00:00",
        "transcript_status": "partial",
        "transcript_source": "gemini",
        "transcript_quality_flags": ["monolithic_severe"],
        "transcript_max_blind_gap_seconds": 588,
        "transcript_blind_gap_at_seconds": 12,
        "transcript_last_dialogue_fraction": 0.02,
        "transcript_dialogue_entries": 1,
        "transcript_timestamp_overrun_entries": 0,
        "transcript_model_served": "model-x",
        "duration_seconds": 600,
        "modes_completed": ["transcript", "scan"],
        "last_error": None,
        "alt_titles": ["An Older Title"],
        "skip_modes": ["concepts"],
        "skip_reason": "kept on purpose",
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """No test here may reach the network, and the id index never leaks between tests."""

    def _unstubbed(*_a, **_kw):
        raise AssertionError("fetch_english_captions was not stubbed")

    monkeypatch.setattr(vi, "fetch_english_captions", _unstubbed)
    vi._invalidate_video_id_cache()
    yield
    vi._invalidate_video_id_cache()


@pytest.fixture
def corpus(tmp_path) -> Path:
    root = tmp_path / "corpus"
    (root / "chan").mkdir(parents=True)
    return root


def _paths(corpus: Path) -> SimpleNamespace:
    chan = corpus / "chan"
    return SimpleNamespace(
        chan=chan,
        transcript=chan / f"{PREFIX}.transcript.md",
        meta=chan / f"{PREFIX}.meta.json",
        sidecar=chan / f"{PREFIX}.transcript.raw.pre-repair.txt",
    )


def _write_corpus(
    corpus: Path, *, shape: str = "single", meta: dict | None = None, text: str | None = None, failed_row: bool = True
):
    p = _paths(corpus)
    sentences, _ = _talk()
    text = text if text is not None else " ".join(sentences)
    if shape == "single":
        _single_shot_markdown(p.transcript, text)
    else:
        _chunked_markdown(p.transcript, text, failed_row=failed_row)
    p.meta.write_text(json.dumps(meta if meta is not None else _meta(), indent=2), encoding="utf-8")
    return p


def _stub_captions(monkeypatch, cues, calls=None):
    snippets = [(start, text) for start, text in cues]

    def fake(video_id, *, reason_sink=None):
        if calls is not None:
            calls.append(video_id)
        return CaptionsResult(snippets=snippets, is_generated=True, language="en", durations=tuple(4.0 for _ in cues))

    monkeypatch.setattr(vi, "fetch_english_captions", fake)


def _stub_no_captions(monkeypatch, kind, exception=None, calls=None):
    def fake(video_id, *, reason_sink=None):
        if calls is not None:
            calls.append(video_id)
        if reason_sink is not None:
            reason_sink.update({"kind": kind, "exception": exception, "message": ""})
        return None

    monkeypatch.setattr(vi, "fetch_english_captions", fake)


def _run(corpus: Path, *, apply: bool = False, url: str = URL, channel: str = "chan"):
    args = _types.SimpleNamespace(url=url, channel=channel, apply=apply)
    return vi.cmd_repair_timestamps(args, {"output_dir": str(corpus)})


def _exit_code(corpus: Path, **kw) -> int:
    with pytest.raises(SystemExit) as exc:
        _run(corpus, **kw)
    return exc.value.code


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _body(path: Path) -> vi.TranscriptBody:
    return vi.parse_transcript_body(path.read_bytes().decode("utf-8"))


def _screens(body: vi.TranscriptBody) -> list[str]:
    return ["".join(e.lines) for e in body.entries if e.kind == vi.TRANSCRIPT_ENTRY_SCREEN]


def _dialogue(body: vi.TranscriptBody) -> list[tuple[vi.TranscriptBodyEntry, vi.DialogueEntryParts]]:
    return [(e, vi.split_dialogue_entry(e)) for e in body.entries if e.kind == vi.TRANSCRIPT_ENTRY_DIALOGUE]


# ---------------------------------------------------------------------------
# Body parser / renderer
# ---------------------------------------------------------------------------


def _fixture_texts(tmp_path: Path) -> dict[str, str]:
    sentences, cues = _talk(8)
    single = tmp_path / "single.md"
    _single_shot_markdown(single, " ".join(sentences))
    chunked = tmp_path / "chunked.md"
    _chunked_markdown(chunked, " ".join(sentences))
    captions = tmp_path / "captions.md"
    body = vi._build_captions_transcript_body(CaptionsResult(cues, True, "en"))
    vi._write_transcript_md(captions, _video(), body, status="Captions (manual)", transcript_source="youtube_captions")
    healthy = vi.merge_transcript_json(
        {
            "transcripts": [{"start": f"{m:02d}:00", "voice": 1, "text": f"line {m}"} for m in range(10)],
            "screen_content": SCREENS,
            "speakers": [{"voice": 1, "name": "Host"}],
        },
        {},
    )
    texts = {
        "single_shot": single.read_bytes().decode("utf-8"),
        "chunked": chunked.read_bytes().decode("utf-8"),
        "captions": captions.read_bytes().decode("utf-8"),
        "lf_only": single.read_bytes().decode("utf-8").replace("\r\n", "\n"),
        "crlf": single.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\n", "\r\n"),
        "no_trailing_newline": "# Transcript: T\n\n---\n\n" + healthy.rstrip("\n"),
        "multiline_entry": '# Transcript: T\n\n---\n\n[00:05] Host: "first line\nsecond line."\n\n[00:40] Host: "x"\n',
    }
    return texts


class TestBodyRoundTrip:
    @pytest.mark.parametrize(
        "name",
        ["single_shot", "chunked", "captions", "lf_only", "crlf", "no_trailing_newline", "multiline_entry"],
    )
    def test_parse_then_render_without_a_split_is_byte_identical(self, tmp_path, name):
        text = _fixture_texts(tmp_path)[name]
        assert vi.parse_transcript_body(text).render() == text

    @pytest.mark.parametrize("name", ["single_shot", "chunked", "captions", "crlf", "multiline_entry"])
    def test_parser_dialogue_stamps_agree_with_the_assessor_reader(self, tmp_path, name):
        text = _fixture_texts(tmp_path)[name]
        parsed = [e.stamp for e in vi.parse_transcript_body(text).entries if e.kind == vi.TRANSCRIPT_ENTRY_DIALOGUE]
        assert parsed == [e["start"] for e in vi.dialogue_entries_from_transcript_markdown(text)]
        assert parsed  # the comparison is not vacuous

    def test_parser_splits_header_entries_and_evidence_trailer(self, tmp_path):
        body = vi.parse_transcript_body(_fixture_texts(tmp_path)["single_shot"])
        assert body.header.startswith("# Transcript: A Talk")
        assert "**Status:** Complete" in body.header
        assert [e.kind for e in body.entries].count(vi.TRANSCRIPT_ENTRY_SCREEN) == len(SCREENS)
        assert body.trailer.lstrip().startswith("---")
        assert "Speaker Identification Evidence" in body.trailer

    def test_split_dialogue_entry_reads_label_and_multiline_text(self, tmp_path):
        body = vi.parse_transcript_body(_fixture_texts(tmp_path)["multiline_entry"])
        parts = vi.split_dialogue_entry(body.entries[0])
        assert parts.label == "Host"
        assert parts.text == "first line\nsecond line."

    def test_split_dialogue_entry_reads_a_captions_line_without_label(self, tmp_path):
        body = vi.parse_transcript_body(_fixture_texts(tmp_path)["captions"])
        parts = vi.split_dialogue_entry(body.entries[0])
        assert parts.label is None
        assert parts.text.startswith("point 0")


# ---------------------------------------------------------------------------
# The two invariants, on several shapes, through the real planner
# ---------------------------------------------------------------------------


def _shape(name: str, tmp_path: Path):
    sentences, cues = _talk()
    text = " ".join(sentences)
    if name == "single_shot":
        path = tmp_path / "s.md"
        _single_shot_markdown(path, text)
        return path.read_bytes().decode("utf-8"), cues
    if name == "chunked":
        path = tmp_path / "c.md"
        _chunked_markdown(path, text)
        return path.read_bytes().decode("utf-8"), cues
    if name == "hh_mm_ss_stamps":
        path = tmp_path / "h.md"
        _single_shot_markdown(path, text, stamp="00:00:12")
        return path.read_bytes().decode("utf-8"), cues
    if name == "offset_track":
        # Gemini's stamp is 40 s late relative to the speech: early boundaries go backwards.
        path = tmp_path / "o.md"
        _single_shot_markdown(path, text, stamp="00:52")
        return path.read_bytes().decode("utf-8"), cues
    if name == "two_walls_and_a_healthy_entry":
        first = " ".join(sentences[:40])
        second = " ".join(sentences[40:])
        md = (
            "# Transcript: T\n\n---\n\n"
            f'[00:12] Host: "{first}"\n\n'
            "  SCREEN [02:00] [slide]: middle slide\n\n"
            f'[03:52] Guest (Panel): "{second}"\n\n'
            '[09:40] Host: "Thanks."\n'
        )
        return md, cues
    raise AssertionError(name)


SHAPES = ["single_shot", "chunked", "hh_mm_ss_stamps", "offset_track", "two_walls_and_a_healthy_entry"]


class TestInvariants:
    @pytest.mark.parametrize("name", SHAPES)
    def test_every_stamp_is_an_original_stamp_or_a_cue_start(self, tmp_path, name):
        text, raw_cues = _shape(name, tmp_path)
        cues = [caption_align.Cue(s, 4.0, t) for s, t in raw_cues]

        repair = vi.plan_transcript_repair(text, cues)

        assert repair.repaired, "the shape must actually split for this test to mean anything"
        original = {vi.timestamp_to_seconds(e.stamp) for e in vi.parse_transcript_body(text).entries}
        cue_starts = {int(c.start) for c in cues}
        for entry in vi.parse_transcript_body(repair.text).entries:
            assert vi.timestamp_to_seconds(entry.stamp) in original | cue_starts, entry.stamp

    @pytest.mark.parametrize("name", SHAPES)
    def test_text_is_preserved_and_the_label_repeats_on_every_piece(self, tmp_path, name):
        text, raw_cues = _shape(name, tmp_path)
        cues = [caption_align.Cue(s, 4.0, t) for s, t in raw_cues]

        repair = vi.plan_transcript_repair(text, cues)

        before = vi.parse_transcript_body(text)
        after = vi.parse_transcript_body(repair.text)
        for candidate in repair.repaired:
            original = next(p for e, p in _dialogue(before) if e.stamp == candidate.stamp)
            rejoined = " ".join(p.text for p in candidate.plan.pieces)
            assert " ".join(rejoined.split()) == " ".join(original.text.split())
            for stamp, piece in zip(candidate.piece_stamps, candidate.plan.pieces, strict=True):
                rendered = next(p for e, p in _dialogue(after) if e.stamp == stamp and p.text == piece.text)
                assert rendered.label == original.label
        words = collections.Counter(w for _, p in _dialogue(before) for w in caption_align.normalize_words(p.text))
        assert words == collections.Counter(
            w for _, p in _dialogue(after) for w in caption_align.normalize_words(p.text)
        )

    @pytest.mark.parametrize("name", SHAPES)
    def test_screen_blocks_are_byte_identical_and_interleave_by_stamp(self, tmp_path, name):
        text, raw_cues = _shape(name, tmp_path)
        cues = [caption_align.Cue(s, 4.0, t) for s, t in raw_cues]

        repaired = vi.plan_transcript_repair(text, cues).text

        assert _screens(vi.parse_transcript_body(repaired)) == _screens(vi.parse_transcript_body(text))
        seconds = [vi.timestamp_to_seconds(e.stamp) for e in vi.parse_transcript_body(repaired).entries]
        assert seconds == sorted(seconds)
        body = vi.parse_transcript_body(repaired)
        assert body.header == vi.parse_transcript_body(text).header
        assert body.trailer == vi.parse_transcript_body(text).trailer


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_prints_the_plan_and_leaves_the_tree_byte_identical(self, corpus, monkeypatch, caplog):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            result = _run(corpus)

        assert result["outcome"] == "dry_run"
        assert _snapshot(corpus) == before
        log = caplog.text
        assert "[00:12] coverage" in log
        assert "re-assessment would say" in log
        assert "caption cues" in log
        assert "Dry run" in log


class TestApply:
    def test_apply_splits_the_wall_and_the_assessor_clears_the_flag(self, corpus, monkeypatch, caplog):
        p = _write_corpus(corpus)
        original_transcript = p.transcript.read_bytes()
        original_meta = json.loads(p.meta.read_text(encoding="utf-8"))
        _stub_captions(monkeypatch, _talk()[1])

        with caplog.at_level(logging.INFO, logger="video_intel"):
            result = _run(corpus, apply=True)

        assert result["outcome"] == "repaired"
        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        written = p.transcript.read_bytes().decode("utf-8")
        # Many timed entries, and the flag cleared because the ASSESSOR says so:
        # the persisted fields equal an independent assessment of the file on disk.
        fields, _flags = vi.assess_transcript_markdown(written, 600, captions=False)
        for key, value in fields.items():
            assert meta[key] == value, key
        assert meta["transcript_dialogue_entries"] >= 15
        assert "monolithic_severe" not in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "complete"
        assert "**Status:** Complete" in vi.parse_transcript_body(written).header
        # The sidecar holds the original bytes; provenance is recorded.
        assert p.sidecar.read_bytes() == original_transcript
        assert meta["transcript_timestamp_repair"] == "caption_cues"
        assert 0.6 <= meta["transcript_timestamp_repair_coverage"] <= 1.0
        assert meta["transcript_timestamp_repair_coverage"] == round(meta["transcript_timestamp_repair_coverage"], 3)
        # No unrelated key touched.
        untouched = set(original_meta) - set(fields) - {"transcript_status"}
        for key in untouched:
            assert meta[key] == original_meta[key], key
        assert set(meta) - set(original_meta) == {"transcript_timestamp_repair", "transcript_timestamp_repair_coverage"}
        assert "index --incremental --channel chan" in caplog.text
        assert "intel_graph.py load --force" in caplog.text

    def test_apply_keeps_a_severe_flag_the_assessor_still_finds(self, corpus, monkeypatch):
        # The talk starts at 11:40 of a 1300 s video: after the split a 700 s
        # LEADING blind gap is still there, so the status must stay partial.
        sentences, cues = _talk(n=50, first=700.0)
        p = _paths(corpus)
        _single_shot_markdown(p.transcript, " ".join(sentences), stamp="11:40")
        p.meta.write_text(json.dumps(_meta(duration_seconds=1300)), encoding="utf-8")
        _stub_captions(monkeypatch, cues)

        _run(corpus, apply=True)

        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        assert "monolithic_severe" not in meta["transcript_quality_flags"]
        assert "blind_gap_severe" in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "partial"
        assert (
            "**Status:** Partial transcript"
            in vi.parse_transcript_body(p.transcript.read_text(encoding="utf-8")).header
        )

    def test_failed_chunk_keeps_partial_and_its_evidence_although_the_flag_clears(self, corpus, monkeypatch):
        p = _write_corpus(
            corpus,
            shape="chunked",
            meta=_meta(transcript_chunks=2, transcript_chunk_minutes=5, transcript_failed_chunks=1),
        )
        header_before = _body(p.transcript).header
        _stub_captions(monkeypatch, _talk()[1])

        _run(corpus, apply=True)

        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        assert "monolithic_severe" not in meta["transcript_quality_flags"]
        assert meta["transcript_failed_chunks"] == 1
        assert meta["transcript_status"] == "partial"
        # A chunked header has no status line and stays byte-identical.
        assert _body(p.transcript).header == header_before

    @pytest.mark.parametrize(
        "evidence",
        [
            {"transcript_thin_chunks": 1},
            {"transcript_recovery": "salvaged_sections"},
            {"transcript_parse_error": "boom"},
        ],
    )
    def test_other_failure_evidence_blocks_promotion(self, corpus, monkeypatch, evidence):
        p = _write_corpus(corpus, meta=_meta(**evidence))
        _stub_captions(monkeypatch, _talk()[1])

        _run(corpus, apply=True)

        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        assert meta["transcript_status"] == "partial"
        for key, value in evidence.items():
            assert meta[key] == value

    def test_chunked_transcript_promotes_to_ok(self, corpus, monkeypatch):
        p = _write_corpus(
            corpus, shape="chunked", meta=_meta(transcript_chunks=2, transcript_failed_chunks=0), failed_row=False
        )
        _stub_captions(monkeypatch, _talk()[1])

        _run(corpus, apply=True)

        assert json.loads(p.meta.read_text(encoding="utf-8"))["transcript_status"] == "ok"

    def test_writer_only_flags_are_preserved_and_block_promotion(self, corpus, monkeypatch):
        flags = ["backward_jump_severe", "chunk_window_mismatch_mild", "monolithic_severe"]
        p = _write_corpus(corpus, meta=_meta(transcript_quality_flags=flags))
        _stub_captions(monkeypatch, _talk()[1])

        _run(corpus, apply=True)

        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        assert "backward_jump_severe" in meta["transcript_quality_flags"]
        assert "chunk_window_mismatch_mild" in meta["transcript_quality_flags"]
        assert "monolithic_severe" not in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "partial"

    def test_writer_only_flag_set_is_exactly_the_emission_order_and_chunk_merge_flags(self):
        expected = {
            "backward_jump_severe",
            "backward_jump_mild",
            "chunk_window_mismatch_severe",
            "chunk_window_mismatch_mild",
        }
        assert expected == vi.WRITER_ONLY_QUALITY_FLAGS

    def test_apply_keeps_screen_blocks_byte_identical_and_interleaved(self, corpus, monkeypatch):
        p = _write_corpus(corpus)
        before = _screens(_body(p.transcript))
        _stub_captions(monkeypatch, _talk()[1])

        _run(corpus, apply=True)

        body = _body(p.transcript)
        assert _screens(body) == before
        kinds = [e.kind for e in body.entries]
        first_screen_after_dialogue = kinds.index(vi.TRANSCRIPT_ENTRY_SCREEN, kinds.index(vi.TRANSCRIPT_ENTRY_DIALOGUE))
        assert vi.TRANSCRIPT_ENTRY_DIALOGUE in kinds[first_screen_after_dialogue:]  # dialogue on both sides

    def test_promotion_removes_the_quality_guard_warning_block(self, corpus, monkeypatch):
        p = _paths(corpus)
        sentences, cues = _talk()
        fused = vi.merge_transcript_json(_raw_json(" ".join(sentences)), {})
        vi._write_transcript_md(
            p.transcript,
            _video(),
            fused,
            status=vi.transcript_header_status("partial"),
            warning=vi.quality_guard_warning(["monolithic_severe"]),
            transcript_source="gemini",
        )
        p.meta.write_text(json.dumps(_meta()), encoding="utf-8")
        _stub_captions(monkeypatch, cues)

        _run(corpus, apply=True)

        header = _body(p.transcript).header
        assert vi.TRANSCRIPT_WARNING_HEADING not in header
        assert "**Status:** Complete" in header

    def test_salvage_warning_is_never_removed(self):
        text = (
            "# Transcript: T\n\n**Status:** Partial transcript\n\n---\n\n"
            f"{vi.TRANSCRIPT_WARNING_HEADING}\n\nGemini returned malformed JSON.\n\n---\n\n"
            '[00:05] Host: "x"\n'
        )
        assert vi.rewrite_transcript_header(text, "complete", []).count(vi.TRANSCRIPT_WARNING_HEADING) == 1

    def test_existing_sidecar_is_never_clobbered(self, corpus, monkeypatch):
        p = _write_corpus(corpus)
        original = p.transcript.read_bytes()
        p.sidecar.write_bytes(b"an earlier original")
        _stub_captions(monkeypatch, _talk()[1])

        result = _run(corpus, apply=True)

        assert p.sidecar.read_bytes() == b"an earlier original"
        assert result["sidecar"] != p.sidecar
        assert result["sidecar"].name.startswith(f"{PREFIX}.transcript.raw.pre-repair.")
        assert result["sidecar"].read_bytes() == original

    def test_a_failure_after_the_write_restores_the_tree(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(vi, "_write_meta_atomic", boom)

        assert _exit_code(corpus, apply=True) == 1
        assert _snapshot(corpus) == before

    def test_second_apply_finds_nothing_to_repair_and_writes_nothing(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        _run(corpus, apply=True)
        vi._invalidate_video_id_cache()
        after_first = _snapshot(corpus)

        result = _run(corpus, apply=True)

        assert result["outcome"] == "nothing"
        assert _snapshot(corpus) == after_first

    def test_follow_up_commands_parse_with_the_real_parsers(self, monkeypatch, tmp_path):
        index_cmd, graph_cmd = vi.repair_follow_up_commands("chan")
        seen = {}
        monkeypatch.setattr(vi, "load_config", lambda: {"output_dir": str(tmp_path)})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_kw: None)
        monkeypatch.setattr(vi, "cmd_index", lambda args, config: seen.update(vars(args)))
        monkeypatch.setattr(sys, "argv", ["video_intel.py", *shlex.split(index_cmd)[2:]])

        vi.main()

        assert seen["command"] == "index" and seen["incremental"] is True and seen["channel"] == "chan"
        graph_args = intel_graph.build_parser().parse_args(shlex.split(graph_cmd)[2:])
        assert graph_args.command == "load" and graph_args.force is True


class TestDeclines:
    def test_low_coverage_track_declines_with_exit_3_and_untouched_tree(self, corpus, monkeypatch, caplog):
        _write_corpus(corpus)
        # A track that shares only every third beat with the transcript.
        _, cues = _talk()
        sparse = [(s, t if i % 6 == 0 else f"unrelated words number {i} here") for i, (s, t) in enumerate(cues)]
        _stub_captions(monkeypatch, sparse)
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            code = _exit_code(corpus, apply=True)

        assert code == vi.EXIT_PARTIAL == 3
        assert _snapshot(corpus) == before
        assert "coverage" in caplog.text

    def test_captions_absent_declines_with_exit_3(self, corpus, monkeypatch, caplog):
        _write_corpus(corpus)
        _stub_no_captions(monkeypatch, "absent", "NoTranscriptFound")
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 3

        assert _snapshot(corpus) == before
        assert "no English caption track" in caplog.text
        assert "refused" not in caplog.text

    def test_captions_refused_declines_with_exit_3_and_the_recovery_wording(self, corpus, monkeypatch, caplog):
        _write_corpus(corpus)
        _stub_no_captions(monkeypatch, "blocked", "IpBlocked")
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 3

        assert _snapshot(corpus) == before
        assert "refused (IpBlocked); nothing changed" in caplog.text
        assert CAPTIONS_BLOCK_RECOVERY not in caplog.text

    def test_unsplittable_candidate_declines_with_exit_3(self, corpus, monkeypatch):
        sentences, cues = _talk()
        unpunctuated = " ".join(s.rstrip(".") for s in sentences)
        _write_corpus(corpus, text=unpunctuated)
        _stub_captions(monkeypatch, cues)
        before = _snapshot(corpus)

        assert _exit_code(corpus, apply=True) == 3
        assert _snapshot(corpus) == before

    def test_track_that_does_not_match_at_all_declines_with_exit_3(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, [(float(i * 10), f"zzz qqq {i}") for i in range(30)])

        assert _exit_code(corpus, apply=True) == 3

    def test_healthy_transcript_has_nothing_to_repair_exit_0(self, corpus, monkeypatch):
        _, cues = _talk()
        p = _paths(corpus)
        raw = {
            "transcripts": [
                {"start": vi._repair_stamp(int(s), "00:00"), "voice": 1, "text": t} for (s, t) in cues[::2]
            ],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        vi._write_transcript_md(p.transcript, _video(), vi.merge_transcript_json(raw, {}), transcript_source="gemini")
        p.meta.write_text(
            json.dumps(_meta(transcript_status="complete", transcript_quality_flags=[])), encoding="utf-8"
        )
        _stub_captions(monkeypatch, cues)
        before = _snapshot(corpus)

        result = _run(corpus, apply=True)

        assert result["outcome"] == "nothing"
        assert _snapshot(corpus) == before


class TestCannotRun:
    def test_url_without_a_youtube_id_exits_1(self, corpus):
        _write_corpus(corpus)
        assert _exit_code(corpus, url="/videos/local-talk.mp4") == 1

    def test_no_transcript_for_the_id_exits_1(self, corpus):
        _write_corpus(corpus)
        assert _exit_code(corpus, url="https://www.youtube.com/watch?v=zzzzzzzzzzz") == 1

    def test_captions_transcript_exits_1_before_any_fetch(self, corpus, monkeypatch):
        _write_corpus(corpus, meta=_meta(transcript_source="youtube_captions"))
        calls: list[str] = []
        _stub_captions(monkeypatch, _talk()[1], calls=calls)
        assert _exit_code(corpus) == 1
        assert calls == []

    def test_unknown_duration_exits_1_before_any_fetch(self, corpus, monkeypatch):
        meta = _meta()
        del meta["duration_seconds"]
        _write_corpus(corpus, meta=meta)
        calls: list[str] = []
        _stub_captions(monkeypatch, _talk()[1], calls=calls)
        before = _snapshot(corpus)
        assert _exit_code(corpus, apply=True) == 1
        assert calls == []
        assert _snapshot(corpus) == before

    def test_unknown_duration_names_backfill_commands_that_parse(self, corpus, monkeypatch, caplog, tmp_path):
        meta = _meta()
        del meta["duration_seconds"]
        _write_corpus(corpus, meta=meta)
        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 1
        commands = [r.getMessage().strip() for r in caplog.records if "scripts/video_intel.py" in r.getMessage()]
        assert len(commands) == 2
        monkeypatch.setattr(vi, "load_config", lambda: {"output_dir": str(tmp_path)})
        monkeypatch.setattr(vi, "backup_config_if_changed", lambda *_a, **_kw: None)
        parsed = []
        monkeypatch.setattr(vi, "cmd_backfill_durations", lambda args, config: parsed.append(vars(args)))
        for command in commands:
            monkeypatch.setattr(sys, "argv", ["video_intel.py", *shlex.split(command)[2:]])
            vi.main()
        assert [(a["command"], a["channel"], a["apply"]) for a in parsed] == [
            ("backfill-durations", "chan", False),
            ("backfill-durations", "chan", True),
        ]

    def test_unreadable_meta_exits_1(self, corpus, monkeypatch):
        p = _write_corpus(corpus)
        p.meta.write_bytes(b'{"video_id": "' + VIDEO_ID.encode() + b'", "title": "\xe2\x82"}')
        _stub_captions(monkeypatch, _talk()[1])
        assert _exit_code(corpus, apply=True) == 1


# ---------------------------------------------------------------------------
# The single-shot writer's header status (the issue's related bug)
# ---------------------------------------------------------------------------


def _stub_gemini(monkeypatch, payload: dict):
    def fake_call_gemini(_client, _types, _uri, _prompt, _model, **kw):
        on_response = kw.get("on_response")
        if on_response is not None:
            on_response(
                SimpleNamespace(
                    usage_metadata=SimpleNamespace(
                        prompt_token_count=5000,
                        cached_content_token_count=0,
                        thoughts_token_count=0,
                        candidates_token_count=100,
                        total_token_count=5100,
                    ),
                    candidates=[SimpleNamespace(finish_reason="STOP")],
                )
            )
        return json.dumps(payload)

    monkeypatch.setattr(vi, "call_gemini", fake_call_gemini)
    return MagicMock(), MagicMock()


class TestSingleShotHeaderMatchesMeta:
    def test_monolithic_payload_header_says_partial_with_warning_block(self, tmp_path, monkeypatch):
        sentences, _ = _talk()
        client, types = _stub_gemini(monkeypatch, _raw_json(" ".join(sentences)))
        channel_dir = tmp_path / "chan"

        prefix, status = vi.process_transcript(
            client, types, _video(), "prompt", "gemini-test", channel_dir, PREFIX, duration_seconds=600
        )

        meta = json.loads((channel_dir / f"{prefix}.meta.json").read_text(encoding="utf-8"))
        header = vi.parse_transcript_body((channel_dir / f"{prefix}.transcript.md").read_text(encoding="utf-8")).header
        assert status == "partial (quality guard)"
        assert meta["transcript_status"] == "partial"
        assert f"**Status:** {vi.transcript_header_status(meta['transcript_status'])}" in header
        assert "**Status:** Partial transcript" in header
        assert vi.TRANSCRIPT_WARNING_HEADING in header
        assert "monolithic_severe" in header

    def test_healthy_payload_header_still_says_complete(self, tmp_path, monkeypatch):
        payload = {
            "transcripts": [{"start": f"{m:02d}:00", "voice": 1, "text": f"line {m}"} for m in range(10)],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
        client, types = _stub_gemini(monkeypatch, payload)
        channel_dir = tmp_path / "chan"

        prefix, status = vi.process_transcript(
            client, types, _video(), "prompt", "gemini-test", channel_dir, PREFIX, duration_seconds=600
        )

        header = vi.parse_transcript_body((channel_dir / f"{prefix}.transcript.md").read_text(encoding="utf-8")).header
        assert status == "done"
        assert "**Status:** Complete" in header
        assert vi.TRANSCRIPT_WARNING_HEADING not in header


# ---------------------------------------------------------------------------
# Review round: dialogue order, status in both directions, failure handling
# ---------------------------------------------------------------------------


def _host_guest_shape() -> tuple[str, list]:
    """A Host wall at [00:12], then a Guest line Gemini stamped [01:50] although it is spoken later."""
    sentences, cues = _talk()
    host = " ".join(sentences[:50])  # beats 0-24: caption time 12 s to about 280 s
    guest = " ".join(sentences[50:60])  # beats 25-29: spoken at about 287-330 s
    md = (
        "# Transcript: T\n\n---\n\n"
        f'[00:12] Host: "{host}"\n\n'
        "  SCREEN [00:40] [slide]: first slide\n\n"
        "  SCREEN [01:20] [slide]: second slide\n\n"
        f'[01:50] Guest: "{guest}"\n\n'
        "  SCREEN [05:00] [slide]: later slide\n"
    )
    return md, cues


def _non_monotone_pair_shape() -> tuple[str, list]:
    sentences, cues = _talk()
    wall = " ".join(sentences[:40])  # caption time 12 s to about 230 s
    md = (
        "# Transcript: T\n\n---\n\n"
        f'[00:12] Host: "{wall}"\n\n'
        '[05:00] Host: "A hand-edited late line."\n\n'
        '[04:30] Host: "A hand-edited early line."\n'
    )
    return md, cues


def _cue_records(raw_cues) -> list:
    return [caption_align.Cue(s, 4.0, t) for s, t in raw_cues]


ORDER_SHAPES = [*SHAPES, "host_guest", "non_monotone_pair"]


def _order_shape(name: str, tmp_path: Path):
    if name == "host_guest":
        return _host_guest_shape()
    if name == "non_monotone_pair":
        return _non_monotone_pair_shape()
    return _shape(name, tmp_path)


class TestDialogueOrder:
    def test_a_later_speaker_line_keeps_its_place_and_no_host_piece_follows_it(self):
        text, raw = _host_guest_shape()

        repair = vi.plan_transcript_repair(text, _cue_records(raw))

        assert repair.repaired
        dialogue = [(e, p) for e, p in _dialogue(vi.parse_transcript_body(repair.text))]
        labels = [p.label for _, p in dialogue]
        assert labels[-1] == "Guest" and set(labels[:-1]) == {"Host"}
        guest_entry = dialogue[-1][0]
        original_guest = next(e for e, p in _dialogue(vi.parse_transcript_body(text)) if p.label == "Guest")
        assert guest_entry.lines == original_guest.lines
        host_seconds = [vi.timestamp_to_seconds(e.stamp) for e, p in dialogue[:-1]]
        assert all(s < vi.timestamp_to_seconds("01:50") for s in host_seconds), host_seconds

    def test_screens_between_the_wall_and_the_next_line_interleave_and_later_screens_stay(self):
        text, raw = _host_guest_shape()

        body = vi.parse_transcript_body(vi.plan_transcript_repair(text, _cue_records(raw)).text)

        kinds = [(e.kind, e.stamp) for e in body.entries]
        first_screen = kinds.index((vi.TRANSCRIPT_ENTRY_SCREEN, "00:40"))
        assert kinds[first_screen - 1][0] == vi.TRANSCRIPT_ENTRY_DIALOGUE
        assert kinds[first_screen + 1][0] == vi.TRANSCRIPT_ENTRY_DIALOGUE  # a piece on both sides
        assert kinds[-1] == (vi.TRANSCRIPT_ENTRY_SCREEN, "05:00")  # after the Guest line, untouched
        assert kinds[-2][1] == "01:50"

    def test_a_hand_edited_non_monotone_pair_is_byte_identical_after_a_split_elsewhere(self):
        text, raw = _non_monotone_pair_shape()

        repaired = vi.plan_transcript_repair(text, _cue_records(raw)).text

        pair = '[05:00] Host: "A hand-edited late line."\n\n[04:30] Host: "A hand-edited early line."\n'
        assert text.endswith(pair) and repaired.endswith(pair)
        assert repaired.count("[00:") + repaired.count("[01:") + repaired.count("[02:") + repaired.count("[03:") > 1

    @pytest.mark.parametrize("name", ORDER_SHAPES)
    def test_dialogue_file_order_survives_a_split(self, tmp_path, name):
        text, raw = _order_shape(name, tmp_path)

        repair = vi.plan_transcript_repair(text, _cue_records(raw))

        assert repair.repaired
        split = {c.stamp: c for c in repair.repaired}
        expected: list[str] = []
        for entry, _ in _dialogue(vi.parse_transcript_body(text)):
            candidate = split.get(entry.stamp)
            if candidate is None:
                expected.append("".join(entry.lines))
            else:
                expected.extend(
                    f"{stamp}|{piece.text}"
                    for stamp, piece in zip(candidate.piece_stamps, candidate.plan.pieces, strict=True)
                )
        got = []
        for entry, parts in _dialogue(vi.parse_transcript_body(repair.text)):
            key = "".join(entry.lines)
            got.append(key if key in expected else f"{entry.stamp}|{parts.text}")
        assert got == expected


class TestStatusBothDirections:
    def test_a_severe_verdict_demotes_a_healthy_status_with_header_and_warning(self, corpus, monkeypatch):
        # A `complete` meta written while the duration was unknown: the talk starts
        # at 11:40 of a 1300 s video, so the re-assessment finds a leading gap.
        sentences, cues = _talk(n=50, first=700.0)
        p = _paths(corpus)
        _single_shot_markdown(p.transcript, " ".join(sentences), stamp="11:40")
        p.meta.write_text(
            json.dumps(_meta(duration_seconds=1300, transcript_status="complete", transcript_quality_flags=[])),
            encoding="utf-8",
        )
        _stub_captions(monkeypatch, cues)

        result = _run(corpus, apply=True)

        assert result["outcome"] == "repaired"
        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        assert "blind_gap_severe" in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "partial"
        header = _body(p.transcript).header
        assert "**Status:** Partial transcript" in header
        assert vi.TRANSCRIPT_WARNING_HEADING in header and "blind_gap_severe" in header

    @pytest.mark.parametrize(
        ("status", "severe", "expected"),
        [
            (None, True, "partial"),
            (None, False, "complete"),
            ("complete", True, "partial"),
            ("ok", True, "partial"),
            ("complete", False, None),
            ("partial", True, None),
            ("truncated_output", True, None),
        ],
    )
    def test_reassessed_status_rule_table(self, status, severe, expected):
        assert vi.reassessed_transcript_status(status, severe) == expected

    def test_assess_transcripts_and_repair_call_the_one_status_rule(self):
        import ast
        import inspect

        for fn in (vi.cmd_assess_transcripts, vi.repaired_transcript_status):
            tree = ast.parse(inspect.getsource(fn))
            calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "reassessed_transcript_status" in calls, fn.__name__

    def test_confabulated_chunks_block_promotion(self, corpus, monkeypatch):
        p = _write_corpus(corpus, meta=_meta(transcript_confabulated_chunks=1))
        _stub_captions(monkeypatch, _talk()[1])

        _run(corpus, apply=True)

        assert json.loads(p.meta.read_text(encoding="utf-8"))["transcript_status"] == "partial"

    def test_a_failed_row_in_the_coverage_table_blocks_promotion(self, corpus, monkeypatch):
        # A meta written before the failed-chunk count existed: only the table says so.
        p = _write_corpus(corpus, shape="chunked", meta=_meta(transcript_chunks=2))
        assert "FAILED" in _body(p.transcript).header
        _stub_captions(monkeypatch, _talk()[1])

        _run(corpus, apply=True)

        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        assert "monolithic_severe" not in meta["transcript_quality_flags"]
        assert meta["transcript_status"] == "partial"


class TestFailureHandling:
    def test_a_copy_that_fails_part_way_leaves_no_sidecar_and_an_untouched_tree(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)
        real_open = Path.open

        class HalfWriter:
            def __init__(self, handle):
                self.handle = handle

            def write(self, data):
                self.handle.write(data[: len(data) // 2])
                raise OSError("disk full")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

        def fake_open(self, mode="r", *a, **kw):
            handle = real_open(self, mode, *a, **kw)
            return HalfWriter(handle) if mode == "xb" else handle

        monkeypatch.setattr(Path, "open", fake_open)

        assert _exit_code(corpus, apply=True) == 1
        assert _snapshot(corpus) == before

    def test_a_copy_that_reads_back_different_leaves_no_sidecar(self, corpus, monkeypatch):
        p = _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)
        real_read = Path.read_bytes
        monkeypatch.setattr(Path, "read_bytes", lambda self: b"not it" if self == p.sidecar else real_read(self))

        assert _exit_code(corpus, apply=True) == 1
        assert _snapshot(corpus) == before

    def test_a_backup_that_cannot_be_made_never_becomes_an_overwrite(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)

        def no_backup(*_a, **_kw):
            raise OSError("read-only folder")

        monkeypatch.setattr(vi, "_keep_pre_repair_copy", no_backup)

        assert _exit_code(corpus, apply=True) == 1
        assert _snapshot(corpus) == before

    def test_an_interrupt_between_the_writes_restores_the_tree_and_re_raises(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)

        def interrupt(*_a, **_kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(vi, "_write_meta_atomic", interrupt)

        with pytest.raises(KeyboardInterrupt):
            _run(corpus, apply=True)
        assert _snapshot(corpus) == before

    def test_a_first_write_that_never_landed_says_so(self, corpus, monkeypatch, caplog):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(vi, "_write_bytes_atomic", boom)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 1
        assert _snapshot(corpus) == before
        assert "restored the original transcript" not in caplog.text  # it never changed
        assert "could not" not in caplog.text

    def test_a_sidecar_that_cannot_be_removed_is_named_and_not_called_a_failed_restore(
        self, corpus, monkeypatch, caplog
    ):
        p = _write_corpus(corpus)
        original = p.transcript.read_bytes()
        _stub_captions(monkeypatch, _talk()[1])
        real_unlink = Path.unlink

        def stubborn(self, *a, **kw):
            if self.name.startswith(f"{PREFIX}.transcript.raw.pre-repair"):
                raise PermissionError("locked")
            return real_unlink(self, *a, **kw)

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "unlink", stubborn)
        monkeypatch.setattr(vi, "_write_meta_atomic", boom)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 1
        assert p.transcript.read_bytes() == original
        assert "restored the original transcript" in caplog.text
        assert "could not restore" not in caplog.text
        assert f"could not remove the pre-repair copy {p.sidecar}" in caplog.text

    def test_the_meta_is_judged_from_the_file_on_disk_not_the_plan(self, corpus, monkeypatch):
        p = _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        real_write = vi._write_bytes_atomic
        calls = []

        def altering(path, data):
            calls.append(path)
            if len(calls) == 1:
                # Land a file that differs from the plan: drop every second dialogue entry.
                body = vi.parse_transcript_body(data.decode("utf-8"))
                kept, drop = [], False
                for entry in body.entries:
                    if entry.kind == vi.TRANSCRIPT_ENTRY_DIALOGUE:
                        drop = not drop
                        if drop and len(kept) > 1:
                            continue
                    kept.append(entry)
                data = vi.TranscriptBody(body.header, kept, body.trailer).render().encode("utf-8")
            real_write(path, data)

        monkeypatch.setattr(vi, "_write_bytes_atomic", altering)

        result = _run(corpus, apply=True)

        meta = json.loads(p.meta.read_text(encoding="utf-8"))
        on_disk, _ = vi.assess_transcript_markdown(p.transcript.read_text(encoding="utf-8"), 600, captions=False)
        assert meta["transcript_dialogue_entries"] == on_disk["transcript_dialogue_entries"]
        planned = sum(len(c.plan.pieces) for c in result["repair"].repaired)
        assert meta["transcript_dialogue_entries"] < planned


class TestShapesThatStayWhole:
    def test_an_unrecognized_wall_shape_declines_with_exit_3(self, corpus, monkeypatch, caplog):
        sentences, cues = _talk()
        p = _paths(corpus)
        p.transcript.write_text(
            '# Transcript: T\n\n**Status:** Complete\n\n---\n\n[00:12] Host: "' + " ".join(sentences) + "\n",
            encoding="utf-8",
        )
        p.meta.write_text(json.dumps(_meta()), encoding="utf-8")
        _stub_captions(monkeypatch, cues)
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 3
        assert _snapshot(corpus) == before
        assert "entry shape not recognized" in caplog.text

    def test_an_entry_hiding_an_unrecognized_stamp_line_stays_whole(self, corpus, monkeypatch, caplog):
        sentences, cues = _talk()
        p = _paths(corpus)
        p.transcript.write_text(
            "# Transcript: T\n\n**Status:** Complete\n\n---\n\n"
            f'[00:12] Host: "{" ".join(sentences[:80])}"\n'
            f'[07:40.500] Guest: "{" ".join(sentences[80:])}"\n',
            encoding="utf-8",
        )
        p.meta.write_text(json.dumps(_meta()), encoding="utf-8")
        _stub_captions(monkeypatch, cues)
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 3
        assert _snapshot(corpus) == before
        assert "unrecognized stamp" in caplog.text

    def test_a_wall_ending_the_file_without_a_newline_puts_every_piece_on_its_own_line(self):
        sentences, cues = _talk()
        text = "# Transcript: T\n\n---\n\n" + f'[00:12] Host: "{" ".join(sentences)}"'

        repair = vi.plan_transcript_repair(text, _cue_records(cues))

        pieces = repair.repaired[0].plan.pieces
        dialogue = _dialogue(vi.parse_transcript_body(repair.text))
        assert len(pieces) > 2
        assert len(dialogue) == len(pieces)
        assert not repair.text.endswith("\n")

    def test_no_dialogue_entries_exits_1_before_any_fetch(self, corpus, monkeypatch):
        p = _paths(corpus)
        p.transcript.write_text("# Transcript: T\n\n---\n\n  SCREEN [00:00] [slide]: only a slide\n", encoding="utf-8")
        p.meta.write_text(json.dumps(_meta()), encoding="utf-8")
        calls: list[str] = []
        _stub_captions(monkeypatch, _talk()[1], calls=calls)

        assert _exit_code(corpus, apply=True) == 1
        assert calls == []


class TestCaptionDeclineWording:
    @pytest.mark.parametrize(
        ("kind", "exception", "must", "must_not"),
        [
            ("blocked", "IpBlocked", "refused (IpBlocked); nothing changed. Run it again later", "no English"),
            ("absent", "NoTranscriptFound", "no English caption track (NoTranscriptFound)", "refused"),
            ("empty", None, "came back empty", "no English caption track"),
            ("video_unavailable", "VideoUnavailable", "reports the video unavailable", "no English"),
            ("no_library", None, "youtube-transcript-api is not installed", "no English"),
            ("other", "YouTubeRequestFailed", "unclassified reason (YouTubeRequestFailed)", "no English"),
        ],
    )
    def test_each_classified_kind_gets_its_own_truthful_wording(
        self, corpus, monkeypatch, caplog, kind, exception, must, must_not
    ):
        _write_corpus(corpus)
        _stub_no_captions(monkeypatch, kind, exception)
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 3

        assert _snapshot(corpus) == before
        assert must in caplog.text
        assert must_not not in caplog.text
        assert CAPTIONS_BLOCK_RECOVERY not in caplog.text
        assert "transcript_source" not in caplog.text


class TestSharedHelpers:
    def test_unclobbered_sidecar_path_keeps_a_free_name_and_suffixes_a_taken_one(self, tmp_path):
        free = tmp_path / "a.transcript.raw.pre-repair.txt"
        assert vi.unclobbered_sidecar_path(free) == free
        free.write_text("x")
        taken = vi.unclobbered_sidecar_path(free)
        assert taken.parent == free.parent and taken != free
        assert taken.name.startswith("a.transcript.raw.pre-repair.") and taken.suffix == ".txt"

    def test_follow_up_commands_double_quote_a_channel_with_a_space(self):
        index_cmd, graph_cmd = vi.repair_follow_up_commands("my chan")
        assert index_cmd == 'python scripts/video_intel.py index --incremental --channel "my chan"'
        assert graph_cmd == "python scripts/intel_graph.py load --force"

    def test_screen_recognizer_agrees_with_chunk_transcript(self, tmp_path):
        samples = [
            "  SCREEN [00:05] [slide]: indented",
            "    SCREEN [100:30-101:00] [code]: minutes past 99",
            "  SCREEN [1:02:03] [other]: hours",
            "  SCREEN [00:08.5] [other]: fractional seconds",
            "SCREEN [00:07] [slide]: not indented",
            "  SCREEN [x] [slide]: not a stamp",
            "\tSCREEN [00:09] [slide]: tab indented",
        ]
        path = tmp_path / "s.md"
        # The writer's layout: one blank line between entries.
        path.write_text("\n\n".join(f'[00:00] A: "open"\n\n{line}' for line in samples) + "\n", encoding="utf-8")
        chunked = [c["text"].splitlines()[0] for c in vi.chunk_transcript(path, chunk_size=1)]
        mine = [
            "".join(e.lines).splitlines()[0] for e in vi.parse_transcript_body(path.read_text(encoding="utf-8")).entries
        ]
        # chunk_transcript strips each chunk, so compare the lines stripped.
        assert [line.strip() for line in chunked if "SCREEN" in line] == [
            line.strip() for line in mine if "SCREEN" in line
        ]
        assert any("SCREEN" in line for line in mine)  # not vacuous

    def test_the_screen_recognizer_is_built_from_the_entry_pattern(self):
        assert vi.ENTRY_TIMESTAMP_PATTERN in vi._SCREEN_LINE_RE.pattern

    def test_the_planner_says_when_alignment_starts(self, caplog):
        text, raw = _host_guest_shape()
        with caplog.at_level(logging.INFO, logger="video_intel"):
            vi.plan_transcript_repair(text, _cue_records(raw))
        assert f"against {len(raw)} caption cues" in caplog.text


# ---------------------------------------------------------------------------
# Peer-review round: on-disk rollback, repeated cue text, SCREEN ownership,
# the duration bound
# ---------------------------------------------------------------------------


class TestRollbackFromDisk:
    """Inject an interrupt at each point and require the start state, with no sidecar."""

    def _interrupt_after_replace(self, monkeypatch, target_name: str):
        real_replace = vi.os.replace
        fired = []

        def replace(src, dst):
            real_replace(src, dst)
            if not fired and Path(dst).name == target_name:
                fired.append(dst)
                raise KeyboardInterrupt

        monkeypatch.setattr(vi.os, "replace", replace)
        return fired

    def test_interrupt_right_after_the_transcript_replace(self, corpus, monkeypatch):
        p = _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)
        fired = self._interrupt_after_replace(monkeypatch, p.transcript.name)

        with pytest.raises(KeyboardInterrupt):
            _run(corpus, apply=True)
        assert fired
        assert _snapshot(corpus) == before

    def test_interrupt_right_after_the_meta_replace(self, corpus, monkeypatch):
        p = _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)
        fired = self._interrupt_after_replace(monkeypatch, p.meta.name)

        with pytest.raises(KeyboardInterrupt):
            _run(corpus, apply=True)
        assert fired
        assert _snapshot(corpus) == before

    def test_interrupt_during_the_header_rewrite(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)

        def interrupt(*_a, **_kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(vi, "rewrite_transcript_header", interrupt)

        with pytest.raises(KeyboardInterrupt):
            _run(corpus, apply=True)
        assert _snapshot(corpus) == before

    def test_interrupt_after_both_writes_before_return(self, corpus, monkeypatch):
        _write_corpus(corpus)
        _stub_captions(monkeypatch, _talk()[1])
        before = _snapshot(corpus)

        def interrupt(*_a, **_kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(vi, "repair_follow_up_commands", interrupt)

        with pytest.raises(KeyboardInterrupt):
            _run(corpus, apply=True)
        assert _snapshot(corpus) == before

    def test_a_failed_restore_keeps_the_sidecar_and_names_it(self, corpus, monkeypatch, caplog):
        p = _write_corpus(corpus)
        original = p.transcript.read_bytes()
        _stub_captions(monkeypatch, _talk()[1])
        real_write = vi._write_bytes_atomic

        def no_restore(path, data):
            if data == original:
                raise OSError("read-only now")
            real_write(path, data)

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(vi, "_write_bytes_atomic", no_restore)
        monkeypatch.setattr(vi, "_write_meta_atomic", boom)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 1
        assert p.sidecar.read_bytes() == original
        assert f"the only copy of the original is {p.sidecar}" in caplog.text
        assert "could not restore the original transcript" in caplog.text


def _rolling_track(n: int = 16, step: float = 5.0):
    """Sixteen sentences spoken five seconds apart; each cue shows the previous sentence again plus the new one."""
    sentences = [f"Sentence {WORDS[i]} number {i} says {WORDS[(i * 5 + 2) % len(WORDS)]} clearly." for i in range(n)]
    cues = []
    for i, sentence in enumerate(sentences):
        shown = (sentences[i - 1] + " " if i else "") + sentence
        cues.append(caption_align.Cue(start=i * step, duration=step, text=shown))
    return sentences, cues


class TestRepeatedCueText:
    def test_a_rolling_track_stamps_each_sentence_at_the_cue_it_first_appears_in(self):
        sentences, cues = _rolling_track()

        plan = caption_align.plan_entry_split(" ".join(sentences), cues, original_seconds=0, target=25)

        assert plan.repairable and plan.coverage == 1.0
        for piece in plan.pieces[1:]:
            spoken_at = 5 * next(i for i, s in enumerate(sentences) if piece.text.startswith(s))
            assert piece.stamp_seconds == spoken_at, (piece.text[:30], piece.stamp_seconds)
        assert len(plan.pieces) > 2

    def test_entry_offsets_on_a_rolling_track_are_zero(self):
        sentences, cues = _rolling_track()

        offsets = caption_align.entry_offsets([(5 * i, s) for i, s in enumerate(sentences)], cues)

        assert [o.offset_seconds for o in offsets] == [0.0] * len(sentences)

    def test_a_track_without_repeats_aligns_exactly_as_before(self):
        def old_caption_words(cue_list):
            words, owner = [], []
            for index, cue in enumerate(cue_list):
                cue_words = caption_align.normalize_words(cue.text)
                words.extend(cue_words)
                owner.extend([index] * len(cue_words))
            return words, owner

        import difflib

        for raw in (_talk()[1], _talk(n=20, first=700.0)[1]):
            cues = [caption_align.Cue(s, 4.0, t) for s, t in raw]
            words = caption_align.normalize_words(" ".join(t for _, t in raw[::3]))
            caption, owner = old_caption_words(cues)
            expected = [None] * len(words)
            for a, b, size in difflib.SequenceMatcher(None, caption, words, autojunk=False).get_matching_blocks():
                for k in range(size):
                    expected[b + k] = owner[a + k]
            assert caption_align.align_words_to_cues(words, cues) == expected

    def test_a_cue_that_only_repeats_contributes_no_words(self):
        cues = [
            caption_align.Cue(0.0, 2.0, "alpha beta gamma delta"),
            caption_align.Cue(2.0, 2.0, "gamma delta"),
            caption_align.Cue(4.0, 2.0, "epsilon zeta"),
        ]
        assert caption_align.align_words_to_cues(["gamma", "delta", "epsilon"], cues) == [0, 0, 2]

    def test_a_partial_overlap_drops_only_the_repeated_words_and_one_shared_word_is_kept(self):
        cues = [
            caption_align.Cue(0.0, 2.0, "alpha beta gamma delta"),
            caption_align.Cue(2.0, 2.0, "gamma delta epsilon"),
            caption_align.Cue(4.0, 2.0, "epsilon zeta"),  # one shared word: ordinary speech, kept
        ]
        words = ["gamma", "delta", "epsilon", "epsilon", "zeta"]
        assert caption_align.align_words_to_cues(words, cues) == [0, 0, 1, 2, 2]


def _screen_trap_markdown() -> str:
    """Real writer output: a SCREEN block whose description and on-screen text hold entry-shaped lines and `---`."""
    sentences, _ = _talk()
    raw = {
        "transcripts": [
            {"start": "00:12", "voice": 1, "text": " ".join(sentences)},
            {"start": "09:30", "voice": 1, "text": "Last words."},
        ],
        "screen_content": [
            {
                "start": "01:00",
                "end": "01:30",
                "type": "slide",
                "description": 'A chat log:\n[00:00] Alice: "long quoted text on the slide"\n  SCREEN [00:05] [code]: nested\n---',
                "transcribed_text": 'quote line\n[00:01] Bob: "more"\n---',
            },
            {"start": "02:00", "type": "code", "description": "code", "code": 'x = 1\n[00:02] Carol: "y"'},
        ],
        "speakers": [{"voice": 1, "name": "Host"}],
    }
    return "# Transcript: T\n\n**Status:** Complete\n\n---\n\n" + vi.merge_transcript_json(raw, {})


class TestScreenOwnership:
    def test_entry_shaped_lines_inside_a_screen_block_stay_its_content(self):
        text = _screen_trap_markdown()

        body = vi.parse_transcript_body(text)

        assert body.render() == text
        assert [(e.kind, e.stamp) for e in body.entries] == [
            (vi.TRANSCRIPT_ENTRY_DIALOGUE, "00:12"),
            (vi.TRANSCRIPT_ENTRY_SCREEN, "01:00"),
            (vi.TRANSCRIPT_ENTRY_SCREEN, "02:00"),
            (vi.TRANSCRIPT_ENTRY_DIALOGUE, "09:30"),
        ]
        assert body.trailer == ""
        assert 'Alice: "long quoted text on the slide"' in "".join(body.entries[1].lines)

    def test_a_trap_line_in_the_last_screen_block_is_not_the_trailer(self):
        raw = {
            "transcripts": [{"start": "00:12", "voice": 1, "text": "hello"}],
            "screen_content": [{"start": "01:00", "type": "slide", "description": 'top\n---\n[00:00] Alice: "x"'}],
            "speakers": [{"voice": 1, "name": "Host", "evidence": "named"}],
        }
        text = "# Transcript: T\n\n---\n\n" + vi.merge_transcript_json(raw, {})

        body = vi.parse_transcript_body(text)

        assert body.render() == text
        assert len(body.entries) == 2
        assert "Speaker Identification Evidence" in body.trailer
        assert "top" in "".join(body.entries[1].lines)

    def test_a_repair_never_touches_lines_inside_a_screen_block(self):
        text = _screen_trap_markdown()
        _, raw = _talk()

        repaired = vi.plan_transcript_repair(text, [caption_align.Cue(s, 4.0, t) for s, t in raw]).text

        before = _screens(vi.parse_transcript_body(text))
        assert _screens(vi.parse_transcript_body(repaired)) == before
        for screen in before:
            assert screen in repaired

    def test_the_dialogue_reader_still_counts_a_trap_line_as_dialogue(self):
        # Documented, not fixed here: the assessor's reader reads lines, not entries.
        stamps = [e["start"] for e in vi.dialogue_entries_from_transcript_markdown(_screen_trap_markdown())]
        assert "00:00" in stamps


class TestDurationBound:
    def test_no_new_stamp_runs_past_the_video(self, corpus, monkeypatch):
        # Duration 490 s allows stamps up to 539 s; the track's last cues start at
        # about 551 and 556 s (4 of 100 cues, under the decline threshold), and
        # without the bound the plan stamps a piece at 556 s.
        sentences, cues = _talk()
        cue_records = [caption_align.Cue(s, 4.0, t) for s, t in cues]
        unbounded = caption_align.plan_entry_split(" ".join(sentences), cue_records, original_seconds=12)
        assert not vi.stamp_fits_duration(unbounded.pieces[-1].stamp_seconds, 490)
        p = _write_corpus(corpus, meta=_meta(duration_seconds=490))
        _stub_captions(monkeypatch, cues)

        result = _run(corpus, apply=True)

        assert result["outcome"] == "repaired"
        stamps = [vi.timestamp_to_seconds(e.stamp) for e in _body(p.transcript).entries if e.kind == "dialogue"]
        assert len(stamps) > 10
        assert all(vi.stamp_fits_duration(s, 490) for s in stamps), stamps
        rejoined = " ".join(parts.text for _, parts in _dialogue(_body(p.transcript)))
        assert " ".join(rejoined.split()) == " ".join(" ".join(sentences).split())

    def test_stamp_fits_duration_follows_the_one_plausibility_rule(self):
        tolerance = vi.timestamp_tolerance(100)
        assert vi.stamp_fits_duration(100 + tolerance, 100)
        assert not vi.stamp_fits_duration(101 + tolerance, 100)
        assert vi.stamp_fits_duration(10**6, None)

    def test_a_track_running_far_past_the_video_declines(self, corpus, monkeypatch, caplog):
        _write_corpus(corpus, meta=_meta(duration_seconds=100))
        _stub_captions(monkeypatch, _talk()[1])  # cues run to about 550 s
        before = _snapshot(corpus)

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert _exit_code(corpus, apply=True) == 3
        assert _snapshot(corpus) == before
        assert "different or re-uploaded video" in caplog.text
