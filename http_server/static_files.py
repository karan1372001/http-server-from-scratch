"""Static file serving: safe path resolution, MIME type guessing, real
protection against path-traversal attacks, HTTP caching validators
(ETag / Last-Modified), and single-range byte-range requests (for partial
content / seeking in large files like video).
"""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Optional, Tuple

from .errors import error_page
from .parser import HTTPRequest
from .response import HTTPResponse, http_date_from_timestamp, make_response


def safe_resolve(root: Path, url_path: str) -> Optional[Path]:
    """Resolve a URL path against `root`, refusing to let it escape `root`.

    The actual defense here is: resolve the path FIRST (which collapses any
    "../" segments into a real, final filesystem path), THEN check whether
    that final path is still inside `root`. Checking the raw URL string for
    "../" before resolving is the naive version and is easy to bypass
    (extra slashes, mixed separators, symlink tricks); resolving first and
    checking the real destination is not.

    Returns None if the resolved path lands outside `root`.
    """
    relative = url_path.lstrip("/")
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()

    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None  # candidate escaped root -- traversal attempt

    return candidate


def _compute_etag(data: bytes) -> str:
    # A strong ETag derived from actual file CONTENT, not just metadata --
    # mtime/size can be misleading (e.g. `touch` bumps mtime without
    # changing a single byte), which would make a metadata-only ETag lie
    # about whether the content actually changed.
    return '"' + hashlib.sha1(data).hexdigest() + '"'


def _etag_matches(if_none_match_header: str, etag: str) -> bool:
    if if_none_match_header.strip() == "*":
        return True
    candidates = [c.strip() for c in if_none_match_header.split(",")]
    return etag in candidates


def _parse_range_header(range_header: str, file_size: int) -> Optional[Tuple[int, int]]:
    """Parses a single-range "bytes=START-END" header into inclusive
    (start, end) byte offsets.

    Returns None if the header is absent, malformed, or a MULTI-range
    request ("bytes=0-10,20-30") -- multi-range responses need a
    multipart/byteranges body, which is out of scope here; the caller
    falls back to a normal full 200 response in that case rather than
    erroring, which is spec-permitted.
    """
    if not range_header or not range_header.startswith("bytes="):
        return None

    spec = range_header[len("bytes=") :]
    if "," in spec or "-" not in spec:
        return None

    start_s, _, end_s = spec.partition("-")

    if start_s == "":
        # "bytes=-500" means "the last 500 bytes of the file".
        if end_s == "":
            return None
        try:
            suffix_len = int(end_s)
        except ValueError:
            return None
        if suffix_len <= 0:
            return None
        return (max(0, file_size - suffix_len), file_size - 1)

    try:
        start = int(start_s)
    except ValueError:
        return None

    if end_s == "":
        end = file_size - 1
    else:
        try:
            end = int(end_s)
        except ValueError:
            return None

    return (start, end)


def serve_static_file(root: Path, url_path: str, req: Optional[HTTPRequest] = None) -> HTTPResponse:
    resolved = safe_resolve(root, url_path)
    if resolved is None:
        return error_page(403, "Forbidden")

    if not resolved.exists() or not resolved.is_file():
        return error_page(404, "File not found")

    content_type, _ = mimetypes.guess_type(str(resolved))
    content_type = content_type or "application/octet-stream"

    data = resolved.read_bytes()
    etag = _compute_etag(data)
    last_modified = http_date_from_timestamp(resolved.stat().st_mtime)

    base_headers = {
        "Content-Type": content_type,
        "ETag": etag,
        "Last-Modified": last_modified,
        "Cache-Control": "public, max-age=3600",
        "Accept-Ranges": "bytes",
    }

    if req is not None:
        if_none_match = req.header("if-none-match")
        if if_none_match is not None:
            if _etag_matches(if_none_match, etag):
                return HTTPResponse(status_code=304, headers=base_headers, body=b"")
        else:
            # Only fall back to If-Modified-Since when there's no ETag
            # validator at all -- per spec, a client that sent both should
            # be judged by ETag, the stronger of the two.
            if_modified_since = req.header("if-modified-since")
            if if_modified_since and if_modified_since == last_modified:
                return HTTPResponse(status_code=304, headers=base_headers, body=b"")

        range_header = req.header("range")
        if range_header:
            byte_range = _parse_range_header(range_header, len(data))
            if byte_range is not None:
                start, end = byte_range
                if start < 0 or start >= len(data) or start > end:
                    resp_headers = dict(base_headers)
                    resp_headers["Content-Range"] = f"bytes */{len(data)}"
                    return HTTPResponse(status_code=416, headers=resp_headers, body=b"")

                end = min(end, len(data) - 1)
                chunk = data[start : end + 1]
                resp_headers = dict(base_headers)
                resp_headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                return HTTPResponse(status_code=206, headers=resp_headers, body=chunk)
            # else: malformed Range header -- fall through to a normal 200,
            # a lenient and spec-permitted choice rather than erroring.

    return make_response(200, data, base_headers)
