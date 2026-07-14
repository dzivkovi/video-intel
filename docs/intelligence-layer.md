# The intelligence layer: uncovering nuggets in your corpus

This is the one page to read if you want to go past "search my videos" and start asking the corpus harder questions: who got to an idea first, what is suddenly heating up, which creators actually cluster together, and how the ideas connect. It is optional; the core scan/transcript/search pipeline never touches any of it.

Everything here is experimental and deliberately small. Read it once, run the handful of commands, and you will understand what you are looking at.

## The one idea to hold

Your corpus is a pile of transcripts. This layer turns that pile into a **receipts book**: a small database that knows *who said what, when, and where the exact quote is*. That is the whole mental model.

Once you have that book, every analysis below is just **one honest question asked of it**. You do not need graph theory or statistics to use them. You need to know which question each one answers, and the handful of ways each one could fool you (that part matters, and it has its own section).

The book lives in a single DuckDB file (`~/.cache/video-intel/intel.duckdb`). DuckDB is "SQLite for analytics": no server, no Docker, nothing running in the background. The file is derived from your corpus, so you can delete it and rebuild it any time.

> A note on history: an earlier version of this layer used a Neo4j graph database and community-detection algorithms. We retired that (issue #95): on a corpus this size the graph mostly rediscovered "which topics are popular," which is not insight. Plain DuckDB plus a few honest questions turned out to be the smaller, truer tool. The Neo4j path is kept only as archived research (linked at the end).

## Step 0: build the receipts book (once)

```bash
pip install -e ".[intelligence]"          # the Python side (DuckDB)
python scripts/intel_graph.py load        # reads your corpus, writes the store
```

That is the whole setup. Rerun `load` whenever your corpus grows.

## Question 1: who got to an idea first?

```bash
python scripts/lead_lag_report.py                       # prints the report
python scripts/lead_lag_viz.py --out lead-lag.html      # same thing, as a web page
```

This answers "when a concept shows up across creators, who covered it early and who followed." The trick that makes it honest: it corrects for the fact that channels joined your corpus at different times and post at different rates, so a big, deeply-indexed channel cannot fake being "first." A creator only scores as a leader when they beat the chance their own volume already buys them. The web version adds a plain-English panel and marks which findings are solid versus lucky.

(Under the hood: a minimal coverage-corrected precursor model. You never need that phrase to read the output.)

## Question 2: what is suddenly heating up?

```bash
python scripts/burst_report.py
```

This finds concepts that just "caught fire" - a sharp jump in how often they are mentioned, measured against that concept's *own* usual rate, not against how popular it is overall. Each burst gets a start date, an intensity, and whether it is still rising or has cooled, with a link to the first video in the burst.

The report opens with a corpus-volume table on purpose: read it first. If your whole corpus grew that month (a big scan), many concepts will look like they are "bursting" at once, when really the whole book got thicker.

(Under the hood: Kleinberg burst detection. Again, the label is optional.)

## Question 3: which creators actually cluster together?

```bash
python scripts/sdsm_network.py
```

This finds pairs of creators who share *far more* concepts than you would expect - not because they are both prolific, and not because they both cover whatever is popular, but because they genuinely track the same ideas. The hard part is the "expected": in a corpus where everyone talks about AI coding, almost every pair overlaps a lot, so a naive count calls everyone connected. This tool compares each pair against a null model that already knows how prolific each creator is *and* how popular each concept is, and keeps only the pairs that beat it. On the current corpus that prunes ~300 noisy pairs down to about ten real ties.

Read it as a shortlist of "these two are worth comparing," not as influence: the edges say *overlap beyond chance*, not *who led whom* (that is Question 1). And on a corpus this small, ten ties is a lead for inspection, not a map of the field.

(Under the hood: the Stochastic Degree Sequence Model / bipartite configuration null. You never need that phrase to read the output.)

## Question 4: let me just wander the connections

```bash
python scripts/wiki_atlas.py --wiki-dir <output_dir>/_wiki
```

This writes a small wiki - a dossier per leading creator, an adoption story per top concept, all cross-linked - into a `_wiki` folder, meant to be *browsed*, not read start to finish. It is a reading lens over what Questions 1 and 2 already found, not a new discovery step. The links between pages exist only where the data actually shows a lead-follow relationship, so it stays sparse and honest instead of a tangled hairball.

Open it in [Obsidian](https://obsidian.md) (free), which turns the `[[links]]` into a clickable web with a graph view. A vault is just a folder Obsidian tracks; the catch is that Obsidian's in-app "Change vault" box only *filters folders it already knows* and cannot add one. Two ways to add yours:

- **GUI:** launch Obsidian, click the bottom-left "Open another vault" icon, then "Open folder as vault," and pick your `_wiki` folder (you can paste the path into the file dialog's address bar).
- **Script** (when the GUI won't cooperate): quit Obsidian fully first, then

  ```bash
  python scripts/register_obsidian_vault.py "<output_dir>/_wiki" --open --launch
  ```

  It edits Obsidian's own vault list, refuses to run while Obsidian is open (Obsidian would overwrite the edit), and always writes a timestamped backup. Idempotent and cross-platform.

Registration is one-time per folder. After that Obsidian watches the folder live, so regenerating the wiki shows up instantly. Each generated wiki also ships its own `README.md` with a wandering guide, so it explains itself once opened.

## How not to fool yourself

This is the most important section, and the reason this work is worth trusting. Every number here apologizes for itself; respect the apologies.

- **Small samples lie loudest.** A creator with a huge "lead" over very few chances is one lucky call away from noise. Both the report and the wiki tag these; the tag is not decoration.
- **Upload date is not idea date.** These tools see when something was *published*, not when it was first *thought*. Anything said earlier off-platform is invisible.
- **Corpus growth mimics topic fire.** A burst can just mean you indexed a lot that month. That is exactly why the burst report shows corpus volume first.
- **A link is a claim.** In the wiki, a `[[link]]` exists only where the data validated a real lead-follow relationship. Plain-text names are creators too small to rank, on purpose.
- **The machine finds convergence; you supply the meaning.** These are leads for a human to inspect, never verdicts. On a corpus this size, that framing is not modesty, it is accuracy.

## Poke at the book directly (optional)

If you want to run your own SQL instead of the prepared questions, install the DuckDB command-line app (a separate binary from the Python package: `winget install DuckDB.cli` on Windows, `brew install duckdb` on macOS, `curl https://install.duckdb.org | sh` on Linux) and open the store in its local notebook UI (nothing leaves your machine):

```bash
duckdb -readonly -ui ~/.cache/video-intel/intel.duckdb
```

The tables are the receipts: `sources` (channels), `artifacts` (videos, with `published_at`), `segments`, `concepts`, `entities`, `claims`, joined by `published`, `has_segment`, `mentions`, `has_concept`, `about`, `expresses` (the verbatim quote + timestamp), and the derived `co_occurs`. For example, "which channels covered a concept, earliest first":

```sql
SELECT a.source_id, min(a.published_at::DATE) AS first_covered
FROM has_concept hc JOIN artifacts a USING (artifact_id)
WHERE hc.concept_id = 'ai-engineering.context_engineering'
GROUP BY a.source_id ORDER BY first_covered;
```

## Where to go deeper (optional)

- The science behind the tools, for developers without a stats background: [`intelligence-layer-math.md`](intelligence-layer-math.md). Five primitives (null models, p-values, false-discovery correction, rank correlation, bipartite graphs) that make every report here readable, each tool explained as "those primitives plus one twist," with links to the source papers and beginner-friendly videos.
- Worked examples you can read without running anything: the generated reports in `docs/reports/`.
- The reasoning trail, if you want to see how these choices were made and why other paths were killed: the design notes in `docs/brainstorms/` (the 2026-07-11 weak-signal findings and the 2026-07-12 browsing-surface research are the load-bearing ones).
- The retired Neo4j/GDS graph lens, kept for history: `docs/intelligence-layer-environment-setup.md`.
