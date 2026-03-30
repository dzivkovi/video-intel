# Developer Tooling, Specs, and Compound Engineering Adoption

**Status:** accepted

**Date:** 2026-03-30

**Decision Maker(s):** Daniel

## Context

Video-intel grew from a single-script prototype to a structured project with ARCHITECTURE.md, 7 ADRs, and INSTALLATION.md. However, it lacked developer tooling conventions that had been proven in the brand-arbiter project:

- No `specs/agent-rules.md` — agent coding standards were implicit, not enforceable
- No `pyproject.toml` — no ruff config, no pytest config, no formal Python project definition
- No tests — 5 pure utility functions had no coverage
- No Compound Engineering integration — no plansDirectory, no docs/solutions/, no workflow documentation
- No GitHub Issues policy — no defined backlog system

## Decision

Adopt proven patterns from brand-arbiter, adapted for video-intel's structure as a skill project:

1. **`specs/agent-rules.md`**: Personal engineering rules (minimalism, TDD, ruff, type hints, agentic git hygiene) with video-intel-specific Section 5 (architectural authority referencing ARCHITECTURE.md, Gemini-as-proxy boundary, skill-only Gemini work).

2. **`pyproject.toml`**: Ruff config (line-length=120, py312), pytest config (testpaths=tests, pythonpath=scripts), optional dev dependencies.

3. **`tests/test_utils.py`**: 23 tests covering `slugify()`, `timestamp_to_seconds()`, `parse_since()`, `video_file_prefix()`, `merge_transcript_json()` — all pure functions requiring no API mocking.

4. **Compound Engineering (minus file-based todos)**: `plansDirectory` in `.claude/settings.json`, `docs/solutions/` directory, workflow documentation in CLAUDE.md. GitHub Issues for backlog instead of `todos/` directory.

5. **CLAUDE.md additions**: System Instructions (specs reference), Backlog (GitHub Issues policy), Development commands (pip install, pytest, ruff), Workflows section (CE commands and conventions).

## Consequences

### Positive Consequences

- Agent coding standards are explicit and enforceable via CLAUDE.md → specs reference
- Pure functions have regression protection (23 tests, 0.12s runtime)
- Ruff modernized `timezone.utc` → `datetime.UTC` across the script (Python 3.12+)
- CE workflows documented for structured task execution
- GitHub Issues provides transparent, linkable backlog visible to contributors

### Negative Consequences

- More files to maintain (specs/, tests/, pyproject.toml, docs/solutions/)
- File-based todos pattern (proven in brand-arbiter) explicitly excluded — GitHub Issues lacks the filesystem-as-kanban ergonomics

## Alternatives Considered

- **File-based todos (brand-arbiter pattern):** Rejected — too much maintenance overhead for a solo skill project. GitHub Issues provides equivalent traceability with less friction.
- **No pyproject.toml (keep using bare pip):** Rejected — ruff and pytest need config, and `pip install -e ".[dev]"` is cleaner than a separate requirements-dev.txt.
- **Copy brand-arbiter's agent-rules.md verbatim:** Rejected — Section 5 references brand-arbiter-specific architecture (dual-track, Track A/B). Adapted Section 5 for video-intel's Gemini-proxy boundary.

## Affects

- `specs/agent-rules.md` (new)
- `specs/README.md` (new)
- `pyproject.toml` (new)
- `tests/conftest.py` (new)
- `tests/test_utils.py` (new)
- `docs/solutions/README.md` (new)
- `plans/.gitkeep` (new)
- `.claude/settings.json` (added plansDirectory)
- `CLAUDE.md` (added System Instructions, Backlog, Development, Workflows)
- `.gitignore` (added plans/*.md pattern)
- `scripts/video_intel.py` (ruff auto-fix: `timezone.utc` → `datetime.UTC`)

## Related Debt

- GitHub Issue: Add pytest-cov to CI when GitHub Actions is set up
- GitHub Issue: Add type hints to all public functions in video_intel.py (per agent-rules.md Section 2)

## Research References

- Brand-arbiter `specs/agent-rules.md` — canonical source for personal engineering rules
- [EveryInc/compound-engineering-plugin](https://github.com/EveryInc/compound-engineering-plugin/) — CE plugin
- Brand-arbiter `docs/solutions/process-issues/documentation-drift-three-bucket-rule.md` — three-bucket documentation governance
