"""Static guard: transport adapters may read claims only through the F4 visibility service.

For every module under ``revel/api`` and ``revel/mcp`` (comments ignored):

- referencing the ``Claim`` model, ``revel.db.models.profile`` or raw SQL on the ``claims``
  table requires using ``visible_claims`` or ``owner_claims``;
- the private tier helpers (``_visible_claims_for_tier``, ``_visible_claims_query``) and
  ``ViewerTier.only_me`` must never be used (code only; strings and docstrings are ignored).

A cheap textual check, not a proof; it stops accidental bypasses in new profile-read code.
"""

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "revel"
GUARDED_PACKAGES = ("api", "mcp")

_CLAIM_MODEL = re.compile(r"\bClaim\b|revel\.db\.models\.profile")
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


def guard_violations(root: Path, packages: tuple[str, ...]) -> dict[str, str]:
    violations: dict[str, str] = {}
    for package in packages:
        for path in sorted((root / package).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            code = _strip(source, strings=True)
            with_strings = _strip(source, strings=False)
            name = str(path.relative_to(root))
            if _FORBIDDEN.search(code):
                violations[name] = "uses a private tier helper or ViewerTier.only_me"
            elif (_CLAIM_MODEL.search(code) or _CLAIM_SQL.search(with_strings)) and not (
                _SERVICE_USE.search(code)
            ):
                violations[name] = "reads claims without visible_claims/owner_claims"
    return violations


def test_adapters_read_claims_only_through_visibility_service() -> None:
    assert guard_violations(SRC, GUARDED_PACKAGES) == {}


def test_guard_detects_bypasses(tmp_path: Path) -> None:
    api = tmp_path / "api"
    api.mkdir()
    files = {
        "ok.py": "from revel.db.models import Claim\n"
        "from revel.auth.visibility import visible_claims\n",
        "bad.py": "from revel.db.models import Claim\nselect(Claim)\n",
        "raw.py": 'text("SELECT * FROM claims WHERE profile_id = :p")\n',
        "private.py": "from revel.auth.visibility import (\n"
        "    _visible_claims_for_tier, visible_claims\n)\n",
        "tier.py": "from revel.auth.authz import ViewerTier\nTIER = ViewerTier.only_me\n",
        "comment_only.py": "# ViewerTier.only_me and _visible_claims_for_tier and Claim\nx = 1\n",
        "docstring.py": '"""Mentions ViewerTier.only_me and _visible_claims_for_tier."""\n',
        "unrelated.py": "x = 'claimstub'\n",
    }
    for name, body in files.items():
        (api / name).write_text(body)
    assert set(guard_violations(tmp_path, ("api",))) == {
        "api/bad.py",
        "api/raw.py",
        "api/private.py",
        "api/tier.py",
    }
