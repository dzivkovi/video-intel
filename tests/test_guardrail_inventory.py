"""No guardrail may stop loading (issue #242).

`CLAUDE.md` reached 66,408 tokens - a third of a 200k context window, loaded
before any work starts - with 79.7% of it in one section. Three Codex peer
reviews failed in a single night, two of them by reading this file until their
budget was gone. The fix moves per-subsystem guardrails into `.claude/rules/*.md`,
which auto-load when a session touches a matching path.

**That fix has a silent failure mode**, named in `work/2026-07-25/01-doctor-
cleanup-verification.md` when the same move was made for two earlier files:

> If that assumption were wrong, the guardrails would have been deleted from the
> only place they loaded, with no error, no warning, and no symptom until some
> future PR quietly broke an invariant nobody was reminded of.

That note verified the mechanism empirically (reading `scripts/burst_report.py`
in a live session injected `.claude/rules/intelligence-layer.md` in full). What
it did not leave behind was a standing check. This file is that check.

The manifest at `tests/guardrail_manifest.json` records all 59 guardrails as
they stood before the split. Every one must still resolve: present in the core
file, present in a rule file, or explicitly accounted for. A slug that resolves
nowhere is a protection that was deleted rather than moved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "CLAUDE.md"
RULES_DIR = ROOT / ".claude" / "rules"
MANIFEST = Path(__file__).with_name("guardrail_manifest.json")

#: The core file's whole purpose is to be cheap to load. The pre-split file was
#: 274,900 chars; this budget is a little over a quarter of that. Chars rather
#: than a tokenizer so the check needs no dependency and cannot drift with one.
CORE_MAX_CHARS = 80_000


def _normalize(text: str) -> str:
    """Strip markdown emphasis and collapse whitespace.

    BOTH sides of the resolution check go through this. The first cut
    normalized only the probe, so a title recorded as ``prompt == 0 is a
    refusal`` never matched the file's ``**`prompt == 0` is a refusal``, and 12
    healthy guardrails were reported as deleted. A one-sided normalizer is a
    false-alarm generator, which is the one thing this test must never be.
    """
    return re.sub(r"\s+", " ", text.replace("`", "").replace("*", "")).strip().lower()


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _rule_files() -> list[Path]:
    return sorted(RULES_DIR.glob("*.md")) if RULES_DIR.exists() else []


def _all_guardrail_text() -> str:
    """Every place a guardrail is allowed to live, normalized once."""
    parts = [CORE.read_text(encoding="utf-8")]
    parts += [p.read_text(encoding="utf-8") for p in _rule_files()]
    return _normalize("\n".join(parts))


def _probe_phrase(title: str) -> str:
    """A distinctive, normalized substring of a guardrail's title."""
    return _normalize(title)[:44]


def _frontmatter(path: Path) -> dict:
    import yaml

    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    return yaml.safe_load(text[3:end]) or {}


class TestEveryGuardrailStillResolves:
    """The load-bearing check: nothing was deleted by accident."""

    def test_the_manifest_matches_the_recorded_baseline(self):
        m = _manifest()
        assert len(m["guardrails"]) == m["baseline_bullets"] == 59

    def test_every_guardrail_appears_somewhere_it_can_load(self):
        """A slug resolves if a distinctive phrase from its title appears in the
        core file or in any rule file. The manifest may mark a slug
        `consolidated_into` (a repeated re-derivation folded into one canonical
        statement) or `moved_to` (narrative relocated to docs/), and then the
        TARGET must resolve instead - so a chain still ends somewhere real.
        """
        haystack = _all_guardrail_text()
        m = _manifest()
        by_slug = {g["slug"]: g for g in m["guardrails"]}
        unresolved = []

        for g in m["guardrails"]:
            target = g.get("consolidated_into")
            if target:
                assert target in by_slug, f"{g['slug']} consolidates into unknown slug {target}"
                continue  # the target's own resolution is checked on its own row
            moved = g.get("moved_to")
            if moved:
                assert (ROOT / moved).exists(), f"{g['slug']} moved_to missing file {moved}"
                continue
            probe = _probe_phrase(g["title"])
            if probe and probe not in haystack:
                unresolved.append((g["slug"], probe))

        assert not unresolved, "these guardrails resolve nowhere - they were deleted, not moved:\n" + "\n".join(
            f"  {s}: looked for {p!r}" for s, p in unresolved
        )

    def test_the_resolution_check_is_not_vacuous(self):
        """A probe that matched everything would make the test above pass no
        matter what was deleted. Two properties keep it honest: a phrase that
        was never written must NOT be found, and the probes must be distinct
        enough that losing one guardrail is detectable."""
        haystack = _all_guardrail_text()
        assert "a guardrail that was never written" not in haystack
        probes = [_probe_phrase(g["title"]) for g in _manifest()["guardrails"]]
        assert all(probes), "some guardrail titles produced no usable probe phrase"
        assert len(set(probes)) > 40, "probes are not distinctive enough to detect a loss"


class TestTheCoreStaysCheapToLoad:
    def test_claude_md_is_under_budget(self):
        size = len(CORE.read_text(encoding="utf-8"))
        assert size <= CORE_MAX_CHARS, (
            f"CLAUDE.md is {size:,} chars, over the {CORE_MAX_CHARS:,} budget. "
            "It loads on every session; move per-subsystem guardrails into "
            ".claude/rules/*.md rather than raising this number."
        )

    def test_it_actually_shrank(self):
        m = _manifest()
        now, before = len(CORE.read_text(encoding="utf-8")), m["baseline_chars"]
        assert now < before, f"CLAUDE.md is {now:,} chars, not smaller than the {before:,} baseline"


class TestARuleFileCanActuallyLoad:
    """A rule scoped to a path that does not exist never loads, and nothing
    reports it. That is the silent failure this whole ticket is about, one layer
    down."""

    def test_there_are_rule_files(self):
        assert _rule_files(), "no .claude/rules/*.md found; the split did not happen"

    @pytest.mark.parametrize("path", [p.name for p in _rule_files()] or ["<none>"])
    def test_each_rule_file_declares_paths(self, path):
        if path == "<none>":
            pytest.skip("no rule files")
        fm = _frontmatter(RULES_DIR / path)
        assert fm.get("paths"), f"{path} has no `paths:` frontmatter, so it can never auto-load"

    @pytest.mark.parametrize("path", [p.name for p in _rule_files()] or ["<none>"])
    def test_every_declared_path_matches_something_real(self, path):
        """A glob matching nothing is a rule that can never fire."""
        if path == "<none>":
            pytest.skip("no rule files")
        fm = _frontmatter(RULES_DIR / path)
        dead = [g for g in (fm.get("paths") or []) if not list(ROOT.glob(g))]
        assert not dead, f"{path} declares globs that match no file: {dead}"


class TestEveryNamedTestContractExists:
    """Pre-split this was 0 misses across 59 bullets. It must stay 0: a rule
    pointing at a test file that does not exist is a rule nobody can verify."""

    def test_no_named_test_file_is_missing(self):
        text = "\n".join([CORE.read_text(encoding="utf-8")] + [p.read_text(encoding="utf-8") for p in _rule_files()])
        named = sorted({m for m in re.findall(r"tests/[A-Za-z0-9_/]+\.py", text)})
        assert len(named) >= 50, f"only {len(named)} test contracts found; the text looks truncated"
        missing = [t for t in named if not (ROOT / t).exists()]
        assert not missing, f"guardrails name test files that do not exist: {missing}"
