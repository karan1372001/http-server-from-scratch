"""A tiny reverse proxy: forwards matching requests to another backend HTTP
server and relays the response back, the way nginx does in production.

Everything else in this project is HTTP *server* code -- parsing requests,
building responses. Forwarding a request means being an HTTP *client* to
someone else's server, which is genuinely the other half of the protocol
and needed its own small hand-written piece: `_read_upstream_response`
parses a raw HTTP response (status line + headers + body) the same way
parser.py parses a request, just for the opposite message type.
"""
from __future__ import annotations

import socket
from typing import Dict

from .parser import HTTPRequest
from .response import HTTPResponse, error_response

# Headers that describe THIS hop of the connection, not the actual message
# content -- forwarding them to the backend as-is would be wrong (e.g. we
# want our OWN "Connection: close" to the backend, not to blindly relay
# whatever the original client sent for its connection to US).
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

# On the way BACK, additionally strip these: our own response.py always
# adds fresh Date/Server/Content-Length/Connection headers when building
# the final bytes to send to the real client. Leaving the backend's
# original versions of these in the returned HTTPResponse used to cause
# every one of them to appear TWICE on the wire -- the backend's version
# stored under a lower-cased dict key, ours added right alongside it under
# a properly-cased one, since dict keys are case-sensitive and HTTP header
# names aren't. Found by actually looking at a live proxied response.
RESPONSE_HEADERS_TO_STRIP = HOP_BY_HOP_HEADERS | {"date", "server", "content-length"}


def _read_upstream_response(sock: socket.socket, timeout: float) -> HTTPResponse:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("Upstream closed the connection before sending headers")
        buf += chunk

    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0].decode("ascii", errors="replace")
    parts = status_line.split(" ", 2)
    if len(parts) < 2:
        raise ValueError(f"Malformed upstream status line: {status_line!r}")
    status_code = int(parts[1])

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        header_name = name.decode("ascii", "replace").strip().lower()
        if header_name in RESPONSE_HEADERS_TO_STRIP:
            continue
        headers[header_name] = value.decode("ascii", "replace").strip()

    body = rest
    content_length = headers.get("content-length")
    if content_length is not None:
        needed = int(content_length)
        while len(body) < needed:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
        body = body[:needed]

    # Re-key headers with their original casing where we can, but the
    # lower-cased dict above is what we actually forward -- HTTP header
    # names are case-insensitive, so this loses nothing semantically.
    return HTTPResponse(status_code=status_code, headers=headers, body=body)


def forward_request(host: str, port: int, req: HTTPRequest, timeout: float = 10.0) -> HTTPResponse:
    """Forwards `req` to host:port over a fresh connection and returns the
    upstream's response, translated back into our own HTTPResponse type.

    On any failure to reach or parse a response from upstream, returns a
    502 Bad Gateway rather than raising -- a proxy that crashes because its
    backend is down defeats the purpose of having a proxy in front of it.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            target = req.path + (("?" + req.raw_query) if req.raw_query else "")
            forwarded_headers = {k: v for k, v in req.headers.items() if k not in HOP_BY_HOP_HEADERS}
            forwarded_headers["host"] = f"{host}:{port}"
            forwarded_headers["connection"] = "close"
            if req.client_addr:
                # The standard way a proxy tells the backend who the REAL
                # client was, since from the backend's point of view the
                # proxy itself is the client.
                existing = forwarded_headers.get("x-forwarded-for", "")
                forwarded_headers["x-forwarded-for"] = (
                    f"{existing}, {req.client_addr[0]}" if existing else req.client_addr[0]
                )

            lines = [f"{req.method} {target} HTTP/1.1"]
            lines += [f"{k}: {v}" for k, v in forwarded_headers.items()]
            head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

            sock.sendall(head + req.body)
            return _read_upstream_response(sock, timeout)
    except (OSError, ConnectionError, ValueError, UnicodeError) as e:
        return error_response(502, f"Bad Gateway: could not reach upstream {host}:{port} ({e})")


def proxy_to(host: str, port: int, timeout: float = 10.0):
    """Returns a plain Handler that forwards every request it receives to
    host:port. Mount it on a router path to act as a reverse proxy for
    that path, or as the whole app to proxy everything.
    """

    def handler(req: HTTPRequest) -> HTTPResponse:
        return forward_request(host, port, req, timeout=timeout)

    return handler
