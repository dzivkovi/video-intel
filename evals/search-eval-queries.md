# Search Evaluation Queries

25 frozen queries to benchmark retrieval quality across both search modes.
Each query specifies expected behavior and which skill path it tests.

Generated: 2026-04-03
Corpus: 246 concepts, 351 videos, ~3,673 chunks, 5 channels

## How to Use

Run each query in both modes and record:
- **Concept:** Does it return relevant videos? How many?
- **Vector:** Does it return relevant passages? Is the top-1 hit on-topic?
- **Delta:** Which mode serves this query better?

```bash
python scripts/video_intel.py search "QUERY"
python scripts/video_intel.py search "QUERY" --vector
```

---

## Category A: Concept Search Strengths (exact label/alias hits)

These should work well with concept search. Vector search should also work but concept is sufficient.

### A1. Broad topic — high video count
```
"multi-agent orchestration"
```
- **Tests:** Exact preferred_label match on a 36-video concept
- **Expected concept:** 36 videos across multiple channels
- **Expected vector:** Top hits should be videos deeply about multi-agent patterns
- **Path tested:** Concept search with high-recall topic

### A2. Alias match — user words differ from preferred label
```
"AI agent market competition"
```
- **Tests:** Alias match → "AI Market Dynamics" (59 videos)
- **Expected concept:** Should resolve alias to preferred concept
- **Expected vector:** Videos discussing competitive landscape
- **Path tested:** Taxonomy alias resolution

### A3. Mid-range concept — cross-channel potential
```
"adaptive reasoning"
```
- **Tests:** 5-video concept across potentially multiple creators
- **Expected concept:** 5 videos
- **Expected vector:** Passages about configurable thinking levels
- **Path tested:** Cross-channel concept coverage

### A4. Domain-prefixed concept
```
"context window optimization"
```
- **Tests:** 23-video concept, common across Claude Code content
- **Expected concept:** Heavy natebjones + mark_kashef representation
- **Expected vector:** Specific tips/strategies for managing context
- **Path tested:** High-frequency concept, channel distribution

### A5. Niche concept — minimal coverage
```
"dead man's switch"
```
- **Tests:** 1-video concept with alias "Canary Trigger Concept"
- **Expected concept:** Exactly 1 video
- **Expected vector:** The specific passage explaining the mechanism
- **Path tested:** Rare concept recall, precision on tail

---

## Category B: Vector Search Strengths (semantic/evidence queries)

These need vector search. Concept search will miss or return noise.

### B1. Evidence query — specific claim
```
"how long before helium runs out for AI chips"
```
- **Tests:** Specific factual claim from natebjones video (2026-03-29)
- **Expected concept:** Likely no match (no "helium" concept)
- **Expected vector:** Should surface the exact natebjones video/passage
- **Path tested:** Fact retrieval that bypasses taxonomy entirely

### B2. Paraphrased query — synonym gap
```
"permission errors when AI agents try to do things"
```
- **Tests:** Semantic match for "AI Safety and Alignment" or "AI Agent Configuration" content
- **Expected concept:** Weak — "permission errors" not a concept label
- **Expected vector:** Passages about agent access control, tool permissions
- **Path tested:** Vector search bridging vocabulary gap

### B3. Conversational / natural language query
```
"why does Claude Code keep losing context in long sessions"
```
- **Tests:** Natural phrasing that maps to "Context Window Optimization" or "Agent Memory"
- **Expected concept:** Might partial-match "context" but imprecise
- **Expected vector:** Specific passages about context management strategies
- **Path tested:** Natural language → semantic retrieval

### B4. Quote-style query — looking for who said what
```
"code beats markdown for agent skills"
```
- **Tests:** Should find Sam Witteveen's video "agent-skills-code-beats-markdown-heres-why"
- **Expected concept:** Might match "AI Agent Skills" (21v) — too broad
- **Expected vector:** Should rank Sam's transcript #1 or #2
- **Path tested:** Specific argument retrieval across channels

### B5. Cross-domain query — connects two concepts
```
"using reinforcement learning to train better AI agents"
```
- **Tests:** Bridges "Reinforcement Learning" (3v) and "Autonomous AI Agents" (49v)
- **Expected concept:** Might hit one or the other, not the intersection
- **Expected vector:** Passages where RL and agents are discussed together
- **Path tested:** Cross-concept semantic intersection

---

