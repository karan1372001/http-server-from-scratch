"""TLS tests: generates a real self-signed certificate and performs a real
TLS handshake against the running server -- not a mock, an actual
client-side `ssl` connection, same as a browser would attempt (minus
certificate trust, since it's self-signed).
"""
import shutil
import socket
import ssl
import subprocess
import threading
import time

import pytest

from http_server.response import make_response
from http_server.server import HTTPServer
from http_server.thread_pool_server import ThreadPoolHTTPServer

HOST = "127.0.0.1"

openssl_available = shutil.which("openssl") is not None


def _echo_handler(req):
    return make_response(200, b"secure hello", {"Content-Type": "text/plain"})


@pytest.fixture(scope="module")
def self_signed_cert(tmp_path_factory):
    if not openssl_available:
        pytest.skip("openssl not available on this machine")

    tmp_dir = tmp_path_factory.mktemp("tls")
    cert_path = tmp_dir / "cert.pem"
    key_path = tmp_dir / "key.pem"

    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key_path),
            "-out", str(cert_path),
            "-days", "1",
            "-subj", "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    return str(cert_path), str(key_path)


def _make_server_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


def _make_client_ssl_context() -> ssl.SSLContext:
    # Self-signed -- a real client would refuse to trust this, exactly like
    # a browser would show a warning. For the test, we only care whether
    # the TLS handshake and the HTTP exchange over it work correctly, so
    # certificate verification is turned off deliberately here.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


@pytest.mark.skipif(not openssl_available, reason="openssl not available on this machine")
def test_single_threaded_server_serves_https(self_signed_cert):
    cert_path, key_path = self_signed_cert
    server_ctx = _make_server_ssl_context(cert_path, key_path)
    server = HTTPServer(host=HOST, port=8501, handler=_echo_handler, poll_interval=0.1, ssl_context=server_ctx)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        client_ctx = _make_client_ssl_context()
        with socket.create_connection((HOST, 8501), timeout=5) as raw_sock:
            with client_ctx.wrap_socket(raw_sock, server_hostname="localhost") as tls_sock:
                tls_sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                tls_sock.settimeout(3)
                chunks = []
                while True:
                    chunk = tls_sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                resp = b"".join(chunks)
    finally:
        server.close()
        thread.join(timeout=3)

    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert b"secure hello" in resp


@pytest.mark.skipif(not openssl_available, reason="openssl not available on this machine")
def test_plain_http_request_to_tls_port_fails_cleanly_not_a_crash(self_signed_cert):
    # Sends a plain, un-encrypted HTTP request at a TLS-only port -- proves
    # a bad/missing handshake gets dropped gracefully instead of taking the
    # whole server down (the ssl.SSLError / OSError catch in server.py).
    cert_path, key_path = self_signed_cert
    server_ctx = _make_server_ssl_context(cert_path, key_path)
    server = HTTPServer(host=HOST, port=8502, handler=_echo_handler, poll_interval=0.1, ssl_context=server_ctx)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        with socket.create_connection((HOST, 8502), timeout=5) as sock:
            sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            sock.settimeout(2)
            try:
                resp = sock.recv(4096)
            except (socket.timeout, ConnectionResetError, OSError):
                resp = b""

        # Whatever happened, the server itself must still be alive and able
        # to serve a REAL TLS connection right afterward.
        client_ctx = _make_client_ssl_context()
        with socket.create_connection((HOST, 8502), timeout=5) as raw_sock2:
            with client_ctx.wrap_socket(raw_sock2, server_hostname="localhost") as tls_sock2:
                tls_sock2.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                tls_sock2.settimeout(3)
                real_resp = tls_sock2.recv(4096)
    finally:
        server.close()
        thread.join(timeout=3)

    assert resp == b"" or not resp.startswith(b"HTTP/1.1 200")  # the plain request never got a real response
    assert real_resp.startswith(b"HTTP/1.1 200 OK")  # but the server survived and works for real TLS clients


@pytest.mark.skipif(not openssl_available, reason="openssl not available on this machine")
def test_thread_pool_server_serves_https_too(self_signed_cert):
    cert_path, key_path = self_signed_cert
    server_ctx = _make_server_ssl_context(cert_path, key_path)
    server = ThreadPoolHTTPServer(
        host=HOST, port=8503, handler=_echo_handler, num_workers=4, poll_interval=0.1, ssl_context=server_ctx
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)

    try:
        client_ctx = _make_client_ssl_context()
        with socket.create_connection((HOST, 8503), timeout=5) as raw_sock:
            with client_ctx.wrap_socket(raw_sock, server_hostname="localhost") as tls_sock:
                tls_sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                tls_sock.settimeout(3)
                resp = tls_sock.recv(4096)
    finally:
        server.close()
        thread.join(timeout=3)

    assert resp.startswith(b"HTTP/1.1 200 OK")
