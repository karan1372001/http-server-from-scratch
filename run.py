"""Entry point.

Examples:
    python run.py
    python run.py --mode threaded
    python run.py --mode async --port 9000
    python run.py --tls                      # HTTPS, single-threaded/threaded only

WebSocket routes (registered via @router.websocket in app.py) work in
--mode single and --mode threaded. Not yet supported in --mode async --
see README.md Phase 5 for why, same honest-limitation shape as TLS+async
in Phase 4.
"""
import argparse
import ssl

from app import app_handler, router
from http_server.async_server import AsyncHTTPServer
from http_server.server import HTTPServer
from http_server.thread_pool_server import ThreadPoolHTTPServer

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the from-scratch HTTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--mode", choices=["single", "threaded", "async"], default="single")
    parser.add_argument(
        "--tls",
        action="store_true",
        help="Serve over HTTPS using cert.pem/key.pem (run generate_cert.py first). "
        "Not supported with --mode async -- see README.md Phase 4.",
    )
    parser.add_argument("--cert", default="cert.pem")
    parser.add_argument("--key", default="key.pem")
    args = parser.parse_args()

    ssl_context = None
    if args.tls:
        if args.mode == "async":
            parser.error("--tls is not supported with --mode async yet -- see README.md Phase 4.")
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=args.cert, keyfile=args.key)

    if args.mode == "single":
        HTTPServer(
            host=args.host, port=args.port, handler=app_handler,
            ssl_context=ssl_context, ws_routes=router.ws_routes,
        ).serve_forever()
    elif args.mode == "threaded":
        ThreadPoolHTTPServer(
            host=args.host, port=args.port, handler=app_handler, num_workers=8,
            ssl_context=ssl_context, ws_routes=router.ws_routes,
        ).serve_forever()
    elif args.mode == "async":
        AsyncHTTPServer(host=args.host, port=args.port, handler=app_handler).serve_forever()
