"""Scoring harness for mindmap named-entity recall (issue #228).

The defect: given richer transcript input, the mindmap prompt reliably converts
a NAMED thing into a description of that thing. `tt-ali/archify` becomes
"Polished interactive diagrams generated from raw codebase". For a corpus whose
job is answering "which tool was that", losing the name loses the artifact, and
it propagates - `concepts.json` is extracted from the mindmap, so the taxonomy
inherits the generic label.

This module is deliberately free to run: it scores artifacts already on disk and
makes no API call. Only the variant sweep spends anything.

## Why the scoring is a VECTOR, not a number

Tuning a single scalar here is a Goodhart trap with a known shape: a prompt that
names every tool and explains none of them scores perfectly on recall and is a
regression. So every variant is scored on recall PLUS four guards that must not
move, and a variant only wins if it improves recall while holding all of them.

## Ground truth

Built from the description's canonical `owner/repo` URLs plus multi-word proper
nouns in the transcript. High precision, incomplete by construction - so recall
is comparable ACROSS variants on the same sample, and is not an absolute claim
about how many names a video contains.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# A GitHub repo URL is the highest-confidence name there is: canonical, spelled
# by the creator, and unambiguous.
_REPO_URL = re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
# Other product/tool URLs worth mining for a bare product name.
_BARE_DOMAIN = re.compile(r"https?://(?:www\.)?([a-z0-9-]+)\.(?:ai|dev|io|com|app|sh)\b")

# Sentence-initial and all-caps noise that a capitalization heuristic would
# otherwise pick up as a "name".
_STOPWORDS = frozenset(
    [
        "The",
        "A",
        "An",
        "And",
        "But",
        "Or",
        "So",
        "If",
        "Then",
        "Now",
        "Here",
        "There",
        "This",
        "That",
        "These",
        "Those",
        "It",
        "Its",
        "We",
        "You",
        "I",
        "He",
        "She",
        "They",
        "What",
        "When",
        "Where",
        "Why",
        "How",
        "Who",
        "Which",
        "Not",
        "No",
        "Yes",
        "Okay",
        "Like",
        "Just",
        "Really",
        "Very",
        "Much",
        "More",
        "Most",
        "Some",
        "Any",
        "All",
        "One",
        "Two",
        "Three",
        "Four",
        "Five",
        "Six",
        "Seven",
        "Eight",
        "Nine",
        "Ten",
        "First",
        "Second",
        "Third",
        "Next",
        "Last",
        "Also",
        "Even",
        "Still",
        "Only",
        "My",
        "Your",
        "Our",
        "Their",
        "His",
        "Her",
        "Let",
        "Lets",
        "Going",
        "Get",
        "Got",
        "Make",
        "Made",
        "Take",
        "Took",
        "See",
        "Saw",
        "Know",
        "Knew",
        "Think",
        "Thought",
        "Because",
        "Before",
        "After",
        "While",
        "During",
        "Every",
        "Each",
        "Both",
        "Other",
        "Another",
        "Same",
        "Such",
        "Than",
        "Too",
        "Then",
        "Once",
        "SCREEN",
        "Speaker",
        "Source",
        "Note",
        "Video",
        "Title",
        "Published",
        "Transcript",
        "Mind",
        "Map",
        "Chunk",
        "Coverage",
        "Warning",
    ]
)

# Numbers, percentages, prices, star counts - the quantitative detail the
# Gemini-transcript mindmaps are currently GOOD at. Guard metric: tuning for
# names must not cost these.
_QUANTITATIVE = re.compile(
    r"\b\d[\d,.]*\s*(?:%|percent|k|m|b|x|ms|s\b|minutes?|hours?|stars?|tokens?|dollars?)|\$\s*\d"
)
_TIMESTAMP = re.compile(r"\((?:\d+:)?\d{1,3}:\d{2}\)|\(\d{1,3}:\d{2}\)")
_BULLET = re.compile(r"^\s+- ", re.M)
_THEME = re.compile(r"^## ", re.M)
_SUBTHEME = re.compile(r"^\* \*\*", re.M)


def _normalize(name: str) -> str:
    """Casefold and strip punctuation so `Archify`, `archify` and `archify.` match."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


@dataclass
class GroundTruth:
    video_id: str
    channel: str
    prefix: str
    #: Canonical names, each a set of acceptable spellings.
    names: list[set[str]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.names)


