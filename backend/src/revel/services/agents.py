"""Connected assistants for the signed-in user (F8's "connected agents" UI, #16).

Lists the caller's own delegations and disconnects one through F4's
``revel.auth.agent_disconnect.disconnect_agent``. Every "no such delegation for you" case
(unknown, someone else's, already disconnected) raises the same :class:`AgentNotFound`;
any authorization-store failure raises :class:`AgentServiceUnavailable`, and the
delegation stays active (F2's primitive rolls back when the revoke hook fails).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel

from revel.auth.agent_disconnect import disconnect_agent
from revel.auth.authz import AuthorizationDenied
from revel.auth.identity import IdentityResolver, Principal
from revel.auth.repository import DelegationView, IdentityRepository

logger = logging.getLogger(__name__)

DEFAULT_AGENT_LABEL = "Assistant"


class ConnectedAgentInfo(BaseModel):
    name: str | None
    client_id: str
    label: str  # always non-empty; what the UI displays


class ConnectedAgent(BaseModel):
    delegation_id: UUID
    agent: ConnectedAgentInfo
    status: Literal["active", "disconnected"]
    connected_at: datetime
    disconnected_at: datetime | None


class AgentNotFound(LookupError):
    """No delegation the caller may act on. Never says whether it exists or whose it is."""


class AgentServiceUnavailable(RuntimeError):
    """Authorization could not be updated; nothing changed. Carries no internal detail."""


def agent_label(name: str | None, client_id: str) -> str:
    """Display label: the agent's non-blank name, else an ``https`` client ID's host
    (CIMD client IDs are URLs), else ``"Assistant"``."""
    if name is not None and name.strip():
        return name.strip()
    try:
        parts = urlsplit(client_id.strip())
        host = parts.hostname if parts.scheme.lower() == "https" else None
    except ValueError:
        host = None
    return host or DEFAULT_AGENT_LABEL


def _item(view: DelegationView) -> ConnectedAgent:
    return ConnectedAgent(
        delegation_id=view.id,
        agent=ConnectedAgentInfo(
            name=view.agent.name,
            client_id=view.agent.oauth_client_id,
            label=agent_label(view.agent.name, view.agent.oauth_client_id),
        ),
        status=view.status,
        connected_at=view.connected_at,
        disconnected_at=view.disconnected_at,
    )


async def list_connected_agents(
    principal: Principal, repo: IdentityRepository
) -> list[ConnectedAgent]:
    """The principal's own delegations, any status, newest first."""
    return [_item(view) for view in await repo.list_delegations_for_user(principal.user_id)]


async def disconnect_connected_agent(
    principal: Principal, delegation_id: UUID, resolver: IdentityResolver
) -> ConnectedAgent:
    """Disconnect one of the principal's active delegations and return its updated item."""
    try:
        await disconnect_agent(principal, delegation_id, resolver=resolver)
    except AuthorizationDenied:
        raise AgentNotFound() from None
    except Exception as exc:
        # OpenFGA unconfigured/unavailable, hook timeout or missing hook. The text may carry
        # object IDs; the class name is enough.
        logger.warning("agent disconnect failed: %s", type(exc).__name__)
        raise AgentServiceUnavailable() from None
    view = await resolver.repository.get_delegation_view(principal.user_id, delegation_id)
    if view is None:  # disconnected by us a moment ago; rows are never deleted
        raise AgentNotFound()
    return _item(view)
