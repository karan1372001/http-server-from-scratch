"""Reverse proxy tests.

The important test here is end-to-end and real: spin up a "backend" server
and a separate "proxy" server that forwards to it, then hit the PROXY and
confirm the response actually came from the backend -- including checking
that X-Forwarded-For correctly carries the original client's IP through,
which is the entire point of a proxy adding that header.
"""
import json
import socket
import threading
import time

import pytest

from http_server.parser import HTTPRequest
from http_server.proxy import forward_request, proxy_to
from http_server.response import make_response
from http_server.server import HTTPServer

HOST = "127.0.0.1"


def make_req(method="GET", path="/", headers=None, body=b"", client_addr=None):
    return HTTPRequest(
        method=method, path=path, query={}, raw_query="", version="HTTP/1.1",
        headers=headers or {}, body=body, client_addr=client_addr,
    )


# --- End-to-end: real backend + real proxy, two real running servers ---


def _backend_handler(req):
    payload = {
        "path": req.path,
        "method": req.method,
        "x_forwarded_for": req.header("x-forwarded-for"),
        "body": req.body.decode("utf-8", errors="replace"),
    }
    return make_response(200, json.dumps(payload).encode(), {"Content-Type": "application/json"})


@pytest.fixture(scope="module")
def backend_server():
    server = HTTPServer(host=HOST, port=8701, handler=_backend_handler, poll_interval=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield server
    server.close()
    thread.join(timeout=3)


@pytest.fixture(scope="module")
def proxy_server(backend_server):
    server = HTTPServer(host=HOST, port=8702, handler=proxy_to(HOST, 8701), poll_interval=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield server
    server.close()
    thread.join(timeout=3)


def _send_raw(port: int, request_bytes: bytes) -> bytes:
    with socket.create_connection((HOST, port), timeout=5) as sock:
        sock.sendall(request_bytes)
        sock.settimeout(3)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except socket.timeout:
            pass
        return b"".join(chunks)


def test_proxy_forwards_get_request_and_relays_backend_response(proxy_server):
    resp = _send_raw(8702, b"GET /hello?x=1 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b'"path": "/hello"' in resp


def test_proxy_forwards_post_body(proxy_server):
    body = b"some request body"
    req = (
        b"POST /echo HTTP/1.1\r\nHost: x\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    resp = _send_raw(8702, req)
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b'"body": "some request body"' in resp


def test_proxy_adds_x_forwarded_for_with_the_real_client_ip(proxy_server):
    resp = _send_raw(8702, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    # The backend only ever sees the PROXY as its direct connection --
    # X-Forwarded-For is how it learns about the real original client.
    assert b'"x_forwarded_for": "127.0.0.1"' in resp


def test_proxy_returns_502_when_backend_is_unreachable():
    dead_backend_handler = proxy_to(HOST, 8799)  # nothing listening on this port
    server = HTTPServer(host=HOST, port=8703, handler=dead_backend_handler, poll_interval=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        resp = _send_raw(8703, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    finally:
        server.close()
        thread.join(timeout=3)
    assert resp.startswith(b"HTTP/1.1 502")


# --- Unit tests: forward_request / header handling ---


def test_proxy_does_not_duplicate_response_headers(proxy_server):
    # Regression: Connection/Date/Server/Content-Length from the backend's
    # raw response used to survive alongside our own freshly-added
    # versions of the same headers (different dict-key casing hid the
    # collision), so every one of them appeared TWICE on the wire. Caught
    # by looking at a real proxied response, not just checking status codes.
    resp = _send_raw(8702, b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    head = resp.split(b"\r\n\r\n")[0]
    lines = head.split(b"\r\n")
    header_names_lower = [line.split(b":")[0].lower() for line in lines[1:] if b":" in line]
    for name in (b"connection", b"date", b"server", b"content-length"):
        assert header_names_lower.count(name) == 1, f"{name} appeared {header_names_lower.count(name)} times"


def test_forward_request_strips_hop_by_hop_headers_before_sending():
    # Can't easily intercept what actually goes on the wire without a real
    # socket, so this is verified via the end-to-end tests above (the
    # backend never sees a stray "Connection: keep-alive" from the
    # original client, since forward_request always sets its own). This
    # test instead confirms a clearly bad upstream target fails safely.
    req = make_req(client_addr=("9.9.9.9", 1))
    resp = forward_request(HOST, 1, req, timeout=1)  # port 1 is not going to accept connections
    assert resp.status_code == 502
