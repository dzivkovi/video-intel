#!/usr/bin/env python3
"""Non-destructive A/B harness for swapping the Gemini model.

Why this exists
---------------
A model swap was shipped on a synthetic *text* microbenchmark and generalized to
*video transcription*. Throughput was the only axis measured; price was never
looked at; and the dimension that actually decided the question - timestamp
granularity - was not measured until the owner asked for a real A/B. The result
was a default that put the heaviest step on the most expensive model in the
lineup while making deep-links up to six minutes imprecise.

The lesson is not "be more careful". The gap was known and written down in a
status note, and shipped anyway. So the correction is to make the experiment
cheap enough that there is no reason to skip it, and to score enough facets that
"I tested it" cannot mean "I tested one axis".

What it does
------------
Runs each (fixture x model) pair against the real ``call_gemini`` path with the
real transcript prompt, scores the result across mechanical + economic
dimensions, and writes a scorecard. It NEVER writes into the corpus - only into
``tests/evals/model-cards/``.

The headline metric is ``max_gap_s``: the largest interval between consecutive
timestamps. That is the worst-case error of a ``&t=<seconds>`` deep link, which
is what this corpus is FOR. Two models can emit identical text and identical
token counts while differing 5x on this number.

Issue #292 made it the repeatable benchmark for models AND prompts: every
(fixture x arm) cell runs ``--rolls`` times, a hard fail is detected through the
production parser, the timestamp offset against the caption track is measured
at cue level through ``caption_align.entry_offsets``, ``--candidate-prompt``
adds a prompt arm, and ``decision_rule`` prints PASS / FAIL / NOT EVALUATED per
written rule. ``docs/model-benchmark-runbook.md`` says what to run and read.

Usage
-----
    python scripts/model_eval.py --candidate gemini-3.7-flash --incumbent gemini-3.5-flash
    python scripts/model_eval.py --candidate gemini-3.7-flash --fixtures screenshare-demo
    python scripts/model_eval.py --candidate X --thinking low --dry-run
    python scripts/model_eval.py --candidate X --rolls 3 --candidate-prompt my-prompt.md --incumbent bundled
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import caption_align
import yaml
from youtube_captions import CaptionsResult, captions_failure_is_refusal

import video_intel as v
from gemini_common import reported_name_differs, served_model_from_response
from timestamp_utils import timestamp_tolerance

REPO = Path(__file__).resolve().parent.parent
_FIXTURES_REAL = REPO / "tests" / "evals" / "model_fixtures.yaml"
_FIXTURES_TEMPLATE = REPO / "tests" / "evals" / "model_fixtures.yaml.example"
# The real fixture set is gitignored: it points at videos from the maintainer's
# private corpus and annotates each with the defect it exposes, which is a
# curated-watchlist extract rather than a list of public URLs. Mirrors the
# config.yaml / config.yaml.example split. Falling back to the template keeps
# a fresh checkout runnable enough to fail with a useful message instead of a
# FileNotFoundError.
FIXTURES = _FIXTURES_REAL if _FIXTURES_REAL.exists() else _FIXTURES_TEMPLATE
CARDS = REPO / "tests" / "evals" / "model-cards"

# $ per 1M tokens: (input, output). Output INCLUDES thinking tokens - that is the
# whole reason thinking is an economic dimension and not just a latency one.
# Promo rates for 3.6/3.7 expire 2026-12-31 and then double; `promo_until` is
# carried into the scorecard so a cost ranking is never read as permanent.
PRICING = {
    "gemini-3-flash-preview": (0.50, 3.00, None),
    "gemini-3.5-flash": (1.50, 9.00, None),
    "gemini-3.6-flash": (0.75, 3.75, "2026-12-31"),
    "gemini-3.7-flash": (0.75, 3.75, "2026-12-31"),
    # Same introductory rate and expiry as 3.7 (blog.google, 2026-09): $1.50/$7.50 from 2027-01-01.
    "gemini-3.8-flash": (0.75, 3.75, "2026-12-31"),
    "gemini-2.5-flash": (0.30, 2.50, None),
    "gemini-2.5-pro": (1.25, 10.00, None),
}

# Thinking levels a model accepts. `minimal` is Flash-exclusive and 3.7 dropped
# it; sending it there is a hard 400. None means "let the helper decide".
DEFAULT_THINKING = {"gemini-3.7-flash": "low", "gemini-3.8-flash": "low"}

#: The prompt label of the bundled transcript prompt (or its issue #221 override).
BUNDLED = "bundled"
#: Below this many aligned dialogue entries the caption offset is "not measured" (None), never 0.
OFFSET_MIN_ALIGNED_ENTRIES = 5
#: Transcript token rates per video-minute at MEDIA_RESOLUTION_LOW, measured in
#: docs/cost-model.md. Used only for the dry-run estimate.
EST_INPUT_TOKENS_PER_VIDEO_MINUTE = 5500
EST_OUTPUT_TOKENS_PER_VIDEO_MINUTE = 450
#: Where a cached cell's full key is stored inside its `.usage.json`.
CACHE_KEY_FIELD = "_cache_key"

HARD_FAIL_GENERATION = "generation_failed"
HARD_FAIL_NOT_ENVELOPE = "not_envelope"
HARD_FAIL_MISSING_SPEAKERS = "missing_speakers"
HARD_FAIL_MISSING_SCREEN = "missing_screen_content"

PASS, FAIL, NOT_EVALUATED = "PASS", "FAIL", "NOT EVALUATED"
#: The decision rules, labelled as issue #292 writes them.
RULES = (
    ("R1", "no hard fail in any roll"),
    ("R2", "median max_gap_s no worse than the incumbent's beyond its roll-to-roll spread"),
    ("R3", "median caption offset no worse than the incumbent's beyond its roll-to-roll spread"),
    ("R4", "cost stated"),
)
RULE_IDS = tuple(rid for rid, _ in RULES)

# The identity readers' pattern in video_intel.py (11 id characters with a right
# boundary); there is no shared URL-to-id helper to call.
_YOUTUBE_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")


@dataclass(frozen=True)
class PromptArm:
    """The prompt one arm sends: its label, text, content hash and where it came from."""

    label: str
    text: str
    sha256: str
    source: str
    override: str | None = None


@dataclass(frozen=True)
class CaptionTrack:
    """A fixture's caption cues, or None with the reason they are unavailable."""

    cues: list[caption_align.Cue] | None
    reason: str | None = None


