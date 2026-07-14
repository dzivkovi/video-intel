# The intelligence layer: the science behind it

This is the companion to [`intelligence-layer.md`](intelligence-layer.md). That page tells you *how to run* the tools. This one tells you *why they work*, for developers who want to go deeper but never took a stats or network-science course. You do not need one. Everything here is five small ideas and a few links, and if you learn the five ideas, every report this project prints stops being a black box.

You are not behind for needing this. Almost nobody outside data science has these primitives on call, and the papers below assume you already do. This page is the bridge that is usually missing. Read it once with the reports open beside it.

## The trick to hold before anything else

Every method here works the same way, and it is worth engraving:

> Build a **fake, boring version of your data** that keeps the uninteresting structure but destroys any real signal. Then ask: **did reality beat the fake version?** If yes, that is your finding.

The fake version is called a **null model**. Everything else on this page is just *different ways to build the fake version*, and *different ways to measure "beat."* Once you see that, the four tools collapse into one idea wearing four costumes.

The second thing to hold: in every report you will see an **observed** number and an **expected** number. "Expected" is always what the fake world (the null) predicts. **Observed vs expected is the entire game.** A big gap is a signal; no gap is noise.

---

## Part 1 - The five primitives to engrave

Learn these once. They are reused in every tool.

### 1. Null model (the fake boring world)

