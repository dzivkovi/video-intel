# Model scorecard - gemini-3.7-flash, gemini-3-flash-preview

Generated 2026-08-18 11:31 UTC by `scripts/model_eval.py`.

`max_gap_s` is the headline: the largest interval between consecutive
timestamps, i.e. the worst-case error of a `&t=<seconds>` deep link.
Lower is better. Two models can emit identical text and identical token
counts while differing several-fold on this number.

| fixture | model | shape | segs | max_gap_s | scr | spk | think | $/vid-hr |
|---|---|---|---|---|---|---|---|---|
| screenshare-demo | gemini-3.7-flash | dict | 10 | 200 | 3 | 2 | 0 | 0.301 |
| screenshare-demo | gemini-3-flash-preview | **ERROR** | | | | | | |
| | | `ClientError: 403 PERMISSION_DENIED. {'error': {'code': 403, 'message': 'The caller does not have permission', 'status': 'PERMISSION_DENIED'}}` | | | | | | |
| multi-speaker-podcast | gemini-3.7-flash | dict | 30 | 65 | 16 | 2 | 0 | 0.349 |
| multi-speaker-podcast | gemini-3-flash-preview | list-wrapped | 14 | 89 | 17 | 2 | 0 | 0.223 |
| talking-head-monologue | gemini-3.7-flash | dict | 52 | 78 | 4 | 2 | 0 | 0.358 |
| talking-head-monologue | gemini-3-flash-preview | list-wrapped | 14 | 209 | 5 | 2 | 0 | 0.247 |
| long-form-midpoint | gemini-3.7-flash | dict | 24 | 141 | 1 | 2 | 0 | 0.321 |
| long-form-midpoint | gemini-3-flash-preview | list-wrapped | 9 | 224 | 1 | 2 | 0 | 0.212 |

## Per-facet notes

- **screenshare-demo** - Screen-share with live terminal/editor output. Probes on-screen text capture and the monolithic-collapse failure: the shape where a model transcribes the words correctly but stamps minutes of content as one block, destroying the &t= deep-link precision the corpus exists for.
- **multi-speaker-podcast** - Two-plus speakers with interruption and crosstalk. Probes diarization: whether distinct voices are separated or merged into one speaker.
- **talking-head-monologue** - Single presenter, minimal visual aid, dense continuous speech. The easy case - a model that degrades HERE is disqualified outright.
- **long-form-midpoint** - Deep inside a 90-min+ video, away from any intro. Probes whether quality holds at a chunk boundary rather than only in the opening minutes.

## Verdict

Incumbent: `gemini-3-flash-preview`.

- `gemini-3.7-flash` vs `gemini-3-flash-preview`: mean max_gap **121.0s** vs 174.0s; mean cost/video-hour **$0.332** vs $0.227.

## Not measured here

State this every time. Timestamp *drift* (do stamps match the actual
audio) is unverified - only granularity is. Chunked long-video behavior
near the 64k output cap is not exercised by a single short segment.
Non-English and heavily accented speech are not represented in the
current fixture set. Each run is a single sample per cell.
