"""The main server loop: bind, listen, accept connections.

Phase 1 handles one connection fully (including its whole keep-alive
lifetime) before accepting the next -- there's no concurrency yet. Phase 3
replaces this accept loop with a thread-pool version and, separately, an
async event-loop version, and benchmarks the two against each other.

Shutdown design note: this does NOT rely on closing the listening socket
from another thread to interrupt a blocked accept() call. That's a classic
trap -- it happens to raise an exception on Windows (which is what actually
surfaced this bug: a Phase 2 test run showed a background-thread warning
during teardown), but on Linux/POSIX a blocked accept() often just never
wakes up when the socket is closed out from under it -- it's genuinely
undefined, platform-dependent behavior, not something to depend on.

Instead, the listening socket gets a short timeout, so accept() returns
(with a timeout exception, not a real connection) periodically on its own,
letting the loop check a stop flag and exit cleanly on every platform.
"""
from __future__ import annotations

import socket
import threading
from typing import Optional

from .connection import Handler, handle_connection


class HTTPServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        handler: Optional[Handler] = None,
        poll_interval: float = 0.5,
    ):
        if handler is None:
            raise ValueError("HTTPServer requires a handler function")
        self.host = host
        self.port = port
        self.handler = handler
        self.poll_interval = poll_interval
        self._sock: Optional[socket.socket] = None
        self._stop_event = threading.Event()

    def serve_forever(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(128)
        sock.settimeout(self.poll_interval)
        self._sock = sock
        self._stop_event.clear()
        print(f"Listening on http://{self.host}:{self.port} (single-threaded, Phase 1)")

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue  # no connection yet -- loop back and re-check the stop flag
                # handle_connection sets its own timeout on `conn` immediately,
                # so it doesn't inherit this short listening-socket timeout.
                handle_connection(conn, addr, self.handler)
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        """Request a graceful shutdown.

        Just sets the stop flag -- serve_forever's own loop notices it on
        its next timeout tick (at most `poll_interval` seconds later) and
        closes the socket itself, from the thread that owns it. That avoids
        the earlier bug where two threads both tried to close/null out the
        same socket at once.
        """
        self._stop_event.set()
