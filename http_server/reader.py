"""A small buffered reader over a raw socket.

Sockets don't guarantee that recv() returns exactly one HTTP request, a whole
line, or even a whole header at a time -- it returns whatever bytes happen to
be available. This wraps a socket with an internal buffer so the rest of the
server can ask for "the next line" or "exactly N bytes" without worrying
about partial reads, or where one request ends and the next begins (which
matters for keep-alive, where several requests share one connection and one
recv() can return the start of request 2 while we're still parsing request 1).
"""
from __future__ import annotations

import socket
import time
from typing import Optional


class ConnectionClosed(Exception):
    """Raised when the peer closes the connection."""


class RequestTooLarge(Exception):
    """Raised when a request exceeds configured size limits."""


class SlowClientTimeout(Exception):
    """Raised when a read exceeds an overall wall-clock deadline.

    This is distinct from the socket's own per-call timeout. A per-call
    timeout alone doesn't stop a Slowloris-style attacker sending one byte
    every few seconds forever -- each individual recv() succeeds well
    within its own timeout, so the per-call clock keeps resetting and never
    fires. A deadline that covers the WHOLE read (checked before every
    individual recv() attempt) catches that instead.
    """


class BufferedSocketReader:
    def __init__(self, sock: socket.socket, chunk_size: int = 65536):
        self._sock = sock
        self._buf = b""
        self._chunk_size = chunk_size

    def _fill(self) -> None:
        chunk = self._sock.recv(self._chunk_size)
        if not chunk:
            raise ConnectionClosed()
        self._buf += chunk

    def read_until(self, delimiter: bytes, max_size: int, deadline: Optional[float] = None) -> bytes:
        """Read and consume up to and including delimiter; return the bytes before it."""
        while delimiter not in self._buf:
            if len(self._buf) > max_size:
                raise RequestTooLarge()
            if deadline is not None and time.monotonic() > deadline:
                raise SlowClientTimeout()
            self._fill()
        idx = self._buf.index(delimiter)
        result = self._buf[:idx]
        self._buf = self._buf[idx + len(delimiter):]
        return result

    def read_exact(self, n: int, deadline: Optional[float] = None) -> bytes:
        while len(self._buf) < n:
            if deadline is not None and time.monotonic() > deadline:
                raise SlowClientTimeout()
            self._fill()
        result = self._buf[:n]
        self._buf = self._buf[n:]
        return result

    def drain_buffered(self) -> bytes:
        """Returns and clears any bytes already read into the internal
        buffer but not yet consumed.

        Needed when handing a raw socket off to a different protocol after
        HTTP parsing -- e.g. a WebSocket upgrade. If a client sent its first
        WebSocket frame in the same TCP segment as the upgrade request (or
        the OS just happened to deliver them together), those extra bytes
        are sitting in this buffer, not on the wire -- reading straight from
        the socket after handoff would silently skip them.
        """
        result = self._buf
        self._buf = b""
        return result
