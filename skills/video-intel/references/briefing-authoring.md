# Curated briefing: authoring contract

Rules for the in-session curated briefings this skill routes ("build me a briefing on X"). Generic and user-agnostic: everything reader-specific (pillars, goals, delivery preferences) comes from `<output_dir>/_briefings/audience.md` at authoring time, never from this file.

## Required structure

1. **YAML front matter**: `title`, `date`, `theme`, and `video_ids` listing every video covered. `video_ids` is load-bearing: `load_seen_video_ids()` reads it recursively across `_briefings/**/*.md` to decide what has been surfaced. A video omitted here will be re-surfaced by a later catch-up run as if never briefed.
2. **The one thing** - three sentences maximum: what changed, what to do about it.
3. **Watch these N** - ranked top picks, each with deep link, runtime, and one sentence on why this one for this reader. No separate "featured" slot: the top-ranked item is the lead.
4. **The theme** - the argument the briefing exists to make, carried by evidence from the talks.
5. **Pillar sections** - group the rest under the reader's standing interests (from `audience.md`). Do not force every pillar.
6. **Why it matters to YOU** - concrete hooks into the reader's projects and goals. Name files, decisions, next actions. Requires real reader context; never fabricate personalization.
7. **Signal vs noise** - what to skip and why. Say plainly when a talk is a vendor pitch or a repeat.
8. **Open questions / what I could not verify** - about the video content: unsourced figures, merged transcripts with approximate timestamps, captions-only sources.
9. **What happened while making this** - about the pipeline, not the videos. Include: videos that failed, were recovered, or were degraded, stating the *cause*, not only the consequence ("built from captions" is a consequence; "the multimodal transcript failed and captions were the fallback" is the cause); guard or tooling failures found during the run and whether they are filed; spend, wall-clock, and anything skipped for budget; scope decisions made on the reader's behalf and the triage principle used. Omit the section only when nothing happened - never to keep the briefing tidy.

## Delivery

- Always render the clickable PDF beside the Markdown (`scripts/markdown_pdf.py`).
- Check the reader context for a preferred reading channel. If the reader reads on an e-reader, also build the EPUB in the same batch and place a copy where their device picks it up. A finding that reaches only the chat transcript has not been delivered.

## Deep links

Every video reference carries `https://www.youtube.com/watch?v=<ID>&t=<seconds>`, converted from the mindmap's `(MM:SS)` markers. Never drop the `&t=`. In EPUB the ampersand renders as `&amp;` inside XHTML hrefs - correct, resolves on e-readers. Verify after building:

```bash
unzip -q -o out.epub -d _chk
grep -rhoE 'href="https://www.youtube.com/watch\?v=[A-Za-z0-9_-]+&amp;t=[0-9]+"' _chk | wc -l
```

## Run companion (when subagents authored parts of the work)

Subagent final reports are visible only to the orchestrating assistant, yet they hold the editorial calls, verification limits, and reviewer disagreements the reader cannot reconstruct from the briefing. When a briefing run used subagents, produce one additional artifact in the same formats (md + PDF + EPUB when the reader uses an e-reader):

- Run context: what was asked, what was indexed, cost, scope decisions.
- Each agent's report, verbatim or near-verbatim - do not compress away caveats.
- Operational findings belonging to the run rather than to any one briefing.
- Open decisions carried forward, stated as decisions.

Place it in `_briefings/` root (not a topic subfolder). It carries no `video_ids`, so it cannot disturb seen-state.

## Integrity

- Attribute claims to speaker and video; distinguish what a speaker claimed from what is verified.
- Vendor talks are sales pitches; say so where it matters.
- Never author "why it matters to YOU" from the deterministic ranking alone - that line requires the reader context, which is the point of keeping `audience.md` separate from the machine-scored `profile.yaml`.
