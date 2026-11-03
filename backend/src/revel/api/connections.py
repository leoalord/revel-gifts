"""Connections HTTP API for the human UI (F6, consumed by F8 #16).

Thin adapter over ``revel.services.connections``. Authentication is the browser session (F2,
``revel.auth.ui``): reads use ``CurrentUIPrincipal``; every state-changing route uses
``SameOriginUIPrincipal`` (CSRF ``Origin`` check). UI actions are the user acting directly, so
no delegation is recorded on the request rows, and approve / tier upgrade never need
``owner_confirmed`` here: the browser owner is the confirmation (the service still records
``owner_confirmed_at``). There are no agent-API (bearer) connection routes; agents use the MCP
tools, where approving or upgrading to ``trusted_circle`` needs ``owner_confirmed``.

Contract (all JSON; ids are UUID strings; timestamps ISO 8601). Prefix ``/api/connections``:

- ``GET  /handle`` -> ``HandleInfo`` or ``null``
- ``PUT  /handle`` body ``{handle?, discoverable?, avatar_url?}`` -> ``HandleInfo``
  (``handle`` required the first time; omitted fields are kept; ``avatar_url: null`` clears)
- ``POST /requests`` body ``{handle}`` -> ``RequestResult`` (exact handle; creates or returns
  the live request; 409 ``invalid_state`` with detail "Set your handle before sending a
  connection request." when the caller has no handle yet)
- ``GET  /requests/incoming`` -> ``[IncomingRequest]`` (pending requests to me)
- ``GET  /requests/outgoing`` -> ``[OutgoingRequest]`` (my pending/approved requests)
- ``POST /requests/{id}/approve`` body ``{tier}`` -> ``ResponseResult``
- ``POST /requests/{id}/decline`` -> ``ResponseResult``
- ``POST /requests/{id}/block`` -> ``ResponseResult`` (any request addressed to me: pending,
  approved or past; ``request_id`` in the result may be a new row)
- ``POST /requests/{id}/unblock`` -> ``ResponseResult`` (restores no tier)
- ``POST /requests/{id}/cancel`` -> ``ResponseResult`` (my own pending request)
- ``POST /requests/{id}/tier`` body ``{tier}`` -> ``ResponseResult`` (approved only)
- ``POST /requests/{id}/revoke`` -> ``ResponseResult`` (approved only)
- ``GET  ""`` (``/api/connections``) -> ``[ConnectionEntry]`` (people I approved, with tier)
- ``GET  /blocked`` -> ``[BlockedEntry]``

Shapes: ``HandleInfo {handle, discoverable, avatar_url}``; ``PublicIdentity {display_name,
handle, avatar_url}``; ``Person = PublicIdentity + {assistant: {name, client_id} | null}``;
``RequestResult {target: PublicIdentity, request: {id, status, created_at}, created}``;
``IncomingRequest {id, status, created_at, requester: Person}``; ``OutgoingRequest {id, status,
created_at, target: PublicIdentity}``; ``ConnectionEntry {request_id, tier, since, person:
Person}``; ``BlockedEntry {request_id, blocked_at, person: PublicIdentity}``; ``ResponseResult
{request_id, status, tier}``. ``tier`` is ``connections`` or ``trusted_circle``.

Errors: ``{"detail": <fixed message>, "code": <code>}`` with
404 ``target_not_found`` (nonexistent, hidden, blocking or own handle: identical),
404 ``request_not_found`` (unknown or not yours: identical),
409 ``invalid_state`` (detail "Set your handle before sending a connection request." when the
requester has no handle), 409 ``handle_unavailable``, 409 ``profile_not_set_up``,
422 ``invalid_handle`` / ``invalid_avatar_url`` / ``invalid_tier`` (and FastAPI's own 422 for a
malformed body), 429 ``rate_limited``, 403 ``not_allowed``, 503 ``unavailable`` (retryable;
nothing changed), plus F2's 401 ``Not signed in`` / 403 cross-origin from the dependencies.
403 ``owner_confirmation_required`` ``{detail, code, changes}`` is mapped for completeness (as
F5's agent API) but is unreachable from these browser routes.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal
from revel.services import connections as svc

logger = logging.getLogger(__name__)

PREFIX = "/api/connections"

ERRORS: dict[type[svc.ConnectionsError], tuple[int, str]] = {
    svc.TargetNotFound: (404, "target_not_found"),
    svc.RequestNotFound: (404, "request_not_found"),
    svc.InvalidState: (409, "invalid_state"),
    svc.HandleUnavailable: (409, "handle_unavailable"),
    svc.ProfileNotSetUp: (409, "profile_not_set_up"),
    svc.InvalidHandle: (422, "invalid_handle"),
    svc.InvalidAvatarUrl: (422, "invalid_avatar_url"),
    svc.InvalidTier: (422, "invalid_tier"),
    svc.RateLimited: (429, "rate_limited"),
    svc.NotAllowed: (403, "not_allowed"),
    svc.ConnectionsUnavailable: (503, "unavailable"),
}


def error_response(exc: Exception) -> JSONResponse:
    """Fixed ``{"detail", "code"}`` body for a service error; anything else is a generic 503.

    Returned (not raised) so no exception text can reach the client. A session cookie refreshed
    by F2's dependency is still delivered (F2's ``RefreshedSessionCookieMiddleware``).
    """
    if isinstance(exc, svc.OwnerConfirmationRequired):
        return JSONResponse(
            status_code=403,
            content={
                "detail": exc.reason_code,
                "code": exc.reason_code,
                "changes": exc.changes,
            },
        )
    for cls, (status, code) in ERRORS.items():
        if isinstance(exc, cls):
            return JSONResponse(status_code=status, content={"detail": exc.message, "code": code})
    logger.error("connections route failed (%s)", type(exc).__name__)
    return JSONResponse(
        status_code=503,
        content={"detail": svc.ConnectionsUnavailable.message, "code": "unavailable"},
    )


class SetHandleBody(BaseModel):
    handle: str | None = None
    discoverable: bool | None = None
    avatar_url: str | None = None


class RequestBody(BaseModel):
    handle: str


class TierBody(BaseModel):
    tier: Literal["connections", "trusted_circle"]


router = APIRouter(prefix=PREFIX, tags=["connections"])


async def _call(fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    try:
        return await fn(*args, **kwargs)
    except Exception as exc:
        return error_response(exc)


@router.get("/handle")
async def get_handle(principal: CurrentUIPrincipal) -> svc.HandleInfo | None:
    return await _call(svc.get_my_handle, principal)


@router.put("/handle")
async def put_handle(principal: SameOriginUIPrincipal, body: SetHandleBody) -> svc.HandleInfo:
    kwargs = body.model_dump(exclude_unset=True)
    return await _call(svc.set_handle, principal, **kwargs)


@router.post("/requests")
async def create_request(principal: SameOriginUIPrincipal, body: RequestBody) -> svc.RequestResult:
    return await _call(svc.lookup_and_request, principal, handle=body.handle)


@router.get("/requests/incoming")
async def incoming(principal: CurrentUIPrincipal) -> list[svc.IncomingRequest]:
    return await _call(svc.list_incoming, principal)


@router.get("/requests/outgoing")
async def outgoing(principal: CurrentUIPrincipal) -> list[svc.OutgoingRequest]:
    return await _call(svc.list_outgoing, principal)


@router.post("/requests/{request_id}/approve")
async def approve(
    principal: SameOriginUIPrincipal, request_id: UUID, body: TierBody
) -> svc.ResponseResult:
    return await _call(svc.approve, principal, request_id, tier=body.tier)


@router.post("/requests/{request_id}/decline")
async def decline(principal: SameOriginUIPrincipal, request_id: UUID) -> svc.ResponseResult:
    return await _call(svc.decline, principal, request_id)


@router.post("/requests/{request_id}/block")
async def block(principal: SameOriginUIPrincipal, request_id: UUID) -> svc.ResponseResult:
    return await _call(svc.block, principal, request_id)


@router.post("/requests/{request_id}/unblock")
async def unblock(principal: SameOriginUIPrincipal, request_id: UUID) -> svc.ResponseResult:
    return await _call(svc.unblock, principal, request_id)


@router.post("/requests/{request_id}/cancel")
async def cancel(principal: SameOriginUIPrincipal, request_id: UUID) -> svc.ResponseResult:
    return await _call(svc.cancel_request, principal, request_id)


@router.post("/requests/{request_id}/tier")
async def change_tier(
    principal: SameOriginUIPrincipal, request_id: UUID, body: TierBody
) -> svc.ResponseResult:
    return await _call(svc.change_tier, principal, request_id, tier=body.tier)


@router.post("/requests/{request_id}/revoke")
async def revoke(principal: SameOriginUIPrincipal, request_id: UUID) -> svc.ResponseResult:
    return await _call(svc.revoke, principal, request_id)


@router.get("")
async def connections(principal: CurrentUIPrincipal) -> list[svc.ConnectionEntry]:
    return await _call(svc.list_connections, principal)


@router.get("/blocked")
async def blocked(principal: CurrentUIPrincipal) -> list[svc.BlockedEntry]:
    return await _call(svc.list_blocked, principal)
