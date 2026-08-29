"""Custom HTML error pages for router-level errors (404, 405, etc).

Parse-level errors (malformed requests, before routing even happens) still
use the plain-text error_response in response.py -- there's no route
context that early, so keeping those fast and simple matters more than
making them pretty.
"""
from __future__ import annotations

from .response import STATUS_MESSAGES, HTTPResponse


def error_page(status_code: int, message: str = "") -> HTTPResponse:
    reason = STATUS_MESSAGES.get(status_code, "Error")
    text = message or reason
    body = f"""<!DOCTYPE html>
<html>
<head><title>{status_code} {reason}</title></head>
<body style="font-family: sans-serif; max-width: 640px; margin: 80px auto; color: #222;">
  <h1 style="font-size: 72px; margin-bottom: 0; color: #999;">{status_code}</h1>
  <h2 style="margin-top: 0;">{reason}</h2>
  <p>{text}</p>
  <hr>
  <p style="color: #999; font-size: 13px;">FromScratchHTTP/0.1 (Phase 2)</p>
</body>
</html>
""".encode("utf-8")
    return HTTPResponse(
        status_code=status_code,
        headers={"Content-Type": "text/html; charset=utf-8"},
        body=body,
    )
