"""Tests for scripts/remediation_restore.py: the corpus lock at its entry point (issue #303).

A restore rewrites transcripts, mind maps, concepts and metas, so it takes the
corpus lock unless `--dry-run`. The holder is another process (the driver's
`hold` mode), and the corpus is resolved through a patched `load_config` that
names a scratch folder under `tmp_path`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import remediation_restore as rr  # noqa: E402
from test_corpus_lock_cli import Holder, run_waiting_writer  # noqa: E402

import corpus_lock  # noqa: E402


def _run_restore(monkeypatch, corpus: Path, *argv: str) -> int:
    monkeypatch.setattr(rr.vi, "load_config", lambda: {"output_dir": str(corpus)})
    monkeypatch.setattr(sys, "argv", ["remediation_restore.py", *argv])
    return rr.main()


@pytest.fixture
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    return root


class TestCorpusLock:
    def test_remediation_restore_refuses_while_the_corpus_is_held_without_reading_or_paying(
        self, corpus, tmp_path, monkeypatch, corpus_lock_state_dir
    ):
        reads: list[tuple] = []
        monkeypatch.setattr(rr, "find_regressions", lambda *a: reads.append(a) or [])
        monkeypatch.setattr(corpus_lock, "DEFAULT_WAIT", False)
        holder = Holder(corpus_lock_state_dir, tmp_path / "sync", corpus)
        try:
            with pytest.raises(SystemExit) as exc:
                _run_restore(monkeypatch, corpus)
        finally:
            holder.release()
        assert exc.value.code == corpus_lock.EXIT_CORPUS_BUSY
        assert reads == []

    def test_remediation_restore_dry_run_takes_no_lock(self, corpus, tmp_path, monkeypatch, corpus_lock_state_dir):
        reads: list[tuple] = []
        monkeypatch.setattr(rr, "find_regressions", lambda *a: reads.append(a) or [])
        monkeypatch.setattr(corpus_lock, "DEFAULT_WAIT", False)
        holder = Holder(corpus_lock_state_dir, tmp_path / "sync", corpus)
        try:
            code = _run_restore(monkeypatch, corpus, "--dry-run")
        finally:
            holder.release()
        assert code == 0
        assert len(reads) == 1

    def test_remediation_restore_normal_run_takes_and_releases_the_lock(self, corpus, monkeypatch):
        depths: list[int] = []
        real = rr.find_regressions

        def _spy(output_dir):
            depths.append(corpus_lock.held_depth(output_dir))
            return real(output_dir)

        monkeypatch.setattr(rr, "find_regressions", _spy)
        assert _run_restore(monkeypatch, corpus) == 0
        assert depths == [1]
        assert corpus_lock.held_depth(corpus) == 0

    def test_remediation_restore_exits_one_when_the_corpus_changed_while_it_waited(
        self, corpus, tmp_path, monkeypatch, corpus_lock_state_dir
    ):
        # Arrange: the config names another corpus by the time the lock frees.
        other = tmp_path / "other"
        other.mkdir()
        reads: list[tuple] = []
        monkeypatch.setattr(rr, "find_regressions", lambda *a: reads.append(a) or [])
        configs = [{"output_dir": str(corpus)}]
        loads: list[int] = []
        monkeypatch.setattr(rr.vi, "load_config", lambda: loads.append(1) or dict(configs[-1]))
        monkeypatch.setattr(sys, "argv", ["remediation_restore.py"])

        # Act
        outcome = run_waiting_writer(
            monkeypatch,
            corpus_lock_state_dir,
            tmp_path / "sync",
            corpus,
            rr.main,
            lambda: configs.append({"output_dir": str(other)}),
        )

        # Assert
        error = outcome.get("error")
        assert isinstance(error, SystemExit) and error.code == 1, outcome
        assert len(loads) == 2, "the config is re-read after the wait"
        assert reads == []
        assert corpus_lock.held_depth(corpus) == 0

    def test_remediation_restore_resolves_the_corpus_without_creating_it_before_the_lock(
        self, tmp_path, monkeypatch, corpus_lock_state_dir
    ):
        # Arrange: the corpus folder does not exist and another process holds its lock.
        corpus = tmp_path / "not-yet"
        monkeypatch.setattr(corpus_lock, "DEFAULT_WAIT", False)
        holder = Holder(corpus_lock_state_dir, tmp_path / "sync", corpus)

        # Act
        try:
            with pytest.raises(SystemExit) as exc:
                _run_restore(monkeypatch, corpus)
        finally:
            holder.release()

        # Assert
        assert exc.value.code == corpus_lock.EXIT_CORPUS_BUSY
        assert not corpus.exists(), "nothing, not even the corpus folder, is created before the lock"
