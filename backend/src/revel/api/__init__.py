"""HTTP adapters. ``routers()`` lists the routers included at the application root."""

from fastapi import APIRouter


def routers() -> list[APIRouter]:
    from revel.api.ui_static import router as ui_static_router
    from revel.auth.agents_router import router as agents_router
    from revel.auth.router import router as auth_router
    from revel.guidance import router as guidance_router

    return [auth_router, agents_router, guidance_router(), ui_static_router]
