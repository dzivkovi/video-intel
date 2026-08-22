# The wiki layer: knowledge compounding

## What this is

Retrieval alone answers "find me the passage." It never gets cheaper or more complete the second time you ask a related question - the same synthesis gets re-paid every time. The wiki layer is the compounding half: two mechanisms that file what retrieval produces back into the corpus, so knowledge accumulates instead of only being fetched. Both are inspired by the LLM-wiki pattern named in [ADR-0017](adr/ADR-0017-kb-layer-strategy.md): compile knowledge beside retrieval, not instead of it.

1. **Nugget briefs persist.** Every cross-creator synthesis you ask for via `nugget` is written back into the corpus, so the same question never has to be re-paid to Gemini.
2. **Concept pages.** A standalone generator renders browsable Obsidian pages for the taxonomy concepts that are stable across three or more channels - a human-readable atlas sitting on top of the machine-only `taxonomy.json`.

Both are **derived, additive, rebuildable artifacts**. Nothing downstream of them (search, briefings, taxonomy) reads them back in, and nothing else writes new corpus state through them. Deleting `_briefings/nuggets/` or `_wiki/concept-pages/` loses nothing that cannot be regenerated - nugget briefs by re-running the same query, concept pages by re-running the generator against the same taxonomy and concepts.json files. Treat both as caches with a nicer read surface, not as a second source of truth.

## Nugget persistence

`nugget "query"` already retrieves top-K evidence via hybrid search and synthesizes a consultant-grade cross-creator brief. As of this feature, it also writes that brief to disk:

```bash
python scripts/video_intel.py nugget "forward deployed engineering"
```

writes `_briefings/nuggets/2026-08-22-forward-deployed-engineering.md` in addition to printing the brief to stdout, exactly as before. Running the same query again the same day never overwrites the earlier file - it gets a `-2` suffix, then `-3`, and so on, mirroring the same convention `briefings --unseen` uses for same-day reruns.

To skip persistence entirely and keep the old stdout-only behavior:

```bash
python scripts/video_intel.py nugget "forward deployed engineering" --no-save
```

`--no-save` is fully write-free: no brief file, no config snapshot, nothing touches disk.

### The one subtlety you need to understand

The persisted brief's front matter uses **`cited_video_ids`**, deliberately not `video_ids`. Catch-up briefings (`briefings --unseen`) decide what to skip by unioning every `video_ids` list across `_briefings/**/*.md` - that is the "seen" set. A nugget brief cites videos as supporting evidence for a synthesis; it does not curate them the way a briefing does. If a cited video were folded into the seen set, asking a nugget question about a topic would have the side effect of making that video permanently ineligible for a future catch-up briefing, even though nobody ever decided you'd watched it or reviewed it. Citation is weaker than curation, so the field name is different on purpose, and `load_seen_video_ids` never reads `cited_video_ids` at all.

### Why `nugget` is exempt from config snapshots

Most corpus-mutating commands (`scan`, `process`, `mindmap`, `transcript`, and others) trigger an automatic config snapshot before they run, so `config.latest.yaml` always reflects the channel list that produced the corpus. `nugget` is deliberately **not** in that set, even though it now writes a file. It writes only its own brief under `_briefings/nuggets/` - never channel config, never scan state - so there is nothing for a snapshot to protect. Snapshotting would actually be harmful here: `nugget` is reachable from the globally installed search skill, where the resolved config is often the channel-less user-level `~/.video-intel/config.yaml`. Snapshotting that would overwrite `config.latest.yaml` with a config that has no channels at all, and would churn a dated snapshot on every nugget query run from outside the plugin repo.

### Persistence never costs you the answer

If the corpus write fails - an unmounted cloud drive, a permissions problem, a disk-full condition - the brief still prints to stdout (and to `--output` if you passed one). A logged warning tells you persistence failed; it never blocks or discards the synthesis you already paid a Gemini call for.

## Concept pages

`scripts/wiki_concepts.py` is a standalone script, not a `video_intel.py` subcommand. It reads `taxonomy.json` plus every video's `concepts.json`, `meta.json`, and `mindmap.md`, and renders one Markdown page per stable concept.

```bash
python scripts/wiki_concepts.py --corpus <path-to-output_dir> [--out <path>] [--top N]
```

- `--corpus` (required) - path to your corpus root (the `output_dir` from `config.yaml`), read-only.
- `--out` (optional) - where to write pages. Default: `<corpus>/_wiki/concept-pages/`.
- `--top` (optional) - how many stable concepts to render, must be `>= 1`. Default: 20.

### What a page contains

Each concept page has:

