import time

from http_server.middleware import (
    access_log_middleware,
    apply_middleware,
    cors_middleware,
    gzip_middleware,
    rate_limit_middleware,
)
from http_server.parser import HTTPRequest
from http_server.response import HTTPResponse, make_response


def make_req(method="GET", path="/", headers=None, **overrides):
    defaults = dict(
        method=method,
        path=path,
        query={},
        raw_query="",
        version="HTTP/1.1",
        headers=headers or {},
        body=b"",
    )
    defaults.update(overrides)
    return HTTPRequest(**defaults)


# --- apply_middleware ---


def test_apply_middleware_runs_in_declared_order():
    calls = []

    def mw_a(next_handler):
        def wrapped(req):
            calls.append("a-before")
            resp = next_handler(req)
            calls.append("a-after")
            return resp

        return wrapped

    def mw_b(next_handler):
        def wrapped(req):
            calls.append("b-before")
            resp = next_handler(req)
            calls.append("b-after")
            return resp

        return wrapped

    def base(req):
        calls.append("handler")
        return make_response(200)

    handler = apply_middleware(base, [mw_a, mw_b])
    handler(make_req())

    # mw_a is listed first -> it's outermost -> sees the request first, response last.
    assert calls == ["a-before", "b-before", "handler", "b-after", "a-after"]


# --- access_log_middleware ---


def test_access_log_middleware_logs_one_line_with_key_fields():
    logged = []

    def base(req):
        return make_response(200, b"hello")

    handler = access_log_middleware(log_fn=logged.append)(base)
    req = make_req(method="GET", path="/hi", client_addr=("127.0.0.1", 5555))
    handler(req)

    assert len(logged) == 1
    line = logged[0]
    assert "127.0.0.1" in line
    assert "GET /hi HTTP/1.1" in line
    assert "200" in line
    assert "5" in line  # body byte count


def test_access_log_middleware_does_not_alter_response():
    def base(req):
        return make_response(201, b"created")

    handler = access_log_middleware(log_fn=lambda _: None)(base)
    resp = handler(make_req())
    assert resp.status_code == 201
    assert resp.body == b"created"


# --- gzip_middleware ---


def test_gzip_compresses_when_accepted_and_large_enough():
    big_body = b"x" * 2000

    def base(req):
        return make_response(200, big_body, {"Content-Type": "text/plain"})

    handler = gzip_middleware(min_size=512)(base)
    resp = handler(make_req(headers={"accept-encoding": "gzip, deflate"}))

    assert resp.headers.get("Content-Encoding") == "gzip"
    assert len(resp.body) < len(big_body)
    assert resp.headers["Content-Length"] == str(len(resp.body))


def test_gzip_skips_when_client_does_not_accept_it():
    big_body = b"x" * 2000

    def base(req):
        return make_response(200, big_body)

    handler = gzip_middleware(min_size=512)(base)
    resp = handler(make_req(headers={}))  # no accept-encoding at all

    assert "Content-Encoding" not in resp.headers
    assert resp.body == big_body


def test_gzip_skips_small_bodies():
    small_body = b"tiny"

    def base(req):
        return make_response(200, small_body)

    handler = gzip_middleware(min_size=512)(base)
    resp = handler(make_req(headers={"accept-encoding": "gzip"}))

    assert "Content-Encoding" not in resp.headers
    assert resp.body == small_body


def test_gzip_skips_image_content_types():
    body = b"x" * 2000

    def base(req):
        return make_response(200, body, {"Content-Type": "image/png"})

    handler = gzip_middleware(min_size=512)(base)
    resp = handler(make_req(headers={"accept-encoding": "gzip"}))

    assert "Content-Encoding" not in resp.headers


# --- cors_middleware ---


def test_cors_adds_headers_to_normal_response():
    def base(req):
        return make_response(200, b"ok")

    handler = cors_middleware(allow_origin="https://example.com")(base)
    resp = handler(make_req(method="GET"))

    assert resp.headers["Access-Control-Allow-Origin"] == "https://example.com"
    assert "Access-Control-Allow-Methods" in resp.headers


def test_cors_answers_preflight_without_calling_the_handler():
    called = []

    def base(req):
        called.append(True)
        return make_response(200, b"should not run")

    handler = cors_middleware()(base)
    resp = handler(make_req(method="OPTIONS"))

    assert called == []
    assert resp.status_code == 204
    assert "Access-Control-Allow-Origin" in resp.headers


# --- rate_limit_middleware ---


def test_rate_limit_allows_requests_under_the_cap():
    def base(req):
        return make_response(200, b"ok")

    handler = rate_limit_middleware(max_requests=3, window_seconds=60)(base)
    req = make_req(client_addr=("1.2.3.4", 1111))

    for _ in range(3):
        resp = handler(req)
        assert resp.status_code == 200


def test_rate_limit_rejects_requests_over_the_cap_with_429():
    def base(req):
        return make_response(200, b"ok")

    handler = rate_limit_middleware(max_requests=3, window_seconds=60)(base)
    req = make_req(client_addr=("1.2.3.4", 1111))

    for _ in range(3):
        handler(req)

    resp = handler(req)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_rate_limit_tracks_ips_independently():
    def base(req):
        return make_response(200, b"ok")

    handler = rate_limit_middleware(max_requests=1, window_seconds=60)(base)
    req_a = make_req(client_addr=("1.1.1.1", 1))
    req_b = make_req(client_addr=("2.2.2.2", 2))

    assert handler(req_a).status_code == 200
    assert handler(req_a).status_code == 429  # A is now over its own limit
    assert handler(req_b).status_code == 200  # B is unaffected by A's usage


def test_rate_limit_window_resets_after_time_passes():
    def base(req):
        return make_response(200, b"ok")

    handler = rate_limit_middleware(max_requests=1, window_seconds=0.2)(base)
    req = make_req(client_addr=("3.3.3.3", 3))

    assert handler(req).status_code == 200
    assert handler(req).status_code == 429

    time.sleep(0.25)
    assert handler(req).status_code == 200  # window has slid past the old request
