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

## Upgrading from v1.4.x

The repo changed from "single skill at the root" to "plugin with two skills
under `skills/`." If you previously copied the repo to `~/.claude/skills/video-intel/`,
remove that directory. Then clone the repo fresh and open Claude Code inside it —
the plugin auto-registers via the project settings that ship with the repo.
