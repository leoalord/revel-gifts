"""Connected-assistant routes for the signed-in browser user (contract for F8, #16).

* ``GET /api/agents`` — the caller's own delegations (active and disconnected), newest
  first: ``[{"delegation_id", "agent": {"name", "client_id", "label"}, "status",
  "connected_at", "disconnected_at"}]``. ``agent.label`` is always a non-empty display
  string. 401 when not signed in.
* ``POST /api/agents/{delegation_id}/disconnect`` — same-origin only (403 otherwise).
  200 with the updated item. An unknown, foreign, already disconnected or malformed ID is
  the same 404 ``{"detail": "Not found"}``; an authorization-store failure is 503
  ``{"detail": "Service unavailable"}`` and leaves the delegation active.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from revel.auth.runtime import AuthNotConfigured, AuthRuntime, get_auth_runtime
from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal
from revel.services.agents import (
    AgentNotFound,
    AgentServiceUnavailable,
    ConnectedAgent,
    disconnect_connected_agent,
    list_connected_agents,
)

router = APIRouter(prefix="/api/agents", tags=["agents"])

Runtime = Annotated[AuthRuntime, Depends(get_auth_runtime)]


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service unavailable"
    )


@router.get("", response_model=list[ConnectedAgent])
async def list_agents(
    principal: CurrentUIPrincipal, runtime: Runtime, response: Response
) -> list[ConnectedAgent]:
    response.headers["cache-control"] = "no-store"
    return await list_connected_agents(principal, runtime.repository)


@router.post("/{delegation_id}/disconnect", response_model=ConnectedAgent)
async def disconnect(
    delegation_id: str, principal: SameOriginUIPrincipal, runtime: Runtime, response: Response
) -> ConnectedAgent:
    # Parsed here, not by FastAPI, so a malformed ID is the same 404 rather than a 422.
    try:
        parsed = UUID(delegation_id)
    except ValueError:
        raise _not_found() from None
    try:
        resolver = runtime.require_resolver()
    except AuthNotConfigured:
        raise _unavailable() from None
    try:
        item = await disconnect_connected_agent(principal, parsed, resolver)
    except AgentNotFound:
        raise _not_found() from None
    except AgentServiceUnavailable:
        raise _unavailable() from None
    response.headers["cache-control"] = "no-store"
    return item
