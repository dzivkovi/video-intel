---
date: 2026-06-21
topic: knowledge-gap-detection
---

# Catch-up briefings - surface unseen videos, lightly personalized

## Summary

Add a catch-up mode to the briefing generator: produce a briefing of corpus videos that no existing briefing has surfaced yet, ranked to a lightweight inferred profile. This fills the user's coverage gaps ("there were recommendations last week, but I have holes in what I watched") without building a date-range gap-detection engine - coverage becomes a special case of ranking over the unseen set.

## Problem Frame

Briefings are generated ad hoc, so coverage is lumpy: videos published in weeks that never got a briefing silently never surface. The user notices the hole later.

A second problem appears the moment this is a generic skill: relevance is personal - what is old news to one user is gold to another. But a multi-question onboarding quiz is the friction that kills adoption (the recommender cold-start trap). The signal to bootstrap from already exists: the channels a user chose to scan, and the concept taxonomy those videos produce.

## Key Decisions

These decisions reflect an adversarial peer-review pass (2026-06-21, run via the codex-rescue agent, which answered directly rather than calling the Codex CLI) whose throughline was "less is more - cut to the simplest thing that delivers the value."

- **Coverage is a set difference, not a date-window engine.** The original idea was to diff briefing `scan_window` ranges against the corpus timeline. the review's reframe (adopted): select videos not present in any briefing's `video_ids` across all `_briefings/*.md`. Strict set-based dedup avoids the window-overlap bug and removes all date-boundary math from the core path.
- **Date scoping is an optional flag, not the mechanism.** `--since` / `--until` narrow the unseen set when the user wants a specific period. This preserves the "periods not covered" intent without the gap engine.
- **One cold-start tier, not three.** Infer a starter profile from the scanned channel list plus the top recurring concepts in `taxonomy.json`, and persist it as a readable, hand-editable file. Cut: host-agent seeding from CLAUDE.md/memory (couples the feature to one runtime, breaks headless) and a built-in interactive correction UX (editing the file is the correction path).
- **Dry-run, not a confirmation dialog.** `--dry-run` previews what would be surfaced; without it, generate. No per-gap human-in-the-loop step that gets tedious as the corpus grows.
- **Recency floor against stale backfill.** Default to videos from the last 90 days; `--since` overrides. A six-month-old catch-up about resolved trends is noise, not value.

## Requirements

**Catch-up generation**

- R1. The generator gains an unseen mode that selects corpus videos (from `*.meta.json`) absent from every existing briefing's `video_ids`.
- R2. Selection is strict set difference on `video_ids` across all `_briefings/*.md` - never window-based - so a video surfaced once is never re-surfaced.
- R3. A default recency floor limits the unseen set to roughly the last 90 days; `--since DATE` / `--until DATE` override or narrow it.
- R4. Results are ranked by concept/taxonomy overlap with the profile plus LLM judgment, and written to `_briefings/` with the standard front-matter.
- R5. `--dry-run` reports what would be surfaced (count + titles) and stops; without it, the briefing is generated.

**Personalization (single tier)**

- R6. A per-corpus profile holds a short interest model, inferred on first use from the scanned channel list and the top recurring `taxonomy.json` concepts.
- R7. The profile persists as a readable, hand-editable file so power users can correct it directly. No interactive onboarding flow.
- R8. Ranking labels its relevance framing as that profile's lens (consistent with the existing "relevance lens" convention).

**Prerequisite**

- R9. If date scoping (R3) is implemented, publish-date and any date comparison must be normalized to a single timezone (UTC). The review flagged drift between UTC YouTube timestamps, locally-written `meta.json`, and naive briefing dates as a latent off-by-one. Set-based dedup (R2) keeps this off the core path; it only matters for the `--since/--until` flags.

## Scope Boundaries

Cut or deferred (most on the review's recommendation):

- The date-range gap-detection engine (reduced to set difference + optional date flags).
- Host-agent profile seeding from CLAUDE.md / session memory (runtime coupling).
- Interactive profile-correction UX (hand-edit the file instead).
- A mutable human-readable `audience_profile` tag presented as provenance - if kept, it should be a profile version/hash, since an inferred profile changes and would make old briefings misrepresent how they were ranked.
- Watch-history / completion tracking, ratings UI, collaborative filtering, any trained model. Ranking is taxonomy overlap + LLM judgment.

## Dependencies / Assumptions

- Depends on the Phase-1 briefing convention (`_briefings/` + front-matter with `video_ids`), shipped 2026-06-21.
- Depends on the auto-generator (synthesize a guide from the corpus); this catch-up mode is that generator's first real mode, so they ship together.
- Assumes channels + taxonomy are a good-enough cold-start signal. The review's caution: the taxonomy conflates breadth-of-monitoring with depth-of-interest. Mitigation is the hand-editable profile; the signal to revisit is a high correction rate.

## Outstanding Questions

- Resolve before planning: where does the profile file live - `_briefings/profile.yaml` on the shared corpus, or the user-level config?
- Deferred to planning: the exact recency-floor default (90d is a starting guess) and whether ranking needs recency-weighted concept frequency (Codex: probably not for v1; instrument correction rate first).

## Sources / Research

- Cold-start framing draws on classic recommender practice: implicit feedback (curated channel list) + content-based bootstrapping (taxonomy overlap) to avoid a cold questionnaire.
- A peer-review pass on 2026-06-21 (run via the codex-rescue agent, which answered directly rather than calling the Codex CLI) drove the simplification from a gap-detection engine to the `--unseen` set-difference reframe, the single-tier cold-start, dry-run over confirmation, the recency floor, and the timezone-normalization prerequisite.
- Front-matter schema that makes this cheap (`scan_window`, `video_ids`, `audience_profile`) was designed in the 2026-06-21 `_briefings` brainstorm.
