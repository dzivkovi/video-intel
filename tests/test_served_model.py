"""Record the model name the provider REPORTED for each Gemini call (issue #295).

The reported name can differ from the requested one (an alias resolving to its
backing model, a version suffix, or a different model served), and before this
every artifact recorded only the requested name. The reported name is read off
the response the callers already receive, so no extra call is made.

The writer tests drive the REAL writers with only the Gemini client faked (the
paid boundary) and the real ``google.genai.types``, and assert on what reached
meta.json [core: real-caller].
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import model_eval
import pytest
from google.genai import types as genai_types

import gemini_common
import video_intel as vi

REQUESTED = "gemini-3.7-flash"
SERVED = "gemini-3.8-flash"
OTHER_SERVED = "gemini-3.9-flash"

_MISSING = object()


def _response(text: str, served: object = SERVED, *, prompt: int = 5000) -> SimpleNamespace:
    """A Gemini-shaped response; ``served=_MISSING`` drops the attribute entirely."""
    fields = {
        "text": text,
        "candidates": None,
        "usage_metadata": SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=100,
            total_token_count=prompt + 100,
        ),
    }
    if served is not _MISSING:
        fields["model_version"] = served
    return SimpleNamespace(**fields)


class FakeClient:
    """Fakes ``client.models.generate_content``; returns the queued responses in order."""

    def __init__(self, *responses: SimpleNamespace) -> None:
        self._responses = list(responses)
        self.requested_models: list[str] = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        self.requested_models.append(model)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]


@pytest.fixture(autouse=True)
def _fresh_warning_memo():
    """Save, clear and restore the once-per-process memo (production never resets it)."""
    saved = set(vi._SERVED_MODEL_MISMATCHES_WARNED)
    vi._SERVED_MODEL_MISMATCHES_WARNED.clear()
    yield
    vi._SERVED_MODEL_MISMATCHES_WARNED.clear()
    vi._SERVED_MODEL_MISMATCHES_WARNED.update(saved)


def _mismatch_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and "_model_served" in r.getMessage()]


def _video() -> dict:
    return {
        "video_id": "vid001",
        "url": "https://www.youtube.com/watch?v=vid001",
        "title": "A Talk",
        "published": "2026-01-02",
    }


def _transcript_payload(minutes: list[int]) -> str:
    return json.dumps(
        {
            "transcripts": [{"start": f"00:{m:02d}:00", "voice": 1, "text": f"line {m}"} for m in minutes],
            "screen_content": [],
            "speakers": [{"voice": 1, "name": "Host"}],
        }
    )


def _meta(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The reader and the comparison rule
# ---------------------------------------------------------------------------


class _RaisingProperty:
    @property
    def model_version(self):
        raise RuntimeError("SDK drift")


class TestServedModelReader:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("gemini-3.8-flash", "gemini-3.8-flash"),
            ("  models/gemini-3.8-flash \n", "gemini-3.8-flash"),
            ("models/gemini-2.5-pro", "gemini-2.5-pro"),
        ],
    )
    def test_served_model_from_response_readable_returns_bare_name(self, raw, expected):
        assert gemini_common.served_model_from_response(SimpleNamespace(model_version=raw)) == expected

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(SimpleNamespace(), id="absent"),
            pytest.param(SimpleNamespace(model_version=None), id="none"),
            pytest.param(SimpleNamespace(model_version=""), id="empty"),
            pytest.param(SimpleNamespace(model_version="   "), id="blank"),
            pytest.param(SimpleNamespace(model_version="models/"), id="prefix_only"),
            pytest.param(SimpleNamespace(model_version=38), id="int"),
            pytest.param(MagicMock(), id="magicmock_attribute"),
            pytest.param(_RaisingProperty(), id="raising_property"),
            pytest.param(None, id="no_response"),
        ],
    )
    def test_served_model_from_response_unreadable_returns_none_and_logs_debug(self, response, caplog):
        caplog.set_level(logging.DEBUG, logger="gemini_common")

        assert gemini_common.served_model_from_response(response) is None
        assert any(r.levelno == logging.DEBUG and "served model" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        ("requested", "served", "differs"),
        [
            (REQUESTED, SERVED, True),
            (REQUESTED, REQUESTED, False),
            (f"models/{REQUESTED}", REQUESTED, False),
            (REQUESTED, None, False),
        ],
    )
    def test_served_model_differs_exact_match_after_prefix_strip(self, requested, served, differs):
        assert gemini_common.served_model_differs(requested, served) is differs

    def test_translate_video_does_not_use_the_served_model_reader(self):
        # Issue #129: translate_video.py shares gemini_common.py and must not
        # inherit video-intel behavior. It never imported log_usage_metadata,
        # and it must not pick up the served-model reader either.
        source = (Path(vi.SKILL_DIR) / "scripts" / "translate_video.py").read_text(encoding="utf-8")
        assert "served_model" not in source
        assert "log_usage_metadata" not in source


# ---------------------------------------------------------------------------
# Transcript writers
# ---------------------------------------------------------------------------


def _run_single_shot(tmp_path: Path, client: FakeClient, prefix: str = "2026-01-02-a-talk"):
    return vi.process_transcript(
        client,
        genai_types,
        _video(),
        "PROMPT",
        REQUESTED,
        tmp_path / "chan",
        prefix,
        transcript_source="gemini",
        force=True,
    )


class TestSingleShotTranscript:
    def test_process_transcript_served_differs_records_served_and_warns_once(self, tmp_path, caplog):
        client = FakeClient(_response(_transcript_payload([0, 1, 2, 3])))

        _run_single_shot(tmp_path, client)
        _, status = _run_single_shot(tmp_path, client)

        meta = _meta(tmp_path / "chan" / "2026-01-02-a-talk.meta.json")
        assert not status.startswith("error")
        assert meta["transcript_model_served"] == SERVED
        assert "model" not in meta, "the single-shot writer never wrote the requested field and must not start"
        assert client.requested_models == [REQUESTED, REQUESTED]
        warnings = _mismatch_warnings(caplog)
        assert len(warnings) == 1
        assert REQUESTED in warnings[0].getMessage() and SERVED in warnings[0].getMessage()
        assert "transcript_model_served" in warnings[0].getMessage()

    def test_process_transcript_salvage_records_served(self, tmp_path):
        good = json.loads(_transcript_payload([0, 1, 2, 3, 4, 5]))
        broken = json.dumps(good)[:-40]  # truncated JSON: full parse fails, salvage recovers
        client = FakeClient(_response(broken))

        _, status = _run_single_shot(tmp_path, client)

        assert status.startswith("partial"), status
        assert _meta(tmp_path / "chan" / "2026-01-02-a-talk.meta.json")["transcript_model_served"] == SERVED

    @pytest.mark.parametrize("served", [_MISSING, 38], ids=["absent", "non_string"])
    def test_process_transcript_unreadable_served_writes_nothing_and_succeeds(self, tmp_path, caplog, served):
        client = FakeClient(_response(_transcript_payload([0, 1, 2, 3]), served))

        _, status = _run_single_shot(tmp_path, client)

        assert not status.startswith("error")
        assert "transcript_model_served" not in _meta(tmp_path / "chan" / "2026-01-02-a-talk.meta.json")
        assert _mismatch_warnings(caplog) == []

    def test_process_transcript_prompt_zero_still_trips_guard_with_served_model(self, tmp_path):
        client = FakeClient(_response(_transcript_payload([0, 1]), prompt=0))

        _, status = _run_single_shot(tmp_path, client)

        assert "confabulation" in status

    def test_captions_transcript_retires_a_gemini_served_model(self, tmp_path, monkeypatch):
        client = FakeClient(_response(_transcript_payload([0, 1, 2, 3])))
        _run_single_shot(tmp_path, client)
        meta_path = tmp_path / "chan" / "2026-01-02-a-talk.meta.json"
        assert _meta(meta_path)["transcript_model_served"] == SERVED
        captions = SimpleNamespace(
            snippets=[(float(s), f"cue {s}") for s in range(0, 600, 5)], is_generated=True, language="en"
        )
        monkeypatch.setattr(vi, "fetch_english_captions", lambda video_id, **_kw: captions)

        result = vi._try_captions_transcript(
            _video(),
            tmp_path / "chan" / "2026-01-02-a-talk.transcript.md",
            meta_path,
            "2026-01-02-a-talk",
            reason="r",
            force=True,
            duration_seconds=600,
        )

        assert result is not None
        assert "transcript_model_served" not in _meta(meta_path)
        assert "transcript_model_served" in vi.TRANSCRIPT_ARTIFACT_FIELDS


class TestChunkedTranscript:
    @staticmethod
    def _run(tmp_path: Path, client: FakeClient) -> str:
        return vi._run_chunked_transcript_url(
            client=client,
            types=genai_types,
            video=_video(),
            prompt_text="P",
            model=REQUESTED,
            channel_dir=tmp_path / "chan",
            prefix="p",
            chunks=[(0, 600), (600, 1200)],
            duration_seconds=1200,
            chunk_minutes=10,
            force=True,
        )

    def test_chunked_agreeing_chunks_record_single_value_and_keep_requested_model(self, tmp_path, caplog):
        client = FakeClient(
            _response(_transcript_payload([0, 2, 4, 6, 8])), _response(_transcript_payload([10, 12, 14, 16, 18]))
        )

        self._run(tmp_path, client)

        meta = _meta(tmp_path / "chan" / "p.meta.json")
        assert meta["transcript_model_served"] == SERVED
        assert meta["model"] == REQUESTED
        assert len(_mismatch_warnings(caplog)) == 1

    def test_chunked_disagreeing_chunks_record_sorted_joined_string(self, tmp_path):
        client = FakeClient(
            _response(_transcript_payload([0, 2, 4, 6, 8]), OTHER_SERVED),
            _response(_transcript_payload([10, 12, 14, 16, 18]), SERVED),
        )

        self._run(tmp_path, client)

        assert _meta(tmp_path / "chan" / "p.meta.json")["transcript_model_served"] == f"{SERVED},{OTHER_SERVED}"

    def test_chunked_unreadable_served_writes_nothing(self, tmp_path):
        client = FakeClient(_response(_transcript_payload([0, 2, 4, 6, 8]), _MISSING))

        self._run(tmp_path, client)

        assert "transcript_model_served" not in _meta(tmp_path / "chan" / "p.meta.json")


# ---------------------------------------------------------------------------
# Mindmap writer
# ---------------------------------------------------------------------------


class TestMindmap:
    @staticmethod
    def _run(tmp_path: Path, client: FakeClient, source: str):
        return vi.process_mindmap(
            client,
            genai_types,
            _video(),
            "P",
            REQUESTED,
            tmp_path,
            "chan",
            source=source,
            force=True,
        )

    @pytest.mark.parametrize("source", ["video", "transcript"])
    def test_process_mindmap_served_differs_records_served_and_warns_once(self, tmp_path, caplog, source):
        channel_dir = tmp_path / "chan"
        channel_dir.mkdir()
        (channel_dir / "2026-01-02-a-talk.transcript.md").write_text("[00:00] hello\n", encoding="utf-8")
        client = FakeClient(_response("## Topic\n\n* bullet (0:00)\n"))

        self._run(tmp_path, client, source)
        prefix, status = self._run(tmp_path, client, source)

        meta = _meta(channel_dir / f"{prefix}.meta.json")
        assert status == "done"
        assert meta["mindmap_model_served"] == SERVED
        assert meta["model"] == REQUESTED
        warnings = _mismatch_warnings(caplog)
        assert len(warnings) == 1
        assert "mindmap_model_served" in warnings[0].getMessage()

    def test_process_mindmap_unreadable_served_drops_previous_value(self, tmp_path):
        self._run(tmp_path, FakeClient(_response("## T\n")), "video")
        meta_path = tmp_path / "chan" / "2026-01-02-a-talk.meta.json"
        assert _meta(meta_path)["mindmap_model_served"] == SERVED

        _, status = self._run(tmp_path, FakeClient(_response("## T\n", _MISSING)), "video")

        assert status == "done"
        assert "mindmap_model_served" not in _meta(meta_path)

    def test_mindmap_served_model_is_carried_by_dedupe_provenance(self):
        loser = {"mindmap_model_served": SERVED, "mindmap_source": "video"}

        assert vi._mode_provenance_fields("scan", loser)["mindmap_model_served"] == SERVED
        assert vi._mode_provenance_fields("transcript", {"transcript_model_served": SERVED}) == {
            "transcript_model_served": SERVED
        }


# ---------------------------------------------------------------------------
# Concepts writer
# ---------------------------------------------------------------------------


class TestConcepts:
    @staticmethod
    def _run(tmp_path: Path, client: FakeClient):
        return vi.process_concepts(
            client,
            genai_types,
            _video(),
            "## Topic\n\n* bullet\n",
            {"version": 1, "concepts": {}},
            REQUESTED,
            tmp_path,
            "chan",
            force=True,
        )

    _BODY = json.dumps({"concepts": [{"concept_id": "ai.x", "preferred_label": "X", "status": "new"}]})

    def test_process_concepts_served_differs_records_served_and_warns_once(self, tmp_path, caplog):
        client = FakeClient(_response(self._BODY))

        self._run(tmp_path, client)
        prefix, status = self._run(tmp_path, client)

        meta = _meta(tmp_path / "chan" / f"{prefix}.meta.json")
        assert status.startswith("done")
        assert meta["concepts_model_served"] == SERVED
        assert meta["concepts_status"] == "ok"
        concepts = json.loads((tmp_path / "chan" / f"{prefix}.concepts.json").read_text(encoding="utf-8"))
        assert set(concepts) == {"video_id", "extracted_from", "source_prompt", "concepts"}
        warnings = _mismatch_warnings(caplog)
        assert len(warnings) == 1
        assert "concepts_model_served" in warnings[0].getMessage()

    def test_process_concepts_unreadable_served_writes_nothing(self, tmp_path):
        prefix, status = self._run(tmp_path, FakeClient(_response(self._BODY, _MISSING)))

        meta = _meta(tmp_path / "chan" / f"{prefix}.meta.json")
        assert status.startswith("done")
        assert "concepts_model_served" not in meta


# ---------------------------------------------------------------------------
# model_eval scorecard
# ---------------------------------------------------------------------------


_EVAL_FIXTURE = {"id": "fx1", "url": "https://www.youtube.com/watch?v=vid001", "start": 600}


class TestModelEvalScorecard:
    def test_run_one_records_model_served_and_caches_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(model_eval, "prompt_override", lambda _name: None)
        client = FakeClient(_response(_transcript_payload([10, 11, 12])))

        row = model_eval.run_one(client, genai_types, _EVAL_FIXTURE, REQUESTED, None, 600, tmp_path)

        assert row["model_served"] == SERVED
        cached = json.loads((tmp_path / f"fx1__{REQUESTED}.usage.json").read_text(encoding="utf-8"))
        assert cached["served_model"] == SERVED
        rerun = model_eval.run_one(
            FakeClient(_response("unused")), genai_types, _EVAL_FIXTURE, REQUESTED, None, 600, tmp_path
        )
        assert rerun["model_served"] == SERVED

    def test_run_one_legacy_cache_without_served_records_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(model_eval, "prompt_override", lambda _name: None)
        (tmp_path / f"fx1__{REQUESTED}.json").write_text(_transcript_payload([10, 11]), encoding="utf-8")
        (tmp_path / f"fx1__{REQUESTED}.usage.json").write_text(json.dumps({"prompt": 10}), encoding="utf-8")

        row = model_eval.run_one(
            FakeClient(_response("unused")), genai_types, _EVAL_FIXTURE, REQUESTED, None, 600, tmp_path
        )

        assert row["model_served"] is None

    def test_render_shows_served_model_only_when_it_differs(self):
        base = model_eval.score(_transcript_payload([10, 11]), {"prompt": 10}, REQUESTED, 600, None, start_secs=600)
        differs = {**base, "fixture": "fx1", "model_served": SERVED}
        same = {**base, "fixture": "fx2", "model": SERVED, "model_served": SERVED}
        manifest = {"fixtures": [{"id": "fx1", "facet": "a"}, {"id": "fx2", "facet": "b"}]}

        card = model_eval.render([differs, same], [REQUESTED, SERVED], manifest, None)

        assert f"{REQUESTED} (served {SERVED})" in card
        assert f"{SERVED} (served" not in card


# ---------------------------------------------------------------------------
# Review follow-ups: guards that a mutation could otherwise remove unnoticed
# ---------------------------------------------------------------------------


class _TextRaises:
    """A response whose callback fires normally but whose ``.text`` raises."""

    model_version = SERVED
    candidates = None
    usage_metadata = None

    @property
    def text(self):
        raise RuntimeError("404 NOT_FOUND: CachedContent not found")


class _FakePromptCache:
    def __init__(self) -> None:
        self.invalidated = False

    def prompt_for(self, model, taxonomy_concepts):
        return "cachedContents/c1", ""

    def invalidate(self) -> None:
        self.invalidated = True


class _CacheThenInlineClient:
    """The cached call answers (callback fires with SERVED) and then fails; the inline retry answers."""

    def __init__(self, inline_response: SimpleNamespace) -> None:
        self.inline_response = inline_response
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        return _TextRaises() if config.cached_content else self.inline_response


class TestConceptsRetireAndRetry:
    _BODY = TestConcepts._BODY

    def test_process_concepts_unreadable_rerun_drops_previous_served(self, tmp_path):
        TestConcepts._run(tmp_path, FakeClient(_response(self._BODY)))
        meta_path = tmp_path / "chan" / "2026-01-02-a-talk.meta.json"
        assert _meta(meta_path)["concepts_model_served"] == SERVED

        _, status = TestConcepts._run(tmp_path, FakeClient(_response(self._BODY, _MISSING)))

        assert status.startswith("done")
        assert "concepts_model_served" not in _meta(meta_path)

    def _run_cached_then_inline(self, tmp_path):
        cache = _FakePromptCache()
        prefix, status = vi.process_concepts(
            _CacheThenInlineClient(_response(self._BODY, _MISSING)),
            genai_types,
            _video(),
            "## Topic\n",
            {"version": 1, "concepts": {}},
            REQUESTED,
            tmp_path,
            "chan",
            force=True,
            prompt_cache=cache,
        )
        assert cache.invalidated, "the cached call must have failed into the inline retry"
        assert status.startswith("done"), status
        return _meta(tmp_path / "chan" / f"{prefix}.meta.json")

    def test_failed_cached_call_served_model_is_not_credited_to_the_inline_retry(self, tmp_path):
        meta = self._run_cached_then_inline(tmp_path)

        assert "concepts_model_served" not in meta

    def test_inline_callback_failing_before_capture_cannot_inherit_the_cached_value(self, tmp_path, monkeypatch):
        real = vi.log_usage_metadata
        calls = {"n": 0}

        def flaky(response, label):
            calls["n"] += 1
            if calls["n"] == 2:  # the inline retry's callback dies before the capture runs
                raise RuntimeError("callback broke")
            return real(response, label)

        monkeypatch.setattr(vi, "log_usage_metadata", flaky)

        meta = self._run_cached_then_inline(tmp_path)

        assert "concepts_model_served" not in meta

    def test_capture_served_model_unreadable_removes_a_stale_value(self):
        capture = {vi.SERVED_MODEL_CAPTURE_KEY: SERVED, "prompt": 10}

        vi._capture_served_model(SimpleNamespace(), capture, requested=REQUESTED, mode="concepts")

        assert capture == {"prompt": 10}


class TestWarningRule:
    def test_served_equal_to_requested_records_value_without_warning(self, tmp_path, caplog):
        client = FakeClient(_response(_transcript_payload([0, 1, 2, 3]), REQUESTED))

        _run_single_shot(tmp_path, client)

        assert _meta(tmp_path / "chan" / "2026-01-02-a-talk.meta.json")["transcript_model_served"] == REQUESTED
        assert _mismatch_warnings(caplog) == []

    def test_models_prefixed_and_bare_request_warn_once_for_one_mismatch(self, caplog):
        for requested in (f"models/{REQUESTED}", REQUESTED):
            vi._capture_served_model(SimpleNamespace(model_version=SERVED), {}, requested=requested, mode="transcript")

        assert len(_mismatch_warnings(caplog)) == 1


class _ChunkScriptClient:
    """Each chunk's call either returns a response or raises (``TranscriptTimeout``: no callback fires)."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, model, contents, config):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class TestChunkedPerChunkCapture:
    @staticmethod
    def _run(tmp_path: Path, client) -> str:
        return vi._run_chunked_transcript_url(
            client=client,
            types=genai_types,
            video=_video(),
            prompt_text="P",
            model=REQUESTED,
            channel_dir=tmp_path / "chan",
            prefix="p",
            chunks=[(0, 600), (600, 1200), (1200, 1800)],
            duration_seconds=1800,
            chunk_minutes=10,
            force=True,
        )

    def test_a_chunk_whose_call_did_not_return_contributes_nothing(self, tmp_path):
        client = _ChunkScriptClient(
            _response(_transcript_payload([0, 2, 4, 6, 8]), SERVED),
            vi.TranscriptTimeout("chunk 2 hung"),
            _response(_transcript_payload([20, 22, 24, 26, 28]), OTHER_SERVED),
        )

        self._run(tmp_path, client)

        assert _meta(tmp_path / "chan" / "p.meta.json")["transcript_model_served"] == f"{SERVED},{OTHER_SERVED}"

    def test_only_the_returning_chunk_is_recorded(self, tmp_path):
        client = _ChunkScriptClient(
            _response(_transcript_payload([0, 2, 4, 6, 8]), SERVED),
            vi.TranscriptTimeout("chunk 2 hung"),
            vi.TranscriptTimeout("chunk 3 hung"),
        )

        self._run(tmp_path, client)

        assert _meta(tmp_path / "chan" / "p.meta.json")["transcript_model_served"] == SERVED

    def test_each_chunk_has_its_own_capture_so_a_later_chunk_never_inherits_an_earlier_one(self, tmp_path):
        # Chunk 1 is discarded by the prompt==0 guard (its call returned, so its
        # served model still counts). Chunk 2's response carries no usage
        # metadata, so its callback cannot refill a shared dict: with one dict
        # hoisted out of the loop, chunk 2 would inherit chunk 1's prompt=0 and
        # be discarded too, losing every chunk and the meta write with them.
        no_usage = SimpleNamespace(
            text=_transcript_payload([10, 12, 14, 16, 18]), candidates=None, usage_metadata=None, model_version=SERVED
        )
        client = _ChunkScriptClient(
            _response(_transcript_payload([0, 2, 4, 6, 8]), OTHER_SERVED, prompt=0),
            no_usage,
        )

        vi._run_chunked_transcript_url(
            client=client,
            types=genai_types,
            video=_video(),
            prompt_text="P",
            model=REQUESTED,
            channel_dir=tmp_path / "chan",
            prefix="p",
            chunks=[(0, 600), (600, 1200)],
            duration_seconds=1200,
            chunk_minutes=10,
            force=True,
        )

        meta = _meta(tmp_path / "chan" / "p.meta.json")
        assert meta["transcript_confabulated_chunks"] == 1
        assert meta["transcript_model_served"] == f"{SERVED},{OTHER_SERVED}"


