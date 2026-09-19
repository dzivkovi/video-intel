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
import pathlib
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


#: The file each rule's content is ABOUT. Declaring globs that match something
#: is not the same as declaring the trigger that makes the rule fire when its
#: own subject is edited - a reviewer falsified the weaker check by replacing
#: search-index.md's entire path list with README.md, and all 27 cases still
#: passed while the rule had stopped firing for `scripts/video_intel.py`.
REQUIRED_TRIGGERS = {
    "transcript.md": ["scripts/video_intel.py", "scripts/youtube_captions.py"],
    "scan-config.md": ["scripts/video_intel.py"],
    "search-index.md": ["scripts/video_intel.py"],
    "evals.md": ["scripts/model_eval.py"],
    "briefings.md": ["scripts/video_intel.py"],
    "docs-currency.md": ["tests/test_docs_currency.py", "README.md"],
    "intelligence-layer.md": ["scripts/intel_graph.py"],
    "translate-bcs.md": ["scripts/translate_video.py"],
}


class TestEachRuleFiresForTheCodeItGoverns:
    """The gap between "this glob matches a file" and "this rule loads when its
    own subject is edited".

    A rule about the transcript path that lists only test files never fires
    during a transcript edit. Nothing reports that; the rule simply stops
    protecting anything, which is the silent-deletion failure this whole ticket
    exists to prevent, one layer down.
    """

    def test_the_trigger_map_covers_every_rule_file(self):
        """A new rule file must be classified, exactly like CONFIG_BACKUP_COMMANDS.
        Without this, adding one silently opts it out of the coverage check."""
        on_disk = {p.name for p in _rule_files()}
        assert on_disk == set(REQUIRED_TRIGGERS), (
            f"rule files without a declared trigger: {sorted(on_disk - set(REQUIRED_TRIGGERS))}; "
            f"triggers naming no rule file: {sorted(set(REQUIRED_TRIGGERS) - on_disk)}"
        )

    @pytest.mark.parametrize("name", sorted(REQUIRED_TRIGGERS))
    def test_the_rule_declares_a_path_matching_its_own_subject(self, name):
        rule = RULES_DIR / name
        if not rule.exists():
            pytest.skip(f"{name} not present")
        globs = _frontmatter(rule).get("paths") or []
        for required in REQUIRED_TRIGGERS[name]:
            covered = any(required == g or pathlib.PurePath(required).match(g) for g in globs)
            assert covered, (
                f"{name} declares no path matching {required}, the file its own content "
                f"governs, so it will not load when that file is edited. Declared: {globs}"
            )


class TestEveryNamedTestContractExists:
    """Pre-split this was 0 misses across 59 bullets. It must stay 0: a rule
    pointing at a test file that does not exist is a rule nobody can verify."""

    def test_no_named_test_file_is_missing(self):
        text = "\n".join([CORE.read_text(encoding="utf-8")] + [p.read_text(encoding="utf-8") for p in _rule_files()])
        named = sorted({m for m in re.findall(r"tests/[A-Za-z0-9_/]+\.py", text)})
        assert len(named) >= 50, f"only {len(named)} test contracts found; the text looks truncated"
        missing = [t for t in named if not (ROOT / t).exists()]
        assert not missing, f"guardrails name test files that do not exist: {missing}"


