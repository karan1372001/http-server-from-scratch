"""Concurrency Model B: a single-threaded event loop using `selectors`.

No OS threads are created per connection -- one thread handles many
connections by reacting to readiness events (socket X has data to read,
socket Y is ready to write) instead of blocking on any single one. This is
the same fundamental approach behind nginx workers, Node.js, and Python's
own asyncio -- non-blocking sockets plus an OS-level readiness notification
mechanism (epoll/kqueue/select, wrapped here by `selectors`).

Real trade-off, not just a claim: this scales to many concurrent
connections with far less per-connection overhead than the thread-pool
model (no thread stack, no context-switch cost per connection). But the
handler functions themselves still run synchronously, on this one thread.
A slow handler -- blocking I/O, CPU work, `time.sleep()` -- blocks the
ENTIRE event loop for its duration, so every other connection waits, even
ones with no relation to that slow request. That's not a bug in this
implementation; it's the actual, general limitation of layering synchronous
handler code under an event loop, and it's exactly why frameworks built on
asyncio require `async def` handlers using non-blocking primitives
(`await asyncio.sleep(...)`, async DB drivers, etc.) rather than ordinary
blocking calls. See the benchmark results in README.md Phase 3, where this
limitation shows up directly in real numbers, not just in this comment.
"""
from __future__ import annotations

import selectors
import socket
import time
from typing import Optional

from .async_connection import AsyncConnection
from .connection import IDLE_TIMEOUT_SECONDS, MAX_REQUEST_READ_SECONDS, Handler


class AsyncHTTPServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        handler: Optional[Handler] = None,
        poll_interval: float = 0.5,
    ):
        if handler is None:
            raise ValueError("AsyncHTTPServer requires a handler function")
        self.host = host
        self.port = port
        self.handler = handler
        self.poll_interval = poll_interval
        self._sel = selectors.DefaultSelector()
        self._listen_sock: Optional[socket.socket] = None
        self._stop_requested = False

    def serve_forever(self) -> None:
        listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listen_sock.bind((self.host, self.port))
        listen_sock.listen(128)
        listen_sock.setblocking(False)
        self._listen_sock = listen_sock
        self._stop_requested = False

        self._sel.register(listen_sock, selectors.EVENT_READ, data=None)
        print(f"Listening on http://{self.host}:{self.port} (async/selectors, Phase 3)")

        try:
            while not self._stop_requested:
                events = self._sel.select(timeout=self.poll_interval)
                for key, mask in events:
                    if key.data is None:
                        self._accept(key.fileobj)
                    else:
                        self._service(key, mask)
                self._sweep_idle_connections()
        finally:
            for key in list(self._sel.get_map().values()):
                try:
                    key.fileobj.close()
                except OSError:
                    pass
            self._sel.close()
            self._sel = selectors.DefaultSelector()  # fresh selector if restarted
            self._listen_sock = None

    def close(self) -> None:
        self._stop_requested = True

    def _accept(self, listen_sock: socket.socket) -> None:
        try:
            conn, addr = listen_sock.accept()
        except OSError:
            return  # spurious wakeup -- nothing actually ready
        conn.setblocking(False)
        state = AsyncConnection(self.handler, addr=addr)
        self._sel.register(conn, selectors.EVENT_READ, data=state)

    def _service(self, key: "selectors.SelectorKey", mask: int) -> None:
        sock: socket.socket = key.fileobj  # type: ignore[assignment]
        state: AsyncConnection = key.data

        if mask & selectors.EVENT_READ:
            try:
                data = sock.recv(65536)
            except BlockingIOError:
                data = None  # nothing actually available this time
            except OSError:
                self._close_connection(sock)
                return

            if data is not None:
                if not data:
                    # Empty recv() on a readable socket means the peer
                    # closed -- distinct from BlockingIOError ("try later").
                    self._close_connection(sock)
                    return

                state.feed(data)

                if state.is_closed:
                    self._close_connection(sock)
                    return

                if state.wants_write:
                    self._sel.modify(sock, selectors.EVENT_READ | selectors.EVENT_WRITE, data=state)

        if mask & selectors.EVENT_WRITE and state.wants_write:
            try:
                sent = sock.send(state.bytes_to_send())
            except BlockingIOError:
                return
            except OSError:
                self._close_connection(sock)
                return

            state.advance_after_send(sent)

            if state.is_closed:
                self._close_connection(sock)
                return

            if not state.wants_write:
                self._sel.modify(sock, selectors.EVENT_READ, data=state)

    def _close_connection(self, sock: socket.socket) -> None:
        try:
            self._sel.unregister(sock)
        except (KeyError, ValueError):
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _sweep_idle_connections(self) -> None:
        now = time.monotonic()
        for key in list(self._sel.get_map().values()):
            state = key.data
            if state is None:
                continue  # the listening socket itself

            if state.is_receiving_request and state.request_in_progress_seconds > MAX_REQUEST_READ_SECONDS:
                # Async equivalent of connection.py's SlowClientTimeout: this
                # connection has been trickling in one request for too long
                # (the actual Slowloris defense -- distinct from plain
                # idleness, which last_active/IDLE_TIMEOUT_SECONDS covers
                # below). Fail it with 408 and make sure that response
                # actually gets flushed by switching this socket to expect
                # a write.
                state.fail_with_timeout()
                if state.wants_write:
                    try:
                        self._sel.modify(key.fileobj, selectors.EVENT_READ | selectors.EVENT_WRITE, data=state)
                    except (KeyError, ValueError):
                        pass
                continue

            if now - state.last_active > IDLE_TIMEOUT_SECONDS:
                self._close_connection(key.fileobj)