## Category C: Channel Filtering

### C1. Broad topic + channel filter
```
"AI agent skills" --channel mark_kashef
```
- **Tests:** Filtering a 21-video concept to one creator
- **Expected concept:** mark_kashef subset only
- **Expected vector:** mark_kashef passages about skills
- **Path tested:** Channel filter on both modes

### C2. Niche query + channel filter
```
"obsidian second brain" --channel mark_kashef
```
- **Tests:** Should find "claude-code-turned-obsidian-into-my-dream-second-brain"
- **Expected concept:** Possibly no concept match
- **Expected vector:** Specific passages from that one video
- **Path tested:** Channel filter recovering a specific video by content

### C3. Channel with few transcripts
```
"document parsing" --channel samwitteveenai
```
- **Tests:** Sam has only 3 transcripts; should find "liteparse-the-local-document-parser"
- **Expected concept:** May not match
- **Expected vector:** Limited candidates — tests behavior with sparse index
- **Path tested:** Sparse channel vector retrieval

---

## Category D: Edge Cases and Limits

### D1. Single-word broad query
```
"agents"
```
- **Tests:** Extremely broad — should match dozens of concepts
- **Expected concept:** Massive result set (49+ videos under "Autonomous AI Agents" alone)
- **Expected vector:** Top hits should be the most agent-focused passages
- **Path tested:** Ranking quality under high recall

### D2. Very specific technical term
```
"IVF cosine index"
```
- **Tests:** Implementation detail that might appear in zero transcript chunks
- **Expected concept:** No match
- **Expected vector:** Possibly 0 results or very low scores
- **Path tested:** Graceful handling of out-of-corpus queries

### D3. Negation / absence query
```
"problems with MCP that nobody talks about"
```
- **Tests:** Embeddings don't handle negation well
- **Expected concept:** Might match "Model Context Protocol" (28v)
- **Expected vector:** Will likely return MCP content (ignoring "problems" + "nobody")
- **Path tested:** Known weakness — negation in semantic search

### D4. Multi-language / non-English
```
"inteligencia artificial agentes"
```
- **Tests:** Voyage 4 has multilingual capability but corpus is English
- **Expected concept:** No match
- **Expected vector:** May surface agent-related content if cross-lingual embedding works
- **Path tested:** Cross-lingual retrieval (expected to be weak)

### D5. Empty result / low similarity
```
"kubernetes pod autoscaling strategies"
```
- **Tests:** Topic completely outside the corpus domain
- **Expected concept:** No match
- **Expected vector:** All results should be <0.01 similarity (noise)
- **Path tested:** True negative — no false positives

---

## Category E: Display and Output Modes

### E1. Full chunk vs preview comparison
```
"ephemeral user interfaces" --vector
"ephemeral user interfaces" --vector --preview
```
- **Tests:** Same query, two display modes
- **Expected:** Full mode shows up to 3000 chars with SCREEN blocks; preview shows 200-char single-line
- **Path tested:** --preview flag behavior, chunk truncation

### E2. High-limit stress test
```
"workflow automation" --vector --limit 25
```
- **Tests:** 27-video concept — requesting more results than default
- **Expected:** 25 results (after dedup, one per video), quality degrades in tail
- **Path tested:** Limit override, dedup under high-N, tail quality

### E3. Min-similarity filter
```
"token economics" --vector --min-similarity 0.05
```
- **Tests:** 18-video concept with similarity floor
- **Expected:** Only results above 0.05 threshold; likely fewer than default 10
- **Path tested:** --min-similarity filtering, score distribution

### E4. Concept search with limit
```
"AI safety" --limit 5
```
- **Tests:** Broad concept (23 videos) capped to 5
- **Expected concept:** Top 5 most relevant videos
- **Path tested:** Concept search limit override

### E5. Vector search dedup behavior
```
"Claude Mythos" --vector
```
- **Tests:** Very recent topic (2026-04-01) likely concentrated in one video
- **Expected:** Should dedup to 1 result from natebjones, not repeat same video
- **Path tested:** _dedup_by_video() with concentrated results

---

## Scoring Rubric

For each query, score 0-2 on:

| Score | Meaning |
|-------|---------|
| 0 | Wrong or no useful results |
| 1 | Partially relevant — right topic area but not the best match |
| 2 | Correct — top result directly answers the query intent |

Track: query_id, mode (concept/vector), score, top_result_id, notes
