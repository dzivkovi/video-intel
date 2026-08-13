"""Issue #129: transient-transport retry, and a non-zero exit for a missing artifact.

Two defects surfaced in the 2026-08-11/12 bulk ingest of 36 videos:

1. ``Server disconnected without sending a response.`` (``httpx.RemoteProtocolError``)
   is not a ``google.genai.errors.APIError``, so ``get_retry_delay`` returned
   ``None`` and one dropped socket killed a whole pipeline step. It happened 7
   times across three different stages under 4-way concurrency, and a plain
   serial re-run fixed every one.
2. A ``concepts`` step could report an error, write no ``.concepts.json``, and
   exit 0. Since ``is_processed()`` never looks at concepts, that video was never
   re-queued and never reached ``taxonomy.json`` or the search index.
"""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors

import video_intel
from gemini_common import (
    MAX_RETRIES_TRANSPORT,
    get_retry_delay,
    is_transient_transport_error,
)


class TestTransportErrorClassification:
    """What counts as a transient transport failure, and what must never be one."""

    def test_the_observed_failure_is_classified_as_transport(self):
        exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")

        assert is_transient_transport_error(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("connection refused"),
            httpx.ConnectTimeout("timed out connecting"),
            httpx.WriteTimeout("timed out writing"),
            httpx.PoolTimeout("no connection available"),
            httpx.ReadError("socket read failed"),
            httpx.WriteError("socket write failed"),
            httpx.ProxyError("proxy refused"),
        ],
    )
    def test_transport_class_is_retryable(self, exc):
        assert is_transient_transport_error(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.LocalProtocolError("we built a malformed request"),
            httpx.UnsupportedProtocol("bad://scheme"),
        ],
    )
    def test_client_side_faults_are_not_retryable(self, exc):
        """Retrying either of these fails identically and only burns time."""
        assert is_transient_transport_error(exc) is False

    def test_read_timeout_is_excluded_on_cost(self):
        """Codex peer review, PR #136.

        The ``read`` timeout in ``create_client`` is 1200s - 40x the other
        three. Retrying it risks a second 20-minute wait on the mindmap and
        concepts paths, which have no outer deadline (transcript calls are
        capped at 600s by ``_run_with_timeout``). Pre-#129 those failed after
        one 20-minute wait; doubling that is not a fix. Every failure actually
        observed in the issue was ``RemoteProtocolError``.
        """
        assert is_transient_transport_error(httpx.ReadTimeout("timed out reading")) is False
        # ...while the three SHORT timeouts (30s each) stay retryable.
        assert is_transient_transport_error(httpx.ConnectTimeout("x")) is True
        assert is_transient_transport_error(httpx.WriteTimeout("x")) is True
        assert is_transient_transport_error(httpx.PoolTimeout("x")) is True

    def test_plain_exceptions_are_not_transport_errors(self):
        assert is_transient_transport_error(RuntimeError("Server disconnected")) is False
        assert is_transient_transport_error(ValueError("timeout")) is False

    def test_http_status_error_is_not_a_transport_error(self):
        """ce-code-review (testing), PR #136: a named invariant nothing proved.

        ``HTTPStatusError`` carries a real server verdict, so it belongs to the
        ``APIError`` branch. If it ever landed in the transport net, a 403 or
        400 surfaced this way would be retried - the exact refusal-retry the
        issue forbids. Both the docstring and the CLAUDE.md guardrail name this
        exclusion, so it deserves a test rather than prose.
        """
        request = httpx.Request("POST", "https://example.invalid/v1")
        response = httpx.Response(403, request=request)
        exc = httpx.HTTPStatusError("permission denied", request=request, response=response)

        assert is_transient_transport_error(exc) is False
        assert get_retry_delay(exc, 0, max_retries_transport=99) is None


