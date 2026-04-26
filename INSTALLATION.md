# Installation Guide

> Two skills in one install: **video-intel** (English transcripts, mind maps,
> concept search) and **translate-bcs** (Bosnian/Croatian/Serbian subtitles).
> Both activate on natural language — no setup beyond installation.

## Prerequisites

### API Keys (both free)

| Key | Get it at | What it does |
|-----|-----------|-------------|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Gemini multimodal API (watches videos) |
| `YOUTUBE_API_KEY` | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) | YouTube Data API v3 (discovers new videos) |

**macOS / Linux** — add to `~/.zshrc` or `~/.bashrc`:

```bash
export GEMINI_API_KEY="your-key"
export YOUTUBE_API_KEY="your-key"
```

**Windows** — System Properties > Environment Variables > User variables > New:

| Variable name | Variable value |
|---------------|---------------|
| `GEMINI_API_KEY` | your-key |
| `YOUTUBE_API_KEY` | your-key |

Restart your terminal after setting them. The script also accepts `GOOGLE_API_KEY`
as a fallback for `GEMINI_API_KEY` (same key, different name).

### Python Dependencies

```bash
pip install google-genai google-api-python-client pyyaml youtube-transcript-api

# Optional: for hybrid vector search
pip install lancedb voyageai
```

## Install

### Claude Code (recommended)

Clone the repo and open Claude Code inside it:

```bash
git clone https://github.com/dzivkovi/video-intel.git
cd video-intel
claude
```

That's it. The repo ships with `.claude/settings.json` which auto-registers
the plugin as a local marketplace. On first launch, Claude Code shows a
trust prompt for the `video-intel` plugin — click **"Install for this project"**
to enable both skills for this repo. No manual config, no path editing.

After the one-time trust prompt, both `video-intel` and `translate-bcs` skills
appear automatically in every session opened in this directory.

**Tip:** You can also set API keys in `~/.claude/settings.local.json`
instead of system environment variables (this file is never committed):

```json
{
  "env": {
    "GEMINI_API_KEY": "your-key",
    "YOUTUBE_API_KEY": "your-key"
  }
}
```

### Claude Code user-level (access the search skill from any project)

After the project-scoped install above works, you can make the **read-only**
`video-intel-search` skill available globally so queries like "find videos
about MCP" or "nugget brief on prompt caching" work from any Claude Code
session, not only the plugin repo. Curate operations (scan, index, concepts,
dedupe, process) still require the plugin repo as CWD - that split is
intentional, see `skills/video-intel-search/SKILL.md` for the rationale.

