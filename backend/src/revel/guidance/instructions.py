"""MCP server ``instructions`` text, from package data (``server-instructions.txt``).

The file lives outside ``skills/``, so it is not served as a guidance page, prompt or resource.
It names no tools: ``instructions`` is fixed when ``MCPServer`` is constructed, so a tool list
would go stale as features register tools. Clients are told to rely on their tool list.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

TEMPLATE_FILE = "server-instructions.txt"


@cache
def server_instructions() -> str:
    """The server instructions as one line of text."""
    text = resources.files("revel.guidance").joinpath(TEMPLATE_FILE).read_text("utf-8")
    return " ".join(text.split())
