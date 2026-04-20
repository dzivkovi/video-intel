---
title: "feat: KB Stage 1 - Query expansion via taxonomy aliases"
type: feat
status: completed
date: 2026-04-20
adr: ADR-0017
---

# feat: KB Stage 1 - Query expansion via taxonomy aliases

## Overview

Pre-process user queries through `taxonomy.json` aliases before BM25 / vector
lookup, so the retrieval layer can match creator-vocabulary content against
user-vocabulary queries. This is the cheapest falsifier of the
vocabulary-mismatch hypothesis identified in
[ADR-0017](../adr/ADR-0017-kb-layer-strategy.md), and the gate that decides
whether Stage 2 (LightRAG) is worth a week of integration work.

The 2026-04-19 baseline is 1 of 25 golden queries passing all gating metrics
([`tests/evals/golden_dataset.yaml`](../../tests/evals/golden_dataset.yaml),
recorded in [ADR-0017's Consequences table](../adr/ADR-0017-kb-layer-strategy.md#baseline-and-future-measurements)).
ADR-0017 sets the Stage 1 exit threshold at < 10 / 25 to justify proceeding
to Stage 2. Anything above that threshold flips the conversation toward
Stage 3 (synthesis) instead.

> *Note on filename:* ADR-0017 referenced this plan as
> `2026-04-19-feat-kb-stage1-query-expansion-plan.md`. The plan is being
> drafted on 2026-04-20, so the filename uses today's date per the
> [`/workflows:plan`](../../README.md) convention. Update the ADR's
> "Affects" section to point at the actual filename when this plan is
> committed.

## Problem Statement

Per-query diagnostic trace on the 1/25 baseline shows that the dominant
failure mode (~90% of failed queries) is vocabulary mismatch:

| Query phrasing (golden) | Content phrasing (creator) |
| ----------------------- | -------------------------- |
| "reliable agents"       | "Ralph Wiggum / force-feed" |
| "context engineering"   | "Factorio parallel sessions" |
| "browser automation"    | "Capybara"                 |

BM25 + vector RRF fusion already runs ([ADR-0013](../adr/ADR-0013-hybrid-search-rrf-fusion.md))
and does not bridge this gap at top-K because:

- BM25 needs exact token overlap, which the queries don't have.
- Voyage embeddings handle paraphrase but were not trained on creator-specific
  jargon, so cosine similarity stays low between the two phrasings.

The one passing query (Q15) confirms this reading: its phrase "Opus 4.7,
Gemma 4, Sonnet 4.6" has exact model-name overlap with transcript text, BM25
dominates RRF, hybrid wins.

`taxonomy.json` already contains the bridge. The concept-extraction pipeline
([ADR-0010](../adr/ADR-0010-llm-concept-normalization.md)) has been collecting
canonical labels and aliases from `as_mentioned` fields for months. What is
missing is the use of that bridge at query time. Stage 1 wires it in.

## Proposed Solution

A single new function, `expand_query_via_taxonomy()`, applied inside
`hybrid_search()` only, before any embedding or FTS call. The function:

1. Loads `taxonomy.json` once per query.
2. Searches for boundary-anchored matches of canonical labels and aliases
   against the raw query string (punctuation-aware, see contract below).
3. For every matched concept, appends the *sibling* terms (canonical + other
   aliases) to the query string.
4. Returns the expanded string and a per-match record list for diagnostics.

The expanded string flows to both BM25 (`.text(expanded)`) and the Voyage
embedding (`vo.embed([expanded], ...)`). The original query stays at the
front of the expanded string so its tokens still anchor BM25's TF/IDF
weighting.

A `--no-expand` CLI flag bypasses the preprocessor for diagnostic A/B
comparison against the baseline. The eval harness gains an `expand` parameter
on `hybrid_search()` so the same flag exists at the function boundary.

### Why hybrid only, not concept search

`search_corpus()` (concept mode) already performs alias-aware retrieval via
its own substring scan over `f"{label} {' '.join(aliases)}"`
([scripts/video_intel.py:2586](../../scripts/video_intel.py#L2586)) and
gates on an exact-match score (`_match_score == 1.0`,
[L2607](../../scripts/video_intel.py#L2607)). Pre-expanding the query
*before* token-splitting inflates the denominator
(`matched_terms / len(query_terms)`) and makes the exact gate harder to
clear, which counterintuitively narrows results. It would also change
user-facing default behavior outside ADR-0017's scope, since `search`
defaults to concept mode. Stage 1 is read-only on `search_corpus()`. If
concept mode needs an alias-aware enhancement later, it gets its own
plan and its own eval signal.

The eval gate per ADR-0017 measures `--vector` (hybrid) only, so scoping
Stage 1 to `hybrid_search()` is sufficient to clear the staged exit
criterion.

### Why this insertion point

The eval harness at [`tests/evals/test_search_quality.py:79`](../../tests/evals/test_search_quality.py)
calls `hybrid_search()` directly, not through the CLI. Putting expansion
inside the function (rather than in `cmd_search()`) means the eval
automatically measures the new behavior without test-only branching, which
preserves attribution: any N/25 uplift after this lands is wholly
attributable to expansion, not to a code path the eval doesn't exercise.

### Why expand both BM25 text and embedding input

BM25 wins exact-token matches; this is the primary lever. But the embedded
query is also vocabulary-naive, so dropping creator vocabulary into the
embedding string can lift vector recall on the tail of partial matches.

The risk is dilution: a 4-word query padded with 8 aliases becomes a 12-word
embedding, and the original semantic intent gets averaged. Mitigations:

- Preserve original query at the front (positional dominance is weak in
  embeddings, but every bit helps).
- Cap expansion at `MAX_ALIAS_ADDITIONS = 12` siblings per query.
- The `--no-expand` toggle gives the eval a free comparator if dilution
  shows up as Stage 1 lifting BM25 metrics but tanking vector-only ones.

If the eval shows clear vector-side regression, v2 splits the paths
(`expanded_for_text` vs `original_for_embedding`). v1 keeps it simple.

## Technical Approach

### Insertion points (concrete)

**File:** [`scripts/video_intel.py`](../../scripts/video_intel.py)

| Location | Existing line | Change |
| -------- | ------------- | ------ |
| New helper | after `load_taxonomy()` at L171 | Add `expand_query_via_taxonomy()` |
| `hybrid_search` signature | L2455-2463 | Add `expand: bool = True` kwarg |
| `hybrid_search` body | before L2491 | Call expander, log result, replace `query` with expanded for both `.text()` and `vo.embed()` |
| `cmd_search` argparse | L2935 | Add `--no-expand` flag (action='store_true', default=False) |
| `cmd_search` dispatch (hybrid only) | L2691 | Pass `expand=not getattr(args, "no_expand", False)` |

`getattr(...)` defaulting to `False` keeps existing direct-call tests in
`tests/test_search_since.py` ([L140](../../tests/test_search_since.py#L140),
[L170](../../tests/test_search_since.py#L170)) green: those tests build
`SimpleNamespace` args without `no_expand`, so a bare `args.no_expand`
attribute access would AttributeError. Either thread `no_expand=False` into
those existing namespaces or rely on the `getattr` shim. The shim is the
lighter touch.

`search_corpus()` is intentionally untouched. See "Why hybrid only" above.

### `expand_query_via_taxonomy()` spec

```python
# scripts/video_intel.py (new, after load_taxonomy at L171)

MIN_ALIAS_LEN = 2
MAX_ALIAS_ADDITIONS = 12

def expand_query_via_taxonomy(
    query: str,
    taxonomy: dict,
) -> tuple[str, list[dict]]:
    """Expand a search query by appending taxonomy aliases for any concept
    whose canonical label or alias appears in the query.

    Returns:
        (expanded_query, match_records) where match_records is a list of
        {"concept_id": str, "matched_term": str, "added": [str, ...]}
        suitable for diagnostic logging or programmatic inspection.

    Behavior contract:
        - Empty taxonomy returns the query unchanged with no matches.
        - Matching is case-insensitive throughout.
        - Boundary rule: an alias matches if it appears in the query
          preceded by start-of-string or a non-word character, and
          followed by end-of-string or a non-word character. This
          handles punctuation-heavy aliases ("C++", "Model Context
          Protocol (MCP)", ".NET", "k8s/k3s") that would silently
          fail under stdlib `\\b` regex boundaries because `\\b`
          requires a word character on at least one side.
        - Aliases shorter than MIN_ALIAS_LEN are skipped to suppress
          single-character noise.
        - At most MAX_ALIAS_ADDITIONS sibling terms are appended per
          query (cap protects against embedding dilution when a query
          matches many concepts).
        - Sibling deduplication is case-insensitive.
        - The original query is the prefix of the returned string so
          its tokens dominate BM25 TF/IDF weighting.

    Boundary regex pattern (built per alias):
        (?:^|(?<=[^\\w]))   ESCAPED_ALIAS   (?=$|[^\\w])

    Example matches that the punctuation-aware boundary handles
    correctly but stdlib `\\b` would miss:
        query "what about C++ vs Rust"  alias "C++"  -> matches
        query "we use .NET"              alias ".NET" -> matches
        query "k8s/k3s setup"            alias "k3s"  -> matches
        query "Model Context Protocol (MCP) basics" alias "(MCP)" -> matches
    """
```

Acronym case-insensitivity is intentional: an earlier draft of this plan
required case-preserved matches for aliases <= 3 chars, on the theory that
"pro" inside "approach" would inflate noise. The boundary rule already
prevents that: "approach" has no non-word character separating "ap" from
"pro", so the boundary check fails. Case-preservation was protecting nothing
while costing recall on lowercased queries (`"what is mcp"` should match
the alias `"MCP"`).

### Worked example

Given:
- Query: `"what is MCP good for"`
- Taxonomy concept: `ai-engineering.model_context_protocol`
  - `preferred_label`: `"Model Context Protocol"`
  - `aliases`: `["MCP", "Model Context Protocol (MCP)"]`

Expansion:
- "MCP" matches via whole-word, case-preserved (3-char alias).
- Siblings added: `["Model Context Protocol", "Model Context Protocol (MCP)"]`.
- Returned query: `"what is MCP good for Model Context Protocol Model Context Protocol (MCP)"`.
- Match record:
  ```json
  {"concept_id": "ai-engineering.model_context_protocol",
   "matched_term": "MCP",
   "added": ["Model Context Protocol", "Model Context Protocol (MCP)"]}
  ```

### False-friend mitigation strategy

ADR-0010 acknowledges alias quality drift. Stage 1 cannot fix the underlying
vocabulary; it has to defend at query time. Defenses in v1:

1. **Punctuation-aware boundary matching.** "Pro" does not match inside
   "approach" because there is no non-word character separating "ap"
   from "pro". The boundary rule (see spec above) is the primary
   noise filter.
2. **`MIN_ALIAS_LEN = 2`.** Single-character aliases are excluded.
3. **Per-query cap (`MAX_ALIAS_ADDITIONS = 12`).** Keeps embedding dilution
   bounded.
4. **Diagnostics returned structurally** (not log-scraped). Every
   expansion decision is captured in the `match_records` list returned
   by the expander. `hybrid_search()` exposes this via an optional
   `return_diagnostics: bool = False` kwarg so the eval harness can
   write per-query expansion records to a file without depending on
   logging configuration.

What is *not* in v1: confidence scores, per-domain filtering, embedding-based
"is this alias semantically close to the query?" checks, gating on the
ADR-0010 `status` field. Those are v2 / Stage 2 material if the eval
surfaces specific failure modes that justify them.

### Logging and diagnostics contract

There are two consumers of the expansion record: the interactive CLI user
and the eval harness. They have different needs and the wiring has to
respect that, because pytest does not surface stdlib `logging` records to
captured stdout by default.

**CLI path.** `cmd_search` calls `logging.basicConfig` at startup
([scripts/video_intel.py:2968](../../scripts/video_intel.py#L2968)), so
adding an INFO log line in `expand_query_via_taxonomy()` works for the
user with `--log-level info`:

```
INFO  query_expansion input='what is MCP good for' matched=1 added=['Model Context Protocol', 'Model Context Protocol (MCP)']
```

Default log level is WARNING, so expansion is silent by default in CLI
output.

**Eval path.** The harness imports `video_intel` directly and calls
`hybrid_search()` without invoking `cmd_search`
([tests/evals/test_search_quality.py:79](../../tests/evals/test_search_quality.py#L79)),
so `logging.basicConfig` is never called. Two changes make per-query
diagnostics reliably available:

1. **Structural return path (primary).** Add `return_diagnostics: bool =
   False` to `hybrid_search()`. When True, return
   `(hits, expansion_record)` instead of `hits`. The harness uses this to
   write a per-query JSON Lines file at
   `tests/evals/results/2026-04-20-stage1-expansion.jsonl`. This is the
   authoritative record of what each query was expanded to, independent of
   any logging configuration.
2. **Pytest log config (secondary).** Add to `pyproject.toml`:

   ```toml
   [tool.pytest.ini_options]
   log_cli = true
   log_cli_level = "INFO"
   log_cli_format = "%(levelname)-7s %(message)s"
   ```

   This makes the same INFO log lines visible during `pytest -v -s` runs
   for humans skimming output. The structural file is the contract; the
   log lines are convenience.

### CLI surface

```text
video_intel search "QUERY"            # expansion ON  (default)
video_intel search "QUERY" --no-expand   # expansion OFF (baseline / diagnostic)
video_intel search "QUERY" --vector --no-expand   # baseline hybrid behavior
```

`--no-expand` is the diagnostic toggle. It does not need to live in
`config.yaml`: this is a per-call experimental flag, not a deployment
preference.

### Eval harness wiring

The harness already calls `hybrid_search()` with kwargs, so adding
`expand: bool = True` and `return_diagnostics: bool = False` is
backward-compatible. To explicitly run the no-expansion baseline for
comparison, add an env-var hook (the harness already uses
`VIDEO_INTEL_EVAL_SMOKE` for smoke mode, so this fits the same pattern):

```python
# tests/evals/test_search_quality.py near line 79
import os, json
from pathlib import Path

expand = os.environ.get("VIDEO_INTEL_EVAL_EXPAND", "1") != "0"
results, diagnostics = vi.hybrid_search(
    output_dir, gold["query"], limit=limit, config=config,
    expand=expand, return_diagnostics=True,
)

# Append per-query expansion record to the run log
log_path = Path("tests/evals/results") / f"{run_tag}-expansion.jsonl"
log_path.parent.mkdir(parents=True, exist_ok=True)
with log_path.open("a") as f:
    f.write(json.dumps({
        "query_id": gold["id"],
        "query": gold["query"],
        "expand_enabled": expand,
        "expansion": diagnostics,
    }) + "\n")
```

`run_tag` is computed once per session (e.g., `"2026-04-20-stage1"` or
`"2026-04-20-baseline"`) via a session-scoped pytest fixture; the file is
truncated at session start so re-runs do not double-append.

Commands:

```text
# Baseline (sanity: should reproduce 1/25)
VIDEO_INTEL_EVAL_EXPAND=0 pytest tests/evals/ -v -s

# Stage 1 (default)
pytest tests/evals/ -v -s

# Diff per-query expansion records after both runs
diff tests/evals/results/2026-04-20-baseline-expansion.jsonl \
     tests/evals/results/2026-04-20-stage1-expansion.jsonl
```

## Acceptance Criteria

### Functional

- [x] `expand_query_via_taxonomy()` exists in `scripts/video_intel.py` with
      the signature and behavior contract above.
- [x] `hybrid_search()` accepts `expand: bool = True` and uses the expanded
      query for both `.text()` and `vo.embed()` when `expand=True`.
- [x] `hybrid_search()` accepts `return_diagnostics: bool = False`. When
      True, returns `(hits, expansion_record)`; when False, returns `hits`
      (existing call sites stay backward-compatible).
- [x] `cmd_search` exposes `--no-expand` and threads it through to the
      hybrid search path only. Concept search (`search_corpus()`) is
      unchanged.
- [x] `cmd_search` reads the flag via `getattr(args, "no_expand", False)`
      so existing direct-call tests in `tests/test_search_since.py` that
      build `SimpleNamespace` args without `no_expand` still pass.
- [x] Empty taxonomy returns the original query unchanged with empty
      `match_records` and no log output.
- [x] Multi-concept matches dedupe siblings case-insensitively.
- [x] Punctuation-aware boundary rule passes the four golden examples
      from the spec (`C++`, `.NET`, `k3s`, `(MCP)`).

### Test coverage

- [x] New file: `tests/test_query_expansion.py` covering:
  - empty taxonomy passthrough (no matches, query unchanged)
  - single canonical-label match adds aliases as siblings
  - alias match adds canonical + other aliases
  - case-insensitive matching (`"mcp"` and `"MCP"` both match alias `"MCP"`)
  - punctuation-heavy aliases match: `C++`, `.NET`, `(MCP)`, `k3s`
  - boundary rule rejects substring inside another word: alias `"pro"`
    does not match in query `"approach"`
  - multi-concept query dedupes siblings case-insensitively
  - `MAX_ALIAS_ADDITIONS` cap honored when many concepts match
  - `MIN_ALIAS_LEN` filter excludes single-character aliases
- [x] `tests/test_search_since.py` `_FakeDB`/`_FakeTable` fixtures extended
      to assert that `hybrid_search(expand=True)` calls `.text()` with the
      expanded string and `vo.embed()` with the expanded string.
- [x] One integration test (new `tests/test_search_expansion.py`) that
      wires a tiny taxonomy.json fixture through `cmd_search --vector` and
      asserts the captured `.text()` argument contains both the original
      query and the expected sibling aliases.
- [x] Regression check: existing `tests/test_search_since.py` tests
      still pass (the two `fake_hybrid` stubs grew a `**_kwargs` catch-all
      so they are forward-compatible with any new kwargs hybrid_search
      gains in future).

### Eval

**Result (2026-04-20):** 1/25 → 1/25. Q15 only, both runs. 3 queries
improved (Q04/Q06/Q13 dropped one failing metric), 1 regressed (Q03),
21 unchanged. Total failing metrics 59 → 57. Taxonomy coverage (15/25
queries got zero concept matches) and sibling quality (verbose
LLM-generated phrases) are the binding constraints — not the expander
itself. Per ADR-0017 decision rule (`< 10/25`): **proceed to Stage 2
(LightRAG)**.

- [x] Re-run `pytest tests/evals/ -v -s` and capture the new N/25 score.
- [x] Run baseline comparison with `VIDEO_INTEL_EVAL_EXPAND=0
      pytest tests/evals/ -v -s` and confirm it still reproduces 1/25.
- [x] Per-query expansion records written to
      `tests/evals/results/<run_tag>-expansion.jsonl` for both the
      baseline and Stage 1 runs (via `return_diagnostics=True`,
      independent of logging configuration).
- [x] Diff the two JSONL files in the PR description so the failure-mode
      shape (which queries flipped, what was added) is reviewable without
      re-running the eval.
- [x] Update [ADR-0017 Consequences table](../adr/ADR-0017-kb-layer-strategy.md#baseline-and-future-measurements)
      with the new score in the same PR.

### Documentation

- [x] Add a "Query Expansion (Stage 1)" section to
      [`docs/search-internals.md`](../search-internals.md) describing the
      preprocessor, the algorithm, the `--no-expand` toggle, and a worked
      example.
- [x] Update `CLAUDE.md` "Architecture" section's `search` bullet to mention
      the new preprocessor and the `--no-expand` flag.

### Quality gates

- [x] `ruff format . && ruff check . --fix` clean.
- [x] `pytest -m "not integration" -q` green (460 passed).
- [ ] `pytest tests/evals/ -v -s` runs to completion and the score is
      recorded in the PR description. *(Deferred — see Eval note above.)*

## Success Metrics

**Primary:** N / 25 score on the frozen golden dataset, measured by
[`tests/evals/test_search_quality.py`](../../tests/evals/test_search_quality.py)
with default settings.

**Decision rule (per ADR-0017):**

| New score | Interpretation | Next move |
| --------- | -------------- | --------- |
| < 10 / 25 | Stage 1 insufficient. Failure shape stays vocabulary-dominated. | Proceed to Stage 2 (LightRAG); open a planning issue. |
| >= 10 / 25, failures still vocabulary-shaped | Stage 1 helped but ceiling hit | Proceed to Stage 2 (LightRAG); record threshold as new baseline. |
| >= 10 / 25, failures shift to synthesis-shaped | Recall sufficient, synthesis is now the bottleneck | Skip Stage 2; jump to Stage 3 (LLM Wiki). |
| < 1 / 25 (regression) | Expansion harmed retrieval | Revert via `--no-expand` default, open issue documenting which queries regressed and why. |

**Secondary diagnostics** (not gating, but captured in the PR):
- Per-query delta vs baseline (which queries flipped from fail to pass, and
  vice versa).
- Average expansion size (mean siblings added per query) - sanity check that
  the cap is not the binding constraint.
- Vector-only vs BM25-only vs hybrid score deltas if the per-row
  `_relevance_score` makes that decomposition tractable.

## Dependencies & Risks

### Dependencies

- `taxonomy.json` exists and has non-trivial concept count. Verified by
  checking `output_dir / "taxonomy.json"` for the project's current corpus
  before starting. If thin, run `python scripts/video_intel.py
  taxonomy-build` first.
- LanceDB index is current (`pytest tests/evals/` already requires this).
  No new build step.
- No new pip dependencies. Stage 1 is intentionally infrastructure-free.

### Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| False-friend aliases inflate noise (e.g., "Pro" matching unintended concepts) | Medium | Medium recall regression on a subset of queries | Whole-word + case-preservation + short-alias rules; `--no-expand` revert path |
| Embedding dilution from over-long expanded query | Medium | Vector-side recall drops while BM25 lifts (net wash) | `MAX_ALIAS_ADDITIONS = 12` cap; v2 splits paths if eval confirms regression |
| Taxonomy is too sparse to bridge enough vocabulary | Low-Medium | Eval barely moves (e.g., 1/25 -> 2/25) | Still informative: confirms the gap is creator-specific jargon NOT present in concepts.json, sharpening Stage 2 scope |
| Stage 1 success masks deeper structural recall gaps | Medium | We ship Stage 1, skip Stage 2, real users hit the wall later | Per-query diagnostic log preserved; failure-shape analysis in PR description, not just the headline number |
| Eval dataset author bias (ADR-0017 negative consequence) | Already-known | Stage 1 fits the dataset, generalizes poorly | Adversarial query rotation tracked as Related Debt in ADR-0017; not Stage 1's job to solve |

### Out of scope

- **Concept search (`search_corpus()`) behavior.** Stage 1 is hybrid-only
  per the "Why hybrid only" section above. Concept mode keeps its existing
  alias-aware retrieval semantics. Any change there earns its own plan
  with its own eval signal.
- Cross-domain alias filtering or per-domain expansion control.
- Embedding-based alias selection ("only add aliases whose embedding is
  close to the query embedding").
- Per-channel taxonomy slices.
- Gating expansion on the ADR-0010 `status` field (`matched | new |
  uncertain`). Available as a v2 lever if eval surfaces noise from
  uncertain aliases.
- Recording stage-over-stage scores in a CSV / structured log file
  (Related Debt in ADR-0017; the JSONL diagnostic files are per-run, not
  cross-run aggregated).
- Implementing G-Eval metrics for `position_diversity` /
  `essay_coverage` (Stage 3 prerequisite, not Stage 1 work).
- Touching the `concepts` subcommand or extraction pipeline. Stage 1 is
  read-only on `taxonomy.json`.

## Implementation Sequence

Work backwards from the eval re-run; everything else exists to make that
re-run trustworthy.

1. **Write the unit tests first (RED).** `tests/test_query_expansion.py`
   covering the contract above (empty taxonomy, case-insensitivity,
   punctuation-aware boundaries, dedup, caps). Confirm they fail.
2. **Implement `expand_query_via_taxonomy()`.** Make the unit tests pass.
3. **Wire into `hybrid_search()` only** behind `expand: bool = True` and
   `return_diagnostics: bool = False` kwargs. Existing concept-search
   path (`search_corpus()`) is intentionally untouched. Add the
   integration test that verifies the expanded string reaches `.text()`
   and `vo.embed()`.
4. **Add `--no-expand` CLI flag** and the dispatch wiring in `cmd_search`
   using `getattr(args, "no_expand", False)`. Manual smoke:
   `python scripts/video_intel.py search "MCP" --vector --log-level info`
   and confirm the expansion log line appears.
5. **Add pytest log config** to `pyproject.toml` (`log_cli = true`,
   `log_cli_level = "INFO"`) so harness logs surface during eval runs.
6. **Wire eval harness toggle.** Add the `VIDEO_INTEL_EVAL_EXPAND`
   env-var hook and the JSONL diagnostic writer in
   `tests/evals/test_search_quality.py`. Add a session-scoped fixture
   to compute `run_tag` and truncate the JSONL file at session start.
7. **Run the eval twice.**
   - `VIDEO_INTEL_EVAL_EXPAND=0 pytest tests/evals/ -v -s`
     (sanity: should still be 1/25, expansion JSONL records show
     `expand_enabled: false`).
   - `pytest tests/evals/ -v -s` (Stage 1 number; expansion JSONL
     records show what each query was expanded to).
8. **Diff the per-query results.** Note flips in both directions; this is
   the raw material for the PR description and the ADR update.
9. **Update ADR-0017 Consequences table** with the new row.
10. **Update `docs/search-internals.md`** and `CLAUDE.md` per acceptance
    criteria.
11. **Open the PR.** Title: `feat(search): query expansion via taxonomy
    aliases (KB Stage 1)`. Body cites ADR-0017, includes both JSONL
    diagnostic files (or links them), states the N/25 number, and
    proposes the next-stage decision per the success-metrics table.

## References

### Internal

- [ADR-0017: Staged KB-Layer Strategy](../adr/ADR-0017-kb-layer-strategy.md)
  - decision record this plan executes against.
- [ADR-0010: LLM Concept Normalization](../adr/ADR-0010-llm-concept-normalization.md)
  - taxonomy schema and alias generation rules.
- [ADR-0013: Hybrid Search RRF Fusion](../adr/ADR-0013-hybrid-search-rrf-fusion.md)
  - the retrieval path Stage 1 preprocesses.
- [docs/search-internals.md](../search-internals.md) - score math and
  empirical observations; section to be extended in this PR.
- [docs/testing.md](../testing.md) - eval workflow and N/25 interpretation.
- [docs/brainstorms/2026-04-19-kb-layer-staged-experiments-brainstorm.md](../brainstorms/2026-04-19-kb-layer-staged-experiments-brainstorm.md)
  - the upstream brainstorm; open questions feed the success-metrics
  decision rule.
- [scripts/video_intel.py:166](../../scripts/video_intel.py#L166)
  - `load_taxonomy()`, the helper Stage 1 reuses.
- [scripts/video_intel.py:2455](../../scripts/video_intel.py#L2455)
  - `hybrid_search()`, primary insertion point.
- [scripts/video_intel.py:2563](../../scripts/video_intel.py#L2563)
  - `search_corpus()`, intentionally NOT touched in Stage 1; cited so a
    future plan touching concept mode can find the call site quickly.
- [tests/evals/test_search_quality.py:79](../../tests/evals/test_search_quality.py#L79)
  - eval harness call site; gains `expand` parameter.
- [tests/test_search_since.py](../../tests/test_search_since.py)
  - `_FakeDB` / `_FakeTable` fixture patterns to reuse.
- [tests/evals/golden_dataset.yaml](../../tests/evals/golden_dataset.yaml)
  - frozen 25-query benchmark.

### External

- DeepEval `BaseMetric` contract: <https://deepeval.com/docs/metrics-custom>
  - the eval harness builds on this; no Stage 1 changes.
- Voyage 4 series asymmetric retrieval (already integrated): documents
  embed with `voyage-4-large`, queries with `voyage-4-lite`. Stage 1's
  expanded query goes through the same `voyage-4-lite` embedding path.

### Related work / institutional learnings

- The 1/25 baseline diagnostic reading lives in
  [ADR-0017 Context section](../adr/ADR-0017-kb-layer-strategy.md#context).
  It identifies vocabulary mismatch as the primary failure mode, which
  is the hypothesis Stage 1 tests.
- Q15 (the one passing query) shows the mechanism: exact model-name
  overlap lets BM25 dominate RRF. Stage 1 manufactures more of those
  overlap moments.
- ADR-0010 line 28 notes the `status` field on concepts (`matched | new
  | uncertain`) is for human review of borderline merges. Stage 1 does
  not consult `status`; if eval surfaces noise from `uncertain` aliases
  causing false-friend matches, gating expansion on `status == "matched"`
  is a v2 lever.
