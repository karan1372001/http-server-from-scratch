# From-Scratch HTTP Server — Phase 2

A production-style HTTP/1.1 server built from raw TCP sockets, with no web
framework (no FastAPI/Flask/Django). This is the companion piece to
`ai-agent-assistant` (which uses FastAPI): that project shows I can use a
professional framework, this one shows I understand what the framework is
actually doing underneath.

## Phase 2 scope (this commit)

Everything from Phase 1, plus:

- **A real router** — matches path + method to a handler, including path
  parameters (`/users/{id}`) and a catch-all form for static files
  (`/static/{filepath*}`). Registering two methods on the same path (e.g.
  `GET /items` and `POST /items`) works correctly, and a path that matches
  but with the wrong method returns `405` with a proper `Allow` header,
  not a `404`.
- **Request body parsing**: JSON, `application/x-www-form-urlencoded`, and
  `multipart/form-data` (real file uploads). The multipart parser is
  hand-written — boundary detection, per-part headers, filename extraction —
  since that's the genuinely tricky part of an HTTP server most people never
  implement themselves. JSON/urlencoded use the standard library's own
  data-format parsers (`json`, `urllib.parse`), which is different from
  using a web *framework* — see the comment at the top of `body_parser.py`
  for the reasoning.
- **Static file serving** with real path-traversal protection: paths are
  *resolved* against the served root first, then checked for containment —
  not just string-checked for `"../"`, which is the naive version and is
  easy to bypass. Verified live against a real attack attempt (see below).
- **Custom HTML error pages** (404/405/etc.) via `errors.py`, separate from
  the plain-text parse-level errors from Phase 1.

## Project layout

```
http_server/
  reader.py        # buffered socket reader
  parser.py        # raw bytes -> HTTPRequest
  response.py       # HTTPResponse -> raw bytes
  connection.py     # per-connection loop, keep-alive
  server.py         # accept loop (still single-threaded -- Phase 3)
  router.py         # NEW: path/method matching, path params, catch-all
  body_parser.py    # NEW: JSON / form / multipart parsing
  static_files.py   # NEW: safe static file serving
  errors.py         # NEW: HTML error pages
app.py              # demo app wired through the router
public/             # sample static files served at /static/*
run.py              # entry point
tests/
  test_parser.py, test_router.py, test_body_parser.py,
  test_static_files.py, test_integration.py
```

## Running it

```bash
python run.py                  # listens on 127.0.0.1:8080
```

Try in a browser or with curl:

```bash
curl http://127.0.0.1:8080/users/1
curl http://127.0.0.1:8080/static/hello.txt
curl -X POST -d "name=Ada&role=engineer" http://127.0.0.1:8080/form
curl -X POST -F "avatar=@somefile.txt" http://127.0.0.1:8080/upload
```

## Running the tests

```bash
pip install pytest
python -m pytest -v
```

**63 tests, all passing** — unit tests per module (router matching including
catch-all and 405-vs-404 behavior; body parsing including malformed
multipart input; static file serving including path-traversal attempts
against a real temp directory) plus end-to-end integration tests that spin
up the real server and hit it over an actual socket.

## Three real bugs this phase's testing caught

1. **Multipart part-length test typo, not a code bug** — an integration
   test asserted the wrong byte count for an uploaded file's size (miscounted
   by 1). Worth naming honestly: this one was a test-authoring mistake, not
   a server bug, and the fix was to assert against the computed length
   instead of a hardcoded number.
2. **Missing `403` status message** — a live manual test of the
   path-traversal defense (see below) showed the response as `403 Unknown`
   instead of `403 Forbidden`, because `403` was never added to the
   status-message table in `response.py`, even though the code was already
   returning the right status *code*. Easy to miss with unit tests alone
   (they check `status_code`, not the rendered reason phrase) — caught by
   actually looking at the raw bytes a browser would receive.
3. **Shutdown race between threads, and it's platform-dependent** — running
   the test suite on Windows surfaced a background-thread crash during
   teardown (`AttributeError: 'NoneType' object has no attribute 'close'`):
   closing the listening socket from the main thread while the server's own
   thread was blocked inside `accept()` raced against that thread's own
   cleanup code. The first fix attempt (catch the exception `accept()`
   raises when its socket gets closed) turned out to be platform-dependent
   in the wrong direction: it worked on Windows but a follow-up test showed
   that on Linux, closing a socket that another thread is blocked on
   `accept()` for often doesn't interrupt it at all -- it just hangs
   forever, since POSIX leaves that behavior undefined. The real fix was to
   stop depending on that entirely: the listening socket now gets a short
   timeout, so `accept()` wakes up periodically on its own to check a stop
   flag, and only the thread that owns the socket ever closes it. Verified
   with a dedicated regression test (`test_server_shutdown.py`) that starts
   a server, closes it mid-`accept()`, and asserts the thread exits cleanly
   with no exception.

## Verified live: the path-traversal defense actually works

Manually attacked the running server with:

```
GET /static/../app.py HTTP/1.1
```

— an attempt to escape the `public/` folder and read the server's own
source code (which contains the fake user data, so a leak would be obvious).
Result: `403 Forbidden`, no file contents returned. This is what
"resolve first, then check containment" (see `static_files.py`) is for —
checking the raw URL string for `"../"` before resolving is the naive
version and is easier to bypass.

## What's next

- **Phase 3** — two concurrency models (thread pool vs. async event loop via
  `selectors`), benchmarked against each other
- **Phase 4** — TLS, middleware pipeline, caching headers, range requests,
  request-size limits, connection timeouts, access logging
- **Phase 5** (stretch) — WebSockets, rate limiting, reverse-proxy mode
- **Phase 6** — load-test benchmarks vs. nginx/uvicorn, Docker, CI, final README
