"""Website delivery: ``GET /guidance`` (index) and ``GET /guidance/{name}.md``.

Public and unauthenticated: the documents are static, non-personal instructions that clients
must be able to read before they hold a token. Names are looked up in the loaded document map,
never used as filesystem paths. The routes are included in OpenAPI so an agent reading
``/openapi.json`` can find the guidance.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response

from revel.guidance.render import UnknownGuidance, render_index, render_markdown

MEDIA_TYPE = "text/markdown; charset=utf-8"
HEADERS = {"Cache-Control": "public, max-age=300", "X-Content-Type-Options": "nosniff"}
_MARKDOWN_RESPONSE: dict[int | str, dict[str, object]] = {
    200: {"content": {"text/markdown": {}}, "description": "Markdown document"}
}


def _markdown(text: str) -> Response:
    return Response(content=text.encode("utf-8"), media_type=MEDIA_TYPE, headers=HEADERS)


def router() -> APIRouter:
    r = APIRouter(prefix="/guidance", tags=["guidance"])

    @r.get(
        "",
        response_class=Response,
        responses=_MARKDOWN_RESPONSE,
        summary="Agent guidance index (Markdown)",
    )
    async def guidance_index() -> Response:
        return _markdown(render_index())

    @r.get(
        "/{name}.md",
        response_class=Response,
        responses=_MARKDOWN_RESPONSE,
        summary="One agent guidance document (Markdown)",
    )
    async def guidance_document(name: str) -> Response:
        try:
            return _markdown(render_markdown(name))
        except UnknownGuidance:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown guidance"
            ) from None

    return r
