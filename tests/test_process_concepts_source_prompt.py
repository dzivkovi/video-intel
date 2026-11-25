"""Issue #315: `process` names the prompt that produced the mind map it reads.

`concepts.json` carries `source_prompt`, the provenance of the mind map the
concepts were extracted from. `process --url` used to record a name derived in
the command and `process --file` the config/CLI name, so when the mind map on
disk was written with prompt A and this run resolves prompt B (config changed,
or the mind-map step was skipped as already done), the concepts named a prompt
that never produced their source. `concepts_source_prompt` is now the one
definition [core: one-definition]: the meta's recorded `prompt`, read from the
writer's own meta path after the mind-map step [core: writer's-path], else the
resolved name.

The tests drive the real `cmd_process` with only the Gemini boundary
(`call_gemini`, `call_gemini_text`, `upload_local_video`), the YouTube lookups
and ffprobe stubbed, and read the persisted `concepts.json` [core: real-caller].
"""

from __future__ import annotations

import argparse
import ast
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import video_intel as vi

SOURCE_PATH = Path(vi.__file__)
CHANNEL = "chan"
VIDEO_ID = "abcdefghijk"
PROMPT_A = "mindmap-light"
PROMPT_B = "mindmap-heavy"
MINDMAP = "# Mind map\n\n## Branch [00:01:00]\n- leaf [00:01:05]\n"
TRANSCRIPT = "# Transcript\n\n[00:00:01] Speaker: hello there.\n"
URL_VIDEO = {"video_id": VIDEO_ID, "title": "A Talk", "published": "2026-08-12"}
FILE_PREFIX = "talk"


def _torn_bytes() -> bytes:
    """A meta whose Cyrillic title is cut in the middle of a two-byte character."""
    full = json.dumps({"video_id": VIDEO_ID, "title": "Привет мир"}, ensure_ascii=False).encode()
    return full[: full.index("и".encode()) + 1]


@pytest.fixture(autouse=True)
def _clean_caches():
    vi._invalidate_video_id_cache()
    vi._DAMAGED_META_WARNED.clear()
    yield
    vi._invalidate_video_id_cache()
    vi._DAMAGED_META_WARNED.clear()


@pytest.fixture
def gemini(monkeypatch):
    """Stub the paid and network boundaries; record what Gemini was asked."""
    calls: list[str] = []

    def fake_text(_client, _types, text, _model, **_kw):
        if "# TRANSCRIPT TO PROCESS" in text:
            calls.append("mindmap")
            return MINDMAP
        calls.append("concepts")
        return json.dumps({"concepts": [{"concept_id": "ai.example", "preferred_label": "Example"}]})

    def no_video_call(*_a, **_k):
        raise AssertionError("no video-bearing Gemini call is expected in these scenarios")

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, MagicMock()))
    monkeypatch.setattr(vi, "create_client", lambda _key: MagicMock())
    monkeypatch.setattr(vi, "call_gemini_text", fake_text)
    monkeypatch.setattr(vi, "call_gemini", no_video_call)
    monkeypatch.setattr(vi, "upload_local_video", lambda _c, _p: "files/stub-upload")
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _v: 600)
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _v: False)
    monkeypatch.setattr(vi, "_local_file_duration_seconds", lambda _p: 60)
    return calls


def _config(output_dir: Path, mindmap_source: str) -> dict:
    return {
        "output_dir": str(output_dir),
        "default_prompt": PROMPT_B,
        "channels": [
            {"name": CHANNEL, "url": f"https://youtube.com/@{CHANNEL}", "mindmap_source": mindmap_source},
        ],
    }


