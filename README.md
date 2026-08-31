# From-Scratch HTTP Server — Phase 4

A production-style HTTP/1.1 server built from raw TCP sockets, with no web
framework (no FastAPI/Flask/Django). This is the companion piece to
`ai-agent-assistant` (which uses FastAPI): that project shows I can use a
professional framework, this one shows I understand what the framework is
actually doing underneath.

## Phase 4 scope (this commit): production-grade features, proven live

Everything from Phase 1-3, plus:

- **HTTPS/TLS** — wraps the accepted socket with Python's `ssl` module.
  Works on both the single-threaded and thread-pool servers (the handshake
  runs inside the *worker* thread for the pool, so a slow handshake can't
  stall other connections). **Not implemented for the async/selectors
  server** — see "Known limitation" below for why, honestly.
- **Middleware pipeline** (`middleware.py`) — a real decorator-chain, not a
  toy: **access logging** (Apache/nginx-style, with the real client IP),
  **gzip compression** (skips bodies that are too small or already
  compressed, so it doesn't make things worse), and **CORS** headers
  (including answering `OPTIONS` preflight requests directly).
- **Caching headers** — `ETag` (a real SHA-1 hash of file content, not just
  metadata) and `Last-Modified`, with correct `304 Not Modified` handling
  for both `If-None-Match` and `If-Modified-Since`.
- **Range requests** — `Range: bytes=...` support with correct `206 Partial
  Content` / `416 Range Not Satisfiable` responses, including open-ended
  and suffix ranges (`bytes=100-`, `bytes=-500`). Multi-range requests
  fall back to a full `200` rather than erroring — documented as
  out of scope rather than silently wrong.
- **Security hardening**, two real defenses, each proven by a test that
  demonstrates the actual attack, not just the happy path:
  1. **Header injection / response splitting** — closed on both sides.
     Incoming: a header value containing a lone `\r` or `\n` (not part of
     a full `\r\n` pair) used to sail through the parser undetected as
     valid ASCII, secretly carrying what looks like an extra header line.
     Outgoing: if a handler ever reflects unsanitized input into a
     response header, the server now refuses to serialize it and fails
     safe with a `500` instead of letting the injection reach the wire.
  2. **Slowloris slow-drip defense** — genuinely new, not just relabeled.
     Phase 1's per-call socket timeout only resets if a connection goes
     completely silent; it does nothing against an attacker sending one
     byte every few seconds forever. Verified this empirically before
     writing it up: with only the old per-call timeout, a request sent one
     byte at a time (0.15s apart) sailed through in ~6.9s with a normal
     `200 OK` — completely unprotected. With the new overall deadline
     (tracked across the whole read, not per `recv()` call), the same
     attack gets cut off in about a second with a `408`.

## Phase 3 recap: concurrency models, benchmarked (still true, still here)

Two concurrency models sit under all of this: a thread pool
(`thread_pool_server.py`) and a single-threaded async event loop using
`selectors` (`async_server.py` + `async_connection.py`). `benchmark.py`
fires 20 concurrent requests at each, against a fast endpoint (`/`) and a
deliberately slow one (`/slow`, `time.sleep(0.1)`):

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

The thread pool gives a real ~6.7x speedup on slow requests. The async
server gives **zero** speedup, statistically identical to the fully
single-threaded baseline -- because the handler still runs synchronously on
the one event-loop thread, so a blocking `time.sleep()` blocks everything
else waiting on that same thread, exactly like Node.js with synchronous
code. The async model's real strength is holding many *idle* connections
cheaply, not speeding up *blocking work* -- a different axis than this
particular benchmark measures.

## Known limitation, stated plainly: no TLS on the async server

Non-blocking TLS is a genuinely different, harder problem than blocking
TLS: the handshake itself needs `SSLWantReadError`/`SSLWantWriteError`
handling and its own mini state machine layered into the selectors event
loop, on top of the request-parsing state machine `async_connection.py`
already is. Bolting that on quickly would risk a half-correct, hard-to-trust
implementation of exactly the kind of security-sensitive code that
shouldn't be half-correct. Documented here as a deliberate scope decision,
not a bug -- and a legitimate answer if asked "what would you do next."

## Running it

```bash
python generate_cert.py                          # one-time: creates cert.pem/key.pem via openssl
python run.py                                     # plain HTTP, single-threaded
python run.py --mode threaded                     # thread pool
python run.py --mode async                        # selectors event loop
python run.py --tls                               # HTTPS (single/threaded only)
python run.py --tls --mode threaded --port 8443
```

## Running the tests

```bash
pip install pytest
python -m pytest -v
```

**110 tests, all passing, zero warnings.** New in this phase:
`test_middleware.py`, `test_security_hardening.py` (including the timed
Slowloris proof), `test_tls.py` (real TLS handshakes over real sockets,
using a freshly-generated cert), and new caching/range cases added to
`test_static_files.py`.

## Verified live, not just tested

Actually ran the server and hit it for real, beyond the test suite:

- **HTTPS**: a real TLS client handshake against the running server,
  encrypted request/response confirmed
- **Gzip**: a 6.7 KB static file came back as 5.2 KB with
  `Content-Encoding: gzip` when requested with `Accept-Encoding: gzip`
- **Range**: `Range: bytes=0-9` returned `206 Partial Content` with
  `Content-Range: bytes 0-9/6756`
- **Caching**: fetched a file, took its `ETag`, requested again with
  `If-None-Match`, got back a correct `304 Not Modified` with an empty body
- **Access log**: printed a real line with the actual client IP:
  `127.0.0.1 - - [31/Aug/2026:12:44:25 +0000] "GET /users/1 HTTP/1.1" 200 35 0.1ms`

## Previous bugs (Phases 1-3) — still documented for context

1. **Zero-headers parsing bug** (Phase 1) — reading the request line and
   headers as two separate delimited reads broke for a request with no
   headers at all.
2. **Missing `403` status message** (Phase 2) — path-traversal defense
   returned the right status code but rendered as `403 Unknown`.
3. **Shutdown race, platform-dependent** (Phase 2) — closing the listening
   socket from another thread while `accept()` was blocked raised on
   Windows but silently hung forever on Linux.

## Project layout

```
http_server/
  reader.py               # buffered socket reader + Slowloris deadline
  parser.py                # raw bytes -> HTTPRequest, incoming CRLF-injection defense
  response.py               # HTTPResponse -> raw bytes, outgoing CRLF-injection defense
  connection.py              # blocking per-connection loop, keep-alive, client_addr
  server.py                  # single-threaded accept loop, optional TLS
  thread_pool_server.py       # concurrency Model A, optional TLS (handshake in worker)
  async_connection.py          # non-blocking request state machine
  async_server.py               # concurrency Model B (selectors event loop)
  router.py                      # path/method matching, path params, catch-all
  body_parser.py                  # JSON / form / multipart parsing
  static_files.py                  # safe serving + ETag/Last-Modified + Range
  errors.py                         # HTML error pages
  middleware.py                     # NEW: logging, gzip, CORS pipeline
app.py                              # demo app, middleware wired in
generate_cert.py                    # NEW: self-signed cert generator (openssl)
benchmark.py                        # compares all three concurrency models
public/                             # sample static files served at /static/*
run.py                              # entry point -- argparse, --mode, --tls
tests/                               # 110 tests across every module
```

## What's next

- **Phase 5** (stretch) — WebSockets, rate limiting, reverse-proxy mode
- **Phase 6** — load-test benchmarks vs. nginx/uvicorn, Docker, CI, final README
