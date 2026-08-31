"""Hand-written WebSocket support (RFC 6455): the HTTP Upgrade handshake
and the binary frame format, no WebSocket library.

Two halves:
  1. The handshake -- an ordinary HTTP request with a magic set of headers,
     answered with `101 Switching Protocols` and a computed accept key.
     This part reuses the existing HTTP request/response machinery.
  2. The frame protocol -- once upgraded, the connection stops speaking
     HTTP entirely and starts exchanging binary frames until it closes.
     This is genuinely new wire-format work: masking (client frames must
     be masked, server frames must not), three different payload-length
     encodings depending on size, and control frames (ping/pong/close)
     interleaved with data frames.

Scope: text and binary messages, ping/pong, and close are all supported.
Fragmented messages (continuation frames) are NOT -- every message from
this implementation is sent as a single complete frame, and an incoming
continuation frame raises rather than silently mishandling it. That's a
real, documented limitation, not a silent gap.
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Optional

from .parser import HTTPRequest
from .response import HTTPResponse

WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


def is_websocket_upgrade_request(req: HTTPRequest) -> bool:
    return (
        req.method == "GET"
        and (req.header("upgrade") or "").lower() == "websocket"
        and "upgrade" in (req.header("connection") or "").lower()
        and req.header("sec-websocket-key") is not None
    )


def compute_accept_key(client_key: str) -> str:
    """RFC 6455 4.2.2: SHA-1(client_key + magic GUID), base64-encoded."""
    digest = hashlib.sha1((client_key + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def build_handshake_response(req: HTTPRequest) -> HTTPResponse:
    accept_key = compute_accept_key(req.header("sec-websocket-key"))
    return HTTPResponse(
        status_code=101,
        headers={
            "Upgrade": "websocket",
            "Connection": "Upgrade",
            "Sec-WebSocket-Accept": accept_key,
        },
    )


@dataclass
class Frame:
    opcode: int
    payload: bytes
    fin: bool = True


class WebSocketClosed(Exception):
    """Raised when the peer closes the connection (Close frame or socket EOF)."""


class WebSocketProtocolError(Exception):
    """Raised for wire-format violations (bad length encoding, fragmented
    messages, etc) -- deliberately distinct from WebSocketClosed so a
    handler could tell "hung up" apart from "sent garbage" if it cared to.
    """


def encode_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    """Encodes a SERVER -> client frame. Server frames must NOT be masked
    (RFC 6455 5.1) -- masking is a client-to-server-only requirement.
    """
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)

    if length < 126:
        header = struct.pack("!BB", b0, length)
    elif length < 65536:
        header = struct.pack("!BBH", b0, 126, length)
    else:
        header = struct.pack("!BBQ", b0, 127, length)

    return header + payload


class WebSocketConnection:
    """Sends/receives WebSocket messages over an already-upgraded socket.

    Owns a small read buffer seeded with any bytes the HTTP layer already
    read off the wire before handing the socket off (see
    reader.drain_buffered) -- reading straight from the raw socket after
    handoff would silently lose those.
    """

    def __init__(self, sock: socket.socket, initial_buffer: bytes = b""):
        self._sock = sock
        self._buf = bytearray(initial_buffer)
        self.closed = False

    def send_text(self, text: str) -> None:
        self._send(OPCODE_TEXT, text.encode("utf-8"))

    def send_binary(self, data: bytes) -> None:
        self._send(OPCODE_BINARY, data)

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            return
        try:
            self._sock.sendall(encode_frame(opcode, payload))
        except OSError:
            self.closed = True

    def close(self, code: int = 1000) -> None:
        if not self.closed:
            try:
                self._sock.sendall(encode_frame(OPCODE_CLOSE, struct.pack("!H", code)))
            except OSError:
                pass
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def receive(self) -> Optional[str]:
        """Blocks until the next TEXT (or BINARY, decoded best-effort as
        UTF-8) message arrives. PING is answered with PONG automatically
        and transparently; PONG is discarded. Returns None once the
        connection is closed, for either side's reason.
        """
        while True:
            try:
                frame = self._read_frame()
            except (WebSocketClosed, WebSocketProtocolError, OSError, struct.error):
                self.closed = True
                return None

            if frame.opcode == OPCODE_CLOSE:
                self.close()
                return None
            elif frame.opcode == OPCODE_PING:
                self._send(OPCODE_PONG, frame.payload)
                continue
            elif frame.opcode == OPCODE_PONG:
                continue
            elif frame.opcode in (OPCODE_TEXT, OPCODE_BINARY):
                if not frame.fin:
                    raise WebSocketProtocolError("Fragmented messages are not supported")
                return frame.payload.decode("utf-8", errors="replace")
            elif frame.opcode == OPCODE_CONTINUATION:
                raise WebSocketProtocolError("Fragmented messages are not supported")
            # Unknown opcode -- ignore and keep reading, per RFC 6455 leniency guidance.

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketClosed()
            self._buf.extend(chunk)
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result

    def _read_frame(self) -> Frame:
        header = self._recv_exact(2)
        b0, b1 = header[0], header[1]

        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        payload_len = b1 & 0x7F

        if payload_len == 126:
            payload_len = struct.unpack("!H", self._recv_exact(2))[0]
        elif payload_len == 127:
            payload_len = struct.unpack("!Q", self._recv_exact(8))[0]

        mask_key = self._recv_exact(4) if masked else None
        payload = self._recv_exact(payload_len)

        if mask_key is not None:
            # RFC 6455 5.3: unmask by XOR-ing each payload byte with the
            # mask byte at the same position mod 4.
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        return Frame(opcode=opcode, payload=payload, fin=fin)


# A WebSocket handler owns the connection until it returns -- typically a
# loop of conn.receive() / conn.send_text() calls -- unlike a normal HTTP
# Handler, which returns a single HTTPResponse and is done.
WSHandler = Callable[[WebSocketConnection, HTTPRequest], None]
