# From-Scratch HTTP Server — Phase 5 (stretch)

A production-style HTTP/1.1 server built from raw TCP sockets, with no web
framework (no FastAPI/Flask/Django). This is the companion piece to
`ai-agent-assistant` (which uses FastAPI): that project shows I can use a
professional framework, this one shows I understand what the framework is
actually doing underneath.

## Phase 5 scope (this commit): the stretch goals, all three built

- **WebSockets** (`websocket.py`) — the full RFC 6455 handshake and binary
  frame protocol, hand-written: masking/unmasking, all three payload-length
  encodings (7-bit, 16-bit, 64-bit), ping/pong, and close frames. Verified
  against RFC 6455's own worked example for the handshake math (client key
  `dGhlIHNhbXBsZSBub25jZQ==` -> accept key
  `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`, exactly), and against a real client
  sending one byte at a time to prove the frame reader doesn't assume
  whole frames arrive in one `recv()`. Works in `--mode single` and
  `--mode threaded`; **not implemented for `--mode async`** yet -- same
  honest-limitation shape as TLS+async in Phase 4 (a non-blocking WS frame
  reader would need its own state machine layered on top of the
  HTTP-parsing one `async_connection.py` already is).
- **Rate limiting** (`middleware.py`) — a sliding-window limiter, keyed by
  client IP, that plugs into the same middleware pipeline as logging/gzip/
  CORS. In-memory and per-process by design, stated plainly rather than
  implied to be more robust than it is: fine for a single-process demo
  server, wrong for anything behind a load balancer with multiple server
  processes (that needs shared state, e.g. Redis).
- **Reverse proxy** (`proxy.py`) — forwards requests to another backend and
  relays the response back, the way nginx does. This project had only ever
  written HTTP *server* code before; forwarding a request means being an
  HTTP *client*, so this needed its own small hand-written response
  parser (status line + headers + body) -- the other half of the protocol
  this project hadn't touched yet. Adds `X-Forwarded-For` with the real
  client IP, strips hop-by-hop headers in both directions, and returns a
  proper `502 Bad Gateway` if the backend is unreachable rather than
  crashing.

## Two real bugs found via live testing this phase

1. **Missing `101` status message** — same class of bug as the missing
   `403` in Phase 2. The WebSocket handshake was returning the right
   status *code* but rendering as `101 Unknown` instead of
   `101 Switching Protocols`, because `101` was never added to
   `response.py`'s status-message table. Caught by actually looking at the
   raw handshake bytes from a live server, not just checking `status_code
   == 101` in a test (which would have passed either way).
2. **Duplicate response headers in the proxy** — `Connection`, `Date`,
   `Server`, and `Content-Length` were each appearing **twice** in a
   proxied response. Root cause: the backend's raw response headers were
   parsed into lower-cased dict keys (`"connection"`), and our own
   `response.py` freshly adds its own versions of those same headers using
   `headers.setdefault("Connection", ...)` -- a *properly-cased* key.
   Since Python dict keys are case-sensitive but HTTP header names aren't,
   `setdefault` never recognized them as the same header and both ended up
   on the wire. Fixed by stripping those specific response-level headers
   from the backend's response before merging, since our own code is the
   correct source of truth for all of them anyway. Caught by reading the
   actual bytes of a real proxied response, not by any status-code
   assertion -- worth remembering as a category of bug that only surfaces
   when you look at the real wire output.

## Running it

```bash
python run.py                                     # WS routes work here (single mode)
python run.py --mode threaded                      # WS routes work here too
```

Try the WebSocket echo demo in a browser console once the server's running:
```js
const ws = new WebSocket("ws://127.0.0.1:8080/ws/echo");
ws.onmessage = (e) => console.log(e.data);
ws.onopen = () => ws.send("hello from the browser");
```

Reverse proxy (run as a small standalone script, or adapt into `run.py`):
```python
from http_server.proxy import proxy_to
from http_server.server import HTTPServer
HTTPServer(host="127.0.0.1", port=8081, handler=proxy_to("127.0.0.1", 8080)).serve_forever()
```

## Running the tests

```bash
pip install pytest
python -m pytest -v
```

**136 tests, all passing, zero warnings.** New this phase: `test_websocket.py`
(protocol unit tests plus a real end-to-end handshake-and-echo integration
test), `test_proxy.py` (two real running servers, one proxying to the
other), and new rate-limiting cases in `test_middleware.py`.

## Previous bugs (Phases 1-4) — still documented for context

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

## Project layout

```
http_server/
  reader.py, parser.py, response.py, connection.py    # core + Phases 1-4 hardening
  server.py, thread_pool_server.py, async_server.py     # three concurrency models
  async_connection.py                                    # non-blocking HTTP state machine
  router.py, body_parser.py, static_files.py, errors.py  # Phase 2
  middleware.py                                            # logging, gzip, CORS, NEW: rate limiting
  websocket.py                                              # NEW: RFC 6455 handshake + frames
  proxy.py                                                   # NEW: reverse proxy (HTTP client half)
app.py                    # demo app: routes, /ws/echo, middleware pipeline wired in
generate_cert.py          # self-signed cert generator (openssl)
benchmark.py               # compares all three concurrency models
public/                   # sample static files served at /static/*
run.py                     # entry point -- argparse, --mode, --tls
tests/                     # 136 tests across every module
```

## What's next

- **Phase 6** — load-test benchmarks vs. nginx/uvicorn, Docker, CI, final README