- The canonical label and any known aliases, pulled from `taxonomy.json`.
- Member videos, grouped by channel, each with a timestamped `&t=` deep link back to the moment in the mindmap where the concept was mentioned.
- `> [!todo] PROSE` slots - mechanical skeletons, not written prose. An agent (or you) fills these in later, as a separately gated step; the generator itself makes no LLM calls.
- A "Related concepts" section cross-linking other pages that share videos with this one, scoped to concepts that also made the top-N cut.

A `concept-pages.md` Map of Content page is written alongside the concept pages, listing every selected concept with its video/channel counts.

### The selection rule

A concept only earns a page when it appears across **three or more distinct channels**. This is a stability bar, not a popularity bar: a concept one creator mentions constantly but nobody else covers never claims a page, no matter how many of that creator's own videos mention it. If fewer than `--top N` concepts clear the bar, you get fewer pages - the bar is never lowered to hit a target count. On a small or narrow corpus, expect single digits even when you asked for 20.

### Safety rules worth knowing

- **The generator only ever overwrites pages it generated itself.** Every page it writes carries a `generator: wiki_concepts.py` stamp in its front matter. Before overwriting an existing file at the same path, it checks that stamp; if the file has no frontmatter, empty or malformed frontmatter, or a different generator's stamp, the whole run aborts with an error and nothing is written. A hand-authored note or another tool's file with a colliding name is never silently clobbered.
- **It prunes only its own stale pages.** After a run, any file under `--out` that this same generator wrote in a prior run but that is absent from the current page set gets deleted. Foreign files, and files with no readable generator stamp, are never touched.
- **It never touches `wiki_atlas.py`'s pages.** `wiki_atlas.py` (the lead-lag creator atlas, a separate generator) writes to `_wiki/concepts/` by default; `wiki_concepts.py` writes to `_wiki/concept-pages/` by default. The two namespaces are deliberately distinct so a default run of either tool can never overwrite the other's output.
- **Reruns are byte-deterministic for identical inputs**, but a rerun replaces entire pages, including any prose you filled into the `PROSE` blocks. Treat these pages as generated views, not editing surfaces - if you want prose to survive, keep it somewhere else and re-paste it, or don't regenerate that page.

## First run on a real corpus

A few things worth knowing before your first run against a real, populated corpus:

- **Run `taxonomy-build` first.** The generator reads `taxonomy.json` for canonical labels and aliases; if it is stale or absent, pages fall back to whatever label each concept's own `concepts.json` carries, which is usually less polished.
- **The first run walks the whole corpus on whatever filesystem it lives on.** If your `output_dir` is a cloud-synced mount (Google Drive, OneDrive, Dropbox), expect the walk itself to be slower than on local disk - this is a read-heavy pass over every channel folder.
- **Unreadable or corrupt per-video files are skipped, never a crash.** A `concepts.json` or `meta.json` that fails to read or parse is logged as a WARNING and counted; the final summary line reports how many files were skipped. This is the same best-effort discipline the rest of the corpus tooling uses for damaged files.
- **Timestamps come from mindmap files, or fall back to a plain video URL.** The generator matches a concept's mention against the video's own parsed `mindmap.md` headings and bullets. When nothing matches - including deliberately, for very short labels that would otherwise substring-match the wrong bullet - it links to the plain video URL rather than guessing. A wrong timestamp is worse than none.
- **To browse pages in Obsidian**, open (or register) the corpus's `_wiki/` folder as a vault, or as part of one. `scripts/register_obsidian_vault.py` (documented alongside the intelligence layer) can register it for you if Obsidian is installed and closed.

## Troubleshooting

| Symptom | What it means |
|---|---|
| Run refuses with "not generated by wiki_concepts" | A foreign or hand-authored file at that path has a colliding name. Move it aside, or point `--out` at a different directory. |
| Fewer pages than `--top` | Expected: the three-channel stability bar was not met by enough concepts. This is by design, not a bug. |
| Nugget brief file is missing but the brief printed to stdout | Persistence failed and logged a WARNING (permissions, unmounted cloud mount, disk full). Re-run, or check that the corpus mount is reachable. |
| A `--no-save` nugget run still logs a config-backup line | Should not happen - `nugget` is exempt from config snapshots regardless of `--no-save`. If you see this, it is a bug worth reporting. |

## Relation to the broader design

This is the wiki layer's minimal, evidence-gated first slice, per [ADR-0017](adr/ADR-0017-kb-layer-strategy.md) and [ADR-0018](adr/ADR-0018-nugget-cli-cross-creator-synthesis.md): pages and briefs compile beside hybrid search, they do not replace it, and no graph databases or LLM page-writing are involved anywhere in this slice.