def ground_truth_from_artifacts(meta: dict, transcript: str, prefix: str) -> GroundTruth:
    """High-precision name set for one video.

    Deliberately conservative: a false positive here penalizes every variant
    equally but makes the absolute number meaningless, so only names with a
    canonical source (a repo URL, or a repeated multi-word proper noun) count.
    """
    gt = GroundTruth(
        video_id=str(meta.get("video_id") or ""),
        channel=str(meta.get("channel") or ""),
        prefix=prefix,
    )
    description = meta.get("description") or ""

    # 1. owner/repo from the description. The repo NAME is what a mindmap would
    #    plausibly carry; accept the bare repo or the full slug.
    for owner, repo in _REPO_URL.findall(description):
        spellings = {repo, f"{owner}/{repo}"}
        # Repos are often hyphenated where speech is not ("i-have-adhd").
        spellings.add(repo.replace("-", " "))
        gt.names.append({s for s in spellings if len(s) > 2})

    # 2. bare product domains from the description (e.g. https://raindrop.ai)
    for host in _BARE_DOMAIN.findall(description):
        if len(host) > 3 and host not in {"youtube", "github", "google", "twitter"}:
            gt.names.append({host})

    # 3. multi-word proper nouns repeated in the transcript. A single mention is
    #    too weak; two or more means the video is actually about it.
    counts: dict[str, int] = {}
    for m in re.finditer(r"\b([A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+){1,2})\b", transcript):
        phrase = m.group(1)
        if any(w in _STOPWORDS for w in phrase.split()):
            continue
        counts[phrase] = counts.get(phrase, 0) + 1
    for phrase, n in counts.items():
        if n >= 3:
            gt.names.append({phrase})

    # Deduplicate by normalized form, keeping the first spelling set.
    seen: set[str] = set()
    deduped = []
    for group in gt.names:
        keys = {_normalize(s) for s in group}
        if keys & seen:
            continue
        seen |= keys
        deduped.append(group)
    gt.names = deduped
    return gt


def score_mindmap(mindmap: str, gt: GroundTruth) -> dict:
    """Recall plus the four guard metrics. Higher is better for every key."""
    body = _normalize(mindmap)
    hits = sum(1 for group in gt.names if any(_normalize(s) in body for s in group))
    return {
        "named_entity_recall": (hits / gt.size) if gt.size else None,
        "names_found": hits,
        "names_expected": gt.size,
        # --- guards: these must not regress ---
        "quantitative_details": len(_QUANTITATIVE.findall(mindmap)),
        "timestamped_bullets": len(_TIMESTAMP.findall(mindmap)),
        "bullets": len(_BULLET.findall(mindmap)),
        "themes": len(_THEME.findall(mindmap)),
        "subthemes": len(_SUBTHEME.findall(mindmap)),
    }


GUARD_KEYS = ("quantitative_details", "timestamped_bullets", "bullets", "themes", "subthemes")


def aggregate(rows: list[dict]) -> dict:
    """Mean recall over videos that have any ground truth, plus guard totals."""
    scored = [r for r in rows if r.get("named_entity_recall") is not None]
    out = {
        "videos": len(rows),
        "videos_with_ground_truth": len(scored),
        "mean_recall": (sum(r["named_entity_recall"] for r in scored) / len(scored)) if scored else None,
        "names_found": sum(r["names_found"] for r in rows),
        "names_expected": sum(r["names_expected"] for r in rows),
    }
    for k in GUARD_KEYS:
        out[k] = sum(r[k] for r in rows)
    return out


def holds_guards(baseline: dict, candidate: dict, *, tolerance: float = 0.20) -> tuple[bool, list[str]]:
    """A variant may not lose more than `tolerance` of any guard total.

    The tolerance is 0.20 because that is what the MEASUREMENT NOISE turned out
    to be, not a comfort margin. Two rolls of the IDENTICAL prompt over the same
    15-video sample produced `quantitative_details` of 60 and 51 - a 15% swing
    with nothing changed. A 10% gate (the first value used here) therefore
    rejected variants for noise: v3 at 47 and v4 at 44 were reported BROKEN
    and are inside the noise band, while v2 at 37 (-38%) is a real collapse.
    Calibrate this against a repeated control before tightening it; a guard
    stricter than the instrument manufactures false rejections, which in a
    hill-climb means discarding real improvements.
    """
    broken = []
    for k in GUARD_KEYS:
        base = baseline.get(k) or 0
        cand = candidate.get(k) or 0
        if base and cand < base * (1 - tolerance):
            broken.append(f"{k}: {base} -> {cand}")
    return (not broken), broken


def load_sample(corpus: Path, sample: list[tuple[str, str]]) -> list[tuple[GroundTruth, str, str]]:
    """[(ground_truth, transcript_text, on_disk_mindmap_text)] for (channel, prefix) pairs."""
    out = []
    for channel, prefix in sample:
        d = corpus / channel
        meta_p, tx_p, mm_p = (d / f"{prefix}.meta.json", d / f"{prefix}.transcript.md", d / f"{prefix}.mindmap.md")
        if not (meta_p.exists() and tx_p.exists()):
            continue
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        transcript = tx_p.read_text(encoding="utf-8", errors="replace")
        mindmap = mm_p.read_text(encoding="utf-8", errors="replace") if mm_p.exists() else ""
        out.append((ground_truth_from_artifacts(meta, transcript, prefix), transcript, mindmap))
    return out
