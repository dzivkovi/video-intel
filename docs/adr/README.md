# Architecture Decision Records

This directory contains Architecture Decision Records (ADRs) for Video Intel.

## What are ADRs?

ADRs document significant architectural decisions made during development, capturing:

- **Context:** Why we needed to make a decision
- **Decision:** What we chose to do
- **Consequences:** Trade-offs and implications
- **Alternatives:** What we considered and rejected

## Format

We use the [Michael Nygard template](template.md):

- **Status:** proposed | accepted | rejected | deprecated | superseded
- **Date:** When the decision was made
- **Context:** The problem or opportunity
- **Decision:** What we're doing
- **Consequences:** Impact (positive and negative)
- **Alternatives:** Options we evaluated
- **Affects:** Source files changed
- **Related Debt:** Todos spawned

## Naming Convention

Files are named: `ADR-NNNN-title-with-dashes.md`

## Current ADRs

| ADR | Title | Status | Date |
| --- | ----- | ------ | ---- |
| [ADR-0001](ADR-0001-gemini-as-multimodal-proxy.md) | Gemini as multimodal proxy, Claude as reasoning layer | accepted | 2026-03-28 |
| [ADR-0002](ADR-0002-three-decoupled-tasks.md) | Three decoupled tasks for transcript quality | accepted | 2026-03-28 |
| [ADR-0003](ADR-0003-single-model-replaces-pipeline.md) | Single model replaces Whisper + pyannote pipeline | accepted | 2026-03-28 |
| [ADR-0004](ADR-0004-external-prompt-files.md) | External self-contained prompt files | accepted | 2026-03-28 |
| [ADR-0005](ADR-0005-error-tracking-via-meta-json.md) | Error tracking via meta.json (DLQ pattern) | accepted | 2026-03-29 |
| [ADR-0006](ADR-0006-idempotency-via-filename.md) | Idempotency via filename convention | accepted | 2026-03-28 |
| [ADR-0007](ADR-0007-per-channel-config.md) | Per-channel configuration | accepted | 2026-03-28 |
| [ADR-0008](ADR-0008-developer-tooling-and-specs.md) | Developer tooling, specs, and Compound Engineering adoption | accepted | 2026-03-30 |
| [ADR-0009](ADR-0009-deterministic-video-discovery.md) | Deterministic video discovery via uploads playlist | accepted | 2026-04-01 |
| [ADR-0010](ADR-0010-llm-concept-normalization.md) | LLM-powered concept normalization (thesaurus layer) | accepted | 2026-04-01 |
| [ADR-0011](ADR-0011-structured-logging.md) | Structured logging via Python logging module | accepted | 2026-04-02 |
| [ADR-0012](ADR-0012-vector-search-lancedb-voyage.md) | Vector search via LanceDB + Voyage AI embeddings | accepted | 2026-04-02 |

## Process

1. **Identify Decision:** Architecture-level choices that impact system design
2. **Draft ADR:** Use [template.md](template.md), fill in all sections
3. **Decide:** Update status to "accepted" or "rejected"
4. **Commit:** Check into Git with descriptive commit message

## Relationship to Other Documentation

- **`README.md`**: User-facing guide (WHAT it does, HOW to use it)
- **`CLAUDE.md`**: Operational context for Claude Code (commands, packaging, release)
- **`ARCHITECTURE.md`**: System overview and vision (WHERE it's heading)
- **`docs/adr/`**: Decisions (WHY we chose this approach)

## Working with AI Assistants

### How to Ask Claude to Create an ADR

**Good prompt:**

```text
Create an ADR for [decision]. Use the template at docs/adr/template.md.
Include these alternatives we discussed: [list alternatives].
```

**What Claude needs to know:**

1. The decision you made
2. Why you needed to make it (context/problem)
3. What alternatives you considered
4. Which files were affected and any debt spawned

**Common mistake:** Asking "document this decision" without specifying template.
Claude might create a generic markdown file instead of following Michael Nygard format.

## References

- [ADR GitHub Organization](https://adr.github.io/)
- [Joel Parker Henderson's ADR Repo](https://github.com/joelparkerhenderson/architecture-decision-record)