class TestRefusalsStillFailFast:
    """The error classes issue #129 explicitly forbids retrying."""

    def test_permission_denied_is_never_retried(self):
        """A members-only or gated video. Retrying re-bills the identical refusal."""
        exc = errors.APIError(403, {"error": {"message": "denied", "status": "PERMISSION_DENIED"}})

        assert get_retry_delay(exc, 0, max_retries_transport=MAX_RETRIES_TRANSPORT) is None

    def test_invalid_argument_is_never_retried(self):
        exc = errors.APIError(400, {"error": {"message": "bad", "status": "INVALID_ARGUMENT"}})

        assert get_retry_delay(exc, 0, max_retries_transport=MAX_RETRIES_TRANSPORT) is None

    def test_no_non_retryable_api_error_code_is_ever_retried(self):
        """Sweeps the non-retryable code space, rather than just 400/403.

        Scope note (ce-code-review, testing, PR #136): this does NOT prove the
        ``APIError`` branch's early ``return None`` is load-bearing today.
        ``errors.APIError`` subclasses no httpx exception, so deleting that
        return currently changes nothing observable - verified by running it.
        The early return is defense against a future refactor that widens the
        transport net; what this test actually guarantees is the outcome
        (no non-retryable code is ever retried), which is the property worth
        locking regardless of which line enforces it.
        """
        for code in (400, 401, 403, 404, 409, 422):
            exc = errors.APIError(code, {"error": {"message": "no", "status": "X"}})
            assert get_retry_delay(exc, 0, max_retries_transport=99) is None, code

    def test_prompt_zero_confabulation_is_not_an_exception_this_layer_sees(self):
        """The ``prompt == 0`` guards (issues #60/#119/#123) cannot be retried away.

        They read usage metadata off a response that arrived successfully, so
        they run after the retry loop has already returned. Nothing they raise
        is an httpx error, so even if one did reach this classifier it would not
        be retryable.
        """
        assert is_transient_transport_error(RuntimeError("prompt=0 confabulation; discarding")) is False


class TestTransportRetryBudget:
    def test_first_attempt_retries_with_a_short_backoff(self, monkeypatch):
        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")

        result = get_retry_delay(exc, 0, max_retries_transport=MAX_RETRIES_TRANSPORT)

        assert result is not None
        kind, wait, cap = result
        assert kind == "Transport error"
        assert cap == MAX_RETRIES_TRANSPORT
        # Must stay far below the 600s _run_with_timeout budget it competes with.
        assert wait <= 10

    def test_budget_is_bounded(self):
        """Never an unbounded loop - the CLAUDE.md "bounded retries only" rule."""
        exc = httpx.RemoteProtocolError("dropped")

        assert get_retry_delay(exc, MAX_RETRIES_TRANSPORT, max_retries_transport=MAX_RETRIES_TRANSPORT) is None

    def test_off_by_default_so_translate_video_is_unaffected(self):
        """``translate_video.py`` shares this helper and is operationally separate.

        It must not inherit a new retry policy as a side effect of a video-intel
        ticket, so the transport budget defaults to 0 and video-intel's call
        sites opt in explicitly.
        """
        exc = httpx.RemoteProtocolError("dropped")

        assert get_retry_delay(exc, 0) is None

    def test_transport_backoff_is_far_shorter_than_the_server_ladder(self, monkeypatch):
        """A dropped socket must not wait like a 503 does.

        The server ladder starts at 60s and climbs to 480s. Inside
        ``_run_with_timeout``'s 600s cap that would consume the budget on its
        own, leaving nothing for the retried call.
        """
        monkeypatch.setattr("gemini_common.random.uniform", lambda _a, _b: 0)
        transport = get_retry_delay(httpx.ReadError("x"), 0, max_retries_transport=2)
        server = get_retry_delay(errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}), 0)

        assert transport is not None and server is not None
        assert transport[1] * 10 < server[1]


