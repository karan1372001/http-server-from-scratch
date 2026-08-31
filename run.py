"""Entry point: python run.py [host] [port] [mode]

mode is one of: single (default, Phase 1/2 behavior), threaded, async
"""
import sys

from app import app_handler
from http_server.async_server import AsyncHTTPServer
from http_server.server import HTTPServer
from http_server.thread_pool_server import ThreadPoolHTTPServer

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    mode = sys.argv[3] if len(sys.argv) > 3 else "single"

    if mode == "single":
        HTTPServer(host=host, port=port, handler=app_handler).serve_forever()
    elif mode == "threaded":
        ThreadPoolHTTPServer(host=host, port=port, handler=app_handler, num_workers=8).serve_forever()
    elif mode == "async":
        AsyncHTTPServer(host=host, port=port, handler=app_handler).serve_forever()
    else:
        print(f"Unknown mode '{mode}'. Use: single, threaded, or async.")
        sys.exit(1)
