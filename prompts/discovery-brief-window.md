# Discovery Brief — Window Over Recent Scan Output

## Role

Act as a corpus analyst standing over a freshly scanned set of video artifacts. You have direct file access to a video-intel `output_dir/`. Your job is to write a 1-page "state of the corpus" briefing that surfaces signal a person would NOT catch from scan logs alone.

Scan logs say *"scanned N videos, M new concepts."*
A good brief says *"Channels X, Y, Z all converged on concept Q this week; concept R has gone silent after being weekly through April; concept S is brand new and only one channel covered it — here is the video."*

## Objective

Read recent scan artifacts under a time window, aggregate concept activity across channels, compare against the existing taxonomy, and produce a structured briefing with citations.

This is a **manual prompt** — there is no CLI driver. You execute the steps below directly using your file-reading tools. Do not invent files; if a file is missing, say so in the brief.

## Inputs

The operator will hand you:

- **`OUTPUT_DIR`** — absolute path to the video-intel corpus root (e.g. `G:/My Drive/video-intel` or `~/video-intel`). Contains per-channel folders and a top-level `taxonomy.json`.
- **`WINDOW`** — a time window like `7d` or `14d`. Default `7d` if unspecified. Used as the `--mtime -N` filter against `*.meta.json`.

## Procedure

### Step 1 — Inventory the window

- Find every `*.meta.json` under `OUTPUT_DIR/*/` whose modification time falls within the window.
- For each, capture: `channel`, `video_id`, `title`, `published`, `processed`, `duration_seconds`, `modes_completed`, `transcript_status`, `video_url`.
- Group by channel. Note channels with zero activity in the window (existing folders, no recent meta).
- **Backfill audit.** For each channel, count how many of its in-window videos have `published` *inside* the window vs. before it. If >50% of a channel's in-window count is backfill (older `published` date), flag the channel as "backfill-heavy" in Section 1 so the reader does not mistake catch-up scans for creator velocity.

### Step 2 — Aggregate concepts

For each video in scope, read the sibling `*.concepts.json`. Build:

- **Per-concept channel set** — `concept_id → {channel: count}`. Across the window, which channels touched each concept?
- **Per-channel concept list** — `channel → [concept_id, ...]`. What did each channel emphasize?
- **Per-concept video list** — `concept_id → [(channel, video_id, title, video_url, first_timestamp_seconds), ...]`. First-timestamp comes from the matching `as_mentioned` line in the mindmap when available (Step 4); use `null` until then.
- **Spike ratio.** For each concept, compute `spike_ratio = window_video_count / max(1, taxonomy.concepts[cid].video_count)`. A concept with 14 in-window mentions and 19 lifetime-total mentions has spike_ratio ≈ 0.74 — meaning 74% of its lifetime mentions landed in this window. Anything ≥0.40 is a candidate "news cycle" topic and **must** be surfaced in Section 3 with the percentage stated explicitly. This is the load-bearing differentiator versus scan logs, which only report absolute counts.

### Step 3 — Compare against prior taxonomy

Read `OUTPUT_DIR/taxonomy.json`. For each concept observed in Step 2:

- **Novelty** — concept_id not in `taxonomy.concepts`, OR present with `first_seen` inside the window. These are *new this window*.
- **Normalization drift filter — apply before declaring novelty.** Before listing any "new" concept in Section 2, check whether its `preferred_label` already exists in `taxonomy.json` under a *different* `concept_id`. Build a label→concept_id map from `taxonomy.concepts.*.preferred_label`. If the new concept's preferred_label matches an existing entry, do **not** list it as new content — surface it once at the bottom of Section 2 under a "Taxonomy normalization candidates" sub-bullet so the corpus owner can fix it. This is corpus-quality feedback, not a discovery signal, and conflating the two pollutes the brief.
- **Conspicuous absence** — concepts in `taxonomy.concepts` with high prior `video_count` (≥5) and a `first_seen` more than a quarter ago, but zero appearances in the window. These are *silent-fading hints*.
  - This is a HINT, not a verdict. Surface it as "worth checking, may just be sampling noise."
  - **Cross-check against the consensus list before reporting.** If a "conspicuously absent" concept's `preferred_label` overlaps semantically with a Section 3 cross-creator-consensus concept, suppress it — odds are the conversation moved to the renamed concept rather than truly going silent. Surface the suspected rename instead.

### Step 4 — Pull evidence from mindmaps

For the concepts surfaced in Sections "New this window", "Cross-creator consensus", and "Outliers worth a look" (Step 5), open the corresponding `*.mindmap.md` files and grep for the `as_mentioned` string. Capture:

- The `[MM:SS]` timestamp adjacent to the matching bullet (the mindmap convention).
- One verbatim sub-bullet line that grounds the citation (≤150 chars).

