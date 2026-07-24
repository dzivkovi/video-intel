"""Tests for require_channels_config() and its per-command wiring.

Curate commands that read config['channels'] must fail fast with an actionable
message when channels is absent - the failure mode R6a guards against.

Guard scope (per plan Decision batch):
  - Unconditional in cmd_scan, cmd_concepts, cmd_dedupe
  - Inside the args.channel branch of cmd_mindmap, cmd_transcript, cmd_process
  - NOT called in cmd_taxonomy_build, cmd_index, cmd_search, cmd_nugget, cmd_status,
    cmd_profile (issue #117: `profile show` is routed from the globally
    installable read-only search skill, which has no `channels:`)
"""

from __future__ import annotations

import pytest

import video_intel as vi


class TestRequireChannelsConfigHelper:
    def test_passes_silently_when_channels_present(self, caplog):
        vi.require_channels_config({"channels": [{"name": "x"}], "output_dir": "/y"})
        # No log or exit; helper is a no-op on success.
        assert not any("channels" in r.message.lower() for r in caplog.records)

    def test_exits_when_channels_missing(self, caplog):
        with pytest.raises(SystemExit) as exc:
            vi.require_channels_config({"output_dir": "/y"})
        assert exc.value.code == 1
        assert "channels" in caplog.text.lower()
        assert "plugin repo" in caplog.text.lower() or "VIDEO_INTEL_OUTPUT_DIR" in caplog.text

    def test_exits_when_channels_empty_list(self, caplog):
        with pytest.raises(SystemExit) as exc:
            vi.require_channels_config({"channels": [], "output_dir": "/y"})
        assert exc.value.code == 1
        assert "channels" in caplog.text.lower()

    def test_exits_when_channels_is_none(self, caplog):
        with pytest.raises(SystemExit) as exc:
            vi.require_channels_config({"channels": None, "output_dir": "/y"})
        assert exc.value.code == 1


class TestGuardWiring:
    """Each curate command that reads config['channels'] must call the guard
    at its entry point (before any work). Search-side commands must NOT.

    Strategy: monkeypatch require_channels_config to raise a sentinel error.
    Invoking each cmd_* with a minimal args and a config missing 'channels'
    should hit the sentinel (guard called) or NOT (guard not called).
    """

    @pytest.fixture
    def guard_sentinel(self, monkeypatch):
        """Replace require_channels_config with a sentinel-raising stub."""

        class GuardCalled(Exception):
            pass

        def _sentinel(_config):
            raise GuardCalled()

        monkeypatch.setattr(vi, "require_channels_config", _sentinel)
        return GuardCalled

    def test_cmd_scan_calls_guard(self, guard_sentinel, tmp_path):
        import argparse

        config = {"output_dir": str(tmp_path)}  # no channels key
        args = argparse.Namespace(channel=None, since=None, dry_run=True, force=False, model=None)
        with pytest.raises(guard_sentinel):
            vi.cmd_scan(args, config)

    def test_cmd_concepts_calls_guard(self, guard_sentinel, tmp_path):
        import argparse

        config = {"output_dir": str(tmp_path)}
        args = argparse.Namespace(channel=None, force=False, model=None)
        with pytest.raises(guard_sentinel):
            vi.cmd_concepts(args, config)

    def test_cmd_dedupe_calls_guard(self, guard_sentinel, tmp_path):
        import argparse

        config = {"output_dir": str(tmp_path)}
        args = argparse.Namespace(channel=None, apply=False)
        with pytest.raises(guard_sentinel):
            vi.cmd_dedupe(args, config)

    def test_cmd_taxonomy_build_does_NOT_call_guard(self, guard_sentinel, tmp_path):
        """taxonomy-build is derived from on-disk artifacts; doesn't read channels."""
        import argparse

        config = {"output_dir": str(tmp_path)}
        args = argparse.Namespace()
        try:
            vi.cmd_taxonomy_build(args, config)
        except guard_sentinel:
            pytest.fail("cmd_taxonomy_build should not call require_channels_config")
        except Exception:
            pass  # other exceptions are fine for this test

    def test_cmd_index_does_NOT_call_guard(self, guard_sentinel, tmp_path):
        """index builds LanceDB from on-disk transcripts; doesn't read channels."""
        import argparse

        config = {"output_dir": str(tmp_path)}
        args = argparse.Namespace(force=False)
        try:
            vi.cmd_index(args, config)
        except guard_sentinel:
            pytest.fail("cmd_index should not call require_channels_config")
        except Exception:
            pass


