"""A pluggable middleware pipeline.

A middleware here is a function that takes the "next" handler in the chain
and returns a new handler wrapping it -- the standard decorator-chain
pattern (the same shape Express, Django, and most other frameworks use).
`apply_middleware` composes a list of them around a base handler, in order,
so the first middleware in the list is the outermost -- it sees the request
first and the response last.
"""
from __future__ import annotations

import gzip as gzip_module
import time
from typing import Callable, List

from .parser import HTTPRequest
from .response import HTTPResponse

Middleware = Callable[["Handler_"], "Handler_"]
Handler_ = Callable[[HTTPRequest], HTTPResponse]


def apply_middleware(handler: Handler_, middlewares: List[Middleware]) -> Handler_:
    for mw in reversed(middlewares):
        handler = mw(handler)
    return handler


def access_log_middleware(log_fn: Callable[[str], None] = print) -> Middleware:
    """Apache/nginx-style access logging: one line per request, after it's handled.

    Format loosely follows the Common Log Format:
        CLIENT_IP - - [DATE] "METHOD PATH VERSION" STATUS BODY_BYTES TIME_MS
    """

    def middleware(next_handler: Handler_) -> Handler_:
        def wrapped(req: HTTPRequest) -> HTTPResponse:
            start = time.monotonic()
            response = next_handler(req)
            elapsed_ms = (time.monotonic() - start) * 1000

            client_ip = req.client_addr[0] if req.client_addr else "-"
            date_str = _log_date()
            log_fn(
                f'{client_ip} - - [{date_str}] "{req.method} {req.path} {req.version}" '
                f"{response.status_code} {len(response.body)} {elapsed_ms:.1f}ms"
            )
            return response

        return wrapped

    return middleware


def _log_date() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).strftime("%d/%b/%Y:%H:%M:%S +0000")


def gzip_middleware(min_size: int = 512) -> Middleware:
    """Compresses the response body with gzip when the client says it accepts
    gzip encoding (Accept-Encoding header) and the body is worth compressing.

    Skips bodies that are already compressed/binary-ish (best-effort, based
    on Content-Type) and anything under `min_size` bytes, where gzip's own
    framing overhead can make the "compressed" output bigger than the
    original -- genuinely counterproductive below a certain size.
    """

    SKIP_CONTENT_TYPES = ("image/", "video/", "audio/", "application/gzip", "application/zip")

    def middleware(next_handler: Handler_) -> Handler_:
        def wrapped(req: HTTPRequest) -> HTTPResponse:
            response = next_handler(req)

            accept_encoding = req.header("accept-encoding", "")
            if "gzip" not in accept_encoding.lower():
                return response
            if len(response.body) < min_size:
                return response
            if "content-encoding" in {k.lower() for k in response.headers}:
                return response  # already encoded by something else (e.g. Range slice)

            content_type = response.headers.get("Content-Type", "")
            if any(content_type.startswith(t) for t in SKIP_CONTENT_TYPES):
                return response

            compressed = gzip_module.compress(response.body)
            if len(compressed) >= len(response.body):
                return response  # not actually a win for this particular body

            response.body = compressed
            response.headers["Content-Encoding"] = "gzip"
            response.headers["Content-Length"] = str(len(compressed))
            existing_vary = response.headers.get("Vary", "")
            if "Accept-Encoding" not in existing_vary:
                response.headers["Vary"] = (existing_vary + ", Accept-Encoding").lstrip(", ")
            return response

        return wrapped

    return middleware


def cors_middleware(
    allow_origin: str = "*",
    allow_methods: str = "GET, POST, PUT, DELETE, HEAD, OPTIONS",
    allow_headers: str = "Content-Type, Authorization",
) -> Middleware:
    """Adds CORS headers to every response, and answers CORS preflight
    (OPTIONS) requests directly without involving the router/handler at all
    -- a preflight request never has a real body to act on anyway.
    """

    def middleware(next_handler: Handler_) -> Handler_:
        def wrapped(req: HTTPRequest) -> HTTPResponse:
            if req.method == "OPTIONS":
                response = HTTPResponse(status_code=204)
            else:
                response = next_handler(req)

            response.headers.setdefault("Access-Control-Allow-Origin", allow_origin)
            response.headers.setdefault("Access-Control-Allow-Methods", allow_methods)
            response.headers.setdefault("Access-Control-Allow-Headers", allow_headers)
            return response

        return wrapped

    return middleware
