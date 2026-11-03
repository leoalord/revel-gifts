"""Content lint for canonical guidance: live vs provisional operations, forbidden content."""

import re
from collections.abc import Iterable

import pytest
from fastapi.testclient import TestClient

from revel.guidance import documents, render_markdown
from revel.guidance.operations import (
    IMPLEMENTED_HTTP,
    IMPLEMENTED_MCP_TOOLS,
    PROVISIONAL_MARKER,
    PROVISIONAL_OPERATIONS,
)

from .test_discovery_f5 import F5_VOCABULARY
from .test_skills_f6_f7 import F6_F7_VOCABULARY

DOC_NAMES = list(documents())
BACKTICK = re.compile(r"`([^`\n]+)`")
HTTP_ROUTE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$")
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

# Backticked lowercase identifiers that are not derived from F5/F6/F7 code (see
# ``F5_VOCABULARY`` and ``F6_F7_VOCABULARY``): OAuth metadata fields and a JSON literal.
FIELDS_AND_VALUES = frozenset({"resource", "authorization_servers", "true"})
ALL_OPERATIONS = IMPLEMENTED_MCP_TOOLS | set(PROVISIONAL_OPERATIONS)
DOC_NAME_TOKENS = frozenset(DOC_NAMES)


API_PATH = re.compile(r"/api/[^\s`|]*")


def provisional_patterns(operations: Iterable[str]) -> dict[str, re.Pattern[str]]:
    return {op: re.compile(rf"(?<![\w/]){re.escape(op)}(?![\w/])") for op in operations}


PROVISIONAL_PATTERNS = provisional_patterns(PROVISIONAL_OPERATIONS)


def unknown_tokens(text: str) -> list[str]:
    """Backticked routes/identifiers that are neither known operations nor allowed terms."""
    found = []
    for number, line in enumerate(text.split("\n"), start=1):
        for token in BACKTICK.findall(line):
            if HTTP_ROUTE.match(token):
                if token not in IMPLEMENTED_HTTP and token not in PROVISIONAL_OPERATIONS:
                    found.append(f"{number}: route {token!r}")
            elif IDENT.match(token) and token not in (
                ALL_OPERATIONS
                | FIELDS_AND_VALUES
                | DOC_NAME_TOKENS
                | F5_VOCABULARY
                | F6_F7_VOCABULARY
            ):
                found.append(f"{number}: {token!r}")
    return found


def unmarked_provisional(
    text: str, patterns: dict[str, re.Pattern[str]] | None = None
) -> list[str]:
    """Lines mentioning a provisional operation, or an /api/ path that is not implemented,
    without the marker."""
    patterns = PROVISIONAL_PATTERNS if patterns is None else patterns
    found = []
    for number, line in enumerate(text.split("\n"), start=1):
        mentioned = [op for op, p in patterns.items() if p.search(line)]
        unlisted_api = [
            path
            for path in API_PATH.findall(line)
            if not any(route.endswith(" " + path) for route in IMPLEMENTED_HTTP)
            and path != "/api/v1/profile"
        ]
        if (mentioned or unlisted_api) and PROVISIONAL_MARKER not in line:
            found.append(f"{number}: {mentioned or unlisted_api}")
    return found


@pytest.mark.parametrize("name", DOC_NAMES)
def test_backticked_operations_are_known(name: str) -> None:
    assert unknown_tokens(documents()[name].body) == [], (
        "add new operations to revel.guidance.operations, or terms to FIELDS_AND_VALUES"
    )


@pytest.mark.parametrize("name", DOC_NAMES)
def test_provisional_operations_are_marked(name: str) -> None:
    assert unmarked_provisional(documents()[name].body) == []


def test_lint_catches_violations() -> None:
    future = provisional_patterns({"future_tool"})  # no operation is provisional since 0.4.0
    assert unmarked_provisional("Call `future_tool` now.", future) == ["1: ['future_tool']"]
    assert unmarked_provisional("Call `future_tool`. <!-- PROVISIONAL: x -->", future) == []
    assert unmarked_provisional("Call `get_updates` now.") == []
    assert unmarked_provisional("Use `GET /api/v1/x`.") == ["1: ['/api/v1/x']"]
    assert unmarked_provisional("Use `GET /api/v1/profile/policy`.") == []
    assert unmarked_provisional("Call `add_facts`.") == []
    assert unmarked_provisional("Call `whoami`.") == []
    assert unknown_tokens("Call `delete_everything`.") == ["1: 'delete_everything'"]
    assert unknown_tokens("Call `POST /api/v1/secret`.") == ["1: route 'POST /api/v1/secret'"]
    assert unknown_tokens("Call `ping` with `evidence_type`.") == []


