import re

import pytest
from mcp import Client

from revel.guidance import document_names, server_instructions
from revel.guidance.operations import IMPLEMENTED_MCP_TOOLS, PROVISIONAL_OPERATIONS
from revel.mcp.server import create_mcp_server
from revel.settings import Settings

AVAILABLE = re.compile(r"Available now: ([^.]*)\.")
PLANNED = re.compile(r"[^.]*\bplanned\b[^.]*\.")
TOOL_LIKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)*\b")


@pytest.mark.anyio
async def test_instructions_match_registered_tools(settings: Settings) -> None:
    server = create_mcp_server(settings)
    async with Client(server) as client:
        registered = {t.name for t in (await client.list_tools()).tools}
        instructions = client.instructions
    assert instructions == server_instructions()

    # Every tool named as available is registered, and every implemented tool is named.
    available = {n.strip() for n in AVAILABLE.search(instructions).group(1).split(",")}
    assert available <= registered, f"instructions name unregistered tools {available - registered}"
    assert available >= IMPLEMENTED_MCP_TOOLS

    # No registered tool is described as planned/unavailable.
    for sentence in PLANNED.findall(instructions):
        named = set(TOOL_LIKE.findall(sentence)) & registered
        assert not named, f"registered tools {named} described as unavailable"

    # No unregistered operation name appears anywhere.
    mentioned = set(TOOL_LIKE.findall(instructions)) & (
        set(PROVISIONAL_OPERATIONS) | IMPLEMENTED_MCP_TOOLS
    )
    assert mentioned <= registered


@pytest.mark.anyio
async def test_provisional_tool_registration_forces_guidance_update(settings: Settings) -> None:
    # When a provisional operation becomes a registered tool, move it to
    # IMPLEMENTED_MCP_TOOLS and update the guidance text (and the "planned" sentence).
    registered = {t.name for t in await create_mcp_server(settings).list_tools()}
    newly_live = registered & set(PROVISIONAL_OPERATIONS)
    assert not newly_live, f"{newly_live} are registered but still PROVISIONAL in guidance"


def test_instructions_short_and_pointing_to_guidance() -> None:
    text = server_instructions()
    assert len(text) < 600
    assert "\n" not in text and "{tools}" not in text
    assert "https://api.revel.gifts/guidance" in text
    assert "revel://guidance/index" in text
    assert "ping, whoami" in text


def test_tools_argument() -> None:
    assert "Available now: a, b." in server_instructions(["b", "a"])


@pytest.mark.anyio
async def test_instructions_not_served_as_guidance(settings: Settings) -> None:
    assert "server-instructions" not in document_names()
    server = create_mcp_server(settings)
    resources = [str(r.uri) for r in await server.list_resources()]
    prompts = [p.name for p in await server.list_prompts()]
    assert not any("instructions" in x for x in [*resources, *prompts])
