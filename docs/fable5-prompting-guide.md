# Fable 5 Prompting Guide (Daniel's working copy)

A living, grounded guide to prompting Claude Fable 5. Each rule is tagged by source confidence:

- **[OFFICIAL]** = stated in Anthropic's official docs (highest confidence).
- **[CREATOR]** = an empirical claim or benchmark from a creator video in the corpus. Useful but not vendor-verified. Treat specific numbers as anecdotal.
- **[DANIEL]** = a note about how this maps to my own workflow (monologue to spec to product).

Primary source: Anthropic, "Prompting Claude Fable 5" (https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5). Creator sources are listed in the Source Map at the bottom.

---

## 0. The one-paragraph mental model

Fable 5 is built for long-horizon, self-verifying, agentic work: tasks that take a human hours, days, or weeks, run inside a harness, and need the model to plan, call tools, read results, validate its own output, and correct course with little supervision. **[OFFICIAL]** It is slow to first token and can run for many minutes on a hard task at high effort. The dominant failure mode is treating it like a chat model: drip-feeding context turn by turn, over-instructing it with enumerated rules, and giving it nothing concrete to verify against. Almost every "you are using Fable wrong" lesson reduces to: front-load the whole spec, steer with one short instruction instead of ten, route cheap work elsewhere, and give it a way to check its own work.

---

## 1. Model routing is the meta-skill (use Fable only when it pays)

The highest-leverage habit, and it is NOT in the official prompting doc: **match the model to the job.** "Opus for drafting, Fable for heavy lifting." **[CREATOR: Kieran Flanagan]**

- Fable's output tokens cost roughly 2x Opus (about $50 per million). **[CREATOR]** It only pays off for "big input, autonomous execution, finished output": work that would take a skilled person half a day across many sources. **[CREATOR: Kieran Flanagan]**
- For routine drafting, quick edits, and interactive back-and-forth, stay on Opus 4.8 or Sonnet. **[CREATOR]**
- Inside a single Fable session you can hand off: plan with Fable at high effort, write a `SPEC.md`, then switch to a cheaper model fleet for the bulk execution via the `/model` command. **[CREATOR: Mark Kashef, Greg Isenberg]**
- Krieger's framing: using Fable for trivial questions is "a rocket launcher for an NBA trivia question." Anthropic uses sticky model selection per surface so the heavy model is reserved for heavy surfaces. **[CREATOR: Mike Krieger / Anthropic]**

**[DANIEL]** This is the discipline I most need to build: my instinct is to reach for the most powerful model for everything. The routing rule is: discovery and drafting on Opus, the autonomous build on Fable.

---

## 2. Effort is the primary dial

Effort trades intelligence against latency and cost. **[OFFICIAL]**

- Default to **high**. Use **xhigh** for the most capability-sensitive work, **medium** or **low** for routine work. **[OFFICIAL]**
- Lower-effort Fable still performs well, and often exceeds xhigh on prior models. Reduce effort if a task completes but takes longer than necessary, or if you want a quicker, more interactive feel. **[OFFICIAL]**
- At higher effort Fable can over-deliberate and over-tidy. Pair high effort with an explicit anti-over-engineering instruction (see sec. 6). **[OFFICIAL]**

Creator benchmark claims (treat as anecdotal, directionally useful, not vendor-confirmed):

- "Low effort is the alpha": Fable Low reportedly matches or beats Opus 4.8 at its highest setting on routine work; Fable Medium exceeds Opus Max; Fable Max is a large jump over prior generations. **[CREATOR: Mark Kashef, Greg Isenberg]**
- Practical effort heuristic from the creators: **Max** for high-stakes planning and final shipping, **X-High** for complex multi-subagent orchestration, **Medium** for cost-effective execution, **Low** for competent baseline tasks. **[CREATOR: Mark Kashef]**

**[DANIEL]** The honest takeaway under the marketing: do not default to Max. Plan/ship at high or max; run the middle of the job at low or medium; let cheaper models do the grunt work.

---

## 3. Prompt structure: front-load the whole spec

