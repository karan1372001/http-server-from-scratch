"""Tests around server startup/shutdown.

Specifically: stopping the server from another thread while its accept()
call is blocked waiting for a connection must not raise inside the server's
own thread. This is exactly the race that surfaced as a
PytestUnhandledThreadExceptionWarning during the Phase 2 test run -- the
main thread's close() call and the server thread's own cleanup both touched
self._sock without coordination.
"""
import threading
import time

from http_server.response import make_response
from http_server.server import HTTPServer


def _noop_handler(req):
    return make_response(200, b"ok")


def test_close_during_blocked_accept_does_not_raise_in_server_thread():
    server = HTTPServer(host="127.0.0.1", port=8123, handler=_noop_handler, poll_interval=0.1)
    errors = []

    def run():
        try:
            server.serve_forever()
        except Exception as e:  # this is exactly what the fix prevents
            errors.append(e)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    time.sleep(0.3)  # give it time to reach the blocking accept() call at least once

    server.close()
    thread.join(timeout=2)

    assert not thread.is_alive(), "server thread did not shut down in time"
    assert errors == [], f"server thread raised an exception during shutdown: {errors}"
