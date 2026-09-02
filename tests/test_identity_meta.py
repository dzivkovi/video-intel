"""Tests for transcript-meta identity stamping + repair-metas backfill (issue #66).

The single-shot and captions transcript writers used to persist a meta with no
``video_id``, so when the transcript loop was the first writer (inverted ordering)
it left an identity-less meta that ``_load_video_id_index`` skips - breaking
idempotency (the video is re-transcribed every scan). These cover the prevention
(stamp identity on write) and the backfill (reconstruct from the .transcript.md
header for metas already on disk).
"""

import json
import types as _types

from youtube_captions import CaptionsResult

import video_intel as vi

_VALID_PAYLOAD = {
    "transcripts": [{"start": "00:00", "voice": 1, "text": "hi"}],
    "screen_content": [],
    "speakers": [{"voice": 1, "name": "A"}],
}


def _resp(prompt_tokens):
    return _types.SimpleNamespace(
        usage_metadata=_types.SimpleNamespace(
            prompt_token_count=prompt_tokens,
            cached_content_token_count=0,
            thoughts_token_count=0,
            candidates_token_count=10,
            total_token_count=prompt_tokens + 10,
        )
    )


def _video():
    return {
        "video_id": "vid123",
        "url": "https://www.youtube.com/watch?v=vid123",
        "title": "Test Title",
        "published": "2026-06-13",
    }


def _run(tmp_path, source="gemini", **kw):
    prefix = "2026-06-13-test"
    chan = tmp_path / "mychannel"
    chan.mkdir(exist_ok=True)
    res = vi.process_transcript(
        object(),
        None,
        _video(),
        "p",
        "stub-model",
        chan,
        prefix,
        transcript_source=source,
        media_resolution="LOW",
        **kw,
    )
    return res, chan / f"{prefix}.meta.json"


# ---------------------------------------------------------------------------
# Prevention: every transcript meta carries identity
# ---------------------------------------------------------------------------


