"""Regression tests for the static-dashboard route in backend_app.py --
specifically the path-traversal fix in serve_dashboard().

Run with: pip install -r requirements-dev.txt && pytest test_backend_app.py

Why the traversal tests build a raw ASGI scope instead of using
TestClient.get(path) directly: httpx's URL class normalizes ".." segments
out of a request path before the request is ever sent (RFC 3986 dot-segment
removal), so client.get("/../../etc/passwd") would actually reach the app
as "GET /etc/passwd" -- silently not exercising the traversal at all. A
hand-built scope reproduces what a client that does NOT normalize (curl
--path-as-is, a raw socket, most traversal payloads in the wild) sends on
the wire. Per the ASGI spec, scope["path"] is already percent-decoded by
the server (uvicorn) before routing sees it -- so a percent-encoded
"..%2f..%2f" and a literal ".." both arrive at serve_dashboard() as the
same decoded string; both cases are included to document that explicitly,
not because they exercise different code paths.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

import backend_app


@pytest.fixture
def webapp_dist(tmp_path, monkeypatch):
    dist = tmp_path / "webapp_dist"
    dist.mkdir()
    (dist / "index.html").write_text("SPA-SHELL")
    (dist / "favicon.svg").write_text("<svg></svg>")
    # A real file OUTSIDE webapp_dist, sibling to it -- stands in for
    # /etc/passwd so the traversal test can actually prove something: the
    # pre-fix code (candidate.is_file() with no containment check) would
    # have served this successfully.
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    monkeypatch.setattr(backend_app, "WEBAPP_DIST", dist.resolve())
    return dist


@pytest.fixture
def client(webapp_dist):
    return TestClient(backend_app.app)


def test_real_file_served_as_itself(client):
    res = client.get("/favicon.svg")
    assert res.status_code == 200
    assert res.text == "<svg></svg>"


def test_unmatched_path_falls_back_to_spa(client):
    res = client.get("/some/deep/route")
    assert res.status_code == 200
    assert res.text == "SPA-SHELL"


async def _raw_get(app, decoded_path: str, raw_path_bytes: bytes):
    """Drives the ASGI app directly with `decoded_path` already in
    scope["path"] (as a real server would deliver it, percent-decoded),
    bypassing any client-side URL normalization."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "path": decoded_path,
            "raw_path": raw_path_bytes,
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "scheme": "http",
        },
        receive,
        send,
    )
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m["body"] for m in messages if m["type"] == "http.response.body")
    return status, body.decode()


@pytest.mark.parametrize(
    "decoded_path,raw_path_bytes",
    [
        ("/../../etc/passwd", b"/../../etc/passwd"),
        ("/../../etc/passwd", b"/..%2f..%2fetc%2fpasswd"),
    ],
    ids=["literal-dotdot", "percent-encoded"],
)
def test_traversal_outside_root_falls_back_to_spa(webapp_dist, decoded_path, raw_path_bytes):
    status, body = asyncio.run(_raw_get(backend_app.app, decoded_path, raw_path_bytes))
    assert status == 200
    assert body == "SPA-SHELL"


def test_traversal_to_a_real_sibling_file_is_blocked(webapp_dist):
    """Deterministic proof of the fix: ../secret.txt resolves to a file
    that genuinely exists (created by the fixture) just outside
    WEBAPP_DIST. Pre-fix, candidate.is_file() alone would have served it;
    post-fix, is_relative_to(WEBAPP_DIST) rejects it before that check."""
    status, body = asyncio.run(
        _raw_get(backend_app.app, "/../secret.txt", b"/../secret.txt")
    )
    assert status == 200
    assert body == "SPA-SHELL"
    assert "TOP SECRET" not in body