A model that generates data with the structure you want to *control for* but nothing else. If reality looks like the null, there is no news. Example in our SDSM report: the null is "creators pick concepts at random, but each creator still covers the same *number* of concepts and each concept stays as *popular* as it really is." If two creators overlap more than *that* predicts, the overlap is real, not an accident of them both being prolific.
Learn more: [Null model (overview)](https://en.wikipedia.org/wiki/Null_model), and for the network flavor, [Cimini et al., "The statistical physics of real-world networks," 2019](https://doi.org/10.1038/s42254-018-0002-6).

### 2. p-value and significance (how surprised to be)

`p = the probability the null world could produce something at least this extreme by pure luck.` Small p means "luck alone rarely does this" means signal. p = 0.0001 in the lead-lag report means "a creator this far ahead of their expected pace happens by chance about 1 time in 10,000." There is nothing magic about 0.05; it is a convention for "surprising enough to look at."
Learn more (best first stop): search [StatQuest](https://www.youtube.com/c/joshstarmer) for "p-values, clearly explained".

### 3. Multiple testing and FDR (why the tables show `q`, not `p`)

If you test 465 creator pairs at p < 0.05, roughly 23 will look "significant" *by chance alone* (5% of 465). So a raw p-value lies when you run many tests. The **Benjamini-Hochberg** procedure corrects for this and produces `q`, the "false-discovery-adjusted" p. **When a table shows `q < 0.05`, it means "still significant after accounting for how many things we tested."** That is why the SDSM edge table reports `q`, and why the lead-lag `p (perm)` caveat says "after Benjamini-Hochberg."
Learn more: StatQuest, ["FDR and the Benjamini-Hochberg method"](https://www.youtube.com/watch?v=K8LQSvtjcEo); original paper: [Benjamini & Hochberg, 1995](https://doi.org/10.1111/j.2517-6161.1995.tb02031.x).

### 4. Rank correlation (Spearman) - do two rankings secretly agree?

A number from -1 to +1: +1 = identical order, 0 = unrelated, -1 = reversed. The lead-lag "kill diagnostic" `Spearman(lift, corpus size) = -0.50` is asking one question: *is the "who leads" ranking secretly just the "who is biggest" ranking?* If it were, the whole result would be an artifact. Near zero or negative = the two are independent = the leadership signal is real, not a proxy for size.
Learn more: StatQuest, ["Spearman correlation, clearly explained"](https://www.youtube.com/watch?v=Yr1Wbas_QPo); reference: [Spearman's rank correlation](https://en.wikipedia.org/wiki/Spearman%27s_rank_correlation_coefficient).

### 5. Bipartite graph (two kinds of node)

A graph where edges only connect *two different types* of node: here, **creators** on one side, **concepts** on the other, an edge if the creator covered the concept. You cannot directly say "creator A links to creator B" in this graph, but you can *derive* it: two creators are tied if they connect to surprisingly many of the same concepts. That derivation, done carefully with a null model, is the SDSM.
Learn more: [Bipartite network](https://en.wikipedia.org/wiki/Bipartite_network); the projection idea: [Zhou et al., "Bipartite network projection," 2007](https://doi.org/10.1103/PhysRevE.76.046115).

---

## Part 2 - The four tools = the primitives + one twist each

Each tool is the five primitives above plus exactly one new idea (in **bold**). That new idea is the only method-specific math you have to meet.

### Lead-lag report - "who covered an idea first"

- **Question:** who leads on a concept, correcting for the fact that channels entered your corpus at different times and post at different rates.
- **The null:** "firsts handed out in proportion to how often each creator posts." A prolific channel *should* be first a lot just by volume; `lift = observed firsts / expected firsts` divides that out. Lift > 1 means "leads more than volume alone buys."
- **The one twist:** the significance column uses the **Poisson-binomial distribution** - the count of heads when you flip many coins that each have a *different* bias (here, each concept is one flip, and a creator's "bias" to win it is its share of the posting rate). We compute the exact tail: how unlikely is a firsts-count this high?
- **Your numbers:** `seankochel p = 0.0000` (33 firsts vs 18.5 expected: real), `lennyspodcast p = 0.0001` but only 9 concepts (clears the luck test yet still small-sample), `Spearman(lift, size) = -0.50` (leadership is not just size: the result survives its own kill test).
- **Papers:** ["Precursors and Laggards," arXiv:1009.0119](https://ar5iv.labs.arxiv.org/html/1009.0119); ["Who Leads Whom" (Shi, Nallapati, Leskovec, Jurafsky)](https://web.stanford.edu/~jurafsky/grants_v_papers.pdf); [Poisson-binomial distribution](https://en.wikipedia.org/wiki/Poisson_binomial_distribution).

### SDSM network - "which creators genuinely cluster"

- **Question:** which creator pairs share more concepts than chance, given both how prolific each creator is and how popular each concept is.
- **The null:** the **Bipartite Configuration Model (BiCM)**, a **maximum-entropy** model. Plain version: "reshuffle who-covers-what as randomly as possible, but keep every creator's number of concepts *and* every concept's number of creators exactly the same." It is the fairest possible coin-flip world that still respects both margins. A pair that overlaps beyond it is a real tie.
- **The one twist:** "maximum entropy" just means "the most random world consistent with the facts we insist on keeping." Fitting it gives each cell a probability `p_ij`; a pair's expected overlap is a Poisson-binomial again.
- **Your numbers:** the weak (degree-only) null called 314 of 465 pairs "significant" - noise, because in a corpus where everyone talks AI, popularity fakes overlap. The BiCM cut that to **10** real edges. The 1.6x multiples look small *because popularity is already removed*: a surviving 1.6x here beats a naive 6x that was mostly "both are popular."
- **Papers:** [Neal, "backbone" / SDSM, 2021](https://doi.org/10.1038/s41598-021-03238-3); [Tumminello et al., 2011](https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0017994); on why max-entropy is the right null: [Cimini et al., 2019](https://doi.org/10.1038/s42254-018-0002-6).

### Burst report - "what is suddenly heating up"

- **Question:** which concepts just jumped in mention-rate against their *own* usual pace, not against how popular they are overall.
- **The null:** "the concept has one steady posting rate forever." A burst is a stretch that is far better explained by a *second, higher* rate.
- **The one twist:** **Kleinberg's two-state automaton**. Imagine a hidden switch that is either "calm" or "excited," and find the cheapest switch-history that explains the observed gaps between videos (a shortest-path calculation called **Viterbi**). Entering the "excited" state costs something, so noise cannot trip it. Output: a start date, an intensity, and rising-or-cooled.
- **Read-first caveat:** the report shows corpus volume first on purpose - if your whole corpus grew that month, many concepts "burst" at once for a boring reason.
- **Paper:** [Kleinberg, "Bursty and Hierarchical Structure in Streams," 2002](https://www.cs.cornell.edu/home/kleinber/bhs.pdf).

### Disparity backbone - the graph-hygiene "kill test" (issue #99, parked)

- **Question:** on the concept co-occurrence graph, once you strip out edges that are just "both words are common," are the *bridge* concepts anything other than the popular ones?
- **The null / filter:** the **Serrano disparity filter** keeps an edge only if it is disproportionately strong for one of its endpoints *relative to how that node spreads its weight* - it survives if `(1 - normalized_weight)^(degree-1) < 0.05`. Then **Brandes betweenness** finds the bridge nodes (how often a node sits on shortest paths).
- **The result you saw:** overlap between the "bridge" top-15 and the "popular" top-15 came out 9/15 - a formal draw (needs >= 10 to declare "the graph adds nothing beyond popularity," <= 6 to reopen it). That is why #99 is parked, not closed.
- **Papers:** [Serrano, Boguna, Vespignani, PNAS 2009](https://www.pnas.org/doi/10.1073/pnas.0808904106); [Brandes, "A faster algorithm for betweenness centrality," 2001](https://doi.org/10.1080/0022250X.2001.9990249).

---

## Part 3 - A learning path (in order)

**First, the five primitives (fastest payoff, ~2 hours total).** [StatQuest](https://www.youtube.com/c/joshstarmer) is the single best resource for a visual, applied learner - watch, in order: p-values, false discovery rate / Benjamini-Hochberg, then Spearman correlation. For the coin-flip intuition behind Poisson-binomial, [3Blue1Brown, "Binomial distributions"](https://www.youtube.com/watch?v=8idr1WZ1A7Q).

**Then, one method at a time, skim only the introduction** of each paper linked in Part 2. You now have the vocabulary, so the intros will read like English. Start with the two that shipped as clean wins: "Precursors and Laggards" (lead-lag) and Neal 2021 (SDSM).

**For the network-science backbone**, if you want one book: Barabasi's [*Network Science*](http://networksciencebook.com/) is free online and the chapters on degree distributions and random-network models cover the null-model idea properly.

---

## The one paragraph to reread when a report confuses you

Do not read a report as "the output of an algorithm I do not understand." Read it as: *"Someone built a fair, boring coin-flip world that keeps the facts we insist on (how much each creator posts, how popular each concept is), and these few things beat that world by more than luck explains, after correcting for how many things we checked."* That sentence covers roughly 90 percent of everything here. The last 10 percent is which coin-flip world each tool builds - and Part 2 is just that list.

And the honest floor, always: this corpus is about 100x smaller than the studies these methods come from, so every number is a **lead to inspect, not a verdict**. Respecting that is not modesty; on this much data it is simply accurate.