class TestCallGeminiRetriesTransportErrors:
    """The seam all three pipeline stages inherit."""

    @staticmethod
    def _types_stub():
        class _Types:
            @staticmethod
            def FileData(**kw):
                return kw

            @staticmethod
            def VideoMetadata(**kw):
                return kw

            @staticmethod
            def Part(**kw):
                return kw

            @staticmethod
            def Content(**kw):
                return kw

            @staticmethod
            def GenerateContentConfig(**kw):
                return kw

            @staticmethod
            def SafetySetting(**kw):
                return kw

        return _Types

    def _client_failing_n_times(self, n, exc):
        calls = {"n": 0}

        class _Models:
            @staticmethod
            def generate_content(**_kw):
                calls["n"] += 1
                if calls["n"] <= n:
                    raise exc
                return type("R", (), {"text": "OK", "usage_metadata": None})()

        return type("C", (), {"models": _Models})(), calls

    def test_call_gemini_recovers_from_one_dropped_socket(self, monkeypatch):
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        client, calls = self._client_failing_n_times(1, exc)

        result = video_intel.call_gemini(client, self._types_stub(), "https://y/w", "prompt", "m")

        assert result == "OK"
        assert calls["n"] == 2

    def test_call_gemini_text_recovers_from_one_dropped_socket(self, monkeypatch):
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        client, calls = self._client_failing_n_times(1, exc)

        result = video_intel.call_gemini_text(client, self._types_stub(), "text", "m")

        assert result == "OK"
        assert calls["n"] == 2

    def test_call_gemini_gives_up_and_raises_after_the_cap(self, monkeypatch):
        """Bounded: a persistently dead connection must surface, not spin."""
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        client, calls = self._client_failing_n_times(99, exc)

        with pytest.raises(httpx.RemoteProtocolError):
            video_intel.call_gemini(client, self._types_stub(), "https://y/w", "prompt", "m")

        assert calls["n"] == MAX_RETRIES_TRANSPORT + 1

    def test_transport_retry_survives_an_earlier_retry_of_another_class(self, monkeypatch):
        """ce-code-review (reliability), PR #136 - the budget must be per-CLASS.

        The loop index counts failures of every class. With a transport budget
        of 1, a drop that lands after any earlier 429/5xx retry would arrive
        with ``attempt >= 1`` and be denied its only chance - silently
        reverting to pre-#129 behavior in exactly the compound-failure
        scenario the issue describes (7 drops under 4-way concurrency,
        alongside rate limiting). The regression is invisible to any
        single-exception-type test.
        """
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        seq = [
            errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}}),
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
        ]
        calls = {"n": 0}

        class _Models:
            @staticmethod
            def generate_content(**_kw):
                i = calls["n"]
                calls["n"] += 1
                if i < len(seq):
                    raise seq[i]
                return type("R", (), {"text": "OK", "usage_metadata": None})()

        client = type("C", (), {"models": _Models})()

        result = video_intel.call_gemini(client, self._types_stub(), "https://y/w", "prompt", "m")

        assert result == "OK"
        assert calls["n"] == 3  # 503 -> retry, drop -> retry, success

    def test_transport_budget_is_still_bounded_after_another_class_retried(self, monkeypatch):
        """The per-class counter must not become an unbounded transport loop."""
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        server = errors.APIError(503, {"error": {"message": "overloaded", "status": "UNAVAILABLE"}})
        drop = httpx.RemoteProtocolError("Server disconnected without sending a response.")
        seq = [server, drop, drop, drop]
        calls = {"n": 0}

        class _Models:
            @staticmethod
            def generate_content(**_kw):
                i = calls["n"]
                calls["n"] += 1
                raise seq[i] if i < len(seq) else drop

        client = type("C", (), {"models": _Models})()

        with pytest.raises(httpx.RemoteProtocolError):
            video_intel.call_gemini(client, self._types_stub(), "https://y/w", "prompt", "m")

        # 503 (retried) + one drop (retried) + one drop (over budget, raises).
        assert calls["n"] == 3

    def test_permission_denied_is_not_retried_through_call_gemini(self, monkeypatch):
        """End-to-end proof of the refusal rule at the seam, not just the classifier."""
        monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)
        exc = errors.APIError(403, {"error": {"message": "members only", "status": "PERMISSION_DENIED"}})
        client, calls = self._client_failing_n_times(99, exc)

        with pytest.raises(errors.APIError):
            video_intel.call_gemini(client, self._types_stub(), "https://y/w", "prompt", "m")

        assert calls["n"] == 1