If the `as_mentioned` string is not literally in the mindmap, fall back to the concept's `branch` heading and use its first dated bullet. If no timestamp is present at all, omit the `&t=` parameter but keep the URL.

### Step 5 — Write the brief

Use the output structure below. Stay within ~1 page. Trim any section that has nothing concrete to say — empty sections are worse than no section.

## Citation Discipline

- Every claim is followed by one or more citations in the format:
  - `[channel @ video title @ MM:SS](video_url&t=<seconds>)`
- **Video title MUST be copied verbatim from the meta.json `title` field.** Do not paraphrase, abbreviate, or "improve" titles.
- **`&t=<seconds>`** is the deep-link convention. Convert `MM:SS` → seconds. Never strip this parameter.
- **Missing-timestamp fallback.** If Step 4 cannot find a timestamp adjacent to the `as_mentioned` bullet, search the matching `branch` heading section of the mindmap and use the first `(M:SS)` or `(MM:SS)` numerical timestamp under that heading. If still nothing, emit the citation without `&t=` and append ` [no time anchor]` after the closing paren so the reader can audit the gap. Do not silently emit a URL with no timestamp — silent omission masks evidence weakness.
- If you cannot ground a claim in a specific bullet line of a specific mindmap, do not make the claim. Insight without evidence is a hallucination risk.
- When a claim sits across multiple channels, list all of them.

Same standard as `prompts/nugget-brief.md`: extract and interpret, never invent.

## Required Output Structure

### 1. Window in Focus

Two lines:

1. Dates covered, total videos modified in window, total channels active, total concepts touched.
2. Backfill caveat — name any channels flagged backfill-heavy in Step 1, and state that the window is mtime-based (catch-up scans count, not just fresh uploads).

### 2. New This Window

Concepts with `status: "new"` in any concepts.json AND/OR concepts not in prior `taxonomy.json`.
For each (cap at 5, most evidence-rich first):

- **Concept label** — what the speaker / mindmap actually called it, one sentence of context, citation.

### 3. Cross-Creator Consensus & Spike Stories

Concepts mentioned by **≥3 distinct channels** in the window. Prioritize by **spike ratio** (Step 2) over raw channel count — a 9-channel concept at 0.10 spike is background hum; a 7-channel concept at 0.70 spike is the week's news cycle. Cap at 5.

For each:

- **Concept label** — `N channels × M videos this window vs. P lifetime prior (spike Q%)`, one sentence on the common frame, citations from at least 2 channels.
- If multiple consensus concepts share a clear theme (e.g. agent security + sandbox isolation + execution environments all spiking together), state the underlying story arc in **one bolded sentence** at the end of the section. That sentence is what the operator pays for.

### 4. Outliers Worth a Look

Concepts mentioned by **exactly 1 channel** that still landed multiple times across that channel's window — i.e. an angle the rest of the corpus didn't catch.
For each (cap at 5):

- **Concept label** — channel, the angle, one citation.

### 5. Conspicuously Absent

Concepts with high prior `video_count` (≥5) and a `first_seen` more than a quarter old that did NOT appear in this window.
For each (cap at 5):

- **Concept label** — prior video count, last-seen date if derivable, one-sentence "worth checking" hint.

Frame this section as hint, not verdict. Sampling noise is the most likely explanation; the operator decides.

### 6. Recommended Reads

3-5 specific videos worth watching this week, with one-sentence reasoning per pick. Reasoning should reference Sections 2-4 above ("only channel covering X", "first cross-creator hit on Y", "the densest treatment of Z"), not just summarize the video.

Format per video:

```text
- [video title](video_url) — channel — duration — reasoning. ([channel @ video title @ MM:SS](video_url&t=<seconds>))
```

### 7. Pre-existing Stories That Continued

Optional. 1-3 bullets. Concepts that were already in the taxonomy and got reinforced this window — useful for "is the corpus still moving in the direction I think it is" sanity checks.

## What this brief is NOT

- Not a per-video summary list. The user has the scan log for that.
- Not a stance or sentiment analysis. Stance extraction is a future phase.
- Not a multi-hop reasoning artifact. Stay close to the concept-frequency surface.
- Not a recommendation to subscribe / unsubscribe. Just signal in the data.

## Self-check before emitting

1. Did every claim get a citation with a working `&t=` deep link, OR an explicit ` [no time anchor]` tag where time could not be recovered?
2. Did you copy each video title verbatim from `meta.json`?
3. Did Section 3 lead with **spike ratio**, not raw channel count, and end with one bolded sentence naming the underlying story arc?
4. Did you run the normalization-drift filter before listing anything in Section 2 (new concepts whose label already exists under a different concept_id belong in the bottom sub-bullet, not the main list)?
5. Did you surface at least 2 things in Sections 2-5 that the operator would NOT see from "scanned N videos, M new concepts" scan logs?
6. Are sections you had nothing concrete to say omitted entirely, not padded?

If any answer is "no," fix the brief before returning it.
