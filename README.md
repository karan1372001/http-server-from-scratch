# From-Scratch HTTP Server — Phase 3

A production-style HTTP/1.1 server built from raw TCP sockets, with no web
framework (no FastAPI/Flask/Django). This is the companion piece to
`ai-agent-assistant` (which uses FastAPI): that project shows I can use a
professional framework, this one shows I understand what the framework is
actually doing underneath.

## Phase 3 scope (this commit): two concurrency models, benchmarked honestly

Everything from Phase 1/2 was single-threaded: one connection fully handled
(including its whole keep-alive lifetime) before the next was even accepted.
Phase 3 adds two different concurrency models on top of the same request
handling, and benchmarks them against each other and against the baseline
-- not just describing the trade-offs, but proving them with real numbers.

- **Thread pool** (`thread_pool_server.py`) — a fixed pool of worker
  threads pulls connections off a queue; each worker runs the same blocking
  `handle_connection` used since Phase 1. Simple, and scales up to
  `num_workers` connections actively in flight at once.
- **Async / selectors** (`async_server.py` + `async_connection.py`) — a
  single thread, non-blocking sockets, and Python's `selectors` module
  (the same underlying mechanism — epoll/kqueue — behind nginx and
  Node.js). This required a genuinely different implementation, not just a
  wrapper: a non-blocking socket can deliver a request one byte at a time
  across many separate `recv()` calls, so `async_connection.py` is a state
  machine that accumulates bytes and only responds once a full request has
  arrived, instead of connection.py's approach of just blocking until
  enough bytes show up.

### The benchmark (`benchmark.py`) — real results, not estimates

20 concurrent requests fired at each server model, two scenarios:
`GET /` (fast) and `GET /slow` (a demo endpoint that does `time.sleep(0.1)`
to simulate an I/O-bound handler, like a slow database call).

```
model      scenario   time (s)
----------------------------------------------
single     /              0.01
single     /slow          2.01
threaded   /              0.01
threaded   /slow          0.30
async      /              0.01
async      /slow          2.02
```

**Fast requests**: all three models are indistinguishable — the work per
request is trivial, so there's nothing for any concurrency model to help
with.

**Slow requests, and the actually interesting result**: the thread pool
gives a real ~6.7x speedup (2.01s → 0.30s, roughly what you'd expect from
20 requests over 8 workers). The async server gives **zero** speedup
(2.02s — statistically identical to the fully single-threaded baseline).

That's not a bug — it's the honest, important limitation of this
implementation, worth understanding rather than glossing over: the async
server avoids per-connection *thread* overhead, but the handler function
(`time.sleep(0.1)` in this case) still runs synchronously on the *one*
event-loop thread. While it sleeps, the whole loop is blocked — every other
connection, however unrelated, has to wait. This is the exact same
limitation Node.js or any single-threaded event loop has with synchronous
code. A truly async-fast version of `/slow` would need `async def` handlers
using non-blocking primitives (`await asyncio.sleep(...)`, an async DB
driver, etc.) — not blocking calls layered under a selectors loop, which is
what this project intentionally built to make the limitation visible.

**What each model is actually good for**, put plainly: the thread pool is
the right tool when handlers do blocking work (which is most real
handlers, including this one). The async model's real strength is holding
large numbers of *idle or slow-to-send* connections cheaply — thousands of
open sockets waiting on network I/O — without needing a thread per
connection, which is a different axis entirely from the CPU/blocking-work
scenario this benchmark measures. That's the C10K-style scaling case, and
it's why real async frameworks pair the event loop with async-aware
handler code rather than ordinary blocking functions.

## Project layout

```
http_server/
  reader.py            # buffered socket reader (blocking model)
  parser.py            # raw bytes -> HTTPRequest
  response.py          # HTTPResponse -> raw bytes
  connection.py         # blocking per-connection loop, keep-alive
  server.py             # single-threaded accept loop (Phase 1/2 baseline)
  router.py             # path/method matching, path params, catch-all
  body_parser.py         # JSON / form / multipart parsing
  static_files.py        # safe static file serving
  errors.py              # HTML error pages
  thread_pool_server.py  # NEW: concurrency Model A
  async_connection.py    # NEW: non-blocking request state machine
  async_server.py         # NEW: concurrency Model B (selectors event loop)
app.py                   # demo app, includes /slow for the benchmark
benchmark.py              # NEW: compares all three models under load
public/                  # sample static files served at /static/*
run.py                   # entry point -- pick single/threaded/async
tests/                   # 77 tests across every module
```

## Running it

```bash
python run.py 127.0.0.1 8080 single     # Phase 1/2 behavior (default)
python run.py 127.0.0.1 8080 threaded   # thread pool, 8 workers
python run.py 127.0.0.1 8080 async      # selectors event loop
python benchmark.py                     # runs the comparison above
```

## Running the tests

```bash
pip install pytest
python -m pytest -v
```

**77 tests, all passing, zero warnings.** New in this phase:
`test_thread_pool_server.py` proves genuine parallelism by timing (not just
correctness) — concurrent slow requests must complete in a fraction of the
serial-would-take time, or the test fails. `test_async_server.py` unit-tests
the state machine directly against deliberately fragmented input (one byte
at a time, split bodies, pipelined requests), plus integration tests
including a real slow-client simulation over an actual socket.

## Previous bugs (Phase 1 & 2) — still documented for context

1. **Zero-headers parsing bug** (Phase 1) — reading the request line and
   headers as two separate delimited reads broke for a request with no
   headers at all. Fixed by reading the whole head as one block and
   splitting it afterward.
2. **Missing `403` status message** (Phase 2) — path-traversal defense
   returned the right status *code* but rendered as `403 Unknown` because
   `403` was missing from the status-message table.
3. **Shutdown race, and it's platform-dependent** (Phase 2) — closing the
   listening socket from another thread while `accept()` was blocked raised
   on Windows but silently hung forever on Linux. Fixed by using a
   short-timeout-plus-stop-flag design instead of depending on that
   undefined behavior.

## What's next

- **Phase 4** — TLS, middleware pipeline, caching headers, range requests,
  request-size limits, structured access logging
- **Phase 5** (stretch) — WebSockets, rate limiting, reverse-proxy mode
- **Phase 6** — load-test benchmarks vs. nginx/uvicorn, Docker, CI, final README