class TestMissingPipelineArtifacts:
    """The exit-code predicate. Pure function, no Gemini, no filesystem stubs."""

    def test_all_present_is_complete(self, tmp_path):
        art = tmp_path / "a.transcript.md"
        art.write_text("x", encoding="utf-8")

        steps = [{"label": "transcript", "requested": True, "status": "done", "path": art}]

        assert video_intel.missing_pipeline_artifacts(steps) == []

    def test_error_status_is_a_gap(self, tmp_path):
        steps = [{"label": "concepts", "requested": True, "status": "error: boom", "path": tmp_path / "none.json"}]

        assert video_intel.missing_pipeline_artifacts(steps) == ["concepts"]

    def test_success_status_with_no_artifact_is_a_gap(self, tmp_path):
        """ "Reported done, produced nothing" is the silent half of the bug."""
        steps = [{"label": "transcript", "requested": True, "status": "done", "path": tmp_path / "absent.md"}]

        assert video_intel.missing_pipeline_artifacts(steps) == ["transcript"]

    def test_empty_artifact_is_a_gap(self, tmp_path):
        art = tmp_path / "empty.md"
        art.write_text("", encoding="utf-8")

        steps = [{"label": "mindmap", "requested": True, "status": "done", "path": art}]

        assert video_intel.missing_pipeline_artifacts(steps) == ["mindmap"]

    def test_stale_artifact_plus_error_status_is_still_a_gap(self, tmp_path):
        """Codex review: under --force a stale file survives a failed regeneration.

        Presence alone would read that as success and hide the very re-run the
        operator needs, so status and presence are BOTH required.
        """
        art = tmp_path / "stale.transcript.md"
        art.write_text("content from a previous, successful run", encoding="utf-8")

        steps = [{"label": "transcript", "requested": True, "status": "error: dropped", "path": art}]

        assert video_intel.missing_pipeline_artifacts(steps) == ["transcript"]

    @pytest.mark.parametrize("status", ["partial", "truncated_output", "thin", "ok", "complete", "skipped (exists)"])
    def test_degraded_but_real_statuses_are_not_gaps(self, tmp_path, status):
        """Designed partial success must stay exit 0.

        The salvage paths legitimately report a degraded status while writing a
        genuine artifact. Treating those as failures would turn every recovered
        transcript into a false alarm.
        """
        art = tmp_path / "a.transcript.md"
        art.write_text("real salvaged content", encoding="utf-8")

        steps = [{"label": "transcript", "requested": True, "status": status, "path": art}]

        assert video_intel.missing_pipeline_artifacts(steps) == []

    def test_unrequested_steps_can_never_be_a_gap(self, tmp_path):
        """Deliberate skips stay successes: skip_modes, mindmap_source=none,
        the issue #120 livestream mindmap suppression, concepts on _standalone."""
        steps = [
            {"label": "mindmap", "requested": False, "status": "skipped (skip_modes)", "path": tmp_path / "absent.md"},
            {"label": "concepts", "requested": False, "status": None, "path": None},
        ]

        assert video_intel.missing_pipeline_artifacts(steps) == []

    def test_gaps_are_reported_in_pipeline_order(self, tmp_path):
        steps = [
            {"label": "transcript", "requested": True, "status": "error: x", "path": tmp_path / "a.md"},
            {"label": "mindmap", "requested": True, "status": "done", "path": tmp_path / "b.md"},
        ]

        assert video_intel.missing_pipeline_artifacts(steps) == ["transcript", "mindmap"]


class TestFinishPipelineRun:
    def test_exits_partial_on_a_gap(self, tmp_path):
        steps = [{"label": "concepts", "requested": True, "status": "error: x", "path": tmp_path / "none.json"}]

        with pytest.raises(SystemExit) as exc_info:
            video_intel.finish_pipeline_run(steps, label="vid")

        assert exc_info.value.code == video_intel.EXIT_PARTIAL

    def test_partial_is_distinct_from_the_hard_failure_code(self):
        """3, not 1: a run that finished with a hole is not the same event as a
        run that stopped dead, and a batch driver has to be able to tell them
        apart to decide between "re-run this video" and "stop the batch"."""
        assert video_intel.EXIT_PARTIAL not in (0, 1)

    def test_returns_quietly_when_everything_landed(self, tmp_path):
        art = tmp_path / "a.md"
        art.write_text("x", encoding="utf-8")

        steps = [{"label": "mindmap", "requested": True, "status": "done", "path": art}]

        assert video_intel.finish_pipeline_run(steps, label="vid") is None


