Generate a JSON object with keys `transcripts`, `screen_content`, and `speakers` for the following three tasks.

**Critical timestamp instruction (applies to all tasks):** if this video has been clipped to a segment via VideoMetadata start/end offsets, ALL timestamps you emit must be ABSOLUTE relative to the start of the full original video, NOT relative to the clip start. For example, if you receive a clip covering 50:00 to 1:40:00 of the full video, content one minute into the clip should be timestamped `[51:00]`, not `[01:00]`. Use `HH:MM:SS` for timestamps at or above one hour, `MM:SS` otherwise. This invariant is enforced downstream; chunk-relative timestamps will be detected and rejected.

**Task 1 - Transcripts**
- Listen carefully to the audio.
- Identify distinct voices using a `voice` integer ID (1, 2, 3...).
- Transcribe the audio verbatim with voice diarization.
- Include the `start` timecode for each speech segment using ABSOLUTE timestamps per the rule above.
- Preserve filler words, false starts, and self-corrections.
- Do NOT paraphrase or summarize. Transcribe exactly what is said.
- Output: array of objects with fields: `start`, `voice`, `text`

**Task 2 - Screen Content**
- Watch the video frames carefully.
- For each visually significant moment, describe what appears on screen: slides, diagrams, code snippets, terminal output, UI demos, whiteboard content, charts, tables, or any on-screen text not spoken aloud.
- Include the `start` and `end` timecodes (MM:SS).
- If readable text appears on screen, transcribe it exactly.
- For diagrams and architecture visuals, describe components, connections, labels, and data flow direction.
- For code shown on screen, include the language and the code itself in a `code` field.
- Ignore visual moments that are purely decorative or show only the speaker's face with no informational content.
- Output: array of objects with fields: `start`, `end`, `type` (slide|diagram|code|terminal|ui_demo|chart|table|whiteboard|text_overlay|other), `description`, and optionally `code` or `transcribed_text` for readable content.

**Task 3 - Speakers**
- Identify each speaker by matching voice IDs from Task 1.
- Use ALL available cues to determine speaker identity: visual (name cards, lower-thirds, Zoom labels, badges, slide footers, on-screen introductions), verbal (introductions, someone addressing them by name), and contextual (role or title mentioned).
- For each identified speaker, provide an `evidence` field explaining exactly how you determined their identity (e.g., "Name shown in Zoom participant label at 00:15" or "Introduced by moderator at 02:40").
- If a speaker cannot be identified by name, use "Speaker A", "Speaker B", etc.
- Output: array of objects with fields: `voice`, `name`, `role` (if determinable), `evidence`