- Put the entire task, context, output format, and hard constraints in the **first message.** Fable plans multi-step before acting, so drip-feeding starves the plan. **[OFFICIAL]**
- State the role, one line of context, the task, the output format, and the hard constraints. **[OFFICIAL]**
- Give the reason, not just the request. Context lets Fable connect the task to the right information instead of guessing intent: "I'm working on [larger task] for [who]. They need [what the output enables]. With that in mind: [request]." **[OFFICIAL]**
- Steer with ONE short instruction, not a ten-item style guide. Instruction-following is strong enough that "lead with the outcome, be concise" replaces enumerating each behavior. Over-prescribing actually degrades Fable's output. **[OFFICIAL]**

There are two reusable prompt shapes. Pick by task type (full templates in sec. 8):

- **Single-shot, front-loaded** for bounded-but-messy work (one dense paragraph dumping all real-world context, naming tools and intermediate artifacts, ending with a persistence stop-condition).
- **Phased, with a red-team loop** for open-ended or strategic work (explicit phases, each writing a file, with a self-critique gate between them).

---

## 4. Give it something concrete to verify against

Self-verification is Fable's signature strength, but it needs an anchor. **[OFFICIAL]**

- Provide a schema, a reference output, acceptance criteria, or tests. Self-verification works best when there is something concrete to check against. **[OFFICIAL]**
- Ask it to critique before finalizing: list the ways its answer could be wrong, address them, then produce the final result. **[OFFICIAL]**
- For multi-file or live-system changes, fresh-context verifier subagents outperform self-critique: "Establish a method for checking your own work at an interval as you build. Run it every [interval], verifying with subagents against the specification." **[OFFICIAL]**
- Self-verification is NOT a substitute for deterministic verification (real tests, real type-checks) on changes that touch many files or production. **[CREATOR: lushbinary, consistent with official]**
- Build a visual verification loop for interface work: have it capture screenshots or video and check the result against the intent. Anthropic uses screenshot galleries and ffmpeg to catch animation jank. **[CREATOR: Mike Krieger / Anthropic; every.to prompt library]**

---

## 5. Ground progress claims on long runs

On long autonomous runs Fable can fabricate status. Anchor every claim to a tool result. **[OFFICIAL]**

> Before reporting progress, audit each claim against a tool result from this session. Only report work you can point to evidence for; if something is not yet verified, say so explicitly. Report outcomes faithfully: if tests fail, say so with the output; if a step was skipped, say that; when something is done and verified, state it plainly without hedging.

In Anthropic's testing this nearly eliminated fabricated status reports even on tasks designed to elicit them. **[OFFICIAL]**

**[DANIEL]** This is the same failure I hit live today: the Cole Medin livestream produced a confident, fabricated transcript. The lesson generalizes: always demand evidence-backed claims, and spot-check the evidence.

---

## 6. State the boundaries (stop it from over-acting)

Fable can take unrequested actions (drafting an email nobody asked for, making defensive git backups, refactoring around a one-line fix). Define explicit constraints. **[OFFICIAL]**

Assessment-only guard:

> When the user is describing a problem, asking a question, or thinking out loud rather than requesting a change, the deliverable is your assessment. Report your findings and stop. Don't apply a fix until they ask for one.

Anti-over-engineering guard (pair with high effort):

> Don't add features, refactor, or introduce abstractions beyond what the task requires. Do the simplest thing that works. Don't add error handling or validation for scenarios that cannot happen. Only validate at system boundaries (user input, external APIs).

Checkpoint guard (so it pauses only where it must):

> Pause for the user only when the work genuinely requires them: a destructive or irreversible action, a real scope change, or input that only they can provide. If you hit one of these, ask and end the turn, rather than ending on a promise.

---

## 7. Refusal and fallback gotchas (the silent model switch)

Fable runs safety classifiers covering offensive cybersecurity, biology/life-sciences, model-thinking extraction, and frontier-AI development. On a trip it returns `stop_reason: "refusal"` and, by default, silently falls back to Opus 4.8 mid-conversation. **[OFFICIAL + support.claude.com]**

- The classifiers are intentionally broad and can block benign security or life-sciences work. **[OFFICIAL]**
- **Do not ask Fable to echo, transcribe, or explain its own internal reasoning as response text.** That can trip the `reasoning_extraction` classifier and bump you to Opus without you noticing, degrading output. If you need reasoning visibility, read the structured `thinking` blocks from adaptive thinking instead. **[OFFICIAL]**
- Billing nuance: an input-level block charges only Opus rates; a mid-stream block charges Fable rates for the tokens already produced. **[support.claude.com]**
- You can disable automatic switching in Settings > Capabilities. **[support.claude.com]**

