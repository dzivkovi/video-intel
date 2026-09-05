"""Private prompt directories override the shipped `prompts/`.

The repo ships plain, general-purpose templates. An operator who has sharpened
one of them keeps the opinionated version outside this public repo and names
its folder in `prompt_dirs:` (or `$VIDEO_INTEL_PROMPT_DIR`): the private file
wins locally, a fork with neither configured keeps working on the shipped
defaults, and no code depends on a file that is not in the repo.

`resolve_prompt_path` stays the ONE place a prompt name becomes a path (issue
#169 item 8). These tests execute the real resolver and the real `load_prompt`
- never a stub of either - because a stub handed the same path by the test
agrees with the assertion by construction and would keep passing while the two
halves drifted apart (the PR #136 checker/writer class).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi

BUNDLED = vi.SKILL_DIR / "prompts"
# A name that genuinely ships, so "the bundled file wins" is a real comparison
# rather than two missing paths agreeing.
SHIPPED_NAME = "mindmap-knowledge"


@pytest.fixture(autouse=True)
def clean_prompt_resolution(monkeypatch):
    """No config dirs, no env var, fresh log memos, and an empty env cache.

    Every once-per-process memo is reset here. A test that inherits another
    test's memo asserts on log lines that were suppressed for an unrelated
    reason, which is the quietest way for this suite to stop guarding anything.
    """
    monkeypatch.setattr(vi, "PROMPT_DIRS", [])
    monkeypatch.setattr(vi, "_LOGGED_PROMPT_OVERRIDES", set())
    monkeypatch.setattr(vi, "_LOGGED_PROMPT_FALLBACKS", set())
    monkeypatch.setattr(vi, "_LOGGED_UNREADABLE_PROMPT_DIRS", set())
    monkeypatch.setattr(vi, "_ENV_PROMPT_DIRS_CACHE", {})
    monkeypatch.delenv(vi.PROMPT_DIR_ENV_VAR, raising=False)


def write_prompt(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


class TestBundledDefaultWhenNothingConfigured:
    def test_unconfigured_resolution_points_at_the_bundled_prompt(self):
        assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"

    def test_unconfigured_unknown_name_still_returns_the_bundled_path(self):
        # Existence is consulted only to pick a winner. With no candidate
        # anywhere the caller must keep seeing the path it always saw.
        assert vi.resolve_prompt_path("no-such-prompt") == BUNDLED / "no-such-prompt.md"

    def test_no_override_log_when_the_bundled_prompt_wins(self, caplog):
        with caplog.at_level(logging.INFO, logger="video_intel"):
            vi.resolve_prompt_path(SHIPPED_NAME)

        assert "overrides bundled" not in caplog.text


class TestConfigPromptDirsWin:
    def test_a_private_copy_of_a_shipped_name_wins_over_the_bundled_one(self, tmp_path, monkeypatch):
        private = write_prompt(tmp_path / "private", SHIPPED_NAME, "PRIVATE VERSION")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        resolved = vi.resolve_prompt_path(SHIPPED_NAME)

        assert resolved == private
        assert resolved.read_text(encoding="utf-8") == "PRIVATE VERSION"

    def test_an_override_logs_one_info_line_naming_the_directory(self, tmp_path, monkeypatch, caplog):
        write_prompt(tmp_path / "private", SHIPPED_NAME, "PRIVATE VERSION")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        with caplog.at_level(logging.INFO, logger="video_intel"):
            vi.resolve_prompt_path(SHIPPED_NAME)

        assert (
            f"Prompt '{SHIPPED_NAME}' resolved from {tmp_path / 'private'} (overrides bundled prompts/)" in caplog.text
        )

    def test_the_override_line_is_logged_once_per_process_per_name(self, tmp_path, monkeypatch, caplog):
        # A scan resolves the same prompt once per video; 40 identical INFO
        # lines would drown the per-video progress the operator reads.
        write_prompt(tmp_path / "private", SHIPPED_NAME, "PRIVATE VERSION")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        with caplog.at_level(logging.INFO, logger="video_intel"):
            for _ in range(3):
                vi.resolve_prompt_path(SHIPPED_NAME)

        assert caplog.text.count("overrides bundled prompts/") == 1


class TestFallThroughToTheNextDirectory:
    def test_a_dir_without_the_file_falls_through_to_the_next_override_dir(self, tmp_path, monkeypatch):
        (tmp_path / "first").mkdir()
        second = write_prompt(tmp_path / "second", SHIPPED_NAME, "SECOND")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "first", tmp_path / "second"])

        assert vi.resolve_prompt_path(SHIPPED_NAME) == second

    def test_overrides_that_lack_the_file_fall_all_the_way_back_to_bundled(self, tmp_path, monkeypatch):
        write_prompt(tmp_path / "first", "some-other-prompt", "IRRELEVANT")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "first", tmp_path / "second"])

        assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"

    def test_a_nonexistent_override_dir_is_skipped_not_an_error(self, tmp_path, monkeypatch):
        # A shared config may name a folder only some machines have.
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "never-created"])

        assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"


class TestEnvVarPromptDirs:
    def test_two_pathsep_joined_paths_are_searched_in_order(self, tmp_path, monkeypatch):
        first = write_prompt(tmp_path / "a", SHIPPED_NAME, "FROM A")
        write_prompt(tmp_path / "b", SHIPPED_NAME, "FROM B")
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))

        assert vi.resolve_prompt_path(SHIPPED_NAME) == first

    def test_the_second_env_path_wins_when_the_first_lacks_the_file(self, tmp_path, monkeypatch):
        (tmp_path / "a").mkdir()
        second = write_prompt(tmp_path / "b", SHIPPED_NAME, "FROM B")
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))

        assert vi.resolve_prompt_path(SHIPPED_NAME) == second

    def test_config_dirs_beat_env_var_dirs(self, tmp_path, monkeypatch):
        from_config = write_prompt(tmp_path / "config", SHIPPED_NAME, "FROM CONFIG")
        write_prompt(tmp_path / "env", SHIPPED_NAME, "FROM ENV")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "config"])
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, str(tmp_path / "env"))

        assert vi.resolve_prompt_path(SHIPPED_NAME) == from_config

    def test_an_env_var_dir_still_beats_the_bundled_prompt(self, tmp_path, monkeypatch):
        from_env = write_prompt(tmp_path / "env", SHIPPED_NAME, "FROM ENV")
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, str(tmp_path / "env"))

        assert vi.resolve_prompt_path(SHIPPED_NAME) == from_env


class TestMalformedEntriesDegrade:
    """A bad entry warns and is dropped; resolution still works. Aborting a
    paid scan over one typo'd path is the failure this shape avoids (the issue
    #213 convention)."""

    def test_a_relative_entry_is_ignored_with_a_warning_and_resolution_survives(self, tmp_path, monkeypatch, caplog):
        good = write_prompt(tmp_path / "good", SHIPPED_NAME, "GOOD")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            dirs = vi._coerce_prompt_dirs(["prompts-private", str(tmp_path / "good")], source="test-config")
        monkeypatch.setattr(vi, "PROMPT_DIRS", dirs)

        assert "prompts-private" in caplog.text
        assert "relative" in caplog.text
        assert dirs == [tmp_path / "good"]
        assert vi.resolve_prompt_path(SHIPPED_NAME) == good

    def test_a_relative_entry_is_never_resolved_against_the_cwd(self, tmp_path, monkeypatch):
        # Resolving it against the CWD would make the same command send a
        # different prompt to Gemini depending on which folder it ran from.
        write_prompt(tmp_path / "cwd-prompts", SHIPPED_NAME, "CWD VERSION")
        monkeypatch.chdir(tmp_path)

        dirs = vi._coerce_prompt_dirs(["cwd-prompts"], source="test-config")

        assert dirs == []

    def test_a_non_string_entry_is_ignored_with_a_warning(self, tmp_path, caplog):
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            dirs = vi._coerce_prompt_dirs([{"path": "x"}, str(tmp_path)], source="test-config")

        assert dirs == [tmp_path]
        assert "non-string" in caplog.text

    def test_a_wrong_typed_whole_value_is_ignored_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="video_intel"):
            dirs = vi._coerce_prompt_dirs({"a": "b"}, source="test-config")

        assert dirs == []
        assert "expected a list of absolute paths" in caplog.text

    def test_a_bare_absolute_string_is_accepted_as_one_entry(self, tmp_path):
        # `prompt_dirs: /path` instead of a list is an ordinary YAML slip.
        # Iterating it character by character is never the right answer.
        assert vi._coerce_prompt_dirs(str(tmp_path), source="test-config") == [tmp_path]


