# AI Developer Rules of Engagement

## 1. Core Engineering Philosophy
* **Minimalism First:** Implement only the absolute minimum code required to pass the test. Prefer updating existing files over creating new ones.
* **Modern Python (3.12+):** Rely heavily on standard libraries, `dataclasses`, `Enum`, and strict type hinting.
* **Local-First & Deterministic:** Prefer robust, local execution over remote dependencies where possible.
* **Single Source of Truth (No Data Drift):** Never duplicate state or hardcode derived values. If a value can be computed from raw data, compute it dynamically. Do not create redundant fixture dictionaries to patch bad data; fix the root schema.

## 2. Code Quality (Non-Negotiable)

* **Linter/Formatter: Ruff only.** Run `ruff format .` and `ruff check . --fix` before declaring any task complete. Config lives in `pyproject.toml` (line-length=120, Python 3.12 target).
* **Type Hints: Mandatory** for all function parameters and return types. No untyped public functions.
* **Line Length:** 120 characters (Ruff enforced).
* **Dev Dependencies:** Install with `pip install -r requirements-dev.txt`.

## 3. Test-Driven Development (TDD) Discipline
* **Validate Before Success:** You MUST validate your code locally and ensure tests pass before declaring a task complete.
* **TDD Cycle (Mandatory):**
  1. **RED:** Write a failing test first.
  2. **GREEN:** Write the minimum code to pass.
  3. **REFACTOR:** Clean up while keeping tests green.
* **AAA Pattern:** All tests must strictly follow the Arrange, Act, Assert structure.
* **Test Naming:** Use the `test_<what>_<when>_<expected>` convention (e.g., `test_slugify_special_chars_returns_clean_slug`).
* **Unit test coverage:** Target 80%+ for pure functions and data transformations. Run with `pytest --cov --cov-report=term-missing -v`.
* **Integration tests:** Mark with `@pytest.mark.integration`. Run unit tests only with `pytest -m "not integration"`, integration only with `pytest -m "integration"`. No coverage metric — success is the test.
* **When to write tests:**
  * **New feature** → does it have a pure function? → test it.
  * **Bug fix** → can you reproduce it with a test? → write the test FIRST.
  * **Refactor** → do tests exist for what you're touching? → if not, add them before refactoring.

## 4. Agentic Git Hygiene
* **Atomic Auto-Commits:** You are authorized and encouraged to auto-commit to the local branch after *every* successful test pass or completed logical step.
* **Semantic Prefixing:** Prefix all automated commits with `agent: ` or `wip: `.
* **Revert on Failure:** Use `git reset --hard` to revert to your last green commit if you break the test suite, rather than blindly overwriting files to guess the fix.
* **Execution Strategy:** Default to sequential for coupled changes (write one file, test it, verify it, then move to the next). Use parallel sub-agents only when tasks are truly independent and touch separate files — e.g., research, code review, or test generation across unrelated modules.

## 5. Architectural Authority
* **The Blueprint:** Architectural vision lives in `ARCHITECTURE.md`. Decisions are recorded in `docs/adr/`. Project-specific constraints live in `CLAUDE.md`.
* **Respect Boundaries:** Do not merge components that the architecture deliberately separates. If a boundary exists, there is a reason.
* **Verify, Don't Assume:** Always retrieve current documentation via Context7 before using any library, SDK, or API. Use Exa to search for the latest information when docs alone are insufficient. Training data goes stale — treat your own knowledge as a starting point, not a source of truth. Do not build undesigned components from `ARCHITECTURE.md`'s future vision.
