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
    RETIRED_MCP_TOOLS,
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
# Retired names are known (old CHANGELOG entries name them); test_retired_tools confines them.
ALL_OPERATIONS = IMPLEMENTED_MCP_TOOLS | set(PROVISIONAL_OPERATIONS) | RETIRED_MCP_TOOLS
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


def test_no_provisional_markers_remain() -> None:
    # 0.4.2: F5 #9/#28, F6 #15 and F7 #19/#20/#36 are merged, so nothing is provisional.
    assert PROVISIONAL_OPERATIONS == {}
    for name in DOC_NAMES:
        assert PROVISIONAL_MARKER not in documents()[name].body, name
        assert "live today" not in documents()[name].body, name


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
    # Every agent bearer route (marked with F2's bearerAuth scheme) is in the route catalog.
    bearer = bearer_routes(client)
    assert bearer
    for route in bearer:
        assert f"`{route}`" in index, route


def bearer_routes(client: TestClient) -> set[str]:
    """OpenAPI operations marked with the bearerAuth scheme, minus the identity route /me."""
    spec = client.get("/openapi.json").json()
    assert "bearerAuth" in spec["components"]["securitySchemes"]
    return {
        f"{method.upper()} {path}"
        for path, ops in spec["paths"].items()
        for method, op in ops.items()
        if {"bearerAuth": []} in op.get("security", []) and path != "/me"
    }


def test_bearer_routes_are_the_implemented_api_routes(client: TestClient) -> None:
    # The security marks, not a path-prefix list, say which routes take the bearer token.
    implemented_api = {r for r in IMPLEMENTED_HTTP if r.split(" ", 1)[1].startswith("/api/")}
    assert bearer_routes(client) == implemented_api


# --- HTTP API audience (B7): for assistants without MCP access, never for people ----------------

HTTP_AUDIENCE = (
    "The HTTP API is for assistants without MCP access. Access for them is not available yet "
    "(issuing them tokens is deferred), so use MCP, the supported path today."
)
API_TERM = re.compile(
    r"\bAPI\b|\bHTTP\b|/api/|openapi|\b(these|the|bearer|HTTP) routes?\b|\bendpoints?\b|\bcurl\b",
    re.I,
)
# Backticked spans hold routes and parameter names (`owner_confirmed`), not who calls them.
CODE_SPAN = re.compile(r"`[^`]*`")
# People, as opposed to assistants. "your user"/"the owner" are people too.
PERSON = re.compile(
    r"\b(people|persons?|humans?|developers?|members?|owners?|your user|the user|users|"
    r"you yourself|by hand|manually|directly)\b",
    re.I,
)
# Stale or human-facing framings the B7 decision retired.
RETIRED_HTTP_PHRASES = ("not decided yet", "decision is pending", "API-only clients")
NOT_FOR_PEOPLE = (
    "People never use the HTTP API themselves: they use their assistant or the Revel website."
)


def human_api_sentences(text: str) -> list[str]:
    """Prose sentences that mention the API and a person without a negation (tables and code
    excluded: rows name operations, not who calls them)."""
    prose = "\n".join(
        line
        for line in text.split("\n")
        if not line.startswith("|") and not line.lstrip().startswith(("```", "{"))
    )
    flat = " ".join(prose.split())
    found = []
    for sentence in re.split(r"(?<=[.:;])\s+", flat):
        words = CODE_SPAN.sub(" ", sentence)
        api = API_TERM.search(words) or "/api/" in sentence
        if api and PERSON.search(words) and not NEGATION.search(words):
            found.append(sentence)
    return found


def _served_without_history(name: str) -> str:
    body = documents()[name].body
    return body.split("\n## 0.6.0", 1)[0] if name == "CHANGELOG" else body


@pytest.mark.parametrize("name", DOC_NAMES)
def test_http_api_is_presented_for_assistants_only(name: str) -> None:
    text = _served_without_history(name)
    flat = " ".join(text.split())
    for phrase in RETIRED_HTTP_PHRASES:
        assert phrase not in flat, f"{name}: {phrase!r}"
    assert human_api_sentences(text) == [], name
    if name != "CHANGELOG" and HTTP_ROUTE_IN_TEXT.search(text):
        assert HTTP_AUDIENCE in flat, f"{name}: names HTTP routes without the audience sentence"


HTTP_ROUTE_IN_TEXT = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) /api/")


def test_index_says_people_never_use_the_api() -> None:
    flat = " ".join(documents()["index"].body.split())
    assert NOT_FOR_PEOPLE in flat
    assert "HTTP API reference, for assistants without MCP access" in flat
    assert "The HTTP column is the reference for assistants without MCP access" in flat
    assert "declares the bearer scheme (`bearerAuth`) on these routes" in flat
    for skill in (
        "revel-manage-profile",
        "revel-connect-and-read",
        "revel-find-a-gift",
        "revel-answer-question",
    ):
        assert "## HTTP equivalent (assistants without MCP access)" in documents()[skill].body


def test_human_api_lint_catches_violations() -> None:
    for bad in (
        "You or your user can call the HTTP API with a token.",
        "People can use the API directly.",
        "Developers call `GET /api/updates` with curl.",
        "The owner may use these routes in a browser.",
        "Use the API reference to explore it manually.",
    ):
        assert human_api_sentences(bad) == [bad], bad
    for ok in (
        NOT_FOR_PEOPLE,
        HTTP_AUDIENCE,
        "| Read your user's own profile | `get_profile` | `GET /api/v1/profile` |",
        "Owners can also do this in the Revel website.",
    ):
        assert human_api_sentences(ok) == [], ok


@pytest.mark.anyio
async def test_implemented_tools_are_registered() -> None:
    from revel.mcp.server import create_mcp_server
    from revel.settings import Settings

    settings = Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    registered = {t.name for t in await create_mcp_server(settings).list_tools()}
    assert registered >= IMPLEMENTED_MCP_TOOLS
