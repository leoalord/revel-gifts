"""Shared ``whoami`` operation used by the MCP ``whoami`` tool and ``GET /me``."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel

from revel.auth.identity import Principal
from revel.auth.repository import IdentityRepository

DEFAULT_AGENT_LABEL = "Assistant"


def agent_label(name: str | None, client_id: str) -> str:
    """Display label for an acting assistant (shared by ``/api/agents``, ``whoami`` and
    any other surface that names the assistant).

    The agent's non-blank name, else an ``https`` client ID's host (CIMD client IDs are
    URLs), else ``"Assistant"``. ``agents.name`` for DCR clients is filled from WorkOS's
    record of the client registration (``revel.auth.workos_clients``).
    """
    if name is not None and name.strip():
        return name.strip()
    try:
        parts = urlsplit(client_id.strip())
        host = parts.hostname if parts.scheme.lower() == "https" else None
    except ValueError:
        host = None
    return host or DEFAULT_AGENT_LABEL


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
    label: str  # always non-empty; see ``agent_label``


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
            label=agent_label(record.name, record.oauth_client_id),
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
