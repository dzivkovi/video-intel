"""Mandatory config snapshots before any corpus-mutating command.

Why this is code and not a documented habit: the manual routine FAILED. The
corpus went from 2026-07-22 to 2026-08-17 with no snapshot while the channel
list was actively edited (YC Shorts curation, skip_video_ids blocklists, a
headline_digest flag). The routine existed and was written down; it simply did
not run, because it depended on a human or an agent remembering. Per the
durability ladder in CLAUDE.md, a failure a future run can hit with nobody
noticing must become code.

The tests below lock the five behaviours that make this a backup rather than a
habit, plus a dispatch-parity check so a NEW corpus-mutating subcommand cannot
silently opt out of snapshotting.
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

import video_intel
from video_intel import (
    CONFIG_BACKUP_COMMANDS,
    CONFIG_BACKUP_DIR_NAME,
    backup_config_if_changed,
)

CFG_A = b"model: gemini-3.5-flash\nchannels:\n  - name: alpha\n"
CFG_B = b"model: gemini-3.5-flash\nchannels:\n  - name: alpha\n  - name: beta\n"


@pytest.fixture
def corpus(tmp_path):
    out = tmp_path / "corpus"
    out.mkdir()
    return out


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_bytes(CFG_A)
    return p


def _backups(corpus):
    d = corpus / CONFIG_BACKUP_DIR_NAME
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


class TestSnapshotIsWritten:
    def test_first_run_writes_dated_and_latest(self, corpus, cfg):
        written = backup_config_if_changed(corpus, config_path=cfg)
        assert written is not None
        names = _backups(corpus)
        assert "config.latest.yaml" in names
        assert any(re.fullmatch(r"config\.\d{4}-\d{2}-\d{2}\.yaml", n) for n in names), names
        assert (corpus / CONFIG_BACKUP_DIR_NAME / "config.latest.yaml").read_bytes() == CFG_A

    def test_backup_dir_is_created_when_absent(self, corpus, cfg):
        assert not (corpus / CONFIG_BACKUP_DIR_NAME).exists()
        backup_config_if_changed(corpus, config_path=cfg)
        assert (corpus / CONFIG_BACKUP_DIR_NAME).is_dir()


class TestContentComparedNotTimeBased:
    """Ten scans a day must not litter ten snapshots."""

    def test_unchanged_config_writes_nothing_second_time(self, corpus, cfg):
        backup_config_if_changed(corpus, config_path=cfg)
        before = _backups(corpus)
        assert backup_config_if_changed(corpus, config_path=cfg) is None
        assert _backups(corpus) == before

    def test_changed_config_writes_again(self, corpus, cfg):
        backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(CFG_B)
        assert backup_config_if_changed(corpus, config_path=cfg) is not None
        assert (corpus / CONFIG_BACKUP_DIR_NAME / "config.latest.yaml").read_bytes() == CFG_B


class TestDatedSnapshotsAreImmutable:
    """Only config.latest.yaml is ever overwritten."""

    def test_second_different_edit_same_day_is_suffixed_not_clobbered(self, corpus, cfg):
        first = backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(CFG_B)
        second = backup_config_if_changed(corpus, config_path=cfg)

        assert second is not None and second != first
        assert second.name.endswith("-2.yaml"), second.name
        assert first.read_bytes() == CFG_A, (
            "the morning's snapshot was clobbered by the afternoon edit; dated "
            "snapshots must be immutable or the history they exist to preserve is lost"
        )
        assert second.read_bytes() == CFG_B

    def test_third_edit_same_day_gets_next_suffix(self, corpus, cfg):
        backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(CFG_B)
        backup_config_if_changed(corpus, config_path=cfg)
        cfg.write_bytes(b"model: x\nchannels: []\n")
        third = backup_config_if_changed(corpus, config_path=cfg)
        assert third.name.endswith("-3.yaml"), third.name


class TestNeverAbortsTheCaller:
    """A backup failure must not block a scan; it must also never be silent."""

    def test_unreadable_config_returns_none_and_warns(self, corpus, tmp_path, caplog):
        missing = tmp_path / "does-not-exist.yaml"
        with caplog.at_level("WARNING"):
            assert backup_config_if_changed(corpus, config_path=missing) is None
        assert any("Config backup skipped" in r.message for r in caplog.records)

    def test_unwritable_output_dir_returns_none_and_warns(self, cfg, tmp_path, monkeypatch, caplog):
        target = tmp_path / "corpus2"
        target.mkdir()

        def boom(*a, **kw):
            raise OSError("Incorrect function (os error 1)")

        monkeypatch.setattr(pathlib.Path, "mkdir", boom)
        with caplog.at_level("WARNING"):
            assert backup_config_if_changed(target, config_path=cfg) is None
        assert any("Config backup FAILED" in r.message for r in caplog.records)

    def test_no_resolved_path_warns_instead_of_silently_skipping(self, corpus, monkeypatch, caplog):
        """Env-var resolution names a directory, not a config file."""
        monkeypatch.setattr(video_intel, "_LAST_RESOLVED_PATH", None)
        with caplog.at_level("WARNING"):
            assert backup_config_if_changed(corpus) is None
        assert any("not a file" in r.message for r in caplog.records), (
            "a backup that stops backing up must say so; silence here is exactly "
            "how the 2026-07-22 -> 2026-08-17 gap went unnoticed"
        )

    def test_unreadable_latest_writes_fresh_snapshot_rather_than_assuming_unchanged(
        self, corpus, cfg, monkeypatch, caplog
    ):
        backup_config_if_changed(corpus, config_path=cfg)
        latest = corpus / CONFIG_BACKUP_DIR_NAME / "config.latest.yaml"
        real_read = pathlib.Path.read_bytes

        def flaky(self, *a, **kw):
            if self == latest:
                raise OSError("transient cloud-mount read error")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "read_bytes", flaky)
        with caplog.at_level("WARNING"):
            # Config is byte-identical, but `latest` cannot be read to prove it.
            result = backup_config_if_changed(corpus, config_path=cfg)
        assert result is not None, (
            "an unreadable latest is not proof the config is unchanged; assuming "
            "safety there is how a real edit would go unsnapshotted"
        )


class TestResolvedPathIsTracked:
    def test_load_config_records_the_plugin_local_path(self, tmp_path, monkeypatch):
        skill = tmp_path / "skill"
        skill.mkdir()
        (skill / "config.yaml").write_text("output_dir: /tmp/x\nchannels: []\n", encoding="utf-8")
        monkeypatch.setattr(video_intel, "SKILL_DIR", skill)
        video_intel.load_config()
        assert skill / "config.yaml" == video_intel._LAST_RESOLVED_PATH


class TestEveryMutatingCommandSnapshots:
    """Dispatch parity: a new corpus-mutating subcommand cannot silently opt out.

    The command list is derived from the live dispatch table in main() rather
    than restated here, so the test and the code cannot agree by construction.
    Any command that is neither in CONFIG_BACKUP_COMMANDS nor in the explicit
    read-only allowlist fails this test until someone classifies it.
    """

    READ_ONLY = frozenset({"search", "nugget", "status", "briefings", "profile"})

    def test_all_dispatch_commands_are_classified(self):
        src = inspect.getsource(video_intel.main)
        dispatched = set(re.findall(r'args\.command == "([^"]+)"', src))
        assert dispatched, "could not parse the dispatch table"
        unclassified = dispatched - CONFIG_BACKUP_COMMANDS - self.READ_ONLY
        assert not unclassified, (
            f"subcommand(s) {sorted(unclassified)} are neither in "
            "CONFIG_BACKUP_COMMANDS nor the read-only allowlist. Classify them: "
            "if the command can mutate the corpus it MUST snapshot the config first."
        )

    def test_backup_set_contains_no_phantom_commands(self):
        src = inspect.getsource(video_intel.main)
        dispatched = set(re.findall(r'args\.command == "([^"]+)"', src))
        phantom = CONFIG_BACKUP_COMMANDS - dispatched
        assert not phantom, f"CONFIG_BACKUP_COMMANDS names non-existent command(s): {sorted(phantom)}"

    def test_scan_is_in_the_set(self):
        assert "scan" in CONFIG_BACKUP_COMMANDS


class TestScanSnapshotsBeforeMutating:
    """The snapshot must precede the first fetch, not follow it.

    A backup taken after a scan has already mutated the corpus documents the
    wrong state - it records the config that produced the NEXT run's changes,
    not this one's.
    """

    def test_backup_call_precedes_first_fetch_in_cmd_scan(self):
        src = inspect.getsource(video_intel.cmd_scan)
        backup_at = src.find("backup_config_if_changed(")
        assert backup_at != -1, "cmd_scan must snapshot the config itself"
        fetch_positions = [
            src.find(name)
            for name in ("fetch_selective_videos", "fetch_channel_videos", "enrich_with_durations")
            if src.find(name) != -1
        ]
        assert fetch_positions, "could not locate any fetch call in cmd_scan"
        assert backup_at < min(fetch_positions), (
            "backup_config_if_changed must run BEFORE cmd_scan's first fetch/mutation"
        )
