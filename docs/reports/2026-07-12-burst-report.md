# Concept burst report

Kleinberg two-state burst detection over per-concept video streams (issue #103). Corpus end: 2026-07-01. Params: {'min_events': 6, 's': 2.0, 'gamma': 1.0, 'min_gap_days': 0.5}. A burst is a rate jump against the concept's OWN baseline - 'just caught fire', not 'popular overall'. Intensity is the burst run's log-likelihood advantage over that concept's own baseline: it is NOT comparable across concepts with different baselines - use it to rank a concept's bursts against each other, not concept vs concept. Same-day videos are spaced at the min_gap_days floor before rate fitting. This corpus is small; every row is a lead for inspection, not a verdict.

> 11 concept-video rows were excluded for missing publish dates - their absence can shift gaps and rising status for the affected concepts.

## Corpus volume context (read this first)

Every concept stream rides the corpus's indexing volume: when the corpus itself grows, many concepts 'burst' at once. 50 of 95 bursts are currently rising - before reading any single row as a topic catching fire, check whether its start date coincides with a volume surge below.

| Month | Videos published |
|---|---|
| 2025-08 | 31 |
| 2025-09 | 30 |
| 2025-10 | 32 |
| 2025-11 | 40 |
| 2025-12 | 69 |
| 2026-01 | 92 |
| 2026-02 | 111 |
| 2026-03 | 162 |
| 2026-04 | 192 |
| 2026-05 | 202 |
| 2026-06 | 152 |
| 2026-07 | 1 |

## Bursting now (50)

- **ai-engineering.ai_personalization** - began 2026-05-28, still rising (intensity 3.4, 9 videos) - first in burst: thenextnewthingai, [Did Claude just beat Codex? (Opus 4.8 analysis)](https://www.youtube.com/watch?v=V_g3rlQ0st8), 2026-05-28
- **ai-engineering.ai_code_comprehension** - began 2026-05-14, still rising (intensity 2.3, 6 videos) - first in burst: seankochel, [5 "Engineer-Only" Claude Skills Every Vibe Coder NEEDS](https://www.youtube.com/watch?v=M-8lv5TXUYk), 2026-05-14
- **ai-engineering.lead_generation_automation** - began 2026-05-06, still rising (intensity 3.2, 8 videos) - first in burst: everyinc, [Why We Switched From Claude Code to Codex](https://www.youtube.com/watch?v=x9BNBcP_C7Q), 2026-05-06
- **ai-engineering.open_weights_models** - began 2026-04-29, still rising (intensity 4.3, 12 videos) - first in burst: samwitteveenai, [NVIDIA's NEW All-in-One: Nemotron 3 Nano Omni for Multimodal Agents](https://www.youtube.com/watch?v=XNaI4Xd4qXc), 2026-04-29
- **ai-engineering.ai_hardware_infrastructure** - began 2026-04-24, still rising (intensity 3.0, 7 videos) - first in burst: samwitteveenai, [The Era of Agents is Here: Logan Kilpatrick on Why Everyone Is Now a Builder](https://www.youtube.com/watch?v=voWCwpibLZM), 2026-04-24
- **ai-engineering.model_selection_strategy** - began 2026-04-23, still rising (intensity 13.9, 33 videos) - first in burst: colemedin, [Parallel Claude Code + Git Worktrees: This Setup Will Change How You Ship](https://www.youtube.com/watch?v=rFGlJ4oIlhw), 2026-04-23
- **ai-engineering.generative_engine_optimization** - began 2026-04-23, still rising (intensity 1.8, 5 videos) - first in burst: thenextnewthingai, [“I make $4.5 million implementing AI”](https://www.youtube.com/watch?v=LAOXy3DLyPg), 2026-04-23
- **productivity.ai_productivity_workflows** - began 2026-04-21, still rising (intensity 6.3, 14 videos) - first in burst: everyinc, [Introducing Monologue Notes: For Builders, Makers, Doers](https://www.youtube.com/watch?v=qeYtwxjP3tg), 2026-04-21
- **ai-engineering.multimodal_content_generation** - began 2026-04-20, still rising (intensity 11.3, 31 videos) - first in burst: chase_h_ai, [Claude Design + Seedance 2.0 = INSANE Animated Websites](https://www.youtube.com/watch?v=7uW1SKmx-Ic), 2026-04-20
- **ai-engineering.ai_content_quality** - began 2026-04-18, still rising (intensity 3.7, 13 videos) - first in burst: benai92, [8 Claude Skills I Can’t Live Without](https://www.youtube.com/watch?v=bXnRA3pJavE), 2026-04-18
- **ai-engineering.automated_presentation_generation** - began 2026-04-17, still rising (intensity 3.0, 9 videos) - first in burst: chase_h_ai, [Claude Design is INSANE](https://www.youtube.com/watch?v=-tGH2tLwCEw), 2026-04-17
- **ai-engineering.world_models** - began 2026-04-12, still rising (intensity 2.3, 7 videos) - first in burst: natebjones, [I Watched 3 Companies Lay Off Their Managers. All 3 Hit the Same Wall.](https://www.youtube.com/watch?v=zhXgkQ3nYeE), 2026-04-12
- **ai-engineering.inference_workload_optimization** - began 2026-04-11, still rising (intensity 3.8, 13 videos) - first in burst: natebjones, [This New Method Just Killed RAM Limitations](https://www.youtube.com/watch?v=erV_8yrGMA8), 2026-04-11
- **ai-engineering.grounded_generation** - began 2026-04-11, still rising (intensity 3.0, 9 videos) - first in burst: benai92, [The 7 Levels of Using Claude Context Explained in 24 min](https://www.youtube.com/watch?v=l5Diqeoffa4), 2026-04-11
- **ai-engineering.agent_personas** - began 2026-04-07, still rising (intensity 4.7, 14 videos) - first in burst: thenextnewthingai, [One prompt adds 10,000 apps](https://www.youtube.com/watch?v=AtaXBkLU1no), 2026-04-07
- **ai-engineering.agent_execution_environments** - began 2026-04-06, still rising (intensity 25.2, 60 videos) - first in burst: natebjones, [The Missing Orchestration Layer Destroying Teams Right Now](https://www.youtube.com/watch?v=7HP1jFJ9W1c), 2026-04-06
- **ai-engineering.multimodal_analysis** - began 2026-04-02, still rising (intensity 8.7, 37 videos) - first in burst: samwitteveenai, [Gemma 4 Has Landed!](https://www.youtube.com/watch?v=5aqF1HVpjdc), 2026-04-02
- **ai-engineering.ai_security_operations** - began 2026-04-01, still rising (intensity 7.7, 22 videos) - first in burst: natebjones, [Your AI Stack Isn't Ready for Claude Mythos](https://www.youtube.com/watch?v=hV5_XSEBZNg), 2026-04-01
- **ai-engineering.automated_quality_assessment** - began 2026-03-31, still rising (intensity 23.7, 77 videos) - first in burst: chase_h_ai, [Claude Code + Codex = AI GOD](https://www.youtube.com/watch?v=L7NPhaUBpZE), 2026-03-31
- **ai-engineering.local_llm_inference** - began 2026-03-31, still rising (intensity 10.4, 31 videos) - first in burst: natebjones, [Your iPhone Is About to Control Every AI App You Use. Here's What This Means For You.](https://www.youtube.com/watch?v=BhXNtvZvziY), 2026-03-31
- ...and 30 more rising bursts (raise --top)

## Recent bursts, cooled (45)

- ...and 45 more cooled bursts (raise --top)
