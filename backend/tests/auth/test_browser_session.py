"""Browser sign-in (WorkOS AuthKit hosted UI), sealed session cookies, refresh and CSRF."""

from __future__ import annotations

import time
from collections.abc import Iterator
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import anyio
import httpx
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from revel.app import create_app
from revel.auth.browser import (
    BrowserAuthConfig,
    CookieSealer,
    SessionInvalid,
    SessionTokens,
    safe_return_to,
)
from revel.auth.runtime import AuthRuntime, reset_auth_runtimes
from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal
from revel.settings import Settings

from .authkit import CLIENT_ID, COOKIE_PASSWORD, REDIRECT_URI, WORKOS_API, TestAuthKit, rsa_key
from .conftest import PUBLIC_BASE_URL, InMemoryIdentityRepository, RecordingHooks

SESSION = "revel_session"
STATE = "revel_auth_state"
ORIGIN = {"Origin": PUBLIC_BASE_URL}


def _set_cookies(response: httpx.Response) -> dict[str, SimpleCookie]:
    cookies: dict[str, SimpleCookie] = {}
    for header in response.headers.get_list("set-cookie"):
        jar = SimpleCookie()
        jar.load(header)
        for name in jar:
            cookies[name] = jar
    return cookies


def _login(client: TestClient, return_to: str | None = None) -> str:
    params = {"return_to": return_to} if return_to is not None else {}
    response = client.get("/auth/login", params=params, follow_redirects=False)
    assert response.status_code == 302
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def _sign_in(
    client: TestClient, authkit: TestAuthKit, user: str = "user_alice", return_to: str | None = None
) -> httpx.Response:
    state = _login(client, return_to)
    code = authkit.issue_code(user)
    return client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )


# --- login -------------------------------------------------------------------------------


def test_login_redirects_to_authkit_with_state_cookie(auth_client: TestClient) -> None:
    response = auth_client.get("/auth/login", follow_redirects=False)
    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{WORKOS_API}/user_management/authorize"
    )
    query = parse_qs(location.query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["response_type"] == ["code"]
    assert query["provider"] == ["authkit"]
    assert len(query["state"][0]) >= 32
    morsel = _set_cookies(response)[STATE][STATE]
    assert morsel["httponly"] and morsel["samesite"].lower() == "lax" and morsel["path"] == "/"
    assert not morsel["secure"]  # test env over http://localhost
    assert query["state"][0] not in morsel.value  # sealed, not plaintext
    # Each login gets a fresh state.
    assert _login(auth_client) != query["state"][0]


def test_secure_cookies_and_host_prefix_over_https(authkit: TestAuthKit) -> None:
    settings = authkit.settings(
        app_env="production",
        public_base_url="https://revel.example",
        workos_redirect_uri="https://revel.example/auth/callback",
    )
    authkit.install(settings, repository=InMemoryIdentityRepository())
    try:
        with TestClient(create_app(settings), base_url="https://revel.example") as client:
            response = client.get("/auth/login", follow_redirects=False)
            assert response.status_code == 302
            morsel = _set_cookies(response)[f"__Host-{STATE}"][f"__Host-{STATE}"]
            assert morsel["secure"] and morsel["httponly"] and morsel["path"] == "/"
    finally:
        reset_auth_runtimes()


@pytest.mark.parametrize(
    "password", [None, "replace-with-32+-char-random-string", "too-short-password"]
)
def test_browser_sign_in_fails_closed_without_cookie_password(
    authkit: TestAuthKit, password: str | None
) -> None:
    settings = authkit.settings(workos_cookie_password=password)
    authkit.install(settings, repository=InMemoryIdentityRepository())
    try:
        with TestClient(create_app(settings), base_url=PUBLIC_BASE_URL) as client:
            assert client.get("/auth/login", follow_redirects=False).status_code == 503
            assert client.get("/auth/session").status_code == 503
            assert client.get("/auth/callback?code=x&state=y").status_code == 503
    finally:
        reset_auth_runtimes()


# --- return_to ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/",
        "//evil.example/path",
        "/\\evil.example",
        "\\\\evil.example",
        "javascript:alert(1)",
        "evil.example",
        "/ok\r\nSet-Cookie: x=y",
        "/auth/logout",
        "",
        "/" + "a" * 3000,
    ],
)
def test_return_to_open_redirects_rejected(
    auth_client: TestClient, authkit: TestAuthKit, value: str
) -> None:
    assert safe_return_to(value) == "/"
    response = _sign_in(auth_client, authkit, return_to=value)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_return_to_same_origin_path_kept(auth_client: TestClient, authkit: TestAuthKit) -> None:
    response = _sign_in(auth_client, authkit, return_to="/profile/policy?tab=audiences")
    assert response.headers["location"] == "/profile/policy?tab=audiences"


