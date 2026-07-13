# Who Leads the AI-Coding Conversation - Coverage-Corrected Lead-Lag Report

Generated: 2026-07-13 | Issue #93 | Substrate: DuckDB truth store (PR #86)

Corpus: 1259 artifacts across 31 creators (23 rankable at >= 5 artifacts); 551 concepts total, 69 pass the adoption + eligibility filters (adopted by >= 4 creators, >= 3 of them with coverage active at emergence).

## Method in one paragraph

First-mention dates come from `has_concept -> artifacts.published_at`. The confound: creators entered the corpus with different lookback depths, so a deep-backfill channel is 'first' on anything that emerged before the others were indexed. Correction (minimal form of arXiv:1009.0119): (1) a concept only counts if enough adopters' coverage windows were active at its emergence; (2) expected firsts are proportional to posting rate among those eligible adopters, so `lift = observed firsts / expected firsts` rewards leading beyond volume-implied chance. Lift > 1 means the creator is first more often than their posting volume predicts. Creators below the artifact floor still set emergence dates (so nobody inherits a first they did not earn) but are not themselves ranked.

## Corpus coverage windows (the confound, stated)

| Creator | Coverage start | Coverage end | Artifacts | Rate/day | Ranked |
|---|---|---|---|---|---|
| gregisenberg | 2024-08-28 | 2026-06-11 | 16 | 0.025 | yes |
| engineerprompt | 2024-10-15 | 2026-07-01 | 226 | 0.362 | yes |
| seankochel | 2025-01-15 | 2026-06-26 | 134 | 0.254 | yes |
| vanishinggradients | 2025-05-23 | 2026-06-19 | 48 | 0.122 | yes |
| graceleungyl | 2025-07-04 | 2026-06-12 | 29 | 0.084 | yes |
| lennyspodcast | 2025-07-17 | 2026-05-24 | 12 | 0.038 | yes |
| chase_h_ai | 2025-10-24 | 2026-06-26 | 122 | 0.496 | yes |
| iangarlic | 2025-11-18 | 2026-04-24 | 5 | 0.032 | yes |
| propertydaily | 2025-11-30 | 2026-06-17 | 3 | 0.015 | no |
| natebjones | 2025-11-30 | 2026-06-26 | 199 | 0.952 | yes |
| samwitteveenai | 2025-12-03 | 2026-06-26 | 48 | 0.233 | yes |
| ramjad | 2025-12-10 | 2026-06-25 | 29 | 0.146 | yes |
| ycombinator | 2026-01-08 | 2026-06-27 | 47 | 0.275 | yes |
| thenextnewthingai | 2026-02-03 | 2026-06-19 | 66 | 0.482 | yes |
| mark_kashef | 2026-02-15 | 2026-06-30 | 34 | 0.250 | yes |
| colemedin | 2026-02-23 | 2026-06-25 | 32 | 0.260 | yes |
| saminyasar | 2026-02-23 | 2026-06-24 | 14 | 0.115 | yes |
| bioinfquests | 2026-02-23 | 2026-02-23 | 2 | 2.000 | no |
| simonscrapes | 2026-02-27 | 2026-06-25 | 39 | 0.328 | yes |
| benai92 | 2026-03-14 | 2026-06-25 | 17 | 0.163 | yes |
| everyinc | 2026-03-17 | 2026-06-24 | 42 | 0.420 | yes |
| austinmarchese | 2026-03-19 | 2026-06-23 | 23 | 0.237 | yes |
| double-down-news | 2026-04-07 | 2026-04-07 | 1 | 1.000 | no |
| brockmesarich | 2026-04-09 | 2026-06-24 | 25 | 0.325 | yes |
| kieranklaassen | 2026-04-23 | 2026-04-23 | 1 | 1.000 | no |
| prismlabs | 2026-04-24 | 2026-04-24 | 1 | 1.000 | no |
| twist | 2026-04-28 | 2026-06-24 | 16 | 0.276 | yes |
| simon-scrapes | 2026-04-30 | 2026-04-30 | 1 | 1.000 | no |
| claude | 2026-05-18 | 2026-06-21 | 24 | 0.686 | yes |
| saastr | 2026-05-20 | 2026-05-20 | 1 | 1.000 | no |
| databricks | 2026-06-16 | 2026-06-18 | 2 | 0.667 | no |

## Corrected leader ranking (precursor lift)

Creators shown: >= 5 artifacts and >= 5 eligible concepts (14 rankable creators omitted for too few eligible concepts).

