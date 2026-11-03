"""Revoke a user's WorkOS consent for an OAuth client (design (b) of RG-10 reconnect).

When a user disconnects an assistant, Revel also removes the WorkOS "authorized Connect
application" for that user and client, so the next authorization shows WorkOS's consent
screen again and creates a new consent (expected to carry a new ``sid``). Revel's own
``sid``/``iat`` rules (``IdentityResolver``) already refuse the old authorization's tokens;
this is defence in depth and is best effort.

API (https://workos.com/docs/reference/authkit/user/authorized-application):

* ``GET /user_management/users/{user_id}/authorized_applications`` lists the user's
  authorized applications (cursor pagination: ``limit``, ``order``, ``after``); each item's
  ``application`` has ``id`` and ``client_id``.
* ``DELETE /user_management/users/{user_id}/authorized_applications/{application_id}`` where
  ``application_id`` is "the ID or client ID of the application"; we pass the matched
  ``application.id``.

The API key is sent only to the configured WorkOS API base URL. Nothing here logs the key,
user IDs, client IDs or application IDs.
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

PAGE_LIMIT = 100
MAX_PAGES = 20
_DELETED = (200, 202, 204)


class WorkOSRequestFailed(Exception):
    """A WorkOS call returned an unexpected status or body. The message is safe to log."""


class AuthorizedApps(Protocol):
    async def revoke(self, workos_user_id: str, client_id: str) -> bool:
        """Remove the user's authorization of ``client_id``; ``True`` if one was deleted."""
        ...


class NullAuthorizedApps:
    """No WorkOS API key (local development, tests): nothing to revoke."""

    async def revoke(self, workos_user_id: str, client_id: str) -> bool:
        return False


class WorkOSAuthorizedApps:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.workos.com",
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 5.0,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = http_client
        self._timeout = timeout_seconds

    async def _request(
        self, client: httpx.AsyncClient, method: str, url: str, params: dict | None = None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        return await client.request(
            method, url, params=params, headers=headers, timeout=self._timeout
        )

    async def _find_application_ids(
        self, client: httpx.AsyncClient, users_url: str, client_id: str
    ) -> list[str]:
        found: list[str] = []
        after: str | None = None
        for _ in range(MAX_PAGES):
            params: dict[str, str | int] = {"limit": PAGE_LIMIT, "order": "asc"}
            if after:
                params["after"] = after
            response = await self._request(client, "GET", users_url, params)
            if response.status_code != 200:
                raise WorkOSRequestFailed(f"list HTTP {response.status_code}")
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                raise WorkOSRequestFailed("list response is not an object with a data list")
            for item in body["data"]:
                app = item.get("application") if isinstance(item, dict) else None
                if not isinstance(app, dict) or app.get("client_id") != client_id:
                    continue
                app_id = app.get("id")
                if isinstance(app_id, str) and app_id and app_id not in found:
                    found.append(app_id)
            metadata = body.get("list_metadata")
            after = metadata.get("after") if isinstance(metadata, dict) else None
            if not body["data"] or not isinstance(after, str) or not after:
                return found
        raise WorkOSRequestFailed("too many pages")

    async def _revoke(self, client: httpx.AsyncClient, workos_user_id: str, client_id: str) -> bool:
        users_url = (
            f"{self._base_url}/user_management/users/"
            f"{quote(workos_user_id, safe='')}/authorized_applications"
        )
        deleted = False
        for app_id in await self._find_application_ids(client, users_url, client_id):
            response = await self._request(
                client, "DELETE", f"{users_url}/{quote(app_id, safe='')}"
            )
            if response.status_code in _DELETED:
                deleted = True
            elif response.status_code != 404:  # 404: already gone
                raise WorkOSRequestFailed(f"delete HTTP {response.status_code}")
        return deleted

    async def revoke(self, workos_user_id: str, client_id: str) -> bool:
        if not workos_user_id or not client_id:
            return False
        if self._client is not None:
            return await self._revoke(self._client, workos_user_id, client_id)
        async with httpx.AsyncClient() as client:
            return await self._revoke(client, workos_user_id, client_id)
