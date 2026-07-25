---
name: release
description: Cut a release of the video-intel plugin. Use when tagging a version, bumping .claude-plugin/plugin.json, pushing tags, publishing a GitHub Release, or answering how this plugin is distributed and installed by end users.
---

# Releasing the video-intel plugin

Moved verbatim out of the root `CLAUDE.md` so it loads when you are actually cutting a release.

## Packaging and Distribution

**Plugins are distributed as git repositories, not as packaged archive files.** The current Claude Code plugin docs describe two real consumption paths, and neither involves uploading a `.zip` or `.skill` artifact to a GitHub release:

1. **Self-registering local install** (primary): the repo ships with `.claude/settings.json` containing `extraKnownMarketplaces` pointing at itself and `enabledPlugins` pre-activating the plugin. When a user clones the repo and opens Claude Code inside it, the plugin is auto-discovered. Claude shows a one-time trust prompt; the user clicks "Install for this project" and both skills become available. No manual path editing.
2. **Marketplace install** (future, for broader distribution): the plugin author publishes the repo, then a Claude Code marketplace references it. End users install via `/plugin install video-intel@<marketplace-name>` from inside Claude Code.

What matters for shipping a release of this repo:

- Tag the commit on `main` with `vX.Y.Z`. Push the tag.
- Make sure `.claude-plugin/plugin.json` `version` matches the tag.
- The `.claude/settings.json` that ships with the repo handles local auto-discovery for anyone who clones.

The `output_dir` in `config.yaml` should point outside the plugin folder (e.g. `~/video-intel`) for production use, so user data does not live inside the cached plugin directory that Claude Code manages.

## Release Process

1. Bump `.claude-plugin/plugin.json` `version` to match the upcoming tag.
2. Commit the bump.
3. Tag the commit:

   ```bash
   git tag -a vX.Y.Z -m "short description"
   ```

4. Push commits and tag: `git push origin main --tags`
5. (Optional) Create a GitHub Release tied to the tag with release notes — useful for humans browsing changes, even though Claude Code itself does not consume a release asset under the documented install paths.

**Users installing from a previous release:** The repo went from "single skill at repo root" to "plugin with two skills under `skills/`." Anyone who previously installed by copying the old layout to `~/.claude/skills/video-intel/` should remove that directory and re-install via one of the documented plugin paths. Claude Code manages the actual plugin cache itself (`~/.claude/plugins/cache/...`); end users do not copy files there directly.
