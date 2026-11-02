"""Best-effort OAuth client name lookup via the WorkOS Connect Applications API.

AuthKit access tokens carry ``client_id`` but no client name. Clients that registered
through Dynamic Client Registration (DCR) get an opaque WorkOS client ID (``client_...``)
and a Connect application whose ``name`` is what the registration supplied. We read it
with ``GET /connect/applications/{id}`` (the path accepts the application ID or the
OAuth client ID) using the server's WorkOS API key, so the name comes from WorkOS's
record of the registration, not from anything the caller sends on a request.

CIMD clients (``https://`` client IDs) have no Connect application to look up; their
display label is the URL's host (``revel.services.identity.agent_label``).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Protocol
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

MAX_AGENT_NAME_LENGTH = 64
_WHITESPACE = re.compile(r"\s+")


def sanitize_agent_name(value: object) -> str | None:
    """A display-safe agent name, or ``None`` if nothing usable remains.

    Drops control/format characters (Unicode categories ``C*``, e.g. NUL, ANSI escapes,
    bidi overrides), collapses whitespace, strips and caps at
    :data:`MAX_AGENT_NAME_LENGTH` characters.
    """
    if not isinstance(value, str):
        return None
    kept = "".join(
        " " if ch.isspace() else ch
        for ch in value
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    name = _WHITESPACE.sub(" ", kept).strip()[:MAX_AGENT_NAME_LENGTH].rstrip()
    return name or None


def is_url_client_id(client_id: str) -> bool:
    """CIMD client IDs are ``https`` URLs; DCR/dashboard client IDs are opaque strings."""
    return client_id.strip().lower().startswith("https://")


class ClientDirectory(Protocol):
    async def fetch_client_name(self, client_id: str) -> str | None: ...


class NullClientDirectory:
    async def fetch_client_name(self, client_id: str) -> str | None:
        return None


class WorkOSClientDirectory:
    """``GET /connect/applications/{client_id}``; any failure returns ``None``.

    The API key is sent only to the configured WorkOS API base URL and is never logged.
    The response must echo the requested ``client_id``; otherwise it is ignored.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.workos.com",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 3.0,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = http_client
        self._timeout = timeout_seconds

    async def fetch_client_name(self, client_id: str) -> str | None:
        if not client_id or is_url_client_id(client_id):
            return None
        url = f"{self._base_url}/connect/applications/{quote(client_id, safe='')}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            if self._client is not None:
                response = await self._client.get(url, headers=headers, timeout=self._timeout)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=headers, timeout=self._timeout)
            if response.status_code != 200:
                logger.warning("WorkOS client lookup failed: HTTP %s", response.status_code)
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("WorkOS client lookup failed: %s", type(exc).__name__)
            return None
        if not isinstance(data, dict) or data.get("client_id") != client_id:
            return None
        return sanitize_agent_name(data.get("name"))
