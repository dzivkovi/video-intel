"""The docs must not drift from the CLI (issue #204).

A documentation audit on 2026-09-02 found four claims that would make an agent
following the docs literally do the wrong thing: a model default two versions
stale, a chunk default that issue #157 lowered on purpose, an eval command
CLAUDE.md explicitly forbids, and a broken ADR link. Every one of them was
mechanically checkable and nothing was checking.

These tests are that check. They are deliberately cheap - no Gemini call, no
network, no corpus read - because a guard that costs money to run stops being
run. `--help` exits before any side effect, and the link check is a stat.

Scope note: `docs/plans/`, `docs/adr/` and `docs/brainstorms/` are historical
session artifacts under the repo's three-bucket rule, so they are NOT swept.
A plan that recorded the model of its day is correct as history and must not be
rewritten to match today's constant.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "video_intel.py"

# The LIVING docs: what a user or agent reads to decide what to run.
LIVING_DOCS = [
    "README.md",
    "INSTALLATION.md",
    "docs/translate-bcs.md",
    "docs/testing.md",
    "docs/troubleshooting.md",
    "skills/video-intel/SKILL.md",
    "skills/video-intel-search/SKILL.md",
    "skills/translate-bcs/SKILL.md",
]


def _source() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _registered_subcommands() -> set[str]:
    """Every subcommand argparse knows about.

    Anchored to `add_parser` rather than to the dispatch chain, for the same
    reason `tests/test_config_backup.py` is: a subcommand cannot exist without
    registering here, which makes the inventory undodgeable.
    """
    return set(re.findall(r'subparsers\.add_parser\(\s*"([a-z-]+)"', _source()))


def _constant(name: str) -> str:
    m = re.search(rf'^{name}\s*=\s*"?([^"\n]+)"?', _source(), re.M)
    assert m, f"{name} not found in {SCRIPT.name}"
    return m.group(1).strip()


# Global flags that sit BEFORE the subcommand and consume the next token.
_VALUE_TAKING_GLOBALS = {"--model", "-m", "--log-level"}


def _documented_subcommands() -> dict[str, set[str]]:
    """Every subcommand the living docs actually TELL a reader to run.

    This is the docs -> CLI direction, and it is the whole point of the class
    below. Deriving the cases from `_registered_subcommands()` instead reads
    the CLI -> CLI direction: a README that says `video_intel.py scna` fails
    nothing, because the case list never came from the README. Codex caught
    exactly that on PR #208 - the test was named for a contract it did not
    have.

    Returns {subcommand: {docs that mention it}} so a failure names the file.
    """
    found: dict[str, set[str]] = {}
    for doc in LIVING_DOCS:
        for line in (REPO / doc).read_text(encoding="utf-8").splitlines():
            if "video_intel.py" not in line:
                continue
            tokens = line.split("video_intel.py", 1)[1].split()
            skip_next = False
            for tok in tokens:
                if skip_next:
                    skip_next = False
                    continue
                if tok in _VALUE_TAKING_GLOBALS:
                    skip_next = True
                    continue
                if tok.startswith("-"):
                    continue
                if re.fullmatch(r"[a-z][a-z-]*", tok):
                    found.setdefault(tok, set()).add(doc)
                break
    return found


class TestEveryDocumentedCommandParses:
    """The one test a docs change can actually run.

    Two directions, and only the first is about the docs:

    * docs -> CLI: every command a living doc tells the reader to run must be
      a command the CLI has. This is what catches a typo'd or retired
      invocation in a README.
    * CLI -> parser: every registered subcommand's `--help` must exit 0. This
      catches an argparse definition that raises on construction.
    """

    def test_every_documented_command_is_a_real_subcommand(self):
        documented = _documented_subcommands()
        registered = _registered_subcommands()
        unknown = {c: sorted(d) for c, d in documented.items() if c not in registered}
        assert not unknown, f"living docs invoke commands the CLI does not have: {unknown}"

    def test_the_doc_extraction_is_not_vacuous(self):
        """Companion, per the guard-test rule: an extractor that silently
        stops matching turns the test above into `assert not {}` forever. Pin
        the hard instances - a bare command, one behind a global `--model`
        flag, and a two-word `profile show` - not merely a count.
        """
        documented = _documented_subcommands()
        for expected in ("scan", "transcript", "process", "search", "index", "profile"):
            assert expected in documented, f"the doc walk stopped finding `{expected}`"
        assert len(documented) >= 8, f"the doc walk found only {sorted(documented)}"

    @pytest.mark.parametrize("cmd", sorted(_registered_subcommands()))
    def test_subcommand_help_exits_zero(self, cmd):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), cmd, "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        assert r.returncode == 0, f"`{cmd} --help` exited {r.returncode}: {r.stderr[-400:]}"

    def test_the_registry_is_not_empty(self):
        """Companion against a vacuous parametrization: if the `add_parser`
        regex ever stops matching, the test above silently becomes zero cases
        and passes forever. Same tautology class as the issue #182 field walk.
        """
        assert len(_registered_subcommands()) >= 15


class TestNoStaleDefaultsInLivingDocs:
    """The two defaults issue #204 found stale, pinned to the real constants."""

    def test_no_doc_names_a_model_other_than_the_real_default(self):
        default = _constant("DEFAULT_MODEL")
        assert default == "gemini-3.7-flash", "update this test with the new measured default"
        # NO exclusion list, deliberately. The first cut of this test skipped
        # lines containing "measured"/"scorecard"/" vs " on the theory that A/B
        # prose legitimately names the model that lost - and that exclusion made
        # the README's own model row immune, because the row cites the
        # scorecards. Falsification caught it: an injected stale-model row
        # passed. Zero living docs need to name the superseded model at all (the
        # A/B evidence lives in docs/plans and tests/evals/model-cards, which are
        # historical and not swept here), so the check is absolute. If a living
        # doc ever genuinely needs to name a superseded model, narrow this
        # deliberately and RE-FALSIFY - never widen a blanket skip.
        offenders = [
            f"{doc}:{lineno}"
            for doc in LIVING_DOCS
            for lineno, line in enumerate((REPO / doc).read_text(encoding="utf-8").split("\n"), 1)
            if "gemini-3-flash-preview" in line
        ]
        assert not offenders, f"stale model default in living docs: {offenders}"

    def test_the_cli_help_text_names_the_real_default(self):
        """The `--model` help string is itself documentation, and it was two
        versions stale. Built from `DEFAULT_MODEL` now, so it cannot drift."""
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, cwd=str(REPO))
        assert r.returncode == 0
        assert _constant("DEFAULT_MODEL") in r.stdout
        assert "gemini-3-flash-preview" not in r.stdout

    def test_no_doc_offers_the_old_fifty_minute_chunk_default(self):
        """Issue #157 lowered the default from 50 to 30 because a 50-minute
        chunk let the seed case fold back into an effectively single-shot call.
        A doc that restates 50 as the default, or offers `--chunk-minutes 50`
        as the example value, walks an agent straight back into that shape.
        """
        assert int(_constant("TRANSCRIPT_CHUNK_MINUTES_DEFAULT")) == 30
        # Scan a WINDOW, not a single line. The first cut required
        # "chunk-minutes" and "50" on the SAME line, and an accuracy reviewer
        # found a live stale pair it missed: prose wrapped so that
        # "then the default (50)." and "(default: 50)." each sat on a line
        # carrying no "chunk-minutes" text of its own. The suite passed 24/24
        # over a real defect - the exact false negative a currency guard
        # cannot afford, and the reason this check is window-based now.
        offenders = []
        for doc in LIVING_DOCS:
            lines = (REPO / doc).read_text(encoding="utf-8").split("\n")
            for lineno, line in enumerate(lines, 1):
                if not re.search(r"\(\s*(?:default:?\s*)?50\s*\)|\bdefault:?\s+50\b", line, re.I):
                    continue
                # A historical diagnosis may name the threshold of its day, as
                # long as it says so.
                if "at the time" in line:
                    continue
                # Only a chunk-minutes context is in scope; an unrelated
                # "default 50" elsewhere in these docs is not this bug.
                window = "\n".join(lines[max(0, lineno - 4) : lineno + 3])
                if re.search(r"chunk[- _]minutes", window, re.I):
                    offenders.append(f"{doc}:{lineno}: {line.strip()}")
        assert not offenders, f"stale chunk default in living docs: {offenders}"