| # | Creator | Lift | Firsts (obs) | Firsts (expected) | Eligible concepts | Mean lag (days) | p (perm) |
|---|---|---|---|---|---|---|---|
| 1 | lennyspodcast | 9.57 | 5.0 | 0.5 | 9 | 59 | 0.0001 |
| 2 | graceleungyl | 2.42 | 2.0 | 0.8 | 6 | 67 | 0.1924 |
| 3 | seankochel | 1.79 | 33.0 | 18.5 | 47 | 34 | 0.0000 |
| 4 | gregisenberg | 1.17 | 2.0 | 1.7 | 44 | 280 | 0.5150 |
| 5 | natebjones | 0.85 | 5.0 | 5.9 | 9 | 37 | 0.8421 |
| 6 | chase_h_ai | 0.78 | 2.0 | 2.6 | 6 | 55 | 0.8215 |
| 7 | vanishinggradients | 0.58 | 2.0 | 3.5 | 20 | 95 | 0.8885 |
| 8 | engineerprompt | 0.26 | 8.0 | 30.3 | 58 | 107 | 1.0000 |
| 9 | ycombinator | 0.00 | 0.0 | 1.0 | 5 | 89 | 1.0000 |

`p (perm)` (Spec A.2): P(firsts >= observed) under the rate-proportional null - each concept's single first slot goes to a rankable eligible adopter with probability proportional to its posting rate (the closed-form Poisson-binomial tail of the 10,000-draw permutation). **2 of 9** ranked creators clear p < 0.05 after Benjamini-Hochberg correction. A small-sample creator can clear this rate-null and still be a coverage artifact - the column tests 'beyond volume-implied luck', not 'beyond every confound'; read it with the small-sample caveat below.

## Naive ranking (uncorrected, for contrast)

| # | Creator | Naive firsts |
|---|---|---|
| 1 | seankochel | 73.0 |
| 2 | engineerprompt | 48.0 |
| 3 | vanishinggradients | 10.0 |
| 4 | natebjones | 10.0 |
| 5 | graceleungyl | 8.0 |
| 6 | lennyspodcast | 7.0 |
| 7 | chase_h_ai | 6.0 |
| 8 | iangarlic | 4.0 |
| 9 | ramjad | 4.0 |
| 10 | gregisenberg | 3.0 |
| 11 | saminyasar | 2.0 |
| 12 | ycombinator | 1.0 |
| 13 | thenextnewthingai | 1.0 |

## Kill-criterion diagnostics

- Spearman(corrected lift, coverage-start date): **-0.18**. Negative means earlier-indexed channels still rank higher (coverage artifact); near zero means the correction removed the indexing-age effect.
- Spearman(corrected lift, corpus size): **-0.50**. Positive means bigger channels still rank higher (popularity artifact); negative means smaller channels out-lead their posting volume.
- Issue #93 kill criterion: if, after coverage correction, the leaders are just the biggest / oldest-indexed channels, the influence signal is not there.

## Top 10 findings (adoption chains with evidence)

### 1. `ai-engineering.agentic_commerce`

Chain: chase_h_ai(26-01-09) -> samwitteveenai(26-01-12) -> ycombinator(26-02-21) -> natebjones(26-03-22) -> gregisenberg(26-05-11)

Leader evidence (chase_h_ai first on 2026-01-09):