class TestLoadPromptAgreesWithTheResolver:
    def test_load_prompt_reads_exactly_what_the_resolver_points_at_under_an_override(self, tmp_path, monkeypatch):
        # The issue #169 item 8 property, re-proved with overrides in play:
        # both halves derived independently, then compared.
        write_prompt(tmp_path / "private", SHIPPED_NAME, "PRIVATE VERSION\n")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        assert vi.resolve_prompt_path(SHIPPED_NAME).read_text(encoding="utf-8") == vi.load_prompt(SHIPPED_NAME)

    def test_load_prompt_returns_the_private_body_not_the_bundled_one(self, tmp_path, monkeypatch):
        write_prompt(tmp_path / "private", SHIPPED_NAME, "PRIVATE VERSION\n")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        assert vi.load_prompt(SHIPPED_NAME) == "PRIVATE VERSION\n"

    def test_unknown_name_exits_1_and_the_error_names_every_searched_dir(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "config-dir"])
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, str(tmp_path / "env-dir"))

        with caplog.at_level(logging.ERROR, logger="video_intel"), pytest.raises(SystemExit) as excinfo:
            vi.load_prompt("no-such-prompt")

        assert excinfo.value.code == 1
        assert str(tmp_path / "config-dir") in caplog.text
        assert str(tmp_path / "env-dir") in caplog.text
        assert str(BUNDLED) in caplog.text


