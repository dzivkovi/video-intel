---
title: "Gemini's implicit cache stopped hitting, so every concepts call paid full price for the taxonomy"
date: 2026-10-31
category: integration-issues
module: video_intel.py
problem_type: integration_issue
component: tooling
symptoms:
  - "Every `usage concepts` log line showed cached=0 on a back-to-back batch (25 of 25 calls), and every mind-map call too (204 of 204)"
  - A curated catch-up estimated at about CA$77 was heading for about CA$125 because concepts cost ~CA$0.31 per video instead of ~CA$0.06
  - Nothing failed and nothing warned; the only signal was the cached= field in the usage lines
root_cause: wrong_api
resolution_type: code_fix
severity: high
related_components:
  - documentation
tags:
  - gemini
  - context-caching
  - concepts
  - cost
  - taxonomy
---

# Gemini's implicit cache stopped hitting, so every concepts call paid full price for the taxonomy

## Problem

Each concepts call sends the concepts prompt plus the whole `taxonomy.json` (about 278k tokens on the 2026-10 corpus) and then one mind map. `docs/cost-model.md` priced that call at ~CA$0.06 on the assumption that Gemini's implicit cache would serve the shared prefix when calls run back to back. On 2026-10-31 (gemini-3.7-flash, which the API has been redirecting to 3.8 since the 3.7 retirement) the implicit cache served nothing, so each call cost ~CA$0.31 and a 204-video `scan --channel aidotengineer` catch-up ran about CA$55 over its estimate before it was noticed.

## Symptoms

- `usage concepts prompt=278453 cached=0 ...` on every call, including calls 20 to 40 seconds apart with byte-identical prefixes.
- `usage mindmap ... cached=0` on all 204 mind-map calls of the same run.
- No error, no warning, no change in output quality. The bill is the only other place it shows, and the billing console lags by hours.

## What Didn't Work

- **Suspecting our own prompt drift.** ADR-0010 accumulation (`accumulate_concepts_into_taxonomy`) mutates the in-memory taxonomy between videos, which would break a prefix cache if it edited earlier entries. It does not: it only adds new keys, which `json.dumps` appends at the end, and the taxonomy sits at the end of `prompts/concepts.md`, so the shared prefix was stable. Calls with no newly minted concepts also missed.
- **Waiting for the implicit cache to warm up.** Twenty-five sequential calls with identical prefixes never produced a single cached token. There is nothing on our side to tune: implicit caching is a provider-side optimization with no contract.

## Solution

Before writing code, a probe costing under CA$1: create an explicit cache over the exact prefix, then send two real mind maps against it.

```python
cache = client.caches.create(model="gemini-3.7-flash", config=types.CreateCachedContentConfig(
    contents=[types.Content(role="user", parts=[types.Part(text=prefix)])], ttl="600s"))
r = client.models.generate_content(model=..., contents=mindmap_tail,
    config=types.GenerateContentConfig(cached_content=cache.name, response_mime_type="application/json"))
# usage: prompt 278401, cached 277572 -> 99.7% served from cache
client.caches.delete(name=cache.name)
```

The fix (PR #271, pending merge at the time of writing) adds `ConceptsPromptCache` in `scripts/video_intel.py`: one explicit cache per batch of two or more videos (`scan` per channel, `concepts` for the whole run), built from the one shared `build_concepts_prompt_prefix`. Concepts minted earlier in the run travel in the uncached part as a delta block, so ADR-0010 still holds. Any cache failure falls back to the full inline prompt. Measured afterwards: 179 and then 125 concepts calls, zero uncached, about CA$0.045 per video including output, with the cache rebuilt once when it neared its one-hour expiry.

## Why This Works

An explicit cache is a contract: the prefix is stored once (billed as one input pass plus hourly storage) and every call that names it pays the cached rate for those tokens. The implicit cache is best-effort and can stop hitting after a model redirect or a provider change without any API-visible signal. Concepts is the step where this matters most, because its prompt is dominated by a prefix that grows with the corpus and is identical across every call in a batch.

## Prevention

- **Read `cached=` in the first three `usage concepts` lines of any paid batch.** A batch that should be cached and shows `cached=0` is burning money at about 5x; stop it. Stopping is safe: `concepts` resumes and skips every video that already has a `concepts.json`.
- **Price a run, then check it.** Sum the `usage` lines against the rates in `docs/cost-model.md` after the first few minutes, not only after the run. This is how the gap was caught at 25 videos instead of 204.
- **Do not assume implicit caching for any other step either.** The same run showed mind-map-from-transcript calls uncached too; they are small (20k to 60k tokens), so the cost is minor, but an estimate that counts on cache hits needs an explicit cache behind it.

## Related Issues

- PR #271 (the explicit cache), `docs/cost-model.md` (rates and the batch guidance), `.claude/rules/search-index.md` (the review guardrail for `ConceptsPromptCache`).
- (auto memory [claude]) The September 2026 invoice surprise traced to the same step: concepts carries the whole taxonomy and was the largest share of the bill.
