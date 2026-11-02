"""HTTP adapters. ``routers()`` lists the routers included at the application root."""

from fastapi import APIRouter


def routers() -> list[APIRouter]:
    from revel.auth.agents_router import router as agents_router
    from revel.auth.router import router as auth_router

    return [auth_router, agents_router]