class TestPromptSearchDirsOrder:
    def test_bundled_is_always_last(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "config"])
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, str(tmp_path / "env"))

        assert vi.prompt_search_dirs() == [tmp_path / "config", tmp_path / "env", BUNDLED]

    def test_unconfigured_search_is_the_bundled_dir_alone(self):
        assert vi.prompt_search_dirs() == [BUNDLED]


class TestLoadConfigPopulatesPromptDirs:
    def test_plugin_local_config_populates_prompt_dirs(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "config.yaml").write_text(
            f"output_dir: {tmp_path / 'corpus'}\nprompt_dirs:\n  - {tmp_path / 'private'}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(vi, "SKILL_DIR", skill_dir)

        vi.load_config()

        assert [tmp_path / "private"] == vi.PROMPT_DIRS

    def test_an_absent_key_clears_a_previous_loads_value(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "config.yaml").write_text(f"output_dir: {tmp_path / 'corpus'}\n", encoding="utf-8")
        monkeypatch.setattr(vi, "SKILL_DIR", skill_dir)
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "stale"])

        vi.load_config()

        assert vi.PROMPT_DIRS == []

    def test_the_env_var_output_dir_fallback_leaves_prompt_dirs_empty(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        monkeypatch.setattr(vi, "SKILL_DIR", skill_dir)
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "stale"])
        monkeypatch.setenv("VIDEO_INTEL_OUTPUT_DIR", str(tmp_path / "corpus"))

        vi.load_config()

        assert vi.PROMPT_DIRS == []

    def test_the_user_level_config_can_supply_prompt_dirs(self, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        user_config = tmp_path / "user" / "config.yaml"
        user_config.parent.mkdir()
        user_config.write_text(
            f"output_dir: {tmp_path / 'corpus'}\nprompt_dirs:\n  - {tmp_path / 'private'}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(vi, "SKILL_DIR", skill_dir)
        monkeypatch.setattr(vi, "_user_config_path", lambda: user_config)
        monkeypatch.delenv("VIDEO_INTEL_OUTPUT_DIR", raising=False)

        vi.load_config()

        assert [tmp_path / "private"] == vi.PROMPT_DIRS


class TestADirectoryNamedLikeAPromptIsNotACandidate:
    """Selection is `is_file()`, never `exists()`.

    A folder called `<name>.md` inside an override dir satisfied the old
    existence check, so the preflight reported the name as resolving and
    `load_prompt` then died with `IsADirectoryError` - the checker and the
    writer disagreeing about the same path (the PR #136 class).
    """

    def test_the_resolver_skips_a_directory_named_like_the_prompt_file(self, tmp_path, monkeypatch):
        (tmp_path / "private" / f"{SHIPPED_NAME}.md").mkdir(parents=True)
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"

    def test_load_prompt_agrees_and_reads_the_bundled_body(self, tmp_path, monkeypatch):
        (tmp_path / "private" / f"{SHIPPED_NAME}.md").mkdir(parents=True)
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        assert vi.load_prompt(SHIPPED_NAME) == (BUNDLED / f"{SHIPPED_NAME}.md").read_text(encoding="utf-8")


class TestAnUnreadableOverrideDirDegrades:
    """An unreadable override folder raises `PermissionError` from `is_file()`.

    That must not escape: the same resolver runs inside the report-only
    `--dry-run` preflight, which is documented as never exiting.
    """

    @staticmethod
    def _deny(monkeypatch, denied: Path) -> None:
        real_is_file = Path.is_file

        def fake_is_file(self: Path) -> bool:
            if self.parent == denied:
                raise PermissionError(13, "Permission denied")
            return real_is_file(self)

        monkeypatch.setattr(Path, "is_file", fake_is_file)

    def test_it_warns_and_falls_through_instead_of_raising(self, tmp_path, monkeypatch, caplog):
        denied = tmp_path / "denied"
        denied.mkdir()
        monkeypatch.setattr(vi, "PROMPT_DIRS", [denied])
        self._deny(monkeypatch, denied)

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            resolved = vi.resolve_prompt_path(SHIPPED_NAME)

        assert resolved == BUNDLED / f"{SHIPPED_NAME}.md"
        assert str(denied) in caplog.text
        assert "Permission denied" in caplog.text

    def test_the_unreadable_dir_warning_is_once_per_directory_per_process(self, tmp_path, monkeypatch, caplog):
        denied = tmp_path / "denied"
        denied.mkdir()
        monkeypatch.setattr(vi, "PROMPT_DIRS", [denied])
        self._deny(monkeypatch, denied)

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            for _ in range(4):
                vi.resolve_prompt_path(SHIPPED_NAME)

        assert caplog.text.count("is unreadable") == 1

    def test_validate_channel_knobs_still_returns_no_prompt_problem(self, tmp_path, monkeypatch):
        denied = tmp_path / "denied"
        denied.mkdir()
        monkeypatch.setattr(vi, "PROMPT_DIRS", [denied])
        self._deny(monkeypatch, denied)

        problems = vi.validate_channel_knobs({"name": "alpha"}, {"default_prompt": SHIPPED_NAME})

        assert [p for p in problems if p[0] == "prompt"] == []


class TestFallingBackToBundledIsNeverSilent:
    def test_configured_overrides_that_lack_the_name_log_info_once_per_name(self, tmp_path, monkeypatch, caplog):
        # A name never overridden in this process is the routine case: it
        # must not read as a WARNING just because some OTHER name is overridden.
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        with caplog.at_level(logging.INFO, logger="video_intel"):
            for _ in range(3):
                vi.resolve_prompt_path(SHIPPED_NAME)

        assert caplog.text.count("not in any override dir") == 1
        assert SHIPPED_NAME in caplog.text
        assert str(tmp_path / "private") in caplog.text
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_no_fallback_log_when_no_override_dir_is_configured(self, caplog):
        with caplog.at_level(logging.INFO, logger="video_intel"):
            vi.resolve_prompt_path(SHIPPED_NAME)

        assert "not in any override dir" not in caplog.text

    def test_deleting_the_override_file_mid_run_switches_to_bundled_with_a_warning(self, tmp_path, monkeypatch, caplog):
        # The memo must not swallow this: an operator who deletes the private
        # file mid-scan silently starts sending Gemini a different prompt.
        # This name previously resolved from an override, so falling back now
        # is a real regression and must escalate to WARNING, not INFO.
        private = write_prompt(tmp_path / "private", SHIPPED_NAME, "PRIVATE VERSION")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        with caplog.at_level(logging.INFO, logger="video_intel"):
            assert vi.resolve_prompt_path(SHIPPED_NAME) == private
            private.unlink()
            assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"

        assert any(
            r.levelno == logging.WARNING and "falls back to bundled prompts/" in r.getMessage() for r in caplog.records
        )

    def test_fallback_then_override_then_fallback_logs_the_warning(self, tmp_path, monkeypatch, caplog):
        # Reported bug: the once-per-name fallback memo used to be a single
        # set keyed on name alone. Sequence: (1) no override file yet -> INFO
        # fallback, name memoized; (2) the file appears -> INFO override line;
        # (3) the file is deleted again -> this SHOULD escalate to WARNING
        # (the name previously won from an override), but the old code's
        # early return on step 1's memo fired first and nothing was logged
        # at all. A third fallback must not repeat the WARNING.
        private_dir = tmp_path / "private"
        monkeypatch.setattr(vi, "PROMPT_DIRS", [private_dir])

        with caplog.at_level(logging.INFO, logger="video_intel"):
            # Step 1: no override file present yet -> INFO fallback.
            assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"

            # Step 2: the override file appears -> INFO override line.
            private = write_prompt(private_dir, SHIPPED_NAME, "PRIVATE VERSION")
            assert vi.resolve_prompt_path(SHIPPED_NAME) == private

            # Step 3: the override file is deleted again -> WARNING fallback.
            private.unlink()
            assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"

            # Step 4: still gone -> no duplicate WARNING.
            assert vi.resolve_prompt_path(SHIPPED_NAME) == BUNDLED / f"{SHIPPED_NAME}.md"

        assert caplog.text.count("not in any override dir") == 1
        assert caplog.text.count("overrides bundled prompts/") == 1
        warning_lines = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "falls back to bundled prompts/" in r.getMessage()
        ]
        assert len(warning_lines) == 1

    def test_a_second_overridden_name_logs_its_own_info_line(self, tmp_path, monkeypatch, caplog):
        write_prompt(tmp_path / "private", SHIPPED_NAME, "ONE")
        write_prompt(tmp_path / "private", "transcript", "TWO")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        with caplog.at_level(logging.INFO, logger="video_intel"):
            vi.resolve_prompt_path(SHIPPED_NAME)
            vi.resolve_prompt_path("transcript")

        assert caplog.text.count("overrides bundled prompts/") == 2

    def test_two_overridden_names_and_five_bundled_names_log_five_info_and_no_warnings(
        self, tmp_path, monkeypatch, caplog
    ):
        # Two names genuinely override; five others only ever exist bundled.
        # None of the five untouched names may escalate to WARNING just
        # because other names in the same config are overridden.
        write_prompt(tmp_path / "private", SHIPPED_NAME, "ONE")
        write_prompt(tmp_path / "private", "cliffnotes-distiller", "TWO")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])
        bundled_only_names = ["mindmap-light", "mindmap-heavy", "concepts", "transcript", "topic-digest"]

        with caplog.at_level(logging.INFO, logger="video_intel"):
            vi.resolve_prompt_path(SHIPPED_NAME)
            vi.resolve_prompt_path("cliffnotes-distiller")
            for name in bundled_only_names:
                vi.resolve_prompt_path(name)

        assert caplog.text.count("not in any override dir") == 5
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)


