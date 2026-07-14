# Who Leads the AI-Coding Conversation - Coverage-Corrected Lead-Lag Report

Generated: 2026-07-13 | Issue #93 | Substrate: DuckDB truth store (PR #86)

Corpus: 1384 artifacts across 41 creators (26 rankable at >= 5 artifacts); 591 concepts total, 116 pass the adoption + eligibility filters (adopted by >= 4 creators, >= 3 of them with coverage active at emergence).

## Method in one paragraph

First-mention dates come from `has_concept -> artifacts.published_at`. The confound: creators entered the corpus with different lookback depths, so a deep-backfill channel is 'first' on anything that emerged before the others were indexed. Correction (minimal form of arXiv:1009.0119): (1) a concept only counts if enough adopters' coverage windows were active at its emergence; (2) expected firsts are proportional to posting rate among those eligible adopters, so `lift = observed firsts / expected firsts` rewards leading beyond volume-implied chance. Lift > 1 means the creator is first more often than their posting volume predicts. Creators below the artifact floor still set emergence dates (so nobody inherits a first they did not earn) but are not themselves ranked.

## Corpus coverage windows (the confound, stated)

| Creator | Coverage start | Coverage end | Artifacts | Rate/day | Ranked |
|---|---|---|---|---|---|
| ycombinator | 2024-07-08 | 2026-06-27 | 48 | 0.067 | yes |
| gregisenberg | 2024-08-28 | 2026-07-01 | 28 | 0.042 | yes |
| engineerprompt | 2024-10-15 | 2026-07-02 | 233 | 0.372 | yes |
| seankochel | 2025-01-15 | 2026-07-02 | 135 | 0.253 | yes |
| vanishinggradients | 2025-05-23 | 2026-07-02 | 50 | 0.123 | yes |
| graceleungyl | 2025-07-04 | 2026-06-30 | 30 | 0.083 | yes |
| lennyspodcast | 2025-07-17 | 2026-05-24 | 13 | 0.042 | yes |
| arize | 2025-09-19 | 2026-02-18 | 2 | 0.013 | no |
| chase_h_ai | 2025-10-24 | 2026-07-08 | 127 | 0.492 | yes |
| iangarlic | 2025-11-18 | 2026-04-24 | 5 | 0.032 | yes |
| natebjones | 2025-11-30 | 2026-07-10 | 210 | 0.942 | yes |
| propertydaily | 2025-11-30 | 2026-06-17 | 3 | 0.015 | no |
| samwitteveenai | 2025-12-03 | 2026-07-07 | 52 | 0.240 | yes |
| ramjad | 2025-12-10 | 2026-07-05 | 31 | 0.149 | yes |
| mark_kashef | 2026-01-13 | 2026-07-05 | 49 | 0.282 | yes |
| thenextnewthingai | 2026-02-03 | 2026-06-19 | 66 | 0.482 | yes |
| systemsmadebetter | 2026-02-12 | 2026-06-20 | 9 | 0.070 | yes |
| colemedin | 2026-02-23 | 2026-07-02 | 33 | 0.254 | yes |
| bioinfquests | 2026-02-23 | 2026-02-23 | 2 | 2.000 | no |
| saminyasar | 2026-02-23 | 2026-06-29 | 15 | 0.118 | yes |
| simonscrapes | 2026-02-27 | 2026-06-25 | 39 | 0.328 | yes |
| sean-c-davis | 2026-03-07 | 2026-06-26 | 20 | 0.179 | yes |
| benai92 | 2026-03-14 | 2026-07-02 | 19 | 0.171 | yes |
| everyinc | 2026-03-17 | 2026-07-08 | 45 | 0.395 | yes |
| austinmarchese | 2026-03-19 | 2026-06-28 | 24 | 0.235 | yes |
| double-down-news | 2026-04-07 | 2026-04-07 | 1 | 1.000 | no |
| brockmesarich | 2026-04-09 | 2026-06-29 | 26 | 0.317 | yes |
| the-entrepreneurs-studio | 2026-04-14 | 2026-04-14 | 1 | 1.000 | no |
| kieranklaassen | 2026-04-23 | 2026-04-23 | 1 | 1.000 | no |
| prismlabs | 2026-04-24 | 2026-04-24 | 1 | 1.000 | no |
| twist | 2026-04-28 | 2026-07-01 | 19 | 0.292 | yes |
| simon-scrapes | 2026-04-30 | 2026-04-30 | 1 | 1.000 | no |
| kunchenguid | 2026-05-17 | 2026-06-20 | 4 | 0.114 | no |
| indydevdan | 2026-05-18 | 2026-07-13 | 9 | 0.158 | yes |
| claude | 2026-05-18 | 2026-07-08 | 26 | 0.500 | yes |
| saastr | 2026-05-20 | 2026-05-20 | 1 | 1.000 | no |
| databricks | 2026-06-16 | 2026-06-18 | 2 | 0.667 | no |
| product-grade | 2026-06-16 | 2026-06-16 | 1 | 1.000 | no |
| larridin-inc | 2026-06-25 | 2026-06-25 | 1 | 1.000 | no |
| payton-clark-smith | 2026-06-26 | 2026-06-26 | 1 | 1.000 | no |
| systems-led-growth | 2026-07-02 | 2026-07-02 | 1 | 1.000 | no |