# --- callback ----------------------------------------------------------------------------


def test_callback_creates_user_and_sets_sealed_cookie(
    auth_client: TestClient,
    authkit: TestAuthKit,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
) -> None:
    response = _sign_in(auth_client, authkit)
    assert response.status_code == 302
    cookies = _set_cookies(response)
    morsel = cookies[SESSION][SESSION]
    assert morsel["httponly"] and morsel["samesite"].lower() == "lax" and morsel["path"] == "/"
    assert int(morsel["max-age"]) > 0
    # Sealed: neither token appears in the cookie.
    issued = authkit.authenticate_requests[-1]
    assert issued["grant_type"] == "authorization_code"
    assert issued["client_secret"] == "sk_test_fake"
    assert "rt_" not in morsel.value and "eyJ" not in morsel.value
    # State cookie is consumed.
    assert cookies[STATE][STATE]["max-age"] == "0"

    [user] = repo.users.values()
    assert (user.workos_user_id, user.email, user.display_name) == (
        "user_alice",
        "alice@revel.test",
        "Alice Liddell",
    )
    assert hooks.calls == [("on_user_created", {"user_id": user.id})]
    assert repo.agents == {} and repo.delegations == {}
    # Profile came from the authenticate response; no extra directory call.
    assert authkit.user_requests == []

    session = auth_client.get("/auth/session")
    assert session.status_code == 200
    body = session.json()
    assert body["user"]["id"] == str(user.id)
    assert body["agent"] is None and body["via"] == "ui"

    # Signing in again reuses the same user and does not re-run the hook.
    auth_client.cookies.clear()
    assert _sign_in(auth_client, authkit).status_code == 302
    assert len(repo.users) == 1 and len(hooks.calls) == 1


def test_callback_without_state_cookie_rejected(
    auth_client: TestClient, authkit: TestAuthKit
) -> None:
    state = _login(auth_client)
    auth_client.cookies.clear()
    code = authkit.issue_code()
    response = auth_client.get(
        "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
    )
    assert response.status_code == 400
    assert authkit.authenticate_requests == []
    assert SESSION not in _set_cookies(response)


@pytest.mark.parametrize("state", [None, "", "wrong-state"])
def test_callback_bad_or_missing_state_rejected(
    auth_client: TestClient, authkit: TestAuthKit, state: str | None
) -> None:
    _login(auth_client)
    params = {"code": authkit.issue_code()}
    if state is not None:
        params["state"] = state
    response = auth_client.get("/auth/callback", params=params, follow_redirects=False)
    assert response.status_code == 400
    assert authkit.authenticate_requests == []
    assert SESSION not in _set_cookies(response)