class TestEntryNormalization:
    def test_a_leading_tilde_is_expanded_to_the_home_directory(self, tmp_path, monkeypatch):
        # `~/prompts` is not absolute until it is expanded; without
        # `.expanduser()` this entry is dropped as relative.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        assert vi._coerce_prompt_dirs(["~/my-prompts"], source="test-config") == [tmp_path / "my-prompts"]

    @pytest.mark.parametrize("quote", ['"', "'"])
    def test_a_shell_quoted_entry_resolves_and_is_not_called_relative(self, tmp_path, monkeypatch, caplog, quote):
        # `set VIDEO_INTEL_PROMPT_DIR="C:\p"` on cmd.exe keeps the quotes.
        good = write_prompt(tmp_path / "quoted", SHIPPED_NAME, "QUOTED")
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, f"{quote}{tmp_path / 'quoted'}{quote}")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            resolved = vi.resolve_prompt_path(SHIPPED_NAME)

        assert resolved == good
        assert "relative" not in caplog.text

    def test_an_entry_naming_a_file_warns_and_resolution_falls_back(self, tmp_path, caplog):
        a_file = tmp_path / "not-a-dir.txt"
        a_file.write_text("x", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            dirs = vi._coerce_prompt_dirs([str(a_file)], source="test-config")

        assert dirs == []
        assert str(a_file) in caplog.text
        assert "not a directory" in caplog.text

    def test_a_nonexistent_absolute_dir_is_kept(self, tmp_path):
        # One shared config may name a folder only some machines have; the
        # resolver skips it at lookup time rather than dropping it here.
        missing = tmp_path / "never-created"

        assert vi._coerce_prompt_dirs([str(missing)], source="test-config") == [missing]

    @pytest.mark.parametrize("segment", ["", "   "])
    def test_empty_and_whitespace_env_segments_are_ignored(self, tmp_path, monkeypatch, segment):
        good = write_prompt(tmp_path / "good", SHIPPED_NAME, "GOOD")
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, os.pathsep.join([segment, str(tmp_path / "good")]))

        assert vi.resolve_prompt_path(SHIPPED_NAME) == good


