"""Expose guidance over MCP: one prompt per skill and one resource per document.

Uses the pinned SDK's public ``MCPServer.add_prompt`` / ``add_resource`` with
``mcp.server.mcpserver.prompts.base.Prompt`` and ``resources.TextResource``. Guidance is public,
non-personal text, so these operations resolve no principal; the endpoint's bearer middleware
still applies to the transport.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.prompts.base import Prompt
from mcp.server.mcpserver.resources import TextResource

from revel.guidance.loader import documents
from revel.guidance.render import render_markdown

RESOURCE_PREFIX = "revel://guidance/"
MIME_TYPE = "text/markdown"


def resource_uri(name: str) -> str:
    return f"{RESOURCE_PREFIX}{name}"


def _prompt_fn(text: str) -> Callable[[], str]:
    def prompt() -> str:
        return text

    return prompt


def register_mcp(server: MCPServer) -> None:
    """Register every skill as a no-argument prompt and every document as a resource."""
    for name, doc in documents().items():
        text = render_markdown(name)
        server.add_resource(
            TextResource(
                uri=resource_uri(name),
                name=name,
                title=f"Revel guidance: {name}",
                description=doc.description,
                mime_type=MIME_TYPE,
                text=text,
            )
        )
        if doc.is_skill:
            server.add_prompt(
                Prompt.from_function(
                    _prompt_fn(text), name=name, title=name, description=doc.description
                )
            )
