"""HTTP adapters for F5 profiles and contributions (RG-18). Thin: every route calls
``revel.services.profiles``, which owns behavior, authorization and claim access (claim reads
go through F4's ``visible_claims``/``owner_claims`` inside the service).

- ``/api/v1/profile*``: agent API, bearer token (``CurrentPrincipal``).
- ``/api/ui/profile*``: browser (F8), session cookie; unsafe methods are same-origin checked
  (``SameOriginUIPrincipal``).
- ``/api/v1/profiles/{handle}`` (bearer only) and ``/api/ui/profiles/{handle}`` (browser
  session, read-only GET): another person's profile as the caller may see it (RG-56, #28).
  Principal first, then F6's ``resolve_handle``, then ``read_shared_profile`` with the
  resolved profile id; both return the same body and errors. No route accepts a profile id.

Contract: ``docs/contracts/f5-profiles.md``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from revel.auth.authz import AuthorizationDenied
from revel.auth.deps import CurrentPrincipal
from revel.auth.identity import Principal
from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal
from revel.db.enums import Audience, ClaimStatus
from revel.services import connections, profiles
from revel.services.profiles import (
    AddFactsResult,
    FactInput,
    FactOut,
    FactPatch,
    PolicyResponse,
    PolicyUpdate,
    ProfileOut,
    SharedProfileOut,
    SummaryOut,
)

logger = logging.getLogger(__name__)

UNAVAILABLE = "Authorization service unavailable"


async def db_session() -> AsyncIterator[Session]:
    async with profiles.session_scope() as session:
        yield session


DbSession = Annotated[Session, Depends(db_session)]


class FactsBody(BaseModel):
    facts: list[FactInput] = Field(min_length=1, max_length=profiles.MAX_FACTS_PER_BATCH)


class AudienceBody(BaseModel):
    audience: Audience


async def _call[T](operation: Awaitable[T], response: Response) -> T | JSONResponse:
    """Await a service call, mapping service errors to the contract's HTTP errors.

    409 bodies carry the current fact or policy, so they are returned as a ``JSONResponse``
    that copies any refreshed session cookie the UI dependency set on ``response``."""
    try:
        return await operation
    except profiles.FactNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "fact_not_found") from None
    except (profiles.NotAllowed, AuthorizationDenied):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not_allowed") from None
    except profiles.AuthorizationUnavailable:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None
    except (profiles.PolicyInvalid, profiles.InvalidRequest) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, exc.errors) from None
    except profiles.FactConflict as exc:
        body = {"detail": exc.reason, "fact": exc.fact.model_dump(mode="json")}
    except profiles.PolicyVersionConflict as exc:
        body = {"detail": "policy_version_conflict", **exc.policy.model_dump(mode="json")}
    conflict = JSONResponse(status_code=status.HTTP_409_CONFLICT, content=body)
    for cookie in response.headers.getlist("set-cookie"):
        conflict.headers.append("set-cookie", cookie)
    return conflict


StatusFilter = Annotated[list[ClaimStatus] | None, Query(alias="status")]
Since = Annotated[datetime | None, Query()]

# --- Agent API (bearer) -----------------------------------------------------------------------

api = APIRouter(prefix="/api/v1/profile", tags=["profile"])


@api.get("/policy", response_model=PolicyResponse)
async def api_get_policy(
    response: Response, principal: CurrentPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.get_policy(session, principal), response)


@api.post("/facts", response_model=AddFactsResult)
async def api_add_facts(
    response: Response, body: FactsBody, principal: CurrentPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.add_facts(session, principal, body.facts), response)


@api.get("", response_model=ProfileOut)
async def api_get_profile(
    response: Response,
    principal: CurrentPrincipal,
    session: DbSession,
    statuses: StatusFilter = None,
    since: Since = None,
) -> Any:
    return await _call(
        profiles.get_my_profile(session, principal, statuses=statuses, since=since), response
    )


@api.get("/summary", response_model=SummaryOut)
async def api_get_summary(
    response: Response, principal: CurrentPrincipal, session: DbSession, since: Since = None
) -> Any:
    return await _call(profiles.get_summary(session, principal, since=since), response)


@api.patch("/facts/{fact_id}/audience", response_model=FactOut)
async def api_set_audience(
    response: Response,
    fact_id: UUID,
    body: AudienceBody,
    principal: CurrentPrincipal,
    session: DbSession,
) -> Any:
    return await _call(
        profiles.set_fact_audience(session, principal, fact_id, body.audience), response
    )


@api.delete("/facts/{fact_id}", response_model=FactOut)
async def api_remove_fact(
    response: Response, fact_id: UUID, principal: CurrentPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.remove_fact(session, principal, fact_id), response)


# --- Browser UI (cookie; unsafe methods same-origin) ------------------------------------------

ui = APIRouter(prefix="/api/ui/profile", tags=["profile-ui"])


@ui.get("/policy", response_model=PolicyResponse)
async def ui_get_policy(
    response: Response, principal: CurrentUIPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.get_policy(session, principal), response)


@ui.put("/policy", response_model=PolicyResponse)
async def ui_set_policy(
    response: Response, body: PolicyUpdate, principal: SameOriginUIPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.set_policy(session, principal, body), response)


@ui.post("/facts", response_model=AddFactsResult)
async def ui_add_facts(
    response: Response, body: FactsBody, principal: SameOriginUIPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.add_facts(session, principal, body.facts), response)


@ui.get("", response_model=ProfileOut)
async def ui_get_profile(
    response: Response,
    principal: CurrentUIPrincipal,
    session: DbSession,
    statuses: StatusFilter = None,
    since: Since = None,
) -> Any:
    return await _call(
        profiles.get_my_profile(session, principal, statuses=statuses, since=since), response
    )


@ui.get("/summary", response_model=SummaryOut)
async def ui_get_summary(
    response: Response, principal: CurrentUIPrincipal, session: DbSession, since: Since = None
) -> Any:
    return await _call(profiles.get_summary(session, principal, since=since), response)


@ui.patch("/facts/{fact_id}", response_model=FactOut)
async def ui_patch_fact(
    response: Response,
    fact_id: UUID,
    principal: SameOriginUIPrincipal,
    session: DbSession,
    body: Annotated[FactPatch, Body()],
) -> Any:
    return await _call(profiles.patch_fact(session, principal, fact_id, body), response)


@ui.delete("/facts/{fact_id}", response_model=FactOut)
async def ui_remove_fact(
    response: Response, fact_id: UUID, principal: SameOriginUIPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.remove_fact(session, principal, fact_id), response)


@ui.post("/facts/{fact_id}/approve", response_model=FactOut)
async def ui_approve_fact(
    response: Response, fact_id: UUID, principal: SameOriginUIPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.approve_fact(session, principal, fact_id), response)


@ui.post("/facts/{fact_id}/reject", response_model=FactOut)
async def ui_reject_fact(
    response: Response, fact_id: UUID, principal: SameOriginUIPrincipal, session: DbSession
) -> Any:
    return await _call(profiles.reject_fact(session, principal, fact_id), response)


# --- Shared reads: another person's profile by exact handle ------------------------------------


async def _read_shared(principal: Principal, handle: str) -> SharedProfileOut:
    """Every hidden case (nonexistent, malformed, undiscoverable, blocking, no profile, no
    access) is the same 404 ``target_not_found``; errors never carry service text."""
    try:
        async with profiles.session_scope() as session:
            target = await connections.resolve_handle(session, principal, handle=handle)
            return await profiles.read_shared_profile(
                session, principal, profile_id=target.profile_id
            )
    except (connections.TargetNotFound, profiles.SharedProfileNotFound):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "target_not_found") from None
    except connections.RateLimited:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited") from None
    except connections.NotAllowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not_allowed") from None
    except Exception as exc:
        logger.warning("shared profile read failed (%s)", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, UNAVAILABLE) from None


shared = APIRouter(prefix="/api/v1/profiles", tags=["profile"])


@shared.get("/{handle}", response_model=SharedProfileOut)
async def api_get_shared_profile(handle: str, principal: CurrentPrincipal) -> Any:
    return await _read_shared(principal, handle)


ui_shared = APIRouter(prefix="/api/ui/profiles", tags=["profile-ui"])


@ui_shared.get("/{handle}", response_model=SharedProfileOut)
async def ui_get_shared_profile(handle: str, principal: CurrentUIPrincipal) -> Any:
    return await _read_shared(principal, handle)


router = APIRouter()
router.include_router(api)
router.include_router(ui)
router.include_router(shared)
router.include_router(ui_shared)
