from importlib import resources
from pathlib import Path

import pytest

from revel.guidance import loader
from revel.guidance.loader import GuidanceError, load_documents, parse_document

EXPECTED_SKILLS = {
    "revel-get-started",
    "revel-manage-profile",
    "revel-connect-and-read",
    "revel-find-a-gift",
    "revel-answer-question",
}

VALID = """---
name: demo
description: A demo skill.
version: 0.1.0
status: draft
---

# Demo

Body.
"""


def test_canonical_files_are_package_data() -> None:
    # Resolved through importlib.resources (no settings, no repo-relative paths), so it works
    # the same from the source tree and from the installed wheel in the image.
    root = loader.skills_dir()
    assert root.joinpath("index.md").is_file()
    assert root.joinpath("CHANGELOG.md").is_file()
    for name in EXPECTED_SKILLS:
        assert root.joinpath(name, "SKILL.md").is_file()
    pkg = Path(str(resources.files("revel.guidance")))
    assert pkg.name == "guidance" and pkg.parent.name == "revel"


def test_all_documents_load_and_validate() -> None:
    docs = load_documents()
    assert {n for n, d in docs.items() if d.is_skill} == EXPECTED_SKILLS
    assert {"index", "CHANGELOG"} <= set(docs)
    for doc in docs.values():
        assert doc.status == "active", doc.name  # 0.4.0: every operation named is merged
        assert loader.SEMVER_RE.match(doc.version)
        assert not doc.body.startswith("---")


def test_parse_valid() -> None:
    doc = parse_document(VALID, expected_name="demo", is_skill=True)
    assert (doc.name, doc.version, doc.status) == ("demo", "0.1.0", "draft")
    assert doc.body == "# Demo\n\nBody.\n"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# no front matter\n", "missing front matter"),
        ("---\nname: demo\n", "unterminated"),
        (VALID.replace("version: 0.1.0\n", ""), "missing front matter keys"),
        (VALID.replace("0.1.0", "1.0"), "semver"),
        (VALID.replace("0.1.0", "v0.1.0"), "semver"),
        (VALID.replace("status: draft", "status: live"), "status"),
        (VALID.replace("name: demo", "name: other"), "name must match"),
        (VALID.replace("status: draft", "status: draft\nowner: x"), "unknown front matter"),
        (VALID.replace("status: draft", "status: draft\nstatus: draft"), "bad front matter"),
        (VALID.replace("status: draft", "just text"), "bad front matter"),
        (VALID.replace("# Demo\n\nBody.\n", ""), "empty body"),
    ],
)
def test_parse_rejects(text: str, message: str) -> None:
    with pytest.raises(GuidanceError, match=message):
        parse_document(text, expected_name="demo", is_skill=True)


def test_quoted_values() -> None:
    doc = parse_document(
        VALID.replace("A demo skill.", '"A demo: skill."'), expected_name="demo", is_skill=True
    )
    assert doc.description == "A demo: skill."
