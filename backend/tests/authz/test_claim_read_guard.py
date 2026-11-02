"""Static guard: adapters and services may read claims only through the F4 visibility service.

For every module under ``revel/api``, ``revel/mcp`` and ``revel/services`` (comments ignored):

- the private tier helpers (``_visible_claims_for_tier``, ``_visible_claims_query``) and
  ``ViewerTier.only_me`` must never be used (code only; strings and docstrings are ignored);
- a direct claim QUERY is a violation even when the file also uses ``visible_claims`` or
  ``owner_claims``: ``select(...Claim...)``, column access ``Claim.<attr>``, ``get``/``query``/
  ``insert``/``update``/``delete`` on ``Claim``, or raw SQL on the ``claims`` table. Only
  modules listed in ``CLAIM_WRITE_MODULES`` may do this;
- any other reference to the ``Claim`` model or ``revel.db.models.profile`` (an import, a type
  annotation) requires using ``visible_claims`` or ``owner_claims`` in the same module.

``CLAIM_WRITE_MODULES`` is the explicit allowlist of claim WRITE modules (F5's contribution and
review services). It is empty until F5 adds them. To add one: append its path relative to
``src/revel`` (e.g. ``"services/contributions.py"``) with a comment naming the task, keep reads
in that module going through ``visible_claims``/``owner_claims``, and have the F4 owner review
the change. Allowlisted modules are still checked for the private helpers.

A cheap textual check, not a proof; it stops accidental bypasses in new profile-read code.
Known gaps: an aliased import (``from revel.db.models import Claim as C``), a query built in
an unguarded package and called from here, and ORM relationship traversal (none exists on
``Profile`` today) are not detected; review catches those.
"""

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "revel"
GUARDED_PACKAGES = ("api", "mcp", "services")
# Claim WRITE modules, relative to src/revel (see the module docstring). Empty until F5.
CLAIM_WRITE_MODULES: frozenset[str] = frozenset()

_CLAIM_MODEL = re.compile(r"\bClaim\b|revel\.db\.models\.profile")
_CLAIM_QUERY = re.compile(
    r"\bselect\s*\([^)]*\bClaim\b"
    r"|\bClaim\s*\.\s*\w"
    r"|\b(?:get|query|insert|update|delete)\s*\(\s*Claim\b"
)
_CLAIM_SQL = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+claims\b", re.IGNORECASE)
_SERVICE_USE = re.compile(r"\b(?:visible_claims|owner_claims)\b")
_FORBIDDEN = re.compile(
    r"\b_visible_claims_for_tier\b|\b_visible_claims_query\b|\bViewerTier\s*\.\s*only_me\b"
)


def _strip(source: str, *, strings: bool) -> str:
    """Source without comments (and without string literals when ``strings``)."""
    dropped = {tokenize.COMMENT} | ({tokenize.STRING} if strings else set())
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    return tokenize.untokenize(
        (tok.type, "" if tok.type in dropped else tok.string, tok.start, tok.end, tok.line)
        for tok in tokens
    )


def guard_violations(
    root: Path, packages: tuple[str, ...], writers: frozenset[str] = CLAIM_WRITE_MODULES
) -> dict[str, str]:
    violations: dict[str, str] = {}
    for package in packages:
        for path in sorted((root / package).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            code = _strip(source, strings=True)
            with_strings = _strip(source, strings=False)
            name = path.relative_to(root).as_posix()
            if _FORBIDDEN.search(code):
                violations[name] = "uses a private tier helper or ViewerTier.only_me"
            elif name in writers:
                continue
            elif _CLAIM_QUERY.search(code) or _CLAIM_SQL.search(with_strings):
                violations[name] = "queries claims directly (not an allowlisted write module)"
            elif _CLAIM_MODEL.search(code) and not _SERVICE_USE.search(code):
                violations[name] = "reads claims without visible_claims/owner_claims"
    return violations


def test_adapters_read_claims_only_through_visibility_service() -> None:
    assert guard_violations(SRC, GUARDED_PACKAGES) == {}


def test_write_allowlist_names_existing_guarded_modules() -> None:
    for name in CLAIM_WRITE_MODULES:
        assert name.split("/", 1)[0] in GUARDED_PACKAGES and (SRC / name).is_file(), name


def test_guard_detects_bypasses(tmp_path: Path) -> None:
    files = {
        "api/ok.py": "from revel.db.models import Claim\n"
        "from revel.auth.visibility import visible_claims\n"
        "def f(rows: list[Claim]) -> None: ...\n",
        "api/bad.py": "from revel.db.models import Claim\nselect(Claim)\n",
        "api/raw.py": 'text("SELECT * FROM claims WHERE profile_id = :p")\n',
        "api/private.py": "from revel.auth.visibility import (\n"
        "    _visible_claims_for_tier, visible_claims\n)\n",
        "api/tier.py": "from revel.auth.authz import ViewerTier\nTIER = ViewerTier.only_me\n",
        "api/comment_only.py": (
            "# ViewerTier.only_me and _visible_claims_for_tier and Claim\nx = 1\n"
        ),
        "api/docstring.py": '"""Mentions ViewerTier.only_me and _visible_claims_for_tier."""\n',
        "api/unrelated.py": "x = 'claimstub'\n",
        # One mention of the service no longer excuses a direct query in the same file.
        "mcp/mixed.py": "from revel.auth.visibility import owner_claims\n"
        "from revel.db.models import Claim\n"
        "rows = select(Claim.id).where(Claim.profile_id == p)\n",
        "mcp/mixed_get.py": "from revel.auth.visibility import visible_claims\n"
        "row = session.get(Claim, claim_id)\n",
        "mcp/mixed_sql.py": "from revel.auth.visibility import visible_claims\n"
        'text("UPDATE claims SET status = :s")\n',
        "services/reader.py": "from revel.db.models import Claim\n"
        "rows = session.execute(\n    select(\n        Claim\n    )\n)\n",
        # Allowlisted writers may query claims, but never use the private helpers.
        "services/writes.py": "from revel.db.models import Claim\n"
        "session.execute(update(Claim).where(Claim.id == c))\n",
        "services/writes_private.py": "from revel.auth.visibility import _visible_claims_query\n",
    }
    for name, body in files.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
    writers = frozenset({"services/writes.py", "services/writes_private.py"})
    assert set(guard_violations(tmp_path, GUARDED_PACKAGES, writers)) == {
        "api/bad.py",
        "api/raw.py",
        "api/private.py",
        "api/tier.py",
        "mcp/mixed.py",
        "mcp/mixed_get.py",
        "mcp/mixed_sql.py",
        "services/reader.py",
        "services/writes_private.py",
    }