class TestNoDocTeachesTheForbiddenEvalCommand:
    """CLAUDE.md: run the eval module BY NAME, never the directory, or the
    instrument's deliberate failures share the summary line and the N/25 stops
    being derivable. Three living docs taught the directory form anyway."""

    def test_no_living_doc_runs_the_evals_directory(self):
        offenders = []
        for doc in LIVING_DOCS:
            for lineno, line in enumerate((REPO / doc).read_text(encoding="utf-8").split("\n"), 1):
                if re.search(r"pytest\s+tests/evals/\s", line):
                    offenders.append(f"{doc}:{lineno}: {line.strip()}")
        assert not offenders, f"directory-wide eval command in living docs: {offenders}"


class TestRelativeLinksResolve:
    """A broken relative link is a silent dead end, and moving a section
    between directories breaks every root-relative link inside it - which is
    exactly what happened when the BCS section moved from README into docs/."""

    def test_every_relative_link_in_the_living_docs_exists(self):
        offenders = []
        for doc in LIVING_DOCS:
            p = REPO / doc
            for m in re.finditer(r"\[[^\]]+\]\(([^)\s]+)\)", p.read_text(encoding="utf-8")):
                target = m.group(1)
                if target.startswith(("http://", "https://", "#", "mailto:")):
                    continue
                if target == "url":  # prose ABOUT markdown syntax, not a link
                    continue
                path_part = target.split("#", 1)[0]
                if not path_part:
                    continue
                if not (p.parent / path_part).resolve().exists():
                    offenders.append(f"{doc} -> {target}")
        assert not offenders, f"broken relative links: {offenders}"