> "...." SCREEN [35:11-35:38] [terminal]: Claude Code terminal showing the command '/gsd:plan-phase 6' for Stripe integration. [35:14] Chase Hannegan (Host): "So at this point we should have finished up phase five, which was t..."
>
> - chase_h_ai, [Claude Code: n8n Workflow to Deployed SaaS (Complete System)](https://www.youtube.com/watch?v=QgL-Z6YlHeA&t=2086), 2026-01-09

### 2. `ai-engineering.disposable_software`

Chain: natebjones(26-01-20) -> engineerprompt(26-03-09) -> vanishinggradients(26-05-08) -> ycombinator(26-05-21)

Leader evidence (natebjones first on 2026-01-20):

> "[00:00] Nate B. Jones (Content Creator/Writer): "The age of disposable software is here and almost no one understands what that really means. Look, everyone's talking about dispos..."
>
> - natebjones, [Disposable Software: The Trend 90% of People are Getting Wrong--The Hidden Costs We Need to Consider](https://www.youtube.com/watch?v=ra7nYJe86GI&t=0), 2026-01-20

### 3. `ai-engineering.ai_personalization`

Chain: iangarlic(25-12-15) -> natebjones(26-02-05) -> engineerprompt(26-04-14) -> vanishinggradients(26-06-19)

Leader evidence (iangarlic first on 2025-12-15):

> "...derstand that. So AI now has-there's so many subtle things that we see in human faces. But I think hyper-personalization is the future. And it's not just like hyper-personalization in the videos, it's how we deliver the..."
>
> - iangarlic, [The YouTube Strategy Smart Business Owners Use to Win Premium Clients](https://www.youtube.com/watch?v=C4eSW921lhQ&t=1271), 2025-12-15

### 4. `ai-engineering.autonomous_software_production`

Chain: natebjones(26-02-18) -> thenextnewthingai(26-04-13) -> lennyspodcast(26-05-02)

Leader evidence (natebjones first on 2026-02-18):

> "...el 2: Junior Developer Level 3: Developer as Manager Level 4: Developer as Product Manager Level 5: The Dark Factory" [02:49] Nate B. Jones (AI Strategy Consultant & Content Creator): "This is GitHub Copilot in its origi..."
>
> - natebjones, [The 5 Levels of AI Coding (Why Most of You Won't Make It Past Level 2)](https://www.youtube.com/watch?v=bDcgHzCBgmQ&t=142), 2026-02-18

### 5. `ai-engineering.continual_learning`

Chain: engineerprompt(26-01-08) -> vanishinggradients(26-01-27) -> ycombinator(26-03-27)

Leader evidence (engineerprompt first on 2026-01-08):

> "[03:16] Narrator (AI Researcher/Content Creator): "And then we have level 4, which I would call true continual learning. So here, updating the model's weights in real-time without forgetting, without degradation. This is..."
>
> - engineerprompt, [The Holy Grail of Intelligence - Explained.](https://www.youtube.com/watch?v=2NDMtAEu7FQ&t=196), 2026-01-08

### 6. `ai-engineering.information_filtering`

Chain: natebjones(26-01-29) -> engineerprompt(26-02-18) -> vanishinggradients(26-04-30)

Leader evidence (natebjones first on 2026-01-29):

> "[00:00] Nate B. Jones (Content Creator and Analyst): "The most powerful digital platforms in our lives lost their edge in late 2025 and early 2026 and almost nobody has noticed it yet. For as long as we've used digital p..."
>
> - natebjones, [Why Every Cold Application You Send Is a Waste of Time (And What Actually Works)](https://www.youtube.com/watch?v=AoA9h3TjxE0&t=0), 2026-01-29

### 7. `ai-engineering.autonomous_ai_agents`

Chain: seankochel(25-03-14) -> engineerprompt(25-04-21) -> gregisenberg(25-07-09)

Leader evidence (seankochel first on 2025-03-14):

> "[02:00] Sean Kochel (Business Owner and AI Educator): "The reason I feel confident teaching you guys this stuff is because I build and deploy these systems myself in my own businesses that combined do eight figures per y..."
>
> - seankochel, [Why AI Amateurs Are Building Better Agents Than You](https://www.youtube.com/watch?v=BNTcAhmoF1Q&t=120), 2025-03-14

### 8. `ai-engineering.automated_asset_production`

Chain: gregisenberg(25-07-23) -> graceleungyl(25-09-12) -> seankochel(25-11-20)

Leader evidence (gregisenberg first on 2025-07-23):

> "SCREEN [13:16-14:00] [other]: Cody Schneider speaking full screen. [13:50] Greg Isenberg (Host): "Right. Cool. What's the next idea?" [13:52] Cody Schneider (Guest): "Awesome. So, uh, the next one on the list, uh, is Fac..."
>
> - gregisenberg, [The 6 Best AI Agency Niches to Make $50K/mo (Data-Backed)](https://www.youtube.com/watch?v=6FSih5a5aIA&t=796), 2025-07-23

### 9. `ai-engineering.ai_product_management`

Chain: lennyspodcast(25-09-25) -> seankochel(25-09-30) -> vanishinggradients(26-02-17)

Leader evidence (lennyspodcast first on 2025-09-25):

> "SCREEN [01:18-01:27] [text_overlay]: Two quotes on screen. One from Mike Krieger, Anthropic CPO: 'If there is one thing we can teach people, it's that writing evals is probably the most important thing.' The other from K..."
>
> - lennyspodcast, [Why AI evals are the hottest new skill for product builders | Hamel Husain & Shreya Shankar](https://www.youtube.com/watch?v=BsWxPI9UM4c&t=78), 2025-09-25

### 10. `ai-engineering.robotic_learning`

Chain: natebjones(25-12-20) -> lennyspodcast(26-05-17) -> samwitteveenai(26-06-01)

Leader evidence (natebjones first on 2025-12-20):

> "[07:49] Nate B. Jones (AI News Presenter): "I would watch Peter DeSantis's first major announcements here, whether he's got custom silicon roadmaps with Trainium, whether he's got an AGI team product launch to put togeth..."
>
> - natebjones, [Amazon Fired Their AI Chief. Here's Why It Took So Long (Plus 5 Newsworthy Moments in AI This Week)](https://www.youtube.com/watch?v=EaMz3g1OYPA&t=469), 2025-12-20

## Caveats

- This corpus is ~100x smaller than the studies the method comes from: every row above is a lead for manual inspection, not a verdict (issue #95 guardrail).
- `published_at` is upload date; a concept discussed in a members-only or unindexed video earlier is invisible. Eligibility bounds this but cannot eliminate it.
- Concept extraction granularity is uneven (issue #85 lineage); a chain over a generic concept (e.g. 'ai agents') is weaker evidence than one over a specific pattern.
- Small-sample lifts: a creator with expected firsts < 2 can post an extreme lift from one or two lucky firsts. Read the lift column together with the observed/expected columns; lifts backed by expected >= 5 are the trustworthy ones.
- Quotes are located by searching the leader's transcript for the extracted term (verbatim, then entity-link, then token match). A token-matched quote can set the topic's context rather than land on the exact utterance; the timestamped link is the ground truth.
