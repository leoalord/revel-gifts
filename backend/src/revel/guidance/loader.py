"""Load canonical guidance documents shipped as package data.

Canonical sources live in ``revel/guidance/skills/``: ``<name>/SKILL.md`` per skill, plus
``index.md`` and ``CHANGELOG.md``. They are read with ``importlib.resources``, so the same code
works from the source tree, an editable install and the non-editable wheel in the image; no
settings or absolute paths are involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib import resources
from importlib.resources.abc import Traversable

REQUIRED_KEYS = ("name", "description", "version", "status")
STATUSES = frozenset({"draft", "active", "deprecated"})
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
# Semantic Versioning 2.0.0 (core plus optional pre-release/build).
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
INDEX_NAME = "index"
CHANGELOG_NAME = "CHANGELOG"


class GuidanceError(ValueError):
    """A canonical guidance document is malformed."""


@dataclass(frozen=True)
class Document:
    name: str
    description: str
    version: str
    status: str
    body: str  # Markdown after the front matter, exactly as stored
    is_skill: bool


def skills_dir() -> Traversable:
    return resources.files("revel.guidance").joinpath("skills")


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split ``---`` front matter (flat ``key: value`` lines) from the body."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise GuidanceError("missing front matter")
    meta: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body = "\n".join(lines[i + 1 :]).lstrip("\n")
            return meta, body
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep or not key or key in meta:
            raise GuidanceError(f"bad front matter line {i + 1}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        meta[key] = value
    raise GuidanceError("unterminated front matter")


def parse_document(text: str, *, expected_name: str, is_skill: bool) -> Document:
    meta, body = parse_front_matter(text)
    missing = [k for k in REQUIRED_KEYS if not meta.get(k)]
    if missing:
        raise GuidanceError(f"{expected_name}: missing front matter keys {missing}")
    unknown = sorted(set(meta) - set(REQUIRED_KEYS))
    if unknown:
        raise GuidanceError(f"{expected_name}: unknown front matter keys {unknown}")
    if meta["name"] != expected_name or not NAME_RE.match(meta["name"]):
        raise GuidanceError(f"{expected_name}: name must match its file location")
    if not SEMVER_RE.match(meta["version"]):
        raise GuidanceError(f"{expected_name}: version must be semver")
    if meta["status"] not in STATUSES:
        raise GuidanceError(f"{expected_name}: status must be one of {sorted(STATUSES)}")
    if not body.strip():
        raise GuidanceError(f"{expected_name}: empty body")
    return Document(
        name=meta["name"],
        description=meta["description"],
        version=meta["version"],
        status=meta["status"],
        body=body,
        is_skill=is_skill,
    )


def load_documents(root: Traversable | None = None) -> dict[str, Document]:
    """All documents keyed by name: skills (sorted) then ``index`` and ``CHANGELOG``."""
    root = root or skills_dir()
    docs: dict[str, Document] = {}
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        skill_file = entry.joinpath("SKILL.md")
        if entry.is_dir() and skill_file.is_file():
            text = skill_file.read_bytes().decode("utf-8")
            docs[entry.name] = parse_document(text, expected_name=entry.name, is_skill=True)
    for name in (INDEX_NAME, CHANGELOG_NAME):
        text = root.joinpath(f"{name}.md").read_bytes().decode("utf-8")
        docs[name] = parse_document(text, expected_name=name, is_skill=False)
    if not any(d.is_skill for d in docs.values()):
        raise GuidanceError("no skills found")
    return docs


@cache
def documents() -> dict[str, Document]:
    """Canonical documents, loaded once per process."""
    return load_documents()
