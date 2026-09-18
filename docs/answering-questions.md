# Answering questions: which tool, and when

This corpus is not the only tool you have. YouTube's own Ask feature is live, free, and better than this repo at a whole class of question. Knowing which half answers what is the difference between a five-second answer and a week of building the wrong thing.

Written after issue #228, where a real defect led to a feature that a free interactive tool already did better. The reasoning is in [`solutions/process-issues/metric-worthiness-named-entity-recall-20260918.md`](solutions/process-issues/metric-worthiness-named-entity-recall-20260918.md).

## The division of labour

| | YouTube Ask (in the player) | This corpus |
|---|---|---|
| Scope | One video, the one you are watching | ~2,800 videos, ~91 creators, years deep |
| Cost | Free | Gemini per ingest, Voyage per index |
| Freshness | Current, no ingest needed | Only what has been scanned |
| Strength | "What is in THIS video" | "Across everything, who / when / how often" |
| Weakness | No memory of any other video | Cannot see a video you never ingested |

**Ask is better than this repo at naming things inside one video.** It returns tools, repos, people and timestamps directly from the video you are on, with no ingest and no prompt tuning. Do not build a static extractor to compete with it.

**This repo is better at everything that needs more than one video.** Ask has no memory across videos, cannot tell you that this is the fourth mention this month, and cannot tell you who said it first.

## The loop

The two halves are a cycle, not a choice:

1. A search or briefing surfaces a video worth your time.
2. You watch it. **Ask does the in-video extraction** - repos, names, claims, with timestamps.
3. You internalize, and you now know a name you did not know an hour ago.
4. **You search the corpus for that name** to find everywhere else it appears, who else said it, and when it first showed up.
5. What you learn feeds curation: a `--topic` stamp, a channel added or disabled, a note in `audience.md`.

Step 2 is not this tool's job. Steps 1, 4 and 5 are.

## Question shapes and where they go

All commands run from the plugin root as `python scripts/video_intel.py <command>`.

### "What tools/repos were in that video I just watched?"

**YouTube Ask.** Not this tool. It is live, free, timestamped, and needs no ingest.

### "Where else has this thing been mentioned?"

```bash
search "archify" --vector
```

Hybrid BM25 + vector over transcript chunks. Returns ranked videos with `&t=<seconds>` deep-links. Works for tool names, people, and phrases - the index is built from **transcripts**, so anything spoken or on screen is reachable whether or not the mindmap carried it.

### "Has [creator] ever talked about [idea]?"

```bash
search "permission problems" --vector --channel natebjones
```

Also the only correct way to verify a paraphrase or a quote. Never grep the corpus for this - the speaker's words almost never match the paraphrase verbatim.

### "What concepts does my corpus know about [subject]?"

```bash
search "prompt engineering"
```

Concept mode, ranked specificity-first against `taxonomy.json`. **Note the deliberate limit: the taxonomy holds ideas, not names.** `prompts/concepts.md` skips proper nouns on purpose, so searching concept mode for `Archify` or `LanceDB` returns nothing. Use `--vector` for names.

### "What have I not read yet?"

```bash
briefings --unseen --dry-run    # preview the ranked candidates
briefings --unseen              # write the briefing
```

Strict set difference against every `video_ids` list in `_briefings/`, ranked against your interest profile. Unbounded by date - an old video you never briefed is still a candidate.

### "Why am I being shown this?"

```bash
profile show
```

Prints the resolved interest model and the paths to `_briefings/audience.md` (your prose preferences) and `_briefings/profile.yaml` (the weights). Writes nothing. Edit `audience.md` by hand to retune; back it up first, nothing regenerates it.

### "What does my corpus say about X, synthesized?"

```bash
nugget "context engineering"
nugget --topic fde
```

Cross-creator synthesis with citations, written to `_briefings/nuggets/`. `--topic` scopes retrieval to that topic's members at the index level, so a thread's own videos do not have to out-rank the other 2,700.

### "What is in this thread I have been curating?"

```bash
search --topic fde              # pure listing, no retrieval
search --topic fde "evals"      # scoped search
```

Topics come from briefing folders and `--topic` stamps; rebuild with `topics-build`.

### "Who talks about this first?" / "Is this spiking?"

```bash
python scripts/lead_lag_report.py
python scripts/burst_report.py
python scripts/sdsm_network.py
```

The analytics layer. Lead-lag finds creators who mention things before others; burst finds spikes against a lifetime base rate; SDSM measures creator independence, which is what stops an echo cascade reading as corroboration.

## What this tool cannot do

Stated plainly, because knowing the limits is what prevents building around them.

- **It cannot enumerate names you do not already know.** Search answers "where did X appear"; it cannot answer "which tools should I know about". That is an aggregate question and the corpus has no entity layer - the taxonomy is concepts only, by design.
- **It cannot see a video you never ingested.** Scan windows are cost guardrails; a creator's back catalogue outside the window does not exist to any query.
- **It cannot tell you what is true.** It tells you who said what, when, with a timestamp. Provenance is the product; verification is yours.
- **Popularity is not corroboration.** Ten reaction videos to one tweet is one source. `sdsm_network.py` is the check.
- **The mindmap is a reading surface, not an index.** Retrieval reads transcripts. A thin or badly-named mindmap does not degrade search.

## Ad hoc beats a feature

Some questions are worth one throwaway script and no code. Example: "which people appear on three or more of my channels" is a regex over the `[ts] Name (role):` lines that every Gemini transcript already carries - about a minute of work, no new subcommand, no maintenance. Running it once told us Boris Cherny appears on 9 channels and that the top of the list is junk (`Narrator`, `Host`, `Audience Member`).

**Ask for the answer before asking for the feature.** If the same question comes back three times, that is the trigger to build it.
