"""Normalized authentication configuration derived from application settings.

F1 owns ``revel.settings``. This module only reads the attribute names agreed with
F1 (``workos_authkit_domain``, ``workos_api_key``, ``public_base_url``,
``mcp_resource_url``) so it works with any settings object exposing them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.routes import build_resource_metadata_url

# Whole values that mean "not configured" (compared case-insensitively). Real domains or
# keys that merely contain these words are accepted.
_PLACEHOLDER_VALUES = frozenset({"placeholder", "changeme", "change-me", "todo", "tbd", "xxx"})
# ``.env.example`` convention, e.g. ``replace-with-authkit-domain``.
_PLACEHOLDER_PREFIX = "replace-with-"
_DEV_APP_ENVS = ("development", "test")


def is_placeholder(value: str) -> bool:
    """True for template values such as ``<your-domain>`` or ``replace-with-...``."""
    lowered = value.strip().lower()
    return (
        lowered in _PLACEHOLDER_VALUES
        or lowered.startswith(_PLACEHOLDER_PREFIX)
        or (lowered.startswith("<") and lowered.endswith(">"))
    )


def _plain(value: Any) -> str:
    """Return a stripped string, unwrapping pydantic ``SecretStr`` and URL types."""
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()
    return str(value).strip()


def normalize_authkit_issuer(domain: Any, *, allow_http_localhost: bool = False) -> str | None:
    """Turn ``WORKOS_AUTHKIT_DOMAIN`` into the issuer URL, or ``None`` if unusable.

    Accepts a bare host (``tenant.authkit.app``) or a URL and strips any trailing
    slash. Placeholder or empty values return ``None`` so callers fail closed. Only
    ``https`` is accepted, except ``http://localhost``/``127.0.0.1`` when
    ``allow_http_localhost`` (development/test only).
    """
    raw = _plain(domain)
    if not raw or is_placeholder(raw):
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return None
    if parsed.scheme == "http" and not (
        allow_http_localhost and parsed.hostname in ("localhost", "127.0.0.1")
    ):
        return None
    if parsed.query or parsed.fragment or parsed.params:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")


@dataclass(frozen=True)
class AuthConfig:
    """Auth settings used by the verifier, routes and identity resolver."""

    issuer: str | None
    """AuthKit issuer (``https://<authkit_domain>``) or ``None`` when unconfigured."""
    resource_url: str
    """The MCP resource indicator / expected token audience."""
    workos_api_key: str | None
    workos_api_base_url: str = "https://api.workos.com"

    @property
    def configured(self) -> bool:
        return self.issuer is not None

    @property
    def jwks_url(self) -> str | None:
        return f"{self.issuer}/oauth2/jwks" if self.issuer else None

    @property
    def authorization_server_metadata_url(self) -> str | None:
        return f"{self.issuer}/.well-known/oauth-authorization-server" if self.issuer else None

    @property
    def resource_metadata_url(self) -> str:
        """RFC 9728 metadata URL for the MCP resource.

        Built with the SDK helper so ``GET /me`` advertises exactly the URL the
        SDK puts in the MCP endpoint's ``WWW-Authenticate`` header.
        """
        return str(build_resource_metadata_url(self.resource_url))

    @classmethod
    def from_settings(cls, settings: Any) -> AuthConfig:
        resource = _plain(getattr(settings, "mcp_resource_url", None))
        if not resource:
            base = _plain(getattr(settings, "public_base_url", None)).rstrip("/")
            path = _plain(getattr(settings, "mcp_path", None)) or "/mcp"
            resource = f"{base}{path}"
        api_key = _plain(getattr(settings, "workos_api_key", None)) or None
        if api_key and is_placeholder(api_key):
            api_key = None
        # Settings without app_env are treated as production (strictest).
        dev = getattr(settings, "app_env", "production") in _DEV_APP_ENVS
        return cls(
            issuer=normalize_authkit_issuer(
                getattr(settings, "workos_authkit_domain", None), allow_http_localhost=dev
            ),
            resource_url=resource,
            workos_api_key=api_key,
        )
