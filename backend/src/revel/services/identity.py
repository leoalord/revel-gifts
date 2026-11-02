"""Shared ``whoami`` operation used by the MCP ``whoami`` tool and ``GET /me``."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from revel.auth.identity import Principal
from revel.auth.repository import IdentityRepository


class WhoAmIUser(BaseModel):
    id: UUID
    workos_user_id: str
    email: str | None
    display_name: str | None


class WhoAmIAgent(BaseModel):
    agent_id: UUID
    delegation_id: UUID
    client_id: str
    name: str | None


class WhoAmI(BaseModel):
    user: WhoAmIUser
    agent: WhoAmIAgent | None
    via: Literal["mcp", "api", "ui"]


class IdentityNotFound(LookupError):
    """The principal refers to a row that no longer exists."""


async def whoami(principal: Principal, repo: IdentityRepository) -> WhoAmI:
    user = await repo.get_user(principal.user_id)
    if user is None:
        raise IdentityNotFound("user not found")
    agent: WhoAmIAgent | None = None
    if principal.agent_id is not None and principal.delegation_id is not None:
        record = await repo.get_agent(principal.agent_id)
        if record is None:
            raise IdentityNotFound("agent not found")
        agent = WhoAmIAgent(
            agent_id=record.id,
            delegation_id=principal.delegation_id,
            client_id=record.oauth_client_id,
            name=record.name,
        )
    return WhoAmI(
        user=WhoAmIUser(
            id=user.id,
            workos_user_id=user.workos_user_id,
            email=user.email,
            display_name=user.display_name,
        ),
        agent=agent,
        via=principal.via,
    )
