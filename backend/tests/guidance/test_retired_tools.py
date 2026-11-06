"""RG-59 tool consolidation: retired tool names never appear in served guidance.

Served guidance is every rendered document (HTTP Markdown), the MCP server ``instructions`` and
every MCP prompt and resource text. The only exception is CHANGELOG entries for versions before
``RETIRED_IN_VERSION``, which describe the history. A retired name that is still a live action
value (``report_user_contact`` is now an ``update_assignment`` action) may appear only as that
action, never as a tool to call.
"""

from __future__ import annotations

import re

import pytest
from mcp import Client

from revel.guidance import documents, render_markdown, server_instructions
from revel.guidance.operations import (
    IMPLEMENTED_MCP_TOOLS,
    REMOVED_MCP_TOOLS,
    RENAMED_IN_VERSION,
    RENAMED_SKILLS,
    RETIRED_IN_VERSION,
    RETIRED_MCP_TOOLS,
)
from revel.mcp.server import create_mcp_server
from revel.settings import Settings

from .test_skills_f6_f7 import TOOLS

CHANGELOG = "CHANGELOG"
VERSION_HEADING = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\b", re.M)
# Action values of registered tools (from their input schemas, not copied).
ACTION_VALUES = frozenset(
    value
    for tool in TOOLS.values()
    for value in tool.parameters["properties"].get("action", {}).get("enum", [])
)
# Where an action-valued retired name counts as the action, matched against the text directly
# before it (whitespace collapsed); the name must follow immediately:
_LIST_ITEM = r"`[a-z_]+`(?:, | and | or |, and |, or )"
ACTION_CONTEXTS = (
    # "action `x`", "`action` `x`"
    re.compile(r"\b`?action`? `$"),
    # an action list: "actions `a`, `b` and `x`", "(actions `a` ... `x`", "`action` (`a`, ... `x`"
    re.compile(rf"\b`?actions?`? \(?(?:{_LIST_ITEM})+`$"),
    # the JSON value of "action"
    re.compile(r'"action": "$'),
    # a quoted error message's action list: "expected one of: a, b, x"
    re.compile(r"expected one of: (?:[a-z_]+, )*$"),
)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def current_changelog(text: str) -> str:
    """The changelog without entries for versions before RETIRED_IN_VERSION."""
    cutoff = _version(RETIRED_IN_VERSION)
    kept, keep = [], True
    for line in text.split("\n"):
        if m := VERSION_HEADING.match(line):
            keep = tuple(int(g) for g in m.groups()) >= cutoff
        if keep:
            kept.append(line)
    return "\n".join(kept)


def _in_action_table(lines: list[str], number: int) -> bool:
    """A table row whose table's first header cell is `action`."""
    if not lines[number].startswith("|"):
        return False
    for line in reversed(lines[:number]):
        if not line.startswith("|"):
            return False
        if re.match(r"^\| `action` \|", line):
            return True
    return False


def retired_mentions(text: str, retired: frozenset[str] = RETIRED_MCP_TOOLS) -> list[str]:
    """Mentions of retired tool names; action values count only outside an action context."""
    found = []
    lines = text.split("\n")
    for name in sorted(retired):
        pattern = re.compile(rf"(?<![\w/]){re.escape(name)}(?![\w/])")
        for match in pattern.finditer(text):
            number = text.count("\n", 0, match.start())
            if name in ACTION_VALUES:
                before = text[: match.start()]
                trailing = " " if before[-1:].isspace() else ""
                before = " ".join(before.split()) + trailing
                if any(c.search(before) for c in ACTION_CONTEXTS) or _in_action_table(
                    lines, number
                ):
                    continue
            found.append(f"{number + 1}: {name}")
    return found


def served_documents() -> dict[str, str]:
    served = {name: render_markdown(name) for name in documents()}
    served[CHANGELOG] = current_changelog(served[CHANGELOG])
    served["server-instructions"] = server_instructions()
    return served


def test_removed_tools_are_only_mentioned_in_the_changelog() -> None:
    for name, text in served_documents().items():
        if name != CHANGELOG:
            assert retired_mentions(text, REMOVED_MCP_TOOLS) == [], name


def test_removed_tools_are_not_registered() -> None:
    assert not REMOVED_MCP_TOOLS & IMPLEMENTED_MCP_TOOLS
    assert not REMOVED_MCP_TOOLS & set(TOOLS)


def test_retired_action_collisions_are_known() -> None:
    # A retired tool name that is also a live action value is only allowed as that action;
    # a new collision must be reviewed (and its action-context rule checked) before it ships.
    assert {"report_user_contact"} == RETIRED_MCP_TOOLS & ACTION_VALUES
    assert {"approve", "change_tier", "revoke", "cancel", "set_audience"} <= ACTION_VALUES


