Watch the video frames and listen to the audio. When concepts are shown visually (slides, diagrams, code, demos) but not fully described in speech, include them as concepts in the mind map with their visual details. Read all on-screen text.

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
- Include timestamp at end of each bullet (e.g., "concept phrase (3:45)")
- Keep key statistics, numbers, benchmarks, and named examples
- Each concept appears in exactly one place — merge duplicates across themes
- If a bullet wouldn't be worth highlighting, cut it

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
