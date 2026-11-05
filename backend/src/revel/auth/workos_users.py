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

    async def fetch_verified_email(self, workos_user_id: str) -> str | None:
        """Fresh verification evidence for email-bound capabilities; fail closed on any error.

        Cached Revel email and OAuth email claims are not sufficient for redemption.
        """
        url = f"{self._base_url}/user_management/users/{quote(workos_user_id, safe='')}"
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            if self._client is not None:
                response = await self._client.get(url, headers=headers, timeout=self._timeout)
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.get(url, headers=headers, timeout=self._timeout)
            if response.status_code != 200:
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("id") != workos_user_id:
            return None
        if data.get("email_verified") is not True:
            return None
        email = data.get("email")
        return email if isinstance(email, str) and email else None

    async def subjects_for_email(self, email: str) -> list[str] | None:
        """Private exact-email lookup for delivery block enforcement, never a directory API.

        https://workos.com/docs/reference/authkit/user/list documents the email filter.
        None indicates an outage; [] is a successful empty match. Fail closed if paginated.
        """
        url = f"{self._base_url}/user_management/users"
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        try:
            if self._client is not None:
                response = await self._client.get(
                    url,
                    headers=headers,
                    params={"email": email, "limit": 100},
                    timeout=self._timeout,
                )
            else:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        url,
                        headers=headers,
                        params={"email": email, "limit": 100},
                        timeout=self._timeout,
                    )
            if response.status_code != 200:
                return None
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            return None
        metadata = data.get("list_metadata", {})
        if not isinstance(metadata, dict) or metadata.get("after"):
            return None
        subjects = []
        for user in data["data"]:
            if (
                not isinstance(user, dict)
                or not isinstance(user.get("id"), str)
                or not isinstance(user.get("email"), str)
                or user["email"].strip().lower() != email
            ):
                return None
            subjects.append(user["id"])
        return subjects
