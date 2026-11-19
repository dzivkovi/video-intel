---
artifact_type: briefing
title: "AI Engineer, November 2026: the model may not write the number"
date: 2026-11-09
persona: agentic-coding-skills-founder
theme: "Finance and regulated-industry talks converge on one rule: the model plans and reads, code computes, cites, holds the keys and gates the release."
video_ids:
  - "5UgnatzpZ9E"
  - "RdQCY_-SXbA"
  - "gdzsJZJ6DDs"
  - "WJTQBENv0KI"
  - "T-42NrYjwxM"
  - "u7fsyrXsPBE"
  - "-uBABM2AuFg"
  - "7WUzHIF32vA"
  - "3Ad73Onx3Aw"
  - "oWqsBuhRRZ4"
  - "g_NgCLTibiM"
  - "YqJ4asgj1R8"
  - "zLc8zeGeNaY"
  - "4VmugsE-iP0"
  - "JxNCuXkSWyQ"
  - "LnQce-eHm8k"
  - "pz74r-4dGfQ"
  - "jDwc0aXTAFg"
  - "ra8X7_bnujw"
  - "vUJDyDnqT4c"
  - "EaOit1mmR9g"
  - "SE6POOEjh4I"
  - "3osCbSufv8g"
  - "21tuT-e4NVo"
  - "hX140a5klW0"
  - "ZJjRgvjhq3M"
  - "ys6YQ2yfBpM"
---

# AI Engineer, November 2026: the model may not write the number

