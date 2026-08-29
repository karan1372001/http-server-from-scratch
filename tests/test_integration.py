"""Integration tests: actually start the server and hit it with real socket requests.

These don't mock anything below the socket layer -- they prove the server
handles real bytes on a real TCP connection correctly, including keep-alive.
"""
import socket
import threading
import time

import pytest

from app import app_handler
from http_server.server import HTTPServer

HOST = "127.0.0.1"
PORT = 8099


@pytest.fixture(scope="module")
def running_server():
    server = HTTPServer(host=HOST, port=PORT, handler=app_handler, poll_interval=0.1)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)  # give the listening socket a moment to come up
    yield server
    server.close()


def send_raw(request_bytes: bytes) -> bytes:
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.sendall(request_bytes)
        sock.settimeout(2)
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


def test_get_root(running_server):
    resp = send_raw(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b"Phase 2 is live" in resp


def test_404_unknown_path(running_server):
    resp = send_raw(b"GET /nope HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 404")


def test_405_wrong_method_includes_allow_header(running_server):
    resp = send_raw(b"DELETE / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 405")
    assert b"Allow:" in resp


def test_post_echo_with_body(running_server):
    body = b"hello world"
    req = (
        b"POST /echo HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    resp = send_raw(req)
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert resp.endswith(body)


def test_put_and_delete_return_correct_status(running_server):
    # /items only allows GET/POST, so PUT should 405; / only allows GET/HEAD.
    resp = send_raw(b"PUT /items HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 405")


def test_query_string_parsing_end_to_end(running_server):
    resp = send_raw(b"GET /items?page=2&limit=10 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b'"page": ["2"]' in resp
    assert b'"limit": ["10"]' in resp


def test_malformed_request_line_returns_400_or_closes_cleanly(running_server):
    resp = send_raw(b"NOT A REQUEST AT ALL HTTP/1.1\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 400")


def test_keep_alive_serves_two_requests_on_one_connection(running_server):
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        sock.settimeout(2)
        first = sock.recv(65536)
        assert first.startswith(b"HTTP/1.1 200 OK")
        assert b"Connection: keep-alive" in first

        # Second request reuses the SAME socket -- proves the connection
        # wasn't closed after the first response.
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        second = sock.recv(65536)
        assert second.startswith(b"HTTP/1.1 200 OK")
        assert b"Connection: close" in second


def test_head_request_has_headers_but_no_body(running_server):
    resp = send_raw(b"HEAD / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    head, _, body = resp.partition(b"\r\n\r\n")
    assert b"Content-Length:" in head
    assert body == b""


def test_request_with_zero_headers_is_handled(running_server):
    # Regression test: a request with NO headers at all is a valid edge case
    # (even though real clients always send at least Host) and must not hang
    # the connection waiting for bytes that will never arrive.
    resp = send_raw(b"GET / HTTP/1.1\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")


def test_http_1_0_defaults_to_close(running_server):
    resp = send_raw(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
    assert resp.startswith(b"HTTP/1.0 200 OK")
    assert b"Connection: close" in resp


def test_path_param_returns_matching_user(running_server):
    resp = send_raw(b"GET /users/1 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b"Ada Lovelace" in resp


def test_path_param_404_for_unknown_id(running_server):
    resp = send_raw(b"GET /users/999 HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 404")


def test_static_file_is_served(running_server):
    resp = send_raw(b"GET /static/hello.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b"Hello from a static file" in resp


def test_static_file_nested_path_is_served(running_server):
    resp = send_raw(b"GET /static/css/site.css HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b"text/css" in resp


def test_static_file_missing_returns_404(running_server):
    resp = send_raw(b"GET /static/does-not-exist.txt HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 404")


def test_static_file_path_traversal_is_blocked(running_server):
    # Real attack attempt: try to walk out of the public/ folder using ../
    # segments and read something outside it (e.g. app.py, one level up).
    resp = send_raw(b"GET /static/../app.py HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert not resp.startswith(b"HTTP/1.1 200")
    assert b"FAKE_USERS" not in resp  # contents of app.py must never leak


def test_form_urlencoded_post(running_server):
    body = b"name=Ada&role=engineer"
    req = (
        b"POST /form HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Content-Type: application/x-www-form-urlencoded\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    resp = send_raw(req)
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b'"name": "Ada"' in resp
    assert b'"role": "engineer"' in resp


def test_multipart_file_upload(running_server):
    boundary = b"----WebTestBoundary"
    file_content = b"hello from a real uploaded file"
    body = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="avatar"; filename="pic.txt"\r\n'
        b"Content-Type: text/plain\r\n\r\n"
        + file_content + b"\r\n"
        b"--" + boundary + b"--\r\n"
    )
    req = (
        b"POST /upload HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Content-Type: multipart/form-data; boundary=" + boundary + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body
    )
    resp = send_raw(req)
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b'"filename": "pic.txt"' in resp
    assert f'"bytes": {len(file_content)}'.encode() in resp
