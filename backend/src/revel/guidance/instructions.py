"""MCP server ``instructions`` text, from package data (``server-instructions.txt``).

The file lives outside ``skills/``, so it is not served as a guidance page, prompt or resource.
``MCPServer`` takes ``instructions`` at construction, before any tool is registered, and the
pinned SDK exposes no public setter. So the "available now" list comes from
``operations.IMPLEMENTED_MCP_TOOLS``, and a test checks it against the tools
``create_mcp_server`` actually registers.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
from importlib import resources

from revel.guidance.operations import IMPLEMENTED_MCP_TOOLS

TEMPLATE_FILE = "server-instructions.txt"


@cache
def _template() -> str:
    text = resources.files("revel.guidance").joinpath(TEMPLATE_FILE).read_text("utf-8")
    return " ".join(text.split())


def server_instructions(tools: Iterable[str] | None = None) -> str:
    """Short server instructions naming ``tools`` (default: the implemented MCP tools)."""
    names = sorted(IMPLEMENTED_MCP_TOOLS if tools is None else tools)
    return _template().replace("{tools}", ", ".join(names))
