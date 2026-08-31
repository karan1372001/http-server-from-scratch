from pathlib import Path

from http_server.parser import HTTPRequest
from http_server.static_files import safe_resolve, serve_static_file


def test_serves_an_existing_file(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hi there")
    resp = serve_static_file(tmp_path, "/hello.txt")
    assert resp.status_code == 200
    assert resp.body == b"hi there"
    assert resp.headers["Content-Type"].startswith("text/plain")


def test_serves_a_nested_file(tmp_path: Path):
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "site.css").write_text("body{}")
    resp = serve_static_file(tmp_path, "/css/site.css")
    assert resp.status_code == 200
    assert resp.body == b"body{}"
    assert resp.headers["Content-Type"].startswith("text/css")


def test_404_for_missing_file(tmp_path: Path):
    resp = serve_static_file(tmp_path, "/nope.txt")
    assert resp.status_code == 404


def test_safe_resolve_blocks_dot_dot_traversal(tmp_path: Path):
    # A secret file OUTSIDE the served root, and the served root as a subdir.
    (tmp_path / "secret.txt").write_text("top secret")
    root = tmp_path / "public"
    root.mkdir()

    assert safe_resolve(root, "/../secret.txt") is None


def test_serve_static_file_returns_403_for_traversal_attempt(tmp_path: Path):
    (tmp_path / "secret.txt").write_text("top secret")
    root = tmp_path / "public"
    root.mkdir()
    (root / "ok.txt").write_text("fine")

    resp = serve_static_file(root, "/../secret.txt")
    assert resp.status_code == 403
    assert b"top secret" not in resp.body


def test_safe_resolve_blocks_deeply_nested_traversal(tmp_path: Path):
    (tmp_path / "secret.txt").write_text("top secret")
    root = tmp_path / "a" / "b" / "public"
    root.mkdir(parents=True)

    assert safe_resolve(root, "/../../../secret.txt") is None


def test_safe_resolve_allows_normal_nested_path(tmp_path: Path):
    root = tmp_path / "public"
    (root / "css").mkdir(parents=True)
    (root / "css" / "site.css").write_text("body{}")

    resolved = safe_resolve(root, "/css/site.css")
    assert resolved is not None
    assert resolved.read_text() == "body{}"


# --- Caching validators: ETag / Last-Modified / conditional requests ---


def _req_with_headers(headers: dict) -> HTTPRequest:
    return HTTPRequest(
        method="GET", path="/x", query={}, raw_query="", version="HTTP/1.1", headers=headers, body=b""
    )


def test_response_includes_etag_and_last_modified(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    resp = serve_static_file(tmp_path, "/a.txt")
    assert resp.status_code == 200
    assert resp.headers["ETag"].startswith('"') and resp.headers["ETag"].endswith('"')
    assert "Last-Modified" in resp.headers
    assert resp.headers["Accept-Ranges"] == "bytes"


def test_matching_if_none_match_returns_304(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    first = serve_static_file(tmp_path, "/a.txt")
    etag = first.headers["ETag"]

    second = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"if-none-match": etag}))
    assert second.status_code == 304
    assert second.body == b""


def test_non_matching_if_none_match_returns_full_200(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    resp = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"if-none-match": '"not-the-real-etag"'}))
    assert resp.status_code == 200
    assert resp.body == b"hello"


def test_matching_if_modified_since_returns_304(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    first = serve_static_file(tmp_path, "/a.txt")
    last_modified = first.headers["Last-Modified"]

    second = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"if-modified-since": last_modified}))
    assert second.status_code == 304


def test_etag_changes_when_content_changes(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("version one")
    etag1 = serve_static_file(tmp_path, "/a.txt").headers["ETag"]

    f.write_text("version two, totally different")
    etag2 = serve_static_file(tmp_path, "/a.txt").headers["ETag"]

    assert etag1 != etag2


# --- Range requests ---


def test_range_request_returns_206_with_correct_slice(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"0123456789")
    resp = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"range": "bytes=2-5"}))
    assert resp.status_code == 206
    assert resp.body == b"2345"
    assert resp.headers["Content-Range"] == "bytes 2-5/10"


def test_range_request_open_ended_returns_rest_of_file(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"0123456789")
    resp = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"range": "bytes=7-"}))
    assert resp.status_code == 206
    assert resp.body == b"789"
    assert resp.headers["Content-Range"] == "bytes 7-9/10"


def test_range_request_suffix_returns_last_n_bytes(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"0123456789")
    resp = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"range": "bytes=-3"}))
    assert resp.status_code == 206
    assert resp.body == b"789"


def test_range_request_beyond_file_size_returns_416(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"0123456789")
    resp = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"range": "bytes=100-200"}))
    assert resp.status_code == 416
    assert resp.headers["Content-Range"] == "bytes */10"


def test_multi_range_request_falls_back_to_full_200(tmp_path: Path):
    # Multi-range responses need multipart/byteranges bodies -- explicitly
    # out of scope, and this proves the fallback is a normal full file
    # rather than a crash or a wrong slice.
    (tmp_path / "a.txt").write_bytes(b"0123456789")
    resp = serve_static_file(tmp_path, "/a.txt", _req_with_headers({"range": "bytes=0-1,5-6"}))
    assert resp.status_code == 200
    assert resp.body == b"0123456789"


def test_response_without_request_still_works_no_conditional_logic(tmp_path: Path):
    # Backward-compat: req is optional, callers that don't pass one just
    # get a normal full response with caching headers present but unused.
    (tmp_path / "a.txt").write_text("hello")
    resp = serve_static_file(tmp_path, "/a.txt")
    assert resp.status_code == 200
    assert resp.body == b"hello"
