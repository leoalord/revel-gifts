"""WorkOSTokenVerifier: signature, issuer, audience, expiry and claim checks."""

from __future__ import annotations

import logging
import time

import httpx
import jwt
import pytest

from revel.auth.config import AuthConfig, normalize_authkit_issuer
from revel.auth.tokens import JWKSCache, TokenRejected, WorkOSTokenVerifier
from revel.settings import Settings

from .authkit import ISSUER, TestAuthKit, rsa_key
from .conftest import RESOURCE

pytestmark = pytest.mark.anyio


@pytest.fixture
def verifier(auth_settings: Settings, authkit: TestAuthKit) -> WorkOSTokenVerifier:
    config = AuthConfig.from_settings(auth_settings)
    assert config.jwks_url is not None
    return WorkOSTokenVerifier(
        config, jwks=JWKSCache(config.jwks_url, http_client=authkit.http_client())
    )


async def _rejected(verifier: WorkOSTokenVerifier, token: str) -> str:
    assert await verifier.verify_token(token) is None
    with pytest.raises(TokenRejected) as info:
        await verifier.verify(token)
    return info.value.category


async def test_valid_token(verifier, make_token):
    token = make_token()
    access = await verifier.verify_token(token)
    assert access is not None
    assert access.subject == "user_alice"
    assert access.client_id == "client_muse"
    assert access.resource == RESOURCE
    assert access.claims == {"iss": ISSUER, "sid": "session_1"}
    assert access.expires_at and access.expires_at > time.time()


async def test_issuer_with_trailing_slash_accepted(verifier, make_token):
    assert await verifier.verify_token(make_token(iss=ISSUER + "/")) is not None


async def test_audience_list_containing_resource_accepted(verifier, make_token):
    assert await verifier.verify_token(make_token(aud=["https://other.test", RESOURCE])) is not None


async def test_azp_fallback_for_client_id(verifier, make_token):
    access = await verifier.verify_token(make_token(drop=("client_id",), azp="client_from_azp"))
    assert access is not None and access.client_id == "client_from_azp"


async def test_expired(verifier, make_token):
    now = int(time.time())
    assert await _rejected(verifier, make_token(iat=now - 600, exp=now - 120)) == "expired"


async def test_wrong_audience(verifier, make_token):
    assert (
        await _rejected(verifier, make_token(aud="https://api.revel.test/other"))
        == "invalid_audience"
    )


async def test_wrong_issuer(verifier, make_token):
    assert await _rejected(verifier, make_token(iss="https://evil.authkit.app")) == "invalid_issuer"


async def test_bad_signature_other_key(verifier, make_token):
    # Signed with a key that is not published, but claiming the published kid.
    assert await _rejected(verifier, make_token(key=rsa_key("other"))) == "invalid_signature"


async def test_alg_none_rejected(verifier, make_token):
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": RESOURCE,
            "sub": "user_alice",
            "client_id": "c",
            "iat": 1,
            "exp": 2**31,
        },
        key=None,
        algorithm="none",
        headers={"kid": "kid-primary"},
    )
    assert await _rejected(verifier, token) == "unsupported_alg"


async def test_hs256_rejected(verifier, make_token):
    token = make_token(key="shared-secret-that-is-at-least-32-bytes!", algorithm="HS256")
    assert await _rejected(verifier, token) == "unsupported_alg"


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "a.b", "....."])
async def test_malformed(verifier, token):
    assert await _rejected(verifier, token) == "malformed"


async def test_missing_sub(verifier, make_token):
    assert await _rejected(verifier, make_token(drop=("sub",))) == "missing_claim"


async def test_missing_exp(verifier, make_token):
    assert await _rejected(verifier, make_token(drop=("exp",))) == "missing_claim"


async def test_missing_client_id(verifier, make_token):
    assert await _rejected(verifier, make_token(drop=("client_id",))) == "missing_client_id"


async def test_missing_kid(verifier, make_token):
    assert await _rejected(verifier, make_token(kid=None)) == "missing_kid"


async def test_unknown_kid_triggers_single_refresh(verifier, make_token, authkit: TestAuthKit):
    assert await verifier.verify_token(make_token()) is not None
    assert authkit.jwks_requests == 1

    # Key rotation: AuthKit publishes a new key; a token with the new kid forces one refresh.
    authkit.published.append((rsa_key("rotated"), "kid-rotated"))
    assert (
        await verifier.verify_token(make_token(key=rsa_key("rotated"), kid="kid-rotated"))
        is not None
    )
    assert authkit.jwks_requests == 2

    # Known kids are served from cache.
    assert await verifier.verify_token(make_token()) is not None
    assert authkit.jwks_requests == 2


