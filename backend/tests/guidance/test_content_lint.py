"""Content lint for canonical guidance: live vs provisional operations, forbidden content."""

import re

import pytest

from revel.guidance import documents, render_markdown
from revel.guidance.operations import (
    IMPLEMENTED_HTTP,
    IMPLEMENTED_MCP_TOOLS,
    PROVISIONAL_MARKER,
    PROVISIONAL_OPERATIONS,
)

DOC_NAMES = list(documents())
BACKTICK = re.compile(r"`([^`\n]+)`")
HTTP_ROUTE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE) (/\S*)$")
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")

# Backticked lowercase identifiers that are field names or enum values, not operations.
FIELDS_AND_VALUES = frozenset(
    {
        # F5 categories and fields/enums (provisional contract)
        "interest",
        "dislike",
        "owned_item",
        "size",
        "birthday",
        "location",
        "category",
        "statement",
        "details",
        "evidence_type",
        "explicit",
        "observed",
        "inferred",
        "confidence",
        "low",
        "medium",
        "high",
        "unknown",
        "observed_at",
        "observed_precision",
        "day",
        "month",
        "year",
        "audience",
        "trusted_circle",
        "supersedes_id",
        "accepted",
        "pending_review",
        "rejected",
        "reason_code",
        "forbidden_category",
        "resource",
        "authorization_servers",
        # F7 fields/states (Questions page)
        "question",
        "private_purpose",
        "deadline",
        "escalation",
        "type",
        "evidence",
        "waiting_for_user",
    }
)
ALL_OPERATIONS = IMPLEMENTED_MCP_TOOLS | set(PROVISIONAL_OPERATIONS)
DOC_NAME_TOKENS = frozenset(DOC_NAMES)


PROVISIONAL_PATTERNS = {
    op: re.compile(rf"(?<![\w/]){re.escape(op)}(?![\w/])") for op in PROVISIONAL_OPERATIONS
}


def unknown_tokens(text: str) -> list[str]:
    """Backticked routes/identifiers that are neither known operations nor allowed terms."""
    found = []
    for number, line in enumerate(text.split("\n"), start=1):
        for token in BACKTICK.findall(line):
            if HTTP_ROUTE.match(token):
                if token not in IMPLEMENTED_HTTP and token not in PROVISIONAL_OPERATIONS:
                    found.append(f"{number}: route {token!r}")
            elif IDENT.match(token) and token not in (
                ALL_OPERATIONS | FIELDS_AND_VALUES | DOC_NAME_TOKENS
            ):
                found.append(f"{number}: {token!r}")
    return found


def unmarked_provisional(text: str) -> list[str]:
    """Lines mentioning a provisional operation (or an /api/ route) without the marker."""
    found = []
    for number, line in enumerate(text.split("\n"), start=1):
        mentioned = [op for op, p in PROVISIONAL_PATTERNS.items() if p.search(line)]
        if (mentioned or "/api/" in line) and PROVISIONAL_MARKER not in line:
            found.append(f"{number}: {mentioned or '/api/ route'}")
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
    assert unmarked_provisional("Call `add_facts` now.") == ["1: ['add_facts']"]
    assert unmarked_provisional("Call `add_facts`. <!-- PROVISIONAL: x -->") == []
    assert unmarked_provisional("Use `GET /api/v1/x`.") == ["1: /api/ route"]
    assert unmarked_provisional("Call `whoami`.") == []
    assert unknown_tokens("Call `delete_everything`.") == ["1: 'delete_everything'"]
    assert unknown_tokens("Call `POST /api/v1/secret`.") == ["1: route 'POST /api/v1/secret'"]
    assert unknown_tokens("Call `ping` with `evidence_type`.") == []


def test_provisional_names_are_not_implemented() -> None:
    assert not (set(PROVISIONAL_OPERATIONS) & (IMPLEMENTED_MCP_TOOLS | IMPLEMENTED_HTTP))


def test_get_me_is_not_presented_to_agents() -> None:
    for name in DOC_NAMES:
        assert "/me`" not in documents()[name].body and "GET /me" not in documents()[name].body


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
