---
title: Non-scannable video sources unified under per-channel `enabled: false` flag
date: 2026-04-24
category: integration-issues
module: cmd_scan
problem_type: integration_issue
component: tooling
symptoms:
  - "Skool community channels in config.yaml burn 2 YouTube API quota units per scan trying to resolve a non-YouTube URL"
  - "WARNING log noise per scan for every non-YouTube channel ('Channel not found: https://www.skool.com/...')"
  - "Members-only YouTube videos return exit 0 from `mindmap --url`/`transcript --url` even though Gemini logged 403 PERMISSION_DENIED — silent failure"
  - "Vimeo / Spotify / arbitrary platform URLs cannot be processed via `--url` because the URL parser hardcodes the YouTube 11-char video-ID regex"
  - "Users want occasional one-off processing of a creator without scanning their full feed (e.g., Lenny's Podcast)"
root_cause: design_gap
resolution_type: feature_addition
severity: medium
tags:
  - configuration
  - cmd_scan
  - skool
  - vimeo
  - members-only
  - one-off-creators
  - opt-in-flag
related_components:
  - documentation
  - tests
---

# Non-scannable video sources unified under per-channel `enabled: false` flag

## Problem

The `scan` subcommand assumed every entry under `channels:` in `config.yaml` resolved to a YouTube channel. Real-world corpora have several source types that this assumption broke:

1. **Skool communities** (`https://www.skool.com/...`) — paywalled, no public RSS/API. Currently configured with a `url:` field that YouTube cannot resolve.
2. **Members-only YouTube videos** — return `403 PERMISSION_DENIED` from Gemini's URL fetch (separate solved problem; recovery is download-and-`process --file`).
3. **Vimeo / Spotify / Twitch** — the `--url` regex `(?:v=|/)([a-zA-Z0-9_-]{11})` only matches YouTube's 11-character video IDs. Other platforms need download-and-`process --file`.
4. **One-off creators** — the user wants to occasionally process a single video from a creator (e.g., Lenny's Podcast, Prism Labs) without pulling their entire feed into every regular scan.

Pre-flag behavior for source type 1: `cmd_scan` called `get_channel_id()` against the Skool URL, burned two YouTube API quota units (one `forHandle` lookup, one fallback `id` lookup), got `(None, None)` back, emitted a `WARNING [name] Channel not found: <url>` log line, and continued to the next channel. The comment in `config.yaml` said "Do not run `scan` for this channel" but had no enforcement; every scan paid the cost.

Pre-flag behavior for source type 4: no clean answer. The user could either (a) skip adding the creator to config and lose the ability to route `mindmap --url --channel <name>` artifacts to a named folder + lose concepts extraction, or (b) add the creator and accept the full feed being scanned. Both options were lossy.

## Symptoms

- `WARNING [earlyaidopters] Channel not found: https://www.skool.com/earlyaidopters` on every scan, accompanied by 2 wasted YouTube API quota units.
- No clean way to express "keep this creator in config for manual one-offs only" — the closest workaround was `since: 0d` (still costs API calls and emits scan log noise) or omitting from config entirely (loses `--channel` routing and concepts coverage).
- New non-YouTube sources (Vimeo, Spotify, etc.) had no documented config pattern.

## What Didn't Work

- **`since: 0d` workaround.** Setting the lookback to "now" made `fetch_channel_videos()` return zero results, which technically prevents new mindmaps. But `cmd_scan` still resolved the channel ID and logged the per-channel header — wasted YouTube API quota and visual noise. The flag is also a side-effect of the date-window logic, not a first-class "don't scan this" signal, so future scan refactors could break the workaround silently.
- **Auto-slugified fallback (`mindmap --url URL` without `--channel`).** When YouTube auto-detection finds a channel ID not in `config.yaml`, the script slugifies the YouTube channel title into a folder name (e.g., "Lenny's Podcast" → `lennyspodcast`) and drops artifacts there. `index --force` picks up these folders for search. But `concepts` iterates configured channels only, so auto-slugified folders never get a `concepts.json` and miss concept-based search hits. Acceptable for very-occasional one-offs, lossy as a general pattern.
- **Comment-only convention.** The pre-flag `earlyaidopters` entry had a comment "Do not run `scan` for this channel" with no enforcement. Comments rot. New scan code paths can ignore them.

## Solution

A per-channel `enabled` boolean flag, default `true` when the key is omitted (opt-in, no breaking change). Setting `enabled: false`:

```yaml
channels:
  - name: earlyaidopters
    url: https://www.skool.com/earlyaidopters
    enabled: false                  # Skool: no YouTube API metadata
    auto_transcript: all

  - name: lennyspodcast
    url: https://youtube.com/@lennyspodcast
    enabled: false                  # one-off creator: too broad to scan in full
    auto_transcript: all
```

Effects:

- `scan` skips the channel entirely. No YouTube API call. No WARNING. One `INFO [name] Skipping (enabled: false). Use mindmap/transcript --url --channel <name> for one-offs.` log line — informational, not noise.
- `scan --channel <disabled-name>` also skips. Strict semantics (not advisory) — the flag's whole point is durable manual-only routing.
- `mindmap --url URL --channel <name>` works (URL paths route to the named folder regardless of the flag).
- `transcript --url URL --channel <name>` works.
- `mindmap --file PATH --channel <name>` / `transcript --file PATH --channel <name>` / `process --file PATH --channel <name>` work — these are the canonical recovery paths for Skool, Vimeo, members-only YouTube, local recordings.
- `concepts` iterates configured channels and includes `enabled: false` ones, so concept extraction and taxonomy still cover one-off videos. The flag scopes to scan only.
- `index` walks the filesystem (independent of config), so search includes one-off folders unchanged.

The implementation is four lines in `cmd_scan` (after the existing `--channel` filter): a list comprehension that splits the channels list into enabled and disabled, an `INFO` log per skipped channel, and continuation with the enabled list. See `scripts/video_intel.py` around the `# Skip channels with enabled:false` comment.

### Naming note

GitHub issue #36 originally proposed `scan: false` as the flag name. Both names work; `enabled: false` was chosen for shipping because it reads more naturally to a human scanning `config.yaml` — the channel block is "not enabled for the regular pipeline", which matches the user's mental model. `scan: false` is technically more precise (the flag scopes to scan, while concepts/index/`--url`/`--file` paths still work), but the precision premium did not justify deviating from the more idiomatic YAML reading. The PR thread captures the rename decision.

## Why This Works

The flag draws a single uniform boundary that covers four distinct failure modes (Skool, Vimeo-class, members-only, one-off) by addressing the common shape: **the source can't be batch-fetched**. Whether the obstruction is a paywall (Skool), a missing parser (Vimeo), an auth wall (members-only YouTube), or a curation choice (one-off), the answer is the same — manual ingestion via `--file` or `--url --channel <name>`, no batch scan.

A single boolean gate is preferable to per-platform special-casing because:

- **Closed set vs. open set.** Per-platform handling assumes we know the full list of unsupported platforms. We don't — a new platform appears every few months. The boolean treats "scannable" as the small closed set (currently: YouTube) and everything else as "manual ingestion".
- **Configuration locality.** All four use cases are expressed in the same place (`config.yaml`), with the same key (`enabled: false`), discoverable by reading any one example. A new contributor encountering Skool can copy the pattern from the Lenny's Podcast entry without learning a new mechanism.
- **Reversibility.** Removing the flag re-enables full scanning. No data loss, no migration. The user can A/B-test "is this creator worth scanning in full?" by toggling.

The `enabled: true` default (when key absent) keeps the change non-breaking. Existing config entries scan exactly as before.

## Prevention

**For new non-YouTube sources:** Add the channel with `enabled: false` from the start. Use `--file`-based commands for ingestion. Document the platform in the channel's comment block (1-2 lines).

**For one-off creators:** Add with `enabled: false`. Run `mindmap --url URL --channel <name>` and `transcript --url URL --channel <name>` for each video. Run `concepts` periodically (it will pick up the new channels automatically since they're configured). Search and taxonomy work without further intervention.

**For agents working on this codebase:** When a user asks "process this video from a creator I don't follow regularly", the answer is **always** `enabled: false` + `mindmap --url --channel <name>` + `transcript --url --channel <name>`. Never lean on the auto-slugified-folder fallback as a workaround — concepts coverage gap is a known cost of that path. Never invent a `since: 0d`-style gate as an alternative — it costs API quota and is a side-effect, not a contract.

**Documentation footprint** (so the pattern remains discoverable):

- `CLAUDE.md` Architecture section: bullet describing the flag and pointing here.
- `skills/video-intel/SKILL.md` Configuration section: "One-off creators (`enabled: false`)" subsection with example.
- `config.yaml.example`: template entry with comment explaining the flag.
- `tests/test_channel_enabled_flag.py`: contract tests covering default-true, explicit-false, and explicit-`--channel`-against-disabled.
- This solution doc: the durable why.

## Related Issues

- **GitHub issue #36** ("feat(scan): add per-channel scan:false flag to silence non-YouTube source warnings") — the original spec for this work. Shipped flag name differs (see Naming note above); other acceptance criteria match.
- **Members-only YouTube recovery:** see `skills/video-intel/SKILL.md` "When a YouTube URL returns 403" subsection. The recovery path (download → `process --file`) is the same workflow `enabled: false` Skool entries use; the flag unifies them.
- **`docs/adr/ADR-0017`** (KB-layer strategy): Stage-2 LightRAG and Stage-3 LLM Wiki both assume a clean, deduplicated corpus. `enabled: false` on Skool / one-off creators keeps their content discoverable through `mindmap --file` / `process --file` ingestion without polluting the scan-driven corpus rhythm.
- **Test contract:** `tests/test_channel_enabled_flag.py` — three cases (default-true, explicit-false skips, explicit-`--channel` against disabled also skips). Any future scan refactor must keep these green.
