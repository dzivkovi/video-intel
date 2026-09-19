# Model scorecard - gemini-3.8-flash, gemini-3.7-flash

Generated 2026-09-19 20:20 UTC by `scripts/model_eval.py`.

`max_gap_s` is the headline: the largest interval between consecutive
timestamps, i.e. the worst-case error of a `&t=<seconds>` deep link.
Lower is better. Two models can emit identical text and identical token
counts while differing several-fold on this number.

| fixture | model | shape | segs | max_gap_s | scr | spk | think | $/vid-hr | window |
|---|---|---|---|---|---|---|---|---|---|
| screenshare-demo | gemini-3.8-flash | dict | 34 | 167 | 3 | 2 | 0 | 0.302 | ok |
| screenshare-demo | gemini-3.7-flash | dict | 10 | 200 | 3 | 2 | 0 | 0.301 | ok |
| multi-speaker-podcast | gemini-3.8-flash | dict | 34 | 59 | 18 | 2 | 0 | 0.377 | ok |
| multi-speaker-podcast | gemini-3.7-flash | dict | 30 | 65 | 16 | 2 | 0 | 0.349 | ok |
| talking-head-monologue | gemini-3.8-flash | dict | 110 | 21 | 6 | 2 | 0 | 0.393 | ok |
| talking-head-monologue | gemini-3.7-flash | dict | 118 | 19 | 4 | 2 | 0 | 0.414 | ok |
| long-form-midpoint | gemini-3.8-flash | dict | 116 | 25 | 1 | 2 | 0 | 0.387 | ok |
| long-form-midpoint | gemini-3.7-flash | dict | 24 | 141 | 1 | 2 | 0 | 0.321 | ok |
| four-presenter-panel | gemini-3.8-flash | dict | 26 | 68 | 0 | 0 | 0 | 0.326 | ok |
| four-presenter-panel | gemini-3.7-flash | dict | 58 | 46 | 5 | 4 | 0 | 0.366 | ok |
| busy-screen-two-presenters | gemini-3.8-flash | dict | 147 | 10 | 36 | 2 | 0 | 0.466 | ok |
| busy-screen-two-presenters | gemini-3.7-flash | dict | 118 | 20 | 17 | 2 | 0 | 0.409 | ok |

## Per-facet notes

- **screenshare-demo** - Screen-share with live terminal/editor output. Probes on-screen text capture and the monolithic-collapse failure: the shape where a model transcribes the words correctly but stamps minutes of content as one block, destroying the &t= deep-link precision the corpus exists for.
- **multi-speaker-podcast** - Two-plus speakers with interruption and crosstalk. Probes diarization: whether distinct voices are separated or merged into one speaker.
- **talking-head-monologue** - Single presenter, minimal visual aid, dense continuous speech. The easy case - a model that degrades HERE is disqualified outright.
- **long-form-midpoint** - Deep inside a 90-min+ video, away from any intro. Probes whether quality holds at a chunk boundary rather than only in the opening minutes.
- **four-presenter-panel** - Four presenters in a 36-minute session, little on screen. Probes attribution at scale: whether four voices stay four speakers across handoffs, or collapse into two, and whether segment density holds when nobody is driving a screen.
- **busy-screen-two-presenters** - Two presenters over a dense, fast-changing screen share. Probes the multimodal case the corpus is built for: on-screen text captured as screen_content while dialogue is still attributed to the right of two voices.

## Verdict

Incumbent: `gemini-3.7-flash`.

- `gemini-3.8-flash` vs `gemini-3.7-flash`: mean max_gap **58.333s** vs 81.833s; mean cost/video-hour **$0.375** vs $0.36.

## Roll history (hand-recorded)

Format: `max_gap_s / segments / speakers / screen items` per roll. `-` means that roll was not run for that fixture.

| fixture | model | roll 1 | roll 2 | roll 3 |
|---|---|---|---|---|
| talking-head-monologue | gemini-3.8-flash | 175 | 21 | - |
| talking-head-monologue | gemini-3.7-flash | 78 | 19 | - |
| screenshare-demo | gemini-3.8-flash | 167 | - | - |
| screenshare-demo | gemini-3.7-flash | 200 | - | - |
| multi-speaker-podcast | gemini-3.8-flash | 59 | - | - |
| multi-speaker-podcast | gemini-3.7-flash | 65 | - | - |
| long-form-midpoint | gemini-3.8-flash | 25 | - | - |
| long-form-midpoint | gemini-3.7-flash | 141 | - | - |
| four-presenter-panel | gemini-3.8-flash | 66/38/4/5 | 82/25/4/5 | 68/26/0/0 |
| four-presenter-panel | gemini-3.7-flash | 68/27/4/5 | 26/79/4/5 | 46/58/4/5 |
| busy-screen-two-presenters | gemini-3.8-flash | 23/82/2/24 | 63/62/2/17 | 10/147/2/36 |
| busy-screen-two-presenters | gemini-3.7-flash | 29/52/2/17 | 25/111/2/31 | 20/118/2/17 |

Medians (max_gap_s) for the three-roll fixtures: four-presenter-panel 68 (3.8) vs 46 (3.7); busy-screen-two-presenters 23 (3.8) vs 25 (3.7).

**Dropped-task roll:** on four-presenter-panel, gemini-3.8-flash's third roll returned ONLY the transcripts task - no speakers list and no screen_content, even though voices 1-4 were present but unnamed in the transcript text. gemini-3.7-flash returned all three tasks (speakers, screen_content, transcripts) on every roll of both new fixtures.

Cost note: gemini-3.8-flash ran about 5% higher on these fixtures from more output tokens; the per-token price list is identical between the two models.

## Not measured here

State this every time. Timestamp *drift* (do stamps match the actual
audio) is unverified - only granularity is. Chunked long-video behavior
near the 64k output cap is not exercised by a single short segment.
Non-English and heavily accented speech are not represented in the
current fixture set. Each run is a single sample per cell.

## Decision

Staying on `gemini-3.7-flash`. On the four-presenter shape 3.7 holds a lower median max_gap_s (46 vs 68) and kept all four speakers plus screen_content on every roll, while 3.8 dropped the speakers and screen_content tasks entirely on one of three rolls; busy-screen-two-presenters is a tie inside the noise floor, and the single-roll wins elsewhere sit inside that same noise. See [issue #219](https://github.com/dzivkovi/video-intel/issues/219).