class TestIdentityStamping:
    def test_gemini_success_stamps_identity(self, tmp_path, monkeypatch):
        def fake(client, types, media_uri, prompt, model, response_json=False, **kw):
            kw["on_response"](_resp(5000))
            return json.dumps(_VALID_PAYLOAD)

        monkeypatch.setattr(vi, "call_gemini", fake)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda t, m: None)
        (_, status), mpath = _run(tmp_path)
        assert status == "done"
        meta = json.loads(mpath.read_text())
        assert meta["video_id"] == "vid123"
        assert meta["video_url"] == "https://www.youtube.com/watch?v=vid123"
        assert meta["channel"] == "mychannel"
        assert meta["title"] == "Test Title"
        assert meta["published"] == "2026-06-13"

    def test_captions_failover_stamps_identity(self, tmp_path, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("token cap")

        monkeypatch.setattr(vi, "call_gemini", boom)
        monkeypatch.setattr(vi, "_make_thinking_config_for_transcript", lambda t, m: None)
        monkeypatch.setattr(vi, "fetch_english_captions", lambda vid: CaptionsResult([(0.0, "hi")], True, "en"))
        (_, status), mpath = _run(tmp_path, source="auto")
        assert "captions" in status
        meta = json.loads(mpath.read_text())
        assert meta["video_id"] == "vid123"
        assert meta["channel"] == "mychannel"
        assert meta["transcript_source"] == "youtube_captions"


# ---------------------------------------------------------------------------
# Header parser
# ---------------------------------------------------------------------------

_HEADER = (
    "# Transcript: Some Title\n\n"
    "**Source:** https://www.youtube.com/watch?v=abcDEF12345\n"
    "**Published:** 2026-06-01\n\n---\n"
)


class TestIdentityFromHeader:
    def test_parses_full_header(self, tmp_path):
        t = tmp_path / "x.transcript.md"
        t.write_text(_HEADER, encoding="utf-8")
        out = vi._identity_from_transcript_header(t)
        assert out["video_id"] == "abcDEF12345"
        assert out["video_url"] == "https://www.youtube.com/watch?v=abcDEF12345"
        assert out["title"] == "Some Title"
        assert out["published"] == "2026-06-01"
        assert out["channel"] == tmp_path.name

    def test_parses_youtu_be_short_url(self, tmp_path):
        t = tmp_path / "x.transcript.md"
        t.write_text("# Transcript: T\n**Source:** https://youtu.be/abcDEF12345\n", encoding="utf-8")
        assert vi._identity_from_transcript_header(t)["video_id"] == "abcDEF12345"

    def test_no_source_returns_none(self, tmp_path):
        t = tmp_path / "x.transcript.md"
        t.write_text("# Transcript: No source here\n", encoding="utf-8")
        assert vi._identity_from_transcript_header(t) is None

    def test_rejects_overlong_id_instead_of_truncating(self, tmp_path):
        # 16 id-chars after v= is not a valid 11-char id; the right-boundary
        # lookahead must reject it, never truncate to a wrong id (#66 review).
        t = tmp_path / "x.transcript.md"
        t.write_text("**Source:** https://www.youtube.com/watch?v=ABCDEFGHIJK12345\n", encoding="utf-8")
        assert vi._identity_from_transcript_header(t) is None


class TestIdentityFieldsDropFalsy:
    def test_drops_none_and_empty_never_downgrades(self, tmp_path):
        # Local-file flows can pass url="" / title=None; those must be dropped so a
        # re-stamp never clobbers a previously-good value (ce-data-integrity #66).
        out = vi._transcript_identity_fields(
            {"video_id": "vid123", "url": "", "title": None, "published": None}, tmp_path / "chan"
        )
        assert out == {"video_id": "vid123", "channel": "chan"}

    def test_keeps_truthy(self, tmp_path):
        out = vi._transcript_identity_fields(
            {"video_id": "v", "url": "u", "title": "t", "published": "2026-01-01"}, tmp_path / "c"
        )
        assert out == {"video_url": "u", "video_id": "v", "channel": "c", "title": "t", "published": "2026-01-01"}


# ---------------------------------------------------------------------------
# Backfill command
# ---------------------------------------------------------------------------


def _identity_less(chan, prefix="v"):
    (chan / f"{prefix}.meta.json").write_text(json.dumps({"transcript_status": "complete"}), encoding="utf-8")
    (chan / f"{prefix}.transcript.md").write_text(
        "# Transcript: T\n**Source:** https://youtu.be/abcDEF12345\n**Published:** 2026-06-01\n",
        encoding="utf-8",
    )


class TestRepairMetas:
    def _chan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "resolve_output_dir", lambda cfg: tmp_path)
        chan = tmp_path / "chan"
        chan.mkdir()
        return chan

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        chan = self._chan(tmp_path, monkeypatch)
        _identity_less(chan)
        vi.cmd_repair_metas(_types.SimpleNamespace(channel=None, apply=False), {})
        assert "video_id" not in json.loads((chan / "v.meta.json").read_text())

    def test_apply_backfills_and_preserves(self, tmp_path, monkeypatch):
        chan = self._chan(tmp_path, monkeypatch)
        _identity_less(chan)
        vi.cmd_repair_metas(_types.SimpleNamespace(channel=None, apply=True), {})
        meta = json.loads((chan / "v.meta.json").read_text())
        assert meta["video_id"] == "abcDEF12345"
        assert meta["channel"] == "chan"
        assert meta["transcript_status"] == "complete"  # existing field preserved

    def test_skips_meta_that_has_identity(self, tmp_path, monkeypatch):
        chan = self._chan(tmp_path, monkeypatch)
        (chan / "v.meta.json").write_text(json.dumps({"video_id": "keepme"}), encoding="utf-8")
        vi.cmd_repair_metas(_types.SimpleNamespace(channel=None, apply=True), {})
        assert json.loads((chan / "v.meta.json").read_text())["video_id"] == "keepme"

    def test_non_ascii_meta_does_not_crash(self, tmp_path, monkeypatch):
        # A Cyrillic-titled meta must not crash the walk via cp1252 decode on
        # Windows (the encoding="utf-8" fix; ce-correctness #66).
        chan = self._chan(tmp_path, monkeypatch)
        (chan / "v.meta.json").write_text(
            json.dumps({"transcript_status": "complete", "title": "Привет мир"}, ensure_ascii=False),
            encoding="utf-8",
        )
        (chan / "v.transcript.md").write_text(
            "# Transcript: Привет\n**Source:** https://youtu.be/abcDEF12345\n", encoding="utf-8"
        )
        vi.cmd_repair_metas(_types.SimpleNamespace(channel=None, apply=True), {})
        assert json.loads((chan / "v.meta.json").read_text(encoding="utf-8"))["video_id"] == "abcDEF12345"

    def test_missing_transcript_is_unrepairable_not_crash(self, tmp_path, monkeypatch):
        chan = self._chan(tmp_path, monkeypatch)
        (chan / "v.meta.json").write_text(json.dumps({"transcript_status": "complete"}), encoding="utf-8")
        # no sibling v.transcript.md
        vi.cmd_repair_metas(_types.SimpleNamespace(channel=None, apply=True), {})
        assert "video_id" not in json.loads((chan / "v.meta.json").read_text(encoding="utf-8"))
