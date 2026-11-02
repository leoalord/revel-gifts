"""Browser sessions: WorkOS AuthKit hosted sign-in and sealed session cookies (F2).

The human UI (F8) signs users in through WorkOS User Management with AuthKit as the
hosted UI. The resulting WorkOS access/refresh tokens are kept only in an encrypted,
authenticated cookie. Each request verifies the access token's signature, issuer and
expiry against WorkOS's client JWKS, refreshes it when expired, and resolves the
``sub`` (the WorkOS user id) to the same ``users`` row the MCP OAuth path uses.

WorkOS endpoints used (https://workos.com/docs/reference/authkit):

* ``GET  /user_management/authorize`` — hosted sign-in (``provider=authkit``).
* ``POST /user_management/authenticate`` — ``authorization_code`` and ``refresh_token``
  grants with ``client_id`` + ``client_secret`` (the WorkOS API key).
* ``GET  /sso/jwks/<client_id>`` — keys that sign User Management access tokens.
* ``GET  /user_management/sessions/logout?session_id=...`` — ends the WorkOS session.

Tokens and cookie contents are never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit

import anyio
import httpx
import jwt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from revel.auth.config import _DEV_APP_ENVS, _plain, is_placeholder, normalize_authkit_issuer
from revel.auth.tokens import ALGORITHM, DEFAULT_LEEWAY_SECONDS, JWKSCache, TokenRejected
from revel.auth.workos_users import UserProfile

logger = logging.getLogger(__name__)

MIN_COOKIE_PASSWORD_LENGTH = 32
SESSION_COOKIE = "revel_session"
STATE_COOKIE = "revel_auth_state"
# ``__Host-`` cookies must be Secure, Path=/ and host-only; used whenever cookies are Secure.
SECURE_PREFIX = "__Host-"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600
STATE_MAX_AGE_SECONDS = 600
# A request still carrying a just-rotated refresh token reuses that refresh's result.
REFRESH_REUSE_SECONDS = 60
# Browsers cap a cookie (name + value) at about 4096 bytes.
MAX_COOKIE_BYTES = 4000
CALLBACK_PATH = "/auth/callback"
_SEAL_VERSION = "v1"
_SEAL_SALT = b"revel-browser-session/v1"


class BrowserAuthNotConfigured(RuntimeError):
    """Browser sign-in settings are missing or unsafe; fail closed."""


class SessionInvalid(Exception):
    """The browser session is missing, unreadable or no longer valid (respond 401)."""


class SessionRefreshRejected(SessionInvalid):
    """WorkOS rejected the cookie's refresh token (respond 401 but keep the cookie).

    Another request (possibly on another instance) may have just rotated the token and
    set a fresher cookie; deleting the cookie here could race with and wipe that one.
    """


class WorkOSUnavailable(Exception):
    """WorkOS could not be reached or answered with a server error (respond 503/502)."""


class WorkOSRejected(Exception):
    """WorkOS rejected a code or refresh token (respond 401 / restart sign-in)."""


# --- configuration ---------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserAuthConfig:
    client_id: str | None
    api_key: str | None
    redirect_uri: str | None
    cookie_password: str | None
    public_origin: str
    extra_origins: tuple[str, ...]
    development: bool
    secure_cookies: bool
    authkit_issuer: str | None
    api_base_url: str = "https://api.workos.com"

    @property
    def configured(self) -> bool:
        return all((self.client_id, self.api_key, self.redirect_uri, self.cookie_password))

    @property
    def jwks_url(self) -> str | None:
        return f"{self.api_base_url}/sso/jwks/{self.client_id}" if self.client_id else None

    @property
    def accepted_issuers(self) -> list[str]:
        """Issuers WorkOS uses for User Management access tokens.

        The docs show ``https://api.workos.com``; tokens are also seen with
        ``https://api.workos.com/user_management/<client_id>`` and, with a custom
        authentication domain, the AuthKit domain. All are signed by the client JWKS.
        """
        issuers = [self.api_base_url]
        if self.client_id:
            issuers.append(f"{self.api_base_url}/user_management/{self.client_id}")
        if self.authkit_issuer:
            issuers.append(self.authkit_issuer)
        return issuers + [i + "/" for i in issuers]

    @property
    def session_cookie_name(self) -> str:
        return (SECURE_PREFIX if self.secure_cookies else "") + SESSION_COOKIE

    @property
    def state_cookie_name(self) -> str:
        return (SECURE_PREFIX if self.secure_cookies else "") + STATE_COOKIE

    @classmethod
    def from_settings(cls, settings: Any) -> BrowserAuthConfig:
        def setting(name: str) -> str | None:
            value = _plain(getattr(settings, name, None))
            return None if not value or is_placeholder(value) else value

        development = getattr(settings, "app_env", "production") in _DEV_APP_ENVS
        base = _plain(getattr(settings, "public_base_url", None)).rstrip("/")
        parts = urlsplit(base)
        public_origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""
        password = setting("workos_cookie_password")
        if password is not None and len(password) < MIN_COOKIE_PASSWORD_LENGTH:
            logger.error(
                "WORKOS_COOKIE_PASSWORD is shorter than %s characters; browser sign-in disabled",
                MIN_COOKIE_PASSWORD_LENGTH,
            )
            password = None
        extras = tuple(
            o.strip().rstrip("/")
            for o in _plain(getattr(settings, "allowed_origins", "")).split(",")
            if o.strip()
        )
        return cls(
            client_id=setting("workos_client_id"),
            api_key=setting("workos_api_key"),
            # Same default as infra: PUBLIC_BASE_URL + /auth/callback.
            redirect_uri=setting("workos_redirect_uri") or (base + CALLBACK_PATH if base else None),
            cookie_password=password,
            public_origin=public_origin,
            extra_origins=extras,
            development=development,
            # Secure everywhere except development/test served over plain http.
            secure_cookies=not (development and parts.scheme == "http"),
            authkit_issuer=normalize_authkit_issuer(
                getattr(settings, "workos_authkit_domain", None), allow_http_localhost=development
            ),
        )


# --- sealing ---------------------------------------------------------------------------


class CookieSealer:
    """AES-256-GCM sealing with a purpose-bound key derived (HKDF-SHA256) from the password.

    Each purpose (``session``, ``state``) gets its own key and the purpose is also the
    associated data, so one cookie can never be replayed as the other.
    """

    def __init__(self, password: str):
        if len(password) < MIN_COOKIE_PASSWORD_LENGTH:
            raise BrowserAuthNotConfigured("cookie password too short")
        self._password = password.encode()
        self._keys: dict[str, AESGCM] = {}

    def _aead(self, purpose: str) -> AESGCM:
        aead = self._keys.get(purpose)
        if aead is None:
            key = HKDF(
                algorithm=hashes.SHA256(), length=32, salt=_SEAL_SALT, info=purpose.encode()
            ).derive(self._password)
            aead = self._keys[purpose] = AESGCM(key)
        return aead

    def seal(self, purpose: str, data: dict[str, Any]) -> str:
        nonce = os.urandom(12)
        plaintext = json.dumps(data, separators=(",", ":")).encode()
        sealed = self._aead(purpose).encrypt(nonce, plaintext, purpose.encode())
        body = base64.urlsafe_b64encode(nonce + sealed).decode().rstrip("=")
        return f"{_SEAL_VERSION}.{body}"

    def unseal(self, purpose: str, value: str | None) -> dict[str, Any]:
        """Return the sealed object or raise :class:`SessionInvalid` (tampered/garbage)."""
        if not value or not value.startswith(_SEAL_VERSION + "."):
            raise SessionInvalid("unreadable cookie")
        body = value[len(_SEAL_VERSION) + 1 :]
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            if len(raw) < 12 + 16:
                raise ValueError("short")
            plaintext = self._aead(purpose).decrypt(raw[:12], raw[12:], purpose.encode())
            data = json.loads(plaintext)
        except (ValueError, InvalidTag, UnicodeDecodeError):
            raise SessionInvalid("unreadable cookie") from None
        if not isinstance(data, dict):
            raise SessionInvalid("unreadable cookie")
        return data


# --- return_to -------------------------------------------------------------------------


def safe_return_to(value: str | None) -> str:
    """Only same-origin absolute paths (``/...``) are allowed; anything else becomes ``/``.

    Rejects scheme-relative (``//host``), backslash tricks (``/\\host``), absolute URLs,
    control characters and paths into the auth endpoints themselves.
    """
    if not value or len(value) > 2048:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    if parts.path.startswith("/auth/"):
        return "/"
    return value


# --- verified session ------------------------------------------------------------------


@dataclass(frozen=True)
class SessionTokens:
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class VerifiedSession:
    workos_user_id: str
    session_id: str | None
    tokens: SessionTokens
    refreshed: bool = False


@dataclass(frozen=True)
class AuthenticationResult:
    tokens: SessionTokens
    workos_user_id: str
    session_id: str | None
    profile: UserProfile | None


def _profile_from_user(user: Any) -> UserProfile | None:
    if not isinstance(user, dict):
        return None
    email = user.get("email") if isinstance(user.get("email"), str) else None
    names = [user.get("first_name"), user.get("last_name")]
    display = " ".join(n.strip() for n in names if isinstance(n, str) and n.strip()) or None
    return UserProfile(email=email or None, display_name=display)


class BrowserSessionManager:
    """Builds sign-in URLs, talks to WorkOS, seals cookies and verifies sessions."""

    def __init__(
        self,
        config: BrowserAuthConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        jwks: JWKSCache | None = None,
        leeway_seconds: int = DEFAULT_LEEWAY_SECONDS,
        timeout_seconds: float = 10.0,
    ):
        self.config = config
        self._client = http_client
        self._leeway = leeway_seconds
        self._timeout = timeout_seconds
        self._sealer = CookieSealer(config.cookie_password) if config.cookie_password else None
        self._jwks = jwks
        if self._jwks is None and config.jwks_url:
            self._jwks = JWKSCache(config.jwks_url, http_client=http_client)
        # Single-flight refresh per refresh token within this process: concurrent requests
        # carrying the same (possibly single-use) refresh token share one WorkOS call, while
        # refreshes of different sessions run independently.
        self._inflight: dict[str, _RefreshFlight] = {}
        self._recent_refreshes: dict[str, tuple[float, SessionTokens]] = {}
        if not config.configured:
            logger.warning("Browser sign-in is not configured; /auth routes will answer 503")

    # --- configuration guards ----------------------------------------------------------

    def require_configured(self) -> CookieSealer:
        if not self.config.configured or self._sealer is None:
            raise BrowserAuthNotConfigured("browser sign-in is not configured")
        return self._sealer

    # --- sign-in -----------------------------------------------------------------------

    def start_login(self, return_to: str | None) -> tuple[str, str]:
        """Return ``(authorize_url, sealed_state_cookie)``."""
        sealer = self.require_configured()
        state = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        sealed = sealer.seal(
            "state",
            {
                "state": state,
                "return_to": safe_return_to(return_to),
                "exp": int(time.time()) + STATE_MAX_AGE_SECONDS,
            },
        )
        query = urlencode(
            {
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "response_type": "code",
                "provider": "authkit",
                "state": state,
            }
        )
        return f"{self.config.api_base_url}/user_management/authorize?{query}", sealed

    def check_state(self, sealed_state: str | None, state: str | None) -> str:
        """Validate the callback ``state`` against the state cookie; return ``return_to``."""
        sealer = self.require_configured()
        data = sealer.unseal("state", sealed_state)
        expected = data.get("state")
        exp = data.get("exp")
        if not isinstance(expected, str) or not isinstance(exp, int) or exp < time.time():
            raise SessionInvalid("state expired")
        if not isinstance(state, str) or not _constant_eq(state, expected):
            raise SessionInvalid("state mismatch")
        return safe_return_to(data.get("return_to"))

    async def _authenticate(self, body: dict[str, str]) -> dict[str, Any]:
        url = f"{self.config.api_base_url}/user_management/authenticate"
        payload = {
            "client_id": self.config.client_id or "",
            "client_secret": self.config.api_key or "",
            **body,
        }
        try:
            if self._client is not None:
                response = await self._client.post(url, json=payload, timeout=self._timeout)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.post(url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.warning("WorkOS authenticate failed: %s", type(exc).__name__)
            raise WorkOSUnavailable("workos unreachable") from None
        if response.status_code >= 500 or response.status_code == 429:
            logger.warning("WorkOS authenticate failed: HTTP %s", response.status_code)
            raise WorkOSUnavailable("workos error")
        if response.status_code != 200:
            # Body may echo request details; log the status only.
            logger.info("WorkOS authenticate rejected: HTTP %s", response.status_code)
            raise WorkOSRejected("grant rejected")
        try:
            data = response.json()
        except ValueError:
            raise WorkOSUnavailable("invalid workos response") from None
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("access_token"), str)
            or not isinstance(data.get("refresh_token"), str)
        ):
            raise WorkOSUnavailable("invalid workos response")
        return data

    async def exchange_code(self, code: str) -> AuthenticationResult:
        """``authorization_code`` grant, then verify the issued access token."""
        self.require_configured()
        if not code:
            raise WorkOSRejected("missing code")
        data = await self._authenticate({"grant_type": "authorization_code", "code": code})
        tokens = SessionTokens(data["access_token"], data["refresh_token"])
        try:
            claims = await self.verify_access_token(tokens.access_token)
        except TokenRejected as exc:
            logger.warning("WorkOS session token rejected after code exchange: %s", exc.category)
            raise WorkOSRejected("issued token invalid") from None
        user = data.get("user")
        if isinstance(user, dict) and user.get("id") not in (None, claims["sub"]):
            raise WorkOSRejected("user mismatch")
        return AuthenticationResult(
            tokens=tokens,
            workos_user_id=claims["sub"],
            session_id=claims.get("sid") if isinstance(claims.get("sid"), str) else None,
            profile=_profile_from_user(user),
        )

    # --- access tokens -----------------------------------------------------------------

    async def verify_access_token(
        self, token: str, *, allow_expired: bool = False
    ) -> dict[str, Any]:
        """Verify signature (client JWKS), issuer, ``exp`` and ``sub``; raise TokenRejected."""
        if self._jwks is None:
            raise TokenRejected("not_configured")
        if not isinstance(token, str) or token.count(".") != 2:
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
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key.key,
                algorithms=[ALGORITHM],
                issuer=self.config.accepted_issuers,
                leeway=self._leeway,
                # WorkOS does not document an ``aud`` for User Management session tokens
                # (its own SDK skips the audience check); the cookie is sealed by us.
                options={
                    "require": ["exp", "iss", "sub"],
                    "verify_aud": False,
                    "verify_exp": not allow_expired,
                },
            )
        except jwt.ExpiredSignatureError:
            raise TokenRejected("expired") from None
        except jwt.InvalidIssuerError:
            raise TokenRejected("invalid_issuer") from None
        except jwt.InvalidSignatureError:
            raise TokenRejected("invalid_signature") from None
        except jwt.MissingRequiredClaimError:
            raise TokenRejected("missing_claim") from None
        except jwt.PyJWTError:
            raise TokenRejected("invalid") from None
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise TokenRejected("missing_claim")
        return claims

    # --- cookies -----------------------------------------------------------------------

    def seal_session(self, tokens: SessionTokens, session_id: str | None) -> str:
        sealer = self.require_configured()
        value = sealer.seal(
            "session", {"at": tokens.access_token, "rt": tokens.refresh_token, "sid": session_id}
        )
        if len(value) + len(self.config.session_cookie_name) + 1 > MAX_COOKIE_BYTES:
            logger.error("Sealed browser session exceeds the cookie size limit")
            raise WorkOSUnavailable("session too large for a cookie")
        return value

    def read_session(self, sealed: str | None) -> tuple[SessionTokens, str | None]:
        data = self.require_configured().unseal("session", sealed)
        at, rt, sid = data.get("at"), data.get("rt"), data.get("sid")
        if not isinstance(at, str) or not isinstance(rt, str) or not at or not rt:
            raise SessionInvalid("incomplete session")
        return SessionTokens(at, rt), sid if isinstance(sid, str) else None

    # --- per-request verification --------------------------------------------------------

    async def authenticate_cookie(self, sealed: str | None) -> VerifiedSession:
        """Verify the session cookie, refreshing an expired access token when possible.

        Raises :class:`SessionInvalid` (401, clear cookie), its subclass
        :class:`SessionRefreshRejected` (401, keep cookie) or :class:`WorkOSUnavailable`
        (503, keep cookie). ``refreshed`` tells the caller to re-set the cookie.
        """
        if not sealed:
            raise SessionInvalid("no session")
        tokens, sid = self.read_session(sealed)
        try:
            claims = await self.verify_access_token(tokens.access_token)
            return VerifiedSession(claims["sub"], _sid(claims, sid), tokens)
        except TokenRejected as exc:
            if exc.category == "jwks_unavailable":
                raise WorkOSUnavailable("jwks unavailable") from None
            if exc.category != "expired":
                logger.info("Browser session token rejected: %s", exc.category)
                raise SessionInvalid("invalid access token") from None
        # Signature/issuer of the expired token are still checked before refreshing.
        try:
            old_claims = await self.verify_access_token(tokens.access_token, allow_expired=True)
        except TokenRejected as exc:
            logger.info("Browser session token rejected: %s", exc.category)
            raise SessionInvalid("invalid access token") from None
        new_tokens = await self._refresh(tokens.refresh_token)
        try:
            claims = await self.verify_access_token(new_tokens.access_token)
        except TokenRejected as exc:
            logger.warning("Refreshed WorkOS token rejected: %s", exc.category)
            raise SessionInvalid("refreshed token invalid") from None
        if claims["sub"] != old_claims["sub"]:
            logger.warning("Refreshed WorkOS token belongs to a different user")
            raise SessionInvalid("subject changed")
        return VerifiedSession(claims["sub"], _sid(claims, sid), new_tokens, refreshed=True)

    async def _refresh(self, refresh_token: str) -> SessionTokens:
        key = hashlib.sha256(refresh_token.encode()).hexdigest()
        now = time.monotonic()
        self._recent_refreshes = {
            k: v for k, v in self._recent_refreshes.items() if now - v[0] < REFRESH_REUSE_SECONDS
        }
        cached = self._recent_refreshes.get(key)
        if cached is not None:
            return cached[1]
        flight = self._inflight.get(key)
        if flight is not None:
            await flight.done.wait()
            if flight.tokens is None:
                # Fresh exception per waiter; an interrupted leader counts as unavailable.
                error = flight.error or WorkOSUnavailable("refresh interrupted")
                raise type(error)(str(error))
            return flight.tokens
        flight = self._inflight[key] = _RefreshFlight()
        try:
            data = await self._authenticate(
                {"grant_type": "refresh_token", "refresh_token": refresh_token}
            )
            flight.tokens = SessionTokens(data["access_token"], data["refresh_token"])
            self._recent_refreshes[key] = (time.monotonic(), flight.tokens)
            return flight.tokens
        except WorkOSRejected:
            flight.error = SessionRefreshRejected("refresh rejected")
            raise flight.error from None
        except WorkOSUnavailable as exc:
            flight.error = WorkOSUnavailable(str(exc))
            raise
        finally:
            flight.done.set()
            self._inflight.pop(key, None)

    # --- logout ------------------------------------------------------------------------

    def logout_url(self, session_id: str | None, return_to: str | None = None) -> str | None:
        """WorkOS URL that ends the AuthKit session (navigate the browser to it)."""
        if not session_id:
            return None
        params = {"session_id": session_id}
        if return_to:
            params["return_to"] = return_to
        return f"{self.config.api_base_url}/user_management/sessions/logout?{urlencode(params)}"

    def session_id_from_cookie(self, sealed: str | None) -> str | None:
        """Best-effort session id for logout (no network, no verification needed)."""
        try:
            tokens, sid = self.read_session(sealed)
        except (SessionInvalid, BrowserAuthNotConfigured):
            return None
        if sid:
            return sid
        try:
            claims = jwt.decode(tokens.access_token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None
        value = claims.get("sid")
        return value if isinstance(value, str) else None


@dataclass
class _RefreshFlight:
    done: anyio.Event = field(default_factory=anyio.Event)
    tokens: SessionTokens | None = None
    error: Exception | None = None


def _sid(claims: dict[str, Any], fallback: str | None) -> str | None:
    value = claims.get("sid")
    return value if isinstance(value, str) and value else fallback


def _constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