def test_retired_tools_are_not_registered_or_implemented() -> None:
    assert RETIRED_MCP_TOOLS
    assert not RETIRED_MCP_TOOLS & set(TOOLS)
    assert not RETIRED_MCP_TOOLS & IMPLEMENTED_MCP_TOOLS


@pytest.mark.parametrize("name", list(served_documents()))
def test_served_guidance_names_no_retired_tool(name: str) -> None:
    assert retired_mentions(served_documents()[name]) == []


@pytest.mark.anyio
async def test_mcp_prompts_resources_and_instructions_name_no_retired_tool() -> None:
    settings = Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    server = create_mcp_server(settings)
    async with Client(server) as client:
        texts = {"instructions": server.instructions or ""}
        for prompt in (await client.list_prompts()).prompts:
            result = await client.get_prompt(prompt.name)
            texts[f"prompt {prompt.name}"] = "\n".join(
                m.content.text for m in result.messages if hasattr(m.content, "text")
            )
        for resource in (await client.list_resources()).resources:
            result = await client.read_resource(resource.uri)
            texts[f"resource {resource.uri}"] = "\n".join(
                c.text for c in result.contents if hasattr(c, "text")
            )
    assert len(texts) > len(documents())  # every resource plus prompts were read
    changelog_uri = f"resource revel://guidance/{CHANGELOG}"
    texts[changelog_uri] = current_changelog(texts[changelog_uri])
    for label, text in texts.items():
        assert retired_mentions(text) == [], label


def test_every_document_is_at_the_latest_changelog_version() -> None:
    # One release: every guidance file carries the version of the newest CHANGELOG entry, which
    # is at least the version that retired the old tools.
    first = VERSION_HEADING.search(documents()[CHANGELOG].body)
    assert first is not None
    latest = ".".join(first.groups())
    assert _version(latest) >= _version(RETIRED_IN_VERSION)
    assert "unreleased" not in documents()[CHANGELOG].body
    for name, doc in documents().items():
        assert doc.version == latest, name


def test_index_what_works_today_lists_every_tool() -> None:
    body = documents()["index"].body
    section = body.split("## What works today", 1)[1].split("\n## ", 1)[0]
    for tool in TOOLS:
        assert f"`{tool}`" in section, tool


def test_changelog_exemption_is_only_for_older_versions() -> None:
    full = render_markdown(CHANGELOG)
    # The history does name retired tools, so the exemption is doing something...
    assert retired_mentions(full)
    # ...and the entry for the retiring version exists and is checked.
    current = current_changelog(full)
    assert f"## {RETIRED_IN_VERSION}" in current
    assert "## 0.5.4" not in current and "## 0.5.4" in full


# --- Renamed skills (0.7.0) ----------------------------------------------------------------------

RETIRED_SKILL_NAMES = frozenset(RENAMED_SKILLS)
# In the renaming CHANGELOG entry only, an old name may appear as the rename note
# ("(was `old`)") or as the redirected page ("/guidance/old.md").
_RENAME_NOTE = re.compile(r"\(was `([a-z-]+)`\)|/guidance/([a-z-]+)\.md")


def changelog_since(text: str, version: str) -> str:
    """The changelog without entries for versions before ``version``."""
    cutoff, kept, keep = _version(version), [], True
    for line in text.split("\n"):
        if m := VERSION_HEADING.match(line):
            keep = tuple(int(g) for g in m.groups()) >= cutoff
        if keep:
            kept.append(line)
    return "\n".join(kept)


def retired_skill_mentions(text: str, *, rename_notes: bool = False) -> list[str]:
    """Mentions of old skill names (``rename_notes``: except in the renaming entry's notes)."""
    if rename_notes:
        text = _RENAME_NOTE.sub(
            lambda m: "" if (m.group(1) or m.group(2)) in RETIRED_SKILL_NAMES else m.group(0), text
        )
    found = []
    for name in sorted(RETIRED_SKILL_NAMES):
        for match in re.finditer(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text):
            found.append(f"{text.count(chr(10), 0, match.start()) + 1}: {name}")
    return found


def served_skill_texts() -> dict[str, str]:
    """Every served text, the CHANGELOG reduced to entries since RENAMED_IN_VERSION."""
    served = {name: render_markdown(name) for name in documents()}
    served[CHANGELOG] = changelog_since(served[CHANGELOG], RENAMED_IN_VERSION)
    served["server-instructions"] = server_instructions()
    return served


@pytest.mark.parametrize("name", list(served_skill_texts()))
def test_served_guidance_names_no_retired_skill(name: str) -> None:
    text = served_skill_texts()[name]
    assert retired_skill_mentions(text, rename_notes=name == CHANGELOG) == []