def _secs(stamp: str) -> int | None:
    try:
        parts = [int(p) for p in str(stamp).split(":")]
    except (ValueError, AttributeError):
        return None
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def score(
    raw: str,
    usage: dict,
    model: str,
    seg_secs: int,
    wall: float | None,
    start_secs: int | None = None,
    caption_track: CaptionTrack | None = None,
) -> dict:
    """Score one run. Every field here is a dimension a model can regress on.

    ``start_secs`` (issue #219) is the fixture's requested ``start`` offset. It
    is what lets a cell state whether it actually measured the window it asked
    for. Default ``None`` keeps every pre-#219 caller scoring exactly as before.
    ``caption_track`` (issue #292) feeds the cue-level caption offset; omitted,
    the offset fields are None with a reason.
    """
    parsed, err = v.try_parse_transcript_json(raw)
    # A top-level list is Gemini returning the envelope wrapped in an array. The
    # pipeline recovers from it, but it IS a malformation and a model that emits
    # it is a worse citizen than one that does not - so it is scored, not hidden.
    shape = "dict" if isinstance(parsed, dict) else ("list-wrapped" if isinstance(parsed, list) else "unparsed")
    env = parsed[0] if isinstance(parsed, list) and parsed else parsed
    # Issue #292: the envelope production accepts (merge_transcript_json takes
    # parsed[0] of a list-wrapped response), judged before the {} fallback.
    envelope_ok = isinstance(env, dict) and isinstance(env.get("transcripts"), list)
    env = env if isinstance(env, dict) else {}

    tr = env.get("transcripts") or []
    stamps = sorted(s for s in (_secs(e.get("start")) for e in tr) if s is not None)
    gaps = [b - a for a, b in itertools.pairwise(stamps)] if len(stamps) > 1 else []
    # Trailing gap: from the last stamp to the end of the requested window. A
    # model that stops stamping at 03:45 of a 10-minute window has a 375s
    # trailing gap even though its text may run to the end.
    trailing = (seg_secs - (stamps[-1] - stamps[0])) if stamps else seg_secs
    chars = sum(len(e.get("text") or "") for e in tr)

    # Issue #219: does this cell measure the window it asked for?
    #
    # Issue #141 spent weeks on a suspicion that `start_offset` had stopped
    # clipping, and the harness could not answer it. Worse, `trailing` goes
    # NEGATIVE when the returned span runs past the window, and
    # `max(gaps + [trailing])` then silently discards it - so an unclipped cell
    # scored as if nothing were wrong. A scorecard that cannot tell a clipped
    # run from an unclipped one cannot defend its own cost numbers.
    #
    # `start_secs is None` means the caller did not say, so no claim is made -
    # NOT that the window was fine.
    tol = timestamp_tolerance(seg_secs)
    window_exceeded = None
    first_stamp_offset = None
    if start_secs is not None and stamps:
        first_stamp_offset = stamps[0] - start_secs
        span_end = stamps[-1] - start_secs
        window_exceeded = bool(span_end > seg_secs + tol or abs(first_stamp_offset) > tol)

    pin, pout, promo = PRICING.get(model, (None, None, None))
    itok = usage.get("prompt") or 0
    otok = usage.get("candidates") or 0
    think = usage.get("thoughts") or 0
    cost_seg = (itok / 1e6 * pin + (otok + think) / 1e6 * pout) if pin else None

    return {
        "model": model,
        "parse_ok": parsed is not None,
        "parse_error": (err or "")[:80] or None,
        "envelope_shape": shape,
        "segments": len(tr),
        "segments_per_min": round(len(tr) / (seg_secs / 60), 2) if seg_secs else 0,
        # THE headline metric: worst-case deep-link error in seconds.
        "max_gap_s": max(gaps + [trailing]) if (gaps or stamps) else None,
        "median_gap_s": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "trailing_gap_s": trailing if stamps else None,
        # None = not checked (caller passed no start), True = this cell did not
        # measure its window and must not feed the verdict means.
        "window_exceeded": window_exceeded,
        "first_stamp_offset_s": first_stamp_offset,
        "chars": chars,
        "screen_content": len(env.get("screen_content") or []),
        "speakers": len(env.get("speakers") or []),
        "in_tok": itok,
        "out_tok": otok,
        "thinking_tok": think,
        "cost_segment": round(cost_seg, 4) if cost_seg is not None else None,
        "cost_per_video_hour": round(cost_seg * (3600 / seg_secs), 3) if cost_seg is not None else None,
        "promo_until": promo,
        "wall_s": round(wall, 1) if wall else None,
        # Issue #295: the model that actually answered. A request can be served
        # by another model, so `model` alone does not say what was measured.
        # None when unreadable, or for a cell cached before this field existed.
        "model_served": usage.get(v.SERVED_MODEL_CAPTURE_KEY),
        "envelope_ok": envelope_ok,
        **caption_offset_metric(tr, caption_track, start_secs, seg_secs),
    }


