# Video Intel: AI Developer Rules of Engagement

## 1. Core Engineering Philosophy
* **Minimalism First:** Implement only the absolute minimum code required to pass the test. Prefer updating existing files over creating new ones.
* **Modern Python (3.12+):** Rely heavily on standard libraries, `dataclasses`, `Enum`, and strict type hinting.
* **Local-First & Deterministic:** Prefer robust, local execution over remote dependencies where possible.
* **Single Source of Truth (No Data Drift):** Never duplicate state or hardcode derived values. If a value can be computed from raw data, compute it dynamically. Do not create redundant fixture dictionaries to patch bad data; fix the root schema.

## 2. Code Quality (Non-Negotiable)

* **Linter/Formatter: Ruff only.** Run `ruff format .` and `ruff check . --fix` before declaring any task complete. Config lives in `pyproject.toml` (line-length=120, Python 3.12 target).
* **Type Hints: Mandatory** for all function parameters and return types. No untyped public functions.
* **Line Length:** 120 characters (Ruff enforced).
* **Dev Dependencies:** Install with `pip install -e ".[dev]"`.

## 3. Test-Driven Development (TDD) Discipline
* **Validate Before Success:** You MUST validate your code locally and ensure tests pass before declaring a task complete.
* **TDD Cycle (Mandatory):**
  1. **RED:** Write a failing test first.
  2. **GREEN:** Write the minimum code to pass.
  3. **REFACTOR:** Clean up while keeping tests green.
* **AAA Pattern:** All tests must strictly follow the Arrange, Act, Assert structure.
* **Test Naming:** Use the `test_<what>_<when>_<expected>` convention (e.g., `test_slugify_special_chars_returns_clean_slug`).
* **Coverage:** Target 80%+ minimum. Run with `pytest --cov=scripts --cov-report=term-missing -v`.

## 4. Agentic Git Hygiene
* **Atomic Auto-Commits:** You are authorized and encouraged to auto-commit to the local branch after *every* successful test pass or completed logical step.
* **Semantic Prefixing:** Prefix all automated commits with `agent: ` or `wip: `.
* **Revert on Failure:** Use `git reset --hard` to revert to your last green commit if you break the test suite, rather than blindly overwriting files to guess the fix.
* **Execution Strategy:** Default to sequential for coupled changes (write one file, test it, verify it, then move to the next). Use parallel sub-agents only when tasks are truly independent and touch separate files — e.g., research, code review, or test generation across unrelated modules.

## 5. Architectural Authority
* **The Blueprint:** Architectural vision lives in `ARCHITECTURE.md`. Decisions are recorded in `docs/adr/`.
* **Gemini is a Proxy:** Gemini does multimodal video understanding. Claude does triage and reasoning. Never merge these roles.
* **Skill Boundary:** The skill only does Gemini work (scan, transcript). Triage and deep-dive are conversations with Claude, not API calls.
* **Zero-Inference:** Do not hallucinate external database connections, APIs, or complex UI frameworks. Do not build undesigned tiers from `ARCHITECTURE.md`'s future vision.