class TestTheCitationMechanismHolds:
    """Six cross-cutting principles are stated once in the core and cited by
    tag from the rule files, so a per-feature entry keeps the case-specific
    WHAT and drops the re-derivation of WHY.

    A citation pointing at a tag that does not exist is a dangling reference
    in a binding rules file: the reader is told to go somewhere that has
    nothing. That is the failure this guards.
    """

    #: A tag is short and never spans a line. The first cut used
    #: ``[^\]]+`` which crossed newlines and swallowed whole paragraphs when
    #: a bracket went unclosed, so the check reported nonsense instead of
    #: dangling references.
    TAG_RE = r"\[core: ([^\]\n]{1,24})\]"

    #: A tag at the HEAD of a bullet defines the principle. The same tag
    #: appearing inline is a citation, and the core is allowed to cite itself -
    #: probe-before-pay cites real-caller for the ordering-test rule. Conflating
    #: the two made "defined exactly once" fail on a legitimate cross-reference.
    DEFINE_RE = r"(?m)^- \*\*\[core: ([^\]\n]{1,24})\]"

    def _core_tags(self) -> set[str]:
        """Tags DEFINED in the core, not merely mentioned."""
        return set(re.findall(self.DEFINE_RE, CORE.read_text(encoding="utf-8")))

    def test_the_core_defines_the_six_principles(self):
        expected = {
            "writer's-path",
            "real-caller",
            "identity #66",
            "one-definition",
            "probe-before-pay",
            "#124 read-guard",
        }
        assert self._core_tags() == expected, (
            "the core's principle tags changed. Adding one is fine, but every "
            "rule-file citation of a removed tag becomes a dangling reference."
        )

    def test_every_citation_resolves_to_a_real_tag(self):
        defined = self._core_tags()
        dangling = []
        for rf in _rule_files():
            for tag in re.findall(self.TAG_RE, rf.read_text(encoding="utf-8")):
                if tag not in defined:
                    dangling.append((rf.name, tag))
        assert not dangling, f"citations naming no core principle: {dangling}"

    def test_a_tag_is_defined_exactly_once(self):
        """Two canonical statements of one principle is the disease this whole
        consolidation treats, one level up. Counts DEFINITIONS only: a core
        bullet citing another core principle inline is correct and expected."""
        tags = re.findall(self.DEFINE_RE, CORE.read_text(encoding="utf-8"))
        dupes = {t for t in tags if tags.count(t) > 1}
        assert not dupes, f"a principle is DEFINED more than once in the core: {sorted(dupes)}"

    def test_the_core_cites_itself_at_least_once(self):
        """Companion, so the definition-vs-citation distinction is not vacuous:
        if the core stopped citing itself, `DEFINE_RE` and a plain tag search
        would agree and the distinction above would be untested."""
        defined = len(re.findall(self.DEFINE_RE, CORE.read_text(encoding="utf-8")))
        mentioned = len(re.findall(self.TAG_RE, CORE.read_text(encoding="utf-8")))
        assert mentioned > defined, (
            "no core bullet cites another core principle, so the "
            "definition-versus-citation distinction is currently unexercised"
        )


#: Phrases that point at a neighbour by POSITION rather than by name. They were
#: all correct inside one monolithic file and several broke the moment the file
#: was split - the citing bullet kept its text verbatim while the bullet it
#: pointed at moved to another file, so the line-based inverse check saw nothing.
POSITIONAL_REFERENCES = (
    "entry above",
    "entry below",
    "rule above",
    "rule below",
    "guardrail above",
    "guardrail below",
    "bullet above",
    "bullet below",
    "the entry below",
    "the section above",
)


class TestNoGuardrailPointsAtANeighbourByPosition:
    """A rule file is read in isolation, so "above" and "below" are meaningless
    in it and actively misleading once a bullet moves.

    Five of these shipped broken in the first cut of this split, found by an
    adversarial review rather than by any check - including one in the core
    pointing at a guardrail that had moved into a rule file Codex never loads.
    Reference a rule by name and file, never by position.
    """

    def test_no_rule_file_uses_a_positional_reference(self):
        offenders = []
        for rf in _rule_files():
            text = _normalize(rf.read_text(encoding="utf-8"))
            for phrase in POSITIONAL_REFERENCES:
                if phrase in text:
                    offenders.append(f"{rf.name}: {phrase!r}")
        assert not offenders, (
            "positional references in a path-scoped rule file, which is read in "
            "isolation - name the rule and its file instead:\n  " + "\n  ".join(offenders)
        )

    def test_the_core_does_not_point_below_at_something_that_moved(self):
        """The core keeps 14 bullets; anything it points at "below" must still be
        one of them. This is the instance that mattered most: the core is what a
        reviewer that does not auto-load rule files actually reads."""
        text = _normalize(CORE.read_text(encoding="utf-8"))
        offenders = [p for p in ("guardrail below", "entry below", "rule below") if p in text]
        assert not offenders, (
            f"the core points at {offenders} - if the target moved into a rule file, "
            "a reviewer reading only CLAUDE.md follows it to nothing"
        )

    def test_the_scan_detects_a_positional_reference_when_one_exists(self):
        """Companion, so a scan that silently matched nothing could not pass.

        The first cut asserted that these phrases appear somewhere in `docs/`,
        which was a claim about an unrelated corpus and simply false. The
        property that matters is that the DETECTOR fires, so it is tested
        directly on synthetic text.
        """
        synthetic = _normalize("See the quality-assessor guardrail below for the rest.")
        assert any(p in synthetic for p in POSITIONAL_REFERENCES)
        clean = _normalize("See the quality-assessor guardrail in `.claude/rules/transcript.md`.")
        assert not any(p in clean for p in POSITIONAL_REFERENCES)
