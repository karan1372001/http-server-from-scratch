import pytest

from http_server.parser import HTTPParseError, parse_headers, parse_request, parse_request_line


def test_parses_simple_get_request_line():
    method, path, query, raw_query, version = parse_request_line(b"GET /hello HTTP/1.1")
    assert method == "GET"
    assert path == "/hello"
    assert version == "HTTP/1.1"


def test_parses_query_string():
    _, path, query, raw_query, _ = parse_request_line(b"GET /items?page=2&limit=10 HTTP/1.1")
    assert path == "/items"
    assert query == {"page": ["2"], "limit": ["10"]}


def test_rejects_missing_parts():
    with pytest.raises(HTTPParseError) as exc:
        parse_request_line(b"GET /hello")
    assert exc.value.status_code == 400


def test_rejects_unknown_method():
    with pytest.raises(HTTPParseError) as exc:
        parse_request_line(b"FOO / HTTP/1.1")
    assert exc.value.status_code == 501


def test_rejects_bad_http_version():
    with pytest.raises(HTTPParseError) as exc:
        parse_request_line(b"GET / HTTP/9.9")
    assert exc.value.status_code == 505


def test_rejects_target_without_leading_slash():
    with pytest.raises(HTTPParseError) as exc:
        parse_request_line(b"GET hello HTTP/1.1")
    assert exc.value.status_code == 400


def test_url_decodes_path_after_splitting_query():
    _, path, query, raw_query, _ = parse_request_line(b"GET /a%20b?x=1%202 HTTP/1.1")
    assert path == "/a b"
    assert query == {"x": ["1 2"]}


def test_parses_headers():
    raw = b"Host: example.com\r\nContent-Length: 5\r\n"
    headers = parse_headers(raw)
    assert headers == {"host": "example.com", "content-length": "5"}


def test_header_names_are_case_insensitive_via_lowercasing():
    headers = parse_headers(b"Content-Type: text/plain\r\n")
    assert headers["content-type"] == "text/plain"


def test_rejects_header_without_colon():
    with pytest.raises(HTTPParseError) as exc:
        parse_headers(b"BrokenHeaderNoColon\r\n")
    assert exc.value.status_code == 400


def test_rejects_too_many_headers():
    raw = b"".join(f"X-Header-{i}: v\r\n".encode() for i in range(200))
    with pytest.raises(HTTPParseError) as exc:
        parse_headers(raw)
    assert exc.value.status_code == 400


def test_handles_empty_headers():
    assert parse_headers(b"") == {}


def test_ignores_blank_lines_between_headers():
    # Defensive: shouldn't normally occur given how header_block is split,
    # but the parser should not choke on it.
    headers = parse_headers(b"Host: x\r\n\r\nContent-Length: 1\r\n")
    assert headers["host"] == "x"
    assert headers["content-length"] == "1"


def test_full_request_parse_combines_line_headers_and_body():
    req = parse_request(b"POST /items HTTP/1.1", b"Host: x\r\nContent-Length: 4", b"data")
    assert req.method == "POST"
    assert req.path == "/items"
    assert req.body == b"data"
    assert req.header("host") == "x"
    assert req.header("missing", "default") == "default"


def test_garbage_bytes_raise_parse_error_not_crash():
    with pytest.raises(HTTPParseError):
        parse_request_line(b"\x00\x01\x02 binary garbage")