def test_callback_expired_state_rejected(
    auth_client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    sealer = CookieSealer(COOKIE_PASSWORD)
    stale = sealer.seal("state", {"state": "s", "return_to": "/", "exp": int(time.time()) - 1})
    auth_client.cookies.set(STATE, stale)
    response = auth_client.get(
        "/auth/callback", params={"code": authkit.issue_code(), "state": "s"}
    )
    assert response.status_code == 400
    assert authkit.authenticate_requests == []


def test_callback_session_cookie_cannot_stand_in_for_state(
    auth_client: TestClient, authkit: TestAuthKit
) -> None:
    sealer = CookieSealer(COOKIE_PASSWORD)
    forged = sealer.seal("session", {"state": "s", "return_to": "/", "exp": 2**40})
    auth_client.cookies.set(STATE, forged)
    response = auth_client.get(
        "/auth/callback", params={"code": authkit.issue_code(), "state": "s"}
    )
    assert response.status_code == 400


def test_callback_error_or_missing_code(auth_client: TestClient, authkit: TestAuthKit) -> None:
    state = _login(auth_client)
    response = auth_client.get("/auth/callback", params={"state": state, "error": "access_denied"})
    assert response.status_code == 400
    state = _login(auth_client)
    assert auth_client.get("/auth/callback", params={"state": state}).status_code == 400
    assert authkit.authenticate_requests == []


def test_callback_code_rejected_by_workos(auth_client: TestClient, authkit: TestAuthKit) -> None:
    state = _login(auth_client)
    response = auth_client.get("/auth/callback", params={"code": "unknown", "state": state})
    assert response.status_code == 401
    assert SESSION not in _set_cookies(response)


def test_callback_workos_outage(auth_client: TestClient, authkit: TestAuthKit) -> None:
    authkit.authenticate_status = 503
    response = _sign_in(auth_client, authkit)
    assert response.status_code == 502
    assert SESSION not in _set_cookies(response)


def test_callback_rejects_token_from_wrong_key(
    auth_client: TestClient, authkit: TestAuthKit, repo: InMemoryIdentityRepository
) -> None:
    original = authkit.session_token

    def forged(**kwargs):  # type: ignore[no-untyped-def]
        return original(key=rsa_key("attacker"), **kwargs)

    authkit.session_token = forged  # type: ignore[method-assign]
    response = _sign_in(auth_client, authkit)
    assert response.status_code == 401
    assert repo.users == {}


# --- session cookie ----------------------------------------------------------------------


def test_no_cookie_is_401_json(auth_client: TestClient) -> None:
    response = auth_client.get("/auth/session")
    assert response.status_code == 401
    assert response.json() == {"detail": "Not signed in"}
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize("kind", ["garbage", "tampered", "other_password", "state_purpose"])
def test_invalid_cookie_is_401_and_cleared(
    auth_client: TestClient, authkit: TestAuthKit, kind: str
) -> None:
    _sign_in(auth_client, authkit)
    good = auth_client.cookies[SESSION]
    token = TestAuthKit.session_token()
    values = {
        "garbage": "not-a-session",
        "tampered": good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB"),
        "other_password": CookieSealer("x" * 40).seal(
            "session", {"at": token, "rt": "rt", "sid": None}
        ),
        "state_purpose": CookieSealer(COOKIE_PASSWORD).seal(
            "state", {"at": token, "rt": "rt", "sid": None}
        ),
    }
    auth_client.cookies.clear()
    auth_client.cookies.set(SESSION, values[kind])
    response = auth_client.get("/auth/session")
    assert response.status_code == 401
    assert _set_cookies(response)[SESSION][SESSION]["max-age"] == "0"


@pytest.mark.parametrize(
    "token_kwargs",
    [
        {"key": "attacker"},
        {"iss": "https://evil.example"},
        {"drop": ("sub",)},
        {"drop": ("exp",)},
    ],
)
def test_cookie_with_invalid_access_token_rejected(
    auth_client: TestClient, runtime: AuthRuntime, token_kwargs: dict
) -> None:
    if token_kwargs.get("key") == "attacker":
        token_kwargs = {"key": rsa_key("attacker")}
    browser = runtime.require_browser()
    token = TestAuthKit.session_token(**token_kwargs)
    auth_client.cookies.set(SESSION, browser.seal_session(SessionTokens(token, "rt_x"), None))
    assert auth_client.get("/auth/session").status_code == 401


@pytest.mark.parametrize(
    "issuer",
    [
        WORKOS_API,
        f"{WORKOS_API}/user_management/{CLIENT_ID}",
        "https://revel-test.authkit.app",
    ],
)
def test_accepted_workos_issuers(
    auth_client: TestClient, runtime: AuthRuntime, issuer: str
) -> None:
    browser = runtime.require_browser()
    token = TestAuthKit.session_token(iss=issuer)
    auth_client.cookies.set(SESSION, browser.seal_session(SessionTokens(token, "rt_x"), None))
    assert auth_client.get("/auth/session").status_code == 200


def _expired_session(runtime: AuthRuntime, authkit: TestAuthKit, rt: str = "rt_old") -> str:
    authkit.refresh_tokens[rt] = "user_alice"
    expired = TestAuthKit.session_token(iat=int(time.time()) - 900, exp=int(time.time()) - 120)
    return runtime.require_browser().seal_session(SessionTokens(expired, rt), "session_browser_1")


def test_expired_access_token_refreshes_and_resets_cookie(
    auth_client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    auth_client.cookies.set(SESSION, _expired_session(runtime, authkit))
    response = auth_client.get("/auth/session")
    assert response.status_code == 200
    assert response.json()["via"] == "ui"
    [refresh] = authkit.authenticate_requests
    assert refresh["grant_type"] == "refresh_token" and refresh["refresh_token"] == "rt_old"
    new_cookie = _set_cookies(response)[SESSION][SESSION]
    assert new_cookie.value and int(new_cookie["max-age"]) > 0
    tokens, sid = runtime.require_browser().read_session(new_cookie.value)
    assert tokens.refresh_token != "rt_old" and sid == "session_browser_1"
    # The re-set cookie is used next time without another refresh.
    auth_client.cookies.set(SESSION, new_cookie.value)
    again = auth_client.get("/auth/session")
    assert again.status_code == 200
    assert len(authkit.authenticate_requests) == 1
    assert SESSION not in _set_cookies(again)


def test_refresh_failure_is_401_and_clears_cookie(
    auth_client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    sealed = _expired_session(runtime, authkit)
    authkit.refresh_tokens.clear()  # revoked / already used
    auth_client.cookies.set(SESSION, sealed)
    response = auth_client.get("/auth/session")
    assert response.status_code == 401
    assert _set_cookies(response)[SESSION][SESSION]["max-age"] == "0"


def test_refresh_outage_is_503_and_keeps_cookie(
    auth_client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    auth_client.cookies.set(SESSION, _expired_session(runtime, authkit))
    authkit.authenticate_status = 500
    response = auth_client.get("/auth/session")
    assert response.status_code == 503
    assert SESSION not in _set_cookies(response)


def test_refresh_to_different_user_rejected(
    auth_client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    sealed = _expired_session(runtime, authkit)
    authkit.refresh_tokens["rt_old"] = "user_bob"
    auth_client.cookies.set(SESSION, sealed)
    assert auth_client.get("/auth/session").status_code == 401


@pytest.mark.anyio
async def test_concurrent_refreshes_share_one_workos_call(
    authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    sealed = _expired_session(runtime, authkit)
    browser = runtime.require_browser()
    results = []

    async def run() -> None:
        results.append(await browser.authenticate_cookie(sealed))

    async with anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(run)
    assert len(authkit.authenticate_requests) == 1
    assert len({r.tokens.refresh_token for r in results}) == 1
    assert all(r.refreshed for r in results)


@pytest.mark.anyio
async def test_missing_session_raises(runtime: AuthRuntime) -> None:
    with pytest.raises(SessionInvalid):
        await runtime.require_browser().authenticate_cookie(None)


# --- one identity across transports ------------------------------------------------------


def test_mcp_bearer_and_browser_session_resolve_to_same_user(
    auth_client: TestClient,
    authkit: TestAuthKit,
    auth_settings: Settings,
    repo: InMemoryIdentityRepository,
    hooks: RecordingHooks,
) -> None:
    bearer = TestAuthKit.token(auth_settings, sub="user_alice")
    me = auth_client.get("/me", headers={"Authorization": f"Bearer {bearer}"})
    assert me.status_code == 200
    assert _sign_in(auth_client, authkit).status_code == 302
    session = auth_client.get("/auth/session")
    assert session.status_code == 200
    assert session.json()["user"]["id"] == me.json()["user"]["id"]
    assert me.json()["agent"] is not None and session.json()["agent"] is None
    assert len(repo.users) == 1
    assert [name for name, _ in hooks.calls].count("on_user_created") == 1


def test_browser_first_then_mcp_same_user(
    auth_client: TestClient,
    authkit: TestAuthKit,
    auth_settings: Settings,
    repo: InMemoryIdentityRepository,
) -> None:
    _sign_in(auth_client, authkit)
    ui_user = auth_client.get("/auth/session").json()["user"]["id"]
    auth_client.cookies.clear()
    bearer = TestAuthKit.token(auth_settings, sub="user_alice")
    me = auth_client.get("/me", headers={"Authorization": f"Bearer {bearer}"})
    assert me.json()["user"]["id"] == ui_user
    assert len(repo.users) == 1


def test_cookie_never_authenticates_mcp_or_me(
    auth_client: TestClient, authkit: TestAuthKit
) -> None:
    _sign_in(auth_client, authkit)
    assert auth_client.get("/auth/session").status_code == 200  # cookie is valid
    assert auth_client.get("/me").status_code == 401
    response = auth_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream", **ORIGIN},
    )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]


def test_session_token_is_not_a_bearer_token(auth_client: TestClient) -> None:
    """A browser session access token (no MCP audience) is rejected on /me."""
    token = TestAuthKit.session_token()
    assert auth_client.get("/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# --- logout ------------------------------------------------------------------------------


def test_logout_clears_cookie_and_returns_workos_logout_url(
    auth_client: TestClient, authkit: TestAuthKit
) -> None:
    _sign_in(auth_client, authkit)
    response = auth_client.post("/auth/logout", headers=ORIGIN)
    assert response.status_code == 200
    url = urlsplit(response.json()["logout_url"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == (
        f"{WORKOS_API}/user_management/sessions/logout"
    )
    assert parse_qs(url.query) == {"session_id": ["session_browser_1"]}
    assert _set_cookies(response)[SESSION][SESSION]["max-age"] == "0"
    assert auth_client.get("/auth/session").status_code == 401


def test_logout_without_session(auth_client: TestClient) -> None:
    response = auth_client.post("/auth/logout", headers=ORIGIN)
    assert response.status_code == 200
    assert response.json() == {"logout_url": None}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Referer": "https://evil.example/x"},
    ],
)
def test_logout_requires_same_origin(
    auth_client: TestClient, authkit: TestAuthKit, headers: dict[str, str]
) -> None:
    _sign_in(auth_client, authkit)
    response = auth_client.post("/auth/logout", headers=headers)
    assert response.status_code == 403
    assert SESSION not in _set_cookies(response)
    assert auth_client.get("/auth/session").status_code == 200


def test_logout_accepts_same_origin_referer(auth_client: TestClient, authkit: TestAuthKit) -> None:
    _sign_in(auth_client, authkit)
    response = auth_client.post("/auth/logout", headers={"Referer": f"{PUBLIC_BASE_URL}/settings"})
    assert response.status_code == 200


# --- CSRF helper for F8 routes -----------------------------------------------------------


def _csrf_app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    router = APIRouter()

    @router.get("/ui-test/read")
    async def read(principal: CurrentUIPrincipal) -> dict[str, str]:
        return {"user_id": str(principal.user_id), "via": principal.via}

    @router.post("/ui-test/write")
    async def write(principal: SameOriginUIPrincipal) -> dict[str, str]:
        return {"user_id": str(principal.user_id)}

    # Insert ahead of the "/" MCP mount (F8 adds routes via revel.api.routers()).
    app.router.routes[0:0] = router.routes
    return app


@pytest.fixture
def csrf_client(runtime: AuthRuntime, auth_settings: Settings) -> Iterator[TestClient]:
    with TestClient(_csrf_app(auth_settings), base_url=PUBLIC_BASE_URL) as client:
        yield client


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Origin": PUBLIC_BASE_URL}, 200),
        ({"Origin": "http://localhost:5173"}, 200),  # dev/test only
        ({"Origin": "http://127.0.0.1:3000"}, 200),  # dev/test only
        ({"Referer": f"{PUBLIC_BASE_URL}/policy"}, 200),
        ({}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "https://localhost:8000"}, 403),
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "http://localhost.evil.example"}, 403),
        ({"Referer": "https://evil.example/"}, 403),
        ({"Origin": "https://evil.example", "Referer": f"{PUBLIC_BASE_URL}/"}, 403),
    ],
)
def test_same_origin_ui_principal(
    csrf_client: TestClient, authkit: TestAuthKit, headers: dict[str, str], expected: int
) -> None:
    _sign_in(csrf_client, authkit)
    assert csrf_client.post("/ui-test/write", headers=headers).status_code == expected


