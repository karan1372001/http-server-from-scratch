"""Static file serving: safe path resolution, MIME type guessing, and real
protection against path-traversal attacks.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Optional

from .errors import error_page
from .response import HTTPResponse, make_response


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


def serve_static_file(root: Path, url_path: str) -> HTTPResponse:
    resolved = safe_resolve(root, url_path)
    if resolved is None:
        return error_page(403, "Forbidden")

    if not resolved.exists() or not resolved.is_file():
        return error_page(404, "File not found")

    content_type, _ = mimetypes.guess_type(str(resolved))
    content_type = content_type or "application/octet-stream"

    data = resolved.read_bytes()
    return make_response(200, data, {"Content-Type": content_type})
