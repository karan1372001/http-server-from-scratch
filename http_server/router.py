"""Hand-written HTTP router: matches request path + method to a handler.

Supports static paths ("/items"), single-segment path parameters
("/users/{id}"), and catch-all parameters that match the rest of the path
including slashes ("/static/{filepath*}"), used for static file serving.

This uses Python's `re` module to compile path patterns into regexes. That's
using the standard library's own text-matching tool, the same category as
using `socket` in Phase 1 -- it's not a web framework doing routing "magic"
for us; the routing logic itself (compiling patterns, matching, tracking
allowed methods for 405s) is all written here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Pattern, Tuple

from .errors import error_page
from .parser import HTTPRequest
from .response import HTTPResponse

RouteHandler = Callable[[HTTPRequest], HTTPResponse]

# Matches {name} for a single path segment, or {name*} for "rest of path".
_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)(\*)?\}")


def _compile_path(path_pattern: str) -> Pattern[str]:
    parts = []
    last_end = 0
    for m in _PARAM_RE.finditer(path_pattern):
        parts.append(re.escape(path_pattern[last_end : m.start()]))
        param_name = m.group(1)
        is_catchall = bool(m.group(2))
        if is_catchall:
            parts.append(f"(?P<{param_name}>.+)")
        else:
            parts.append(f"(?P<{param_name}>[^/]+)")
        last_end = m.end()
    parts.append(re.escape(path_pattern[last_end:]))
    return re.compile("^" + "".join(parts) + "$")


@dataclass
class _Route:
    method: str
    pattern: Pattern[str]
    handler: RouteHandler
    original_path: str


class Router:
    def __init__(self):
        self._routes: List[_Route] = []

    def add_route(self, method: str, path_pattern: str, handler: RouteHandler) -> None:
        self._routes.append(_Route(method.upper(), _compile_path(path_pattern), handler, path_pattern))

    def get(self, path_pattern: str):
        def decorator(handler: RouteHandler) -> RouteHandler:
            self.add_route("GET", path_pattern, handler)
            return handler

        return decorator

    def post(self, path_pattern: str):
        def decorator(handler: RouteHandler) -> RouteHandler:
            self.add_route("POST", path_pattern, handler)
            return handler

        return decorator

    def put(self, path_pattern: str):
        def decorator(handler: RouteHandler) -> RouteHandler:
            self.add_route("PUT", path_pattern, handler)
            return handler

        return decorator

    def delete(self, path_pattern: str):
        def decorator(handler: RouteHandler) -> RouteHandler:
            self.add_route("DELETE", path_pattern, handler)
            return handler

        return decorator

    def match(
        self, method: str, path: str
    ) -> Tuple[Optional[RouteHandler], Dict[str, str], List[str]]:
        """Returns (handler, path_params, allowed_methods_for_this_path).

        - No route matches the path at all: (None, {}, []) -> caller returns 404.
        - Path matches but not this method: (None, {}, [allowed...]) -> caller returns 405.
        - Match: (handler, {params}, [this_method]).
        """
        allowed_methods: List[str] = []
        method = method.upper()
        # A path registered for GET automatically also answers HEAD requests
        # (with the response body stripped later, in connection.py).
        effective_method = "GET" if method == "HEAD" else method

        for route in self._routes:
            m = route.pattern.match(path)
            if not m:
                continue
            if route.method not in allowed_methods:
                allowed_methods.append(route.method)
            if route.method == effective_method:
                return route.handler, m.groupdict(), allowed_methods

        return None, {}, allowed_methods

    def as_handler(self) -> RouteHandler:
        """Wraps this router as a plain Handler, for use with HTTPServer."""

        def handler(req: HTTPRequest) -> HTTPResponse:
            path_handler, path_params, allowed_methods = self.match(req.method, req.path)

            if path_handler is None:
                if allowed_methods:
                    return HTTPResponse(
                        status_code=405,
                        headers={"Allow": ", ".join(sorted(set(allowed_methods)))},
                        body=f"Method {req.method} not allowed on {req.path}\n".encode(),
                    )
                return error_page(404, f"No such path: {req.path}")

            req.path_params = path_params
            return path_handler(req)

        return handler
