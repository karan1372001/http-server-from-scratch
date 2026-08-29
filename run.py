"""Entry point: python run.py [host] [port]"""
import sys

from app import app_handler
from http_server.server import HTTPServer

if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8080
    HTTPServer(host=host, port=port, handler=app_handler).serve_forever()