class TestConceptsProvenanceAndVerdict:
    def test_concepts_served_model_is_carried_by_dedupe_provenance(self):
        loser = {"concepts_model_served": SERVED, "concepts_status": "ok"}

        assert vi._mode_provenance_fields("concepts", loser) == {"concepts_model_served": SERVED}

    def test_verdict_line_names_the_served_model_of_each_side(self):
        base = model_eval.score(_transcript_payload([10, 11]), {"prompt": 10}, REQUESTED, 600, None, start_secs=600)
        candidate = {**base, "fixture": "fx1", "model": REQUESTED, "model_served": SERVED}
        incumbent = {**base, "fixture": "fx1", "model": SERVED, "model_served": SERVED}
        manifest = {"fixtures": [{"id": "fx1", "facet": "a"}]}

        card = model_eval.render([candidate, incumbent], [REQUESTED, SERVED], manifest, SERVED)

        verdict = card.split("## Verdict", 1)[1]
        assert f"- `{REQUESTED}` (served {SERVED}) vs `{SERVED}`: mean max_gap" in verdict


class TestReportedNameRules:
    """The WARNING tolerates a version suffix; the scorecard shows any difference."""

    @pytest.mark.parametrize(
        ("requested", "served", "differs"),
        [
            ("gemini-2.0-flash", "gemini-2.0-flash-001", False),
            ("models/gemini-2.0-flash", "models/gemini-2.0-flash-001", False),
            ("gemini-2.0-flash", "gemini-2.0-flashy", True),
            (REQUESTED, SERVED, True),
            ("gemini-flash-latest", SERVED, True),
        ],
    )
    def test_served_model_differs_tolerates_only_a_hyphenated_version_suffix(self, requested, served, differs):
        assert gemini_common.served_model_differs(requested, served) is differs

    @pytest.mark.parametrize(
        ("requested", "served", "differs"),
        [
            ("gemini-2.0-flash", "gemini-2.0-flash-001", True),
            ("models/gemini-2.0-flash", "gemini-2.0-flash", False),
            (REQUESTED, None, False),
        ],
    )
    def test_reported_name_differs_flags_any_difference_after_prefix_strip(self, requested, served, differs):
        assert gemini_common.reported_name_differs(requested, served) is differs

    def test_version_suffix_records_reported_name_without_warning(self, tmp_path, caplog):
        client = FakeClient(_response(_transcript_payload([0, 1, 2, 3]), f"{REQUESTED}-001"))

        _run_single_shot(tmp_path, client)

        meta = _meta(tmp_path / "chan" / "2026-01-02-a-talk.meta.json")
        assert meta["transcript_model_served"] == f"{REQUESTED}-001"
        assert _mismatch_warnings(caplog) == []

    @pytest.mark.parametrize(
        ("requested", "served"),
        [("gemini-2.0-flash", "gemini-2.0-flashy"), (REQUESTED, SERVED), ("gemini-flash-latest", SERVED)],
        ids=["no_hyphen_boundary", "different_version", "alias"],
    )
    def test_mismatch_warns_once_with_neutral_wording(self, caplog, requested, served):
        vi._capture_served_model(SimpleNamespace(model_version=served), {}, requested=requested, mode="mindmap")

        warnings = _mismatch_warnings(caplog)
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert requested in message and served in message
        assert "alias" in message and "different model" in message and "mindmap_model_served" in message
        assert "redirect" not in message.lower() and "retired" not in message.lower()

    def test_card_shows_a_version_suffix_the_warning_ignores(self):
        base = model_eval.score(_transcript_payload([10, 11]), {"prompt": 10}, REQUESTED, 600, None, start_secs=600)
        row = {**base, "fixture": "fx1", "model_served": f"{REQUESTED}-001"}
        manifest = {"fixtures": [{"id": "fx1", "facet": "a"}]}

        card = model_eval.render([row], [REQUESTED], manifest, None)

        assert f"{REQUESTED} (served {REQUESTED}-001)" in card
