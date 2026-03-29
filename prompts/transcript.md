Generate a JSON object with keys `transcripts`, `screen_content`, and `speakers` for the following three tasks.

**Task 1 - Transcripts**
- Listen carefully to the audio.
- Identify distinct voices using a `voice` integer ID (1, 2, 3...).
- Transcribe the audio verbatim with voice diarization.
- Include the `start` timecode (MM:SS) for each speech segment.
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
