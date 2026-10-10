# Cost model: what a video really costs

Read this before quoting the cost of any `scan`, `process`, backfill or curated batch. The rule of thumb that misled the first estimate here was "one expensive transcript call plus two cheap text calls". That holds for the mind map. **It is false for concepts**, which on a corpus this size can cost more than the transcript itself.

## Measured unit rates (gemini-3.7-flash, promo pricing)

Derived from a real monthly Gemini invoice (2026, billed in CAD) by dividing each SKU's cost by its token count:

| SKU | Rate (CAD per million tokens) |
| --- | --- |
| Input, text or video (video at `MEDIA_RESOLUTION_LOW`) | 1.04 |
| Cached input (implicit cache hit) | 0.104 |
| Output, including thinking tokens | 5.20 |

These are promo rates that end 2026-12-31 and roughly double after that (see `.claude/rules/evals.md`). Re-derive them from the next invoice rather than trusting this table past that date.

## Cost per video, by step

| Step | What it sends | Typical tokens | Typical cost (CAD) |
| --- | --- | --- | --- |
| Transcript (Gemini watches the video) | the video | ~5,500 input per video-minute, ~450 output per minute | **~0.50 per video-hour** |
| Mind map from transcript | the transcript text | 20k-60k in, ~1k out | 0.03-0.07 |
| Mind map from video (fallback only) | the video again | same as the transcript | ~0.35 per video-hour |
| **Concepts** | **the mind map plus the WHOLE taxonomy** | **~270k in (grows with the corpus), 4k-7k thinking/out** | **0.06 on a cache hit, 0.31 on a miss** |
| Captions transcript (`yt-captions`, or a livestream VOD routed captions-first) | nothing to Gemini | 0 | 0 |

The concepts cost is **per video, independent of video length**, and it grows as `taxonomy.json` grows, because the `{{taxonomy}}` slot in `prompts/concepts.md` carries every concept. On a 20-minute video it can be the most expensive step.

## The cache decides the concepts bill

Gemini's implicit cache only hits when an identical prompt prefix arrives again within a few minutes. So the same work costs very different amounts depending on how it is run:

- **`scan`, and `concepts --channel X`, run concepts calls back to back** and mostly hit the cache. A month of normal scanning showed about four cached text tokens for every uncached one.
- **`process --url` one video at a time puts 3-5 minutes of transcript work between concepts calls**, so they mostly miss. Measured on a 21-video curated batch: concepts cost more than the transcripts, with under a quarter of the concepts input tokens cached.

**Curated batches:** for a hand-picked list, run `transcript --url ... --channel X` and `mindmap --url ... --channel X` per video, then one `concepts --channel X` pass per channel at the end. `concepts` skips any video that already has a `concepts.json`, so the pass only pays for the new ones, back to back.

## Estimating before a run

```text
estimate (CAD) = 0.50 x video-hours on the Gemini transcript path
               + 0.05 x videos                     (mind map from transcript)
               + 0.06 x videos  if concepts run back to back (scan, concepts --channel)
                 0.31 x videos  if concepts run per video (process --url loop)
               + ~10% for chunk retries and quality-guard re-runs
```

Videos routed to captions (a `yt-captions` channel, or a livestream VOD) drop the first term to zero but still pay the mind-map and concepts terms. `scan --dry-run` gives the video list; the duration of each video comes from the YouTube API at no Gemini cost.

**Checking an estimate afterwards:** every Gemini call logs a `usage <step> prompt=N cached=N thoughts=N candidates=N` line. Summing those lines from a run log and applying the rates above reproduces the bill to within a few percent. The billing console lags by several hours, so a run from the same evening does not show there yet.

## Where a month's bill goes

Share of one month's Gemini bill for a corpus of a few thousand videos:

| SKU | Share |
| --- | --- |
| Output text (transcript JSON, mind maps, thinking) | 44% |
| Input text (by prompt size mostly concepts; also mind maps from transcript) | 24% |
| Input video (the multimodal transcripts) | 16% |
| Cached input text | 11% |
| Everything else (storage, secrets, other models) | 5% |

Watching the video was a sixth of the bill. Output tokens and the taxonomy carried by every concepts call were most of it.
