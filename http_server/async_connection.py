"""Per-connection state machine for the non-blocking, selectors-based server.

The blocking version in connection.py can just call sock.recv() and wait
until enough bytes have arrived. A non-blocking socket can't do that -- data
can arrive in arbitrarily small fragments across many separate calls (down
to one byte at a time in the worst case, e.g. a slow client on a bad
connection). This class accumulates bytes across repeated calls to feed()
and only produces a response once a complete request has actually arrived.

It intentionally reuses the exact same parsing rules as connection.py (read
the whole head as one block, then split it -- the fix for the zero-headers
edge case from Phase 1 applies here unchanged), the same keep-alive decision
(`wants_keep_alive`), and the same fail-safe handling of a header-injection
attempt in an outgoing response, rather than re-deriving any of them, so the
concurrency models can't quietly drift into different HTTP behavior.
"""
from __future__ import annotations

import time
from enum import Enum, auto
from typing import Optional, Tuple

from .connection import MAX_BODY_SIZE, MAX_HEAD_SIZE, MAX_REQUEST_LINE, Handler, wants_keep_alive
from .parser import HTTPParseError, HTTPRequest, parse_request
from .response import HeaderInjectionError, error_response


class _State(Enum):
    READING_HEAD = auto()
    READING_BODY = auto()
    WRITING = auto()
    CLOSED = auto()


class AsyncConnection:
    """Tracks parsing/response state for one non-blocking socket connection."""

    def __init__(self, handler: Handler, addr: Optional[Tuple[str, int]] = None):
        self.handler = handler
        self.addr = addr
        self._state = _State.READING_HEAD
        self._read_buf = bytearray()
        self._write_buf = bytearray()
        self._req: Optional[HTTPRequest] = None
        self._content_length: Optional[int] = None
        self._body_start_index = 0
        self._http_version = "HTTP/1.1"
        self.should_close = False
        self.last_active = time.monotonic()
        # Tracks how long the CURRENT in-progress request has been arriving,
        # for the same Slowloris-style defense connection.py applies via a
        # read deadline -- see request_in_progress_seconds and the sweep in
        # async_server.py, which is what actually enforces this for the
        # async model (a per-connection state machine has no loop of its
        # own to check a deadline against; the server's event loop does).
        self._request_started_at = time.monotonic()

    @property
    def wants_write(self) -> bool:
        return len(self._write_buf) > 0

    @property
    def is_closed(self) -> bool:
        return self._state == _State.CLOSED

    @property
    def request_in_progress_seconds(self) -> float:
        """How long the CURRENT request has been arriving. Only meaningful
        while actively reading a head/body; the server's idle sweep should
        only apply this check in that state, not while idle between
        keep-alive requests (that's what last_active / IDLE_TIMEOUT is for).
        """
        return time.monotonic() - self._request_started_at

    @property
    def is_receiving_request(self) -> bool:
        return self._state in (_State.READING_HEAD, _State.READING_BODY)

    def fail_with_timeout(self) -> None:
        """Called by the server's idle sweep when this connection has spent
        too long sending a single request -- the async equivalent of
        connection.py's SlowClientTimeout -> 408 response.
        """
        self._fail(408, "Request took too long to send")

    def feed(self, data: bytes) -> None:
        """Call with newly-received bytes. May advance internal state and,
        once a full request has arrived, populate the outgoing response.
        """
        self.last_active = time.monotonic()
        if not data:
            self._state = _State.CLOSED
            return

        self._read_buf.extend(data)

        if self._state == _State.READING_HEAD:
            self._try_parse_head()

        if self._state == _State.READING_BODY:
            self._try_complete_body()

    def bytes_to_send(self) -> bytes:
        return bytes(self._write_buf)

    def advance_after_send(self, n: int) -> None:
        """Call after successfully sending `n` bytes from bytes_to_send()."""
        del self._write_buf[:n]
        if self._write_buf:
            return

        if self.should_close:
            self._state = _State.CLOSED
            return

        # Reset for the next keep-alive request. Any bytes already sitting
        # in _read_buf belong to a pipelined next request -- try parsing
        # immediately instead of waiting for the next feed() call.
        self._state = _State.READING_HEAD
        self._content_length = None
        self._req = None
        self._request_started_at = time.monotonic()
        if self._read_buf:
            self._try_parse_head()

    def _fail(self, status_code: int, message: str) -> None:
        resp = error_response(status_code, message)
        resp.headers["Connection"] = "close"
        self._write_buf.extend(resp.to_bytes())
        self.should_close = True
        self._state = _State.WRITING

    def _try_parse_head(self) -> None:
        idx = self._read_buf.find(b"\r\n\r\n")
        if idx == -1:
            if len(self._read_buf) > MAX_HEAD_SIZE:
                self._fail(431, "Request head too large")
            return  # not enough data yet -- wait for more

        head = bytes(self._read_buf[:idx])
        body_start = idx + 4

        if head == b"":
            # Lenient-parsing courtesy: a stray leading blank line between
            # keep-alive requests. Drop it and immediately retry in case a
            # full request is already sitting behind it in the buffer.
            del self._read_buf[:body_start]
            self._try_parse_head()
            return

        lines = head.split(b"\r\n")
        request_line, header_lines = lines[0], lines[1:]

        if len(request_line) > MAX_REQUEST_LINE:
            self._fail(414, "Request line too long")
            return

        header_block = b"\r\n".join(header_lines)

        try:
            self._req = parse_request(request_line, header_block, b"")
        except HTTPParseError as e:
            self._fail(e.status_code, e.message)
            return

        self._req.client_addr = self.addr
        self._http_version = self._req.version
        self._body_start_index = body_start

        content_length_header = self._req.header("content-length")
        if content_length_header is not None:
            try:
                content_length = int(content_length_header)
                if content_length < 0:
                    raise ValueError
            except ValueError:
                self._fail(400, "Invalid Content-Length")
                return
            if content_length > MAX_BODY_SIZE:
                self._fail(413, "Body too large")
                return
            self._content_length = content_length
        elif "transfer-encoding" in self._req.headers:
            self._fail(501, "Chunked transfer-encoding not yet supported")
            return
        else:
            self._content_length = 0

        self._state = _State.READING_BODY
        self._try_complete_body()

    def _try_complete_body(self) -> None:
        needed = self._body_start_index + self._content_length
        if len(self._read_buf) < needed:
            return  # still waiting for more body bytes

        body = bytes(self._read_buf[self._body_start_index : needed])
        del self._read_buf[:needed]
        self._req.body = body

        try:
            response = self.handler(self._req)
        except Exception as e:  # a bug in the handler must never take the server down
            response = error_response(500, f"Internal server error: {e}")

        keep_alive = wants_keep_alive(self._req) and response.status_code < 500
        response.headers.setdefault("Connection", "keep-alive" if keep_alive else "close")
        include_body = self._req.method != "HEAD"

        try:
            response_bytes = response.to_bytes(include_body=include_body, http_version=self._http_version)
        except HeaderInjectionError:
            # Same fail-safe as connection.py: never let an injected header
            # reach the wire, even if it means downgrading to a plain 500.
            safe = error_response(500, "Internal server error: invalid response header")
            safe.headers["Connection"] = "close"
            response_bytes = safe.to_bytes()
            keep_alive = False

        self._write_buf.extend(response_bytes)
        self.should_close = not keep_alive
        self._state = _State.WRITING
