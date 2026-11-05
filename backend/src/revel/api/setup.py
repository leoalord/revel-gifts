"""Owner polling setup over browser and bearer transports."""

from __future__ import annotations

import anyio.to_thread
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from revel.auth.deps import CurrentPrincipal
from revel.auth.identity import Principal
from revel.auth.ui import CurrentUIPrincipal, SameOriginUIPrincipal
from revel.db.session import get_sessionmaker
from revel.services import inbox_setup as svc

router = APIRouter(tags=["setup"])


async def _call(fn, principal: Principal, *args):
    try:
        return await anyio.to_thread.run_sync(lambda: fn(principal, *args, get_sessionmaker()))
    except svc.SetupError as exc:
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "detail": exc.detail}
        )


@router.get("/api/setup", response_model=svc.SetupStatus)
async def ui_get(principal: CurrentUIPrincipal):
    return await _call(svc.read_status, principal)


@router.put("/api/setup", response_model=svc.SetupStatus)
async def ui_put(body: svc.ScheduleRequest, principal: SameOriginUIPrincipal):
    return await _call(svc.configure, principal, body)


@router.post("/api/setup/host-task", response_model=svc.SetupStatus)
async def ui_report(body: svc.TaskReport, principal: SameOriginUIPrincipal):
    return await _call(svc.report_task, principal, body)


@router.get("/api/v1/setup", response_model=svc.SetupStatus)
async def agent_get(principal: CurrentPrincipal):
    return await _call(svc.read_status, principal)


@router.put("/api/v1/setup", response_model=svc.SetupStatus)
async def agent_put(body: svc.ScheduleRequest, principal: CurrentPrincipal):
    return await _call(svc.configure, principal, body)


@router.post("/api/v1/setup/host-task", response_model=svc.SetupStatus)
async def agent_report(body: svc.TaskReport, principal: CurrentPrincipal):
    return await _call(svc.report_task, principal, body)
