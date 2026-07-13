"""Tests for scripts/register_obsidian_vault.py.

The registry mutation is a pure function over a dict (register_vault), so the
add/idempotence/open-exclusivity/normalization contract is testable without a
real Obsidian or registry file. The IO helpers (atomic write, malformed-JSON
handling, per-platform paths, process check) are exercised with tmp files and
monkeypatches.
"""

from __future__ import annotations

import json

import pytest

from scripts.register_obsidian_vault import (
    _norm,
    _vault_id,
    load_registry,
    obsidian_running,
    register_vault,
    registry_path,
    write_registry_atomic,
)


class TestRegisterVault:
    def test_new_folder_is_added(self, tmp_path):
        cfg: dict = {"vaults": {}}
        vid, was_new = register_vault(cfg, tmp_path, make_open=False)
        assert was_new is True
        assert cfg["vaults"][vid]["path"] == str(tmp_path)

    def test_re_registering_same_path_is_idempotent(self, tmp_path):
        cfg: dict = {"vaults": {}}
        vid1, new1 = register_vault(cfg, tmp_path, make_open=False)
        vid2, new2 = register_vault(cfg, tmp_path, make_open=False)
        assert (vid1, new1) == (vid2, False) or (new1, new2) == (True, False)
        assert vid1 == vid2
        assert len(cfg["vaults"]) == 1  # no duplicate

    def test_equivalent_path_does_not_duplicate(self, tmp_path):
        # a `.`-laden but equivalent path must resolve to the same vault
        cfg: dict = {"vaults": {}}
        register_vault(cfg, tmp_path, make_open=False)
        equivalent = tmp_path / "." / ""
        _, was_new = register_vault(cfg, equivalent, make_open=False)
        assert was_new is False
        assert len(cfg["vaults"]) == 1

    def test_open_flag_is_exclusive(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        cfg: dict = {"vaults": {}}
        register_vault(cfg, a, make_open=True)
        register_vault(cfg, b, make_open=True)
        opened = [vid for vid, v in cfg["vaults"].items() if v.get("open")]
        assert len(opened) == 1
        assert cfg["vaults"][opened[0]]["path"] == str(b)

    def test_id_is_stable_across_runs(self, tmp_path):
        # derived from the normalized path, not the clock, so it never drifts
        assert _vault_id(tmp_path) == _vault_id(tmp_path)

    def test_creates_vaults_key_when_absent(self, tmp_path):
        cfg: dict = {}
        register_vault(cfg, tmp_path, make_open=False)
        assert tmp_path.name in str(cfg["vaults"])


class TestNorm:
    def test_collapses_redundant_segments(self, tmp_path):
        assert _norm(tmp_path / "a" / ".." / "b") == _norm(tmp_path / "b")


class TestRegistryIO:
    def test_atomic_write_round_trips(self, tmp_path):
        reg = tmp_path / "obsidian.json"
        reg.write_text("{}", encoding="utf-8")
        write_registry_atomic(reg, {"vaults": {"x": {"path": "/p"}}})
        assert json.loads(reg.read_text(encoding="utf-8"))["vaults"]["x"]["path"] == "/p"
        # no leftover temp files beside it
        assert list(tmp_path.glob("*.tmp-*")) == []

    def test_malformed_registry_exits_cleanly(self, tmp_path):
        reg = tmp_path / "obsidian.json"
        reg.write_text("{ not json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            load_registry(reg)
        assert "not valid JSON" in str(exc.value)


class TestPlatform:
    @pytest.mark.parametrize(
        "system,needle",
        [("Windows", "obsidian"), ("Darwin", "Application Support"), ("Linux", ".config")],
    )
    def test_registry_path_per_platform(self, monkeypatch, system, needle):
        monkeypatch.setattr("scripts.register_obsidian_vault.platform.system", lambda: system)
        assert needle.lower() in str(registry_path()).lower()

    def test_running_check_reads_process_list(self, monkeypatch):
        monkeypatch.setattr("scripts.register_obsidian_vault.platform.system", lambda: "Linux")

        class _R:
            returncode = 0

        monkeypatch.setattr("scripts.register_obsidian_vault.subprocess.run", lambda *a, **k: _R())
        assert obsidian_running() is True

    def test_running_check_is_false_when_probe_fails(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("no pgrep")

        monkeypatch.setattr("scripts.register_obsidian_vault.subprocess.run", boom)
        assert obsidian_running() is False
