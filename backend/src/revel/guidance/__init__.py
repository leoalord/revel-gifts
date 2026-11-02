"""F9 agent guidance: one canonical source rendered for the website and for MCP.

Canonical Markdown is package data under ``skills/`` (see ``loader``). ``render_markdown`` is
the single renderer; ``register_mcp`` and ``router`` are the transport adapters.
"""

from revel.guidance.instructions import server_instructions
from revel.guidance.loader import Document, GuidanceError, documents, load_documents
from revel.guidance.mcp_adapter import register_mcp, resource_uri
from revel.guidance.render import (
    UnknownGuidance,
    document_names,
    render_index,
    render_markdown,
    skill_names,
)
from revel.guidance.web import router

__all__ = [
    "Document",
    "GuidanceError",
    "UnknownGuidance",
    "document_names",
    "documents",
    "load_documents",
    "register_mcp",
    "render_index",
    "render_markdown",
    "resource_uri",
    "router",
    "server_instructions",
    "skill_names",
]