> **Critical:** The marketplace key under `extraKnownMarketplaces` and the
> suffix after `@` in `enabledPlugins` MUST both be exactly `video-intel`
> (matching `.claude-plugin/plugin.json`'s `name` field). Do NOT add a
> suffix like `-local` or any variant. Claude Code's internal plugin
> registry normalizes the key to match `plugin.json`, so any suffix you add
> gets stripped silently. The result: `enabledPlugins` points at a
> marketplace name that does not exist, the plugin never installs, and no
> skills appear in other projects. **Symptom if you get this wrong: skills
> not appearing after Claude Code restart.** The fix below uses the correct
> names - copy it verbatim.

> **Critical:** The user-level marketplace path is read **fresh from the
> working tree on every session start** - there is no install-time cache.
> Whatever branch is checked out at that path is what every `claude`
> session sees globally, regardless of CWD. Three implications:
> (1) **Pre-merge testing is valid** - iterating on `SKILL.md` /
> `CLAUDE.md` / `specs/agent-rules.md`? Check out your branch in the
> marketplace path; any `claude` session reads the live files, no need
> to merge to test.
> (2) **Branch-switching has global side effects** - `git checkout main`
> in one terminal silently changes what every other open `claude`
> session sees on its next prompt.
> (3) **Use worktrees for parallel work** when any session is iterating
> on plugin internals - `git worktree add` to a separate path does NOT
> affect the marketplace-registered path's plugin state.
> See `docs/solutions/integration-issues/plugin-install-reads-from-working-tree-not-frozen-cache-20260425.md`
> for the original incident that surfaced this behavior.

**Step 1 - edit `~/.claude/settings.json`.** Add these two entries (merge
with any existing content; do not replace the whole file):

```json
{
  "extraKnownMarketplaces": {
    "video-intel": {
      "source": {
        "source": "directory",
        "path": "ABSOLUTE_PATH_TO_PLUGIN_CHECKOUT"
      }
    }
  },
  "enabledPlugins": {
    "video-intel@video-intel": true
  }
}
```

Replace `ABSOLUTE_PATH_TO_PLUGIN_CHECKOUT` with the absolute path where you
cloned this repo:

| OS | Example path |
|----|---------|
| Windows | `C:/Users/YOURNAME/ws/Skills/video-intel` (forward slashes work fine in JSON) |
| macOS | `/Users/YOURNAME/ws/Skills/video-intel` |
| Linux | `/home/YOURNAME/ws/Skills/video-intel` |

**Step 2 - point the skill at your corpus.** Pick one:

- Env var in your shell profile:
  ```bash
  export VIDEO_INTEL_OUTPUT_DIR="/absolute/path/to/your/corpus"
  ```

- Or create `~/.video-intel/config.yaml` (works across shell restarts):
  ```yaml
  output_dir: /absolute/path/to/your/corpus
  # vector_db_dir: /local/cache/lancedb   # optional; see docs/adr/ADR-0016
  ```

The plugin's own `config.yaml` at `SKILL_DIR/config.yaml` still wins when
present, so you do not need step 2 if you have a local `config.yaml` in
the plugin repo. Step 2 only matters for users who want a cache-resident
install or a shared corpus pointer across machines.

**Step 3 - verify.** Close and reopen Claude Code (plugin registration
happens at session start). From any non-plugin directory, ask Claude:

```
which skills do i have access to
```

You should see `video-intel`, `video-intel-search`, and `translate-bcs` in
the list. Then try:

```
find videos about MCP
```

The `video-intel-search` skill should fire, return concept matches, and
list relevant videos. If skills do not appear, re-read the critical note
above - the most common cause is a typo in the marketplace key.

### Other platforms

The two `SKILL.md` files follow the open [Agent Skills](https://agentskills.io/specification)
format. Platforms that support it can consume individual skill folders.
Copy the specific skill you want into that platform's skills directory:

| Platform | What to copy | Where to put it |
|----------|-------------|-----------------|
| **Gemini CLI** | `skills/video-intel/` | `~/.gemini/skills/video-intel/` |
| **Cursor** | `skills/video-intel/` | `~/.cursor/skills/video-intel/` |
| **GitHub Copilot** | `skills/video-intel/` | `~/.agents/skills/video-intel/` |
| **OpenAI Codex** | `skills/video-intel/` | `~/.codex/skills/video-intel/` |

For BCS translation, copy `skills/translate-bcs/` to the same location pattern.

**Important:** on non-Claude-Code platforms, each skill folder needs access to
the shared `scripts/` and `prompts/` directories at the repo root. Either copy
those directories into the skill folder, or install the full repo and adjust
paths. Cross-platform interoperability has not been verified by this project —
feedback welcome.

Gemini CLI also has a native installer:

```bash
gemini skills install https://github.com/dzivkovi/video-intel.git
```

## Verify

After installing, open Claude Code and try these two phrases:

```
scan my channels
```

If the video-intel skill activates, you'll see it preparing to call
`video_intel.py scan`. (First run will ask you to configure channels — see below.)

```
translate this YouTube video to Bosnian: https://www.youtube.com/watch?v=VIDEO_ID
```

If the translate-bcs skill activates, you'll see it preparing to call
`translate_video.py`. Both skills should be listed when you ask Claude
"what skills do you have?"

## Configure

Edit `config.yaml` at the repo root to add your YouTube channels:

```yaml
output_dir: ~/video-intel        # Where output files are saved
default_since: 10d               # How far back to look for new videos
default_prompt: mindmap-light    # Which prompt to use
model: gemini-3-flash-preview    # Gemini model

channels:
  - name: samwitteveenai
    url: https://youtube.com/@samwitteveenai
    auto_transcript: none        # "all" or "none"
    since: 10d
```

Or just ask Claude: **"add @samwitteveenai to my channels"** — it will
update the config for you.

The translate-bcs skill needs no configuration — just a YouTube URL.

## Updating

```bash
cd /path/to/video-intel
git pull
```

Your `config.yaml` is preserved — git pull won't overwrite local changes.

## Platform Compatibility

### Works (local execution, full network access)

| Platform | Notes |
|----------|-------|
| **Claude Code** (CLI, Desktop, VS Code) | Full plugin support. Both skills auto-discovered. |
| **Gemini CLI** | Single-skill install via `skills/video-intel/`. |
| **Cursor, Copilot, Codex** | Single-skill install. Not verified by this project. |

### Does not work (sandboxed cloud environments)

| Platform | Why |
|----------|-----|
| **claude.ai** / **Claude Desktop** (chat) | Sandbox blocks external API calls |
| **ChatGPT** | No skill execution environment |

These platforms are great for **triaging results after a scan** — upload
mind map files and ask "which videos are worth watching?" since that step
needs no API calls.

## Upgrading

### From v1.9.x to v1.10.x (April 2026)

Two structural changes you should know about:

1. **`config.yaml` is now gitignored.** If you have local edits in `config.yaml`,
   they will not be overwritten by `git pull`. New cloners copy
   `config.yaml.example` to `config.yaml` and edit — the committed template
   replaces the previously-tracked real config. No action needed if you
   already have a working `config.yaml` on disk.

2. **Skill split into three.** The single `video-intel` skill is now two:
   `video-intel` (ingest / curate — scan, transcribe, index, dedupe) and
   `video-intel-search` (read-only query — search, nugget, status).
   `translate-bcs` is unchanged. After `git pull`, close and reopen Claude
   Code to pick up the new skill registration; both skills auto-discover
   from `.claude/settings.json` as before.

3. **User-level install now supported.** The `video-intel-search` skill can
   be made available from any Claude Code project via `~/.claude/settings.json`.
   See the "Claude Code user-level" section above. **Read the marketplace-key
   callout before editing** — a common typo breaks the install silently.

### From v1.4.x

The repo changed from "single skill at the root" to "plugin with skills
under `skills/`." If you previously copied the repo to
`~/.claude/skills/video-intel/`, remove that directory. Then clone the repo
fresh and open Claude Code inside it — the plugin auto-registers via the
project settings that ship with the repo.
