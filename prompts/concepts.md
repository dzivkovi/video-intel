Extract key concepts from this mind map and normalize them against the existing taxonomy provided below.

Return a JSON object with a single key `concepts` containing an array of extracted concepts.

## Rules

- Scale concept count with content density: roughly 1 concept per 3-5 minutes of video. A short (1-3 min) may have 1-2 concepts. A 10-minute video may have 3-5. A 30-minute deep-dive may have 8-10. Use the timestamps in the mind map to judge duration.
- Each concept must be a **noun phrase** using established domain terminology
- Prefer branch-level concepts over sub-category details. Roll up implementation details to their parent concept — "Path-scoped rules" and "Configuration hierarchy" both roll up to "AI Agent Configuration" if they're sub-details of the same idea.
- Skip generic structural labels: "Introduction", "Conclusion", "Resources Mentioned", "Overview"
- Skip proper nouns (product names, company names, person names) UNLESS they represent a concept category — "Claude Code" is a product, "AI Coding Assistants" is a concept

## Normalization

- If the existing taxonomy contains a matching concept, reuse its `concept_id` and `preferred_label` exactly
- Match semantically, not just by string: "Agent-Centric Engineering" matches existing "Multi-Agent Orchestration" if they describe the same idea
- When uncertain whether two concepts are the same, use `"status": "uncertain"` — do not force a merge
- For genuinely new concepts, generate `concept_id` as `{domain}.{snake_case_of_preferred_label}`
- Never regenerate a `concept_id` for an existing concept — reuse it as-is from the taxonomy

## Output Schema

Each concept object must have these fields:

- `concept_id` — stable identifier: `{domain}.{snake_case}` (reuse from taxonomy when matching)
- `preferred_label` — human-readable canonical name (reuse from taxonomy when matching)
- `as_mentioned` — what this mind map actually called it (exact branch or sub-category name)
- `branch` — the top-level mind map heading this concept appears under
- `confidence` — 0.0 to 1.0, your confidence in the normalization
- `status` — one of: `matched` (confident match to existing), `new` (no match found), `uncertain` (borderline — could be existing or new)
- `domain` — broad domain tag (e.g., `ai-engineering`, `productivity`, `software-architecture`)

## Existing Taxonomy

```json
{{taxonomy}}
```
