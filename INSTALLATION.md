# Installation Guide

> **This guide covers installing video-intel as a user.** For development setup
> (cloning the repo, editing prompts, contributing), see the
> [README](../README.md).

Video-intel follows the open [Agent Skills](https://agentskills.io/specification)
standard. One skill directory, multiple platforms.

## Prerequisites

### API Keys (both free)

| Key | Get it at | What it does |
|-----|-----------|-------------|
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Gemini multimodal API (watches videos) |
| `YOUTUBE_API_KEY` | [console.cloud.google.com/apis/credentials](https://console.cloud.google.com/apis/credentials) | YouTube Data API v3 (discovers new videos) |

Set them as permanent environment variables on your machine:

**macOS / Linux** (add to `~/.zshrc` or `~/.bashrc`):

```bash
export GEMINI_API_KEY="your-key"
export YOUTUBE_API_KEY="your-key"
```

**Windows** (System Properties > Environment Variables > User variables > New):

| Variable name | Variable value |
|---------------|---------------|
| `GEMINI_API_KEY` | your-key |
| `YOUTUBE_API_KEY` | your-key |

Restart your terminal after setting them. The script also accepts `GOOGLE_API_KEY`
as a fallback for `GEMINI_API_KEY` (same key, different name).

### Python Dependencies

```bash
pip install google-genai google-api-python-client pyyaml
```

## Install the Skill

### Option A: Copy to your platform's skill directory

Clone or download the repo, then copy the `video-intel` directory to the
location your AI coding agent expects:

```bash
git clone https://github.com/dzivkovi/video-intel.git
```

Then copy to your platform:

| Platform | Install command | Skill directory |
|----------|----------------|-----------------|
| **Claude Code** | `cp -r video-intel ~/.claude/skills/` | `~/.claude/skills/video-intel/` |
| **Gemini CLI** | `cp -r video-intel ~/.gemini/skills/` | `~/.gemini/skills/video-intel/` |
| **OpenAI Codex** | `cp -r video-intel ~/.codex/skills/` | `~/.codex/skills/video-intel/` (also reads `.agents/skills/` in repos) |
| **Cursor** | `cp -r video-intel ~/.cursor/skills/` | `~/.cursor/skills/video-intel/` |
| **GitHub Copilot** | `cp -r video-intel ~/.agents/skills/` | `~/.agents/skills/video-intel/` (configure via VS Code `chat.agentSkillsLocations`) |
| **Cross-platform** | `cp -r video-intel ~/.agents/skills/` | Works with Gemini CLI, Copilot, and others |

On Windows with Git Bash, `~` resolves to `C:\Users\<you>\`.

### Option B: Universal installer (NPX)

The [Vercel Labs skills CLI](https://github.com/vercel-labs/skills) can install
to multiple agents in one command:

```bash
# Install for Claude Code and Gemini CLI simultaneously
npx skills add dzivkovi/video-intel -a claude-code -a gemini-cli

# Install for all detected agents
npx skills add dzivkovi/video-intel

# Verify installation
npx skills list
```

### Option C: Gemini CLI built-in installer

Gemini CLI has native skill management:

```bash
# From your terminal
gemini skills install https://github.com/dzivkovi/video-intel.git

# Or from inside a Gemini CLI session
/skills install https://github.com/dzivkovi/video-intel.git
```

### Option D: Databricks

Databricks has adopted the Agent Skills standard for its coding agents. Their
own skills are installed via `databricks experimental aitools skills install`,
which pulls from the
[databricks/databricks-agent-skills](https://github.com/databricks/databricks-agent-skills)
catalog. Video-intel's SKILL.md follows the same spec, so it can be placed
in the Databricks skills directory manually.

## Multi-Platform Tip: Use Symlinks

Different tools look in different directories. Rather than copying the skill
multiple times, use symlinks from a single source of truth:

```bash
# Pick one canonical location
cp -r video-intel ~/.agents/skills/

# Symlink for other platforms
ln -s ~/.agents/skills/video-intel ~/.claude/skills/video-intel
ln -s ~/.agents/skills/video-intel ~/.gemini/skills/video-intel
ln -s ~/.agents/skills/video-intel ~/.codex/skills/video-intel
```

On Windows (PowerShell as Administrator):

```powershell
New-Item -ItemType SymbolicLink -Path "$HOME\.claude\skills\video-intel" -Target "$HOME\.agents\skills\video-intel"
New-Item -ItemType SymbolicLink -Path "$HOME\.gemini\skills\video-intel" -Target "$HOME\.agents\skills\video-intel"
```

## Verify It Works

After installation, open your AI coding agent from any project directory:

```
# Claude Code
claude
> scan my channels

# Gemini CLI
gemini
> scan my YouTube channels for new videos
```

The skill activates on natural language (e.g., "what's new from Sam Witteveen",
"transcribe this video", any YouTube URL followed by a question). No special
syntax needed.

## Configure Your Channels

After installation, edit the `config.yaml` inside the installed skill directory:

```yaml
output_dir: ~/video-intel        # Where output files are saved
default_since: 10d               # Default lookback window
default_prompt: mindmap-light    # Which prompt to use

channels:
  - name: samwitteveenai
    url: https://youtube.com/@samwitteveenai
    prompt: mindmap-light
    auto_transcript: none        # "all" or "none"
    since: 10d                   # How far back to look
```

Or just ask your AI agent: "add @samwitteveenai to my channels" and it will
update the config for you.

## Environment Variables for Claude Code

Claude Code supports setting environment variables in its settings files. This
is useful if you prefer not to set system-wide variables. Create
`~/.claude/settings.local.json` (this file is never committed to git):

```json
{
  "env": {
    "GEMINI_API_KEY": "your-key",
    "YOUTUBE_API_KEY": "your-key"
  }
}
```

This file follows the official Claude Code settings schema. You can add
`"$schema": "https://json.schemastore.org/claude-code-settings.json"` for
IDE autocompletion.

## Platform Compatibility Notes

### Runs on (local execution, full network access)

Skills that call external APIs (YouTube, Gemini) require local execution.
These platforms run skills on your machine:

| Platform | How it works |
|----------|-------------|
| **Claude Code** (CLI) | Runs locally. Reads `~/.claude/skills/`. Full network access. |
| **Gemini CLI** | Runs locally. Reads `~/.gemini/skills/`. Full network access. |
| **OpenAI Codex** (CLI) | Runs locally. Reads `~/.codex/skills/`. Full network access. |
| **Cursor** | Runs locally. Reads `~/.cursor/skills/`. Full network access. |
| **GitHub Copilot** (VS Code) | Runs locally. Reads workspace or user skills. Full network access. |
| **Databricks** | Runs in Databricks workspace. Agent skills installed via CLI. |

### Does not run on (sandboxed cloud environments)

Cloud-hosted chat interfaces run code in isolated sandboxes that block
external API calls. Video-intel cannot run in these environments:

| Platform | Why it doesn't work |
|----------|-------------------|
| **claude.ai** / **Claude Desktop** (chat) | Code execution sandbox blocks `googleapis.com` |
| **OpenAI Codex Cloud** | Sandbox has restricted network; requires Secrets config for API keys |
| **ChatGPT** | No skill execution environment |

These platforms are excellent for triaging results after a scan. Upload the
mind map files and ask "which of these videos are worth watching for topic X?"
since that step requires no external API calls.

## Scope: Personal vs. Project

| Scope | Where it lives | Who sees it |
|-------|---------------|-------------|
| **Personal** (recommended) | `~/.claude/skills/video-intel/` | You, across all projects |
| **Project** | `.claude/skills/video-intel/` | Anyone working on that project |

For video-intel, personal scope makes most sense since your channel list and
viewing preferences are personal, not project-specific.

## Updating

To update to the latest version:

```bash
cd ~/.agents/skills/video-intel   # or wherever you installed
git pull                          # if cloned
```

Or re-run the NPX installer, which checks for updates:

```bash
npx skills add dzivkovi/video-intel
```

Your `config.yaml` (channel list) is preserved because git pull won't overwrite
local changes to tracked files. If you used the copy method, back up your
`config.yaml` before overwriting.