class TestEnvParsingIsMemoized:
    def test_a_malformed_env_entry_warns_once_across_five_resolutions(self, monkeypatch, caplog):
        # Measured before the cache: 150+ identical lines in one scan, because
        # every prompt resolution re-parsed the variable.
        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, "relative-prompts")

        with caplog.at_level(logging.WARNING, logger="video_intel"):
            for _ in range(5):
                vi.resolve_prompt_path(SHIPPED_NAME)

        assert caplog.text.count("relative-prompts") == 1

    def test_a_changed_env_value_is_parsed_again(self, tmp_path, monkeypatch):
        first = write_prompt(tmp_path / "one", SHIPPED_NAME, "ONE")
        second = write_prompt(tmp_path / "two", SHIPPED_NAME, "TWO")

        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, str(tmp_path / "one"))
        assert vi.resolve_prompt_path(SHIPPED_NAME) == first

        monkeypatch.setenv(vi.PROMPT_DIR_ENV_VAR, str(tmp_path / "two"))
        assert vi.resolve_prompt_path(SHIPPED_NAME) == second


class TestUnreadablePromptFileExitsOne:
    def test_an_unreadable_prompt_file_exits_1_and_names_the_file(self, tmp_path, monkeypatch, caplog):
        private = write_prompt(tmp_path / "private", SHIPPED_NAME, "PRIVATE")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])
        real_read_text = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs) -> str:
            if self == private:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        with caplog.at_level(logging.ERROR, logger="video_intel"), pytest.raises(SystemExit) as excinfo:
            vi.load_prompt(SHIPPED_NAME)

        assert excinfo.value.code == 1
        assert str(private) in caplog.text

    def test_a_non_utf8_prompt_file_exits_1_and_names_the_file(self, tmp_path, monkeypatch, caplog):
        private = tmp_path / "private" / f"{SHIPPED_NAME}.md"
        private.parent.mkdir(parents=True)
        private.write_bytes(b"\xff\xfe not utf-8 \xff")
        monkeypatch.setattr(vi, "PROMPT_DIRS", [tmp_path / "private"])

        with caplog.at_level(logging.ERROR, logger="video_intel"), pytest.raises(SystemExit) as excinfo:
            vi.load_prompt(SHIPPED_NAME)

        assert excinfo.value.code == 1
        assert str(private) in caplog.text
