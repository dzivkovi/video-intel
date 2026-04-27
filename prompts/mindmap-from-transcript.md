You are reading a rich transcript of a video. The transcript was produced from the original audio and on-screen content; it preserves what was said AND what was shown.

Each line is one of:
- A speech turn: `[HH:MM:SS] Speaker Name (role): "..."`. Treat the quoted text as primary content and the role parenthetical as context.
- A SCREEN block: `SCREEN [start-end] [type]: description`. These describe what was visible on screen — slides, demos, charts, code, UI overlays, on-screen text. Treat them as first-class content; do NOT skip them just because they are not speech.
- An optional `## Speaker Identification Evidence` footer near the end. Use it only to disambiguate speakers; do not include it as a top-level concept.

The transcript may carry a header note like `**Source:** chunked transcript` and a coverage table — ignore those for the mind map. If you see a `<!-- source: partial transcript ... -->` HTML comment or a `## ⚠️` warning block, the transcript is partial; build the best mind map you can from what is present and do not invent content for missing segments.

Create a thematic mind map of this video in Markdown format.

## Structure

Group by concept (not chronologically). Use 4-6 main branches as noun-phrase headers. Under each branch, add **bold sub-categories**, then bullet the specific insights beneath them. Details must nest under a sub-category — never directly under a main branch.

## Labeling

- Noun phrases only — not sentences, not questions, no leading articles ("The", "A"), no trailing colons
- Use established domain terminology over idiosyncratic phrasing (e.g., "retrieval-augmented generation" not "the RAG thing", "agentic workflows" not "agent stuff")
- Preserve proper nouns as-is: book titles, product names, company names, URLs, CLI commands
- Quality test: could this label work as a folder name or index entry?

## Bullets

- Tight concept phrases (5-10 words), not full sentences
- Include timestamp at end of each bullet, derived from the transcript line where the concept appears (e.g., "concept phrase (3:45)" — drop the leading hour for sub-1h videos, keep `H:MM:SS` for longer)
- Keep key statistics, numbers, benchmarks, and named examples — including those that appear only in SCREEN blocks
- Each concept appears in exactly one place — merge duplicates across themes
- If a bullet wouldn't be worth highlighting, cut it

## Coverage discipline

- Concepts shown only on screen (slides, charts, code) count as concepts. Pull them through.
- Speaker disagreement, named tools, named techniques, and named studies are usually worth a bullet.
- Filler conversation, interview pleasantries, sponsor reads, and intro/outro chatter are not concepts.

## Example

```
## Retrieval-Augmented Generation

* **Architecture Components**
  - Vector store with embedding-based similarity search (2:15)
  - Chunking strategy directly affects retrieval precision (4:30)
  - Reranking layer between retrieval and generation (7:12)

* **Production Tradeoffs**
  - Latency-accuracy tradeoff in chunk size selection (9:45)
  - Hallucination rate drops 40% with source grounding (11:20)
  - Cold-start indexing cost for large document corpora (14:03)
```

Output the mind map in Markdown. Do not wrap the output in ```markdown fences. Do not preface with explanation. The mind map IS the response.
