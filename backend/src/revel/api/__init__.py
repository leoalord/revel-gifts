"""HTTP adapters. ``routers()`` lists the routers included at the application root."""

from fastapi import APIRouter


def routers() -> list[APIRouter]:
    from revel.auth.router import router as auth_router

    return [auth_router]
