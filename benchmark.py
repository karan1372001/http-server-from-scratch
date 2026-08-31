"""Phase 3 benchmark: compares all three concurrency models under real load.

Run with: python benchmark.py

For each server model (single-threaded, thread-pool, async/selectors),
starts the server, fires a batch of concurrent requests at it using a
simple thread-based client, and records total wall-clock time. Two
scenarios run against each model:

  1. FAST requests to "/"      -- raw connection-handling overhead
  2. SLOW requests to "/slow"  -- each takes ~100ms server-side (time.sleep),
                                   simulating an I/O-bound handler like a
                                   slow database call

The SLOW scenario is the interesting one -- see README.md Phase 3 for what
the results actually mean, including the async server's real limitation.
"""
from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import List

from app import app_handler
from http_server.async_server import AsyncHTTPServer
from http_server.server import HTTPServer
from http_server.thread_pool_server import ThreadPoolHTTPServer

HOST = "127.0.0.1"
CONCURRENCY = 20


@dataclass
class Result:
    label: str
    scenario: str
    total_seconds: float
    ok_count: int
    concurrency: int


def _fire_requests(host: str, port: int, path: str, count: int) -> List[bytes]:
    results: List[bytes] = [b""] * count
    threads = []

    def one(i: int):
        try:
            with socket.create_connection((host, port), timeout=10) as sock:
                req = f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
                sock.sendall(req)
                sock.settimeout(10)
                chunks = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                results[i] = b"".join(chunks)
        except OSError as e:
            results[i] = f"ERROR: {e}".encode()

    for i in range(count):
        t = threading.Thread(target=one, args=(i,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=15)

    return results


def _run_scenario(host: str, port: int, path: str, count: int, label: str) -> Result:
    start = time.monotonic()
    results = _fire_requests(host, port, path, count)
    elapsed = time.monotonic() - start
    ok_count = sum(1 for r in results if r.startswith(b"HTTP/1.1 200"))
    return Result(label=label, scenario=path, total_seconds=elapsed, ok_count=ok_count, concurrency=count)


def _benchmark_server(label: str, server, port: int) -> List[Result]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    results = []
    for path in ("/", "/slow"):
        r = _run_scenario(HOST, port, path, CONCURRENCY, label)
        results.append(r)
        print(
            f"  {label:10s} {path:6s} concurrency={CONCURRENCY:3d}  "
            f"{r.total_seconds:6.2f}s total  ({r.ok_count}/{CONCURRENCY} ok)"
        )

    server.close()
    thread.join(timeout=3)
    time.sleep(0.3)  # let the OS release the port before the next server binds it
    return results


def main():
    print(f"Benchmarking with {CONCURRENCY} concurrent requests per scenario\n")

    all_results: List[Result] = []

    print("Single-threaded (Phase 1/2 baseline):")
    all_results += _benchmark_server(
        "single", HTTPServer(host=HOST, port=8301, handler=app_handler, poll_interval=0.1), 8301
    )

    print("\nThread pool (8 workers):")
    all_results += _benchmark_server(
        "threaded",
        ThreadPoolHTTPServer(host=HOST, port=8302, handler=app_handler, num_workers=8, poll_interval=0.1),
        8302,
    )

    print("\nAsync / selectors:")
    all_results += _benchmark_server(
        "async", AsyncHTTPServer(host=HOST, port=8303, handler=app_handler, poll_interval=0.05), 8303
    )

    print("\n" + "=" * 46)
    print(f"{'model':10s} {'scenario':8s} {'time (s)':>10s}")
    print("-" * 46)
    for r in all_results:
        print(f"{r.label:10s} {r.scenario:8s} {r.total_seconds:10.2f}")


if __name__ == "__main__":
    main()
