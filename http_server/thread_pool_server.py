"""Concurrency Model A: a fixed-size thread pool.

The accept loop stays on the main thread and just hands each new connection
off to a queue; a fixed number of worker threads pull connections from that
queue and run them through the exact same blocking `handle_connection` used
since Phase 1 -- no new connection-handling logic needed here, only the
concurrency layer around it.

Trade-off worth understanding: this scales to `num_workers` connections
being actively serviced at once. A connection beyond that just waits in the
queue until a worker frees up -- simple and predictable, but each worker
thread has real OS overhead (memory, context-switch cost), so this doesn't
scale to thousands of concurrent connections the way Model B (async,
selectors) can. See async_server.py and README.md Phase 3 for that trade-off
in the other direction.
"""
from __future__ import annotations

import queue
import socket
import threading
from typing import List, Optional

from .connection import Handler, handle_connection


class ThreadPoolHTTPServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        handler: Optional[Handler] = None,
        num_workers: int = 8,
        poll_interval: float = 0.5,
    ):
        if handler is None:
            raise ValueError("ThreadPoolHTTPServer requires a handler function")
        self.host = host
        self.port = port
        self.handler = handler
        self.num_workers = num_workers
        self.poll_interval = poll_interval
        self._queue: "queue.Queue" = queue.Queue()
        self._workers: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._sock: Optional[socket.socket] = None

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                conn, addr = self._queue.get(timeout=self.poll_interval)
            except queue.Empty:
                continue
            try:
                handle_connection(conn, addr, self.handler)
            except Exception:
                # A bug handling one connection must not kill this worker
                # thread -- that would slowly shrink the pool over time.
                try:
                    conn.close()
                except OSError:
                    pass
            finally:
                self._queue.task_done()

    def serve_forever(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(128)
        sock.settimeout(self.poll_interval)
        self._sock = sock
        self._stop_event.clear()

        self._workers = [
            threading.Thread(target=self._worker_loop, daemon=True) for _ in range(self.num_workers)
        ]
        for w in self._workers:
            w.start()

        print(
            f"Listening on http://{self.host}:{self.port} "
            f"(thread pool, {self.num_workers} workers, Phase 3)"
        )

        try:
            while not self._stop_event.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                self._queue.put((conn, addr))
        finally:
            try:
                sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        self._stop_event.set()