def caption_offset_metric(transcripts: list, track: CaptionTrack | None, start_secs: int | None, seg_secs: int) -> dict:
    """Median and p90 of |entry stamp - start of the cue holding its first words| (issue #292).

    Cue level, never word level: the time compared is a real cue's start. The
    alignment is ``caption_align.entry_offsets`` and nothing else
    [core: one-definition]. Stamps are read as absolute video time, the same
    reading the issue #219 window check uses. Only cues whose start lies inside
    the requested window are used. Unaligned entries are counted and excluded;
    fewer than ``OFFSET_MIN_ALIGNED_ENTRIES`` aligned makes the metric None.
    """
    out = {
        "caption_offset_median_s": None,
        "caption_offset_p90_s": None,
        "caption_aligned": None,
        "caption_unaligned": None,
        "caption_reason": None,
    }
    if track is None:
        out["caption_reason"] = "no caption track supplied"
        return out
    if track.cues is None:
        out["caption_reason"] = track.reason
        return out
    entries: list[tuple[float, str]] = []
    for e in transcripts:
        if not isinstance(e, dict):
            continue
        stamp = _secs(e.get("start"))
        text = str(e.get("text") or "").strip()
        if stamp is not None and text:
            entries.append((float(stamp), text))
    cues = track.cues
    if start_secs is not None:
        cues = [c for c in cues if start_secs <= c.start < start_secs + seg_secs]
    if not entries or not cues:
        out["caption_reason"] = "no dialogue entries" if not entries else "no caption cues inside the window"
        return out
    offsets = caption_align.entry_offsets(entries, cues)
    aligned = sorted(abs(o.offset_seconds) for o in offsets if o.offset_seconds is not None)
    out["caption_aligned"] = len(aligned)
    out["caption_unaligned"] = len(offsets) - len(aligned)
    if len(aligned) < OFFSET_MIN_ALIGNED_ENTRIES:
        out["caption_reason"] = (
            f"only {len(aligned)} of {len(offsets)} entries aligned (needs {OFFSET_MIN_ALIGNED_ENTRIES})"
        )
        return out
    out["caption_offset_median_s"] = round(statistics.median(aligned), 2)
    # Nearest rank: always an observed offset.
    out["caption_offset_p90_s"] = round(aligned[math.ceil(0.9 * len(aligned)) - 1], 2)
    return out


def hard_fails(row: dict, fixture: dict | None = None) -> list[str]:
    """The hard fails of one roll (issue #292), named; empty when there are none.

    Read from fields ``score()`` computed through the production parser
    (``try_parse_transcript_json`` and its wrapper normalization). A missing task
    and an empty one are the same fail: the normalization turns a missing task
    into ``[]``. A row without ``envelope_ok`` was written before this field
    existed and makes no claim, like ``cell_measured_its_window``.
    """
    if row.get("error"):
        return [HARD_FAIL_GENERATION]
    if row.get("envelope_ok") is False:
        return [HARD_FAIL_NOT_ENVELOPE]
    fixture = fixture or {}
    fails = []
    if fixture.get("expects_speakers", True) and row.get("speakers") == 0:
        fails.append(HARD_FAIL_MISSING_SPEAKERS)
    if fixture.get("expects_screen", True) and row.get("screen_content") == 0:
        fails.append(HARD_FAIL_MISSING_SCREEN)
    return fails


def prompt_override(prompt_name: str) -> str | None:
    """The resolved prompt path when it is NOT the bundled template.

    A scorecard is a claim about a MODEL, but `prompt_dirs:` and
    `$VIDEO_INTEL_PROMPT_DIR` can redirect the transcript prompt - and this
    harness never calls `load_config`, so a config it never read still moves
    its input through the module-level resolver. A card produced against a
    private prompt is not comparable to one produced against the shipped
    template, so the run says so and the card records it.
    """
    path = v.resolve_prompt_path(prompt_name)
    try:
        path.relative_to(v.SKILL_DIR / "prompts")
    except ValueError:
        return str(path)
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def bundled_prompt() -> PromptArm:
    """The transcript prompt production would send, labelled ``bundled``.

    Resolved through ``load_prompt``, so an issue #221 override is what is sent,
    hashed and recorded.
    """
    override = prompt_override("transcript")
    text = v.load_prompt("transcript")
    return PromptArm(BUNDLED, text, _sha256(text.encode("utf-8")), override or "prompts/transcript.md", override)


def candidate_prompt_arm(path: Path) -> PromptArm:
    """A ``--candidate-prompt`` file as an arm, labelled by the first 8 hex of its bytes' sha256."""
    data = path.read_bytes()
    digest = _sha256(data)
    return PromptArm(f"candidate:{digest[:8]}", data.decode("utf-8"), digest, path.name)


def thinking_label(model: str, thinking: str | None) -> str:
    """The thinking level in the cache key; ``helper-default`` when the helper decides from the model."""
    return thinking or DEFAULT_THINKING.get(model) or "helper-default"


def cache_stem(fixture_id: str, model: str, label: str, roll: int) -> str:
    """Roll 1 of the bundled arm keeps the pre-#292 stem, so existing caches still load."""
    if label == BUNDLED and roll == 1:
        return f"{fixture_id}__{model}"
    return f"{fixture_id}__{model}__{label.replace(':', '-')}__r{roll}"


def cache_key(fx: dict, model: str, prompt: PromptArm, roll: int, seg: int, thinking: str | None) -> dict:
    """Everything that changes the response; a cell is replayed only under an equal key."""
    return {
        "fixture": fx["id"],
        "url": fx["url"],
        "start": fx["start"],
        "segment_seconds": seg,
        "model": model,
        "prompt_sha256": prompt.sha256,
        "thinking": thinking_label(model, thinking),
        "roll": roll,
    }


