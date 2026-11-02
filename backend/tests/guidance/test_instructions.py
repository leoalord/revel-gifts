import re

import pytest
from mcp import Client

from revel.guidance import document_names, server_instructions
from revel.guidance.operations import IMPLEMENTED_MCP_TOOLS, PROVISIONAL_OPERATIONS
from revel.mcp.server import create_mcp_server
from revel.settings import Settings


def test_instructions_name_no_tools() -> None:
    # Instructions are fixed at construction, so they must not list tools (they would go stale).
    text = server_instructions()
    tool_names = IMPLEMENTED_MCP_TOOLS | {op for op in PROVISIONAL_OPERATIONS if " " not in op}
    for name in tool_names:
        assert not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text), name
    assert "Use only the tools in your tool list" in text


@pytest.mark.anyio
async def test_client_receives_instructions(settings: Settings) -> None:
    async with Client(create_mcp_server(settings)) as client:
        assert client.instructions == server_instructions()


def test_instructions_short_and_pointing_to_guidance() -> None:
    text = server_instructions()
    assert len(text) < 600
    assert "\n" not in text
    assert "https://api.revel.gifts/guidance" in text
    assert "revel://guidance/index" in text


@pytest.mark.anyio
async def test_instructions_not_served_as_guidance(settings: Settings) -> None:
    assert "server-instructions" not in document_names()
    server = create_mcp_server(settings)
    resources = [str(r.uri) for r in await server.list_resources()]
    prompts = [p.name for p in await server.list_prompts()]
    assert not any("instructions" in x for x in [*resources, *prompts])
    text = server_instructions()
    for resource in await server.list_resources():
        contents = await server.read_resource(resource.uri)
        assert all(text not in c.content for c in contents)
