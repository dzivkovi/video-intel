# The topics layer

A user guide. What it answers, what you actually do, and when to ignore it.

## The one-sentence version

Your briefing folders were already the answer to "why is this channel in my corpus"; the topics layer just makes that answer queryable, without you maintaining anything new.

## The question it exists to answer

The corpus grows two ways. Some channels you follow. Others arrive because a research thread pulled in one video: you were reading about forward-deployed engineering, so `a16z`, `altimeter`, `southparkcommons`, `goodfirms` and `liamottley` each contributed exactly one video and were never heard from again.

Six months later that tail is unreadable. `a16z` has one video and no explanation. The reason lived in a config comment, or a chat session that scrolled away.

`taxonomy.json` cannot help, and this is the important distinction:

| Layer | Answers | Direction |
| --- | --- | --- |
| `taxonomy.json` | what the video **says** | bottom-up, emergent from content |
| `topics.json` | why **you pulled it in** | top-down, your curation intent |

Both are true at once and they must not be mixed. A video can be *about* agent orchestration while the reason it is in your corpus is *the FDE thread*.

## The part that surprises people: you already did the work

You have been filing curated briefings into `_briefings/fde/`, `_briefings/sales/`, `_briefings/evals/` for months. Each of those carries a `video_ids:` front-matter list.

**That act of curation was the topic assignment.** Nobody had to tag anything. The topics layer reads what is already on disk.

So the retroactive cost was zero. The first build on your corpus produced 11 topics and 239 memberships out of 16 briefings, with **zero** unresolved ids, and you did nothing to prepare it.

## What you actually do

### Day to day: nothing

Curate briefings the way you already do. Put them in a topic folder. That is the whole workflow.

### After you add or edit briefings: rebuild

```bash
python scripts/video_intel.py topics-build
```

Derived and byte-stable, exactly like `taxonomy-build`. Run it as often as you like; identical inputs give an identical file. Add `--dry-run` to see what it *would* derive without writing.

### To read it back

```bash
python scripts/video_intel.py status                        # per-channel rollup
python scripts/video_intel.py search "positioning" --topic fde
```

`status` is the "why is this channel here" surface:

```text
  a16z: 1 mindmaps, 1 transcripts, 1 concepts
    topics: fde
  peterahnsales: 11 mindmaps, 11 transcripts, 11 concepts
    topics: fde
  gregisenberg: 47 mindmaps, 47 transcripts, 47 concepts
    topics: ai-engineering, differentiation, fde, marketing, operator-brain, sales
```

### The one new flag, for the gap before a briefing exists

`--topic` on `process` / `transcript` / `mindmap`, repeatable:

```bash
python scripts/video_intel.py process --url "https://youtu.be/XXXX" --channel a16z --topic fde
```

Use it when you pull a video in *now* and the briefing will come *later* (or never). It records the intent at the moment you know it.

It also works as a pure backfill on a video you already have. When every stage lazy-skips because the artifacts exist, the tag is still written and **no Gemini call is made** - that is the case the flag was designed for:

```text
Step 1/3 transcript -> skipped (exists)
Step 2/3 mindmap    -> skipped (exists)
Step 3/3 concepts   -> skipped (exists)
topics for ...meta.json: operator-brain, sandboxing
```

Slugs normalize, so `FDE`, `fde` and `fde/` are one topic, and `--topic "founder led sales"` becomes `founder-led-sales`.

## You do not need to type any of this

Both skills route the natural phrasing. Say it the way you think it:

| You say | What runs |
| --- | --- |
| "why is this channel in my corpus?" | `topics-build` + `status` |
| "which channels belong to my FDE thread?" | `status` rollup |
| "rebuild the topic index" | `topics-build` |
| "tag this one for the FDE thread" | `--topic fde` |
| "search my FDE thread for positioning" | `search "positioning" --topic fde` |
| "what did I pull in for evals?" | `search --topic evals` |

## Two behaviors that look wrong and are not

**1. A filtered search can return videos the unfiltered search did not show.**

`search "X" --topic fde` is not "filter the top 20 results down to FDE". It means "the top N *within* FDE". The scope reaches retrieval itself rather than the ranked output, so a member never has to beat the rest of the corpus for a result slot: a genuine FDE video that would have ranked 340th corpus-wide still surfaces. The two modes get there differently. `--vector` scopes at the search index, before anything is ranked. Concept search ranks its corpus-wide concept matches first and then applies the scope before the result cap, which is equally exact because it has the whole matching set in hand at that point. Without that, every topic member below the display cap would be invisible.

A member's corpus-wide *video* rank is irrelevant at any depth, in both modes. `--limit` still caps how many results you see, as it does for any search; raise it on a big topic to see more of them.

One cliff remains, and only in concept search. When your query has no exact concept match, concept search picks the five best partial-matching concepts and looks up videos for those alone. That cut happens before the topic scope, so a member whose only relevant concept ranked sixth stays invisible however high you set `--limit`. If a member you expected is missing from a concept search, that is the reason: use `--vector` (no such cut), or query the concept's own label. `search --topic <slug>` with no query always lists every member regardless.

This replaced an earlier `limit * 5` over-fetch (retired in issue #203), which was a probability improvement rather than a guarantee and starved in practice: a 19-member topic had to out-rank 2,300+ other videos for 25 pool slots, and the scoped search returned nothing at all for a question the unscoped search answered well.

**2. Renaming a topic folder renames the topic.**

This is deliberate and it is a change from the old rule. Briefing folder names used to be meaningless to the code. They still are for *briefing selection and seen-state* - moving a briefing between folders never re-surfaces its videos. But for *topic derivation* the first folder under `_briefings/` **is** the topic name.

`_briefings/fde/deep-dives/note.md` is topic `fde`, never `deep-dives`. A briefing sitting directly in `_briefings/` root asserts no topic. `_briefings/nuggets/` is reserved and excluded - nugget briefs are synthesis output, not curation.

So renaming `_briefings/fde/` to `_briefings/forward-deployed/` re-slugs every membership on the next build. That is the intended way to rename a topic.

## Removing things

There is no `--remove-topic`, by design. The build never argues with its inputs, so you remove at the source:

- delete the briefing, or remove the id from its `video_ids:` list, or
- delete the slug from the video's `meta.json` `topics` list

then rebuild. A membership with no remaining assertion disappears.

## Where things live

| File | What it is | Hand-edit? |
| --- | --- | --- |
| `_briefings/<topic>/*.md` | your curated briefings, the primary assertion | yes, this is your work |
| `<video>.meta.json` `topics` | per-video stamps from `--topic` | rarely, but it is the removal path |
| `topics.json` | the derived join at the corpus root | **never** - rebuilt from the two above |

`topics.json` is disposable. Delete it and rebuild; nothing else is touched, and `taxonomy.json` is never involved.

## When it degrades

Absent or unparseable `topics.json` does not crash anything. `status` and `search --topic` print one message naming `topics-build` and carry on. An unknown topic lists the ones that do exist.

A briefing id that resolves to no corpus artifact is kept and flagged `unresolved`, excluded from the channel rollup, and counted in the build summary - so a typo in a `video_ids:` list is visible rather than silently dropped.

## What it deliberately does not do

- **No influence on ranking.** Topics are provenance. They do not touch `briefings --unseen` relevance or the interest model.
- **No automatic inference.** A topic is something you asserted, never something the tool guessed from concepts.
- **No config schema.** Nothing to maintain in `config.yaml`; channel membership is derived from video-level facts.
- **No topic pages in `_wiki/`.** Possible later if the artifact proves useful.