class TestCheckerAndWriterAgreeOnPaths:
    """The blind spot the rest of this file has by construction.

    ce-code-review (adversarial), PR #136: every stub-based test hands the
    artifact path to the stub, so the writer and the exit-code checker agree
    automatically and three real path-mismatch defects sat live under a green
    suite. These tests assert the two independently and compare, which is the
    only shape that can catch a checker that re-derives a path the writer never
    used. A mismatch here is a permanent false exit 3 on a healthy run - the
    worst failure mode for a guard whose whole value is being believed.
    """

    def test_concepts_artifact_lands_where_the_orchestrator_looks(self, tmp_path, monkeypatch):
        """`process_concepts` hardcodes ``output_dir / channel_name``.

        The `--file` orchestrator's identity resolver can set ``channel_dir`` to
        the source file's parent instead, so the step entry must be built from
        the writer's rule, not from ``channel_dir``.
        """
        output_dir = tmp_path / "corpus"
        channel = "demo"
        prefix = "2026-08-01-a-talk"
        video = {"video_id": "abc12345678", "url": "u", "title": "A Talk", "published": "2026-08-01"}

        monkeypatch.setattr(video_intel, "load_prompt", lambda _n: "prompt {{taxonomy}}")
        monkeypatch.setattr(video_intel, "call_gemini_text", lambda *_a, **_kw: '{"concepts": []}')

        written_prefix, status = video_intel.process_concepts(
            None, None, video, "# mindmap", {"concepts": {}}, "m", output_dir, channel, prefix=prefix
        )

        assert not status.startswith("error")
        # What the orchestrator's step entry claims the path is:
        claimed = output_dir / channel / f"{written_prefix}.concepts.json"
        assert claimed.exists(), f"writer and checker disagree; tree = {sorted(output_dir.rglob('*'))}"
        assert (
            video_intel.missing_pipeline_artifacts(
                [{"label": "concepts", "requested": True, "status": status, "path": claimed}]
            )
            == []
        )

    def test_mindmap_artifact_lands_under_the_prefix_the_orchestrator_indexed(self, tmp_path, monkeypatch):
        """Title rotation: the indexed prefix and the title-computed prefix differ.

        `_cmd_process_url` looks up an existing prefix from the video_id index,
        then must pass it to `process_mindmap`. Without that, the mindmap lands
        under the CURRENT title's slug while the checker looks under the indexed
        one, and the run exits 3 forever on a video whose mindmap is on disk.
        """
        output_dir = tmp_path / "corpus"
        channel = "demo"
        indexed_prefix = "2026-01-01-old-seo-title"
        video = {
            "video_id": "abc12345678",
            "url": "u",
            "title": "New Rotated Title",  # would compute a DIFFERENT slug
            "published": "2026-01-01",
        }
        assert video_intel.video_file_prefix(video) != indexed_prefix

        transcript_path = output_dir / channel / f"{indexed_prefix}.transcript.md"
        transcript_path.parent.mkdir(parents=True)
        transcript_path.write_text("# Transcript\n\n[00:00] hello\n", encoding="utf-8")
        monkeypatch.setattr(video_intel, "call_gemini_text", lambda *_a, **_kw: "# Mind map\n\n- a branch\n")

        video_intel.process_mindmap(
            None,
            None,
            video,
            "prompt",
            "m",
            output_dir,
            channel,
            prefix=indexed_prefix,
            source="transcript",
            transcript_path=transcript_path,
        )

        found = video_intel.find_mindmap_source(output_dir / channel, indexed_prefix)
        assert found is not None, f"mindmap not under the indexed prefix; tree = {sorted(output_dir.rglob('*'))}"


