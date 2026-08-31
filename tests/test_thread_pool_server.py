"""Tests for the thread-pool concurrency model: multiple connections should
be served genuinely in parallel, not serialized like the Phase 1/2 baseline.
"""
import socket
import threading
import time

import pytest

from http_server.response import make_response
from http_server.thread_pool_server import ThreadPoolHTTPServer

HOST = "127.0.0.1"
PORT = 8199
SLOW_DELAY = 0.2
NUM_WORKERS = 8


def _slow_handler(req):
    time.sleep(SLOW_DELAY)
    return make_response(200, b"ok")


@pytest.fixture(scope="module")
def running_pool_server():
    server = ThreadPoolHTTPServer(
        host=HOST, port=PORT, handler=_slow_handler, num_workers=NUM_WORKERS, poll_interval=0.1
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield server
    server.close()
    thread.join(timeout=3)


def _make_request():
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        sock.settimeout(5)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        return b"".join(chunks)


def test_basic_request_still_works(running_pool_server):
    resp = _make_request()
    assert resp.startswith(b"HTTP/1.1 200 OK")


def test_concurrent_slow_requests_run_in_parallel_not_serially(running_pool_server):
    n = 6  # comfortably within the 8-worker pool
    results = []
    threads = []

    def worker():
        results.append(_make_request())

    start = time.monotonic()
    for _ in range(n):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=5)
    elapsed = time.monotonic() - start

    assert len(results) == n
    assert all(r.startswith(b"HTTP/1.1 200 OK") for r in results)
    # Serialized (single-threaded) would take roughly n * SLOW_DELAY = 1.2s.
    # Run concurrently across the pool, it should take roughly ONE delay's
    # worth of time. Generous margin for CI scheduling noise.
    assert elapsed < n * SLOW_DELAY * 0.6, (
        f"{n} requests took {elapsed:.2f}s -- this looks serialized, not concurrent "
        f"(serial floor would be ~{n * SLOW_DELAY:.2f}s)"
    )


def test_requests_beyond_pool_size_still_all_complete_correctly(running_pool_server):
    # More concurrent requests than worker threads -- some must queue and
    # wait, but every single one must still complete correctly.
    n = NUM_WORKERS * 2
    results = []
    threads = []

    def worker():
        results.append(_make_request())

    for _ in range(n):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=10)

    assert len(results) == n
    assert all(r.startswith(b"HTTP/1.1 200 OK") for r in results)