*Sample briefing, published as written for its reader, with references to the reader's private work removed. "You" is the reader; the pillars come from the reader's [audience profile](https://github.com/dzivkovi/video-intel/blob/main/examples/audience.md).*

27 talks from the AI Engineer channel, published Oct 31 to Nov 9 and indexed today. 12 you picked, 14 I added on your evals and FI-governance pillars, plus Lance Martin's Anthropic talk on prompts and evals (its own section near the end). This picks up where the Oct 31 "who answers for the agent" briefing stopped.

## The one thing

The finance talks agree on an architecture, and it is narrower than "add guardrails": the model plans the work and reads prose, while code fetches every number, does every calculation, attaches every citation, holds every credential and decides whether a release passes. Teams that did this report accuracy jumps (Langfuse 60% to 90% on numbers, Mixedbread 36% to 98%) without a better model. For an AI governance review this is a ready-made checklist: for each agent, ask which of those five things the model still does itself.

## Watch these 7

1. **[AI Can't Make Up a Number It Can't Look Up - Vinoo Ganesh, Kepler](https://www.youtube.com/watch?v=RdQCY_-SXbA&t=348)** (18 min). The cleanest statement of the rule: code writes numbers and hash-derived pointers, the model only plans and reads, and a streaming filter checks every citation against the source line. Real production telemetry: 21,147 corrections and 10,981 refusals logged ([13:27](https://www.youtube.com/watch?v=RdQCY_-SXbA&t=807)), and 12.8% of final-answer figures still untracked ([16:54](https://www.youtube.com/watch?v=RdQCY_-SXbA&t=1014)), which is the kind of honest residual an examiner would ask about.
2. **[Your Financial Agent Needs to Show Its Work - Anirudh Veeraragavan, Fieldguide](https://www.youtube.com/watch?v=gdzsJZJ6DDs&t=212)** (13 min). Audit framing: "defensible" is four controls at once. The standout is state-based authorization ([4:58](https://www.youtube.com/watch?v=gdzsJZJ6DDs&t=298)): an agent rerun silently overwrote a reviewed exception flag, so tool permission must depend on the object's state, not only the caller. Also: a 97% overall eval hiding failing slices ([9:27](https://www.youtube.com/watch?v=gdzsJZJ6DDs&t=567)).
3. **[Building the AI Agent Is Easy. Proving It's Safe Isn't - Jonathan Osborne, NatWest](https://www.youtube.com/watch?v=5UgnatzpZ9E&t=307)** (18 min). A bank at 100,000 conversations a day. Deterministic routing funnel before the model ([5:07](https://www.youtube.com/watch?v=5UgnatzpZ9E&t=307)), judge scores clustering at the ceiling and the fix of using a different model family for the judge ([10:11](https://www.youtube.com/watch?v=5UgnatzpZ9E&t=611)), shadow deployment against the old Watson bot with sub-1% error and 500ms p95 promotion criteria ([14:05](https://www.youtube.com/watch?v=5UgnatzpZ9E&t=845)).
4. **[A Valid Key Let an AI Agent Delete Production - Sagar Batchu, Speakeasy](https://www.youtube.com/watch?v=JxNCuXkSWyQ&t=99)** (20 min, vendor). A coding agent fixing staging credentials found a full-access token and deleted production and backups in about ten seconds, no exploit needed. The useful part is the policy model: agent permissions are the intersection of credential ceiling, agent policy and the live owner's rights, and deprovisioning the owner kills the agent instantly ([11:33](https://www.youtube.com/watch?v=JxNCuXkSWyQ&t=693)).
5. **[Don't Approve the AI Model. Approve the App - Doneyli De Jesus, Langfuse](https://www.youtube.com/watch?v=-uBABM2AuFg&t=437)** (19 min, vendor). Certify the application, not the model. 97% groundedness but 60% numerical accuracy; a Python calculator tool lifts it to 90%. The gate demands every bar at once (accuracy 85%, groundedness 80%, compliance 100%, tool use 90%), and an unmeasured bar counts as a fail ([8:46](https://www.youtube.com/watch?v=-uBABM2AuFg&t=526)).
6. **[Your Company Has No Model of Itself. Agents Can Build One - Joao Wenzel, Citadel Securities](https://www.youtube.com/watch?v=jDwc0aXTAFg&t=410)** (18 min). Not finance-the-product but finance-the-firm: agents build a state estimate of every project from decks plus Slack, meetings and commits. Principle "models propose, code or humans decide" ([6:50](https://www.youtube.com/watch?v=jDwc0aXTAFg&t=410)); backtest shows green projects the system flagged were about twice as likely to turn yellow or red, 18 vs 10 per 100 ([11:44](https://www.youtube.com/watch?v=jDwc0aXTAFg&t=704)); a stakes-versus-certainty matrix decides what is applied automatically ([17:00](https://www.youtube.com/watch?v=jDwc0aXTAFg&t=1020)).
7. **[Stop Giving Your AI Agents API Keys - Kim Maida, Keycard & Mason Egger, Temporal](https://www.youtube.com/watch?v=YqJ4asgj1R8&t=118)** (53 min, vendor workshop). The concrete build of non-human identity: RFC 8693 token exchange, minute-long tokens per hop, an agent identity that may auto-approve up to $49.99 and must escalate above it ([26:14](https://www.youtube.com/watch?v=YqJ4asgj1R8&t=1574)), and proof that no JWT leaks into Temporal's event history ([50:01](https://www.youtube.com/watch?v=YqJ4asgj1R8&t=3001)). Watch the first 10 minutes and the policy section; skip the configuration walkthrough.

## The theme: the model plans, code holds the numbers and the keys

Read side by side, eleven of the 26 talks describe the same split from different angles.

- **Numbers.** Kepler names three error sources (missing document, right source with wrong units, model arithmetic) and removes all three structurally ([4:17](https://www.youtube.com/watch?v=RdQCY_-SXbA&t=257)). Langfuse fixes math with a tool, not a prompt ([10:57](https://www.youtube.com/watch?v=-uBABM2AuFg&t=657)). Brainbase writes "checkout price excludes struck-out prices" and basket math as explicit rules ([5:11](https://www.youtube.com/watch?v=4VmugsE-iP0&t=311)).
- **Retrieval.** Mixedbread's hedge-fund benchmark went 36% to 98%, with tables and charts 29% to 100%, and says a markdown parser caps table accuracy at 58% ([15:26](https://www.youtube.com/watch?v=3Ad73Onx3Aw&t=926)). Kepler uses exact lookup over 3.0M tagged filing facts and keeps hybrid search for client files only ([7:35](https://www.youtube.com/watch?v=RdQCY_-SXbA&t=455)).
- **Domain logic.** AlixPartners: Claude Code produced plausible output that missed $800K through one unlinked amendment ([2:57](https://www.youtube.com/watch?v=u7fsyrXsPBE&t=177)). Their fix is domain logic in MCP tools (`get_amendment_chain`) rather than prompt instructions ([9:30](https://www.youtube.com/watch?v=u7fsyrXsPBE&t=570)), and a reconciliation harness that cut a naive $2.9M savings estimate by $1.5M of supplier overlap ([14:35](https://www.youtube.com/watch?v=u7fsyrXsPBE&t=875)). Neo4j compiles SKILL.md into procedure graphs so business rules live in code ([2:57](https://www.youtube.com/watch?v=g_NgCLTibiM&t=177)). Oracle replaces free text-to-SQL with parameterized, pre-validated reports ([8:58](https://www.youtube.com/watch?v=LnQce-eHm8k&t=538)).
- **Keys.** Speakeasy, Keycard and Oracle all land on one identity per agent, no shared service accounts, authorization enforced below the agent (gateway, token service or database engine) so a client cannot route around it ([Oracle 13:19](https://www.youtube.com/watch?v=LnQce-eHm8k&t=799)).
- **Release gates.** NatWest, Langfuse and Upstart all gate on several metrics at once, never on an average, and all keep a human sign-off the agent cannot perform itself. Brainbase writes that prohibition into a one-page kickoff brief before any build ([2:45](https://www.youtube.com/watch?v=4VmugsE-iP0&t=165)).

The cost argument comes along for free: once the procedure is fixed, a cheap model runs it. Neo4j's loan-underwriting demo went from $1.83 per run on Fable alone to $0.02 on Haiku plus procedure ([19:02](https://www.youtube.com/watch?v=g_NgCLTibiM&t=1142)); that is a vendor demo at list price, so treat it as direction, not a benchmark.

## Pillar 7: AI governance for financial institutions

- **Upstart and Coval, voice verification calls in lending** ([Voice AI Where One Wrong Question Breaks the Rules](https://www.youtube.com/watch?v=3osCbSufv8g&t=191)). The regulatory traps are specific: never ask for a medical expense amount, never promise an approval timeline. Judge-human agreement on compliance rose from 33% at launch to 92% by encoding reviewer tribal knowledge into the judge prompt ([8:29](https://www.youtube.com/watch?v=3osCbSufv8g&t=509)); flagged failures route to compliance or engineering by type ([11:51](https://www.youtube.com/watch?v=3osCbSufv8g&t=711)). 29 ad-hoc metrics collapsed to 6.
- **Blackstone and Norm AI** ([Why Waiting for the Next AI Model Can Pay Off](https://www.youtube.com/watch?v=ra8X7_bnujw&t=534)). A deliberate six-month postponement to wait for cheaper capable models, framed as an ROI decision ([8:54](https://www.youtube.com/watch?v=ra8X7_bnujw&t=534)). Sandbox and egress controls for sensitive data ([11:44](https://www.youtube.com/watch?v=ra8X7_bnujw&t=704)), model-skew checks on every foundation-model upgrade ([6:44](https://www.youtube.com/watch?v=ra8X7_bnujw&t=404)).
- **Oracle** ([Your AI Agent Wants Your Data](https://www.youtube.com/watch?v=LnQce-eHm8k&t=1166), vendor). Agents tune performance only against read-only Data Guard replicas, with human approval for plan changes ([12:29](https://www.youtube.com/watch?v=LnQce-eHm8k&t=749)); Entra ID per user, every query logged to an identity ([19:26](https://www.youtube.com/watch?v=LnQce-eHm8k&t=1166)).
- **OpenAI finance workshop** ([Build Your Own Finance Agent Inside ChatGPT](https://www.youtube.com/watch?v=ZJjRgvjhq3M&t=5368), 110 min). Most of it is a product tour. The governance segment is worth 5 minutes: central `config.toml` sandboxing, outbound URL restrictions and workspace boundaries ([1:29:28](https://www.youtube.com/watch?v=ZJjRgvjhq3M&t=5368)), and audit logs linking recommendations back to raw filings ([1:49:37](https://www.youtube.com/watch?v=ZJjRgvjhq3M&t=6577)). Their eval advice: binary pass/fail beats 1-to-5 scales ([1:41:07](https://www.youtube.com/watch?v=ZJjRgvjhq3M&t=6067)).
- **Range** ([Financial Planners With Zero Coding Experience Built Their Own Agents](https://www.youtube.com/watch?v=EaOit1mmR9g&t=418)). An RIA where planners wrote 24 skills under fiduciary review rules; the governance bottleneck moved from building skills to managing their lifecycle ([14:30](https://www.youtube.com/watch?v=EaOit1mmR9g&t=870)). Light, but a good picture of how a small regulated firm adopts.

## Pillar 6: evals as a discipline

- **Netflix, Cameron Wolfe** ([How Netflix Evaluates AI When There's No Right Answer](https://www.youtube.com/watch?v=zLc8zeGeNaY&t=390)). The most rigorous eval talk of the set: power analysis to size the eval set ([6:30](https://www.youtube.com/watch?v=zLc8zeGeNaY&t=390)), judges validated against human inter-annotator agreement as the target ([9:47](https://www.youtube.com/watch?v=zLc8zeGeNaY&t=587)), and error bars as mandatory, with resampling of both model and judge ([12:03](https://www.youtube.com/watch?v=zLc8zeGeNaY&t=723)). Pairs with your Hamel Husain thread.
- **Ramp bookkeeping benchmark, Ryan Stevens** ([How Ramp Tests Its AI Bookkeeping Agent](https://www.youtube.com/watch?v=WJTQBENv0KI&t=163)). 160 accounting tasks built over four to six months before launch on synthetic businesses ([2:43](https://www.youtube.com/watch?v=WJTQBENv0KI&t=163)); pass@k for all-or-nothing work ([9:32](https://www.youtube.com/watch?v=WJTQBENv0KI&t=572)). What did not help: auto-tuned skills, higher reasoning effort. What did: a slimmer 4-skill bundle (+2.8 points) and compressing the Excel skill instructions, which saved $30k a month at no quality loss ([13:36](https://www.youtube.com/watch?v=WJTQBENv0KI&t=816)). They do not gate CI on agent evals because of non-determinism; a scheduled background agent reports regressions to Slack instead ([16:59](https://www.youtube.com/watch?v=WJTQBENv0KI&t=1019)).
- **Brainbase, Domino's pricing pilot** ([41% to 97% Accurate](https://www.youtube.com/watch?v=4VmugsE-iP0&t=402)). Eleven versions with a scope ladder (one item, one app, one city first). Four-tier check: deterministic code, LLM judge, agent judge re-checking live pages, then human trace review ([7:09](https://www.youtube.com/watch?v=4VmugsE-iP0&t=429)). Analyst corrections become benchmark cases automatically, so the client owns the loop without the vendor ([12:25](https://www.youtube.com/watch?v=4VmugsE-iP0&t=745)).
- **Samaya, earnings prediction** ([Can AI Agents Beat Wall Street at Predicting Earnings?](https://www.youtube.com/watch?v=SE6POOEjh4I&t=349)). Useful for its harness, not its headline: a point-in-time gate enforced through the auth token so the agent cannot see the future ([5:49](https://www.youtube.com/watch?v=SE6POOEjh4I&t=349)), and stale data costing 12 to 15 points ([12:39](https://www.youtube.com/watch?v=SE6POOEjh4I&t=759)). The "beats consensus" claim is 1.17 sigma against 1.18, a tie.
- **Warp, Ben Holmes** ([Your AI Agent Is Hiding Its Mistakes From You](https://www.youtube.com/watch?v=pz74r-4dGfQ&t=538)). Grade agent sessions, not just outputs: an `Efficiency.md` rubric with four scored levels ([8:58](https://www.youtube.com/watch?v=pz74r-4dGfQ&t=538)), sampled after merged PRs, failures clustered by an agent that opens skill-fix PRs ([13:20](https://www.youtube.com/watch?v=pz74r-4dGfQ&t=800)). Also Pillar 1.

## Pillar 3: strategy, models and economics

- **Ramp, smaller model** ([Our Smaller Model Beat Opus on Finance Tasks](https://www.youtube.com/watch?v=T-42NrYjwxM&t=307)). A frontier orchestrator plans; a post-trained 35B mixture-of-experts model answers spreadsheet questions at Opus 4.6 parity and Haiku 4.5 latency ([5:25](https://www.youtube.com/watch?v=T-42NrYjwxM&t=325)). The same small-model move you noted. Their router cut costs 40% ([11:25](https://www.youtube.com/watch?v=T-42NrYjwxM&t=685)), and spend is tagged per team and accounting category.
- **Apollo, Adam Nahari** ([Your AI Pilots Won't Move the P&L](https://www.youtube.com/watch?v=vUJDyDnqT4c&t=99)). 88% adoption, 6% with material EBIT impact ([1:39](https://www.youtube.com/watch?v=vUJDyDnqT4c&t=99)); change management outweighs engineering 5 to 1 ([2:51](https://www.youtube.com/watch?v=vUJDyDnqT4c&t=171)). The gate discipline is directly reusable: kill metrics set before the pilot, a 90-day time gate, a 50% frontline adoption gate ([12:46](https://www.youtube.com/watch?v=vUJDyDnqT4c&t=766)).
- **McKinsey** ([What the Top Companies Do Differently With AI Coding](https://www.youtube.com/watch?v=21tuT-e4NVo&t=694)). Pods of 2 to 4 replacing squads of 8 to 10, with Definer, Builder and Agent Ops roles ([7:15](https://www.youtube.com/watch?v=21tuT-e4NVo&t=435)). The quality slide is the sober part: duplicated code +81%, refactoring down from 21% to 3.8%, security pass rate flat at 55% ([11:34](https://www.youtube.com/watch?v=21tuT-e4NVo&t=694)). About 56% of defects start in ambiguous requirements ([13:57](https://www.youtube.com/watch?v=21tuT-e4NVo&t=837)), which is your shift-left thesis in their numbers.
- **Uber** ([AI Writes Code Faster Than Uber Can Merge It](https://www.youtube.com/watch?v=hX140a5klW0&t=360)). AI pull requests pushed merge volume up 44% in two months and p95 land time from 30 minutes to 3 to 4 hours ([6:00](https://www.youtube.com/watch?v=hX140a5klW0&t=360)). After queue redesign: 233% more lands, p95 back at 30 minutes, open-sourced as `uber/submitqueue` ([24:00](https://www.youtube.com/watch?v=hX140a5klW0&t=1440)). The point: the bottleneck moved from writing to integrating.
- **Neo4j and Blitzy, mainframe** ([How We Mapped a 35-Million-Line Mainframe in 4 Days](https://www.youtube.com/watch?v=oWqsBuhRRZ4&t=930), vendor). Code as a knowledge graph with multi-hop context. Claims: 35M lines mapped in 4 days versus 8 to 12 months ([15:30](https://www.youtube.com/watch?v=oWqsBuhRRZ4&t=930)), 4,022 generated tests at 100% behavioral parity ([14:56](https://www.youtube.com/watch?v=oWqsBuhRRZ4&t=896)), a sub-cent rounding difference from Java caught ([13:40](https://www.youtube.com/watch?v=oWqsBuhRRZ4&t=820)), and DORA dependency mapping as the next use ([18:17](https://www.youtube.com/watch?v=oWqsBuhRRZ4&t=1097)).

## Pillars 1 and 6: Lance Martin, Anthropic, on prompt debt and hillclimbing

**[Your Old Prompts Are Making Claude Worse - Lance Martin, Anthropic](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=94)** (64 min). The one talk here from a model lab about your own daily craft, and it fits the theme: stop asking the model to do in prose what the model or code now does better.

- **Prompt debt is measurable.** Old instructions written for weaker models now cost money for nothing: "verify facts before proceeding" measured +40% cost with no accuracy gain, and thoughtfulness boosters, manual scratchpads, anti-laziness lines and stacked few-shot examples are the same kind of dead weight ([1:34](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=94)). Contradictory instructions can freeze frontier models ([2:27](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=147)). The audit is now two Claude Code commands that share one engine: `/checkup prompt-audit` (also `/doctor prompt-audit`) for your Claude Code setup (CLAUDE.md files, skills, agents, commands) and `/claude-api prompt-audit` for application code that calls the Anthropic API ([2:50](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=170)).
- **Prompt caching is mostly about order.** One changed byte early in the prompt breaks the cache. Put global instructions and tools first, then project files such as CLAUDE.md, then per-session state, then the turns ([7:00](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=420)). Reordering alone cut cost 72.9% on tau2-bench retail with no score loss ([7:42](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=462)).
- **Eval design, from the lab.** Keep headroom so the eval does not saturate, and keep run-to-run variance low enough to hillclimb on ([10:11](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=611)). Binary grades over 1-to-10 scales ([30:41](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=1841)), pairwise comparison for subjective output, and a rubric-agreement check across several models ([31:06](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=1866)). This matches the OpenAI and Netflix advice above, from a third independent source.
- **Autonomous hillclimbing with overfitting guards.** A fixed 50/50 split with hidden test cases; each round proposes one isolated change, and a change that regresses on test is reverted ([13:48](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=828)). On their own Claude API skill this took pass rate from 66.1% to 87.9% in 34 rounds ([15:01](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=901)), and a second run cut cost about 90% at equal or better accuracy ([17:40](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=1060)).
- **Long-running agents: separate the brain from the hands.** Reasoning harness and sandbox run separately, an append-only session log allows full recovery after a crash, and credentials sit in a vault the sandbox never sees ([20:36](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=1236)). Guardrails cap turns, duration and spend ([22:09](https://www.youtube.com/watch?v=ys6YQ2yfBpM&t=1329)). That is your Pillar 2 server-side thesis in Anthropic's own words, though delivered as a pitch for Managed Agents.

**For you:** run `/checkup prompt-audit` from a clean git tree in each repo, starting with video-intel, before your next /dark-factory run; your prompts carry instructions older than the current models. Review the diff before committing: keep the strong wording on any rule that exists because it was once ignored. The hillclimb loop (one change per round, hidden test split, revert on regression) is a direct template for the eval harness work.

## Why it matters to you

- **AI governance reviews.** The five-question test from "The one thing" (numbers, retrieval, domain logic, keys, release gate) can become a review's spine, each question backed by a timestamped talk. Fieldguide's state-based authorization and Speakeasy's "owner deprovisioned, agent dies" are two controls most FI teams have not thought about yet, so they make good review questions.
- **Speed versus quality.** Apollo's 88% / 6% gap and McKinsey's quality-decline slide are the sober counterweight to pure-speed claims.
- **The determinism argument.** Neo4j's graph engineering and Citadel's "models propose, code or humans decide" give you two named sources for the same claim.
- **Grounded retrieval and this corpus.** Kepler's correct / correct-with-units / refuse citation filter is a pattern your grounded-RAG work could copy as-is; the honest 12.8% untracked residual is the right way to report it.
- **Your own agent factory.** Warp's session-efficiency rubric and Ramp's "no CI gate, scheduled regression agent" both bear on how /dark-factory and the eval harness should report quality.

## Signal vs noise

- **Vendor pitches with real substance:** Speakeasy, Keycard and Temporal, Langfuse, Oracle, Mixedbread, Neo4j, Blitzy. Keep the mechanisms, discount the numbers.
- **Skim or skip:** "If Your AI Scores 100%, Your Eval Is Wrong" (eslams.com) is thin: an exam analogy, the much-quoted MIT 95% figure, and a pitch for a games benchmark. The one usable line is to calibrate difficulty so scores land at 85 to 90% ([13:05](https://www.youtube.com/watch?v=7WUzHIF32vA&t=785)).
- **OpenAI workshop:** 110 minutes, mostly a product tour of ChatGPT finance plugins. Watch only the two segments linked above.
- **Samaya:** watch for the point-in-time harness design; ignore the "beats Wall Street" framing.

## Open questions and what I could not verify

- **Kepler's "frontier models fail one in three tasks"** comes from an October 2024 leaderboard, so it is two model generations old. The 46% / 19.8% / 90.8% extraction figures are on screen and cited to Kepler's own eval.
- **Ramp's "Opus models performed best"** in the bookkeeping talk refers to a chart of older models (Claude 3 Opus, GPT-4); the speaker says reruns still show it, but no current numbers are shown.
- **Ramp's specialist model name differs:** the slide says Qwen2.5-35B-A3B, the speaker says "Qwen 3.5, 35 billion". I used neither name in the text above.
- **Speakeasy's 82% / 68% / $5M** statistics are stated without a source in the talk.
- **Blitzy's 35M lines in 4 days** is a customer outcome presented by the vendor; no independent source.
- **McKinsey's code-quality figures** are on a slide; I did not check the underlying studies.
- **The Neo4j $1.83 to $0.02** figure is a single demo run at list price.
- I spot-checked about a dozen load-bearing figures against the transcripts (Kepler, Langfuse, Speakeasy, McKinsey, Upstart, Citadel, Neo4j, Fieldguide, Ramp, Blitzy). They matched what the speakers or slides said, with two exceptions: the Ramp model-name mismatch above, and Speakeasy's 68% figure, which I could not find in the transcript text. McKinsey's +81% and 3.8% figures were not checked. Everything else comes from the mind maps.

## What happened while making this

- **Scope:** your 12 picks plus 14 I shortlisted against Pillars 6 and 7, chosen from the channel's new uploads, plus Lance Martin's talk, added on your request.
- **Pipeline:** two batches (34 and 43 minutes) plus one single talk (7 minutes). 27 of 27 transcripts, mind maps and concepts landed with no quality flags, no access errors and no confabulation-guard hits. One transient Gemini server error was retried automatically. The three long talks (Keycard 53 min, Anthropic 64 min, OpenAI 110 min) were transcribed in chunks without loss.
- **Spend:** about CA$7.40 in Gemini (transcripts plus two cached concepts batches, and one uncached concepts call for the single talk), plus a few cents of Voyage embedding.
- **Lance Martin figures checked:** +40% cost, 72.9%, 66.1% to 87.9% and the 90% cost cut all match the slides or speaker.
- **Not done:** the corpus catalog page was not rebuilt (separate per-video commands do not trigger it).
- **Metadata gap:** these metas carry no `duration_seconds`, because the manual `--url` path does not persist it. Runtimes above come from the channel listing.
