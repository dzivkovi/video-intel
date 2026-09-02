---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: ce-plan-bootstrap
origin: https://github.com/dzivkovi/video-intel/issues/203
created: 2026-09-02
depth: standard
---

# fix(search): route `--topic` with a query through the index-level predicate

## Summary

`search --topic <slug> "<query>"` starves on a global candidate pool. Both modes rank corpus-wide first and post-filter to the topic afterwards, so a 19-member topic must out-rank 2,300+ other videos for `limit * TOPIC_FILTER_OVERFETCH` slots. The fix scopes retrieval **at the source** in both modes - the `video_ids_filter` index predicate for `--vector` (the mechanism `nugget --topic` already uses since #188), and an exact pre-truncation filter inside `search_corpus` for concept mode - and retires `TOPIC_FILTER_OVERFETCH` deliberately, because an exact scope makes a probability multiplier meaningless.

---

## Problem Frame

Reproduced on the live corpus, 2026-09-02 (read-only):

```
$ search "AI as a thinking partner, not an answer machine" --vector --topic thinking-partner --limit 5
No results ... The ranking returned 25 result(s) and the topic filter removed all of them.
```

The topic has 19 members across 9 channels, the query-less listing shows them all, and the unscoped `--vector` search answers the question well. Concept mode starves the same way, less visibly: `search "thinking partner" --topic thinking-partner --limit 5` returns **1** of 19 members, because the matched concept `ai_technical_fluency` spans 188 videos and the top-25 window fills with non-members.

This is the identical shape #188 measured and fixed for `nugget --topic` (a 19-video topic reduced to 2 surviving excerpts). CLAUDE.md topics row 9 recorded that `search --topic` was left on the post-filter deliberately, pending its own decision. Issue #203 is that decision.

---

## Requirements

- **R1.** `search --topic <slug> "<query>" --vector` scopes retrieval at the index query, not by post-filtering a global pool.
- **R2.** Concept mode (`--topic` without `--vector`) applies the topic scope **before** the result cap, so no member is lost to rank.
- **R3.** The semantic contract is unchanged: a topic surface **filters, never reorders or boosts**. Within the scope, ranking order is whatever the retriever produced.
- **R4.** The user's `--limit` passes through untouched. No multiplier on either surface.
- **R5.** One resolver (`resolve_topic_filter`), one belt check, one no-match message shared with `nugget`, so the two surfaces cannot drift.
- **R6.** `TOPIC_FILTER_OVERFETCH` and its two frozen tests are retired **on purpose**, with a replacement test asserting the stronger guarantee and a marker test proving the constant is gone.
- **R7.** An emptied result names a remedy that can actually work. `Raise --limit` becomes false advice under an exact filter and must go.
- **R8.** `nugget --topic` behavior is byte-identical after the shared-helper extraction.
- **R9.** Eval discipline: `tests/evals/test_instrument.py` first, then `tests/evals/test_search_quality.py` by module name; record N/25 against the 1/25 baseline on the 85,854-chunk index.

---

## Key Technical Decisions

**KTD1 - Vector mode uses `video_ids_filter`, not a wider post-filter.** Raising the multiplier only moves the cliff: #188 measured one chunk-dense member consuming 23 of 95 pool slots. The predicate is exact and `hybrid_search` already sizes the scoped pool to the scope (`max(fetch, min(1000, 15 * len(ids)))`), so depth comes for free. The unscoped pool that #190 invariant 4 pins is untouched.

**KTD2 - Concept mode filters inside `search_corpus`, before truncation - and that is exact, not a better heuristic.** `search_corpus` already walks the whole corpus and materializes every matching video before `[:limit]`. Applying the scope there costs nothing extra and gives a guarantee no multiplier can: a member is never lost to rank. `search_corpus` takes a `video_ids_filter` parameter with the same convention as `hybrid_search` - `None` means no filter, an empty set matches nothing, never everything.

**KTD3 - One belt, one message, shared with nugget.** `drop_topic_leaks()` and `topic_no_match_message()` are extracted from `cmd_nugget` verbatim and called by both surfaces. Two copies of "what a leak means" is how the two surfaces drift about a slug, which is the exact failure `resolve_topic_filter` exists to prevent.

**KTD4 - The two empty results stay distinguishable, and the vector one changes meaning.** In concept mode the topic scope still narrows a corpus-wide concept match, so "the ranking returned N and the topic filter removed all of them" remains accurate and diagnostic - minus the now-false `Raise --limit` remedy, replaced by the members listing. In vector mode the ranking is *already* scoped, so "the filter removed all of them" would be a lie; that branch uses the shared no-match message instead. Distinguishing an emptied filter from a genuinely empty index (the #146 Finding-6 contract) survives in both.

**KTD5 - Retirement is asserted, not assumed.** A test asserts `video_intel` has no `TOPIC_FILTER_OVERFETCH` attribute. The constant's only purpose was compensating for a post-filter that no longer exists; leaving it would invite a future edit to re-plumb the post-filter around it.

---

## Implementation Units

### U1. Shared topic-scope helpers, nugget refactored onto them

**Goal:** One definition each of "drop leaks past the predicate" and "nothing in this topic matched", used by `nugget` and `search --vector`.

**Requirements:** R5, R8

**Dependencies:** none

**Files:** `scripts/video_intel.py`, `tests/test_topics.py`

**Approach:** Extract `drop_topic_leaks(hits, topic_ids, topic_slug)` (WARNING + drop, wording preserved from `cmd_nugget`) and `topic_no_match_message(query, topic_slug)` (wording preserved) as module-level helpers next to `resolve_topic_filter`. Rewire `cmd_nugget` to call them. No behavior change on the nugget surface.

**Patterns to follow:** `resolve_topic_filter` - the shared-resolver idiom introduced by #188.

**Test scenarios:**
- `nugget --topic` still scopes with the user's own limit and the resolved id set (existing `TestNuggetTopicScoping` stays green unmodified).
- A leak past the predicate is still dropped with the same WARNING substring.
- The helper returns the same message text `cmd_nugget` printed pre-refactor.

**Verification:** `TestNuggetTopicScoping` passes with zero edits to its assertions.

### U2. `search_corpus` gains an exact `video_ids_filter`

**Goal:** Concept mode can scope to a topic before the result cap.

**Requirements:** R2, R3, R4

**Dependencies:** none

**Files:** `scripts/video_intel.py`, `tests/test_topics.py`

**Approach:** Add keyword-only `video_ids_filter=None`. Apply after the existing `--since` filter and the relevance sort, before `matching_videos[:limit]`, so scope narrows without reordering. Return an additive `videos_before_topic_filter` key (count before the scope, after `--since`) so the caller can still tell an emptied filter from a genuinely empty match. `None` = no filter; empty set = no videos.

**Patterns to follow:** `hybrid_search`'s `video_ids_filter` convention, including the `is not None` test (never truthiness).

**Test scenarios:**
- A member ranked far below the cap still surfaces at a small `--limit`.
- `None` leaves results byte-identical to pre-change for every existing caller.
- An empty set returns no videos (never all videos).
- The filter does not reorder: two members keep their relative `(-matched_concepts, published)` order.
- `videos_before_topic_filter` reports the pre-scope count.

**Verification:** existing `search_corpus` tests in `tests/test_utils.py`, `tests/test_search_since.py`, `tests/test_concept_rank_specificity.py` pass unmodified.

### U3. `cmd_search` routes both modes at the source; `TOPIC_FILTER_OVERFETCH` retired

**Goal:** The user-visible fix.

**Requirements:** R1, R2, R4, R6, R7

**Dependencies:** U1, U2

**Files:** `scripts/video_intel.py`

**Approach:** Delete `fetch_limit` and the multiplier. Vector branch passes `limit=args.limit, video_ids_filter=topic_ids` to `hybrid_search`, then `drop_topic_leaks`, then `topic_no_match_message` on empty. Concept branch passes `video_ids_filter=topic_ids` into `search_corpus` and drops the post-filter list comprehension; the emptied branch reads `videos_before_topic_filter`. Delete `TOPIC_FILTER_OVERFETCH` and amend `topic_filter_emptied_message` (drop `Raise --limit`, add the members-listing remedy, re-word the docstring for the exact-filter world).

**Test scenarios:**
- Vector: a member whose best chunk ranks below the global cap surfaces; the predicate reaches `hybrid_search` as `video_ids_filter`; the limit is the user's own.
- Vector: no results in a scoped search prints the no-match message, never "Is the index built?" and never "the topic filter removed all of them".
- Concept: query matched videos but none in the topic prints the emptied message, with no `--limit` remedy.
- Concept: query matched no concepts at all prints the no-concepts message (unchanged).
- Unscoped search (`--topic` absent) is byte-identical to pre-change on both modes.
- `--channel` and `--since` still compose with `--topic` on both modes.

**Verification:** the live Gate-1 reproduction returns members instead of the starvation message.

### U4. Test contract: retire the frozen over-fetch class, assert the stronger guarantee

**Goal:** The retirement is deliberate and the replacement proves more than the thing it replaces.

**Requirements:** R6

**Dependencies:** U3

**Files:** `tests/test_topics.py`

**Approach:** Delete `TestTopicFilterOverfetchIsLoadBearing` (both mode tests and the constant assertion) and the `TOPIC_FILTER_OVERFETCH` import. Add `TestTopicScopeIsExactNotOverfetched` reusing the same fixture shapes but with the member ranked *arbitrarily* low - proving the new mechanism does not merely widen a window. Add a marker test asserting the constant no longer exists. Update `TestEmptyTopicResultNamesTheRightRemedy` for the vector-branch message change and the dropped `--limit` remedy.

**Test scenarios:**
- Concept mode: the member ranks last of many with `--limit 1`; still returned.
- Vector mode: driven through the real `hybrid_search` with a stubbed table, asserting the `where` clause carried `video_id IN (...)`.
- `not hasattr(video_intel, "TOPIC_FILTER_OVERFETCH")`.
- The emptied concept message no longer contains `--limit`.

### U5. Docs and skill parity, same diff

**Goal:** No surface still describes the retired mechanism.

**Requirements:** R6

**Dependencies:** U3

**Files:** `CLAUDE.md`, `docs/topics-layer.md`, `skills/video-intel-search/SKILL.md`

**Approach:** Amend CLAUDE.md topics row 9 - the sentence pinning `search --topic` to the post-filter as a frozen contract becomes the record of its deliberate retirement, naming #203, the two mechanisms, the shared helpers, and the reviewer guardrail. Rewrite `docs/topics-layer.md`'s "A filtered search can return videos the unfiltered search did not show" so the stated limitation (over-fetch is `limit * 5`, a member below that is still missed) is replaced by the exact-scope guarantee. Update the search SKILL.md `--topic` bullet so it describes index-level scoping for both modes.

**Test expectation:** none - documentation. The CLAUDE.md guardrail is itself the reviewer contract.

---

## Scope Boundaries

**In scope:** `search --topic` with a query, both modes; the shared helpers; the constant's retirement; docs parity.

**Not in scope:** the query-less topic listing (`render_topic_listing`, #188 - untouched); `nugget`'s retrieval behavior (refactor only); the unscoped `hybrid_search` pool (#190 invariant 4); `topics-build`; anything in `taxonomy.json`.

### Deferred to Follow-Up Work

- Concept mode's *concepts* list is still corpus-wide under `--topic` (only the video list is scoped). That is the pre-existing contract and narrowing it is a separate product question.

---

## Risks & Dependencies

- **Eval movement.** The change touches `hybrid_search`'s call site but not its unscoped path, and the golden dataset uses no `--topic`, so N/25 is expected unchanged at 1/25. Re-measure and record either way.
- **`search_corpus` signature.** Three test modules call it; the new parameter is keyword-only and defaulted, so existing calls are unaffected.

---

## Verification Contract

1. `pytest tests/ -q --ignore=tests/evals` green (baseline 2449 passed, 1 skipped).
2. `ruff format . && ruff check .` clean.
3. `pytest tests/evals/test_instrument.py` - 50/50, the ruler intact.
4. `pytest tests/evals/test_search_quality.py` by module name - record N/25.
5. Gate 1 live, read-only: the issue's exact command returns members; concept mode returns more than 1 of 19.

---

## Definition of Done

All five verification gates pass, both review layers are applied, CLAUDE.md row 9 records the retirement, and no surface in the repo still describes `TOPIC_FILTER_OVERFETCH`.
