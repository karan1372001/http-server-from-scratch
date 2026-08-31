import socket
import struct
import threading
import time

import pytest

from http_server.websocket import (
    OPCODE_BINARY,
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_TEXT,
    Frame,
    WebSocketConnection,
    compute_accept_key,
    encode_frame,
    is_websocket_upgrade_request,
)
from http_server.parser import HTTPRequest


def make_req(headers=None, method="GET"):
    return HTTPRequest(
        method=method, path="/ws", query={}, raw_query="", version="HTTP/1.1",
        headers=headers or {}, body=b"",
    )


# --- Handshake ---


def test_accept_key_matches_rfc6455_worked_example():
    # This is the EXACT example given in RFC 6455 section 1.3 -- if this
    # doesn't match, the handshake math itself is wrong, not just untested.
    client_key = "dGhlIHNhbXBsZSBub25jZQ=="
    expected_accept = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    assert compute_accept_key(client_key) == expected_accept


def test_is_websocket_upgrade_request_true_for_valid_handshake():
    req = make_req(headers={
        "upgrade": "websocket",
        "connection": "Upgrade",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
        "sec-websocket-version": "13",
    })
    assert is_websocket_upgrade_request(req) is True


def test_is_websocket_upgrade_request_false_for_normal_get():
    req = make_req(headers={})
    assert is_websocket_upgrade_request(req) is False


def test_is_websocket_upgrade_request_false_missing_key():
    req = make_req(headers={"upgrade": "websocket", "connection": "Upgrade"})
    assert is_websocket_upgrade_request(req) is False


def test_is_websocket_upgrade_request_false_wrong_method():
    req = make_req(
        method="POST",
        headers={"upgrade": "websocket", "connection": "Upgrade", "sec-websocket-key": "x"},
    )
    assert is_websocket_upgrade_request(req) is False


# --- Frame encoding ---


def test_encode_frame_short_payload_uses_single_byte_length():
    frame = encode_frame(OPCODE_TEXT, b"hi")
    # FIN=1, opcode=0x1 -> 0x81; length=2, no mask bit (server frames unmasked)
    assert frame[0] == 0x81
    assert frame[1] == 2
    assert frame[2:] == b"hi"


def test_encode_frame_medium_payload_uses_16_bit_length():
    payload = b"x" * 200
    frame = encode_frame(OPCODE_BINARY, payload)
    assert frame[1] == 126
    length = struct.unpack("!H", frame[2:4])[0]
    assert length == 200
    assert frame[4:] == payload


def test_encode_frame_large_payload_uses_64_bit_length():
    payload = b"x" * 70000
    frame = encode_frame(OPCODE_BINARY, payload)
    assert frame[1] == 127
    length = struct.unpack("!Q", frame[2:10])[0]
    assert length == 70000
    assert frame[10:] == payload


def test_encode_frame_never_sets_mask_bit():
    frame = encode_frame(OPCODE_TEXT, b"anything")
    assert (frame[1] & 0x80) == 0  # server frames are never masked


# --- WebSocketConnection: reading masked client frames ---


