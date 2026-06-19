"""Tests for the pre-flight metadata filter (issue #70).

Skip videos that have not aired (scheduled premieres / live) or are non-public
BEFORE any Gemini call, so they never confabulate a prompt=0 stub.
"""

from unittest.mock import MagicMock

import video_intel as vi


class TestPreflightSkipReason:
    def test_upcoming_is_skipped(self):
        assert "not yet aired" in vi.preflight_skip_reason({"live_broadcast_content": "upcoming"})

    def test_live_is_skipped(self):
        assert "not yet aired" in vi.preflight_skip_reason({"live_broadcast_content": "live"})

    def test_private_is_skipped(self):
        assert "non-public" in vi.preflight_skip_reason({"privacy_status": "private"})

    def test_unlisted_is_skipped(self):
        assert "non-public" in vi.preflight_skip_reason({"privacy_status": "unlisted"})

    def test_public_aired_is_kept(self):
        assert vi.preflight_skip_reason({"live_broadcast_content": "none", "privacy_status": "public"}) is None

    def test_empty_status_fails_safe_to_keep(self):
        # Missing metadata is NOT a positive skip signal.
        assert vi.preflight_skip_reason({}) is None

    def test_none_values_fail_safe_to_keep(self):
        assert vi.preflight_skip_reason({"live_broadcast_content": None, "privacy_status": None}) is None

    def test_upcoming_takes_precedence_over_privacy(self):
        reason = vi.preflight_skip_reason({"live_broadcast_content": "upcoming", "privacy_status": "public"})
        assert "not yet aired" in reason


class TestFetchPreflightStatus:
    def _yt(self, items):
        yt = MagicMock()
        yt.videos.return_value.list.return_value.execute.return_value = {"items": items}
        return yt

    def test_parses_live_and_privacy(self):
        yt = self._yt(
            [
                {"id": "a", "snippet": {"liveBroadcastContent": "none"}, "status": {"privacyStatus": "public"}},
                {"id": "b", "snippet": {"liveBroadcastContent": "upcoming"}, "status": {"privacyStatus": "public"}},
            ]
        )
        result = vi.fetch_preflight_status(yt, ["a", "b"])
        assert result["a"] == {"live_broadcast_content": "none", "privacy_status": "public"}
        assert result["b"]["live_broadcast_content"] == "upcoming"

    def test_missing_id_maps_to_empty_dict(self):
        yt = self._yt([{"id": "a", "snippet": {"liveBroadcastContent": "none"}, "status": {"privacyStatus": "public"}}])
        result = vi.fetch_preflight_status(yt, ["a", "missing"])
        assert result["missing"] == {}  # deleted/gated -> fail-safe keep

    def test_empty_input_returns_empty(self):
        assert vi.fetch_preflight_status(MagicMock(), []) == {}

    def test_batches_over_fifty(self):
        # 120 ids -> 3 batches of <=50; assert the API was called 3 times.
        yt = MagicMock()
        yt.videos.return_value.list.return_value.execute.return_value = {"items": []}
        vi.fetch_preflight_status(yt, [f"v{i}" for i in range(120)])
        assert yt.videos.return_value.list.return_value.execute.call_count == 3

    def test_end_to_end_filter_via_reason(self):
        # The two functions compose: a fetched upcoming video yields a skip reason.
        yt = self._yt(
            [{"id": "prem", "snippet": {"liveBroadcastContent": "upcoming"}, "status": {"privacyStatus": "public"}}]
        )
        status = vi.fetch_preflight_status(yt, ["prem"])["prem"]
        assert vi.preflight_skip_reason(status) is not None
