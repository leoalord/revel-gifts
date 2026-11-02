"""Render canonical guidance into the published Markdown shared by HTTP and MCP.

Both delivery formats call :func:`render_markdown`, so the website page, the MCP prompt and the
MCP resource for a document are byte-identical: a short header derived from the front matter
followed by the canonical body, unchanged.
"""

from __future__ import annotations

from revel.guidance.loader import INDEX_NAME, Document, documents

DRAFT_NOTE = (
    "Draft guidance: operations marked PROVISIONAL are planned and not yet available. "
    "Use only tools that appear in your tool list."
)


class UnknownGuidance(LookupError):
    """No guidance document has this name."""


def header(doc: Document) -> str:
    lines = [
        f"> Revel guidance `{doc.name}` · version {doc.version} · status: {doc.status}",
        ">",
        f"> {doc.description}",
    ]
    if doc.status == "draft":
        lines += [">", f"> {DRAFT_NOTE}"]
    return "\n".join(lines) + "\n\n"


def get_document(name: str) -> Document:
    try:
        return documents()[name]
    except KeyError:
        raise UnknownGuidance(name) from None


def render_markdown(name: str) -> str:
    doc = get_document(name)
    return header(doc) + doc.body


def render_index() -> str:
    return render_markdown(INDEX_NAME)


def skill_names() -> list[str]:
    return [d.name for d in documents().values() if d.is_skill]


def document_names() -> list[str]:
    return list(documents())
