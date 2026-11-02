"""A refreshed session cookie reaches the browser on every response shape (F2).

WorkOS rotates refresh tokens, so dropping the re-sealed cookie signs the user out on the
next request. ``RefreshedSessionCookieMiddleware`` delivers it when FastAPI does not merge
the dependency's response headers (raised ``HTTPException`` or a returned ``Response``).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import httpx
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient

from revel.app import create_app
from revel.auth.runtime import AuthRuntime
from revel.auth.session_cookie import RefreshedSessionCookieMiddleware
from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal
from revel.settings import Settings

from .authkit import TestAuthKit
from .conftest import PUBLIC_BASE_URL
from .test_browser_session import ORIGIN, SESSION, _expired_session, _sign_in

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _session_set_cookies(response: httpx.Response) -> list[str]:
    return [
        value
        for value in response.headers.get_list("set-cookie")
        if value.split("=", 1)[0].strip() == SESSION
    ]


def _assert_rotated_once(
    response: httpx.Response, runtime: AuthRuntime, authkit: TestAuthKit
) -> None:
    [header] = _session_set_cookies(response)
    value = header.split("=", 1)[1].split(";", 1)[0]
    tokens, sid = runtime.require_browser().read_session(value)
    assert tokens.refresh_token != "rt_old" and sid == "session_browser_1"
    assert len(authkit.authenticate_requests) == 1
    attrs = header.lower()
    assert "httponly" in attrs and "samesite=lax" in attrs and "path=/" in attrs
    assert "max-age=0" not in attrs


def _app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    router = APIRouter()

    @router.get("/cookie-test/json-error")
    async def json_error(principal: CurrentUIPrincipal) -> JSONResponse:
        return JSONResponse({"detail": "nope"}, status_code=409)

    @router.get("/cookie-test/redirect")
    async def redirect(principal: CurrentUIPrincipal) -> RedirectResponse:
        return RedirectResponse("/somewhere", status_code=302)

    @router.get("/cookie-test/raise")
    async def raise_after(principal: CurrentUIPrincipal) -> dict[str, str]:
        raise HTTPException(status_code=418, detail="teapot")

    @router.post("/cookie-test/write")
    async def write(principal: SameOriginUIPrincipal) -> JSONResponse:
        return JSONResponse({"ok": True})

    # Ahead of the "/" MCP mount (test-only routes; app code adds via revel.api.routers()).
    app.router.routes[0:0] = router.routes
    return app


@pytest.fixture
def client(runtime: AuthRuntime, auth_settings: Settings) -> Iterator[TestClient]:
    with TestClient(_app(auth_settings), base_url=PUBLIC_BASE_URL) as test_client:
        yield test_client


def test_middleware_registered_by_create_app(auth_settings: Settings) -> None:
    app = create_app(auth_settings)
    assert [m.cls for m in app.user_middleware].count(RefreshedSessionCookieMiddleware) == 1


def test_refresh_on_plain_200_route_sets_cookie_once(
    client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    client.cookies.set(SESSION, _expired_session(runtime, authkit))
    response = client.get("/auth/session")
    assert response.status_code == 200
    _assert_rotated_once(response, runtime, authkit)


def test_refresh_then_http_exception_404_carries_cookie(
    client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    client.cookies.set(SESSION, _expired_session(runtime, authkit))
    response = client.post(f"/api/agents/{uuid4()}/disconnect", headers=ORIGIN)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}
    _assert_rotated_once(response, runtime, authkit)


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/cookie-test/json-error", 409),
        ("GET", "/cookie-test/redirect", 302),
        ("GET", "/cookie-test/raise", 418),
        ("POST", "/cookie-test/write", 200),
    ],
)
def test_refresh_then_returned_response_or_raise_carries_cookie(
    client: TestClient,
    authkit: TestAuthKit,
    runtime: AuthRuntime,
    method: str,
    path: str,
    status: int,
) -> None:
    client.cookies.set(SESSION, _expired_session(runtime, authkit))
    response = client.request(method, path, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == status
    _assert_rotated_once(response, runtime, authkit)
    # The rotated cookie works next time without another refresh.
    again = client.request(method, path, headers=ORIGIN, follow_redirects=False)
    assert again.status_code == status
    assert len(authkit.authenticate_requests) == 1
    assert "set-cookie" not in again.headers


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/auth/session", 200),
        ("GET", "/cookie-test/json-error", 409),
        ("GET", "/cookie-test/redirect", 302),
        ("GET", "/cookie-test/raise", 418),
        ("POST", "/api/agents/00000000-0000-0000-0000-000000000000/disconnect", 404),
    ],
)
def test_no_set_cookie_without_refresh(
    client: TestClient, authkit: TestAuthKit, method: str, path: str, status: int
) -> None:
    assert _sign_in(client, authkit).status_code == 302
    response = client.request(method, path, headers=ORIGIN, follow_redirects=False)
    assert response.status_code == status
    assert "set-cookie" not in response.headers
    assert authkit.authenticate_requests and all(
        r["grant_type"] != "refresh_token" for r in authkit.authenticate_requests
    )


def test_session_invalid_clearing_header_unchanged(client: TestClient) -> None:
    client.cookies.set(SESSION, "not-a-session")
    response = client.get("/cookie-test/json-error")
    assert response.status_code == 401
    [header] = _session_set_cookies(response)
    assert "max-age=0" in header.lower()


def test_refresh_rejected_keeps_cookie(
    client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    client.cookies.set(SESSION, _expired_session(runtime, authkit))
    authkit.refresh_tokens.clear()
    response = client.get("/cookie-test/redirect", follow_redirects=False)
    assert response.status_code == 401
    assert _session_set_cookies(response) == []


def _without_middleware(settings: Settings) -> FastAPI:
    app = create_app(settings)
    app.user_middleware = [
        m for m in app.user_middleware if m.cls is not RefreshedSessionCookieMiddleware
    ]
    return app


def test_mcp_and_health_byte_identical_with_middleware(
    runtime: AuthRuntime, auth_settings: Settings, authkit: TestAuthKit
) -> None:
    bearer = TestAuthKit.token(auth_settings, sub="user_alice")
    requests = [
        ("GET", "/health", {}, None),
        ("GET", "/healthz", {}, None),
        ("POST", "/mcp", MCP_HEADERS, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
        (
            "POST",
            "/mcp",
            {**MCP_HEADERS, "Authorization": f"Bearer {bearer}"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        ),
    ]
    with (
        TestClient(create_app(auth_settings), base_url=PUBLIC_BASE_URL) as with_mw,
        TestClient(_without_middleware(auth_settings), base_url=PUBLIC_BASE_URL) as without_mw,
    ):
        # Warm up so first-use side effects (user/delegation creation) do not differ.
        method, path, headers, body = requests[3]
        assert without_mw.request(method, path, headers=headers, json=body).status_code == 200
        for method, path, headers, body in requests:
            a = with_mw.request(method, path, headers=headers, json=body)
            b = without_mw.request(method, path, headers=headers, json=body)
            assert a.status_code == b.status_code
            assert a.content == b.content
            assert a.headers.multi_items() == b.headers.multi_items()
            assert "set-cookie" not in a.headers
    assert [r for r in authkit.authenticate_requests if r["grant_type"] == "refresh_token"] == []
