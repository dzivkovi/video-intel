---
title: Claude Code plugin marketplace key silently normalized to plugin.json name field
date: 2026-04-24
category: integration-issues
module: plugin-install
problem_type: integration_issue
component: tooling
symptoms:
  - "Plugin never installs after adding to `~/.claude/settings.json` despite CLI invocation working from shell"
  - "Skills not appearing in any project after Claude Code restart"
  - "No error message or warning in Claude Code logs"
  - "`enabledPlugins` entry present in settings.json but no corresponding directory under `~/.claude/plugins/cache/`"
root_cause: config_error
resolution_type: config_change
severity: high
tags:
  - claude-code
  - plugins
  - marketplace
  - silent-failure
  - directory-source
related_components:
  - documentation
---

# Claude Code plugin marketplace key silently normalized to plugin.json name field

## Problem

When registering a Claude Code plugin via a user-level `~/.claude/settings.json` entry, the marketplace key under `extraKnownMarketplaces` and the suffix after `@` in `enabledPlugins` **must** match the `name` field in `.claude-plugin/plugin.json` **exactly, with no suffix**. Claude Code's internal plugin registry silently normalizes the key to match `plugin.json`, stripping any suffix the user typed. The `enabledPlugins` entry then references a marketplace name that does not exist under that name, and the plugin never installs. No error appears anywhere.

User-visible impact: a globally-installed plugin's skills do not appear in Claude Code sessions opened in other projects. The failure is completely silent — no warning, no log line, no visible clue.

## Symptoms

- Plugin's skills are absent from the `which skills do i have access to` listing in every session outside the plugin's source repo.
- CLI invocation of the plugin's scripts (e.g., `python /abs/path/to/plugin/scripts/video_intel.py status`) works fine from any CWD, so smoke-testing the CLI gives false confidence that the install succeeded.
- `~/.claude/plugins/known_marketplaces.json` shows the marketplace registered under the **normalized** name (no suffix), while `~/.claude/settings.json` has the **suffixed** name the user typed. The mismatch is only visible by comparing the two files.
- `~/.claude/plugins/installed_plugins.json` has no entry for the plugin — the install never completed.

## What Didn't Work

- **Symlinking `~/.claude/skills/<plugin>/` to the plugin's `skills/*/SKILL.md` directories.** This pattern bypasses the plugin registry entirely and creates a parallel install path. The user-level skill symlink would in principle make SKILL.md files visible, but it fragments the install, misses the plugin's manifest behavior, and leaves orphan entries in `settings.json`. It's a workaround for a different problem (standalone skill install), not this one.
- **Restarting Claude Code to pick up the settings.** Correct step but insufficient — even after restart, the plugin still does not install because the `enabledPlugins` name points at a nonexistent marketplace.
- **Re-running the install agent.** Repeating the bad write does not help; the key normalization happens every time.
- **Searching for the missing `-local` suffix in Claude Code source or docs.** The normalization behavior is not loudly documented; it's an implementation detail of the plugin registry.

## Solution

Use exactly the plugin's `name` field from `.claude-plugin/plugin.json` as the marketplace key. For the `video-intel` plugin whose manifest is:

```json
{
  "name": "video-intel",
  "version": "1.10.0",
  ...
}
```

The correct `~/.claude/settings.json` entry is:

```json
{
  "extraKnownMarketplaces": {
    "video-intel": {
      "source": {
        "source": "directory",
        "path": "C:/Users/danie/ws/Skills/video-intel"
      }
    }
  },
  "enabledPlugins": {
    "video-intel@video-intel": true
  }
}
```

Note both places: `extraKnownMarketplaces["video-intel"]` (not `"video-intel-local"`) and `enabledPlugins["video-intel@video-intel"]` (not `"video-intel@video-intel-local"`). The pattern is `<plugin-name>@<marketplace-name>` where marketplace-name = plugin-name for directory-source installs where the user does not need to disambiguate.

Close and reopen Claude Code after saving settings.json. The plugin registers on session start.

## Why This Works

Claude Code maintains an internal plugin registry at `~/.claude/plugins/known_marketplaces.json`. When the process reads `settings.json` on startup and encounters a new marketplace entry, it stores the marketplace under a key derived from the plugin manifest's `name` field, not the user-provided key. For directory-source marketplaces, the "plugin manifest" is `.claude-plugin/plugin.json` inside the referenced directory.

This means: the user's marketplace key acts as an *alias* during settings parsing but is not preserved. `enabledPlugins` then has to reference the **post-normalization** name. If the user types a suffix (`video-intel-local`), the normalization strips it, and `enabledPlugins["video-intel@video-intel-local"]` is orphaned — it references a marketplace under a name the registry never stored.

The fix is to collapse the alias: make the user's key match the post-normalization name from the start, so both sides of the `@` reference the same string.

## Prevention

**For plugin authors:**

- **Ship `.claude/settings.json` in your repo with the correct key baked in.** Users who clone the repo and open Claude Code inside it skip the user-level install path entirely and never touch their `~/.claude/settings.json`. The project-scoped settings file registers the plugin via the directory-source marketplace correctly.
- **Document the constraint loudly in your install guide.** A `> **Critical:**` callout that names the symptom ("skills not appearing after Claude Code restart") and shows the correct JSON verbatim. Users who hit the bug search for their symptom phrase.
- **Match plugin name and marketplace key in all documentation examples.** Never show `<plugin>-local` or any suffix pattern; it looks like a sensible disambiguation and tempts users into typing it.

**For plugin users:**

- If your plugin's skills do not appear in Claude Code sessions outside the source repo, check `~/.claude/plugins/known_marketplaces.json` and compare the registered key against `~/.claude/settings.json`'s `extraKnownMarketplaces` key. A mismatch indicates this bug.
- Run `python /abs/path/to/plugin/scripts/<entry>.py` directly from a non-plugin CWD to distinguish CLI-path issues (scripts not found) from plugin-registration issues (scripts found, skills absent). This plugin's install bug leaves the CLI perfectly functional, so "CLI works" is not evidence the plugin is registered.

**For this repo specifically:**

- `.claude/settings.json` ships with `video-intel` as the marketplace key (matching `plugin.json`), so cloning + opening Claude Code just works.
- `INSTALLATION.md`'s "Claude Code user-level" section has the `> **Critical:**` callout with verbatim-correct JSON.
- `CLAUDE.md`'s "User-level install" subsection has a matching callout for agent-facing readers.

## Related Issues

- Claude Code Skills documentation: https://code.claude.com/docs/en/skills — describes plugin manifest resolution but does not explicitly document the silent normalization behavior.
- `.claude-plugin/plugin.json` (this repo) — the canonical source for the `name` field that the marketplace key must match.
- PR #35 (this repo) — the install-doc fix that shipped with this learning. Commits `4fd52a9` (marketplace-key fix + install docs), `7f73085` (upgrade-section refresh).
