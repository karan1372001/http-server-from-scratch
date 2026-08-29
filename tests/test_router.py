from http_server.parser import HTTPRequest
from http_server.response import make_response
from http_server.router import Router


def make_req(method="GET", path="/", **overrides):
    defaults = dict(
        method=method,
        path=path,
        query={},
        raw_query="",
        version="HTTP/1.1",
        headers={},
        body=b"",
    )
    defaults.update(overrides)
    return HTTPRequest(**defaults)


def test_matches_static_route():
    router = Router()

    @router.get("/hello")
    def handler(req):
        return make_response(200, b"hi")

    matched, params, allowed = router.match("GET", "/hello")
    assert matched is handler
    assert params == {}


def test_matches_single_path_param():
    router = Router()

    @router.get("/users/{id}")
    def handler(req):
        return make_response(200)

    matched, params, allowed = router.match("GET", "/users/42")
    assert matched is handler
    assert params == {"id": "42"}


def test_path_param_does_not_cross_slash_boundaries():
    router = Router()

    @router.get("/users/{id}")
    def handler(req):
        return make_response(200)

    matched, params, allowed = router.match("GET", "/users/42/extra")
    assert matched is None
    assert allowed == []


def test_catchall_param_matches_nested_path():
    router = Router()

    @router.get("/static/{filepath*}")
    def handler(req):
        return make_response(200)

    matched, params, allowed = router.match("GET", "/static/css/site.css")
    assert matched is handler
    assert params == {"filepath": "css/site.css"}


def test_method_mismatch_reports_allowed_methods_not_404():
    router = Router()

    @router.get("/x")
    def handler(req):
        return make_response(200)

    matched, params, allowed = router.match("POST", "/x")
    assert matched is None
    assert allowed == ["GET"]


def test_no_match_returns_empty_allowed_methods():
    router = Router()
    matched, params, allowed = router.match("GET", "/nope")
    assert matched is None
    assert allowed == []


def test_head_falls_back_to_matching_get_route():
    router = Router()

    @router.get("/x")
    def handler(req):
        return make_response(200)

    matched, params, allowed = router.match("HEAD", "/x")
    assert matched is handler


def test_as_handler_returns_404_for_unknown_path():
    router = Router()
    resp = router.as_handler()(make_req(path="/nope"))
    assert resp.status_code == 404


def test_as_handler_returns_405_with_allow_header_for_wrong_method():
    router = Router()

    @router.get("/x")
    def handler(req):
        return make_response(200)

    resp = router.as_handler()(make_req(method="POST", path="/x"))
    assert resp.status_code == 405
    assert resp.headers.get("Allow") == "GET"


def test_as_handler_populates_path_params_on_the_request():
    router = Router()
    seen = {}

    @router.get("/users/{id}")
    def handler(req):
        seen["id"] = req.path_params["id"]
        return make_response(200)

    router.as_handler()(make_req(path="/users/7"))
    assert seen["id"] == "7"


def test_multiple_methods_on_same_path_both_work():
    router = Router()

    @router.get("/items")
    def list_items(req):
        return make_response(200, b"list")

    @router.post("/items")
    def create_item(req):
        return make_response(201, b"created")

    get_resp = router.as_handler()(make_req(method="GET", path="/items"))
    post_resp = router.as_handler()(make_req(method="POST", path="/items"))
    assert get_resp.body == b"list"
    assert post_resp.body == b"created"