class TestLooseFileGuardScope:
    """cmd_mindmap / cmd_transcript / cmd_process support a loose-file path
    (--file without --channel) that does NOT read config['channels']. The guard
    must fire only when --channel or a channel is inferred.

    This is a wiring check: the guard call must live inside the
    'has-channel' branch, not at command entry.
    """

    def test_mindmap_loose_file_without_channel_skips_guard(self, monkeypatch, tmp_path):
        """Smoke: the guard is not called at cmd_mindmap entry. The channel-branch
        specific wiring is verified by reviewing the code (the unconditional-at-entry
        anti-pattern is what this test prevents).
        """

        class GuardCalled(Exception):
            pass

        def _sentinel(_config):
            raise GuardCalled()

        # If the guard were wired unconditionally at cmd_mindmap entry, we'd see
        # GuardCalled before the command tries to parse --url or --file. Since
        # neither is provided, the command should exit for "need --url or --file"
        # reasons, not guard reasons.
        monkeypatch.setattr(vi, "require_channels_config", _sentinel)

        import argparse

        config = {"output_dir": str(tmp_path)}  # no channels key
        args = argparse.Namespace(
            url=None,
            file=None,
            channel=None,
            video_id=None,
            title=None,
            date=None,
            start=None,
            end=None,
            force=False,
            prompt=None,
            model=None,
        )
        try:
            vi.cmd_mindmap(args, config)
        except GuardCalled:
            pytest.fail(
                "cmd_mindmap called require_channels_config at entry; guard must be scoped to the args.channel branch"
            )
        except SystemExit:
            pass  # expected - no --url or --file
        except Exception:
            pass

    def test_mindmap_with_channel_and_missing_channels_fires_guard(self, monkeypatch, tmp_path):
        """Positive case: ``cmd_mindmap --file X --channel Y`` with no
        ``channels:`` in config MUST fire the guard. Without this test, a
        silent revert of the guard insertion inside the ``if args.channel:``
        branch would not be caught.

        Uses the sentinel pattern from TestGuardWiring so we assert the guard
        was called without relying on GEMINI_API_KEY or Gemini client setup.
        """

        class GuardCalled(Exception):
            pass

        def _sentinel(_config):
            raise GuardCalled()

        monkeypatch.setattr(vi, "require_channels_config", _sentinel)
        # Stub out upstream calls that happen before the guard so the test
        # reaches the `if args.channel:` block without env-var dependencies.
        monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
        monkeypatch.setattr(vi, "create_client", lambda *a, **kw: None)
        monkeypatch.setenv("GEMINI_API_KEY", "fake-for-test")

        import argparse

        real_mp4 = tmp_path / "fake.mp4"
        real_mp4.write_bytes(b"\x00")

        config = {"output_dir": str(tmp_path)}  # no channels key
        args = argparse.Namespace(
            url=None,
            file=str(real_mp4),
            channel="earlyaidopters",  # triggers the args.channel branch
            video_id=None,
            title=None,
            date=None,
            start=None,
            end=None,
            force=False,
            prompt=None,
            model=None,
        )

        with pytest.raises(GuardCalled):
            vi.cmd_mindmap(args, config)


class TestProfileNeedsNoChannels:
    """`profile` reads output_dir only, so it must work under the user-level
    minimal config (`~/.video-intel/config.yaml`) that the globally installed
    search skill uses - no `channels:` present (issue #117).
    """

    @staticmethod
    def _no_guard(monkeypatch):
        def _sentinel(_config):
            raise AssertionError("cmd_profile must not require channels: - the search skill has none")

        monkeypatch.setattr(vi, "require_channels_config", _sentinel)

    def test_cmd_profile_show_does_NOT_call_guard(self, monkeypatch, tmp_path, capsys):
        import argparse

        self._no_guard(monkeypatch)
        vi.cmd_profile(argparse.Namespace(profile_action="show"), {"output_dir": str(tmp_path)})
        assert "Personalization profile" in capsys.readouterr().out

    def test_cmd_profile_init_does_NOT_call_guard(self, monkeypatch, tmp_path):
        """init writes, so it stays curate-routed - but the reason is write scope,
        not a channels dependency. It must not fail on a channels-less config."""
        import argparse

        self._no_guard(monkeypatch)
        vi.cmd_profile(argparse.Namespace(profile_action="init"), {"output_dir": str(tmp_path)})