def _masked_client_frame(opcode: int, payload: bytes) -> bytes:
    """Builds a MASKED frame the way a real client is required to send."""
    mask_key = bytes([0x12, 0x34, 0x56, 0x78])
    masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    b0 = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", b0, 0x80 | length)
    elif length < 65536:
        header = struct.pack("!BBH", b0, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", b0, 0x80 | 127, length)
    return header + mask_key + masked_payload


def test_receives_and_unmasks_a_client_text_frame():
    server_sock, client_sock = socket.socketpair()
    try:
        conn = WebSocketConnection(server_sock)
        client_sock.sendall(_masked_client_frame(OPCODE_TEXT, "hello".encode()))
        msg = conn.receive()
        assert msg == "hello"
    finally:
        server_sock.close()
        client_sock.close()


def test_initial_buffer_bytes_are_not_lost():
    # Simulates a frame that arrived bundled with the HTTP upgrade request
    # -- already sitting in the reader's buffer before WebSocketConnection
    # ever touches the raw socket.
    server_sock, client_sock = socket.socketpair()
    try:
        pre_buffered = _masked_client_frame(OPCODE_TEXT, "early".encode())
        conn = WebSocketConnection(server_sock, initial_buffer=pre_buffered)
        msg = conn.receive()
        assert msg == "early"
    finally:
        server_sock.close()
        client_sock.close()


def test_ping_is_answered_with_pong_automatically():
    server_sock, client_sock = socket.socketpair()
    try:
        conn = WebSocketConnection(server_sock)
        client_sock.sendall(_masked_client_frame(OPCODE_PING, b"ping-data"))
        client_sock.sendall(_masked_client_frame(OPCODE_TEXT, b"after ping"))

        msg = conn.receive()  # should transparently consume the PING and return the text message
        assert msg == "after ping"

        client_sock.settimeout(2)
        pong_bytes = client_sock.recv(100)
        assert (pong_bytes[0] & 0x0F) == 0x0A  # PONG opcode
        assert pong_bytes[2:] == b"ping-data"
    finally:
        server_sock.close()
        client_sock.close()


def test_close_frame_ends_receive_loop():
    server_sock, client_sock = socket.socketpair()
    try:
        conn = WebSocketConnection(server_sock)
        client_sock.sendall(_masked_client_frame(OPCODE_CLOSE, struct.pack("!H", 1000)))
        msg = conn.receive()
        assert msg is None
        assert conn.closed is True
    finally:
        client_sock.close()


def test_large_frame_roundtrip_16_bit_length():
    server_sock, client_sock = socket.socketpair()
    try:
        conn = WebSocketConnection(server_sock)
        payload = ("x" * 500).encode()
        client_sock.sendall(_masked_client_frame(OPCODE_TEXT, payload))
        msg = conn.receive()
        assert msg == "x" * 500
    finally:
        server_sock.close()
        client_sock.close()


# --- End-to-end: real server, real router, real handshake + framed exchange ---


def _echo_ws_handler(conn, req):
    while True:
        msg = conn.receive()
        if msg is None:
            return
        conn.send_text(f"echo: {msg}")


@pytest.fixture(scope="module")
def running_ws_server():
    from http_server.router import Router
    from http_server.server import HTTPServer

    router = Router()
    router.websocket("/ws/echo")(_echo_ws_handler)

    @router.get("/")
    def index(req):
        from http_server.response import make_response
        return make_response(200, b"not a websocket route")

    server = HTTPServer(host="127.0.0.1", port=8799, handler=router.as_handler(), poll_interval=0.1, ws_routes=router.ws_routes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield server
    server.close()
    thread.join(timeout=3)


def test_real_handshake_returns_101_with_correct_accept_key(running_ws_server):
    with socket.create_connection(("127.0.0.1", 8799), timeout=5) as sock:
        sock.sendall(
            b"GET /ws/echo HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.settimeout(3)
        resp = sock.recv(1024)

    # Regression: 101 was missing from response.py's STATUS_MESSAGES table,
    # so this rendered as "101 Unknown" instead of "101 Switching
    # Protocols" -- caught by actually looking at the raw handshake bytes
    # from a live server, same as the 403 bug in Phase 2.
    assert resp.startswith(b"HTTP/1.1 101 Switching Protocols")
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in resp


def test_real_handshake_then_framed_echo_exchange(running_ws_server):
    with socket.create_connection(("127.0.0.1", 8799), timeout=5) as sock:
        sock.sendall(
            b"GET /ws/echo HTTP/1.1\r\n"
            b"Host: x\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.settimeout(3)
        handshake_resp = sock.recv(1024)
        assert handshake_resp.startswith(b"HTTP/1.1 101")

        sock.sendall(_masked_client_frame(OPCODE_TEXT, b"hello server"))
        frame_bytes = sock.recv(1024)

    # Parse it back with our own client-side test helper logic (mirrors
    # what encode_frame would have produced for OPCODE_TEXT).
    assert (frame_bytes[0] & 0x0F) == OPCODE_TEXT
    length = frame_bytes[1] & 0x7F
    payload = frame_bytes[2 : 2 + length]
    assert payload == b"echo: hello server"


def test_normal_http_route_on_same_server_still_works(running_ws_server):
    with socket.create_connection(("127.0.0.1", 8799), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        sock.settimeout(3)
        resp = sock.recv(1024)
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b"not a websocket route" in resp
