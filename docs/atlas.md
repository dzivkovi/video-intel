# The corpus catalog (`_atlas/atlas.json`)

The corpus catalog and the Creator Atlas wiki are different tools that happen to share a word. The **Creator Atlas wiki** (`scripts/wiki_atlas.py`, written to `_wiki/`) is a set of Obsidian pages about who leads on which ideas; see [intelligence-layer.md](intelligence-layer.md). The **corpus catalog** described here (`scripts/atlas_build.py`, written to `_atlas/`) is one machine-readable file that lists every video in the corpus, so a page or a script can show everything that is already there without the CLI.

## What it is and what it is not

- **One derived file.** `<output_dir>/_atlas/atlas.json` holds one row per `video_id`: ids, facets (channel, published date, duration, topics, concepts), a synopsis made of the mindmap's branch headings, up to three timestamped deep links, the transcript quality state, and "already seen" signals (how many briefings list the video, and which wiki pages cover its creator and concepts). Delete it any time; the next build recreates it.
- **Complete by design.** You are the only user, so every record the build finds becomes a row. Nothing is filtered by audience, access or source type, and non-YouTube sources (vendor-portal recordings, local-file ingests) appear too, with a `null` URL when no `https` link was stored.
- **Private.** A copy of `_atlas/` is a copy of everything in the corpus, so it has the same status as `config.yaml`: it stays on your machine and never goes into the repo (`.gitignore` excludes `_atlas/`).
- **Not a sharing format.** Choosing what a second person may see (an access policy, a filtered export) is a separate layer, tracked as [issue #262](https://github.com/dzivkovi/video-intel/issues/262). Do not hand the full file to anyone.
- **No transcript text.** The catalog carries derived material only (headings, concept labels, timestamps) and links out to the source. The build never opens a transcript file; it only notes whether one exists.
- **Read-only and free.** No Gemini call, no network, no write anywhere in the corpus except the one output file.
- **Has a page.** `scripts/atlas_render.py` turns the file into `catalog.html`; see "See it as a page" below.

## Build it

```bash
python scripts/atlas_build.py --corpus <output_dir>
```

Flags:

| Flag | Meaning |
|---|---|
| `--corpus` | The corpus folder (your `output_dir`). Required, must exist. |
| `--out` | Folder for `atlas.json`. Default: `<corpus>/_atlas`. |
| `--generated-at` | A value for the `generated_at` header, written verbatim. Default: derived from the corpus, never the clock (see "Determinism"). |

For example, to write a scratch copy somewhere else:

```bash
python scripts/atlas_build.py --corpus <output_dir> --out <scratch_dir> --generated-at 2026-10-28
```

## See it as a page

```bash
python scripts/atlas_render.py --atlas <output_dir>/_atlas/atlas.json
```

This writes `<output_dir>/_atlas/catalog.html`, one self-contained file that opens from disk by double-click (pass `--out` to put it elsewhere). Everything is inside the file, so a copy works on any machine; the only thing the page ever fetches is the YouTube thumbnail per video, built from each row's own watch URL, and the Thumbnails button turns that off. It is as private as the atlas it embeds.

What the page gives you:

- **Cards**: thumbnail, title, channel (click it to filter), date, length; the mind map's branch headings double as the deep links, with the second they jump to (the timestamps come from the mind map Gemini wrote while watching the video, not from a transcript, so a video without a transcript still has them); six concept chips (a chip narrows the set); an "Already seen" line when the video is in a briefing or has a wiki page.
- **Compact**: a dated table of contents, newest first, 200 rows a page.
- **Grid**: channel by month, last 24 months by default with a link to all months; darker means more videos; a cell lists exactly those videos.
- A filter rail (channel, topic, concept with a filter box, year, month, transcript quality, briefing and wiki membership) whose counts update against every other filter; search where every word must match somewhere in title, headings, concepts or channel, with title hits first; a URL hash that carries the whole view, so a bookmark reopens it; a dark theme by default and a light one behind the button.

Rebuild the page after every atlas rebuild. It refuses to overwrite a `catalog.html` it did not write itself (no `atlas_render.py` generator marker in the file), the same rule the atlas build applies to `atlas.json`.

The page follows the plain rules of good data display: one accent colour for data and grey for everything else, warning colours only on transcript problems, the finding stated as a sentence with its denominators under the title, and rows ordered by size so the eye reads top-down (the "storytelling with data" school).

## Share a subset

The private page is complete by design, so it never leaves the machine. To show the catalog to someone else, export a subset behind a policy:

```bash
python scripts/atlas_export.py --atlas <output_dir>/_atlas/atlas.json --policy <output_dir>/_atlas/export-policy.yaml --deny-terms <your-private-term-list>
```

The policy is a small YAML file with three keys, all default deny:

```yaml
channels:            # required: only these channel folders leave the machine
  - somechannel
  - otherchannel
since: 2025-10-28    # optional: only videos published on or after this date
exclude_video_ids:   # optional: single videos to leave out
  - dQw4w9WgXcQ
```

The export keeps a row only when its channel is listed, its date is on or after `since`, its URL is the canonical public YouTube watch URL of its own id (so other platforms never qualify), and the YouTube Data API reports the video as `public`. That last check is what keeps unlisted videos out: a canonical URL proves nothing about visibility, and publishing an unlisted link is a disclosure the creator did not make. It needs `YOUTUBE_API_KEY` in the environment (one quota unit per 50 videos); without the key the export refuses, and `--no-verify-public` is the explicit way to accept the risk. A local ingest filed under a real-looking id is caught by the same check, since the API does not return it. A policy channel that ends up with no exportable video is a refusal too, so a typo in the allowlist cannot export silently.

Each kept row is projected onto an explicit public field list: id, title, channel, date, length, URL, transcript quality, flag count and transcript source, headings, deep links and concept ids. Topics, briefing membership, wiki keys, alternate titles and the folders a duplicate lived in are not fields of the public row at all. The concept labels and channel counts are rebuilt from the kept rows, so nothing the subset does not reference rides along in a side table. An unknown policy key, a duplicate key, a non-string key, a malformed date or an empty channel list is a refusal, never a default; `exclude_video_ids: []` is fine.

`--deny-terms` is required and takes a file with one term per line (comments are skipped; lines under five characters are ignored and counted on stderr; an empty file is the explicit way to say you have none). The export aborts before any write when a term appears anywhere in the serialized subset, comparing after Unicode normalization and case folding, so an accented or differently cased spelling still matches; use your private term list. A public creator's title can trip it legitimately, as a video title naming a company did on the first real export; the remedy is `exclude_video_ids`, decided by you. Note that an excluded id in a committed policy is itself public: anyone can resolve it to the video.

The bundle lands in `<output_dir>/_atlas-export/` by default (`--out` to choose): `atlas.json` (the subset, `generator: atlas_export.py`, with an `export` header recording the policy fingerprint, the channel list and the date floor, never the private corpus size) and `catalog.html` rendered from it by the same renderer. Exporting into the private `_atlas/` folder is refused, an `atlas.json` or `catalog.html` another tool wrote in the output folder is refused and left untouched, and a failed render removes only what the run wrote. The subset and its page are byte-stable for the same atlas and policy.

The repo ships one such bundle as the demo: [examples/catalog/](../examples/catalog/) holds the exported `atlas.json`, the `catalog.html` built from it and the policy that produced them. `tests/test_catalog_demo.py` keeps the three in step: every row obeys the committed policy and the public field list, the page re-renders byte-identical from the JSON, and no private term appears in any committed byte.

## Read the summary line

A successful build prints exactly one line on stdout. A healthy run on the full corpus looks like this:

```text
atlas_build: wrote atlas.json to video-intel/_atlas (metas=3380 rows=3369 duplicates=2 skipped=0 no_id=9 excluded_metas=4 unthreaded=2390 (70.9%) warnings=0)
```

What each count means:

- `metas`: the `*.meta.json` files found under channel folders.
- `rows`: videos in the catalog, one per `video_id`.
- `duplicates`: extra records of an id already seen (the same video filed in two channel folders, or a rotated title). The record with more mindmap and concepts files wins; the losing titles become the row's `aliases`.
- `skipped`: metas that could not be used (unreadable, not UTF-8 JSON, not a JSON object, too large). Each one is named on stderr.
- `no_id`: metas that are JSON objects but carry no usable `video_id`.
- `excluded_metas`: metas under `_`- and `.`-prefixed folders (for example `_quarantine/`), which are never walked.
- `unthreaded`: rows that belong to no curated topic, with their share of all rows.
- `warnings`: how many distinct warning codes the build raised for missing or malformed optional inputs. The codes (`taxonomy_missing`, `taxonomy_malformed`, `topics_missing`, `topics_malformed`, `concepts_malformed`, `excluded_unlistable`) are in the file's `summary.warnings`; the paths are on stderr.

The file's `summary` also carries two listing counts that the line leaves out on a successful run: `unlistable_channels` (channel folders whose listing failed, always 0 in a written file) and `unlistable_excluded` (`_`- and `.`-prefixed folders whose listing failed, so their metas are missing from `excluded_metas`; this raises the `excluded_unlistable` warning).

**Check the partition:** `metas == rows + duplicates + skipped + no_id`. In the example, 3369 + 2 + 0 + 9 = 3380. If the sum does not match, the build has a bug; open an issue with the line.

**If a channel folder cannot be listed, nothing is written.** A folder the build cannot list (a permission error, or a Drive mount that drops a folder mid-run) has no metas in any count, so the partition would still balance while a whole channel is missing. The build therefore keeps the previous `atlas.json`, exits with code 2 and prints a counts-only line instead of the usual one:

```text
atlas_build: did not write atlas.json: 1 channel folder(s) could not be listed (metas=3290 rows=3279 duplicates=2 skipped=0 no_id=9 excluded_metas=4 unlistable=1)
```

The folder name is on stderr. Fix the access problem and run again.

**Paste only the stdout line into a PR or an issue.** It is counts only by design. The stderr log names files, channels and paths from your corpus, so keep it local.

## The row schema

The full contract is [`schemas/atlas.schema.json`](../schemas/atlas.schema.json). The file's top-level keys are, in order: `schema_version`, `generator`, `generator_version`, `generated_from` (the corpus folder name, never a path), `generated_at`, `summary`, `concepts` (concept id to label, only for ids some row references) and `rows` (sorted by `published`, then `video_id`).

Each row has these eighteen keys, in this order:

1. `video_id`
2. `title`
3. `channel`
4. `channels` (every folder the id was found in)
5. `published` (`YYYY-MM-DD`, or `""`)
6. `duration_seconds` (an integer, or `null`)
7. `url` (the stored `https` link, or `null`; never built from an id)
8. `transcript_quality` (`severe`, `truncated`, `partial`, `complete`, `skipped`, `unknown` or `none`)
9. `transcript_flag_count`
10. `transcript_source` (the stored `transcript_source` from meta.json, such as `gemini` or `youtube_captions`, or `null` when none is recorded)
11. `synopsis` (the mindmap's branch headings)
12. `deep_links` (up to three `{label, seconds, url}`, only for YouTube URLs with an eleven-character id)
13. `concept_ids`
14. `topics` (from `topics.json`)
15. `in_briefings`
16. `briefing_count`
17. `wiki` (`{"creator": <channel or null>, "concepts": [<concept ids>]}`: page keys read from the wiki pages' own front matter, so a renderer maps them to files)
18. `aliases` (titles of the losing duplicate records)

## Plain text for renderers

Titles, synopsis headings, deep-link labels and concept labels are untrusted text copied from the corpus. The build strips control characters and bidi overrides, normalizes to NFC and caps each value at 200 characters, but it keeps `<`, `&` and markdown link syntax literal. **A renderer must escape these fields** (HTML-escape for a web page, escape `|`, `[` and `]` for a Markdown table) and must never insert them as markup.

## Determinism

`generated_at` is never the wall clock. It is the `--generated-at` value when given, else the newest `processed` stamp found in the metas (converted to UTC), else the newest `published` date, else `null`. Rows, keys and text are written in a fixed order with LF line endings and one trailing newline, so rebuilding an unchanged corpus produces a byte-identical file. On a synced drive that means a rebuild with nothing new causes no sync traffic.

## If the build refuses to overwrite

The build only replaces an `atlas.json` whose `generator` header is exactly `atlas_build.py`. If the existing file was written by another tool, has no `generator` key, is not a JSON object, or cannot be read, the build stops before reading the corpus and prints `refusing to overwrite <path>: it is <reason>, not atlas_build.py`. Nothing is changed. Move that file aside (it may be something you want to keep), or pass `--out` to a folder no other tool writes to, then run again.

The write itself goes through a temporary `atlas.json.<pid>.tmp` in the same folder and one rename, so a crashed run leaves the previous catalog intact. A leftover temp of that exact shape is removed on a later run once it is more than an hour old; a younger one is left alone, and any other file in `_atlas/` is never touched. Do not run two builds of the same corpus at the same time: they write to the same `atlas.json`, and the last one to finish wins.

## On a Google Drive mount

A full build over the Drive-mounted corpus took 243 seconds and wrote a 6.1 MB file. That is normal: the build lists each channel folder once and reads each meta, concepts file and mindmap once, and the mount makes every read slow. Nothing in the build calls a paid API, so a slow run costs only time.
