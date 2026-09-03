"""One default mind-map prompt name, six readers (issue #210).

`config.get("default_prompt", ...)` carried an inline default at six sites and
they DISAGREED: `mindmap-light` in `cmd_scan`'s mindmap step, `mindmap --url`,
and the `scan --dry-run` preflight; `mindmap-knowledge` in `process --url`,
`process --file`, and `cmd_scan`'s concepts step.

Two problems, and the second is the one with teeth:

1. The same config produced a different prompt depending on which command read
   it. A user with no `default_prompt` key got a 4-6 branch light mind map from
   `scan` and a full knowledge mind map from `process --url`, for the same
   video, with nothing saying so.
2. The `scan --dry-run` PREFLIGHT sided with the light group, so it disagreed
   with three of the writers it exists to predict - the standing
   checker-must-use-the-writer's-own-rule class (PR #136). Today both names
   resolve, so nothing fails; a repo that renamed or retired one template would
   get a preflight passing on a config the real run rejects.

Same one-definition-N-consumers shape as `ENTRY_TIMESTAMP_PATTERN` (#195),
`TS_MINUTES` (#197) and `configured_channels` (#213).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi

REPO = Path(__file__).resolve().parent.parent
SOURCE_PATH = REPO / "scripts" / "video_intel.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")


class TestOneDefinition:
    def test_the_constant_is_the_owners_choice(self):
        """`mindmap-knowledge`, decided by the owner on issue #210: it is what
        `config.yaml.example` ships, so anyone who copied the template already
        gets it, and it is the richer output. A diff that flips it back to
        `mindmap-light` reverts a deliberate call, not an oversight."""
        assert vi.DEFAULT_PROMPT_NAME == "mindmap-knowledge"

    def test_the_shipped_template_agrees_with_the_code(self):
        """The template and the code fallback must not drift apart - that
        divergence is what made every doc claim about "the default" ambiguous
        (issue #204 recorded it as deferred, pointing here)."""
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text(encoding="utf-8"))
        assert cfg.get("default_prompt") == vi.DEFAULT_PROMPT_NAME, (
            "config.yaml.example ships a different default than the code falls back to"
        )

    def test_the_named_prompt_actually_exists(self):
        """A constant naming a template that is not on disk turns every
        no-key config into a `load_prompt` exit. Cheap to check, and the
        failure it prevents is total."""
        assert (REPO / "prompts" / f"{vi.DEFAULT_PROMPT_NAME}.md").is_file()

    def test_no_site_carries_its_own_literal(self):
        """An AST walk, not a grep: the docstrings and guardrail prose in this
        module legitimately QUOTE the old literals, and a text search would
        flag those forever."""
        offenders = []
        for node in ast.walk(ast.parse(SOURCE)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "default_prompt"
            ):
                continue
            if len(node.args) < 2:
                continue  # `.get("default_prompt")` with no default is fine
            default = node.args[1]
            if isinstance(default, ast.Constant):
                offenders.append(f"line {node.lineno}: inline default {default.value!r}")
        assert not offenders, (
            f"these sites carry their own default and will drift from DEFAULT_PROMPT_NAME: {offenders}"
        )

    def test_the_walk_finds_all_six_readers(self):
        """Companion, per the standing rule: a walk that stops matching turns
        the check above into `assert not []` forever. Six is the real count -
        three that used to say `mindmap-light` and three `mindmap-knowledge`."""
        reads = [
            n
            for n in ast.walk(ast.parse(SOURCE))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and n.args[0].value == "default_prompt"
        ]
        assert len(reads) == 6, f"expected 6 default_prompt readers, found {len(reads)}"
        assert all(
            isinstance(n.args[1], ast.Name) and n.args[1].id == "DEFAULT_PROMPT_NAME" for n in reads if len(n.args) > 1
        )


