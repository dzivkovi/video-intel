"""One default mind-map prompt name, and every reader uses it (issue #210).

`config.get("default_prompt", ...)` carried an inline default at six sites and
they DISAGREED: `mindmap-light` in `cmd_scan`'s mindmap step, `mindmap --url`
and `mindmap --file`, and the `scan --dry-run` preflight; `mindmap-knowledge` in
`process --url`, `process --file`, and `cmd_scan`'s concepts step.

Two problems, and the second is the one with teeth:

1. The same config produced a different prompt depending on which command read
   it. Measured at the real `load_prompt` call: `mindmap --url` loaded
   `mindmap-light` while `process --url` loaded `mindmap-knowledge`.
2. The `scan --dry-run` PREFLIGHT sided with the light group, so it disagreed
   with three of the writers it exists to predict - the PR #136
   checker-must-use-the-writer's-own-rule class. Both names resolve today so
   nothing failed; a repo that renamed a template would get a preflight passing
   on a config the real run rejects.

**These tests DRIVE the real commands.** The first cut re-implemented
`channel.get("prompt") or config.get("default_prompt", DEFAULT_PROMPT_NAME)`
inside the test bodies, which exercises Python's `or` operator and `dict.get`,
never the module. A reviewer deleted the `prompt` override from all six sites -
making the per-channel `prompt:` key and the `--prompt` flag total no-ops
corpus-wide - and all 14 tests passed. Reverting the constant to the literal
`"mindmap-light"` also passed. A test that recomputes the rule it is checking
cannot fail when the rule changes.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import video_intel as vi

REPO = Path(__file__).resolve().parent.parent
SOURCE = (REPO / "scripts" / "video_intel.py").read_text(encoding="utf-8")

# Every function that supplies a fallback for `default_prompt`. Named, not
# counted: a reviewer added a seventh reader that CORRECTLY used the constant
# and the old `== 6` assertion failed with a message giving no hint that what
# they had done was fine. `cmd_scan`'s concepts step is deliberately absent -
# the review found it re-derived a provenance string that the writer already
# records in meta.json, disagreeing with the artifact on 42% of the corpus, so
# that reader was DELETED rather than normalized.
EXPECTED_READERS = {
    "validate_channel_knobs",  # the scan --dry-run preflight
    "cmd_scan",  # the mindmap step
    "_cmd_mindmap_impl",  # mindmap --url and --file
    "_cmd_process_url",
    "_cmd_process_impl",  # process --file
}


def _default_prompt_readers() -> dict[str, list[ast.Call]]:
    """Every `<something>.get("default_prompt", ...)` call, by enclosing function."""
    tree = ast.parse(SOURCE)
    out: dict[str, list[ast.Call]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "default_prompt"
            ):
                out.setdefault(fn.name, []).append(node)
    return out


class TestOneDefinition:
    def test_the_constant_is_the_owners_choice(self):
        """`mindmap-knowledge`, decided by the owner on issue #210: it is what
        `config.yaml.example` ships, so anyone who copied the template already
        gets it, and it is the richer output."""
        assert vi.DEFAULT_PROMPT_NAME == "mindmap-knowledge"

    def test_the_shipped_template_agrees_with_the_code(self):
        cfg = yaml.safe_load((REPO / "config.yaml.example").read_text(encoding="utf-8"))
        assert cfg.get("default_prompt") == vi.DEFAULT_PROMPT_NAME

    def test_the_named_prompt_actually_exists(self):
        """A constant naming a template that is not on disk turns every no-key
        config into a `load_prompt` exit."""
        assert (REPO / "prompts" / f"{vi.DEFAULT_PROMPT_NAME}.md").is_file()

    def test_every_reader_is_named_and_uses_the_constant(self):
        readers = _default_prompt_readers()
        assert set(readers) == EXPECTED_READERS, (
            "the set of default_prompt readers changed - classify the new one "
            f"deliberately.\n  found:    {sorted(readers)}\n  expected: {sorted(EXPECTED_READERS)}"
        )
        offenders = []
        for fn_name, calls in readers.items():
            for call in calls:
                if len(call.args) < 2:
                    # `.get("default_prompt")` returns None, and
                    # `resolve_prompt_path(None)` raises TypeError. The old
                    # guard blessed this shape as "fine"; it is the one
                    # remaining way to break the contract.
                    offenders.append(f"{fn_name}: no default at all (resolves to None)")
                elif not (isinstance(call.args[1], ast.Name) and call.args[1].id == "DEFAULT_PROMPT_NAME"):
                    offenders.append(f"{fn_name}: inline default {ast.unparse(call.args[1])}")
        assert not offenders, f"readers that bypass DEFAULT_PROMPT_NAME: {offenders}"

    def test_the_walk_is_not_vacuous(self):
        """Positive control. A walk that stops matching turns the check above
        into `assert set() == set()`."""
        assert _default_prompt_readers(), "the walk finds no reader at all"


# ---------------------------------------------------------------------------
# Caller-level: what does each command ACTUALLY load?
# ---------------------------------------------------------------------------

NO_KEY_CONFIG = {"channels": [{"name": "alpha", "url": "https://youtube.com/@alpha"}]}


def _args(**kw):
    base = {
        "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "file": None,
        "channel": "alpha",
        "title": "A Talk",
        "date": "2026-08-12",
        "start": None,
        "end": None,
        "force": False,
        "prompt": None,
        "model": None,
        "video_id": "abcdefghijk",
        "media_resolution": "low",
        "chunk_minutes": None,
        "transcript_source": None,
        "topic": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def loaded_prompts(monkeypatch, tmp_path):
    """Records every prompt name the real command asks `load_prompt` for."""
    seen: list[str] = []
    monkeypatch.setattr(vi, "load_prompt", lambda name: (seen.append(name), "PROMPT")[1])
    monkeypatch.setattr(vi, "resolve_output_dir", lambda *a, **k: tmp_path)
    monkeypatch.setattr(vi, "require_gemini", lambda: (None, None))
    monkeypatch.setattr(vi, "create_client", lambda *a, **k: object())
    monkeypatch.setattr(vi, "resolve_model", lambda *a, **k: "stub")
    monkeypatch.setattr(vi, "require_youtube", lambda: lambda *a, **k: None)
    monkeypatch.setattr(vi, "require_channels_config", lambda _c: None)
    monkeypatch.setattr(vi, "_lookup_was_livestream", lambda _v: False)
    monkeypatch.setattr(vi, "_lookup_video_duration_seconds", lambda _v: 600)
    for name in ("process_mindmap", "process_transcript", "process_concepts"):
        monkeypatch.setattr(vi, name, lambda *a, **k: ("2026-08-12-a-talk", "done"))
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    return seen


def _mindmap_names(seen: list[str]) -> list[str]:
    return [n for n in seen if n.startswith("mindmap")]


class TestWhatEachCommandActuallyLoads:
    """The Gate 1 measurement, frozen as a test."""

    @pytest.mark.parametrize("command", ["mindmap", "process"])
    def test_a_config_with_no_key_gets_the_constant(self, command, loaded_prompts):
        fn = {"mindmap": vi.cmd_mindmap, "process": vi.cmd_process}[command]
        with pytest.raises(BaseException):  # noqa: B017 - may exit; the capture is the point
            fn(_args(), dict(NO_KEY_CONFIG))
            raise SystemExit(0)
        names = _mindmap_names(loaded_prompts)
        assert names, f"{command} loaded no mindmap prompt at all"
        assert names[0] == vi.DEFAULT_PROMPT_NAME, (
            f"{command} loaded {names[0]!r}; before #210 mindmap and process disagreed here"
        )

    def test_only_two_readers_consult_the_per_channel_prompt_key(self):
        """Writing this test the obvious way - drive `mindmap --url` with a
        channel carrying `prompt: mindmap-heavy` - FAILED, and the failure was
        real: `mindmap --url`, `process --url` and `process --file` never read
        the channel's `prompt` key at all. Only `cmd_scan` and the preflight do.

        That asymmetry PREDATES this ticket and is out of its scope (#210 is
        about the FALLBACK), but it is the same family as issue #127, where the
        manual `--url` commands were found ignoring channel config. Pinned here
        so it is recorded rather than silently accepted, and so a future fix
        has a test to flip. Filed as its own issue.

        This one check is SOURCE-level on purpose, unlike its neighbours, which
        drive the real commands. Driving `cmd_scan` end to end needs its whole
        channel-id / fetch / enrich / is-processed chain stubbed, and #210
        changes nothing about this behavior - it only records it. When the
        #127-family fix lands, that ticket should replace this with a real
        caller-level test.
        """
        readers = _default_prompt_readers()
        consults_channel = set()
        src_lines = SOURCE.splitlines()
        for fn_name, calls in readers.items():
            for call in calls:
                line = src_lines[call.lineno - 1]
                if '.get("prompt")' in line:
                    consults_channel.add(fn_name)
        assert consults_channel == {"validate_channel_knobs", "cmd_scan"}, (
            "the set of readers honoring a per-channel `prompt:` changed. If a manual "
            "command GAINED it, that is the #127-family fix and this assertion should "
            f"widen deliberately. Found: {sorted(consults_channel)}"
        )

    def test_an_explicit_top_level_default_still_wins_AT_THE_COMMAND(self, loaded_prompts):
        cfg = dict(NO_KEY_CONFIG, default_prompt="mindmap-heavy")
        with pytest.raises(BaseException):  # noqa: B017
            vi.cmd_mindmap(_args(), cfg)
            raise SystemExit(0)
        assert _mindmap_names(loaded_prompts)[:1] == ["mindmap-heavy"]

    def test_the_cli_prompt_flag_still_wins_AT_THE_COMMAND(self, loaded_prompts):
        cfg = dict(NO_KEY_CONFIG, default_prompt="mindmap-heavy")
        with pytest.raises(BaseException):  # noqa: B017
            vi.cmd_mindmap(_args(prompt="mindmap-light"), cfg)
            raise SystemExit(0)
        assert _mindmap_names(loaded_prompts)[:1] == ["mindmap-light"], (
            "the --prompt flag was ignored - it is the operator's last override"
        )

    def test_mindmap_light_is_still_a_real_template(self):
        """The fallback moved; the template did not go away."""
        assert (REPO / "prompts" / "mindmap-light.md").is_file()


class TestThePreflightAgreesWithAWriter:
    """The load-bearing half, and the first cut could not test it: line 125
    re-derived the preflight expression inline, so reverting the constant to
    `"mindmap-light"` - the exact pre-#210 defect - left both tests passing."""

    def test_the_preflight_resolves_what_mindmap_actually_loads(self, loaded_prompts, monkeypatch):
        """Both sides derived independently, then compared. The preflight side
        comes from `validate_channel_knobs`'s OWN behavior (it reports no
        problem only when the name it resolved is loadable); the writer side is
        captured at the real `load_prompt` call."""
        channel = dict(NO_KEY_CONFIG["channels"][0])
        problems = vi.validate_channel_knobs(channel, dict(NO_KEY_CONFIG))
        assert not [p for p in problems if p[0] == "prompt"], (
            f"the preflight rejects the default it is supposed to predict: {problems}"
        )
        with pytest.raises(BaseException):  # noqa: B017
            vi.cmd_mindmap(_args(), dict(NO_KEY_CONFIG))
            raise SystemExit(0)
        loaded = _mindmap_names(loaded_prompts)[0]
        assert vi.resolve_prompt_path(loaded).is_file(), (
            f"the writer loaded {loaded!r}, which the preflight's own resolver cannot find"
        )

    def test_the_preflight_rejects_a_name_the_writer_could_not_load(self):
        """The other direction: the two must agree about FAILURE too."""
        channel = {"name": "alpha", "url": "u", "prompt": "mindmap-imaginary"}
        problems = vi.validate_channel_knobs(channel, {})
        assert [p for p in problems if p[0] == "prompt"], "the preflight missed an unresolvable prompt"
        assert not vi.resolve_prompt_path("mindmap-imaginary").is_file()


