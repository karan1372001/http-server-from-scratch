"""Handles a single client connection: read request(s), call the handler, write response(s).

Owns the keep-alive loop: after each response, decides -- based on the
request's HTTP version and Connection header -- whether to read another
request off the same socket or close it.
"""
from __future__ import annotations

import socket
from typing import Callable

from .parser import HTTPParseError, HTTPRequest, parse_request
from .reader import BufferedSocketReader, ConnectionClosed, RequestTooLarge
from .response import HTTPResponse, error_response

MAX_REQUEST_LINE = 8192
MAX_HEAD_SIZE = 64 * 1024  # request line + all headers combined
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB -- generous placeholder for Phase 1
IDLE_TIMEOUT_SECONDS = 30  # basic only; real Slowloris protection is Phase 4

Handler = Callable[[HTTPRequest], HTTPResponse]


def _wants_keep_alive(req: HTTPRequest) -> bool:
    connection_header = (req.header("connection") or "").lower()
    if connection_header:
        return connection_header != "close"
    # HTTP/1.1 defaults to keep-alive; HTTP/1.0 defaults to close.
    return req.version == "HTTP/1.1"


def handle_connection(sock: socket.socket, addr, handler: Handler) -> None:
    sock.settimeout(IDLE_TIMEOUT_SECONDS)
    reader = BufferedSocketReader(sock)

    try:
        while True:
            # Read the whole head (request line + all headers) as one block,
            # up to the blank line that separates it from the body. Splitting
            # it AFTER reading -- rather than reading the request line and
            # headers as two separate delimited reads -- avoids an off-by-one
            # trap: with zero headers, the request line's own CRLF plus the
            # blank line's CRLF don't add up to a clean "\r\n\r\n" once the
            # request line has already been consumed separately.
            try:
                head = reader.read_until(b"\r\n\r\n", max_size=MAX_HEAD_SIZE)
            except ConnectionClosed:
                return  # peer closed between requests -- normal on keep-alive
            except socket.timeout:
                return  # idle too long -- close quietly
            except RequestTooLarge:
                _send_and_close(sock, error_response(431, "Request head too large"))
                return

            if head == b"":
                # Some clients send a stray leading blank line between
                # keep-alive requests (allowed by spec as a lenient-parsing
                # courtesy, RFC 7230 3.5) -- skip it and read the real head.
                try:
                    head = reader.read_until(b"\r\n\r\n", max_size=MAX_HEAD_SIZE)
                except (ConnectionClosed, socket.timeout):
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
                    req.body = reader.read_exact(content_length)
                except (ConnectionClosed, socket.timeout):
                    return
            elif "transfer-encoding" in req.headers:
                # Chunked transfer-encoding is out of scope for Phase 1.
                _send_and_close(sock, error_response(501, "Chunked transfer-encoding not yet supported"))
                return

            try:
                response = handler(req)
            except Exception as e:  # a bug in the handler must never take the server down
                response = error_response(500, f"Internal server error: {e}")

            keep_alive = _wants_keep_alive(req) and response.status_code < 500
            response.headers.setdefault("Connection", "keep-alive" if keep_alive else "close")

            include_body = req.method != "HEAD"
            try:
                sock.sendall(response.to_bytes(include_body=include_body, http_version=req.version))
            except OSError:
                return

            if not keep_alive:
                return
    finally:
        sock.close()


def _send_and_close(sock: socket.socket, response: HTTPResponse) -> None:
    response.headers["Connection"] = "close"
    try:
        sock.sendall(response.to_bytes())
    except OSError:
        pass
    sock.close()