def cached_cell(scratch: Path, stem: str, key: dict, label: str, roll: int) -> tuple[str, dict, str] | None:
    """``(raw, usage, cache_state)`` when the cache may serve this cell, else None.

    The ONE lookup both ``main``'s hit planning and ``run_one`` use. A stored key
    must equal ``key``; an entry with no stored key predates issue #292 and is
    served, marked ``legacy``, only as roll 1 of the bundled arm.
    """
    raw_p, use_p = scratch / f"{stem}.json", scratch / f"{stem}.usage.json"
    if not (raw_p.exists() and use_p.exists()):
        return None
    try:
        raw = raw_p.read_bytes().decode("utf-8")
        usage = json.loads(use_p.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(usage, dict):
        return None
    stored = usage.get(CACHE_KEY_FIELD)
    if stored is None:
        return (raw, usage, "legacy") if label == BUNDLED and roll == 1 else None
    return (raw, usage, "hit") if stored == key else None


def run_one(
    client,
    types,
    fx: dict,
    model: str,
    thinking: str | None,
    seg: int,
    scratch: Path,
    *,
    roll: int = 1,
    prompt: PromptArm | None = None,
    caption_track: CaptionTrack | None = None,
) -> dict:
    """One roll of one (fixture, model, prompt) cell. Cached on disk so a re-run costs nothing."""
    # Resolved BEFORE the cache check: a card re-rendered from a warm cache must
    # still say which prompt is active, or a cached rerun under an override
    # would be labelled as measuring the bundled template.
    prompt = prompt or bundled_prompt()
    override = prompt.override
    stem = cache_stem(fx["id"], model, prompt.label, roll)
    key = cache_key(fx, model, prompt, roll, seg, thinking)
    raw_p, use_p = scratch / f"{stem}.json", scratch / f"{stem}.usage.json"
    wall = None
    arm_fields = {"prompt_label": prompt.label, "roll": roll}
    hit = cached_cell(scratch, stem, key, prompt.label, roll)
    if hit:
        raw, usage, cache_state = hit
        print(f"  [cached] {stem}")
    else:
        level = thinking or DEFAULT_THINKING.get(model)
        tc = (
            types.ThinkingConfig(thinking_level=level)
            if level
            else v._make_thinking_config_for_transcript(types, model)
        )
        usage: dict = {}
        if override:
            v.log.warning(
                "A private prompt override is active: the transcript prompt comes from %s, not the bundled "
                "prompts/. This scorecard measures that prompt, so it is not comparable to a card run against "
                "the shipped template.",
                override,
            )
        t0 = time.time()
        try:
            raw = v.call_gemini(
                client,
                types,
                fx["url"],
                prompt.text,
                model,
                response_json=True,
                start_offset=fx["start"],
                end_offset=fx["start"] + seg,
                thinking_config=tc,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                on_response=lambda r, c=usage, m=model: c.update(
                    v.log_usage_metadata(r, m) or {},
                    # Issue #295: cached with the counts, so a warm re-render keeps it.
                    **{v.SERVED_MODEL_CAPTURE_KEY: served_model_from_response(r)},
                ),
            )
        except Exception as e:
            print(f"  [FAIL]   {stem}: {type(e).__name__}: {str(e)[:140]}")
            # Issue #292: a failed roll is a row, counted, never dropped.
            return {
                "model": model,
                "fixture": fx["id"],
                "error": f"{type(e).__name__}: {str(e)[:160]}",
                **arm_fields,
            }
        wall = time.time() - t0
        usage[CACHE_KEY_FIELD] = key
        raw_p.write_text(raw, encoding="utf-8")
        use_p.write_text(json.dumps(usage), encoding="utf-8")
        cache_state = "live"
        print(f"  [live]   {stem}  {wall:.1f}s")
    row = score(raw, usage, model, seg, wall, start_secs=fx.get("start"), caption_track=caption_track)
    row["fixture"] = fx["id"]
    row["prompt_override"] = override
    row.update(arm_fields, prompt_sha256=prompt.sha256, prompt_source=prompt.source, cache=cache_state)
    return row


def fixture_caption_track(fx: dict, scratch: Path) -> CaptionTrack:
    """The fixture's caption cues, fetched once and cached in ``scratch`` (issue #292).

    Only through ``fetch_english_captions`` (issue #231's one fetcher, with a
    fresh reason sink) and the issue #289 adapter ``caption_cues_from_captions``.
    A refused, absent or failed fetch gives None with its reason and never fails
    the run; only a successful track is cached.
    """
    match = _YOUTUBE_VIDEO_ID.search(str(fx.get("url") or ""))
    if not match:
        return CaptionTrack(None, "no YouTube video id in the fixture URL")
    video_id = match.group(1)
    cache_p = scratch / f"{fx['id']}__captions.json"
    cached = _read_caption_cache(cache_p, video_id)
    if cached is not None:
        return CaptionTrack(v.caption_cues_from_captions(cached))
    reason: dict = {}
    try:
        captions = v.fetch_english_captions(video_id, reason_sink=reason)
    except Exception as e:
        return CaptionTrack(None, f"caption fetch failed ({type(e).__name__})")
    if captions is None:
        if captions_failure_is_refusal(reason.get("kind")):
            return CaptionTrack(
                None,
                f"caption request refused ({reason.get('exception') or 'blocked'}); says nothing about the video",
            )
        return CaptionTrack(None, f"no English caption track ({reason.get('kind') or 'absent'})")
    payload = {
        "video_id": video_id,
        "snippets": [[start, text] for start, text in captions.snippets],
        "durations": list(captions.durations),
        "is_generated": captions.is_generated,
        "language": captions.language,
    }
    try:
        cache_p.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        print(f"  WARNING: caption cache not written ({e}); the next run fetches again.")
    return CaptionTrack(v.caption_cues_from_captions(captions))


def _read_caption_cache(path: Path, video_id: str) -> CaptionsResult | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
        if data.get("video_id") != video_id:
            return None
        return CaptionsResult(
            snippets=[(float(start), str(text)) for start, text in data["snippets"]],
            is_generated=bool(data.get("is_generated")),
            language=str(data.get("language") or "en"),
            durations=tuple(float(d) for d in data.get("durations") or ()),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def cell_measured_its_window(row: dict) -> bool:
    """False only when this cell is KNOWN to have missed its window (#219).

    `window_exceeded` is tri-state and the distinction is load-bearing: `None`
    means no claim was made (the caller passed no `start_secs`, or nothing came
    back to measure), and treating that as a failure would drop every legacy
    cached row out of the verdict. Only an explicit `True` excludes a cell.
    """
    return row.get("window_exceeded") is not True


def served_label(model: str, rows: list[dict]) -> str:
    """``model`` in backticks, plus ``(served X)`` when its cells were answered by another model.

    Issue #295: a verdict between two names that one model served measures that
    model against itself, so the line that compares them must say so.
    """
    served = sorted(
        {
            r["model_served"]
            for r in rows
            if r.get("model") == model and reported_name_differs(model, r.get("model_served"))
        }
    )
    return f"`{model}` (served {', '.join(served)})" if served else f"`{model}`"


def arm_of(row: dict) -> tuple[str, str]:
    """A row's arm: (model, prompt label); a row written before issue #292 is the bundled arm."""
    return (row.get("model"), row.get("prompt_label") or BUNDLED)


def _arm_text(arm: tuple[str, str], rows: list[dict]) -> str:
    model, label = arm
    text = served_label(model, rows)
    return text if label == BUNDLED else f"{text} [prompt {label}]"


def _num(x) -> str:
    if x is None:
        return "-"
    return f"{x:g}" if isinstance(x, float) else str(x)


def roll_stats(values: list) -> tuple:
    """(median, spread) over the rolls that produced a value; one value has spread None, never 0."""
    vals = [x for x in values if x is not None]
    if not vals:
        return None, None
    return statistics.median(vals), (max(vals) - min(vals) if len(vals) >= 2 else None)


def _combine(statuses) -> str:
    statuses = list(statuses)
    if FAIL in statuses:
        return FAIL
    if not statuses or NOT_EVALUATED in statuses:
        return NOT_EVALUATED
    return PASS


def _rule_no_hard_fail(mine: list[dict], theirs: list[dict], fixture: dict) -> tuple[str, str]:
    found = []
    for r in mine:
        fails = hard_fails(r, fixture)
        if fails:
            what = ", ".join(fails)
            if r.get("error"):
                what += f" ({str(r['error']).split(':', 1)[0]})"
            found.append(f"roll {r.get('roll') or 1}: {what}")
    hard = sum(1 for r in theirs if hard_fails(r, fixture))
    note = f"incumbent: {hard} of {len(theirs)} roll(s) hard-failed" if theirs else "no incumbent rolls"
    if found:
        return FAIL, "; ".join(found) + f" ({note})"
    return PASS, f"{len(mine)} roll(s) without a hard fail ({note})"


def _contributing(rows: list[dict], metric: str) -> tuple[list, int, int]:
    """(values, dropped, legacy): the rolls a median may use, and the ones it may not."""
    values, dropped, legacy = [], 0, 0
    for r in rows:
        if r.get("error"):
            continue
        if r.get("cache") == "legacy":
            legacy += 1
        elif not cell_measured_its_window(r) or r.get(metric) is None:
            dropped += 1
        else:
            values.append(r[metric])
    return values, dropped, legacy


def _rule_within_spread(mine: list[dict], theirs: list[dict], metric: str) -> tuple[str, str]:
    """The candidate median against the incumbent median plus the incumbent's own spread."""
    if not theirs:
        return NOT_EVALUATED, "no incumbent rolls for this fixture"
    cand, cand_dropped, cand_legacy = _contributing(mine, metric)
    inc, inc_dropped, inc_legacy = _contributing(theirs, metric)
    notes = []
    if cand_legacy or inc_legacy:
        notes.append(f"{cand_legacy + inc_legacy} legacy-cache roll(s) excluded")
    if inc_dropped:
        notes.append(f"{inc_dropped} incumbent roll(s) without a usable value")
    suffix = f" [{'; '.join(notes)}]" if notes else ""
    if cand_dropped:
        # A dropped bad roll must never flatter the candidate (the issue #228 lesson).
        return (
            NOT_EVALUATED,
            f"{cand_dropped} candidate roll(s) gave no usable {metric} (missed window or None){suffix}",
        )
    if not cand:
        return NOT_EVALUATED, f"the candidate has no {metric}{suffix}"
    if len(inc) < 2:
        return (
            NOT_EVALUATED,
            f"needs --rolls >= 2: the incumbent has {len(inc)} roll(s) with a value, so its spread is unknown{suffix}",
        )
    inc_median, spread = roll_stats(inc)
    cand_median, _ = roll_stats(cand)
    detail = f"candidate {_num(cand_median)} vs incumbent {_num(inc_median)}, tolerance {_num(spread)}{suffix}"
    return (PASS if cand_median <= inc_median + spread else FAIL), detail


def _rule_cost_stated(mine: list[dict], theirs: list[dict]) -> tuple[str, str]:
    def costs(rows: list[dict]) -> list:
        return [
            r["cost_per_video_hour"] for r in rows if not r.get("error") and r.get("cost_per_video_hour") is not None
        ]

    mine_costs = costs(mine)
    if not mine_costs:
        return FAIL, "no cost figure (is the model in PRICING?)"
    return PASS, f"median ${_num(roll_stats(mine_costs)[0])}/video-hour vs ${_num(roll_stats(costs(theirs))[0])}"


def _one_served_model(mine: list[dict], theirs: list[dict], cand_model: str, inc_model: str) -> str | None:
    """The one model that served both arms when two different names were requested (issue #295), else None."""
    if cand_model == inc_model:
        return None
    mine_names = [r["model_served"] for r in mine if r.get("model_served")]
    their_names = [r["model_served"] for r in theirs if r.get("model_served")]
    if not mine_names or not their_names:
        return None
    first = mine_names[0]
    return first if all(not reported_name_differs(first, n) for n in mine_names + their_names) else None


def decision_rule(rows: list[dict], manifest: dict, incumbent_arm: tuple[str, str]) -> dict:
    """The ONE decision rule (issue #292): per candidate arm, per fixture and overall, each rule's verdict.

    R1 no hard fail in any candidate roll. R2 / R3: the candidate's median
    ``max_gap_s`` / caption offset is no worse than the incumbent's median by
    more than the incumbent's own roll-to-roll spread - a measured tolerance,
    not a chosen constant. R4 a cost figure exists. A rule that cannot be judged
    (unknown spread, a None metric, a dropped candidate roll, one model serving
    both names) is NOT EVALUATED, never PASS. Combine order: FAIL, then NOT
    EVALUATED, then PASS.
    """
    by_id = {f["id"]: f for f in manifest.get("fixtures", [])}
    result: dict = {"incumbent": incumbent_arm, "candidates": {}}
    for arm in dict.fromkeys(arm_of(r) for r in rows):
        if arm == incumbent_arm:
            continue
        fixtures: dict = {}
        for fid in dict.fromkeys(r["fixture"] for r in rows if arm_of(r) == arm):
            mine = [r for r in rows if arm_of(r) == arm and r["fixture"] == fid]
            theirs = [r for r in rows if arm_of(r) == incumbent_arm and r["fixture"] == fid]
            rules = {
                "R1": _rule_no_hard_fail(mine, theirs, by_id.get(fid, {})),
                "R2": _rule_within_spread(mine, theirs, "max_gap_s"),
                "R3": _rule_within_spread(mine, theirs, "caption_offset_median_s"),
                "R4": _rule_cost_stated(mine, theirs),
            }
            served = _one_served_model(mine, theirs, arm[0], incumbent_arm[0])
            if served:
                for rid in ("R2", "R3"):
                    rules[rid] = (
                        NOT_EVALUATED,
                        f"both arms were served by {served}: this compares one model with itself",
                    )
            fixtures[fid] = rules
        summary = {
            rid: (
                _combine(f[rid][0] for f in fixtures.values()),
                "; ".join(f"{fid}: {f[rid][1]}" if len(fixtures) > 1 else f[rid][1] for fid, f in fixtures.items()),
            )
            for rid in RULE_IDS
        }
        result["candidates"][arm] = {
            "fixtures": fixtures,
            "rules": summary,
            "overall": _combine(status for status, _ in summary.values()),
        }
    return result


def render(rows: list[dict], models: list[str], manifest: dict, incumbent: str | tuple[str, str] | None) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Model scorecard - {', '.join(models)}", "", f"Generated {stamp} by `scripts/model_eval.py`.", ""]
    out += [
        "`max_gap_s` is the headline: the largest interval between consecutive",
        "timestamps, i.e. the worst-case error of a `&t=<seconds>` deep link.",
        "Lower is better. Two models can emit identical text and identical token",
        "counts while differing several-fold on this number.",
        "",
    ]
    overrides = sorted({r["prompt_override"] for r in rows if r.get("prompt_override")})
    if overrides:
        out += [
            "**Prompt override active.** The transcript prompt came from "
            + ", ".join(f"`{p}`" for p in overrides)
            + ", not the bundled `prompts/`. This card measures that prompt; it is not comparable to a card run "
            "against the shipped template.",
            "",
        ]
    legacy = sum(1 for r in rows if r.get("cache") == "legacy")
    if legacy:
        out += [
            f"**{legacy} cell(s) replayed from a cache entry written before the cache key existed.** Their prompt, "
            "window and thinking level are unverified, so the decision rule leaves them out of its medians.",
            "",
        ]
    by_id = {f["id"]: f for f in manifest["fixtures"]}
    cols = [
        ("fixture", "fixture", 22),
        ("model", "model", 22),
        # Issue #292: one line per roll of each (model, prompt) arm, generated, never hand-written.
        ("prompt", "prompt_label", 10),
        ("roll", "roll", 4),
        ("shape", "envelope_shape", 13),
        ("segs", "segments", 5),
        ("max_gap_s", "max_gap_s", 10),
        ("off_med", "offset_median", 7),
        ("off_p90", "offset_p90", 7),
        ("scr", "screen_content", 4),
        ("spk", "speakers", 4),
        ("think", "thinking_tok", 6),
        ("$/vid-hr", "cost_per_video_hour", 9),
        # Issue #219: a reader of the rendered card must be able to see that a
        # cell did not measure its own window. Without this column the flag
        # existed only in the JSON sidecar, which nothing read.
        ("window", "window_ok", 8),
        ("hard_fail", "hard_fail", 10),
    ]
    out.append("| " + " | ".join(c[0] for c in cols) + " |")
    out.append("|" + "|".join("---" for _ in cols) + "|")
    for r in rows:
        label, roll = arm_of(r)[1], r.get("roll") or 1
        if r.get("error"):
            pad = [""] * (len(cols) - 6)
            out.append(
                "| "
                + " | ".join([r["fixture"], r["model"], label, str(roll), "**ERROR**", *pad, HARD_FAIL_GENERATION])
                + " |"
            )
            out.append("| " + " | ".join(["", "", "", "", f"`{r['error']}`", *pad, ""]) + " |")
            continue
        # Rendered, not just stored: "WINDOW MISSED" is what a human sees.
        r = dict(r)
        r["window_ok"] = (
            "**MISSED**" if r.get("window_exceeded") is True else ("ok" if r.get("window_exceeded") is False else "-")
        )
        r["prompt_label"], r["roll"] = label, roll
        r["offset_median"] = _num(r.get("caption_offset_median_s"))
        r["offset_p90"] = _num(r.get("caption_offset_p90_s"))
        r["hard_fail"] = ", ".join(hard_fails(r, by_id.get(r["fixture"]))) or "-"
        # Issue #295: name the model that answered when it is not the one asked for.
        if reported_name_differs(r["model"], r.get("model_served")):
            r["model"] = f"{r['model']} (served {r['model_served']})"
        out.append("| " + " | ".join(str(r.get(c[1], "")) for c in cols) + " |")
    out += [
        "",
        "## Medians across rolls",
        "",
        "Median (spread) over the rolls that produced a value; spread is `-` with one value, never 0.",
        "",
        "| fixture | model | prompt | rolls | failed | max_gap_s | off_med | off_p90 | $/vid-hr |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for fid, arm in dict.fromkeys((r["fixture"], arm_of(r)) for r in rows):
        group = [r for r in rows if r["fixture"] == fid and arm_of(r) == arm]
        ok = [r for r in group if not r.get("error")]
        cells = []
        for metric in ("max_gap_s", "caption_offset_median_s", "caption_offset_p90_s"):
            median, spread = roll_stats([r.get(metric) for r in ok])
            cells.append(f"{_num(median)} ({_num(spread)})")
        cost = _num(roll_stats([r.get("cost_per_video_hour") for r in ok])[0])
        failed = len(group) - len(ok)
        out.append(f"| {fid} | {arm[0]} | {arm[1]} | {len(group)} | {failed} | {' | '.join(cells)} | {cost} |")
    out += ["", "## Prompts per arm", ""]
    for arm in dict.fromkeys(arm_of(r) for r in rows):
        recorded = next((r for r in rows if arm_of(r) == arm and r.get("prompt_sha256")), None)
        if recorded:
            out.append(
                f"- `{arm[0]}` / `{arm[1]}`: `{recorded.get('prompt_source')}`, sha256 `{recorded['prompt_sha256']}`"
            )
        else:
            out.append(f"- `{arm[0]}` / `{arm[1]}`: not recorded (cells cached before prompts were hashed)")
    out += [
        "",
        "## Caption alignment",
        "",
        "The offset is the absolute difference between a dialogue entry's stamp and the start of the caption cue "
        "holding its first words, at cue level, never word level, computed by `caption_align.entry_offsets`. "
        f"Fewer than {OFFSET_MIN_ALIGNED_ENTRIES} aligned entries leaves it unmeasured.",
        "",
    ]
    for fid in dict.fromkeys(r["fixture"] for r in rows):
        ok = [r for r in rows if r["fixture"] == fid and not r.get("error")]
        reasons = sorted({r["caption_reason"] for r in ok if r.get("caption_reason")})
        counts = [
            f"{r['caption_aligned']}/{r['caption_aligned'] + r['caption_unaligned']}"
            for r in ok
            if r.get("caption_aligned") is not None
        ]
        parts = []
        if counts:
            parts.append("aligned entries per roll: " + ", ".join(counts))
        if reasons:
            parts.append("not measured: " + "; ".join(reasons))
        out.append(f"- **{fid}** - {'; '.join(parts) or 'no caption data'}")
    out += ["", "## Per-facet notes", ""]
    for fid in dict.fromkeys(r["fixture"] for r in rows):
        out.append(f"- **{fid}** - {' '.join((by_id.get(fid, {}).get('facet') or '').split())}")
    if incumbent:
        incumbent_arm = (incumbent, BUNDLED) if isinstance(incumbent, str) else tuple(incumbent)
        inc_model = incumbent_arm[0]
        out += ["", "## Verdict", "", f"Incumbent: `{inc_model}`.", ""]
        # The mean line compares bundled arms only: a prompt run must never
        # average two prompts into one figure (issue #292).
        bundled_rows = [r for r in rows if arm_of(r)[1] == BUNDLED]
        for m in models:
            if m == inc_model:
                continue
            # Issue #219: a cell that did not measure its own window must not
            # feed the comparison that decides a model swap. Its max_gap_s can
            # look healthy (see the test contract) and its cost is derived from
            # tokens billed for a window it did not get.
            mine = [r for r in bundled_rows if r["model"] == m and not r.get("error") and cell_measured_its_window(r)]
            theirs = [
                r
                for r in bundled_rows
                if r["model"] == inc_model and not r.get("error") and cell_measured_its_window(r)
            ]
            excluded = sum(1 for r in bundled_rows if r["model"] in (m, inc_model) and r.get("window_exceeded") is True)
            if excluded:
                out.append(
                    f"- **{excluded} cell(s) excluded from this comparison: the returned span "
                    f"did not match the requested window.** Their numbers describe a different "
                    f"segment than the fixture asked for."
                )
            if not mine or not theirs:
                out.append(f"- `{m}`: insufficient data (a run errored, or every cell missed its window).")
                continue
            g_new = [r["max_gap_s"] for r in mine if r["max_gap_s"] is not None]
            g_old = [r["max_gap_s"] for r in theirs if r["max_gap_s"] is not None]
            c_new = [r["cost_per_video_hour"] for r in mine if r["cost_per_video_hour"]]
            c_old = [r["cost_per_video_hour"] for r in theirs if r["cost_per_video_hour"]]
            am = lambda xs: round(sum(xs) / len(xs), 3) if xs else None  # noqa: E731
            out.append(
                f"- {served_label(m, rows)} vs {served_label(inc_model, rows)}: mean max_gap "
                f"**{am(g_new)}s** vs {am(g_old)}s; mean cost/video-hour "
                f"**${am(c_new)}** vs ${am(c_old)}."
            )
        out += _render_decision(rows, manifest, incumbent_arm)
    max_roll = max((r.get("roll") or 1 for r in rows), default=1)
    out += [
        "",
        "## Not measured here",
        "",
        "State this every time. Timestamp offset is measured at cue level against the caption track only: word-level "
        "drift is not measured, and on an auto-generated track the ruler is itself speech-recognition timing. "
        "Chunked long-video behavior near the 64k output cap is not exercised by a single short segment. "
        "Non-English and heavily accented speech are not represented in the current fixture set. "
        f"Each cell ran {max_roll} roll(s).",
        "",
    ]
    return "\n".join(out)


def _render_decision(rows: list[dict], manifest: dict, incumbent_arm: tuple[str, str]) -> list[str]:
    """The decision block: each rule's PASS / FAIL / NOT EVALUATED and the overall line (issue #292)."""
    out = [
        "",
        "## Decision rule",
        "",
        "Adopt a candidate arm only if it has no hard fail in any roll, its median max_gap_s and median caption "
        "offset are no worse than the incumbent's by more than the incumbent's own roll-to-roll spread, and its "
        "cost is stated. A rule that cannot be judged is NOT EVALUATED, never PASS. PASS does not compare cost or "
        "transcript completeness: read the medians table for those.",
        "",
    ]
    decision = decision_rule(rows, manifest, incumbent_arm)
    if not decision["candidates"]:
        out.append("No candidate arm differs from the incumbent arm, so there is nothing to compare.")
    for arm, verdict in decision["candidates"].items():
        out += [f"### {_arm_text(arm, rows)} vs {_arm_text(incumbent_arm, rows)}", ""]
        for rid, description in RULES:
            status, detail = verdict["rules"][rid]
            out.append(f"- {rid} {description}: {status} - {detail}")
        out += ["", f"Overall: **{verdict['overall']}**", ""]
    return out


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if n < 1:
        raise argparse.ArgumentTypeError(f"--rolls must be at least 1, got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Non-destructive Gemini model and prompt A/B for the transcript step.")
    ap.add_argument("--candidate", required=True, help="Model to evaluate.")
    ap.add_argument(
        "--incumbent",
        help="Model to compare against, or 'bundled' for the candidate model's bundled prompt "
        f"(default: {v.DEFAULT_MODEL}, the model in use today).",
    )
    ap.add_argument("--thinking", help="Force a thinking_level for ALL models (default: per-model correct value).")
    ap.add_argument("--fixtures", help="Comma-separated fixture ids (default: all).")
    ap.add_argument("--segment-seconds", type=int, help="Override segment length.")
    ap.add_argument("--rolls", type=_positive_int, default=1, help="Runs per (fixture x arm) cell (default: 1).")
    ap.add_argument(
        "--candidate-prompt",
        help="A transcript prompt file to run as a second arm for the candidate model, next to the bundled prompt.",
    )
    ap.add_argument("--out", help="Scorecard path (default: tests/evals/model-cards/<candidate>.md).")
    ap.add_argument("--dry-run", action="store_true", help="Print the plan and the cost estimate; call nothing.")
    return ap


def resolve_arms(
    candidate: str, incumbent: str | None, bundled: PromptArm, candidate_prompt: PromptArm | None
) -> tuple[list[tuple[str, PromptArm]], tuple[str, str]]:
    """The arms to run and the incumbent arm (issue #292).

    ``--incumbent bundled`` is the candidate model's bundled-prompt arm; a model
    name is that model's bundled arm; omitted is ``DEFAULT_MODEL`` with the
    bundled prompt. A candidate prompt adds one arm for the candidate model.
    """
    inc_model = candidate if incumbent == BUNDLED else (incumbent or v.DEFAULT_MODEL)
    models = [candidate] + ([inc_model] if inc_model != candidate else [])
    arms = [(m, bundled) for m in models]
    if candidate_prompt is not None:
        arms.append((candidate, candidate_prompt))
    return arms, (inc_model, BUNDLED)


def estimate_cost(models: list[str], fixtures: int, rolls: int, seg: int) -> tuple[float, list[str]]:
    """Dry-run estimate in dollars for one arm per entry of ``models``, and the models with no pricing."""
    total = 0.0
    unknown: list[str] = []
    for model in models:
        pin, pout, _ = PRICING.get(model, (None, None, None))
        if pin is None:
            if model not in unknown:
                unknown.append(model)
            continue
        per_cell = (
            seg / 60 * (EST_INPUT_TOKENS_PER_VIDEO_MINUTE * pin + EST_OUTPUT_TOKENS_PER_VIDEO_MINUTE * pout) / 1e6
        )
        total += per_cell * fixtures * rolls
    return total, unknown


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    manifest = yaml.safe_load(FIXTURES.read_text(encoding="utf-8"))
    if FIXTURES == _FIXTURES_TEMPLATE:
        print(
            f"No fixture set at {_FIXTURES_REAL}.",
        )
        print(
            f"Copy {_FIXTURES_TEMPLATE.name} to {_FIXTURES_REAL.name} and point each "
            "fixture at a real video from your corpus."
        )
        print("The template holds placeholder URLs; running against them would spend Gemini quota on nothing.")
        return 2
    seg = args.segment_seconds or manifest.get("defaults", {}).get("segment_seconds", 600)
    fixtures = manifest["fixtures"]
    if args.fixtures:
        want = {s.strip() for s in args.fixtures.split(",")}
        fixtures = [f for f in fixtures if f["id"] in want]
        if not fixtures:
            print(f"No fixture matched {sorted(want)}. Available: {[f['id'] for f in manifest['fixtures']]}")
            return 2
    # Probe before paying [core: probe-before-pay]: the candidate prompt is read
    # before any client, fetch or call, and before the dry run prints a plan.
    candidate_prompt = None
    if args.candidate_prompt:
        path = Path(args.candidate_prompt)
        if not path.is_file():
            print(f"--candidate-prompt {path} is not a file; nothing was called.")
            return 2
        try:
            candidate_prompt = candidate_prompt_arm(path)
        except (OSError, ValueError) as exc:
            print(f"--candidate-prompt {path} cannot be read as UTF-8 text ({exc}); nothing was called.")
            return 2
    arms, incumbent_arm = resolve_arms(args.candidate, args.incumbent, bundled_prompt(), candidate_prompt)
    models = list(dict.fromkeys(m for m, _ in arms))

    calls = len(fixtures) * len(arms) * args.rolls
    print(f"{len(fixtures)} fixture(s) x {len(arms)} arm(s) x {args.rolls} roll(s) = {calls} calls, {seg}s each")
    for model, prompt in arms:
        print(f"  arm: {model} / {prompt.label}")
    print(f"  incumbent: {incumbent_arm[0]} / {incumbent_arm[1]}")
    for m in models:
        if m not in PRICING:
            print(f"  WARNING: no pricing for {m}; cost columns will be blank.")
    total, unknown = estimate_cost([m for m, _ in arms], len(fixtures), args.rolls, seg)
    print(
        f"Estimated cost: ${total:.4f} for {calls} call(s), at about {EST_INPUT_TOKENS_PER_VIDEO_MINUTE} input and "
        f"{EST_OUTPUT_TOKENS_PER_VIDEO_MINUTE} output tokens per video-minute (docs/cost-model.md). An estimate from "
        "average measured rates that ignores cached cells; output-heavy models can exceed it."
        + (f" Not priced: {', '.join(unknown)}." if unknown else "")
    )
    if args.dry_run:
        for f in fixtures:
            print(f"  {f['id']:24} {f['url']}  start={f['start']}s")
        print("\nDry run: nothing called, nothing written.")
        return 0

    scratch = CARDS / "_raw"
    plan = [(fx, m, p, roll) for fx in fixtures for m, p in arms for roll in range(1, args.rolls + 1)]
    misses = sum(
        1
        for fx, m, p, roll in plan
        if cached_cell(
            scratch,
            cache_stem(fx["id"], m, p.label, roll),
            cache_key(fx, m, p, roll, seg, args.thinking),
            p.label,
            roll,
        )
        is None
    )
    # Issue #292: the client is built only when a cell needs a live call, so a
    # full cache replay needs no key and constructs nothing.
    client = types = None
    if misses:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            print(f"GEMINI_API_KEY not set, and {misses} cell(s) need a live call.")
            return 1
        v.require_gemini()
        from google.genai import types

        client = v.create_client(key)
    else:
        print("Every cell is cached: no Gemini client is built.")
    scratch.mkdir(parents=True, exist_ok=True)

    rows = []
    for fx in fixtures:
        print(f"\n{fx['id']}:")
        track = fixture_caption_track(fx, scratch)
        if track.cues is None:
            print(f"  captions: {track.reason}")
        for m, prompt in arms:
            for roll in range(1, args.rolls + 1):
                rows.append(
                    run_one(
                        client, types, fx, m, args.thinking, seg, scratch, roll=roll, prompt=prompt, caption_track=track
                    )
                )

    CARDS.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else CARDS / f"{args.candidate}.md"
    out_path.write_text(render(rows, models, manifest, incumbent_arm), encoding="utf-8")
    (out_path.with_suffix(".json")).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"\nScorecard: {out_path}")
    errs = [r for r in rows if r.get("error")]
    if errs:
        print(f"{len(errs)} run(s) errored - the scorecard records them rather than omitting them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
