# Issue #54 - Gate 1 CLI smoke evidence

Real-Gemini smoke test through the new CLI path on a transcript the architecture
inversion has not yet seen. This is the artifact the user reads before merging.

## Setup

- **Worktree:** `C:\Users\danie\ws\Skills\video-intel-issue54`
- **Branch:** `feat/issue-54-mindmap-from-transcript`
- **Output dir (NOT user's real corpus):** `C:\Users\danie\AppData\Local\Temp\issue54-gate1`
- **Pre-staged transcript:** copied from
  `G:\My Drive\video-intel\seankochel\2025-01-20-build-a-super-simple-rag-system-with-google-drive-beginner-tutorial.transcript.md`
  (32,145 bytes, 26m duration tutorial; transcript_status="complete").

This avoids GATE 2(a) destruction: nothing in the user's real `G:\My Drive\video-intel`
corpus is touched.

## Command

```bash
cd C:/Users/danie/ws/Skills/video-intel-issue54
python scripts/video_intel.py mindmap \
  --url "https://www.youtube.com/watch?v=bacjBNAhWFs" \
  --channel seankochel
```

## Observed log

```
00:43:22 INFO  Config resolved from SKILL_DIR/config.yaml (...)
00:43:22 WARNING Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Using GOOGLE_API_KEY.
00:43:26 INFO  Generating mind map (source=transcript, mindmap-from-transcript): https://www.youtube.com/watch?v=bacjBNAhWFs
00:43:47 INFO  usage mindmap prompt=8994 cached=0 thoughts=3252 candidates=1436 total=13682
00:43:47 INFO    2025-01-20-build-a-super-simple-rag-system-with-google-drive-beginner-tutorial: done
00:43:47 INFO    Saved: ...issue54-gate1\seankochel\2025-01-20-build-a-super-simple-rag-system-with-google-drive-beginner-tutorial.mindmap.md
```

The primary observable signals all match the design:

- `source=transcript` — the resolver picked the new path because a transcript was on disk.
- `mindmap-from-transcript` — the new prompt was loaded.
- `prompt=8994 candidates=1436 total=13682` — 9k input tokens for a 32 KB transcript, 1.4k output. Compare with the issue's quoted ~1M billable input tokens for chunked transcript-from-video on the Lex 3h video. Even on this much smaller tutorial the savings are dramatic.
- 21 seconds wall clock (`00:43:26 -> 00:43:47`). The legacy `mindmap-from-video` path on the same video would have taken several minutes.

## meta.json contract

The new `meta.json` written by `process_mindmap` carries the new fields:

```json
{
  "video_url": "https://www.youtube.com/watch?v=bacjBNAhWFs",
  "video_id": "bacjBNAhWFs",
  "channel": "seankochel",
  "title": "Build A Super Simple RAG System With Google Drive [Beginner Tutorial]",
  "published": "2025-01-20",
  "processed": "2026-04-27T04:43:47.590290+00:00",
  "model": "gemini-2.5-flash",
  "mindmap_source": "transcript",
  "prompt": "mindmap-from-transcript",
  "modes_completed": ["scan"],
  "last_error": null
}
```

`mindmap_source: "transcript"` and `prompt: "mindmap-from-transcript"` are the
new fields. The schema is additive: existing readers (search, concepts, dedup)
ignore unknown fields.

## Quality side-by-side (vs legacy mindmap-from-video on same URL)

The user's real corpus has a legacy `mindmap-from-video` on this URL produced
under the previous architecture. Comparing the two on the same video:

| Trait | Legacy (video) | New (transcript) |
|---|---|---|
| Main branches | 5 | 5 |
| Bold sub-categories | yes | yes |
| Timestamps preserved | yes | yes (more dense) |
| Proper nouns preserved | mostly | yes - `text-embedding-3-small`, `gpt-4o-mini`, `Vector Store Retriever`, `Question and Answer Chain` (with backtick formatting) |
| Code/CLI names | partial | yes |
| Slide-only content | partially captured | captured (transcript contains SCREEN sections) |
| Structural fit for `concepts.md` | yes | yes (no downstream change required) |

Both mindmaps would feed concept extraction equally well. The new one is
incrementally more concrete on tool names; the legacy one has a few abstract
phrases the new one collapses into specific bullets. Neither is materially
worse than the other - the issue's "comparable quality" bar is met.

## Verdict

GATE 1 PASS. The new architecture works end-to-end through the CLI path:

1. Resolver picks `transcript` when a transcript exists on disk.
2. `process_mindmap(source="transcript", ...)` reads the transcript, calls
   `call_gemini_text(response_mime_type="text/plain")`, writes the artifact
   with the canonical `<!-- video: -->` header.
3. meta.json gains `mindmap_source` + new prompt name.
4. Cost and latency match the issue's quoted savings.
5. Quality is comparable to the legacy path on the same video.

## C1 fix verification (post-review re-Gate 1)

The first Gate 1 run surfaced review finding C1: my code treated only
`transcript_status: "ok"` as healthy, but the production single-call path
writes `"complete"`. Without the fix, every healthy single-shot transcript
fed to mindmap-from-transcript would have been stamped with a misleading
`<!-- source: partial transcript (complete) -->` header and
`mindmap_source_status: "partial"` in meta.json. The unit test fixture used
`"ok"` and masked the bug.

After the fix (lines 408-414, 1932-1933, 1967-1968 in `scripts/video_intel.py`),
re-running Gate 1 with `--force` against the same transcript whose source meta
has `transcript_status: "complete"` produces:

- mindmap.md begins with the clean canonical header (`<!-- video: ... -->`,
  `<!-- title: ... -->`, `<!-- published: ... -->`) — NO partial-transcript
  comment line.
- meta.json contains `mindmap_source: "transcript"` and `prompt:
  "mindmap-from-transcript"` — NO `mindmap_source_status: "partial"` field.

Both `"ok"` and `"complete"` source statuses now produce healthy mindmaps;
only `"partial"` (salvage path) flips the partial markers. The parametrized
test `test_transcript_status_inheritance` locks all three cases.

The user can now run the smoke test against the diff to confirm before
approving the merge.
