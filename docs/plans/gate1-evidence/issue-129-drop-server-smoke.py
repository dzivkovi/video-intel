"""Gate 1 evidence for issue #129 - real transport failure, no mocks.

Stands up a TCP server that accepts a connection and immediately closes it
without writing a response. That is precisely what produces httpx's
``Server disconnected without sending a response.`` - the message observed 7
times in the 2026-08-11/12 bulk ingest.

Everything downstream of the socket is REAL: the real google-genai client, the
real httpx transport, and the real ``call_gemini`` / ``call_gemini_text`` retry
loop from whichever checkout this is run in. Nothing is monkeypatched, so
running the same script against pre-fix and post-fix code is a true A/B.

Usage:
    python docs/plans/gate1-evidence/issue-129-drop-server-smoke.py [SCRIPTS_DIR]

``SCRIPTS_DIR`` defaults to this checkout's ``scripts/``. Pass another
checkout's ``scripts/`` to capture the pre-fix baseline from the same harness.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
import threading
import time
from pathlib import Path

_scripts = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(_scripts))

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")


class DropServer:
    """Accepts, then closes without responding. Counts connections."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            with contextlib.suppress(OSError):
                conn.recv(65536)  # let the client finish sending its request
            conn.close()  # drop it: no status line, no body

    def close(self) -> None:
        self._stop.set()
        self._sock.close()


def build_client(port: int):
    from google.genai import types

    from gemini_common import require_gemini

    genai, _ = require_gemini()
    return genai.Client(
        api_key="smoke-test-not-a-real-key",
        http_options=types.HttpOptions(base_url=f"http://127.0.0.1:{port}"),
    )


def main() -> int:
    import video_intel
    from gemini_common import require_gemini

    _, types = require_gemini()

    print("=" * 78)
    print("GATE 1 / issue #129 - transient transport retry against a real dropped socket")
    print("=" * 78)

    failures = 0

    # --- call_gemini (video path: transcript, mindmap-from-video) -------------
    server = DropServer()
    client = build_client(server.port)
    started = time.monotonic()
    try:
        video_intel.call_gemini(
            client, types, "https://www.youtube.com/watch?v=AAAAAAAAAAA", "prompt", "gemini-3-flash-preview"
        )
        print("UNEXPECTED: call_gemini returned instead of raising")
        failures += 1
    except Exception as exc:  # reporting, not handling
        elapsed = time.monotonic() - started
        print(f"\ncall_gemini      raised : {type(exc).__name__}: {exc}")
        print(f"call_gemini      attempts: {server.connections}  (elapsed {elapsed:.1f}s)")
    finally:
        server.close()
    call_gemini_attempts = server.connections

    # --- call_gemini_text (text path: concepts, mindmap-from-transcript) ------
    server = DropServer()
    client = build_client(server.port)
    try:
        video_intel.call_gemini_text(client, types, "some mindmap text", "gemini-3-flash-preview")
        print("UNEXPECTED: call_gemini_text returned instead of raising")
        failures += 1
    except Exception as exc:
        print(f"\ncall_gemini_text raised : {type(exc).__name__}: {exc}")
        print(f"call_gemini_text attempts: {server.connections}")
    finally:
        server.close()
    call_gemini_text_attempts = server.connections

    # --- the refusal that must NOT be retried --------------------------------
    # A 403 arrives as a real APIError, never as a transport error. Assert the
    # classifier agrees, since this is the invariant issue #129 must not break.
    from google.genai import errors

    from gemini_common import get_retry_delay, is_transient_transport_error

    denied = errors.APIError(403, {"error": {"message": "members only", "status": "PERMISSION_DENIED"}})
    invalid = errors.APIError(400, {"error": {"message": "bad uri", "status": "INVALID_ARGUMENT"}})
    print("\nrefusals stay fail-fast:")
    for label, exc in (("PERMISSION_DENIED (403)", denied), ("INVALID_ARGUMENT (400)", invalid)):
        retryable = get_retry_delay(exc, 0, max_retries_transport=99) is not None
        transportish = is_transient_transport_error(exc)
        print(f"  {label:26} retryable={retryable}  classified_as_transport={transportish}")
        if retryable or transportish:
            failures += 1

    print("\n" + "-" * 78)
    print(
        f"SUMMARY  call_gemini attempts={call_gemini_attempts}  call_gemini_text attempts={call_gemini_text_attempts}"
    )
    print("  pre-fix  expectation: 1 attempt each (no retry - one dropped socket kills the step)")
    print("  post-fix expectation: 2 attempts each (one bounded retry, MAX_RETRIES_TRANSPORT=1)")
    print("-" * 78)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
