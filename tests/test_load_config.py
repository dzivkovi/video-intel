"""Tests for load_config()'s four-step resolution precedence.

Precedence (R5):
  1. SKILL_DIR/config.yaml  (plugin checkout, existing behavior)
  2. VIDEO_INTEL_OUTPUT_DIR  (env var, must be absolute)
  3. ~/.video-intel/config.yaml  (user-level minimal config)
  4. Hard error naming both overrides

Covers happy paths, precedence order, and the error modes that distinguish
the portability story from a stale env var silently redirecting scan (D3).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import video_intel as vi


def _write_config(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(content), encoding="utf-8")


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Redirect SKILL_DIR and user-config lookup into tmp_path.

    Tests construct config files under `skill_dir` or `user_home_config_dir`
    to exercise specific precedence steps. Env var starts unset.
    """
    skill_dir = tmp_path / "plugin"
    skill_dir.mkdir()
    user_config_dir = tmp_path / "home" / ".video-intel"

    monkeypatch.setattr(vi, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(vi, "_user_config_path", lambda: user_config_dir / "config.yaml")
    monkeypatch.delenv("VIDEO_INTEL_OUTPUT_DIR", raising=False)

    return {
        "skill_dir": skill_dir,
        "skill_config": skill_dir / "config.yaml",
        "user_config_dir": user_config_dir,
        "user_config": user_config_dir / "config.yaml",
    }


class TestStep1PluginConfig:
    def test_skill_dir_config_returns_parsed_contents(self, isolated_config, tmp_path):
        corpus = str(tmp_path / "corpus")
        _write_config(
            isolated_config["skill_config"],
            {"output_dir": corpus, "channels": [{"name": "x"}]},
        )
        config = vi.load_config()
        assert config["output_dir"] == corpus
        assert config["channels"] == [{"name": "x"}]

    def test_skill_dir_wins_over_env_var(self, isolated_config, monkeypatch, tmp_path):
        plugin_corpus = str(tmp_path / "plugin-corpus")
        env_corpus = str(tmp_path / "should-be-ignored")
        _write_config(isolated_config["skill_config"], {"output_dir": plugin_corpus})
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", env_corpus)
        config = vi.load_config()
        assert config["output_dir"] == plugin_corpus

    def test_skill_dir_wins_over_user_config(self, isolated_config, tmp_path):
        plugin_corpus = str(tmp_path / "plugin-corpus")
        user_corpus = str(tmp_path / "user-corpus")
        _write_config(isolated_config["skill_config"], {"output_dir": plugin_corpus})
        _write_config(isolated_config["user_config"], {"output_dir": user_corpus})
        config = vi.load_config()
        assert config["output_dir"] == plugin_corpus


class TestStep2EnvVar:
    def test_env_var_resolves_output_dir_when_plugin_config_absent(self, isolated_config, monkeypatch, tmp_path):
        corpus = str(tmp_path / "some-corpus")
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", corpus)
        config = vi.load_config()
        assert config == {"output_dir": corpus}

    def test_env_var_wins_over_user_config(self, isolated_config, monkeypatch, tmp_path):
        env_corpus = str(tmp_path / "env-corpus")
        user_corpus = str(tmp_path / "user-corpus")
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", env_corpus)
        _write_config(isolated_config["user_config"], {"output_dir": user_corpus})
        config = vi.load_config()
        assert config["output_dir"] == env_corpus

    def test_relative_env_var_exits_with_actionable_message(self, isolated_config, monkeypatch, caplog):
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", "relative/path")
        with pytest.raises(SystemExit) as exc:
            vi.load_config()
        assert exc.value.code == 1
        assert "absolute" in caplog.text.lower()
        assert "VIDEO_INTEL_OUTPUT_DIR" in caplog.text

    def test_empty_env_var_treated_as_unset(self, isolated_config, monkeypatch, tmp_path):
        user_corpus = str(tmp_path / "user-corpus")
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", "")
        _write_config(isolated_config["user_config"], {"output_dir": user_corpus})
        config = vi.load_config()
        assert config["output_dir"] == user_corpus

    def test_whitespace_env_var_treated_as_unset(self, isolated_config, monkeypatch, tmp_path):
        user_corpus = str(tmp_path / "user-corpus")
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", "   ")
        _write_config(isolated_config["user_config"], {"output_dir": user_corpus})
        config = vi.load_config()
        assert config["output_dir"] == user_corpus


class TestStep3UserConfig:
    def test_user_config_parsed_and_filtered(self, isolated_config, tmp_path):
        user_corpus = str(tmp_path / "user-corpus")
        user_cache = str(tmp_path / "user-cache")
        _write_config(
            isolated_config["user_config"],
            {"output_dir": user_corpus, "vector_db_dir": user_cache},
        )
        config = vi.load_config()
        assert config == {"output_dir": user_corpus, "vector_db_dir": user_cache}

    def test_user_config_extras_ignored_with_info_log(self, isolated_config, tmp_path, caplog):
        import logging

        user_corpus = str(tmp_path / "user-corpus")
        caplog.set_level(logging.INFO)
        _write_config(
            isolated_config["user_config"],
            {
                "output_dir": user_corpus,
                "model": "gemini-3-flash-preview",
                "channels": [{"name": "x"}],
            },
        )
        config = vi.load_config()
        assert config == {"output_dir": user_corpus}
        # Single INFO log naming the ignored keys
        ignored_logs = [r for r in caplog.records if "ignoring" in r.message.lower()]
        assert len(ignored_logs) == 1
        assert "model" in ignored_logs[0].message
        assert "channels" in ignored_logs[0].message

    def test_user_config_missing_output_dir_exits(self, isolated_config, tmp_path, caplog):
        only_cache = str(tmp_path / "only-cache")
        _write_config(isolated_config["user_config"], {"vector_db_dir": only_cache})
        with pytest.raises(SystemExit) as exc:
            vi.load_config()
        assert exc.value.code == 1
        assert "output_dir" in caplog.text
        assert ".video-intel" in caplog.text or str(isolated_config["user_config"]) in caplog.text

    def test_user_config_malformed_yaml_exits(self, isolated_config, caplog):
        isolated_config["user_config_dir"].mkdir(parents=True, exist_ok=True)
        isolated_config["user_config"].write_text("output_dir: /x\n\tbad: mixed indent\n broken: :::", encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            vi.load_config()
        assert exc.value.code == 1
        # Error message names the file
        assert ".video-intel" in caplog.text or "config.yaml" in caplog.text


class TestStep4TerminalError:
    def test_all_signals_absent_exits_with_both_override_names(self, isolated_config, caplog):
        # Nothing set: no skill_config, no env var, no user config
        with pytest.raises(SystemExit) as exc:
            vi.load_config()
        assert exc.value.code == 1
        assert "VIDEO_INTEL_OUTPUT_DIR" in caplog.text
        assert "~/.video-intel/config.yaml" in caplog.text


class TestInfoLogNamesSource:
    """KD7: one INFO log line naming the winning precedence source."""

    def test_info_log_on_plugin_config_path(self, isolated_config, tmp_path, caplog):
        import logging

        caplog.set_level(logging.INFO)
        _write_config(isolated_config["skill_config"], {"output_dir": str(tmp_path / "corpus")})
        vi.load_config()
        resolved_logs = [r for r in caplog.records if "Config resolved from" in r.message]
        assert len(resolved_logs) == 1
        assert "config.yaml" in resolved_logs[0].message

    def test_info_log_on_env_var_path(self, isolated_config, monkeypatch, tmp_path, caplog):
        import logging

        caplog.set_level(logging.INFO)
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", str(tmp_path / "env-corpus"))
        vi.load_config()
        resolved_logs = [r for r in caplog.records if "Config resolved from" in r.message]
        assert len(resolved_logs) == 1
        assert "VIDEO_INTEL_OUTPUT_DIR" in resolved_logs[0].message

    def test_info_log_on_user_config_path(self, isolated_config, tmp_path, caplog):
        import logging

        caplog.set_level(logging.INFO)
        _write_config(isolated_config["user_config"], {"output_dir": str(tmp_path / "user-corpus")})
        vi.load_config()
        resolved_logs = [r for r in caplog.records if "Config resolved from" in r.message]
        assert len(resolved_logs) == 1
        assert "~/.video-intel/config.yaml" in resolved_logs[0].message
