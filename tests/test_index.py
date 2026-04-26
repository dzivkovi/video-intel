"""Tests for adaptive batch-halving in the Voyage embedding helper.

Covers issue #44 / docs/plans/2026-04-26-001-fix-voyage-batch-halving-plan.md.
The helper under test is `video_intel._embed_batch`. We exercise it with a
hand-rolled fake Voyage client so no network or credentials are touched.
"""

from __future__ import annotations

import logging
import math
from types import SimpleNamespace

import pytest

import video_intel as vi

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

TOKEN_CAP_MESSAGE = (
    "Request to model 'voyage-4-large' failed. "
    "The max allowed tokens per submitted batch is 120000. "
    "Your batch has 128972 tokens after truncation. "
    "Please lower the number of tokens in the batch."
)
RATE_LIMIT_MESSAGE = "Rate limit exceeded. Please wait."
CONNECTION_MESSAGE = "Connection reset by peer"


class _FakeInvalidRequestError(Exception):
    """Stand-in for voyageai.error.InvalidRequestError. We do not import the
    real class because we want the test to pass even when the SDK is not
    installed; the production code keys on the message substring, which is
    SDK-version stable."""


def _embeddings_for(batch):
    """Return a SimpleNamespace shaped like a real Voyage response."""
    return SimpleNamespace(embeddings=[[0.0] * 4 for _ in batch])


