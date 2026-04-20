---
review_agents:
  - kieran-python-reviewer
  - security-sentinel
  - performance-oracle
  - architecture-strategist
  - agent-native-reviewer
---

# Compound Engineering Config for video-intel

Project context passed to every review agent invoked by `/workflows:review`.
Read this before assessing any PR.

## What this project is

A Claude Code plugin that uses Google's Gemini multimodal API as a proxy to
analyze YouTube videos (vision + audio + on-screen text). Claude triages the
resulting markdown artifacts; it does not call Gemini directly. Two skills
ship together: `video-intel` (scan / transcript / mindmap / concepts /
search) and `translate-bcs` (BCS subtitle translation — operationally
independent from video-intel).

The reasoning substrate is a three-layer pipeline, documented in
[`ARCHITECTURE.md`](ARCHITECTURE.md):

1. **Source artifacts.** Per-video `meta.json`, `mindmap.md`, `transcript.md`,
   `concepts.json`. All filesystem-based markdown.
2. **Concept normalization.** `taxonomy.json` at the output-dir root —
   canonical concept vocabulary built by the LLM (ADR-0010). Concepts have
   preferred labels and alias lists.
3. **Retrieval.** `video_intel search` (concept/taxonomy match) and
   `video_intel search --vector` (BM25 + vector + RRF fusion on LanceDB
   via Voyage AI embeddings). See ADR-0012, ADR-0013.

## What reviewers must know

### Retrieval quality is measured, not guessed

As of 2026-04-19, hybrid search scores **1 of 25** on the grounded golden
dataset at `tests/evals/golden_dataset.yaml`. The staged KB-layer strategy
driving near-term work is in [ADR-0017](docs/adr/ADR-0017-kb-layer-strategy.md).

**Any PR that touches retrieval logic must re-run `pytest tests/evals/` and
record the new N/25 in the PR description.** Regressions from 1/25 are
extremely low-bar and extremely important — the baseline is the floor we're
building up from, not a ceiling we can afford to drop below.

### Cost-sensitive paths

These operations spend real money. Flag any PR that changes their control
flow without tests or explicit cost analysis:

- `build_search_index` calls Voyage AI (~$0.02/M tokens × ~5,300 chunks per
  full re-index).
- `scan` calls Gemini Flash / Pro per video (~$0.01–$0.05 per 1-hour
  video depending on model).
- `transcript` on long videos uses Gemini Pro with `max_output_tokens=65536`.
  Malformed JSON triggers one bounded retry; a second failure falls through
  to salvage + partial-write. Do not add unbounded retries.

[ADR-0016](docs/adr/ADR-0016-vector-db-path-config.md) established a hard
rule: the pre-flight `probe_atomic_writes()` runs *before* any Voyage call.
A bug there costs ~$0.30 per failed run. Reviewers: verify any rework of
`build_search_index` preserves that probe-first ordering.

### Timestamps are data, not decoration

Every retrieved chunk has `timestamp_seconds`. The eval harness scores
`TimestampPrecisionMetric` as a gating metric. Changes to chunking,
dedup, or rendering that drop or corrupt timestamps break user-visible
behavior (timestamped links to YouTube `&t=<seconds>`). Reviewers:
grep for `timestamp_seconds` in any diff touching `hybrid_search`,
`_dedup_by_video`, or chunk rendering.

A related memory: the user explicitly never wants `&t=<seconds>`
stripped from YouTube URLs in search-result summaries.

### Provenance tracking matters

The golden dataset has two provenance types — `manual_verified` and
`concept_augmented_verified` — because not all channels produce transcripts
(`seankochel`'s `auto_transcript: none` means mindmap-only). Any new
retrieval / synthesis layer must respect that distinction. Do not assume
every video has a transcript.

### Filesystem-atomicity gotchas

`output_dir` commonly lives on Google Drive File Stream (confirmed
working). The `vector_db_dir` separately may *not* — GDFS breaks LanceDB's
atomic-rename commit path. Any new on-disk store (Stage 2 / Stage 3 from
ADR-0017) must follow the ADR-0016 pattern: pre-flight integration probe
using the actual library as the oracle, not a mechanism probe of
`os.replace`. Mechanism probes empirically produced false negatives and
burned ~$0.30 of embedding cost per run during the ADR-0016 investigation.

### Skill-parity rule

This is a Claude Code plugin. Any CLI capability should be reachable via
the skill's natural-language protocol (SKILL.md routing). When a PR adds a
new subcommand / flag, the corresponding SKILL.md entry is part of the
diff, not a follow-up. Agent-native-reviewer flags violations.

### Python style

Ruff-formatted, type-hinted, pytest-driven, TDD-preferred. The user's
stated bias is minimalism — YAGNI per [`specs/agent-rules.md`](specs/agent-rules.md)
§1 is binding. Don't add config knobs, feature flags, or abstraction layers
that this PR doesn't require.

### Commit discipline

- Only commit when the user explicitly asks.
- Never push straight to `main` — always a branch + PR, even for docs-only
  changes.
- Prefer follow-up commits over `--amend` once a PR is under peer review.
- Incremental commits within a PR are fine; WIP commits are not.

## Pointers reviewers will need

- [`CLAUDE.md`](CLAUDE.md) — operational rules, commands, architecture
  overview. Authoritative when this file disagrees.
- [`specs/agent-rules.md`](specs/agent-rules.md) — agent constraints;
  `CLAUDE.md` declares this MUST-read pre-task.
- [`docs/adr/`](docs/adr/) — decision records. Read the relevant ADR
  before critiquing a code path.
- [`docs/testing.md`](docs/testing.md) — testing philosophy, eval harness,
  current baseline.
- [`docs/search-internals.md`](docs/search-internals.md) — hybrid search
  mechanics; the eval measures this pipeline.

## Out of scope for review agents

- **Docs plugins artifacts** — `docs/plans/*.md` and `docs/solutions/*.md`
  are living / append-only documents; do not flag for deletion or
  rewriting.
- **`work/` directory** — ephemeral session notes. Never ask for
  cleanup, never treat as authoritative.
- **`plans/` at repo root** — session plan artifacts (historical).
  Different from `docs/plans/` (durable).
