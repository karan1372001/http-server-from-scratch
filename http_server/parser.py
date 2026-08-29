"""HTTP/1.1 request parsing built from raw bytes -- no parsing libraries."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from urllib.parse import unquote, parse_qs


class HTTPParseError(Exception):
    """Raised when a request can't be parsed. Carries the status code to report."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH"}
MAX_HEADER_LINE = 8192
MAX_HEADER_COUNT = 100


@dataclass
class HTTPRequest:
    method: str
    path: str
    query: Dict[str, List[str]]
    raw_query: str
    version: str
    headers: Dict[str, str]  # lower-cased header names -> value
    body: bytes
    path_params: Dict[str, str] = field(default_factory=dict)

    def header(self, name: str, default=None):
        return self.headers.get(name.lower(), default)


def parse_request_line(line: bytes) -> Tuple[str, str, Dict[str, List[str]], str, str]:
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        raise HTTPParseError(400, "Request line is not valid ASCII")

    parts = text.split(" ")
    if len(parts) != 3:
        raise HTTPParseError(400, "Malformed request line")

    method, target, version = parts

    if method not in VALID_METHODS:
        raise HTTPParseError(501, f"Unsupported method: {method}")

    if not target.startswith("/"):
        raise HTTPParseError(400, "Request target must start with '/'")

    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise HTTPParseError(505, f"Unsupported HTTP version: {version}")

    if "?" in target:
        path, _, raw_query = target.partition("?")
    else:
        path, raw_query = target, ""

    # Decode AFTER splitting off the query string. Note: this does NOT resolve
    # "../" dot-segments -- that's the static file handler's job in Phase 2,
    # and it must happen there (after this decoded path is resolved against a
    # real filesystem root), not here. Decoding at the wrong stage is exactly
    # how "../" path-traversal payloads sneak past naive checks.
    path = unquote(path)
    query = parse_qs(raw_query, keep_blank_values=True)

    return method, path, query, raw_query, version


def parse_headers(raw: bytes) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if not raw:
        return headers

    lines = raw.split(b"\r\n")
    count = 0
    for line in lines:
        if not line:
            continue
        count += 1
        if count > MAX_HEADER_COUNT:
            raise HTTPParseError(400, "Too many headers")
        if len(line) > MAX_HEADER_LINE:
            raise HTTPParseError(431, "Header line too large")
        if b":" not in line:
            raise HTTPParseError(400, "Malformed header line")

        name, _, value = line.partition(b":")
        try:
            name_s = name.decode("ascii").strip()
            value_s = value.decode("ascii").strip()
        except UnicodeDecodeError:
            raise HTTPParseError(400, "Header is not valid ASCII")

        if not name_s or " " in name_s or "\t" in name_s:
            raise HTTPParseError(400, "Malformed header name")

        headers[name_s.lower()] = value_s

    return headers


def parse_request(request_line: bytes, header_block: bytes, body: bytes) -> HTTPRequest:
    method, path, query, raw_query, version = parse_request_line(request_line)
    headers = parse_headers(header_block)
    return HTTPRequest(
        method=method,
        path=path,
        query=query,
        raw_query=raw_query,
        version=version,
        headers=headers,
        body=body,
    )