---

## 8. Two reusable templates

### 8a. Single-shot, front-loaded (bounded-but-messy work)

Real example, lightly anonymized, from trq212's Fable video-edit prompt (the "one prompt kicks off the whole edit" pattern). Note how it dumps every real-world wrinkle, names the tools, demands an intermediate artifact (a JSON of clip ranges), and ends with a persistence directive.

```
I'm processing the recordings in @Fable-Full-Recording/. The script is in
@Fable-Full-Recording/fable5script.md. Run the ElevenLabs transcription service on them,
then stitch the best shots into one final clip.

Notes: there are multiple takes per scene; the best takes are usually the ones at the end
with the fewest "ums," but not always. I re-shot the first scene at the end. Organize
everything by timeline. For a few I start by saying "Hey [name]" to warm up the sentence,
cut that out.

Create a JSON file per video showing, per scene, each clip we're using and its time ranges.
Then create the final cut using ffmpeg. Orchestrate this all using workflows.

/goal don't stop until you have a final video
```

Skeleton to reuse:

```
[One paragraph of full context: what I have, where it is, what I want out.]
[All the messy real-world caveats, stated plainly, including the exceptions.]
[Name the tools and any intermediate artifact you want produced for auditability.]
[Output format / where to save it.]
/goal <single sentence completion criterion - don't stop until X>
```

### 8b. Phased, with a red-team loop (open-ended / strategic work)

Structure abstracted from Kieran Flanagan's "Positioning Strategy Master Prompt" (a 5-phase evidence-backed agent prompt). The load-bearing ideas: each phase writes a file, every claim traces to a quoted source, and there is an explicit adversarial gate before the final synthesis.

```
Role & objective: You are a [role]. Build [deliverable] for [subject], for [audience/segment].
Work through the phases below in order. Each phase produces a saved file. Every claim in every
file must trace to a verbatim quote with a source. Never paraphrase when you can quote. Label
anything synthetic as [SAMPLE DATA].

Phase 0 - Load internal data: read [my own export: call transcripts, chat logs, notes].
Phase 1 - External context: gather [competitor / domain] evidence, quoted.
Phase 2 - Internal context: audit [my own current state], ending in a "say vs hear" gap table.
Phase 3 - Principles WITH RED-TEAM LOOP: synthesize 5-7 principles from the three-way overlap of
  (what the evidence shows people want) x (where others are weak) x (where I am provably strong).
  Then attack each: could a competitor claim this tomorrow with a copy change? Is the evidence
  cherry-picked? Does it contradict my own data? Kill or rewrite any that fail. Repeat the
  attack-revise cycle until every survivor passes true/different/desired, or 3 iterations. Show
  the kills; don't hide them.
Phase 4 - Final framework: build from survivors only. Flag anything needing a real decision rather
  than a copy fix.

Rules: quote verbatim, link everything, never fabricate a quote (if a source can't be accessed,
say so and proceed). End with a 5-bullet executive summary.
To reuse: swap the subject/segment/inputs and point Phase 0 at a different data export.
```

**[DANIEL]** 8b is the backbone of the monologue-to-spec converter. The "interview before the build / scope the vague mess into a spec" pattern Greg Isenberg demonstrates (personas that push back on vague answers, spec docs that name three ways the thing could fail) is the same machine pointed at my own rambling instead of at positioning.

---

## 9. Cost mechanics (keep the bill sane)

- Output tokens cost about 5x input tokens, so request only the artifacts you need, not verbose reasoning traces. **[CREATOR: lushbinary]**
- Use a stable cached prefix: put system instructions and stable context at the front to capture the prompt-cache discount on repeated calls. **[CREATOR: lushbinary; consistent with official caching docs]**
- Rough order of magnitude: a task with 200K input + 50K output is on the order of a few dollars before caching. **[CREATOR: lushbinary]**
- Feed raw, messy data (real logs, real transcripts), not a sanitized summary. Fable's reasoning advantage shows up with complexity; pre-summarizing throws away the signal it is best at. **[CREATOR: buildfastwithai]**

---

## 10. Where Fable shines (pick tasks at the top of your difficulty range)

