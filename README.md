# From-Scratch HTTP Server — Phase 6 (final)

A production-style HTTP/1.1 server built from raw TCP sockets, with no web
framework (no FastAPI/Flask/Django). This is the companion piece to
`ai-agent-assistant` (which uses FastAPI): that project shows I can use a
professional framework, this one shows I understand what the framework is
actually doing underneath.

## What this project is

Six phases, each one built, tested, and verified live before moving to the
next:

1. **Core HTTP/1.1** — raw sockets, hand-written request parsing, correct
   responses, keep-alive
2. **Routing & static files** — path params, JSON/form/multipart body
   parsing, path-traversal-safe static file serving
3. **Concurrency** — thread pool and async/selectors event loop, benchmarked
   against the single-threaded baseline
4. **Production hardening** — TLS, middleware (logging/gzip/CORS), HTTP
   caching (ETag/304), Range requests, two real security defenses
   (header-injection, Slowloris)
5. **Stretch goals** — hand-written WebSocket protocol (RFC 6455), rate
   limiting, a reverse proxy
6. **This phase** — real benchmarks against nginx and FastAPI+uvicorn,
   Docker, CI, and this final write-up

## Real benchmark results (this phase), using `wrk`

Not a custom Python load generator this time (that was Phase 3's informal
comparison) — `wrk`, an industry-standard HTTP load-testing tool. Two
separate, fair comparisons rather than one forced three-way test:

**Static files: our server vs nginx** (nginx's actual specialty)

```
                          req/s     avg latency   transfer
ours   (hello.txt, 69B)    7,099      0.140 ms     2.57 MB
ours   (100KB file)        4,569      0.218 ms   447.63 MB
nginx  (hello.txt, 69B)  102,047      0.466 ms    30.66 MB
nginx  (100KB file)       27,978      1.680 ms     2.67 GB
```

nginx wins by 6-14x, exactly as expected — it's a C-based, event-driven
server with decades of production optimization behind it. Not a fair fight
and not meant to be one; the point of running it is to know the actual gap,
not guess at it.

**Dynamic JSON endpoint (`/users/1`): our server vs FastAPI+uvicorn** —
same route, same JSON payload shape, single uvicorn worker (no
multi-process scaling on either side, for a fair apples-to-apples
comparison of the server layer itself)

```
                     req/s     avg latency
ours-single         40,934      0.024 ms
ours-threaded       29,176      0.269 ms
ours-async          28,654      1.680 ms
fastapi+uvicorn      6,370      8.050 ms
```

Our bare implementation wins here — by about 6x. Worth explaining *why*
rather than just reporting it: FastAPI/Starlette do meaningfully more work
per request (Pydantic validation, ASGI protocol layers, dependency
injection, routing machinery, exception-handling middleware) in exchange
for real productivity and safety gains a raw implementation doesn't get.
This project's router does a regex match and a dict lookup. That's the
trade a framework makes, not a flaw in it — and it's exactly the trade this
whole project exists to make visible.

**A second, more surprising finding in the same run**: `ours-single`
beat `ours-threaded` and `ours-async` on this fast endpoint. That's the
opposite of Phase 3's `/slow` result, and it's consistent, not
contradictory: Phase 3 showed the thread pool wins decisively when a
request is genuinely slow/blocking. Here, each request is already so cheap
that the *coordination overhead* of a thread pool (queue put/get, context
switches) or an event loop (selector registration, dispatch) costs more
than it saves. Concurrency machinery isn't free — it pays for itself only
when there's real waiting to hide.

## Docker

```bash
docker build -t from-scratch-http-server .
docker run -p 8080:8080 from-scratch-http-server
docker run -p 8080:8080 from-scratch-http-server --mode threaded
```

No third-party runtime dependencies to install — the whole point of this
project is that the server itself doesn't need any. The image just copies
the source and runs `run.py`.

**Honest caveat, stated plainly**: my working environment doesn't have a
Docker daemon available, so unlike every other feature in this project
(which I ran and verified live), the `Dockerfile` and the CI workflow's
Docker build/smoke-test step were carefully written and reasoned through
but **not run and confirmed by me**. Worth checking the first time you use
it, and worth naming that gap rather than implying a verification that
didn't happen.

## CI (`.github/workflows/ci.yml`)

Runs on every push and PR: installs pytest, runs the full 137-test suite,
and (separately) builds the Docker image and smoke-tests it with a real
`curl` against the running container.

## Running the tests

```bash
pip install pytest
python -m pytest -v
```

**137 tests, all passing, zero warnings**, across parsing, routing, body
parsing, static files (including live path-traversal attempts), all three
concurrency models, middleware, security hardening (a real timed Slowloris
proof, real header-injection attempts), real TLS handshakes, the WebSocket
protocol (including RFC 6455's own worked example), and the reverse proxy.

## Every bug found this project, in order — the honest list

1. **Zero-headers parsing bug** (Phase 1) — reading the request line and
   headers as two separate delimited reads broke for a request with no
   headers at all.
2. **Missing `403` status message** (Phase 2) — path-traversal defense
   returned the right status code but rendered as `403 Unknown`.
3. **Shutdown race, platform-dependent** (Phase 2) — closing the listening
   socket from another thread while `accept()` was blocked raised on
   Windows but silently hung forever on Linux.
4. **Slowloris gap, verified empirically** (Phase 4) — a per-call socket
   timeout alone let a one-byte-at-a-time attacker through completely
   unprotected (~6.9s, normal `200 OK`); a real overall deadline fixed it.
5. **Missing `101` status message** (Phase 5) — same class of bug as #2;
   the WebSocket handshake rendered as `101 Unknown`.
6. **Duplicate response headers in the proxy** (Phase 5) — `Connection`,
   `Date`, `Server`, `Content-Length` each appeared twice because the
   backend's lower-cased header keys and this project's own properly-cased
   ones were never recognized as the same header.

Every one of these was caught by actually running the code and looking at
real output — a live server, a real socket, real bytes on the wire — not
by a test asserting only the thing it was written to check.

## What I'd do differently / do next

- **Async + TLS, async + WebSocket** — both explicitly out of scope,
  documented honestly in Phases 4 and 5 rather than rushed. A non-blocking
  handshake/frame reader needs its own state machine layered onto
  `async_connection.py`; worth doing properly, not quickly.
- **Chunked transfer-encoding** — currently rejected with `501`. Real
  clients/proxies sometimes need it; a real next step, not a permanent gap.
- **Multi-range requests** (`bytes=0-10,20-30`) — falls back to a full
  `200` rather than a `multipart/byteranges` response. Documented, not
  silent.
- **The rate limiter is in-memory, single-process** — fine for this
  project, wrong for anything behind a load balancer with multiple server
  processes; that needs shared state (Redis, etc).
- **Verify the Docker image for real** — the one piece of this project I
  wrote carefully but couldn't run myself.

## Project layout

```
http_server/
  reader.py, parser.py, response.py, connection.py   # core + hardening
  server.py, thread_pool_server.py, async_server.py    # 3 concurrency models
  async_connection.py                                    # non-blocking state machine
  router.py, body_parser.py, static_files.py, errors.py  # routing & files
  middleware.py                                            # logging/gzip/CORS/rate-limit
  websocket.py                                              # RFC 6455
  proxy.py                                                   # reverse proxy
app.py                     # demo app: routes, /ws/echo, middleware pipeline
benchmarks/                # NEW: wrk-driven comparison vs nginx & FastAPI+uvicorn
generate_cert.py           # self-signed cert generator (openssl)
Dockerfile, .dockerignore  # NEW
.github/workflows/ci.yml   # NEW
public/                    # sample static files served at /static/*
run.py                     # entry point -- argparse, --mode, --tls
tests/                     # 137 tests across every module
```
