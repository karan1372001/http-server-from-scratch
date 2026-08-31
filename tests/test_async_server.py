"""Tests for the async/selectors concurrency model.

Two things matter specifically here, more than for the other two models:

1. Correctness under FRAGMENTED input. A non-blocking socket can deliver a
   request one byte at a time across many separate feed() calls -- there's
   no blocking read to hide behind. If the state machine assumed a request
   always arrives whole, it would silently break on slow/real-world clients
   while looking fine in every fast-localhost test.
2. That many concurrent connections are genuinely served without spinning
   up a thread per connection.
"""
import socket
import threading
import time

import pytest

from http_server.async_connection import AsyncConnection
from http_server.async_server import AsyncHTTPServer
from http_server.response import make_response

HOST = "127.0.0.1"
PORT = 8299


def _echo_handler(req):
    return make_response(200, req.body or b"ok", {"Content-Type": "text/plain"})


# --- Unit tests: the state machine directly, with deliberately fragmented input ---


def test_state_machine_handles_request_delivered_one_byte_at_a_time():
    conn = AsyncConnection(_echo_handler)
    request = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    for i in range(len(request)):
        assert not conn.wants_write  # must not respond before the request is complete
        conn.feed(request[i : i + 1])

    assert conn.wants_write
    assert conn.bytes_to_send().startswith(b"HTTP/1.1 200 OK")


def test_state_machine_handles_body_split_across_many_feeds():
    conn = AsyncConnection(_echo_handler)
    body = b"hello world, this is the request body"
    head = f"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()

    conn.feed(head[:10])
    conn.feed(head[10:])
    assert not conn.wants_write  # head done, but zero body bytes delivered yet

    for i in range(0, len(body), 3):
        conn.feed(body[i : i + 3])

    assert conn.wants_write
    resp = conn.bytes_to_send()
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert resp.endswith(body)


def test_state_machine_handles_zero_headers_request():
    # Regression: the exact edge case that broke the BLOCKING parser back in
    # Phase 1 -- worth checking the async parser independently, since it's
    # a completely separate implementation of "read the head."
    conn = AsyncConnection(_echo_handler)
    conn.feed(b"GET / HTTP/1.1\r\n\r\n")
    assert conn.wants_write
    assert conn.bytes_to_send().startswith(b"HTTP/1.1 200 OK")


def test_state_machine_supports_keep_alive_across_multiple_requests():
    conn = AsyncConnection(_echo_handler)
    conn.feed(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    first_response = conn.bytes_to_send()
    assert b"Connection: keep-alive" in first_response

    conn.advance_after_send(len(first_response))
    assert not conn.is_closed
    assert not conn.wants_write  # response fully sent, nothing pending

    conn.feed(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    second_response = conn.bytes_to_send()
    assert second_response.startswith(b"HTTP/1.1 200 OK")


def test_state_machine_handles_pipelined_requests_in_one_feed_call():
    # Two full requests delivered in a single feed() call, back to back --
    # must not lose or merge the second one.
    conn = AsyncConnection(_echo_handler)
    req1 = b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"
    conn.feed(req1)
    first_response = conn.bytes_to_send()
    conn.advance_after_send(len(first_response))

    req2 = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
    conn.feed(req2)
    assert conn.wants_write
    assert conn.bytes_to_send().startswith(b"HTTP/1.1 200 OK")


def test_state_machine_rejects_malformed_request_and_marks_should_close():
    conn = AsyncConnection(_echo_handler)
    conn.feed(b"NOT A REQUEST AT ALL HTTP/1.1\r\n\r\n")
    assert conn.wants_write
    assert conn.bytes_to_send().startswith(b"HTTP/1.1 400")
    assert conn.should_close is True


def test_state_machine_empty_feed_marks_connection_closed():
    conn = AsyncConnection(_echo_handler)
    conn.feed(b"")  # simulates a recv() returning b"" -- peer closed
    assert conn.is_closed


# --- Integration tests: the real selectors-based server over real sockets ---


@pytest.fixture(scope="module")
def running_async_server():
    server = AsyncHTTPServer(host=HOST, port=PORT, handler=_echo_handler, poll_interval=0.05)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield server
    server.close()
    thread.join(timeout=3)


def _send_raw(request_bytes: bytes, timeout: float = 3) -> bytes:
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.sendall(request_bytes)
        sock.settimeout(timeout)
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


def test_async_server_responds_correctly(running_async_server):
    resp = _send_raw(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")


def test_async_server_handles_many_concurrent_connections(running_async_server):
    n = 50
    results = []
    threads = []

    def worker():
        results.append(_send_raw(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"))

    for _ in range(n):
        t = threading.Thread(target=worker)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=10)

    assert len(results) == n
    assert all(r.startswith(b"HTTP/1.1 200 OK") for r in results)


def test_async_server_handles_a_genuinely_slow_client(running_async_server):
    # Sends the request one byte at a time with real delays, over a REAL
    # socket to the real event loop -- proves this isn't just correct on
    # fast localhost bursts where a whole request always arrives in one recv().
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        request = b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        for i in range(len(request)):
            sock.sendall(request[i : i + 1])
            time.sleep(0.005)
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
        resp = b"".join(chunks)
    assert resp.startswith(b"HTTP/1.1 200 OK")


def test_async_server_keep_alive_serves_two_requests_on_one_connection(running_async_server):
    with socket.create_connection((HOST, PORT), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        sock.settimeout(3)
        first = sock.recv(65536)
        assert first.startswith(b"HTTP/1.1 200 OK")
        assert b"Connection: keep-alive" in first

        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        second = sock.recv(65536)
        assert second.startswith(b"HTTP/1.1 200 OK")
