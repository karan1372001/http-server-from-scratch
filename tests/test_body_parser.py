import pytest

from http_server.body_parser import BodyParseError, parse_body
from http_server.parser import HTTPRequest


def make_req(body: bytes, content_type: str) -> HTTPRequest:
    return HTTPRequest(
        method="POST",
        path="/x",
        query={},
        raw_query="",
        version="HTTP/1.1",
        headers={"content-type": content_type},
        body=body,
    )


def test_parses_json_body():
    req = make_req(b'{"a": 1, "b": "two"}', "application/json")
    parsed = parse_body(req)
    assert parsed.json == {"a": 1, "b": "two"}


def test_empty_json_body_is_none_not_an_error():
    req = make_req(b"", "application/json")
    parsed = parse_body(req)
    assert parsed.json is None


def test_rejects_invalid_json():
    req = make_req(b"{not valid json", "application/json")
    with pytest.raises(BodyParseError) as exc:
        parse_body(req)
    assert exc.value.status_code == 400


def test_parses_urlencoded_form():
    req = make_req(b"name=Ada&lang=Python", "application/x-www-form-urlencoded")
    parsed = parse_body(req)
    assert parsed.form == {"name": ["Ada"], "lang": ["Python"]}
    assert parsed.form_value("name") == "Ada"
    assert parsed.form_value("missing", "default") == "default"


def test_parses_multipart_text_field_and_file():
    boundary = "BOUNDARY123"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="username"\r\n\r\n'
        f"ada\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="avatar"; filename="pic.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
        f"hello file contents\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = make_req(body, f"multipart/form-data; boundary={boundary}")
    parsed = parse_body(req)

    assert parsed.form == {"username": ["ada"]}
    assert len(parsed.files) == 1
    f = parsed.files[0]
    assert f.field_name == "avatar"
    assert f.filename == "pic.txt"
    assert f.content_type == "text/plain"
    assert f.data == b"hello file contents"


def test_multipart_with_multiple_files():
    boundary = "B"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="f1"; filename="a.txt"\r\n\r\n'
        f"AAA\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="f2"; filename="b.txt"\r\n\r\n'
        f"BBB\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    req = make_req(body, f"multipart/form-data; boundary={boundary}")
    parsed = parse_body(req)
    assert len(parsed.files) == 2
    assert {f.filename for f in parsed.files} == {"a.txt", "b.txt"}


def test_multipart_missing_boundary_raises_error():
    req = make_req(b"garbage", "multipart/form-data")
    with pytest.raises(BodyParseError) as exc:
        parse_body(req)
    assert exc.value.status_code == 400


def test_multipart_malformed_part_is_skipped_not_crashed():
    boundary = "B"
    # Second part has no header/body separator -- malformed, should be skipped.
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="ok"\r\n\r\n'
        f"fine\r\n"
        f"--{boundary}\r\n"
        f"this part has no proper headers at all"
        f"--{boundary}--\r\n"
    ).encode()
    req = make_req(body, f"multipart/form-data; boundary={boundary}")
    parsed = parse_body(req)  # must not raise
    assert parsed.form.get("ok") == ["fine"]


def test_unknown_content_type_returns_empty_parsed_body():
    req = make_req(b"raw stuff", "application/octet-stream")
    parsed = parse_body(req)
    assert parsed.json is None
    assert parsed.form == {}
    assert parsed.files == []


def test_no_content_type_returns_empty_parsed_body():
    req = HTTPRequest(
        method="POST", path="/x", query={}, raw_query="", version="HTTP/1.1", headers={}, body=b"stuff"
    )
    parsed = parse_body(req)
    assert parsed.json is None
    assert parsed.form == {}
