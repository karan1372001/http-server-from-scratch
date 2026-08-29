"""Parsing request bodies into structured data: JSON, form-urlencoded, and
multipart/form-data (file uploads).

The multipart parser is written by hand -- boundary detection, per-part
headers, splitting parts, extracting filenames -- since that's the genuinely
tricky bit of an HTTP server that most people never implement themselves.

JSON and urlencoded bodies use the standard library's own data-FORMAT
parsers (`json`, `urllib.parse`). That's different from using a web
FRAMEWORK: it's decoding a standard, framework-independent text format, the
same category as using Python's own `socket` module in Phase 1. The
networking and HTTP-specific logic -- everything that makes this a web
server rather than a JSON decoder -- is still all hand-written here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from .parser import HTTPRequest


class BodyParseError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class UploadedFile:
    field_name: str
    filename: str
    content_type: str
    data: bytes


@dataclass
class ParsedBody:
    json: Optional[dict] = None
    form: Dict[str, List[str]] = field(default_factory=dict)
    files: List[UploadedFile] = field(default_factory=list)

    def form_value(self, name: str, default=None):
        values = self.form.get(name)
        return values[0] if values else default


def _parse_content_type(header_value: str) -> Tuple[str, Dict[str, str]]:
    """Split "multipart/form-data; boundary=----abc" into (type, {params})."""
    parts = [p.strip() for p in header_value.split(";")]
    main_type = parts[0].lower()
    params: Dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, _, v = p.partition("=")
            params[k.strip().lower()] = v.strip().strip('"')
    return main_type, params


def parse_body(req: HTTPRequest) -> ParsedBody:
    content_type_header = req.header("content-type", "")
    if not content_type_header:
        return ParsedBody()

    main_type, params = _parse_content_type(content_type_header)

    if main_type == "application/json":
        if not req.body:
            return ParsedBody(json=None)
        try:
            return ParsedBody(json=json.loads(req.body.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise BodyParseError(400, f"Invalid JSON body: {e}")

    if main_type == "application/x-www-form-urlencoded":
        try:
            text = req.body.decode("ascii")
        except UnicodeDecodeError:
            raise BodyParseError(400, "Invalid form body encoding")
        return ParsedBody(form=parse_qs(text, keep_blank_values=True))

    if main_type == "multipart/form-data":
        boundary = params.get("boundary")
        if not boundary:
            raise BodyParseError(400, "Multipart body missing boundary")
        form, files = _parse_multipart(req.body, boundary.encode("ascii"))
        return ParsedBody(form=form, files=files)

    # Unknown content type: leave it unparsed; the handler can still read req.body directly.
    return ParsedBody()


def _parse_multipart(body: bytes, boundary: bytes) -> Tuple[Dict[str, List[str]], List[UploadedFile]]:
    """Hand-written multipart/form-data parser.

    Wire format (RFC 7578):
        --BOUNDARY\\r\\n
        Content-Disposition: form-data; name="field"; filename="a.txt"\\r\\n
        Content-Type: text/plain\\r\\n
        \\r\\n
        <raw bytes of this part>\\r\\n
        --BOUNDARY\\r\\n
        ... more parts ...
        --BOUNDARY--\\r\\n
    """
    delimiter = b"--" + boundary
    raw_parts = body.split(delimiter)

    form: Dict[str, List[str]] = {}
    files: List[UploadedFile] = []

    for raw_part in raw_parts[1:]:  # raw_parts[0] is preamble before the first boundary -- discard
        if raw_part in (b"--\r\n", b"--", b"", b"\r\n"):
            continue  # closing delimiter's tail or an empty split

        part = raw_part
        if part.startswith(b"\r\n"):
            part = part[2:]
        elif part.startswith(b"--"):
            continue  # this was the closing "--BOUNDARY--"
        if part.endswith(b"\r\n"):
            part = part[:-2]

        if b"\r\n\r\n" not in part:
            continue  # malformed part -- skip it rather than crash the whole request

        header_block, _, content = part.partition(b"\r\n\r\n")
        headers: Dict[str, str] = {}
        for line in header_block.split(b"\r\n"):
            if b":" not in line:
                continue
            name, _, value = line.partition(b":")
            headers[name.decode("ascii", "ignore").strip().lower()] = value.decode("ascii", "ignore").strip()

        field_name, filename = _parse_disposition(headers.get("content-disposition", ""))
        if field_name is None:
            continue

        if filename is not None:
            files.append(
                UploadedFile(
                    field_name=field_name,
                    filename=filename,
                    content_type=headers.get("content-type", "application/octet-stream"),
                    data=content,
                )
            )
        else:
            form.setdefault(field_name, []).append(content.decode("utf-8", errors="replace"))

    return form, files


def _parse_disposition(value: str) -> Tuple[Optional[str], Optional[str]]:
    """Extract name="..." and filename="..." from a Content-Disposition header."""
    if not value:
        return None, None
    name = None
    filename = None
    for piece in value.split(";"):
        piece = piece.strip()
        if piece.startswith("name="):
            name = piece[len("name=") :].strip('"')
        elif piece.startswith("filename="):
            filename = piece[len("filename=") :].strip('"')
    return name, filename
