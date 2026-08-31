"""Handles a single client connection: read request(s), call the handler, write response(s).

Owns the keep-alive loop: after each response, decides -- based on the
request's HTTP version and Connection header -- whether to read another
request off the same socket or close it.

Phase 5 adds an optional WebSocket upgrade hook: if the request is a valid
WebSocket handshake AND its path has a registered WS handler, this hands
the raw socket off entirely to that handler (via WebSocketConnection) and
the normal HTTP request/response loop never resumes on this connection.
"""
from __future__ import annotations

import socket
import time
from typing import Callable, Dict, Optional

from .parser import HTTPParseError, HTTPRequest, parse_request
from .reader import BufferedSocketReader, ConnectionClosed, RequestTooLarge, SlowClientTimeout
from .response import HeaderInjectionError, HTTPResponse, error_response
from .websocket import (
    WebSocketConnection,
    WSHandler,
    build_handshake_response,
    is_websocket_upgrade_request,
)

MAX_REQUEST_LINE = 8192
MAX_HEAD_SIZE = 64 * 1024  # request line + all headers combined
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB -- generous placeholder
IDLE_TIMEOUT_SECONDS = 30  # max time with ZERO bytes at all between reads
MAX_REQUEST_READ_SECONDS = 10  # max WALL-CLOCK time to receive one whole request -- Slowloris defense
WEBSOCKET_IDLE_TIMEOUT_SECONDS = 300  # generous -- WS connections are legitimately long-lived/idle

Handler = Callable[[HTTPRequest], HTTPResponse]


def wants_keep_alive(req: HTTPRequest) -> bool:
    connection_header = (req.header("connection") or "").lower()
    if connection_header:
        return connection_header != "close"
    # HTTP/1.1 defaults to keep-alive; HTTP/1.0 defaults to close.
    return req.version == "HTTP/1.1"