async def test_unknown_kid_refresh_is_rate_limited(verifier, make_token, authkit: TestAuthKit):
    assert await verifier.verify_token(make_token()) is not None
    assert (
        await _rejected(verifier, make_token(key=rsa_key("other"), kid="kid-unknown"))
        == "unknown_kid"
    )
    assert authkit.jwks_requests == 2  # one refresh for the unknown kid ...
    for _ in range(3):
        assert (
            await verifier.verify_token(make_token(key=rsa_key("other"), kid="kid-unknown")) is None
        )
    assert authkit.jwks_requests == 2  # ... and no refresh storm afterwards


async def test_jwks_unavailable_fails_closed(auth_settings, make_token):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    config = AuthConfig.from_settings(auth_settings)
    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        verifier = WorkOSTokenVerifier(config, jwks=JWKSCache(config.jwks_url, http_client=http))
        assert await _rejected(verifier, make_token()) == "jwks_unavailable"


async def test_jwks_outage_backs_off_and_serves_cached_keys(make_token):
    """After a failed refresh, at most one fetch per interval; cached keys keep working."""
    state = {"up": True, "requests": 0}
    published = TestAuthKit()

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"] += 1
        if not state["up"]:
            return httpx.Response(503)
        return published.handler(request)

    config = AuthConfig.from_settings(TestAuthKit.settings())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        # ttl=0: every request finds the cache stale and would refetch without backoff.
        jwks = JWKSCache(config.jwks_url, http_client=http, ttl_seconds=0)
        verifier = WorkOSTokenVerifier(config, jwks=jwks)
        assert await verifier.verify_token(make_token()) is not None
        assert state["requests"] == 1

        state["up"] = False
        for _ in range(6):
            assert await verifier.verify_token(make_token()) is not None  # cached key
        assert state["requests"] == 2  # one failed refetch, then backoff

        # Unknown kid during backoff fails closed without another fetch.
        token = make_token(key=rsa_key("other"), kid="kid-new")
        assert await _rejected(verifier, token) == "jwks_unavailable"
        assert state["requests"] == 2


async def test_jwks_non_object_body_fails_closed(make_token):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"kty": "RSA"}])

    config = AuthConfig.from_settings(TestAuthKit.settings())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verifier = WorkOSTokenVerifier(config, jwks=JWKSCache(config.jwks_url, http_client=http))
        assert await _rejected(verifier, make_token()) == "jwks_unavailable"


@pytest.mark.parametrize(
    "domain",
    [
        None,
        "",
        "  ",
        "<your-authkit-domain>",
        "replace-with-authkit-domain",
        "changeme",
        "TODO",
    ],
)
async def test_unconfigured_domain_fails_closed(domain, make_token):
    config = AuthConfig.from_settings(TestAuthKit.settings(workos_authkit_domain=domain))
    assert not config.configured
    verifier = WorkOSTokenVerifier(config)
    assert await _rejected(verifier, make_token()) == "not_configured"


@pytest.mark.parametrize(
    "domain", ["todo-gifts.authkit.app", "your-tenant.authkit.app", "placeholder-co.authkit.app"]
)
async def test_real_domains_containing_placeholder_words_accepted(domain):
    assert AuthConfig.from_settings(TestAuthKit.settings(workos_authkit_domain=domain)).configured


@pytest.mark.parametrize(
    ("raw", "allow_local", "expected"),
    [
        ("tenant.authkit.app", False, "https://tenant.authkit.app"),
        ("https://tenant.authkit.app/", False, "https://tenant.authkit.app"),
        ("http://tenant.authkit.app", True, None),
        ("http://localhost:9000", False, None),
        ("http://localhost:9000", True, "http://localhost:9000"),
    ],
)
async def test_normalize_authkit_issuer(raw, allow_local, expected):
    assert normalize_authkit_issuer(raw, allow_http_localhost=allow_local) == expected


@pytest.mark.parametrize(("app_env", "allowed"), [("production", False), ("development", True)])
async def test_http_localhost_issuer_only_in_dev(app_env, allowed):
    settings = TestAuthKit.settings(
        app_env=app_env,
        public_base_url="https://api.revel.test",
        workos_authkit_domain="http://localhost:9000",
    )
    assert AuthConfig.from_settings(settings).configured is allowed


async def test_rejection_logs_category_only(verifier, make_token, caplog):
    token = make_token(aud="https://wrong.test")
    with caplog.at_level(logging.DEBUG):
        assert await verifier.verify_token(token) is None
    text = caplog.text
    assert "invalid_audience" in text
    assert token not in text
    assert token.split(".")[1] not in text
    assert "user_alice" not in text
