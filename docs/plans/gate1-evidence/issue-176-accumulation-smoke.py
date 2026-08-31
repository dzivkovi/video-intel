"""Gate 1 for issue #176: does scan's concepts loop accumulate between videos?

Real inputs: two actual mindmaps + their meta.json from the live corpus.
Stubbed: only call_gemini_text (the network boundary). Zero API spend.
Asserts the SECOND video's prompt contains the concept the FIRST video minted.
"""
import json, shutil, sys, types as pytypes
from pathlib import Path

WT = Path(sys.argv[1])
sys.path.insert(0, str(WT / "scripts"))
sys.argv = ["x"]
import video_intel as vi

CORPUS = Path(r"G:\My Drive\video-intel")
scratch = Path(sys.argv[0]).parent / "gate1_176_out"
if scratch.exists():
    shutil.rmtree(scratch)
ch_out = scratch / "natebjones"
ch_out.mkdir(parents=True)

# Pull two REAL videos (mindmap + meta) out of the live corpus, read-only.
src = CORPUS / "natebjones"
picked = []
for meta in sorted(src.glob("*.meta.json")):
    stem = meta.name[: -len(".meta.json")]
    mm = src / f"{stem}.mindmap.md"
    if mm.exists() and mm.stat().st_size > 500:
        picked.append(stem)
    if len(picked) == 2:
        break
assert len(picked) == 2, picked
for stem in picked:
    shutil.copy(src / f"{stem}.meta.json", ch_out / f"{stem}.meta.json")
    shutil.copy(src / f"{stem}.mindmap.md", ch_out / f"{stem}.mindmap.md")
print(f"Real corpus videos used: {picked[0][:55]} | {picked[1][:55]}")

MINTED = "gate1-minted-concept-alpha"
prompts_seen = []
call_n = {"i": 0}

def fake_call_gemini_text(client, types, text_content, model, **kw):
    prompts_seen.append(text_content)
    call_n["i"] += 1
    # Video 1 mints a brand-new concept; video 2 mints a different one.
    cid = MINTED if call_n["i"] == 1 else "gate1-second-concept"
    return json.dumps({"concepts": [{"concept_id": cid, "preferred_label": cid, "domain": "test"}]})

vi.call_gemini_text = fake_call_gemini_text
vi.require_gemini = lambda: (None, None)
vi.create_client = lambda *a, **k: object()
vi.require_youtube = lambda: (lambda *a, **k: None)
vi.resolve_output_dir = lambda _c: scratch
vi.resolve_model = lambda *a, **k: "stub"
vi.backup_config_if_changed = lambda *a, **k: None
vi.get_channel_id = lambda *a, **k: ("UCx", "Nate")
vi.fetch_channel_videos = lambda *a, **k: []
vi.render_headline_digest = lambda *a, **k: None
vi.load_taxonomy = lambda _o: {"version": 1, "built_from": 0, "concepts": {}}

import os
os.environ.setdefault("GEMINI_API_KEY", "x")
os.environ.setdefault("YOUTUBE_API_KEY", "x")

args = pytypes.SimpleNamespace(
    channel="natebjones", since=None, dry_run=False, force=False,
    chunk_minutes=None, model=None, media_resolution="low",
)
config = {
    "output_dir": str(scratch),
    "channels": [{"name": "natebjones", "url": "https://youtube.com/@n",
                  "auto_transcript": "none", "auto_mindmap": "none", "auto_concepts": True}],
}
vi.cmd_scan(args, config)

print(f"\nGemini concept calls made: {len(prompts_seen)}")
assert len(prompts_seen) == 2, f"expected 2 concept calls, got {len(prompts_seen)}"
first_in_second = MINTED in prompts_seen[1]
first_in_first = MINTED in prompts_seen[0]
print(f"video 1's prompt already contained the minted concept? {first_in_first}  (must be False)")
print(f"video 2's prompt contains video 1's minted concept?     {first_in_second}  (must be True post-fix)")
print("\nRESULT:", "PASS - accumulation reaches the second prompt" if (first_in_second and not first_in_first) else "FAIL - no accumulation")
sys.exit(0 if (first_in_second and not first_in_first) else 1)