class TestConceptsFailureIsRecorded:
    """A concepts failure used to leave no trace anywhere - no artifact, no meta."""

    @staticmethod
    def _video():
        return {
            "video_id": "abc12345678",
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "title": "A Talk",
            "published": "2026-08-01",
        }

    def test_failure_lands_in_meta_json(self, tmp_path):
        meta_path = tmp_path / "chan" / "2026-08-01-a-talk.meta.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text('{"modes_completed": ["transcript"]}', encoding="utf-8")

        video_intel._record_concepts_error(meta_path, self._video(), meta_path.parent, "Server disconnected")

        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["concepts_status"].startswith("error")
        assert "Server disconnected" in meta["concepts_status"]
        # Pre-existing fields survive.
        assert meta["modes_completed"] == ["transcript"]

    def test_failure_writer_stamps_identity(self, tmp_path):
        """Issue #66: an identity-less meta is one _load_video_id_index skips,
        which re-queues the video for a full re-transcribe. The record of a
        cheap failure must not cost an expensive re-run."""
        meta_path = tmp_path / "chan" / "v.meta.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text("{}", encoding="utf-8")

        video_intel._record_concepts_error(meta_path, self._video(), meta_path.parent, "boom")

        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["video_id"] == "abc12345678"
        assert meta["channel"] == "chan"
        assert meta["title"] == "A Talk"
        assert meta["published"] == "2026-08-01"

    def test_never_creates_a_meta_that_did_not_exist(self, tmp_path):
        """ce-code-review (adversarial), PR #136 - recovery must not corrupt.

        A caller that does not pass an explicit ``prefix`` lets
        ``process_concepts`` recompute one from the current title, so on a
        title-rotated video a single dropped socket used to write a SECOND meta
        claiming the same ``video_id`` - manufacturing a dedupe group that never
        existed. Mirrors ``_record_transcript_error``: no meta, no write.
        """
        channel_dir = tmp_path / "chan"
        channel_dir.mkdir()
        absent = channel_dir / "2026-08-01-a-rotated-title.meta.json"

        video_intel._record_concepts_error(absent, self._video(), channel_dir, "Server disconnected")

        assert not absent.exists()
        assert list(channel_dir.iterdir()) == []

    def test_a_failing_write_never_raises_out_of_the_error_path(self, tmp_path, monkeypatch):
        """An error-path writer that raises destroys the error it preserves.

        Uncaught, this propagates through cmd_scan's concepts loop and aborts
        the whole scan before its failure summary prints - the same principle
        as issue #124's read side.
        """
        meta_path = tmp_path / "chan" / "v.meta.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text("{}", encoding="utf-8")

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(type(meta_path), "write_text", boom)

        video_intel._record_concepts_error(meta_path, self._video(), meta_path.parent, "boom")

    def test_corrupt_meta_does_not_mask_the_error_it_is_recording(self, tmp_path):
        """Issue #124: this is an error path, so the read is best-effort.

        A bare json.loads here would raise from inside the handler that exists
        to preserve the failure - losing the error entirely.
        """
        meta_path = tmp_path / "chan" / "v.meta.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_bytes(b'{"title": "tr\xff\xfeuncated')

        video_intel._record_concepts_error(meta_path, self._video(), meta_path.parent, "boom")

        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["concepts_status"] == "error: boom"
        assert meta["video_id"] == "abc12345678"

    def test_does_not_mark_the_mode_complete(self, tmp_path):
        """Must not route through update_meta - that is the SUCCESS writer, and
        it would clear last_error and append 'concepts' to modes_completed."""
        meta_path = tmp_path / "chan" / "v.meta.json"
        meta_path.parent.mkdir(parents=True)
        meta_path.write_text('{"modes_completed": ["transcript"]}', encoding="utf-8")

        video_intel._record_concepts_error(meta_path, self._video(), meta_path.parent, "boom")

        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert "concepts" not in meta["modes_completed"]
        assert meta["last_error"].startswith("concepts:")
