from pathlib import Path

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