def _args(**kw) -> argparse.Namespace:
    base = {
        "url": None,
        "file": None,
        "channel": CHANNEL,
        "title": None,
        "date": None,
        "video_id": None,
        "start": None,
        "end": None,
        "force": False,
        "prompt": None,
        "model": None,
        "media_resolution": "low",
        "chunk_minutes": None,
        "transcript_source": None,
        "topic": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _run(args, config) -> int:
    try:
        vi.cmd_process(args, config)
    except SystemExit as exc:
        return exc.code or 0
    return 0


def _seed(channel_dir: Path, prefix: str, *, meta: dict | bytes, mindmap: bool) -> Path:
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / f"{prefix}.transcript.md").write_text(TRANSCRIPT, encoding="utf-8")
    if mindmap:
        (channel_dir / f"{prefix}.mindmap.md").write_text(MINDMAP, encoding="utf-8")
    meta_path = channel_dir / f"{prefix}.meta.json"
    if isinstance(meta, bytes):
        meta_path.write_bytes(meta)
    else:
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta_path


def _concepts(channel_dir: Path, prefix: str) -> dict:
    return json.loads((channel_dir / f"{prefix}.concepts.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# process --url
# ---------------------------------------------------------------------------


def _url_args(**kw):
    return _args(url=f"https://www.youtube.com/watch?v={VIDEO_ID}", title="A Talk", date="2026-08-12", **kw)


def _url_meta(**extra) -> dict:
    return {
        "video_id": VIDEO_ID,
        "video_url": f"https://www.youtube.com/watch?v={VIDEO_ID}",
        "title": "A Talk",
        "published": "2026-08-12",
        "channel": CHANNEL,
        "modes_completed": ["scan", "transcript"],
        "transcript_status": "complete",
        **extra,
    }


class TestProcessUrl:
    def test_process_url_when_mindmap_on_disk_from_prompt_a_records_a(self, gemini, tmp_path):
        # Arrange
        prefix = vi.video_file_prefix(URL_VIDEO)
        channel_dir = tmp_path / CHANNEL
        _seed(channel_dir, prefix, meta=_url_meta(prompt=PROMPT_A), mindmap=True)

        # Act
        rc = _run(_url_args(), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert gemini == ["concepts"], "the mind-map step must have been skipped as already done"
        assert _concepts(channel_dir, prefix)["source_prompt"] == PROMPT_A

    def test_process_url_when_mindmap_built_this_run_records_the_writers_prompt(self, gemini, tmp_path):
        # Arrange
        prefix = vi.video_file_prefix(URL_VIDEO)
        channel_dir = tmp_path / CHANNEL
        meta_path = _seed(channel_dir, prefix, meta=_url_meta(), mindmap=False)

        # Act
        rc = _run(_url_args(), _config(tmp_path, "auto"))

        # Assert
        assert rc == 0
        assert gemini == ["mindmap", "concepts"]
        recorded = json.loads(meta_path.read_text(encoding="utf-8"))["prompt"]
        assert _concepts(channel_dir, prefix)["source_prompt"] == recorded

    def test_process_url_when_meta_records_no_prompt_falls_back_to_resolved_name(self, gemini, tmp_path):
        # Arrange
        prefix = vi.video_file_prefix(URL_VIDEO)
        channel_dir = tmp_path / CHANNEL
        _seed(channel_dir, prefix, meta=_url_meta(), mindmap=True)

        # Act
        rc = _run(_url_args(), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert _concepts(channel_dir, prefix)["source_prompt"] == PROMPT_B

    def test_process_url_when_no_meta_exists_falls_back_without_a_damaged_warning(self, gemini, tmp_path, caplog):
        # Arrange: a legacy pair of artifacts with no meta.json at all.
        prefix = vi.video_file_prefix(URL_VIDEO)
        channel_dir = tmp_path / CHANNEL
        meta_path = _seed(channel_dir, prefix, meta={}, mindmap=True)
        meta_path.unlink()

        # Act
        with caplog.at_level(logging.WARNING, logger=vi.log.name):
            rc = _run(_url_args(), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert _concepts(channel_dir, prefix)["source_prompt"] == PROMPT_B
        assert not [r for r in caplog.records if "Damaged meta.json" in r.getMessage()]

    def test_process_url_when_meta_is_torn_writes_concepts_with_fallback_and_one_warning(
        self, gemini, tmp_path, caplog
    ):
        # Arrange
        prefix = vi.video_file_prefix(URL_VIDEO)
        channel_dir = tmp_path / CHANNEL
        meta_path = _seed(channel_dir, prefix, meta=_torn_bytes(), mindmap=True)

        # Act
        with caplog.at_level(logging.WARNING, logger=vi.log.name):
            rc = _run(_url_args(), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert _concepts(channel_dir, prefix)["source_prompt"] == PROMPT_B
        damaged = [r for r in caplog.records if "Damaged meta.json" in r.getMessage()]
        assert len(damaged) == 1
        assert str(meta_path) in damaged[0].getMessage()


# ---------------------------------------------------------------------------
# process --file
# ---------------------------------------------------------------------------


def _file_meta(**extra) -> dict:
    return {
        "video_id": "vid001",
        "video_url": "",
        "title": "Talk",
        "published": "2026-08-12",
        "channel": CHANNEL,
        "modes_completed": ["identity", "scan", "transcript"],
        "transcript_status": "complete",
        **extra,
    }


def _seed_file(tmp_path: Path, *, meta: dict | bytes, mindmap: bool) -> tuple[Path, Path, Path]:
    channel_dir = tmp_path / CHANNEL
    meta_path = _seed(channel_dir, FILE_PREFIX, meta=meta, mindmap=mindmap)
    mp4 = channel_dir / f"{FILE_PREFIX}.mp4"
    mp4.write_bytes(b"fake mp4 bytes")
    return mp4, channel_dir, meta_path


class TestProcessFile:
    def test_process_file_when_mindmap_on_disk_from_prompt_a_records_a(self, gemini, tmp_path):
        # Arrange
        mp4, channel_dir, _ = _seed_file(tmp_path, meta=_file_meta(prompt=PROMPT_A), mindmap=True)

        # Act
        rc = _run(_args(file=str(mp4)), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert gemini == ["concepts"], "the mind-map step must have been skipped as already done"
        assert _concepts(channel_dir, FILE_PREFIX)["source_prompt"] == PROMPT_A

    def test_process_file_when_mindmap_built_this_run_records_the_writers_prompt(self, gemini, tmp_path):
        # Arrange
        mp4, channel_dir, meta_path = _seed_file(tmp_path, meta=_file_meta(), mindmap=False)

        # Act
        rc = _run(_args(file=str(mp4)), _config(tmp_path, "auto"))

        # Assert
        assert rc == 0
        assert gemini == ["mindmap", "concepts"]
        recorded = json.loads(meta_path.read_text(encoding="utf-8"))["prompt"]
        assert _concepts(channel_dir, FILE_PREFIX)["source_prompt"] == recorded

    def test_process_file_when_only_transcript_needs_work_keeps_prompt_a_in_meta_and_concepts(
        self, gemini, tmp_path, monkeypatch
    ):
        """The pre-upload identity write used to stamp the config's prompt (B)
        over a meta whose mind map on disk came from A, whenever an upload was
        needed for the transcript alone."""
        # Arrange
        mp4, channel_dir, meta_path = _seed_file(
            tmp_path, meta=_file_meta(prompt=PROMPT_A, modes_completed=["identity", "scan"]), mindmap=True
        )
        (channel_dir / f"{FILE_PREFIX}.transcript.md").unlink()
        uploads: list[Path] = []
        monkeypatch.setattr(vi, "upload_local_video", lambda _c, p: uploads.append(p) or "files/stub-upload")
        transcript_json = json.dumps(
            {"transcripts": [{"timestamp": "00:00:01", "speaker": "Speaker", "text": "hello there."}]}
        )
        monkeypatch.setattr(vi, "call_gemini", lambda *_a, **_k: transcript_json)

        # Act
        rc = _run(_args(file=str(mp4)), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert uploads, "the transcript step needed work, so an upload must have happened"
        assert (channel_dir / f"{FILE_PREFIX}.transcript.md").exists()
        assert json.loads(meta_path.read_text(encoding="utf-8"))["prompt"] == PROMPT_A
        assert _concepts(channel_dir, FILE_PREFIX)["source_prompt"] == PROMPT_A

    def test_process_file_when_mindmap_and_transcript_need_work_records_the_writers_prompt(
        self, gemini, tmp_path, monkeypatch
    ):
        """Control for the identity-write rule: when the mind map IS written this
        run, its writer records the prompt and concepts name that same value."""
        # Arrange
        mp4, channel_dir, meta_path = _seed_file(
            tmp_path, meta=_file_meta(prompt=PROMPT_A, modes_completed=["identity"]), mindmap=False
        )
        (channel_dir / f"{FILE_PREFIX}.transcript.md").unlink()
        transcript_json = json.dumps(
            {"transcripts": [{"timestamp": "00:00:01", "speaker": "Speaker", "text": "hello there."}]}
        )
        monkeypatch.setattr(vi, "call_gemini", lambda *_a, **_k: transcript_json)

        # Act
        rc = _run(_args(file=str(mp4)), _config(tmp_path, "auto"))

        # Assert
        assert rc == 0
        recorded = json.loads(meta_path.read_text(encoding="utf-8"))["prompt"]
        assert recorded != PROMPT_A, "the mind-map writer must have recorded this run's prompt"
        assert _concepts(channel_dir, FILE_PREFIX)["source_prompt"] == recorded

    def test_process_file_when_meta_records_no_prompt_falls_back_to_resolved_name(self, gemini, tmp_path):
        # Arrange
        mp4, channel_dir, _ = _seed_file(tmp_path, meta=_file_meta(), mindmap=True)

        # Act
        rc = _run(_args(file=str(mp4)), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert _concepts(channel_dir, FILE_PREFIX)["source_prompt"] == PROMPT_B

    def test_process_file_when_meta_is_torn_at_step_three_writes_concepts_with_fallback_and_one_warning(
        self, gemini, tmp_path, caplog, monkeypatch
    ):
        """On `--file` a meta that is already torn when the run starts never
        reaches step 3 torn: its `modes_completed` is unreadable, so work is
        always needed and the identity write replaces it first. The state this
        guards is a meta torn between the mind-map step and the concepts read
        (a cloud-mount write landing mid-run), so the tear happens there, on
        the writer's path, through the step-2 bookkeeping lookup."""
        # Arrange
        mp4, channel_dir, meta_path = _seed_file(tmp_path, meta=_file_meta(prompt=PROMPT_A), mindmap=True)
        real_find = vi.find_mindmap_source

        def find_then_tear(channel_dir_arg, prefix):
            meta_path.write_bytes(_torn_bytes())
            return real_find(channel_dir_arg, prefix)

        monkeypatch.setattr(vi, "find_mindmap_source", find_then_tear)

        # Act
        with caplog.at_level(logging.WARNING, logger=vi.log.name):
            rc = _run(_args(file=str(mp4)), _config(tmp_path, "video"))

        # Assert
        assert rc == 0
        assert _concepts(channel_dir, FILE_PREFIX)["source_prompt"] == PROMPT_B
        damaged = [r for r in caplog.records if "Damaged meta.json" in r.getMessage()]
        assert len(damaged) == 1
        assert str(meta_path) in damaged[0].getMessage()


# ---------------------------------------------------------------------------
# The helper itself
# ---------------------------------------------------------------------------


class TestConceptsSourcePromptHelper:
    @pytest.mark.parametrize(
        ("meta", "fallback", "expected"),
        [
            ({"prompt": PROMPT_A}, PROMPT_B, PROMPT_A),
            ({"prompt": PROMPT_A}, None, PROMPT_A),
            ({}, PROMPT_B, PROMPT_B),
            ({}, None, None),
            ({"prompt": ""}, PROMPT_B, PROMPT_B),
            ({"prompt": 7}, PROMPT_B, PROMPT_B),
            (None, PROMPT_B, PROMPT_B),
        ],
    )
    def test_concepts_source_prompt_when_meta_varies_returns_record_else_fallback(self, meta, fallback, expected):
        assert vi.concepts_source_prompt(meta, fallback) == expected


# ---------------------------------------------------------------------------
# Drift guard: every process_concepts call takes the helper's result
# ---------------------------------------------------------------------------

HELPER = "concepts_source_prompt"
EXPECTED_CALLERS = {"cmd_scan", "cmd_concepts", "_cmd_process_url", "_cmd_process_impl"}


def _is_helper_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == HELPER


def _tuple_index(target: ast.AST, name: str) -> int | None:
    """Index of ``name`` in a for-loop target, looking through ``enumerate``'s ``(i, (...))`` shape."""
    if isinstance(target, ast.Tuple):
        for i, elt in enumerate(target.elts):
            if isinstance(elt, ast.Name) and elt.id == name:
                return i
        for elt in target.elts:
            found = _tuple_index(elt, name)
            if found is not None:
                return found
    return None


def _name_is_bound_from_helper(fn: ast.FunctionDef, name: str) -> bool:
    """True when ``name`` is assigned the helper's result directly, or is unpacked
    by a for loop from tuples whose element at that position is the helper's result
    (the scan and concepts loops carry it on a candidate tuple)."""
    positions = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            if not _is_helper_call(node.value):
                return False
            positions.add("assign")
        elif isinstance(node, ast.For):
            idx = _tuple_index(node.target, name)
            if idx is not None:
                positions.add(idx)
    if not positions:
        return False
    tuple_positions = {p for p in positions if p != "assign"}
    if not tuple_positions:
        return True
    appended = [
        node.args[0]
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and node.args
        and isinstance(node.args[0], ast.Tuple)
    ]
    return all(any(len(t.elts) > idx and _is_helper_call(t.elts[idx]) for t in appended) for idx in tuple_positions)


def _process_concepts_calls() -> list[tuple[str, ast.Call, ast.FunctionDef]]:
    tree = ast.parse(SOURCE_PATH.read_text(encoding="utf-8"))
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "process_concepts":
                out.append((fn.name, node, fn))
    return out


class TestOneDefinitionDriftGuard:
    def test_every_process_concepts_call_when_walked_takes_the_helpers_result(self):
        offenders = []
        for fn_name, call, fn in _process_concepts_calls():
            kw = {k.arg: k.value for k in call.keywords}
            value = kw.get("source_prompt")
            if value is None:
                offenders.append(f"{fn_name}: no source_prompt= argument")
            elif not (
                _is_helper_call(value) or (isinstance(value, ast.Name) and _name_is_bound_from_helper(fn, value.id))
            ):
                offenders.append(f"{fn_name}: source_prompt={ast.unparse(value)}")
        assert not offenders, f"process_concepts callers that bypass {HELPER}: {offenders}"

    def test_the_walk_when_run_finds_every_named_caller(self):
        """Non-vacuity: a walk that stops matching passes the check above trivially."""
        found = {fn_name for fn_name, _call, _fn in _process_concepts_calls()}
        assert found >= EXPECTED_CALLERS, f"the walk missed callers: {sorted(EXPECTED_CALLERS - found)}"

    def test_the_binding_check_when_given_a_private_read_rejects_it(self):
        """The guard must be able to fail: a private `meta.get("prompt")` is not the helper."""
        fn = ast.parse(
            "def f(meta):\n"
            "    p = meta.get('prompt')\n"
            "    process_concepts(source_prompt=p)\n"
            "    rows = []\n"
            "    rows.append((1, meta.get('prompt')))\n"
            "    for _x, q in rows:\n"
            "        process_concepts(source_prompt=q)\n"
        ).body[0]
        assert not _name_is_bound_from_helper(fn, "p")
        assert not _name_is_bound_from_helper(fn, "q")
