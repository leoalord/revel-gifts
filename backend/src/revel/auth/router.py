"""HTTP routes for authentication. Owned by F2.

Included at the application root (no prefix) so F2 can serve e.g.
``/.well-known/oauth-protected-resource/mcp`` and ``GET /me``.
"""

from fastapi import APIRouter

router = APIRouter()