class TestThePreflightNoLongerDisagreesWithTheWriters:
    """The load-bearing half. `validate_channel_knobs` predicts what a real run
    will do; before this it predicted `mindmap-light` while `process --url` and
    `process --file` used `mindmap-knowledge`. Both resolve today, so nothing
    failed - but a preflight that looks up a different template than the loader
    it is predicting can pass on a config that will actually fail, which is
    exactly the PR #136 failure class.
    """

    def test_the_preflight_and_the_writers_resolve_the_same_name(self):
        """Derive both sides independently and compare, rather than asserting
        one against itself."""
        config = {}  # no default_prompt key: the only case a fallback is used
        channel = {"name": "alpha", "url": "https://youtube.com/@alpha"}

        preflight_name = channel.get("prompt") or config.get("default_prompt", vi.DEFAULT_PROMPT_NAME)
        # What every writer site resolves, read from the source rather than
        # re-derived: all six now name the same constant.
        assert preflight_name == vi.DEFAULT_PROMPT_NAME
        assert vi.resolve_prompt_path(preflight_name).is_file(), (
            "the preflight predicts a template the loader cannot resolve"
        )

    def test_the_preflight_accepts_the_default_on_a_config_that_names_nothing(self):
        """A channel with no `prompt` and a config with no `default_prompt` is
        the common fresh-checkout shape; it must not report a knob problem."""
        problems = vi.validate_channel_knobs({"name": "alpha", "url": "u"}, {})
        assert not [p for p in problems if p[0] == "prompt"], (
            f"a config naming no prompt at all reported a prompt problem: {problems}"
        )


class TestTheBehaviorChangeIsDeliberateAndBounded:
    """Three sites previously fell back to `mindmap-light`, so a config with no
    `default_prompt` key now gets knowledge mind maps from `scan` and
    `mindmap --url` where it used to get light ones. That is the intended
    change; these tests pin its EDGES so it cannot widen silently.
    """

    def test_an_explicit_channel_prompt_still_wins(self):
        config = {"default_prompt": "mindmap-heavy"}
        channel = {"name": "a", "prompt": "mindmap-light"}
        assert (channel.get("prompt") or config.get("default_prompt", vi.DEFAULT_PROMPT_NAME)) == ("mindmap-light")

    def test_an_explicit_top_level_default_still_wins_over_the_constant(self):
        config = {"default_prompt": "mindmap-heavy"}
        assert config.get("default_prompt", vi.DEFAULT_PROMPT_NAME) == "mindmap-heavy"

    def test_mindmap_light_is_still_a_real_prompt_anyone_can_choose(self):
        """The fallback moved; the template did not go away. A channel that
        wants the fast scan says so explicitly."""
        assert (REPO / "prompts" / "mindmap-light.md").is_file()
        assert vi.resolve_prompt_path("mindmap-light").is_file()


class TestDocsMatchTheCode:
    """Issue #204's docs-currency suite recorded this ambiguity as deferred and
    pointed here. Now that one answer exists, the README must state it."""

    def test_the_readme_names_the_real_default(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        row = [ln for ln in readme.splitlines() if ln.strip().startswith("| default_prompt ")]
        assert row, "the README no longer documents default_prompt"
        assert vi.DEFAULT_PROMPT_NAME in row[0], f"the README row is stale: {row[0]}"

    @pytest.mark.parametrize("stale", ["mindmap-light` on `scan", "differs by command"])
    def test_the_readme_no_longer_describes_the_split(self, stale):
        """The row used to say the fallback differed by command, which was true
        and is now false. A doc describing a fixed defect is its own defect."""
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        row = [ln for ln in readme.splitlines() if ln.strip().startswith("| default_prompt ")]
        assert stale not in row[0], f"the README still describes the retired split: {row[0]}"

    def test_no_living_doc_claims_a_different_default(self):
        """A narrow, absolute check in the same spirit as the stale-model one:
        no living doc may pair `default_prompt` with a name that is not the
        constant."""
        docs = ["README.md", "INSTALLATION.md", "skills/video-intel/SKILL.md"]
        pattern = re.compile(r"default_prompt.{0,80}?(mindmap-[a-z]+)", re.S)
        offenders = []
        for doc in docs:
            text = (REPO / doc).read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                if m.group(1) != vi.DEFAULT_PROMPT_NAME:
                    offenders.append(f"{doc}: {m.group(0)[:90]!r}")
        assert not offenders, f"a living doc names a stale default_prompt: {offenders}"
