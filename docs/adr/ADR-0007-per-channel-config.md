# Per-Channel Configuration Overrides

**Status:** accepted

**Date:** 2026-03-28

**Decision Maker(s):** Daniel

## Context

Different YouTube creators publish at vastly different frequencies. Nate B Jones publishes ~2 videos/day (a 10d lookback catches ~20 videos). Ray Amjad publishes ~1/week (needs 120d to capture meaningful history). Some channels warrant auto-transcription of every video; others should be scan-only with manual transcript selection.

A single global config cannot serve both use cases without wasting API budget on prolific creators or missing content from infrequent ones.

## Decision

`config.yaml` supports per-channel overrides for three settings:

- `since` — lookback window (e.g., `10d`, `120d`)
- `prompt` — which mind map prompt to use (e.g., `mindmap-light`, `mindmap-heavy`)
- `auto_transcript` — `all` or `none`

Defaults are set at the top level. Per-channel values override defaults. Command-line `--since` overrides everything, enabling one-time backfills.

Override precedence: **CLI > per-channel > default**.

## Consequences

### Positive Consequences

- Each channel captures the user's relationship with that creator
- No one-size-fits-all scanning — API budget is spent proportionally
- Backfill is simple (`--since 180d` on CLI)
- Adding a channel is one YAML block

### Negative Consequences

- More config surface area for users to learn
- Users must understand the three-level override precedence
- No per-video override (only per-channel granularity)

## Alternatives Considered

- **Option:** Global settings only — one `since`, one `prompt` for all channels
- **Pros:** Simpler config, fewer knobs
- **Cons:** Forces lowest-common-denominator lookback (120d for all channels wastes API on prolific creators; 10d misses infrequent creators)
- **Status:** rejected

## Affects

- `config.yaml` (top-level defaults, per-channel overrides)
- `scripts/video_intel.py` (`cmd_scan()` channel loop, `since` resolution)

## Related Debt

None spawned.

## Research References

None.
