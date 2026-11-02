"""Principal model and identity resolution from verified token claims.

Identity comes only from a verified access token (``sub`` and client id); no
client-supplied identity is ever consulted. Resolution upserts the user, the
agent (one per OAuth client) and the user-agent delegation, but never updates
an existing row: a disconnected delegation is rejected, never reactivated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from mcp.server.auth.provider import AccessToken

from revel.auth.hooks import AuthzHooks, ModuleAuthzHooks
from revel.auth.repository import DelegationRecord, IdentityRepository, UserRecord
from revel.auth.workos_users import NullUserDirectory, UserDirectory

logger = logging.getLogger(__name__)

Via = Literal["mcp", "api", "ui"]


@dataclass(frozen=True)
class Principal:
    """The authenticated actor for one request.

    ``agent_id`` and ``delegation_id`` are both set (an assistant acting for the
    user) or both ``None`` (the user acting directly).
    """

    user_id: UUID
    agent_id: UUID | None
    delegation_id: UUID | None
    via: Via

    def __post_init__(self) -> None:
        if self.via not in ("mcp", "api", "ui"):
            raise ValueError("invalid via")
        if (self.agent_id is None) != (self.delegation_id is None):
            raise ValueError("agent_id and delegation_id must both be set or both be None")
        has_agent = self.agent_id is not None
        # Bearer-token transports always act through an assistant delegation; the
        # browser UI is the user acting directly.
        if self.via in ("mcp", "api") and not has_agent:
            raise ValueError(f"via={self.via!r} requires agent_id and delegation_id")
        if self.via == "ui" and has_agent:
            raise ValueError("via='ui' must not carry agent_id or delegation_id")


class IdentityError(Exception):
    """Base class for identity-resolution failures."""


class DelegationDisconnected(IdentityError):
    """The user disconnected this assistant; its tokens no longer act for them."""


class InvalidIdentityClaims(IdentityError):
    """The verified token lacks the claims needed to identify a user and client."""


class IdentityResolver:
    def __init__(
        self,
        repository: IdentityRepository,
        *,
        user_directory: UserDirectory | None = None,
        hooks: AuthzHooks | None = None,
    ):
        self.repository = repository
        self._directory = user_directory or NullUserDirectory()
        self._hooks = hooks or ModuleAuthzHooks()

    async def resolve(self, access_token: AccessToken, via: Via) -> Principal:
        workos_user_id = access_token.subject
        client_id = access_token.client_id
        if not workos_user_id or not client_id:
            raise InvalidIdentityClaims("token has no subject or client id")

        user = await self.repository.get_user_by_workos_id(workos_user_id)
        if user is None:
            profile = await self._directory.fetch_profile(workos_user_id)

            async def user_created(record: UserRecord) -> None:
                await self._hooks.on_user_created(user_id=record.id)

            user, _ = await self.repository.ensure_user(
                workos_user_id,
                email=profile.email if profile else None,
                display_name=profile.display_name if profile else None,
                on_created=user_created,
            )

        agent, _ = await self.repository.ensure_agent(client_id)

        async def delegation_created(record: DelegationRecord) -> None:
            await self._hooks.on_agent_delegated(
                user_id=record.user_id, agent_id=record.agent_id, delegation_id=record.id
            )

        delegation, _ = await self.repository.ensure_delegation(
            user.id, agent.id, on_created=delegation_created
        )
        if delegation.status != "active":
            raise DelegationDisconnected("assistant delegation is disconnected")

        return Principal(user_id=user.id, agent_id=agent.id, delegation_id=delegation.id, via=via)