def test_provisional_names_are_not_implemented() -> None:
    assert not (set(PROVISIONAL_OPERATIONS) & (IMPLEMENTED_MCP_TOOLS | IMPLEMENTED_HTTP))


def test_me_and_auth_routes_are_not_presented_to_agents() -> None:
    # /me and /auth/* may only be mentioned to say they are not the agent path.
    for name in DOC_NAMES:
        text = documents()[name].body
        assert "GET /me" not in text
        lines = text.split("\n")
        for number, line in enumerate(lines):
            if "`/me`" in line or "/auth/" in line:
                window = " ".join(lines[number : number + 2])
                assert "not the agent" in window or "not for assistants" in window, (
                    f"{name}:{number + 1}: /me or /auth/* presented to agents"
                )


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
FORBIDDEN = [
    re.compile(r"Bearer\s+ey", re.I),  # JWT-looking token
    re.compile(r"\bey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bsk[-_][A-Za-z0-9_]{8,}"),  # API-key-looking strings
    re.compile(r"client_secret", re.I),
    re.compile(r"\bpassword\b", re.I),
]
NEGATION = re.compile(r"\b(never|not|no|without)\b", re.I)


@pytest.mark.parametrize("name", DOC_NAMES)
def test_no_forbidden_content(name: str) -> None:
    text = render_markdown(name)
    for pattern in FORBIDDEN:
        assert not pattern.search(text), f"{name}: forbidden pattern {pattern.pattern}"
    for match in EMAIL.finditer(text):
        assert match.group(0).endswith("@example.com"), f"{name}: email {match.group(0)}"
    lead_in = ""  # the sentence introducing a bullet list, e.g. "Never collect or submit:"
    for number, line in enumerate(text.split("\n"), start=1):
        if line.strip() and not line.lstrip().startswith("- ") and not line.startswith("  "):
            lead_in = line
        for phrase in ("birth year", "street address", "home address", "conversation id"):
            if phrase in line.lower():
                assert NEGATION.search(line) or NEGATION.search(lead_in), (
                    f"{name}:{number}: {phrase!r} must only appear as a prohibition"
                )
    # Only the placeholder form of a bearer credential may appear.
    for match in re.finditer(r"Bearer\s+(\S+)", text):
        assert match.group(1).startswith(("<access", "error=")), f"{name}: {match.group(0)}"


# Only F7's pending human-contact change (0.4.1) may still be provisional; remove it, its
# markers and this allowance once F7's PR merges and tests bind to the real code.
PENDING_F7 = frozenset({"human_contact_required"})
MARKED_DOCS = frozenset({"revel-answer-question", "CHANGELOG"})


def test_only_pending_f7_change_is_provisional() -> None:
    assert set(PROVISIONAL_OPERATIONS) == PENDING_F7
    for name in DOC_NAMES:
        body = documents()[name].body
        assert "live today" not in body, name
        if name not in MARKED_DOCS:
            assert PROVISIONAL_MARKER not in body, name
        for line in body.split("\n"):
            if PROVISIONAL_MARKER in line:
                assert "F7 pending PR" in line, f"{name}: {line}"


def test_implemented_http_routes_exist_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    published = {f"{method.upper()} {path}" for path, ops in paths.items() for method in ops}
    # FastAPI's own schema and the OAuth metadata routes are served outside the schema (the
    # metadata answers 503 here because the test settings configure no WorkOS domain).
    for route in sorted(IMPLEMENTED_HTTP - published):
        method, path = route.split(" ", 1)
        assert method == "GET" and client.get(path).status_code in {200, 503}, route


def test_index_lists_every_registered_tool_and_bearer_route(client: TestClient) -> None:
    from revel.mcp.server import create_mcp_server
    from revel.settings import Settings

    settings = Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    server = create_mcp_server(settings)
    registered = {t.name for t in server._tool_manager.list_tools()}
    assert registered == IMPLEMENTED_MCP_TOOLS  # no unlisted (or missing) tool
    index = documents()["index"].body
    for tool in registered:
        assert f"`{tool}`" in index, tool
    paths = client.get("/openapi.json").json()["paths"]
    for path, ops in paths.items():
        if path.startswith(("/api/v1/", "/api/questions", "/api/updates", "/api/assignments")):
            for method in ops:
                assert f"`{method.upper()} {path}`" in index, f"{method.upper()} {path}"


@pytest.mark.anyio
async def test_implemented_tools_are_registered() -> None:
    from revel.mcp.server import create_mcp_server
    from revel.settings import Settings

    settings = Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    registered = {t.name for t in await create_mcp_server(settings).list_tools()}
    assert registered >= IMPLEMENTED_MCP_TOOLS