## Corrected leader ranking (precursor lift)

Creators shown: >= 5 artifacts and >= 5 eligible concepts (15 rankable creators omitted for too few eligible concepts).

| # | Creator | Lift | Firsts (obs) | Firsts (expected) | Eligible concepts | Mean lag (days) | p (perm) |
|---|---|---|---|---|---|---|---|
| 1 | lennyspodcast | 6.71 | 7.0 | 1.0 | 14 | 70 | 0.0000 * |
| 2 | ramjad | 5.79 | 4.0 | 0.7 | 5 | 12 | 0.0013 * |
| 3 | graceleungyl | 3.13 | 4.0 | 1.3 | 8 | 50 | 0.0230 |
| 4 | seankochel | 1.83 | 49.0 | 26.7 | 71 | 41 | 0.0000 * |
| 5 | chase_h_ai | 1.37 | 5.0 | 3.6 | 9 | 37 | 0.2698 |
| 6 | vanishinggradients | 1.08 | 6.0 | 5.5 | 28 | 90 | 0.4889 |
| 7 | natebjones | 0.91 | 8.5 | 9.3 | 14 | 34 | 0.6919 |
| 8 | gregisenberg | 0.61 | 3.0 | 4.9 | 73 | 300 | 0.8774 |
| 9 | mark_kashef | 0.55 | 0.5 | 0.9 | 5 | 36 | 0.6371 |
| 10 | engineerprompt | 0.45 | 22.0 | 48.8 | 93 | 104 | 1.0000 |
| 11 | ycombinator | 0.00 | 0.0 | 8.1 | 75 | 313 | 1.0000 |

`p (perm)` (Spec A.2): the RAW P(firsts >= observed) under the rate-proportional null - each concept's single first slot goes to a rankable eligible adopter with probability proportional to its posting rate (the closed-form Poisson-binomial tail of the 10,000-draw permutation). A trailing `*` marks the **3 of 11** ranked creators that still clear p < 0.05 AFTER Benjamini-Hochberg correction (the raw p alone is not multiple-comparison safe). A small-sample creator can clear this rate-null and still be a coverage artifact - the column tests 'beyond volume-implied luck', not 'beyond every confound'; read it with the small-sample caveat below.

## Naive ranking (uncorrected, for contrast)

| # | Creator | Naive firsts |
|---|---|---|
| 1 | seankochel | 76.0 |
| 2 | engineerprompt | 50.0 |
| 3 | natebjones | 13.5 |
| 4 | vanishinggradients | 10.0 |
| 5 | graceleungyl | 9.0 |
| 6 | lennyspodcast | 7.0 |
| 7 | chase_h_ai | 6.0 |
| 8 | iangarlic | 4.0 |
| 9 | gregisenberg | 4.0 |
| 10 | ramjad | 4.0 |
| 11 | ycombinator | 3.0 |
| 12 | saminyasar | 2.0 |
| 13 | systemsmadebetter | 1.0 |
| 14 | thenextnewthingai | 1.0 |
| 15 | mark_kashef | 0.5 |