[OFFICIAL] capability jumps over Opus 4.8: long-horizon autonomy, first-shot correctness on complex well-specified problems, dense-technical-image vision, enterprise document/spreadsheet/slide work, code review and debugging recall, navigating ambiguity, and dispatching parallel subagents. Anthropic's explicit advice: pick a task harder than you'd give a prior model, and have Fable scope it, ask clarifying questions, and execute.

High-value task families seen across the corpus and articles:

- Large code migrations with cross-file dependencies and test verification at each step. **[CREATOR: buildfastwithai]**
- Overnight delegation of multi-hour work with blocker handling. **[every.to prompt library; Krieger]**
- Multi-document synthesis: agreements, contradictions, and the strongest unique claim per source. **[CREATOR: buildfastwithai]**
- Turn scattered feedback into batched, prioritized changes. **[every.to prompt library; EveryInc Software Factory]**
- Build a first version from a detailed spec. **[every.to prompt library]**
- Adversarial business modeling ("hire it to kill your company"), synthetic focus groups from hundreds of real reviews, contract red-teaming. **[CREATOR: Greg Isenberg]**

---

## 11. Combining /lfg (Compound Engineering) with Fable

`/lfg` is a model-agnostic orchestration scaffold: a strict 9-step pipeline (plan -> work -> code-review -> fix -> residual-handoff -> browser-test -> commit/PR -> watch-CI-until-green -> DONE) with hard gates between steps. It contains no intelligence of its own; it sequences the CE sub-skills. **[/lfg SKILL.md]**

Combining with Fable = run `/lfg` while Fable 5 is the active model, so every step inherits Fable's long-horizon autonomy and self-verification. The fit with my workflow:

```
chaotic monologue --> [converter] --> clean spec / GitHub issue --> /lfg (on Fable) --> PR
   voice note                          the "what I really need"      autonomous build
```

So the converter's output (a clean spec) is literally `/lfg`'s input. They are complementary: the converter replaces my slow "ramble -> discover -> design -> issue" front-end; `/lfg` + Fable replaces the "build the product" back-end. EveryInc's "AI Software Factory" video is this exact loop running in production (Slack feedback -> subagents -> YAML/markdown issues -> batched fixes -> green-CI auto-merge, in 2-4 hour autonomous sessions). **[CREATOR: EveryInc]**

---

## 12. Cross-creator nugget findings (added 2026-06-12)

Generated by `nugget "how to prompt Fable 5..."` over the indexed corpus (11 creators, timestamped + attributed). These refine the sections above; caveat that the synthesis blended some Opus 4.7/4.8 chunks with Fable 5, and provider-tax numbers are creator benchmarks, not vendor-confirmed.

