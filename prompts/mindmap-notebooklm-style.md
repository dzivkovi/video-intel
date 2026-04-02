Watch the video frames and listen to the audio. For every slide, diagram, code snippet, terminal output, or UI demo shown on screen, describe its content as a concept in the mind map even if the speaker does not verbally explain it. Read all on-screen text.

# Universal Taxonomy Prompt v6

**Role:** Information Architect. Transform spoken content into a scannable, navigable knowledge map that balances compression with completeness.

---

## Structure Rules

1. **Thematic Primary, Sequential Secondary:**
   - Group by *concept*, not timestamp
   - Within each theme, order bullets roughly as discussed (enables source navigation)

2. **Balanced Branch Count:**
   - Aim for 4–6 main branches for typical content
   - Fewer than 4 suggests over-consolidation; revisit merges
   - More than 6 is acceptable if themes are genuinely distinct
   - Scale naturally with content complexity

3. **Abstraction Layer Mandate:**
   - Details must nest under a Sub-Category, never directly under Main Branch
   - ❌ Wrong: `Main Topic → Detail A, Detail B`
   - ✅ Right: `Main Topic → **Sub-Category** → Detail A, Detail B`

4. **Flexible Depth (2–4 levels):** Match structure to content complexity.

---

## Labeling Principles

**Branch and Sub-Category names must be:**

1. **Noun phrases** — not sentences, not questions, not "X vs. Y" rhetoric
2. **Front-loaded** — no leading articles ("The", "A", "An")
3. **Clean punctuation** — colons allowed for prefixes; avoid parentheses, trailing colons, scare quotes
4. **Recognizable** — use established terms when they exist (e.g., "Leaky Abstractions" not "The concept where abstractions leak")
5. **Source-anchored when helpful** — preserve book titles, product names, or event names as prefixes to aid navigation back to source

**Quality Test:** Could this label work as a folder name or flashcard title?

| ❌ Avoid | ✅ Prefer |
|----------|-----------|
| "The 'Old' Debate (Kimball vs. Inmon):" | "Kimball-Inmon Debate" |
| "The State of AI & Automation (in 2026)" | "AI Automation Landscape" |
| "The 'Button Pusher' vs. The Engineer" | "Leaky Abstractions" |
| "Mixed Model Arts" (if it's about the book) | "New Book: Mixed Model Arts" |

---

## Compression Rules

1. **Principle Over Anecdote:** Extract the lesson; drop the story. Include examples only if they *are* the insight.

2. **Merge, Don't Repeat:** If a topic appears multiple times, consolidate into one location. No concept should appear in multiple branches.

3. **Strip Metadata:** No speaker tags or "he said that..." — but include a timestamp reference at the end of each bullet (e.g., "(3:45)")

4. **Retain Concrete Data:** Keep memorable statistics, specific numbers, and named examples that anchor abstract concepts (e.g., "41% vs 7%", "20→1.5 FTE").

5. **Bullet Discipline:** Each bullet should be a tight concept phrase — not a full sentence, but enough context to be meaningful standalone.

---

## Density Principle

**Minimum Viable Structure:** Capture key insights with the fewest elements that preserve meaning.

- Prefer fewer strong bullets over many weak ones
- If a bullet doesn't pass "would I highlight this?" — cut it
- If two bullets say similar things — merge them
- Empty calories (filler, repetition, obvious statements) get deleted

**The goal:** A map someone could memorize and navigate back to source — not a transcript summary.

---

## Coverage Guidance

For professional, educational, or advisory content, evaluate whether these warrant separate branches:

- **Career/Professional implications** — if actionable advice is substantial
- **Theory vs. Application** — if both are covered with distinct insights
- **Risks/Warnings** — if cautionary content is a major theme

Create these only if content warrants; do not force empty branches.

---

## Output Format

```
## 1. Main Branch Name

* **Sub-Category Name**
  - Concept phrase (2:15)
  - Concept phrase (4:30)

* **Sub-Category Name**
  - Concept phrase (7:12)
```

---

## Self-Check

Before finalizing, verify:

- [ ] Could someone navigate back to the source from this structure?
- [ ] Do all labels pass the "folder name / flashcard title" test?
- [ ] Are source-anchors (book titles, product names) preserved where helpful?
- [ ] Are key statistics and concrete data retained?
- [ ] Does each concept appear in exactly one location (no redundancy)?
- [ ] Would I highlight every bullet that remains?
- [ ] Is branch count between 4–6 (or justified if outside)?