class TestDocsMatchTheCode:
    """Issue #204's docs-currency suite recorded this ambiguity as deferred and
    pointed here."""

    DOCS = ("README.md", "INSTALLATION.md", "skills/video-intel/SKILL.md")
    # Every `mindmap-*` token within a window of a `default_prompt` mention.
    # The first cut used `default_prompt.{0,80}?(mindmap-[a-z]+)` - non-greedy
    # and forward-only, so it locked onto the FIRST token and never looked
    # further. A reviewer appended "...the scan command and mindmap --url still
    # fall back to mindmap-light for speed" to the README row and all 14 tests
    # passed, because `mindmap-knowledge` appeared earlier in the same row.
    # LINE-scoped, not a character window. A 400-character window reached
    # README's legitimate list of prompt templates from the unrelated row
    # `| prompt | No | Override \`default_prompt\` |`, flagging
    # `mindmap-light` as stale when it is just a template being listed.
    # The evasion this guard exists to catch was appended to the SAME row,
    # so a line is both sufficient and free of that false positive - and a
    # guard that cries wolf gets its exclusions widened until it guards
    # nothing.
    WINDOW = re.compile(r"^.*default_prompt.*$", re.M)
    TOKEN = re.compile(r"mindmap-[a-z-]+")

    def _tokens_near_default_prompt(self, text: str) -> list[str]:
        return [t for w in self.WINDOW.findall(text) for t in self.TOKEN.findall(w)]

    def test_no_living_doc_names_a_prompt_other_than_the_constant_near_default_prompt(self):
        offenders = []
        for doc in self.DOCS:
            for token in self._tokens_near_default_prompt((REPO / doc).read_text(encoding="utf-8")):
                if token != vi.DEFAULT_PROMPT_NAME:
                    offenders.append(f"{doc}: {token}")
        assert not offenders, (
            "a living doc names a prompt other than the default near a `default_prompt` "
            f"mention, which is how a stale fallback claim survives: {offenders}"
        )

    def test_the_doc_scan_is_not_vacuous(self):
        """Zero matches produces zero offenders, which passes. A reviewer
        renamed `default_prompt` to `defaultPromptRenamed` across all three
        docs and the check above passed - the exact class CLAUDE.md's #204
        item 8 and the standing tautology rule police."""
        found = {doc: self._tokens_near_default_prompt((REPO / doc).read_text(encoding="utf-8")) for doc in self.DOCS}
        assert all(found.values()), (
            f"the doc scan found no prompt token near `default_prompt` in: "
            f"{[d for d, t in found.items() if not t]} - it has stopped matching"
        )

    def test_the_readme_row_names_the_real_default(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        row = [ln for ln in readme.splitlines() if ln.strip().startswith("| default_prompt ")]
        assert row, "the README no longer documents default_prompt"
        assert vi.DEFAULT_PROMPT_NAME in row[0], f"the README row is stale: {row[0]}"
