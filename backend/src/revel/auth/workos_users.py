"""Best-effort profile lookup via the WorkOS User Management API."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserProfile:
    email: str | None
    display_name: str | None


class UserDirectory(Protocol):
    async def fetch_profile(self, workos_user_id: str) -> UserProfile | None: ...


class NullUserDirectory:
    async def fetch_profile(self, workos_user_id: str) -> UserProfile | None:
        return None


class WorkOSUserDirectory:
    """``GET /user_management/users/{id}``; any failure returns ``None``.

    The API key is sent only to the configured WorkOS API base URL and is never logged.
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

    async def fetch_profile(self, workos_user_id: str) -> UserProfile | None:
        url = f"{self._base_url}/user_management/users/{quote(workos_user_id, safe='')}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            if self._client is not None:
                response = await self._client.get(url, headers=headers, timeout=self._timeout)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=headers, timeout=self._timeout)
            if response.status_code != 200:
                logger.warning("WorkOS user lookup failed: HTTP %s", response.status_code)
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("WorkOS user lookup failed: %s", type(exc).__name__)
            return None
        if not isinstance(data, dict):
            return None
        email = data.get("email") if isinstance(data.get("email"), str) else None
        names = [data.get("first_name"), data.get("last_name")]
        display_name = (
            " ".join(n.strip() for n in names if isinstance(n, str) and n.strip()) or None
        )
        return UserProfile(email=email or None, display_name=display_name)