## Kill-criterion diagnostics

- Spearman(corrected lift, coverage-start date): **+0.42**. Negative means earlier-indexed channels still rank higher (coverage artifact); near zero means the correction removed the indexing-age effect.
- Spearman(corrected lift, corpus size): **-0.41**. Positive means bigger channels still rank higher (popularity artifact); negative means smaller channels out-lead their posting volume.
- Issue #93 kill criterion: if, after coverage correction, the leaders are just the biggest / oldest-indexed channels, the influence signal is not there.

## Top 10 findings (adoption chains with evidence)

### 1. `ai-engineering.agentic_commerce`

Chain: chase_h_ai(26-01-09) -> samwitteveenai(26-01-12) -> ycombinator(26-02-21) -> natebjones(26-03-22) -> gregisenberg(26-05-11)

Leader evidence (chase_h_ai first on 2026-01-09):

> "...." SCREEN [35:11-35:38] [terminal]: Claude Code terminal showing the command '/gsd:plan-phase 6' for Stripe integration. [35:14] Chase Hannegan (Host): "So at this point we should have finished up phase five, which was t..."
>
> - chase_h_ai, [Claude Code: n8n Workflow to Deployed SaaS (Complete System)](https://www.youtube.com/watch?v=QgL-Z6YlHeA&t=2086), 2026-01-09

### 2. `ai-engineering.ai_personalization`

Chain: iangarlic(25-12-15) -> natebjones(26-02-05) -> engineerprompt(26-04-14) -> gregisenberg(26-05-18) -> vanishinggradients(26-06-19)

Leader evidence (iangarlic first on 2025-12-15):

> "...derstand that. So AI now has-there's so many subtle things that we see in human faces. But I think hyper-personalization is the future. And it's not just like hyper-personalization in the videos, it's how we deliver the..."
>
> - iangarlic, [The YouTube Strategy Smart Business Owners Use to Win Premium Clients](https://www.youtube.com/watch?v=C4eSW921lhQ&t=1271), 2025-12-15

### 3. `ai-engineering.disposable_software`

Chain: natebjones(26-01-20) -> engineerprompt(26-03-09) -> vanishinggradients(26-05-08) -> ycombinator(26-05-21)

Leader evidence (natebjones first on 2026-01-20):

> "[00:00] Nate B. Jones (Content Creator/Writer): "The age of disposable software is here and almost no one understands what that really means. Look, everyone's talking about dispos..."
>
> - natebjones, [Disposable Software: The Trend 90% of People are Getting Wrong--The Hidden Costs We Need to Consider](https://www.youtube.com/watch?v=ra7nYJe86GI&t=0), 2026-01-20

### 4. `ai-engineering.robotic_learning`

Chain: natebjones(25-12-20) -> ycombinator(26-01-22) -> lennyspodcast(26-05-17) -> samwitteveenai(26-06-01) -> gregisenberg(26-06-25)

Leader evidence (natebjones first on 2025-12-20):

> "[07:49] Nate B. Jones (AI News Presenter): "I would watch Peter DeSantis's first major announcements here, whether he's got custom silicon roadmaps with Trainium, whether he's got an AGI team product launch to put togeth..."
>
> - natebjones, [Amazon Fired Their AI Chief. Here's Why It Took So Long (Plus 5 Newsworthy Moments in AI This Week)](https://www.youtube.com/watch?v=EaMz3g1OYPA&t=469), 2025-12-20

### 5. `ai-engineering.ai_governance`

Chain: lennyspodcast(25-07-20) -> vanishinggradients(25-07-29) -> ycombinator(26-02-21) -> engineerprompt(26-05-07) -> gregisenberg(26-06-23)

Leader evidence (lennyspodcast first on 2025-07-20):

> "...perintelligence. And to be clear, I don't think it's actually that dangerous right now. Like our responsible scaling policy defines these AI safety levels that tries to figure out for each level of model intelligence wha..."
>
> - lennyspodcast, [Anthropic co-founder: AGI predictions, leaving OpenAI, what keeps him up at night | Ben Mann](https://www.youtube.com/watch?v=WWoyWNhx2XU&t=2063), 2025-07-20

### 6. `ai-engineering.autonomous_software_production`

Chain: natebjones(26-02-18) -> thenextnewthingai(26-04-13) -> lennyspodcast(26-05-02)

Leader evidence (natebjones first on 2026-02-18):

> "...el 2: Junior Developer Level 3: Developer as Manager Level 4: Developer as Product Manager Level 5: The Dark Factory" [02:49] Nate B. Jones (AI Strategy Consultant & Content Creator): "This is GitHub Copilot in its origi..."
>
> - natebjones, [The 5 Levels of AI Coding (Why Most of You Won't Make It Past Level 2)](https://www.youtube.com/watch?v=bDcgHzCBgmQ&t=142), 2026-02-18

### 7. `ai-engineering.continual_learning`

Chain: engineerprompt(26-01-08) -> vanishinggradients(26-01-27) -> ycombinator(26-03-27)

Leader evidence (engineerprompt first on 2026-01-08):

> "[03:16] Narrator (AI Researcher/Content Creator): "And then we have level 4, which I would call true continual learning. So here, updating the model's weights in real-time without forgetting, without degradation. This is..."
>
> - engineerprompt, [The Holy Grail of Intelligence - Explained.](https://www.youtube.com/watch?v=2NDMtAEu7FQ&t=196), 2026-01-08

### 8. `ai-engineering.information_filtering`

Chain: natebjones(26-01-29) -> engineerprompt(26-02-18) -> vanishinggradients(26-04-30)

Leader evidence (natebjones first on 2026-01-29):

> "[00:00] Nate B. Jones (Content Creator and Analyst): "The most powerful digital platforms in our lives lost their edge in late 2025 and early 2026 and almost nobody has noticed it yet. For as long as we've used digital p..."
>
> - natebjones, [Why Every Cold Application You Send Is a Waste of Time (And What Actually Works)](https://www.youtube.com/watch?v=AoA9h3TjxE0&t=0), 2026-01-29

### 9. `productivity.high_agency_mindset`

Chain: natebjones(26-01-22) -> ycombinator(26-03-09) -> lennyspodcast(26-05-02)

Leader evidence (natebjones first on 2026-01-22):

> "[01:16] Nate B. Jones (Content Creator / Writer): "So what do you do when the ladder disappears? The answer is high agency. And I don't mean ordinary high agency, the kind that was always useful, always correlated with s..."
>
> - natebjones, [The People Getting Promoted All Have This One Thing in Common (AI Is Supercharging this Mindset)](https://www.youtube.com/watch?v=HZ9iL_lFYgQ&t=76), 2026-01-22

### 10. `ai-engineering.autonomous_agent_payments`

Chain: natebjones(26-02-21) -> thenextnewthingai(26-03-19) -> gregisenberg(26-06-02)

Leader evidence (natebjones first on 2026-02-21):

> "[26:05] Nate B. Jones (Tech Commentator and Writer): "In my last video on OpenClaw, I talked about the 70/30 rule: the idea that people consistently want to maintain maybe roughly 70% human control of agent-delegated tas..."
>
> - natebjones, [The $285B Sell-Off Was Just the Beginning — The Infrastructure Story Is Bigger.](https://www.youtube.com/watch?v=O-0poNv2jD4&t=1565), 2026-02-21

## Caveats

- This corpus is ~100x smaller than the studies the method comes from: every row above is a lead for manual inspection, not a verdict (issue #95 guardrail).
- `published_at` is upload date; a concept discussed in a members-only or unindexed video earlier is invisible. Eligibility bounds this but cannot eliminate it.
- Concept extraction granularity is uneven (issue #85 lineage); a chain over a generic concept (e.g. 'ai agents') is weaker evidence than one over a specific pattern.
- Small-sample lifts: a creator with expected firsts < 2 can post an extreme lift from one or two lucky firsts. Read the lift column together with the observed/expected columns; lifts backed by expected >= 5 are the trustworthy ones.
- Quotes are located by searching the leader's transcript for the extracted term (verbatim, then entity-link, then token match). A token-matched quote can set the topic's context rather than land on the exact utterance; the timestamped link is the ground truth.
