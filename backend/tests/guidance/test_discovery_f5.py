"""The discovery skill against F5's merged code (#9): examples, names, enums and reason codes.

Everything here is derived from F5's modules (no copied definitions), so a rename in F5 fails
these tests until the guidance is updated.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, get_args

import pytest

from revel.api import profiles as profiles_api
from revel.db.enums import Audience, ClaimStatus, Confidence, EvidenceType, ObservedPrecision
from revel.guidance import documents
from revel.mcp import profile_tools
from revel.services import categories, claim_writes, profiles

DISCOVERY = "revel-profile-discovery"
EXAMPLE_FENCE = re.compile(r"^```json add_facts([^\n]*)\n(.*?)\n```$", re.M | re.S)
BACKTICK = re.compile(r"`([^`\n]+)`")
IDENT = re.compile(r"^[a-z][a-z0-9_]*$")


def _reason_codes() -> frozenset[str]:
    codes: set[str] = set()
    stack = [categories.CategoryError]
    while stack:
        cls = stack.pop()
        codes.add(cls.reason_code)
        stack.extend(cls.__subclasses__())
    source = inspect.getsource(profiles)
    codes |= set(re.findall(r'_Rejected\(\s*"([a-z_]+)"', source))
    codes |= set(re.findall(r'_rejected\(\s*index,\s*"([a-z_]+)"', source))
    # F5 #29 owner controls and the HTTP error bodies (detail strings and body keys).
    codes.add(profiles.OwnerConfirmationRequired.reason_code)
    codes.add(profile_tools._INVALID_REQUEST)  # RG-59 per-action validation errors
    api_source = inspect.getsource(profiles_api)
    codes |= set(re.findall(r'"detail": "([a-z_]+)"', api_source))
    codes |= set(re.findall(r'"([a-z_]+)": exc\.', api_source))
    # Fact-level 409 reasons (HTTP detail), e.g. fact_not_removable and fact_not_pending.
    codes |= set(
        re.findall(r'ClaimStateConflict\(claim, "([a-z_]+)"\)', inspect.getsource(claim_writes))
    )
    return frozenset(codes)


REASON_CODES = _reason_codes()
OUTCOMES = frozenset(get_args(profiles.Outcome))
ENUM_VALUES = frozenset(
    v.value
    for enum in (Audience, ClaimStatus, Confidence, EvidenceType, ObservedPrecision)
    for v in enum
)
CATEGORY_IDS = frozenset(categories.CATEGORY_BY_ID)
DETAIL_KEYS = frozenset(f.name for c in categories.CATEGORIES for f in c.detail_fields)
MODEL_FIELDS = frozenset(
    name
    for model in (
        profiles.FactInput,
        profiles.FactOutcome,
        profiles.PolicyResponse,
        profiles.PolicyOut,
        profiles.CategoryOut,
        profile_tools.OwnerProfileView,
    )
    for name in model.model_fields
)


def _tool_params() -> frozenset[str]:
    from revel.mcp import profile_tools  # noqa: F401  (tools are closures; read their schema)
    from revel.mcp.server import create_mcp_server
    from revel.settings import Settings

    server = create_mcp_server(
        Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    )
    names: set[str] = set()
    for tool in server._tool_manager.list_tools():
        properties = tool.parameters.get("properties", {})
        names |= set(properties)
        # Action values (RG-59 action tools), from each tool's own schema.
        names |= set(properties.get("action", {}).get("enum", []))
    return frozenset(names)


F5_VOCABULARY = (
    REASON_CODES
    | OUTCOMES
    | ENUM_VALUES
    | CATEGORY_IDS
    | DETAIL_KEYS
    | MODEL_FIELDS
    | _tool_params()
)
# Reason codes the discovery text names; each must exist in F5.
DOCUMENTED_REASON_CODES = frozenset(
    {
        "sensitive_not_explicit",
        "invalid_field",
        "invalid_details",
        "unknown_category",
        "forbidden_category",
        "supersedes_not_found",
        "enrichment_disabled",
    }
)


def _attrs(info: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in info.split())


def examples() -> list[tuple[dict[str, str], dict[str, Any]]]:
    body = documents()[DISCOVERY].body
    found = [(_attrs(info), json.loads(raw)) for info, raw in EXAMPLE_FENCE.findall(body)]
    assert found, "no fenced add_facts examples found"
    # Every add_facts fence is checked here; owner-control fences in test_owner_controls_f5.
    assert body.count("```json add_facts") == len(found)
    return found


def test_reason_code_extraction_found_codes() -> None:
    # Guards the source scan itself: if F5 restructures, this fails rather than passing vacuously.
    assert {"sensitive_not_explicit", "enrichment_disabled", "supersedes_not_found"} <= REASON_CODES
    assert {"unknown_category", "forbidden_category", "invalid_details", "invalid_field"} <= (
        REASON_CODES
    )


@pytest.mark.parametrize(("attrs", "item"), examples())
def test_example_is_valid_fact_input(attrs: dict[str, str], item: dict[str, Any]) -> None:
    fact = profiles.FactInput.model_validate(item)  # F5's real input model (extra="forbid")
    assert attrs["outcome"] in OUTCOMES
    if "status" in attrs:
        assert attrs["status"] in {s.value for s in ClaimStatus}
    if attrs["outcome"] == "rejected":
        assert attrs["reason_code"] in REASON_CODES
    else:
        spec = categories.validate_category(fact.category)
        categories.validate_details(spec, fact.details)


def test_examples_cover_documented_outcomes() -> None:
    seen = {(a["outcome"], bool(i.get("involves_others")), i["category"]) for a, i in examples()}
    assert ("pending_review", True, "life_event") in seen
    assert ("pending_review", False, "sensitive") in seen
    assert any(outcome == "rejected" for outcome, _, _ in seen)
    assert any(outcome == "accepted" for outcome, _, _ in seen)


def test_documented_reason_codes_exist() -> None:
    body = documents()[DISCOVERY].body
    for code in DOCUMENTED_REASON_CODES:
        assert code in REASON_CODES, f"{code} is not an F5 reason code"
        assert f"`{code}`" in body


def test_discovery_terms_exist_in_f5() -> None:
    """Every backticked identifier in discovery is a live tool, a tool parameter or an F5
    field, enum value, category id, details key, outcome or reason code."""
    from revel.guidance.operations import IMPLEMENTED_MCP_TOOLS

    allowed = F5_VOCABULARY | IMPLEMENTED_MCP_TOOLS | {"true"}
    unknown = sorted(
        {
            token
            for token in BACKTICK.findall(documents()[DISCOVERY].body)
            if IDENT.match(token) and token not in allowed
        }
    )
    assert unknown == []


def test_tool_params_named_in_text_exist() -> None:
    params = _tool_params()
    assert {"facts", "fact_id", "audience"} <= params
