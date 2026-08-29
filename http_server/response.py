"""Building raw HTTP/1.1 response bytes, by hand."""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Dict, Optional

STATUS_MESSAGES: Dict[int, str] = {
    200: "OK",
    201: "Created",
    204: "No Content",
    206: "Partial Content",
    301: "Moved Permanently",
    304: "Not Modified",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    411: "Length Required",
    413: "Payload Too Large",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    505: "HTTP Version Not Supported",
}


def http_date(dt: Optional[datetime.datetime] = None) -> str:
    dt = dt or datetime.datetime.now(datetime.timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


@dataclass
class HTTPResponse:
    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def to_bytes(self, *, include_body: bool = True, http_version: str = "HTTP/1.1") -> bytes:
        reason = STATUS_MESSAGES.get(self.status_code, "Unknown")
        headers = dict(self.headers)
        headers.setdefault("Date", http_date())
        headers.setdefault("Server", "FromScratchHTTP/0.1")
        headers.setdefault("Content-Length", str(len(self.body)))

        lines = [f"{http_version} {self.status_code} {reason}"]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

        return head + (self.body if include_body else b"")


def make_response(status_code: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> HTTPResponse:
    return HTTPResponse(status_code=status_code, headers=dict(headers or {}), body=body)


def error_response(status_code: int, message: str = "") -> HTTPResponse:
    text = message or STATUS_MESSAGES.get(status_code, "Error")
    body = f"{status_code} {STATUS_MESSAGES.get(status_code, '')}\n\n{text}\n".encode("utf-8")
    return HTTPResponse(
        status_code=status_code,
        headers={"Content-Type": "text/plain; charset=utf-8"},
        body=body,
    )