class FakeVoyageClient:
    """Scripted Voyage client: each call dequeues one entry from `behaviors`.

    A behavior of `"ok"` returns a fake-embeddings response sized to the
    batch. A behavior of `Exception` raises that exception. A behavior of
    `("err_then_recover", err)` raises once, then the *same* slot continues
    serving "ok" — used to model intermittent errors. The helper records
    every call's batch size into `call_batch_sizes` so tests can assert
    halving depth.
    """

    def __init__(self, behaviors):
        self._behaviors = list(behaviors)
        self.call_batch_sizes: list[int] = []
        self.call_count = 0

    def embed(self, texts, *_args, **_kwargs):
        self.call_count += 1
        self.call_batch_sizes.append(len(texts))
        if not self._behaviors:
            return _embeddings_for(texts)
        action = self._behaviors.pop(0)
        if action == "ok":
            return _embeddings_for(texts)
        if isinstance(action, BaseException):
            raise action
        raise AssertionError(f"unexpected behavior {action!r}")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Zero out the inter-batch and retry-backoff sleeps so tests run instantly."""
    monkeypatch.setattr("video_intel.time.sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _stable_jitter(monkeypatch):
    """Make backoff jitter deterministic for assertions."""
    monkeypatch.setattr("video_intel.random.uniform", lambda _a, _b: 0)


# ---------------------------------------------------------------------------
# Unit 1: happy path — no halving
# ---------------------------------------------------------------------------


class TestEmbedBatchHappyPath:
    def test_single_batch_returns_all_embeddings_no_warnings(self, caplog):
        client = FakeVoyageClient(behaviors=["ok"])
        texts = ["chunk"] * 128

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        assert len(result) == 128
        assert client.call_count == 1
        assert client.call_batch_sizes == [128]
        assert not [r for r in caplog.records if "splitting" in r.message]


# ---------------------------------------------------------------------------
# Unit 2: token-cap error triggers split
# ---------------------------------------------------------------------------


class TestEmbedBatchTokenCapHalving:
    def test_token_cap_error_splits_batch_in_half_and_succeeds(self, caplog):
        err = _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        client = FakeVoyageClient(behaviors=[err, "ok", "ok"])
        texts = ["chunk"] * 128

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        assert len(result) == 128
        assert client.call_count == 3
        assert client.call_batch_sizes == [128, 64, 64]
        split_logs = [r for r in caplog.records if "splitting" in r.message.lower()]
        assert len(split_logs) == 1
        assert "Voyage" in split_logs[0].message

    def test_split_preserves_input_order(self):
        err = _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        client = FakeVoyageClient(behaviors=[err, "ok", "ok"])
        texts = [f"chunk-{i}" for i in range(128)]

        # Make embeddings echo their input index so order is checkable.
        def _ordered_embed(texts_, *_a, **_kw):
            client.call_count += 1
            client.call_batch_sizes.append(len(texts_))
            if client._behaviors:
                action = client._behaviors.pop(0)
                if isinstance(action, BaseException):
                    raise action
            return SimpleNamespace(embeddings=[[float(int(t.split("-")[1]))] for t in texts_])

        client.embed = _ordered_embed  # type: ignore[method-assign]
        result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")
        assert [int(v[0]) for v in result] == list(range(128))


# ---------------------------------------------------------------------------
# Unit 3: recursive halving (split twice)
# ---------------------------------------------------------------------------


class TestEmbedBatchRecursiveHalving:
    def test_two_levels_of_halving_produces_four_size32_calls(self, caplog):
        err = _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        # 1 fail at 128 → split to 64+64. Each 64 fails → split to 32+32.
        # Order of pending after first split: [64, 64]. First 64 fails →
        # pending becomes [32, 32, 64]. Both 32s succeed, then second 64
        # fails → split to [32, 32]. Both succeed.
        # Total calls: 1 fail + 1 fail + 2 ok + 1 fail + 2 ok = 7.
        client = FakeVoyageClient(behaviors=[err, err, "ok", "ok", err, "ok", "ok"])
        texts = ["chunk"] * 128

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        assert len(result) == 128
        assert client.call_count == 7
        # Three split events, each emits one WARN.
        split_logs = [r for r in caplog.records if "splitting" in r.message.lower()]
        assert len(split_logs) == 3


# ---------------------------------------------------------------------------
# Unit 4: pathological single chunk → bounded recursion + raise
# ---------------------------------------------------------------------------


class TestEmbedBatchRecursionBound:
    def test_pathological_chunk_raises_after_bounded_calls(self, caplog):
        err = _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        # Always raises token-cap regardless of size.
        client = FakeVoyageClient(behaviors=[err] * 64)
        texts = ["chunk"] * 128

        with caplog.at_level(logging.ERROR, logger="video_intel"), pytest.raises(_FakeInvalidRequestError):
            vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        # The pending queue is depth-first (prepend halves, pop from front), so
        # the leftmost leaf is the one that raises. Worst-case call count is
        # the depth of the halving tree: log2(128 / MIN_BATCH_SIZE) + 1 = 6.
        # Asserting the exact value pins both bounded-recursion and queue
        # ordering so any future flip to breadth-first (or unbounded) fails
        # loudly here.
        import math

        expected_depth = int(math.log2(128 // vi.MIN_BATCH_SIZE)) + 1
        assert client.call_count == expected_depth
        # The very last call must be at the MIN_BATCH_SIZE floor; that is what
        # pushed the helper across the raise threshold.
        assert client.call_batch_sizes[-1] == vi.MIN_BATCH_SIZE
        # Operator-visible diagnostic must be emitted at ERROR level.
        floor_logs = [r for r in caplog.records if r.levelname == "ERROR" and "floor" in r.message.lower()]
        assert floor_logs, "expected ERROR log naming the floor batch size"

    def test_min_batch_size_constant_exists_and_is_positive(self):
        assert hasattr(vi, "MIN_BATCH_SIZE")
        assert isinstance(vi.MIN_BATCH_SIZE, int)
        assert vi.MIN_BATCH_SIZE >= 1

    def test_halving_depth_scales_as_log2_for_different_initial_size(self):
        """Same depth-first property at a different starting size."""
        err = _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        client = FakeVoyageClient(behaviors=[err] * 64)
        texts = ["chunk"] * 64

        with pytest.raises(_FakeInvalidRequestError):
            vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        # log2(64 / MIN_BATCH_SIZE=4) = 4, plus 1 for the floor call = 5.
        expected_depth = int(math.log2(64 // vi.MIN_BATCH_SIZE)) + 1
        assert client.call_count == expected_depth


# ---------------------------------------------------------------------------
# Unit 5: token-cap precedence over rate-limit when both substrings appear
# ---------------------------------------------------------------------------


class TestEmbedBatchErrorPrecedence:
    def test_token_cap_wins_when_message_also_mentions_rate_limit(self, caplog):
        # Pathological message that contains BOTH substrings — token-cap
        # branch must take precedence to avoid a wasteful exponential
        # backoff on what is actually a sizing problem.
        msg = "rate limit hint: " + TOKEN_CAP_MESSAGE
        err = _FakeInvalidRequestError(msg)
        client = FakeVoyageClient(behaviors=[err, "ok", "ok"])
        texts = ["chunk"] * 128

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        assert len(result) == 128
        # No "rate limited" log line; only the splitting one.
        rate_logs = [r for r in caplog.records if "rate limited" in r.message.lower()]
        split_logs = [r for r in caplog.records if "splitting" in r.message.lower()]
        assert rate_logs == []
        assert len(split_logs) == 1


# ---------------------------------------------------------------------------
# Unit 6: mixed token-cap + rate-limit recovery
# ---------------------------------------------------------------------------


class TestEmbedBatchMixedErrors:
    def test_token_cap_then_rate_limit_then_success(self, caplog):
        token_err = _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        rate_err = _FakeInvalidRequestError(RATE_LIMIT_MESSAGE)
        # First call (size 128): token-cap → split to 64+64.
        # Second call (size 64): rate-limit → backoff → retry → ok.
        # Third call (size 64): ok.
        client = FakeVoyageClient(behaviors=[token_err, rate_err, "ok", "ok"])
        texts = ["chunk"] * 128

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        assert len(result) == 128
        assert client.call_count == 4
        rate_logs = [r for r in caplog.records if "rate limited" in r.message.lower()]
        split_logs = [r for r in caplog.records if "splitting" in r.message.lower()]
        assert len(split_logs) == 1
        assert len(rate_logs) == 1


# ---------------------------------------------------------------------------
# Unit 7: connection error path is unaffected by halving changes
# ---------------------------------------------------------------------------


class TestEmbedBatchConnectionRetryUnaffected:
    def test_connection_error_retries_then_succeeds(self):
        err = _FakeInvalidRequestError(CONNECTION_MESSAGE)
        client = FakeVoyageClient(behaviors=[err, "ok"])
        texts = ["chunk"] * 32

        result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")
        assert len(result) == 32
        assert client.call_count == 2
        # Same-size retry, no split.
        assert client.call_batch_sizes == [32, 32]


# ---------------------------------------------------------------------------
# Unit 8: non-recoverable error raises immediately
# ---------------------------------------------------------------------------


class TestEmbedBatchNonRecoverableError:
    def test_unknown_error_raises_without_retry_or_split(self):
        err = _FakeInvalidRequestError("authentication failed: invalid api key")
        client = FakeVoyageClient(behaviors=[err])
        texts = ["chunk"] * 128

        with pytest.raises(_FakeInvalidRequestError, match="authentication"):
            vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        assert client.call_count == 1


# ---------------------------------------------------------------------------
# Unit 9: edge cases — empty input, retry exhaustion, floor-success
# ---------------------------------------------------------------------------


class TestEmbedBatchEdgeCases:
    def test_empty_texts_returns_empty_without_client_call(self):
        client = FakeVoyageClient(behaviors=[])
        result = vi._embed_batch(client, [], vi.VOYAGE_DOC_MODEL, input_type="document")
        assert result == []
        assert client.call_count == 0

    def test_rate_limit_exhausts_max_retries_then_raises(self):
        err = _FakeInvalidRequestError(RATE_LIMIT_MESSAGE)
        # 1 initial attempt + 5 retries = 6 calls, all fail.
        client = FakeVoyageClient(behaviors=[err] * 6)
        texts = ["chunk"] * 32

        with pytest.raises(_FakeInvalidRequestError, match="Rate limit"):
            vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")
        # Initial attempt + max_retries (5) = 6 calls total.
        assert client.call_count == 6

    def test_halving_to_floor_then_succeeds_at_floor(self):
        """Locks `len(batch) > MIN_BATCH_SIZE` against accidental change to `>=`.

        With MIN_BATCH_SIZE=4 the chain 128 -> 64 -> 32 -> 16 -> 8 still splits
        at 8 (8 > 4 is True), producing two batches of size 4. Each then must
        be embedable -- a regression to `>= MIN_BATCH_SIZE` would refuse to
        split 8 and raise instead.
        """
        err = _FakeInvalidRequestError(TOKEN_CAP_MESSAGE)
        # Trace: 128 fails -> [64,64]. First 64 fails -> [32,32,64]. First 32
        # fails -> [16,16,32,64]. First 16 fails -> [8,8,16,32,64]. First 8
        # fails -> [4,4,8,16,32,64]. From here, the 4-sized batches and all
        # subsequent batches succeed.
        # Failures: 128, 64, 32, 16, 8 = 5 fails. Then ok for the rest.
        # Successful calls: 4, 4 (from the 8-split), 8, 16, 32, 64 = 6 ok plus
        # one more 8-split (the second 16 splits to 8+8 -- wait, only the
        # FIRST 16 fails; the second 16 came from a different parent and is
        # served by the next "ok" in our queue).
        # Simplest: queue 5 fails then enough oks to cover all surviving
        # leaves. With non-power-of-two recursion the count is messy; we just
        # provide a generous tail of "ok" and check the result count.
        client = FakeVoyageClient(behaviors=[err] * 5 + ["ok"] * 20)
        texts = ["chunk"] * 128

        result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")
        assert len(result) == 128
        # Smallest successful call must be at the floor.
        successful_sizes = client.call_batch_sizes[5:]
        assert min(successful_sizes) == vi.MIN_BATCH_SIZE


# ---------------------------------------------------------------------------
# Unit 10: token-cap detection accepts alternate stable phrasings
# ---------------------------------------------------------------------------


class TestEmbedBatchTokenCapAlternatePhrasing:
    def test_alternate_phrase_tokens_per_submitted_batch_triggers_split(self, caplog):
        # Voyage error message that drops "max allowed" but keeps the other
        # stable substring. Production code at video_intel.py:3236 must still
        # classify this as token-cap, not fall through to the bare raise.
        msg = "Request failed. The limit on tokens per submitted batch is 120000. Your batch has 130k tokens."
        err = _FakeInvalidRequestError(msg)
        client = FakeVoyageClient(behaviors=[err, "ok", "ok"])
        texts = ["chunk"] * 128

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            result = vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        assert len(result) == 128
        assert client.call_count == 3
        split_logs = [r for r in caplog.records if "splitting" in r.message.lower()]
        assert len(split_logs) == 1


# ---------------------------------------------------------------------------
# Unit 11: spend summary on partial-failure
# ---------------------------------------------------------------------------


class TestEmbedBatchSpendSummary:
    def test_partial_failure_logs_spend_summary(self, caplog):
        """When _embed_batch raises after at least one successful batch, the
        sunk Voyage spend must be visible in a single WARNING line so the
        operator can audit it before re-running."""
        # Two batches' worth of input. First batch succeeds; second batch hits
        # an unrecoverable auth error.
        auth_err = _FakeInvalidRequestError("authentication failed: invalid api key")
        client = FakeVoyageClient(behaviors=["ok", auth_err])
        # 256 chunks at VOYAGE_BATCH_SIZE=128 -> two batches.
        texts = ["chunk"] * 256

        with caplog.at_level(logging.WARNING, logger="video_intel"), pytest.raises(_FakeInvalidRequestError):
            vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        spend_logs = [r for r in caplog.records if "spend before failure" in r.message.lower()]
        assert len(spend_logs) == 1
        # Message must name the chunk count of the discarded partial result.
        assert "128 chunks" in spend_logs[0].message

    def test_immediate_failure_emits_no_spend_summary(self, caplog):
        """If the very first call fails before any embedding succeeds, the
        spend-summary line is suppressed -- no sunk cost to report."""
        auth_err = _FakeInvalidRequestError("authentication failed: invalid api key")
        client = FakeVoyageClient(behaviors=[auth_err])
        texts = ["chunk"] * 128

        with caplog.at_level(logging.WARNING, logger="video_intel"), pytest.raises(_FakeInvalidRequestError):
            vi._embed_batch(client, texts, vi.VOYAGE_DOC_MODEL, input_type="document")

        spend_logs = [r for r in caplog.records if "spend before failure" in r.message.lower()]
        assert spend_logs == []
