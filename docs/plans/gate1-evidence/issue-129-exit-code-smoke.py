"""Gate 1 evidence for issue #129 - the exit code, through the real CLI.

Reproduces the reported case exactly (video ``Ywl4LsvHKzU``): transcript and
mindmap already on disk, the concepts step drops its connection, no
``.concepts.json`` is written - and the process still exited 0, so the batch
driver's exit-code check reported success.

Real ``video_intel.main()``, real argv, real filesystem, real google-genai
client. The ONLY thing redirected is the network endpoint: ``create_client`` is
wrapped to point at a local socket that accepts and closes without responding,
which is what produces the observed ``Server disconnected without sending a
response.``. The process exit code is therefore genuine and observable as ``$?``.

Usage:
    python docs/plans/gate1-evidence/issue-129-exit-code-smoke.py SCRIPTS_DIR OUT_DIR
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
import threading
from pathlib import Path

SCRIPTS_DIR = Path(sys.argv[1])
OUT_DIR = Path(sys.argv[2])
sys.path.insert(0, str(SCRIPTS_DIR))

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

VIDEO_ID = "Ywl4LsvHKzU"
CHANNEL = "smokechannel"
TITLE = "RAG evaluation is broken"
DATE = "2026-06-03"


class DropServer:
    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            with contextlib.suppress(OSError):
                conn.recv(65536)
            conn.close()


def main() -> int:
    import gemini_common
    import video_intel

    server = DropServer()

    # Redirect the endpoint, nothing else. The client is the real one.
    real_create_client = gemini_common.create_client

    def create_client_at_dropserver(api_key, **kw):
        from google.genai import types

        genai, _ = gemini_common.require_gemini()
        return genai.Client(
            api_key=api_key or "smoke",
            http_options=types.HttpOptions(base_url=f"http://127.0.0.1:{server.port}"),
        )

    gemini_common.create_client = create_client_at_dropserver
    video_intel.create_client = create_client_at_dropserver
    assert real_create_client is not create_client_at_dropserver

    # HARD corpus guard. load_config() resolves SKILL_DIR/config.yaml first, and
    # in a normal checkout that points at the real corpus - so a smoke run would
    # write into it. Pin the config to OUT_DIR instead. Everything else about the
    # invocation stays real.
    video_intel.load_config = lambda: {
        "output_dir": str(OUT_DIR),
        "model": "gemini-3-flash-preview",
        "channels": [{"name": CHANNEL, "url": "https://www.youtube.com/@smoke"}],
    }

    # Steps 1 and 2 are already done: their artifacts exist, so process_transcript
    # and process_mindmap short-circuit on their own `exists() and not force`
    # check and never touch the network. Only concepts runs - and it drops.
    channel_dir = OUT_DIR / CHANNEL
    channel_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"{DATE}-{video_intel.slugify(TITLE)}"
    (channel_dir / f"{prefix}.transcript.md").write_text(
        f"# Transcript: {TITLE}\n\n**Source:** https://www.youtube.com/watch?v={VIDEO_ID}\n\n[00:00] Hello.\n",
        encoding="utf-8",
    )
    (channel_dir / f"{prefix}.mindmap.md").write_text(
        f"<!-- video: https://www.youtube.com/watch?v={VIDEO_ID} -->\n\n# {TITLE}\n\n- A branch\n",
        encoding="utf-8",
    )
    (OUT_DIR / "taxonomy.json").write_text('{"version": 1, "concepts": {}}', encoding="utf-8")

    concepts_path = channel_dir / f"{prefix}.concepts.json"
    meta_path = channel_dir / f"{prefix}.meta.json"

    sys.argv = [
        "video_intel.py",
        "process",
        "--url",
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        "--channel",
        CHANNEL,
        "--title",
        TITLE,
        "--date",
        DATE,
    ]

    rc = 0
    try:
        video_intel.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 1

    print("\n" + "=" * 78)
    print("GATE 1 / issue #129 - exit code when a step produced no artifact")
    print("=" * 78)
    print(f"  gemini connections attempted : {server.connections}")
    print(f"  .transcript.md on disk       : {(channel_dir / f'{prefix}.transcript.md').exists()}")
    print(f"  .mindmap.md    on disk       : {(channel_dir / f'{prefix}.mindmap.md').exists()}")
    print(f"  .concepts.json on disk       : {concepts_path.exists()}   <-- the gap")
    if meta_path.exists():
        import json

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(f"  meta.concepts_status         : {meta.get('concepts_status')!r}")
        print(f"  meta.video_id                : {meta.get('video_id')!r}")
    else:
        print("  meta.json                    : ABSENT")
    print(f"\n  PROCESS EXIT CODE            : {rc}")
    print("    pre-fix  : 0  (gap invisible to any batch driver)")
    print("    post-fix : 3  (EXIT_PARTIAL)")
    print("=" * 78)
    return rc


if __name__ == "__main__":
    sys.exit(main())
