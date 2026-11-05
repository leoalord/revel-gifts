"""Browser invitation adapter; all capabilities travel in request bodies, never route URLs."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal
from revel.services import invitations as svc


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


router = APIRouter(
    prefix="/api/invitations", tags=["invitations"], dependencies=[Depends(no_store)]
)


class CreateBody(BaseModel):
    email: str = Field(max_length=254)
    channel: Literal["copy", "email"] = "copy"
    owner_confirmed: bool = False


class ConfirmBody(BaseModel):
    owner_confirmed: bool = False


class TokenBody(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class RespondBody(TokenBody):
    action: Literal["accept", "decline"]


async def _call(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except svc.InvitationError as exc:
        return JSONResponse(
            status_code=exc.status,
            content={"code": exc.code, "detail": exc.message},
            headers={"Cache-Control": "no-store"},
        )


@router.get("")
async def list_sent(principal: CurrentUIPrincipal, request: Request):
    return await _call(svc.list_sent, principal, settings=request.app.state.settings)


@router.post("")
async def create(principal: SameOriginUIPrincipal, body: CreateBody, request: Request):
    return await _call(
        svc.create, principal, settings=request.app.state.settings, **body.model_dump()
    )


@router.post("/preview")
async def preview(principal: SameOriginUIPrincipal, body: TokenBody, request: Request):
    return await _call(
        svc.redeem,
        principal,
        token=body.token,
        action="preview",
        settings=request.app.state.settings,
    )


@router.post("/join")
async def join(principal: SameOriginUIPrincipal, body: TokenBody, request: Request):
    return await _call(svc.join, principal, token=body.token, settings=request.app.state.settings)


@router.post("/respond")
async def respond(principal: SameOriginUIPrincipal, body: RespondBody, request: Request):
    return await _call(
        svc.redeem, principal, settings=request.app.state.settings, **body.model_dump()
    )


@router.post("/{invitation_id}/send")
async def send(
    principal: SameOriginUIPrincipal, invitation_id: UUID, body: ConfirmBody, request: Request
):
    return await _call(
        svc.send, principal, invitation_id, settings=request.app.state.settings, **body.model_dump()
    )


@router.post("/{invitation_id}/cancel")
async def cancel(principal: SameOriginUIPrincipal, invitation_id: UUID, request: Request):
    return await _call(svc.cancel, principal, invitation_id, settings=request.app.state.settings)