@pytest.mark.anyio
async def test_mcp_texts_name_no_retired_skill() -> None:
    settings = Settings(_env_file=None, app_env="test", public_base_url="http://localhost:8000")
    server = create_mcp_server(settings)
    async with Client(server) as client:
        texts = {"instructions": server.instructions or ""}
        for prompt in (await client.list_prompts()).prompts:
            assert prompt.name not in RETIRED_SKILL_NAMES
            result = await client.get_prompt(prompt.name)
            texts[f"prompt {prompt.name}"] = "\n".join(
                m.content.text for m in result.messages if hasattr(m.content, "text")
            )
        for resource in (await client.list_resources()).resources:
            result = await client.read_resource(resource.uri)
            texts[f"resource {resource.uri}"] = "\n".join(
                c.text for c in result.contents if hasattr(c, "text")
            )
    changelog_uri = f"resource revel://guidance/{CHANGELOG}"
    texts[changelog_uri] = changelog_since(texts[changelog_uri], RENAMED_IN_VERSION)
    for label, text in texts.items():
        assert retired_skill_mentions(text, rename_notes=label == changelog_uri) == [], label


def test_retired_skill_exemptions_are_narrow() -> None:
    full = render_markdown(CHANGELOG)
    current = changelog_since(full, RENAMED_IN_VERSION)
    # The history names the old skills (so the cutoff is doing something)...
    assert retired_skill_mentions(full.replace(current, ""))
    assert f"## {RENAMED_IN_VERSION}" in current and "## 0.6.0" not in current
    # ...and the renaming entry names each old skill only as a rename note or redirect.
    for old, new in RENAMED_SKILLS.items():
        assert f"`{new}` (was `{old}`)" in current, old
        assert f"`/guidance/{old}.md`" in current, old
    assert retired_skill_mentions(current)  # the notes are there, and only notes are exempt
    # Detection: plain mentions, URLs, prompt and resource names all count.
    assert retired_skill_mentions("see `revel-giver-workflow`") == ["1: revel-giver-workflow"]
    for text in (
        "revel://guidance/revel-profile-discovery",
        "https://api.revel.gifts/guidance/revel-profile-discovery.md",
        "the prompt revel-profile-discovery",
    ):
        assert retired_skill_mentions(text) == ["1: revel-profile-discovery"], text
    # Rename notes are exempt only when allowed, and only for retired names.
    note = "`revel-find-a-gift` (was `revel-giver-workflow`)"
    assert retired_skill_mentions(note) == ["1: revel-giver-workflow"]
    assert retired_skill_mentions(note, rename_notes=True) == []
    assert retired_skill_mentions("Use revel-giver-workflow.", rename_notes=True) == [
        "1: revel-giver-workflow"
    ]
    assert retired_skill_mentions("revel-giver-workflow-v2 and xrevel-giver-workflow") == []


def test_retired_mentions_detects_tool_usage() -> None:
    assert retired_mentions("Call `claim_assignment` now.") == ["1: claim_assignment"]
    assert retired_mentions("Then `get_answer`.") == ["1: get_answer"]
    assert "report_user_contact" in ACTION_VALUES
    # An action-valued name is allowed as the action, never as a tool.
    assert retired_mentions("Call `report_user_contact` with `lease_version`.") == [
        "1: report_user_contact"
    ]
    assert retired_mentions("```json report_user_contact\n{}\n```") == ["1: report_user_contact"]
    assert retired_mentions("Use action `report_user_contact`.") == []
    # The word "action" nearby is not enough (review finding on the earlier window rule).
    loose = "Use action `renew` if needed. When your user replies, call `report_user_contact` now."
    assert retired_mentions(loose) == ["1: report_user_contact"]
    assert retired_mentions("an action, then `report_user_contact`") == ["1: report_user_contact"]
    # Each allowed action context, as the guidance uses it.
    for allowed in (
        "(actions `claim`,\n  `renew`, `decline` and `report_user_contact`)",
        "an `action` (`claim`, `decline` or `report_user_contact`)",
        "for action `report_user_contact`,",
        "expected one of: claim,\nrenew, decline, report_user_contact.",
    ):
        assert retired_mentions(allowed) == [], allowed
    assert retired_mentions('{"action": "report_user_contact"}') == []
    table = "| `action` | Required |\n| --- | --- |\n| `report_user_contact` | x |"
    assert retired_mentions(table) == []
    assert retired_mentions("| Tool |\n| --- |\n| `report_user_contact` |") == [
        "3: report_user_contact"
    ]
    assert (
        current_changelog("# C\n\n## 0.6.0\n\nnew\n\n## 0.5.4\n\nold") == "# C\n\n## 0.6.0\n\nnew\n"
    )