class TestSkillCountCannotDrift:
    """Issue #204's headline finding was a README section claiming TWO skills
    when the plugin ships three. The first cut of this PR fixed the "Plugin
    Contents" table and left an identical claim 600 lines later in
    "Cross-Platform Compatibility" - found by a standards reviewer, not by any
    test, because nothing asserted skill-count consistency. Now something does.
    """

    def _skill_dirs(self) -> set[str]:
        return {p.parent.name for p in (REPO / "skills").glob("*/SKILL.md")}

    def test_the_manifest_and_the_filesystem_agree(self):
        """The manifest does not declare skills today - Claude Code discovers
        them from `skills/*/SKILL.md` - and the first cut of this test hid that
        behind `manifest.get("skills", []) or self._skill_dirs()`, which
        compared `_skill_dirs()` with itself and could never fail. Codex caught
        it on PR #208. So: branch explicitly, and pin the no-declaration state
        rather than falling back to a self-comparison.
        """
        import json

        manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        on_disk = self._skill_dirs()
        assert on_disk, "no skills/*/SKILL.md found - the discovery source itself is broken"

        if "skills" not in manifest:
            # Nothing to cross-check. Assert the premise so a manifest that
            # STARTS declaring skills reaches the comparison below instead of
            # silently taking a vacuous path.
            assert set(manifest) == {"name", "version", "description", "author"}, (
                "plugin.json grew a key; if it now declares skills, this test must compare them"
            )
            return

        declared = {s.rstrip("/").split("/")[-1] for s in manifest["skills"]}
        assert declared == on_disk, "plugin.json and skills/ disagree about which skills exist"

    def test_no_living_doc_claims_the_wrong_number_of_skills(self):
        """The count is spelled out in prose ("two independent skills", "the
        three SKILL.md files"), so pin the WORD against the real count rather
        than hoping a grep for a stale table header catches it."""
        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}
        actual = len(self._skill_dirs())
        wrong = {w for n, w in words.items() if n != actual}
        alternation = "|".join(sorted(wrong))
        noun = r"(?:independent skills|skills|`?SKILL\.md`? files)"
        # Only letters, spaces, hyphens and backticks may sit between the count
        # word and the noun. A looser gap matched "two-command summary;
        # [`skills/translate-bcs/SKILL.md`]" - a real false positive this
        # test produced on its first run.
        pattern = re.compile(r"\b(?:" + alternation + r")[A-Za-z `-]{0,25}?\b" + noun, re.I)
        offenders = []
        for doc in LIVING_DOCS:
            for lineno, line in enumerate((REPO / doc).read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{doc}:{lineno}: {line.strip()[:120]}")
        assert not offenders, f"a living doc claims the wrong skill count (real count {actual}): {offenders}"

    def test_every_skill_is_named_in_the_entry_point_docs(self):
        """A count alone is not enough. The omitted skill was
        `video-intel-search`, and fixing only the NUMBER left INSTALLATION's
        opening blurb saying "Three skills" while still listing two by name -
        which is how a count-only check would have let this ship."""
        for doc in ("README.md", "INSTALLATION.md"):
            text = (REPO / doc).read_text(encoding="utf-8")
            missing = [d for d in sorted(self._skill_dirs()) if d not in text]
            assert not missing, f"skills shipped but never named in {doc}: {missing}"


class TestSkillSurfaceMatchesTheCliSurface:
    """Skill-parity, mechanically. Every subcommand a user could be told to
    run should be reachable from the docs, not just from `--help`."""

    def test_every_subcommand_is_named_somewhere_in_the_living_docs(self):
        registered = _registered_subcommands()
        text = "\n".join((REPO / d).read_text(encoding="utf-8") for d in LIVING_DOCS)
        named = {
            cmd for cmd in registered if re.search(rf"video_intel\.py\s+(?:--\S+\s+\S+\s+)*{re.escape(cmd)}\b", text)
        }
        assert not (registered - named), f"subcommands documented nowhere: {sorted(registered - named)}"