def test_csrf_check_runs_before_session(csrf_client: TestClient) -> None:
    # Cross-origin request without a session: 403 (not 401), no WorkOS call.
    assert (
        csrf_client.post("/ui-test/write", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert csrf_client.post("/ui-test/write", headers=ORIGIN).status_code == 401


def test_ui_principal_read_route_and_refresh_cookie_merged(
    csrf_client: TestClient, authkit: TestAuthKit, runtime: AuthRuntime
) -> None:
    csrf_client.cookies.set(SESSION, _expired_session(runtime, authkit))
    response = csrf_client.get("/ui-test/read")
    assert response.status_code == 200 and response.json()["via"] == "ui"
    assert int(_set_cookies(response)[SESSION][SESSION]["max-age"]) > 0


def test_production_csrf_allows_only_public_and_configured_origins(authkit: TestAuthKit) -> None:
    settings = authkit.settings(
        app_env="production",
        public_base_url="https://revel.example",
        allowed_origins="https://app.revel.example",
    )
    config = BrowserAuthConfig.from_settings(settings)
    assert config.secure_cookies and not config.development
    authkit.install(settings, repository=InMemoryIdentityRepository())
    try:
        with TestClient(_csrf_app(settings), base_url="https://revel.example") as client:
            for origin, expected in (
                ("https://revel.example", 401),  # passes CSRF; no session
                ("https://app.revel.example", 401),
                ("http://localhost:8000", 403),
                ("http://revel.example", 403),
            ):
                response = client.post("/ui-test/write", headers={"Origin": origin})
                assert response.status_code == expected, origin
    finally:
        reset_auth_runtimes()


def test_redirect_uri_defaults_to_public_base_url_callback(authkit: TestAuthKit) -> None:
    config = BrowserAuthConfig.from_settings(
        authkit.settings(public_base_url="https://revel.example/", workos_redirect_uri=None)
    )
    assert config.redirect_uri == "https://revel.example/auth/callback"
    assert config.configured
