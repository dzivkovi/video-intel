"""Gate 1 for issue #165: do all four duplicate-selection sites prefer a clean
meta over a severe one?

Real inputs: two ACTUAL meta.json files (plus their mindmap/concepts siblings)
copied read-only out of the live corpus. The live corpus currently has zero
duplicate video_id groups and zero severe-flagged metas (verified: 2097 metas,
0 carrying transcript_quality_flags at all), so the scenario this ticket
addresses cannot be observed there directly. It is constructed here the way it
actually arises in production: a title rotation leaves two metas under one
video_id, and one of them was re-transcribed into a severe-quality result.

The severe meta is made to LOOK BETTER on every pre-#165 tiebreak, so a run
that still picks it is unambiguously pre-fix behavior:
  - it sorts lexicographically FIRST (so _find_canonical_meta_by_video_id picks it)
  - it is FIRST in the sorted glob (so _load_video_id_index's first-wins picks it)
  - it has MORE artifacts on disk (so collect_corpus_videos' artifact count picks it)
  - it has a LATER processed timestamp and MORE modes_completed (so _pick_canonical
    would pick it, were it not already severity-aware since #159)

Zero API calls. Never writes to the corpus.
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

WT = Path(sys.argv[1])
sys.path.insert(0, str(WT / "scripts"))
sys.argv = ["x"]
import video_intel as vi  # noqa: E402

CORPUS = Path(r"G:\My Drive\video-intel")
OUT = Path(tempfile.gettempdir()) / "gate1_165_out"
if OUT.exists():
    shutil.rmtree(OUT)
CH = OUT / "natebjones"
CH.mkdir(parents=True)

# Pull one REAL meta out of the live corpus as the template.
src = CORPUS / "natebjones"
template = None
for m in sorted(src.glob("*.meta.json")):
    d = json.loads(m.read_bytes().decode("utf-8"))
    if d.get("video_id") and (src / f"{m.name[: -len('.meta.json')]}.mindmap.md").exists():
        template = (m, d)
        break
assert template, "no usable real meta found"
tmpl_path, tmpl = template
VID = tmpl["video_id"]
print(f"Real corpus meta used as template: {tmpl_path.name[:60]}")
print(f"  video_id={VID}  title={str(tmpl.get('title'))[:50]}")

# a-* sorts first and wins every pre-#165 tiebreak, but is SEVERE.
severe = dict(tmpl)
severe.update(
    {
        "title": "AAA rotated title (severe rerun)",
        "processed": "2026-08-30T23:59:59",
        "modes_completed": ["scan", "mindmap", "transcript", "concepts"],
        "transcript_status": "partial",
        "transcript_quality_flags": ["monolithic_severe"],
        "topics": ["from-severe"],
    }
)
clean = dict(tmpl)
clean.update(
    {
        "title": "ZZZ original title (clean)",
        "processed": "2026-01-01T00:00:00",
        "modes_completed": ["scan", "transcript"],
        "transcript_status": "complete",
        "topics": ["from-clean"],
    }
)
clean.pop("transcript_quality_flags", None)

A, Z = "2026-01-01-aaa-rotated", "2026-01-01-zzz-original"
(CH / f"{A}.meta.json").write_text(json.dumps(severe, indent=2), encoding="utf-8")
(CH / f"{Z}.meta.json").write_text(json.dumps(clean, indent=2), encoding="utf-8")
# Severe side ALSO has more artifacts on disk, so artifact-count would pick it.
real_mm = (src / f"{tmpl_path.name[: -len('.meta.json')]}.mindmap.md").read_text(encoding="utf-8", errors="replace")
(CH / f"{A}.mindmap.md").write_text(real_mm, encoding="utf-8")
(CH / f"{A}.concepts.json").write_text(json.dumps({"concepts": []}), encoding="utf-8")
(CH / f"{Z}.mindmap.md").write_text(real_mm, encoding="utf-8")

results = {}

vi._invalidate_video_id_cache()
results["_load_video_id_index"] = vi._load_video_id_index(CH).get(VID)

results["_find_canonical_meta_by_video_id"] = p.name if (p := vi._find_canonical_meta_by_video_id(CH, VID)) else None

recs = [r for r in vi.collect_corpus_videos(OUT) if r["video_id"] == VID]
results["collect_corpus_videos"] = recs[0]["title"] if recs else None
topics = recs[0]["topics"] if recs else []

metas = [
    (CH / f"{A}.meta.json", severe),
    (CH / f"{Z}.meta.json", clean),
]
results["_pick_canonical"] = vi._pick_canonical(metas)[0].name

print("\nWhich meta each site selected (SEVERE = a-rotated / 'AAA'; CLEAN = z-original / 'ZZZ'):")
ok = True
for site, got in results.items():
    got_s = str(got)
    picked_severe = ("aaa" in got_s.lower()) or ("AAA" in got_s)
    verdict = "SEVERE  <- pre-fix" if picked_severe else "clean   <- post-fix"
    ok = ok and not picked_severe
    print(f"  {site:34s} {verdict}   ({got_s[:46]})")

topics_ok = set(topics) == {"from-clean", "from-severe"}
print(f"\n  collect_corpus_videos topics UNION preserved: {topics_ok}  ({sorted(topics)})")

print("\nRESULT:", "PASS - every site prefers the clean meta" if (ok and topics_ok) else "FAIL")
sys.exit(0 if (ok and topics_ok) else 1)
