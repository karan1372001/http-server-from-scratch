"""Demo application for Phase 2: real routing, path params, JSON/form/multipart
bodies, and static file serving -- all wired through the router in
http_server/router.py.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from http_server.body_parser import BodyParseError, parse_body
from http_server.errors import error_page
from http_server.parser import HTTPRequest
from http_server.response import HTTPResponse, make_response
from http_server.router import Router
from http_server.static_files import serve_static_file

router = Router()

STATIC_ROOT = Path(__file__).parent / "public"

# In-memory "database" so /users/{id} has something real to look up.
FAKE_USERS = {
    "1": {"id": "1", "name": "Ada Lovelace"},
    "2": {"id": "2", "name": "Alan Turing"},
}


@router.get("/")
def index(req: HTTPRequest) -> HTTPResponse:
    return make_response(
        200,
        b"Phase 2 is live. Try /users/1, /static/hello.txt, POST /form, POST /upload, POST /echo\n",
        {"Content-Type": "text/plain"},
    )


@router.get("/users/{user_id}")
def get_user(req: HTTPRequest) -> HTTPResponse:
    user_id = req.path_params["user_id"]
    user = FAKE_USERS.get(user_id)
    if user is None:
        return error_page(404, f"No user with id {user_id}")
    return make_response(200, json.dumps(user).encode(), {"Content-Type": "application/json"})


@router.post("/echo")
def echo(req: HTTPRequest) -> HTTPResponse:
    content_type = req.header("content-type", "application/octet-stream")
    return make_response(200, req.body, {"Content-Type": content_type})


@router.get("/items")
def list_items(req: HTTPRequest) -> HTTPResponse:
    body = json.dumps({"query": req.query}).encode()
    return make_response(200, body, {"Content-Type": "application/json"})


@router.post("/items")
def create_item(req: HTTPRequest) -> HTTPResponse:
    body = json.dumps({"received_bytes": len(req.body)}).encode()
    return make_response(201, body, {"Content-Type": "application/json"})


@router.post("/form")
def submit_form(req: HTTPRequest) -> HTTPResponse:
    try:
        parsed = parse_body(req)
    except BodyParseError as e:
        return error_page(e.status_code, e.message)
    flat = {k: v[0] for k, v in parsed.form.items()}
    return make_response(200, json.dumps({"form": flat}).encode(), {"Content-Type": "application/json"})


@router.post("/upload")
def upload_files(req: HTTPRequest) -> HTTPResponse:
    try:
        parsed = parse_body(req)
    except BodyParseError as e:
        return error_page(e.status_code, e.message)

    summary = [
        {"field": f.field_name, "filename": f.filename, "content_type": f.content_type, "bytes": len(f.data)}
        for f in parsed.files
    ]
    body = json.dumps({"files": summary, "form": parsed.form}).encode()
    return make_response(200, body, {"Content-Type": "application/json"})


@router.get("/static/{filepath*}")
def static_file(req: HTTPRequest) -> HTTPResponse:
    return serve_static_file(STATIC_ROOT, req.path_params["filepath"])


@router.get("/slow")
def slow(req: HTTPRequest) -> HTTPResponse:
    """Simulates an I/O-bound handler (e.g. a slow database call), purely
    for the Phase 3 concurrency benchmark -- see README.md Phase 3.
    """
    time.sleep(0.1)
    return make_response(200, b"Slow response after ~100ms\n", {"Content-Type": "text/plain"})


app_handler = router.as_handler()
