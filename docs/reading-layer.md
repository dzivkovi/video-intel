# The reading layer: why every summary here has the same shape

This repository turns a firehose (YouTube channels, podcasts, newsletters, a community's talk pages) into artifacts one person can actually read. The pipeline half of that story is documented elsewhere (`docs/search-internals.md`, `docs/topics-layer.md`, `docs/intelligence-layer.md`). This page is about the other half: the documents a reader opens, why they look the way they do, and how to bring your own version of them without forking the code. The sources feeding that reading layer are no longer only video: a newsletter's issues and the talk pages behind them go through the same templates, as the worked example below shows.

If you open this repo cold, the corpus itself is a pile: thousands of transcripts, mind maps and concept files. Nobody reads a pile. The reading layer is the set of rules that decides what gets written on top of the pile so that a busy, easily-interrupted reader gets the right thing on the first screen.

## The problem being solved

A summary that merely shortens a source does not reduce the reader's work; it moves it. The reader still has to find where the important part is, judge whether it matters to them, and infer what to do about it. For a busy, frequently interrupted reader those three steps are exactly where reading fails: the eye slides past the one paragraph that mattered, and the fact that was found is never acted on.

So the design goal is not "shorter". It is "nothing has to be inferred, and nothing important sits below the fold".

## Three properties every reading-layer document must have

1. **A fixed template.** Every document of one kind has the same sections in the same order. After the second one, the reader never searches: the nuggets are in section 4, the caveats are at the end, the repo table is where it always is. The cost of "where is the thing I need" goes to zero.
2. **A ranking.** The most important item comes first in every list, and the first screen carries the conclusion. A reader who stops after two paragraphs still leaves with the right thing. Chronology is allowed only inside a section that is explicitly about time (a chapter table of a recording).
3. **A "so what" on every item.** Each item states its implication explicitly. In the personalized shapes (briefing, topic digest) it is written against a stated reader profile; in a distillation it is the source's own implication, bolded, because a distillation has to stay reusable by any reader. Inference is the step an interrupted reader skips, so the document does it for them.

Two supporting rules make the three properties trustworthy rather than merely tidy:

- **Traceable, never invented.** Every claim links to its source: a timestamp into a recording (`&t=<seconds>` is data, not decoration), a talk page, a repository. When a source is vague the document says "the source is vague" instead of filling the gap. A reader who can click through to the primary source can trust the paraphrase.
- **Popularity is not corroboration.** Ten mentions of one idea in one community is one signal of attention, not ten signals of quality. The reading layer ranks by mechanism, number and named artifact, never by how loud the chatter is.

## Three document shapes, not one

Different questions need different shapes. This repo uses three, and it matters not to blur them, because a template that serves one question badly serves the others.

| Shape | Input | The question it answers | Keyed by | Produced by |
|---|---|---|---|---|
| **Briefing** | Your own corpus over a time window | What happened since I last looked, and what matters to me | `video_ids`, recency, the reader profile | `briefings --unseen`, the scan's headline digest, hand-curated catch-ups |
| **CliffNotes** (distillation) | One long source: a 1 to 5 hour talk, podcast or meeting | What did this conversation actually establish, and which minutes earn a listen | Timestamps into one recording | `prompts/cliffnotes-distiller.md` over a transcript |
| **Topic digest** | Many short sources about one topic: demo pages, abstracts, release notes | What is this community converging on here, what is real, what should I clone | Cluster, a 1 to 5 signal score, repository links | `prompts/topic-digest.md` over a bundle of sources |

A briefing is a front page. CliffNotes are study notes for one book. A topic digest is a landscape scan. All three share the three properties above; they differ in what the ranking is over and what the "so what" is anchored to.

The `nugget` command sits between them: a cross-creator synthesis over the corpus, grounded in retrieved excerpts. It follows the same rules (ranked, cited, "so what" per claim) and is documented in `docs/adr/ADR-0018-nugget-cli-cross-creator-synthesis.md`.

## The template is the asset

The words in any one summary are disposable; next month's sources produce different words. What carries value from run to run is the recipe that forces the three properties onto every output: the section list, and rules such as "bold the implication, not the topic", "a nugget must flip an expectation or name a mechanism", "cut theme prose before cutting chapters, nuggets or quotes".

That recipe is text, so it is a file, and it is versioned like code:

- **Prompts are files.** Every template lives in `prompts/` as a self-contained markdown file. No hidden prefix assembly, no prompt built up inside code. The reading-layer templates (`topic-digest.md`, `cliffnotes-distiller.md`) carry a version number in their heading; the older pipeline prompts predate that convention.
- **Templates are edited deliberately, not improvised per session.** A template improvised inside one conversation drifts the next time the job runs. When an improvised template turns out to be good, the next step is to write it down as a versioned file, not to keep re-deriving it.
- **Change is recorded.** A template edit that changes what readers get is a version bump with a one-line changelog at the top of the file, the same discipline as `tests/evals/golden_dataset.yaml`.

## Bring your own template: precedence, not dependency

The templates shipped in `prompts/` are deliberately plain defaults. An operator usually has a sharper, more opinionated version that encodes their own reader profile and taste, and that version does not belong in a public repository.

The repo therefore supports a precedence rule rather than a hidden dependency:

1. directories listed under `prompt_dirs:` in `config.yaml`, in order;
2. directories in the `VIDEO_INTEL_PROMPT_DIR` environment variable;
3. the bundled `prompts/` directory.

The first directory holding `<name>.md` wins; when an override directory is configured, one log line says which directory supplied the prompt, or that none matched and the bundled default was used. A private `prompts/topic-digest.md` on your machine overrides the shipped one under the same name; a fresh clone without any override still works, with the defaults. The public code never reads a private file by fixed path, the private file is never required, and the outputs of a private template still follow the same three properties because the shape is shared.

This is the same "checker must use the writer's path" discipline the rest of the repo follows: `resolve_prompt_path` is the one place a prompt name becomes a file, the loader and the `scan --dry-run` preflight both go through it, and a test pins that they agree.

## How the reader profile enters

The "so what" lines are only as good as the profile they are written against. The profile is two files in the corpus, never in the repo: `_briefings/profile.yaml` (machine-scored interests, used to rank `briefings --unseen` and the headline digest) and `_briefings/audience.md` (hand-written prose: standing interests, current projects, what counts as signal, what counts as noise, delivery preferences). Templates reference the profile as an input; they do not contain it. `profile show` prints where both files live; `profile init` scaffolds them once and never overwrites them.

## Worked example: a community, end to end

The 2026 AI Tinkerers pass is the reference run for the topic-digest shape, and a good illustration of the whole layer:

- 84 newsletter emails were reduced to 47 with content; 380 demos from 80 chapters were extracted to JSON by cheap models against a fixed schema.
- Each demo was classified into a 14-cluster taxonomy and scored 1 to 5 against the reader profile. The taxonomy and the rubric were written once, by the orchestrator, and the classifiers were forbidden to invent clusters.
- With the operator signed in, the members-only talk pages were harvested so every demo carried the speaker's own description, links and stack, not only the newsletter's paraphrase.
- One topic digest per cluster was written from a fixed template, plus one for the operator's home chapter. Each ends with questions written against the operator's own repositories.
- One synthesis chapter on top: ten convergences with an action each, a section of things the reader did not know they were missing, a curated repository list, and a watch list. Delivered as markdown, PDF and EPUB, because a finding that only reaches a chat window has not been delivered.

Every number in those documents traces to a talk page or a newsletter item; every recommendation names its source. That is the point of the layer: the reader can disagree with a conclusion by clicking through, and never has to take the summary on faith.

## What this is not

- Not a replacement for reading the primary source when a decision depends on it. The layer tells you which source, and which minute.
- Not a popularity feed. Attention is measured separately (a later step can ask YouTube how widely an idea travelled); the reading layer ranks on substance.
- Not a fixed taxonomy. Clusters are per corpus and per reader; the rule is only that a classifier never mints one on its own.
- Not a fetcher for non-video sources. Pulling a community's newsletter issues or talk pages (mail access, sign-in, site harvest) is operator-specific and stays outside this repo's CLI; only what happens once that text exists is documented here.

## Related

- `prompts/topic-digest.md`, `prompts/cliffnotes-distiller.md`: the shipped default templates for two of the three shapes.
- `docs/topics-layer.md`: how briefings assert topic membership.
- `docs/adr/ADR-0018-nugget-cli-cross-creator-synthesis.md`: the grounded synthesis command.
- `specs/agent-rules.md`, section 1: "reduce cognitive load on the next reader" is the same rule applied to code.
