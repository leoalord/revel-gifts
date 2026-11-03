"""Serve the built human UI (``frontend/dist``, Vite ``base: '/app/'``). Owned by F8.

* ``GET /`` — 302 to ``/app/``.
* ``GET /app``, ``/app/``, ``/app/{path}`` — a real file inside the dist directory is served
  as is (``assets/*`` are content-hashed, so cached immutably); a missing ``assets/*`` file is
  404; any other path gets ``index.html`` (``no-cache``) so the client router can handle it.
* The UI not being built (no ``index.html``) is **503** ``{"detail": "UI not built"}``; the app
  still starts and every other route works.

Only these paths are claimed, so ``/auth``, ``/.well-known``, ``/me``, ``/health`` and the MCP
endpoint keep their owners. The UI is same-origin with the API: no CORS headers are added.
Authentication and authorization stay server-side on the API routes; these files are public.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

router = APIRouter(include_in_schema=False)

APP_PREFIX = "/app/"
ASSETS_DIR = "assets"

# Vite's build emits only external module scripts and stylesheets (no inline script/style),
# so 'self' is sufficient and no 'unsafe-inline' is needed.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}
IMMUTABLE = "public, max-age=31536000, immutable"
NO_CACHE = "no-cache"


def _dist_dir(request: Request) -> Path:
    return request.app.state.settings.resolved_frontend_dist_dir.resolve()


def _file_in_dist(dist: Path, relative: str) -> Path | None:
    """Return the regular file ``relative`` names inside ``dist``, or None (never outside it)."""
    if not relative or "\x00" in relative:
        return None
    try:
        candidate = (dist / relative).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(dist) or candidate == dist:
        return None
    return candidate if candidate.is_file() else None


def _normalize(path: str) -> str:
    """Collapse ``//``, ``.`` and ``..`` segments (never above the root) to a relative path,
    so e.g. ``/app//assets/x.js`` and ``/app/./assets/x.js`` are treated as ``assets/x.js``."""
    if "\x00" in path:
        return path
    return posixpath.normpath("/" + path).lstrip("/")


def _headers(cache_control: str) -> dict[str, str]:
    return {**SECURITY_HEADERS, "Cache-Control": cache_control}


def _not_built() -> JSONResponse:
    return JSONResponse(
        {"detail": "UI not built"},
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        headers=_headers("no-store"),
    )


@router.api_route("/", methods=["GET", "HEAD"])
async def root() -> RedirectResponse:
    return RedirectResponse(APP_PREFIX, status_code=status.HTTP_302_FOUND)


@router.api_route("/app", methods=["GET", "HEAD"])
@router.api_route("/app/", methods=["GET", "HEAD"])
@router.api_route("/app/{path:path}", methods=["GET", "HEAD"])
async def ui(request: Request, path: str = "") -> Response:
    dist = _dist_dir(request)
    index = dist / "index.html"
    if not index.is_file():
        return _not_built()

    path = _normalize(path)
    file = _file_in_dist(dist, path)
    if file is not None:
        in_assets = file.parent.is_relative_to(dist / ASSETS_DIR)
        return FileResponse(file, headers=_headers(IMMUTABLE if in_assets else NO_CACHE))

    if path == ASSETS_DIR or path.startswith(ASSETS_DIR + "/"):
        # A stale hashed asset must not be answered with HTML.
        return JSONResponse(
            {"detail": "Not Found"},
            status_code=status.HTTP_404_NOT_FOUND,
            headers=_headers("no-store"),
        )
    return FileResponse(index, headers=_headers(NO_CACHE))
