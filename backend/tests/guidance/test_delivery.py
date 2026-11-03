from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import Client
from mcp.server.mcpserver import MCPServer

from revel import guidance
from revel.guidance import documents, render_markdown, resource_uri, skill_names
from revel.guidance.operations import RENAMED_SKILLS
from revel.mcp.server import create_mcp_server
from revel.settings import Settings


@pytest.fixture
def guidance_client() -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(guidance.router())
    with TestClient(app) as client:
        yield client


@pytest.fixture
def server() -> MCPServer:
    s = MCPServer(name="test")
    guidance.register_mcp(s)
    return s


def test_render_is_header_plus_canonical_body() -> None:
    for name, doc in documents().items():
        rendered = render_markdown(name)
        assert rendered.endswith(doc.body)
        assert f"version {doc.version}" in rendered.split("\n", 1)[0]
        assert f"status: {doc.status}" in rendered.split("\n", 1)[0]


def test_http_index_and_documents(guidance_client: TestClient) -> None:
    index = guidance_client.get("/guidance")
    assert index.status_code == 200
    assert index.headers["content-type"] == "text/markdown; charset=utf-8"
    assert index.headers["cache-control"] == "public, max-age=300"
    assert index.headers["x-content-type-options"] == "nosniff"
    assert index.content == render_markdown("index").encode("utf-8")
    for name in documents():
        response = guidance_client.get(f"/guidance/{name}.md")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"
        assert response.content == render_markdown(name).encode("utf-8")


@pytest.mark.parametrize(
    "path",
    [
        "/guidance/nope.md",
        "/guidance/revel-manage-profile",
        "/guidance/..%2Floader.py.md",
        "/guidance/revel-manage-profile%2FSKILL.md",
        "/guidance/SKILL.md",
    ],
)
def test_http_unknown_404(guidance_client: TestClient, path: str) -> None:
    assert guidance_client.get(path).status_code == 404


@pytest.mark.anyio
async def test_mcp_prompts_and_resources_match_http(
    server: MCPServer, guidance_client: TestClient
) -> None:
    async with Client(server) as client:
        prompts = {p.name: p for p in (await client.list_prompts()).prompts}
        assert set(prompts) == set(skill_names())
        resources = {str(r.uri): r for r in (await client.list_resources()).resources}
        assert set(resources) == {resource_uri(n) for n in documents()}

        for name, doc in documents().items():
            http_bytes = guidance_client.get(f"/guidance/{name}.md").content
            resource = resources[resource_uri(name)]
            assert resource.mime_type == "text/markdown"
            assert resource.description == doc.description
            read = await client.read_resource(resource_uri(name))
            assert len(read.contents) == 1
            assert read.contents[0].text.encode("utf-8") == http_bytes
            if doc.is_skill:
                assert prompts[name].arguments in (None, [])
                assert prompts[name].description == doc.description
                got = await client.get_prompt(name)
                assert len(got.messages) == 1
                assert got.messages[0].role == "user"
                assert got.messages[0].content.text.encode("utf-8") == http_bytes


@pytest.mark.anyio
async def test_unknown_prompt_and_resource_fail(server: MCPServer) -> None:
    async with Client(server) as client:
        with pytest.raises(Exception):  # noqa: B017 - SDK error type is transport-specific
            await client.get_prompt("nope")
        with pytest.raises(Exception):  # noqa: B017
            await client.read_resource("revel://guidance/nope")


def test_wired_into_app(client: TestClient) -> None:
    # Registered through revel.api.routers(), so the MCP mount at "/" does not shadow it.
    response = client.get("/guidance/revel-find-a-gift.md")
    assert response.status_code == 200
    assert response.content == render_markdown("revel-find-a-gift").encode("utf-8")
    assert client.get("/guidance").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert "/guidance" in paths and "/guidance/{name}.md" in paths


@pytest.mark.anyio
async def test_wired_into_mcp_server(settings: Settings) -> None:
    server = create_mcp_server(settings)
    names = {p.name for p in await server.list_prompts()}
    assert names == set(skill_names())
    tools = {t.name for t in await server.list_tools()}
    assert {"ping", "whoami"} <= tools


# --- Renamed skills (0.7.0): old pages redirect, old MCP names are gone --------------------------


def test_renamed_skills_map_old_names_to_live_skills() -> None:
    assert RENAMED_SKILLS == {
        "revel-profile-discovery": "revel-manage-profile",
        "revel-giver-workflow": "revel-find-a-gift",
    }
    for old, new in RENAMED_SKILLS.items():
        assert old not in documents(), old
        assert new in skill_names(), new


@pytest.mark.parametrize(("old", "new"), list(RENAMED_SKILLS.items()))
def test_old_skill_pages_permanently_redirect(
    guidance_client: TestClient, old: str, new: str
) -> None:
    response = guidance_client.get(f"/guidance/{old}.md", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == f"/guidance/{new}.md"
    followed = guidance_client.get(f"/guidance/{old}.md")
    assert followed.status_code == 200
    assert followed.headers["content-type"] == "text/markdown; charset=utf-8"
    assert followed.content == render_markdown(new).encode("utf-8")
    # Only the exact old page redirects; other spellings stay unknown.
    assert guidance_client.get(f"/guidance/{old}", follow_redirects=False).status_code == 404
    assert guidance_client.get(f"/guidance/{old}.MD", follow_redirects=False).status_code == 404


def test_old_skill_pages_redirect_in_the_app(client: TestClient) -> None:
    for old, new in RENAMED_SKILLS.items():
        response = client.get(f"/guidance/{old}.md", follow_redirects=False)
        assert response.status_code == 308, old
        assert response.headers["location"] == f"/guidance/{new}.md"


@pytest.mark.anyio
async def test_old_skill_names_are_not_mcp_prompts_or_resources(server: MCPServer) -> None:
    async with Client(server) as client:
        prompts = {p.name for p in (await client.list_prompts()).prompts}
        resources = {str(r.uri) for r in (await client.list_resources()).resources}
        for old, new in RENAMED_SKILLS.items():
            assert old not in prompts and new in prompts
            assert resource_uri(old) not in resources and resource_uri(new) in resources
            with pytest.raises(Exception):  # noqa: B017 - SDK error type is transport-specific
                await client.get_prompt(old)
            with pytest.raises(Exception):  # noqa: B017
                await client.read_resource(resource_uri(old))