- **Effort divergence is domain-dependent (refines sec. 2 and 3).** "Low effort is the alpha" (Greg Isenberg, citing Morgan Linton) and "xhigh is the baseline" (Chase Lean / Anthropic Claude Code defaults) are BOTH right, for different jobs. Isenberg optimizes high-volume research and asset loops where cost dominates; the engineers optimize codebases where one reasoning error breaks the build. Pick effort by blast-radius-of-an-error, not by a global rule. **[CREATOR]**
- **The succinctness paradox (refines sec. 2).** Higher effort spends more reasoning tokens but often emits fewer output tokens, because the model finds the shortest correct path. So low effort can be MORE expensive in long context: wordier, less accurate output that needs follow-up turns. "Low is the alpha" is not universal. **[CREATOR: samwitteveenai, engineerprompt]**
- **Inference Provider Tax (new).** Fable/Opus accessed via Azure or AWS Bedrock reportedly lose ~10-12% reasoning accuracy vs official hosting (AIME25 ~92% to ~80%). If reasoning quality matters, check which endpoint you are calling. **[CREATOR: engineerprompt, anecdotal benchmark]**
- **Mid-task steering (new, useful for sequenced prompts).** The Messages API now allows system entries inside the message array, so a running agent can be corrected without restarting its context. Relevant to the phased-template workflow in sec. 8b. **[CREATOR: chase_h_ai]**
- **Push-back as a trust signal (corroborates sec. 6 official "judgment to push back").** Users report higher trust when Fable refuses or flags that a requested module "doesn't make sense." Insubordination in service of logic is a senior-collaborator feature, not a defect. **[CREATOR: everyinc]**
- **Plan-with-Fable, build-with-cheap, confirmed (corroborates sec. 1).** Fable at Max writes a SPEC.md, then `/model` switches to a cheaper/lower-effort executor. Manager-worker hierarchy within one session. **[CREATOR: mark_kashef]**
- **Loops, not prompts (the "gardening" model) - the meta-framing that ties the guide together.** Every's Dan Shipper ("The Moral of Fable," 2026-06-12): the people getting Fable's full value are operating at a higher tier of AI use where they delegate whole projects, let agents work asynchronously, review the results, and feed what they learn into the next run. They write loops, not prompts. The gardening metaphor: you don't grow the plant, you set and maintain the conditions (water, weed, prune, stake) and the plant grows. For knowledge work the loop is: collect inputs (feedback) -> turn them into an actionable plan -> AI executes -> review and merge -> integrate the lessons, where **each stage's output becomes the next stage's input** and **"the process becomes the product."** Your job in the loop reduces to three decisions: *what goes in, what it can reach, and when it's done.* Proof point: Cora's Kieran Klaassen set a 24-hour bug-fix goal; median time from bug report to merged fix is now ~5 hours. Swap bug-fixing for copy edits or sales forecasts and the shape holds for any knowledge work. Cost caveat that reinforces sec. 1 and 9: Fable is token-hungry (it will auto-spawn dozens of subagents to check its own work), so running these loops continuously takes real capital - another reason to route only loop-worthy work to Fable. **[CREATOR: Every / Dan Shipper]**
- **The "three decisions" loop checklist (actionable distillation of the above).** Before handing Fable a loop, specify: (1) INPUTS - what raw material goes in each turn (feedback, tickets, transcripts, a content export); (2) REACH - what it may touch and which tools/permissions it has (scope and blast radius); (3) DONE - the explicit completion criterion for one turn. Then review the turn's output and feed the lesson back as the next turn's input. This is the operational form of guide sec. 3 (front-load), sec. 4 (verify), sec. 5 (ground claims), and sec. 6 (boundaries) combined into one repeatable cycle. **[DANIEL, synthesized from Every]**

## Source Map

Official / vendor:
- Anthropic, Prompting Claude Fable 5 (primary): https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-fable-5
- Anthropic support, Why Claude switched models with Fable 5: https://support.claude.com/en/articles/15363606

Articles:
- Kieran Flanagan, 3 marketing jobs worth paying 2x for (model routing): https://www.kieranflanagan.io/p/3-marketing-jobs-worth-paying-2x
- Kieran Flanagan, Positioning Strategy Master Prompt (PDF, phased template source)
- every.to, Claude Fable 5 prompt library (8 named prompts): https://every.to/p/claude-fable-5-prompt-library
- every.to, Dan Shipper, The Moral of Fable (loops/gardening workflow framing; full text): https://every.to/chain-of-thought/the-moral-of-fable
- lushbinary, Fable 5 prompting guide (effort/cost/caching): https://lushbinary.com/blog/claude-fable-5-prompting-guide/
- buildfastwithai, 25 Fable 5 prompts to test every capability: https://www.buildfastwithai.com/blogs/claude-fable-5-prompts-test-capabilitie
- trq212 (X), one-prompt video edit (single-shot template source)
- /lfg SKILL.md: https://github.com/EveryInc/compound-engineering-plugin/blob/main/plugins/compound-engineering/skills/lfg/SKILL.md

Corpus videos (in G:/My Drive/video-intel, queryable via `search --vector` and `nugget`):
- gregisenberg / You are using Claude Fable 5 wrong (advanced use-cases, vague-mess-to-spec)
- mark_kashef / Don't Use Claude Fable 5 Until You See This (effort tiers, plan-then-handoff)
- everyinc / How Anthropic Uses Claude Fable 5 With Mike Krieger (first-party patterns)
- everyinc / How I Built an AI Software Factory With Fable 5 (compound-engineering loop)
- claude / The prompting playbook (eval-driven prompt hygiene, XML structure, generate-evaluate-repair)
- thenextnewthingai / Fable 5 cloning $10B apps (demo)
- brockmesarich / Fable 5 changed how we get customers (marketing angle)

---

*Tags in this doc: [OFFICIAL] vendor-stated, [CREATOR] anecdotal creator claim, [DANIEL] personal workflow note. Update this file rather than starting a new one; delete claims that turn out wrong.*
