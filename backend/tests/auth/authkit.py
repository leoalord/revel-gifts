"""Test-only WorkOS AuthKit stand-in shared by F2 tests and other backend tests.

Provides a test AuthKit domain, locally generated RSA signing keys served as a
mocked JWKS (via ``httpx.MockTransport``), a mocked WorkOS User Management API and
a token minting helper. The application's real verifier is used unchanged; only
its outbound HTTP client is injected through ``configure_auth``. No network access.

Usage from another test module (``tests/`` is on ``sys.path`` under pytest)::

    from auth.authkit import TestAuthKit

    authkit = TestAuthKit()
    settings = authkit.settings(public_base_url="http://localhost:8000")
    authkit.install(settings)          # before create_app(settings)
    headers = {"Authorization": f"Bearer {authkit.token(settings)}"}
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from revel.auth.hooks import AuthzHooks
from revel.auth.repository import IdentityRepository
from revel.auth.runtime import AuthRuntime, configure_auth
from revel.settings import Settings

AUTHKIT_DOMAIN = "revel-test.authkit.app"
ISSUER = f"https://{AUTHKIT_DOMAIN}"
PRIMARY_KID = "kid-primary"


@cache
def rsa_key(name: str) -> rsa.RSAPrivateKey:
    """Deterministically cached per-name test key (generated once per session)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    data.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return data


@dataclass
class TestAuthKit:
    """Mocked AuthKit JWKS / metadata and WorkOS User Management endpoints."""

    __test__ = False  # not a pytest test class

    published: list[tuple[rsa.RSAPrivateKey, str]] = field(
        default_factory=lambda: [(rsa_key("primary"), PRIMARY_KID)]
    )
    jwks_requests: int = 0
    user_requests: list[str] = field(default_factory=list)
    user_auth_headers: list[str] = field(default_factory=list)
    users: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            "user_alice": {
                "object": "user",
                "id": "user_alice",
                "email": "alice@revel.test",
                "first_name": "Alice",
                "last_name": "Liddell",
            }
        }
    )
    user_api_status: int = 200

    # --- HTTP mock -----------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/oauth2/jwks":
            self.jwks_requests += 1
            return httpx.Response(
                200, json={"keys": [public_jwk(k, kid) for k, kid in self.published]}
            )
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/oauth2/authorize",
                    "token_endpoint": f"{ISSUER}/oauth2/token",
                    "registration_endpoint": f"{ISSUER}/oauth2/register",
                    "jwks_uri": f"{ISSUER}/oauth2/jwks",
                },
            )
        prefix = "https://api.workos.com/user_management/users/"
        if url.startswith(prefix):
            user_id = url[len(prefix) :]
            self.user_requests.append(user_id)
            self.user_auth_headers.append(request.headers.get("authorization", ""))
            if self.user_api_status != 200:
                return httpx.Response(self.user_api_status, json={"message": "error"})
            user = self.users.get(user_id)
            if user is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=user)
        return httpx.Response(599, json={"unexpected": url})

    def http_client(self) -> httpx.AsyncClient:
        # MockTransport holds no connections, so the client needs no explicit close.
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    # --- settings / runtime --------------------------------------------------------

    @staticmethod
    def settings(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "app_env": "test",
            "public_base_url": "http://localhost:8000",
            "workos_authkit_domain": AUTHKIT_DOMAIN,
            "workos_api_key": "sk_test_fake",
            "workos_client_id": "client_app_test",
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    def install(
        self,
        settings: Settings,
        *,
        repository: IdentityRepository | None = None,
        hooks: AuthzHooks | None = None,
    ) -> AuthRuntime:
        """Register the auth runtime for ``settings`` with this mock's HTTP client."""
        return configure_auth(
            settings, repository=repository, hooks=hooks, http_client=self.http_client()
        )

    # --- tokens --------------------------------------------------------------------

    @staticmethod
    def token(
        settings: Settings | None = None,
        *,
        key: rsa.RSAPrivateKey | str | None = None,
        kid: str | None = PRIMARY_KID,
        algorithm: str = "RS256",
        drop: tuple[str, ...] = (),
        **overrides: Any,
    ) -> str:
        """Mint an AuthKit-style access token (valid for ``settings`` by default)."""
        now = int(time.time())
        audience = (
            settings.mcp_resource_url if settings is not None else "http://localhost:8000/mcp"
        )
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": audience,
            "sub": "user_alice",
            "client_id": "client_muse",
            "sid": "session_1",
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        for name in drop:
            claims.pop(name, None)
        headers = {"kid": kid} if kid else {}
        return jwt.encode(claims, key or rsa_key("primary"), algorithm=algorithm, headers=headers)
