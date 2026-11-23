# Model and prompt benchmark runbook

One page for the two questions this repo keeps asking: a new Gemini model arrived, or a transcript prompt candidate exists. Both use one harness, `scripts/model_eval.py`, and end with a verdict per written rule instead of a fresh judgment call. Why it works this way: `.claude/rules/evals.md` (the issue #292 entry).

## Before the first run

Copy `tests/evals/model_fixtures.yaml.example` to `tests/evals/model_fixtures.yaml` (gitignored) and point each fixture at a real video. A fixture can set `expects_speakers: false` or `expects_screen: false`; both default to true. Set `expects_screen: false` on a fixture with little or nothing on screen, or every roll of it is a hard fail for a correct empty `screen_content`.

Always dry-run first. The dry run prints the arms, the call count (fixtures x arms x rolls) and the cost estimate, and calls nothing: no Gemini client and no caption fetch.

## A new model arrived

```bash
python scripts/model_eval.py --candidate <new-model> --rolls 3 --dry-run
python scripts/model_eval.py --candidate <new-model> --rolls 3
```

With no `--incumbent`, the incumbent is the default model in `scripts/video_intel.py` (`DEFAULT_MODEL`) with the bundled prompt, so the run has two arms. Name another incumbent with `--incumbent <model>`.

Read `tests/evals/model-cards/<new-model>.md`, the "Decision rule" section first.

## A prompt candidate

```bash
python scripts/model_eval.py --candidate <current-default> --candidate-prompt path/to/candidate-transcript.md --incumbent bundled --rolls 3 --dry-run
python scripts/model_eval.py --candidate <current-default> --candidate-prompt path/to/candidate-transcript.md --incumbent bundled --rolls 3
```

`--incumbent bundled` compares the candidate prompt with the bundled prompt on the same model. The candidate arm is labelled `candidate:<first 8 hex of the file's sha256>`, and the card's "Prompts per arm" section records each arm's full hash and source. Read the same card, the same section.

## The decision rule in plain words

A candidate arm is adopted only if all four hold on every fixture:

- R1: no roll of the candidate is a hard fail. A hard fail is a response that is not the transcript envelope after the production parser, a missing `speakers` or `screen_content` task the fixture expects, or a roll whose generation failed.
- R2: the candidate's median `max_gap_s` (the worst-case deep-link error) is no worse than the incumbent's median by more than the incumbent's own roll-to-roll spread (max minus min).
- R3: the same comparison for the median caption offset.
- R4: a cost figure exists for the candidate.

Each rule prints PASS, FAIL or NOT EVALUATED, and an overall line follows. Overall is PASS only when every rule passes on every fixture. PASS does not compare cost or transcript completeness: read the medians table for those.

## What each NOT EVALUATED means

| Message | Meaning | How to clear it |
|---|---|---|
| needs --rolls >= 2 | the incumbent has fewer than 2 rolls with a value, so its spread is unknown | re-run with `--rolls 3` |
| candidate roll(s) gave no usable value | a candidate roll missed its window or produced no value, and dropping it could flatter the candidate | read the roll table; fix the fixture window or re-run |
| the candidate has no caption offset, or only N entries aligned | no caption track, or fewer than 5 dialogue entries matched a caption cue | the "Caption alignment" section names the reason; a refused request is YouTube refusing this IP, not a video without captions |
| no incumbent rolls for this fixture | the incumbent arm has no cell for that fixture | run both arms on the same fixtures |
| both arms were served by one model | two model names were requested but the provider reported the same model for both | the comparison measures one model against itself; pick a different incumbent |

A `legacy-cache roll(s) excluded` note means a cell came from a cache entry written before the cache key existed. Its prompt is unverified, so the rule leaves it out. Delete that pair of files under `tests/evals/model-cards/_raw/` to measure it again.

A FAIL on R1 caused only by `generation_failed` with a transport error (a timeout, a 429) is an infrastructure failure, not a model verdict. Re-run; failed rolls are never cached.

## The cost line

The dry run prices every arm as fixtures x rolls x segment minutes at the transcript token rates in `docs/cost-model.md` (about 5,500 input and 450 output tokens per video-minute at low media resolution) times the model's rate in `PRICING`. It is an estimate from average measured rates. It ignores cells already cached (those cost nothing), and a model that emits more output tokens can exceed it. A model missing from `PRICING` is named as not priced and adds nothing to the estimate.

## Caching

Every roll is cached under `tests/evals/model-cards/_raw/` (gitignored), keyed on the model, the fixture and its window, the prompt's content hash, the thinking level and the roll number. A re-run replays what is cached and calls Gemini only for the rest; when everything is cached, no API key is needed. Captions are fetched once per fixture and cached in the same folder.

## What the harness does not measure

- Word-level timing. The offset is cue level: a dialogue entry's stamp against the start of the caption cue holding its first words. On an auto-generated track, the ruler is itself speech-recognition timing. Measured once on one real video: about 99% of entries aligned, the median offset was about +1 second (stamps slightly late) and the 90th percentile of the absolute offset about 3 seconds.
- Long videos. Chunked behavior near the output cap is not exercised by a single short segment.
- Non-English and heavily accented speech, unless your fixtures include them.
- Transcript completeness or quality of the text itself, beyond the hard fails.