def handle_connection(
    sock: socket.socket,
    addr,
    handler: Handler,
    ws_routes: Optional[Dict[str, WSHandler]] = None,
) -> None:
    sock.settimeout(IDLE_TIMEOUT_SECONDS)
    reader = BufferedSocketReader(sock)

    try:
        while True:
            # Fresh deadline per REQUEST, not per connection -- a keep-alive
            # connection is allowed to sit idle between requests (up to
            # IDLE_TIMEOUT_SECONDS), but once a client starts sending one, it
            # has to actually finish sending it within this window. See
            # SlowClientTimeout in reader.py for why this needs to be a
            # separate check from the socket's own per-call timeout.
            deadline = time.monotonic() + MAX_REQUEST_READ_SECONDS

            # Read the whole head (request line + all headers) as one block,
            # up to the blank line that separates it from the body. Splitting
            # it AFTER reading -- rather than reading the request line and
            # headers as two separate delimited reads -- avoids an off-by-one
            # trap: with zero headers, the request line's own CRLF plus the
            # blank line's CRLF don't add up to a clean "\r\n\r\n" once the
            # request line has already been consumed separately.
            try:
                head = reader.read_until(b"\r\n\r\n", max_size=MAX_HEAD_SIZE, deadline=deadline)
            except ConnectionClosed:
                return  # peer closed between requests -- normal on keep-alive
            except socket.timeout:
                return  # idle too long -- close quietly
            except SlowClientTimeout:
                _send_and_close(sock, error_response(408, "Request took too long to send"))
                return
            except RequestTooLarge:
                _send_and_close(sock, error_response(431, "Request head too large"))
                return

            if head == b"":
                # Some clients send a stray leading blank line between
                # keep-alive requests (allowed by spec as a lenient-parsing
                # courtesy, RFC 7230 3.5) -- skip it and read the real head.
                try:
                    head = reader.read_until(b"\r\n\r\n", max_size=MAX_HEAD_SIZE, deadline=deadline)
                except (ConnectionClosed, socket.timeout):
                    return
                except SlowClientTimeout:
                    _send_and_close(sock, error_response(408, "Request took too long to send"))
                    return
                except RequestTooLarge:
                    _send_and_close(sock, error_response(431, "Request head too large"))
                    return

            lines = head.split(b"\r\n")
            request_line, header_lines = lines[0], lines[1:]

            if len(request_line) > MAX_REQUEST_LINE:
                _send_and_close(sock, error_response(414, "Request line too long"))
                return

            header_block = b"\r\n".join(header_lines)

            try:
                req = parse_request(request_line, header_block, b"")
            except HTTPParseError as e:
                _send_and_close(sock, error_response(e.status_code, e.message))
                return

            req.client_addr = addr

            if ws_routes and req.path in ws_routes and is_websocket_upgrade_request(req):
                # WebSocket upgrade requests never have a body -- go
                # straight to the handshake, no Content-Length handling.
                _handle_websocket_upgrade(sock, reader, req, ws_routes[req.path])
                return  # this connection now belongs entirely to the WS handler

            content_length_header = req.header("content-length")
            if content_length_header is not None:
                try:
                    content_length = int(content_length_header)
                    if content_length < 0:
                        raise ValueError
                except ValueError:
                    _send_and_close(sock, error_response(400, "Invalid Content-Length"))
                    return

                if content_length > MAX_BODY_SIZE:
                    _send_and_close(sock, error_response(413, "Body too large"))
                    return

                try:
                    req.body = reader.read_exact(content_length, deadline=deadline)
                except (ConnectionClosed, socket.timeout):
                    return
                except SlowClientTimeout:
                    _send_and_close(sock, error_response(408, "Request took too long to send"))
                    return
            elif "transfer-encoding" in req.headers:
                # Chunked transfer-encoding is out of scope for Phase 1.
                _send_and_close(sock, error_response(501, "Chunked transfer-encoding not yet supported"))
                return

            try:
                response = handler(req)
            except Exception as e:  # a bug in the handler must never take the server down
                response = error_response(500, f"Internal server error: {e}")

            keep_alive = wants_keep_alive(req) and response.status_code < 500
            response.headers.setdefault("Connection", "keep-alive" if keep_alive else "close")

            include_body = req.method != "HEAD"
            try:
                response_bytes = response.to_bytes(include_body=include_body, http_version=req.version)
            except HeaderInjectionError:
                # A handler (or middleware) tried to send a header value
                # with illegal control characters in it -- almost certainly
                # unsanitized user input being reflected back. Never let
                # that reach the wire; fail safe with a plain 500 instead.
                safe = error_response(500, "Internal server error: invalid response header")
                safe.headers["Connection"] = "close"
                response_bytes = safe.to_bytes()
                keep_alive = False

            try:
                sock.sendall(response_bytes)
            except OSError:
                return

            if not keep_alive:
                return
    finally:
        sock.close()


def _handle_websocket_upgrade(
    sock: socket.socket, reader: BufferedSocketReader, req: HTTPRequest, ws_handler: WSHandler
) -> None:
    handshake_response = build_handshake_response(req)
    try:
        sock.sendall(handshake_response.to_bytes())
    except OSError:
        return

    # Any bytes the HTTP layer already read off the wire but hadn't
    # consumed yet (e.g. the start of the client's first WS frame,
    # delivered in the same TCP segment as the upgrade request) must be
    # handed to the WebSocket layer too, not silently dropped.
    leftover = reader.drain_buffered()

    # WebSocket connections are long-lived by nature and legitimately sit
    # idle between messages, so this can't reuse IDLE_TIMEOUT_SECONDS --
    # but a generous timeout is still a worthwhile safety net against a
    # connection that's actually dead rather than just quiet. A real
    # production implementation would send periodic PING frames and treat
    # a missed PONG as the actual liveness signal instead of relying on a
    # timeout alone; that's a documented "next" item, not implemented here.
    sock.settimeout(WEBSOCKET_IDLE_TIMEOUT_SECONDS)
    ws_conn = WebSocketConnection(sock, initial_buffer=leftover)
    try:
        ws_handler(ws_conn, req)
    except Exception:
        pass  # a bug in the WS handler must not crash the server
    finally:
        ws_conn.close()


def _send_and_close(sock: socket.socket, response: HTTPResponse) -> None:
    response.headers["Connection"] = "close"
    try:
        sock.sendall(response.to_bytes())
    except OSError:
        pass
    sock.close()
