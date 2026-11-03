"""F8 static UI serving (revel.api.ui_static) and coexistence with the other route owners."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from auth.authkit import TestAuthKit
from fastapi.testclient import TestClient

from revel.api.ui_static import CONTENT_SECURITY_POLICY, _file_in_dist
from revel.app import create_app
from revel.auth.runtime import reset_auth_runtimes
from revel.settings import Settings

BASE_URL = "http://localhost:8000"
INDEX_HTML = "<!doctype html><title>Revel test UI</title><div id=root></div>"
ASSET_JS = "console.log('asset');"
SECRET = "outside-dist-secret"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML)
    (root / "assets" / "index-abc123.js").write_text(ASSET_JS)
    (root / "favicon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (tmp_path / "secret.txt").write_text(SECRET)
    return root


def _client(settings: Settings) -> Iterator[TestClient]:
    TestAuthKit().install(settings)
    try:
        with TestClient(create_app(settings), base_url=BASE_URL) as client:
            yield client
    finally:
        reset_auth_runtimes()


@pytest.fixture
def client(dist: Path) -> Iterator[TestClient]:
    yield from _client(TestAuthKit.settings(public_base_url=BASE_URL, frontend_dist_dir=dist))


@pytest.fixture
def unbuilt_client(tmp_path: Path) -> Iterator[TestClient]:
    missing = tmp_path / "no-dist"
    yield from _client(TestAuthKit.settings(public_base_url=BASE_URL, frontend_dist_dir=missing))


def _assert_security_headers(response) -> None:
    assert response.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "access-control-allow-origin" not in response.headers


def _assert_index(response) -> None:
    assert response.status_code == 200, response.text
    assert response.text == INDEX_HTML
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    _assert_security_headers(response)


def test_root_redirects_to_app(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/app/"


@pytest.mark.parametrize("path", ["/app", "/app/", "/app/index.html"])
def test_index_served_no_cache_with_csp(client: TestClient, path: str) -> None:
    _assert_index(client.get(path, follow_redirects=False))


@pytest.mark.parametrize(
    "path", ["/app/policy", "/app/contributions/123", "/app/connections?tab=pending"]
)
def test_spa_fallback_serves_index(client: TestClient, path: str) -> None:
    _assert_index(client.get(path))


def test_asset_served_immutable(client: TestClient) -> None:
    response = client.get("/app/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.text == ASSET_JS
    assert "javascript" in response.headers["content-type"]
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    _assert_security_headers(response)


def test_head_asset(client: TestClient) -> None:
    response = client.head("/app/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_non_asset_file_not_immutable(client: TestClient) -> None:
    response = client.get("/app/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


def test_missing_asset_is_404_not_html(client: TestClient) -> None:
    response = client.get("/app/assets/index-stale.js")
    assert response.status_code == 404
    assert not response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "path",
    [
        "/app//assets/missing.js",
        "/app/./assets/missing.js",
        "/app/%2E/assets/missing.js",
        "/app/assets//missing.js",
        "/app/policy/../assets/missing.js",
        "/app/policy/%2E%2E/assets/missing.js",
    ],
)
def test_unnormalized_missing_asset_is_404_not_html(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404, path
    assert not response.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/app//assets/index-abc123.js", "/app/./assets/index-abc123.js"])
def test_unnormalized_existing_asset_served(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == ASSET_JS
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize(
    "path",
    [
        "/app/..%2Fsecret.txt",
        "/app/%2e%2e/secret.txt",
        "/app/%2E%2E%2Fsecret.txt",
        "/app/assets/..%2F..%2Fsecret.txt",
        "/app/%2Fetc%2Fpasswd",
        "/app//etc/passwd",
        "/app/..%5Csecret.txt",
    ],
)
def test_traversal_never_serves_outside_dist(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert SECRET not in response.text
    assert "root:" not in response.text
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert response.text == INDEX_HTML


@pytest.mark.parametrize(
    "relative", ["../secret.txt", "assets/../../secret.txt", "/etc/passwd", "", ".", "a\x00b"]
)
def test_file_in_dist_guard(dist: Path, relative: str) -> None:
    assert _file_in_dist(dist.resolve(), relative) is None


def test_file_in_dist_allows_real_files(dist: Path) -> None:
    root = dist.resolve()
    assert _file_in_dist(root, "assets/index-abc123.js") == root / "assets" / "index-abc123.js"


def test_symlink_escaping_dist_not_served(client: TestClient, dist: Path) -> None:
    (dist / "assets" / "link.txt").symlink_to(dist.parent / "secret.txt")
    response = client.get("/app/assets/link.txt")
    assert SECRET not in response.text
    assert response.status_code == 404


def test_not_built_is_503(unbuilt_client: TestClient) -> None:
    for path in ("/app/", "/app/policy", "/app/assets/index-abc123.js"):
        response = unbuilt_client.get(path)
        assert response.status_code == 503
        assert response.json() == {"detail": "UI not built"}
    # The rest of the service still works without a built UI.
    assert unbuilt_client.get("/health").json() == {"status": "ok"}


def test_default_dist_dir_is_repo_frontend_dist() -> None:
    settings = Settings(_env_file=None)
    expected = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    assert settings.frontend_dist_dir is None
    assert settings.resolved_frontend_dist_dir == expected


def test_frontend_dist_dir_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path))
    assert Settings(_env_file=None).resolved_frontend_dist_dir == tmp_path


# --- other owners keep their routes with the UI router included ----------------------------


@pytest.mark.parametrize("path", ["/health", "/healthz"])
def test_health_still_routed(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "path",
    ["/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"],
)
def test_well_known_still_routed(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["resource"] == f"{BASE_URL}/mcp"


def test_auth_routes_still_routed(client: TestClient) -> None:
    session = client.get("/auth/session")
    assert session.status_code == 401
    assert session.json() == {"detail": "Not signed in"}

    login = client.get(
        "/auth/login", params={"return_to": "/app/policy?x=1"}, follow_redirects=False
    )
    assert login.status_code == 302
    assert "/user_management/authorize" in login.headers["location"]

    # POST /auth/logout keeps F2's same-origin check (no Origin from another site).
    logout = client.post("/auth/logout", headers={"Origin": "https://evil.example"})
    assert logout.status_code == 403


def test_me_still_bearer_only(client: TestClient) -> None:
    response = client.get("/me")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_mcp_still_routed(client: TestClient) -> None:
    unauthenticated = client.post("/mcp", json={}, headers=MCP_HEADERS)
    assert unauthenticated.status_code == 401
    assert "resource_metadata" in unauthenticated.headers["www-authenticate"]

    token = TestAuthKit.token(client.app.state.settings)
    init = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "revel"
