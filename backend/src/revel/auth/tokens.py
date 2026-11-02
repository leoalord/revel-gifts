"""WorkOS AuthKit access-token verification for the MCP endpoint and HTTP API.

Tokens are RS256 JWTs signed by AuthKit. We verify signature (JWKS), issuer,
audience (the MCP resource indicator), expiry and required claims locally; the
token is never forwarded anywhere. Failures are logged by category only — never
the token or its claims.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier

from revel.auth.config import AuthConfig

logger = logging.getLogger(__name__)

ALGORITHM = "RS256"
DEFAULT_LEEWAY_SECONDS = 30


class TokenRejected(Exception):
    """A token failed verification. ``category`` is safe to log."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class JWKSCache:
    """Async JWKS fetcher with TTL and rate-limited refresh on unknown ``kid``."""

    def __init__(
        self,
        jwks_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        ttl_seconds: float = 3600.0,
        min_refresh_interval_seconds: float = 30.0,
        timeout_seconds: float = 5.0,
    ):
        self._url = jwks_url
        self._client = http_client
        self._ttl = ttl_seconds
        self._min_refresh = min_refresh_interval_seconds
        self._timeout = timeout_seconds
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._forced_refresh_at: float | None = None
        self._lock = asyncio.Lock()
        self.fetch_count = 0

    def _stale(self) -> bool:
        return self._fetched_at is None or time.monotonic() - self._fetched_at > self._ttl

    def _may_force_refresh(self) -> bool:
        """Unknown-kid refreshes are limited to one per interval (no refresh storms)."""
        return (
            self._forced_refresh_at is None
            or time.monotonic() - self._forced_refresh_at >= self._min_refresh
        )

    async def _fetch(self) -> None:
        self.fetch_count += 1
        if self._client is not None:
            response = await self._client.get(self._url, timeout=self._timeout)
        else:
            async with httpx.AsyncClient() as client:
                response = await client.get(self._url, timeout=self._timeout)
        response.raise_for_status()
        keys: dict[str, jwt.PyJWK] = {}
        for jwk in response.json().get("keys", []):
            if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not jwk.get("kid"):
                continue
            if jwk.get("use", "sig") != "sig":
                continue
            try:
                keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm=ALGORITHM)
            except jwt.PyJWTError:
                continue
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def get_key(self, kid: str) -> jwt.PyJWK:
        async with self._lock:
            stale = self._stale()
            forced = not stale and kid not in self._keys and self._may_force_refresh()
            if stale or forced:
                if forced:
                    self._forced_refresh_at = time.monotonic()
                try:
                    await self._fetch()
                except (httpx.HTTPError, ValueError) as exc:
                    # Keep any previously cached keys; fail only if the kid is unknown.
                    logger.warning("JWKS fetch failed: %s", type(exc).__name__)
                    if kid not in self._keys:
                        raise TokenRejected("jwks_unavailable") from None
            key = self._keys.get(kid)
        if key is None:
            raise TokenRejected("unknown_kid")
        return key


class WorkOSTokenVerifier(TokenVerifier):
    """``mcp`` ``TokenVerifier`` for WorkOS AuthKit access tokens."""

    def __init__(
        self,
        config: AuthConfig,
        *,
        jwks: JWKSCache | None = None,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
    ):
        self._config = config
        self._leeway = leeway_seconds
        self._jwks = jwks
        if self._jwks is None and config.jwks_url:
            self._jwks = JWKSCache(config.jwks_url)
        if not config.configured:
            logger.error(
                "WorkOS AuthKit domain is not configured; all bearer tokens will be rejected"
            )

    @property
    def config(self) -> AuthConfig:
        return self._config

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            return await self.verify(token)
        except TokenRejected as exc:
            logger.info("Bearer token rejected: %s", exc.category)
            return None
        except Exception as exc:  # fail closed on anything unexpected
            logger.warning("Bearer token rejected: unexpected %s", type(exc).__name__)
            return None

    async def verify(self, token: str) -> AccessToken:
        """Verify ``token`` or raise :class:`TokenRejected`."""
        config = self._config
        if not config.configured or self._jwks is None or config.issuer is None:
            raise TokenRejected("not_configured")
        if not isinstance(token, str) or not token or token.count(".") != 2:
            raise TokenRejected("malformed")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenRejected("malformed") from None
        if header.get("alg") != ALGORITHM:
            raise TokenRejected("unsupported_alg")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenRejected("missing_kid")
        key = await self._jwks.get_key(kid)

        audiences = [config.resource_url.rstrip("/"), config.resource_url.rstrip("/") + "/"]
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                audience=audiences,
                issuer=[config.issuer, config.issuer + "/"],
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenRejected("expired") from None
        except jwt.InvalidAudienceError:
            raise TokenRejected("invalid_audience") from None
        except jwt.InvalidIssuerError:
            raise TokenRejected("invalid_issuer") from None
        except jwt.InvalidSignatureError:
            raise TokenRejected("invalid_signature") from None
        except jwt.MissingRequiredClaimError:
            raise TokenRejected("missing_claim") from None
        except jwt.ImmatureSignatureError:
            raise TokenRejected("not_yet_valid") from None
        except jwt.PyJWTError:
            raise TokenRejected("invalid") from None

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise TokenRejected("missing_claim")
        client_id = claims.get("client_id") or claims.get("azp")
        if not isinstance(client_id, str) or not client_id:
            raise TokenRejected("missing_client_id")

        scope = claims.get("scope")
        if isinstance(scope, str):
            scopes = scope.split()
        elif isinstance(claims.get("scp"), list):
            scopes = [s for s in claims["scp"] if isinstance(s, str)]
        else:
            scopes = []

        # Keep only the claims callers need; ``iss`` lets the SDK bind sessions to
        # the (client, issuer, subject) principal.
        kept = {"iss": claims["iss"]}
        for name in ("sid", "org_id"):
            if isinstance(claims.get(name), str):
                kept[name] = claims[name]

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=config.resource_url,
            subject=subject,
            claims=kept,
        )
