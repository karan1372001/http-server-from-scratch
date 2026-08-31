"""Phase 6 benchmark: our server vs nginx (static files) and vs
FastAPI+uvicorn (a dynamic JSON endpoint), using `wrk` -- a real,
industry-standard HTTP load-testing tool -- not a custom Python load
generator like the informal comparison in Phase 3's benchmark.py.

Two separate, fair comparisons rather than one forced three-way test:
  1. STATIC FILES: our server vs nginx, nginx's actual specialty.
  2. DYNAMIC JSON: our server vs FastAPI+uvicorn, serving the exact same
     route and JSON payload shape (see fastapi_app.py) -- the point is to
     compare the SERVER layer, not different application logic.

Requires `wrk`, `nginx`, and `uvicorn`/`fastapi` installed. Run with:
    python benchmarks/run_comparison.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import router
from http_server.async_server import AsyncHTTPServer
from http_server.server import HTTPServer
from http_server.thread_pool_server import ThreadPoolHTTPServer

ROOT = Path(__file__).resolve().parent.parent
WRK_DURATION = "5s"
WRK_CONNECTIONS = "50"
WRK_THREADS = "4"

# Deliberately the BARE router, not app_handler: app_handler includes rate
# limiting (300 req/60s) and per-request stdout access logging, both correct
# in production but both wrong to have active during a load test -- a real
# load test needs to be bypassing rate limits (same as real-world practice)
# and shouldn't have print() on the hot path skewing results with I/O.
bench_handler = router.as_handler()


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        print(f"'{name}' is not installed/on PATH -- can't run this benchmark. Skipping.")
        sys.exit(1)


def run_wrk(url: str) -> str:
    result = subprocess.run(
        ["wrk", "-t", WRK_THREADS, "-c", WRK_CONNECTIONS, "-d", WRK_DURATION, "--latency", url],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout


def parse_wrk_output(output: str) -> dict:
    rps_match = re.search(r"Requests/sec:\s+([\d.]+)", output)
    latency_match = re.search(r"Latency\s+([\d.]+)(us|ms|s)", output)
    transfer_match = re.search(r"Transfer/sec:\s+([\d.]+\S*)", output)

    latency_ms = None
    if latency_match:
        val, unit = float(latency_match.group(1)), latency_match.group(2)
        latency_ms = val / 1000 if unit == "us" else (val * 1000 if unit == "s" else val)

    return {
        "requests_per_sec": float(rps_match.group(1)) if rps_match else None,
        "avg_latency_ms": round(latency_ms, 3) if latency_ms is not None else None,
        "transfer_per_sec": transfer_match.group(1) if transfer_match else None,
    }


def print_result(label: str, parsed: dict) -> None:
    rps = parsed["requests_per_sec"]
    lat = parsed["avg_latency_ms"]
    xfer = parsed["transfer_per_sec"]
    print(f"  {label:28s} {rps:>10.1f} req/s   {lat:>8.3f} ms avg latency   {xfer}")


# --- Comparison 1: static files, our server vs nginx ---


def benchmark_static_files():
    print("\n" + "=" * 70)
    print("STATIC FILES: our server vs nginx")
    print("=" * 70)

    results = {}

    # Our server, single-threaded (Phase 1/2 baseline concurrency model)
    server = HTTPServer(host="127.0.0.1", port=8081, handler=bench_handler, poll_interval=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    for fname in ("hello.txt", "bench_100k.bin"):
        out = run_wrk(f"http://127.0.0.1:8081/static/{fname}")
        results[f"ours ({fname})"] = parse_wrk_output(out)

    server.close()
    thread.join(timeout=3)
    time.sleep(0.3)

    # nginx, serving the identical files from the identical public/ directory
    nginx_proc = subprocess.Popen(
        ["nginx", "-c", str(ROOT / "benchmarks" / "nginx.conf"), "-p", str(ROOT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)

    for fname in ("hello.txt", "bench_100k.bin"):
        out = run_wrk(f"http://127.0.0.1:8070/{fname}")
        results[f"nginx ({fname})"] = parse_wrk_output(out)

    nginx_proc.terminate()
    nginx_proc.wait(timeout=5)
    time.sleep(0.3)

    for label, parsed in results.items():
        print_result(label, parsed)

    return results


# --- Comparison 2: dynamic JSON endpoint, our server (all 3 modes) vs FastAPI+uvicorn ---


def benchmark_dynamic_endpoint():
    print("\n" + "=" * 70)
    print("DYNAMIC JSON ENDPOINT (/users/1): our server (3 modes) vs FastAPI+uvicorn")
    print("=" * 70)

    results = {}

    # Our server -- all three concurrency models, same route, same handler code
    configs = [
        ("ours-single", HTTPServer(host="127.0.0.1", port=8082, handler=bench_handler, poll_interval=0.1)),
        (
            "ours-threaded",
            ThreadPoolHTTPServer(host="127.0.0.1", port=8083, handler=bench_handler, num_workers=8, poll_interval=0.1),
        ),
        ("ours-async", AsyncHTTPServer(host="127.0.0.1", port=8084, handler=bench_handler, poll_interval=0.05)),
    ]

    for label, server in configs:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.3)
        out = run_wrk(f"http://127.0.0.1:{server.port}/users/1")
        results[label] = parse_wrk_output(out)
        server.close()
        thread.join(timeout=3)
        time.sleep(0.3)

    # FastAPI + uvicorn, single worker (fair: no multi-process comparison)
    uvicorn_proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "benchmarks.fastapi_app:app",
            "--host", "127.0.0.1", "--port", "8090", "--log-level", "warning",
        ],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)  # uvicorn/FastAPI takes a bit longer to fully start than our own server

    out = run_wrk("http://127.0.0.1:8090/users/1")
    results["fastapi+uvicorn"] = parse_wrk_output(out)

    uvicorn_proc.terminate()
    uvicorn_proc.wait(timeout=5)

    for label, parsed in results.items():
        print_result(label, parsed)

    return results


def main():
    require_tool("wrk")
    require_tool("nginx")

    static_results = benchmark_static_files()
    dynamic_results = benchmark_dynamic_endpoint()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print("\nStatic files:")
    for label, parsed in static_results.items():
        print_result(label, parsed)
    print("\nDynamic JSON endpoint:")
    for label, parsed in dynamic_results.items():
        print_result(label, parsed)


if __name__ == "__main__":
    main()
