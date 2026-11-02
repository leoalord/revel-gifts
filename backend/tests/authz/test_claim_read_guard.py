"""Static guard: transport adapters may read claims only through the F4 visibility service.

Any module under ``revel/api`` or ``revel/mcp`` that references the ``Claim`` model or the
``claims`` table must also use ``visible_claims`` or ``owner_claims``. This is a cheap textual
check, not a proof; it stops accidental direct queries in new profile-read code.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "revel"
GUARDED_PACKAGES = ("api", "mcp")

_CLAIM_REFERENCE = re.compile(
    r"\bClaim\b|revel\.db\.models\.profile|\b(?:FROM|JOIN|INTO|UPDATE)\s+claims\b",
    re.IGNORECASE,
)
_SERVICE_USE = re.compile(r"\b(?:visible_claims|owner_claims)\b")


def claim_reads_bypassing_service(root: Path, packages: tuple[str, ...]) -> list[Path]:
    offenders: list[Path] = []
    for package in packages:
        for path in sorted((root / package).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            # ``Claim`` (case-sensitive) or a raw SQL reference to the claims table.
            references = re.search(r"\bClaim\b", source) or _CLAIM_REFERENCE.search(source)
            if references and not _SERVICE_USE.search(source):
                offenders.append(path)
    return offenders


def test_adapters_read_claims_only_through_visibility_service() -> None:
    offenders = claim_reads_bypassing_service(SRC, GUARDED_PACKAGES)
    assert offenders == [], (
        "These modules reference claims without visible_claims/owner_claims "
        f"(revel.auth.visibility): {[str(p.relative_to(SRC)) for p in offenders]}"
    )


def test_guard_detects_bypass(tmp_path: Path) -> None:
    api = tmp_path / "api"
    api.mkdir()
    (api / "ok.py").write_text(
        "from revel.db.models import Claim\nfrom revel.auth.visibility import visible_claims\n"
    )
    (api / "bad.py").write_text("from revel.db.models import Claim\nselect(Claim)\n")
    (api / "raw.py").write_text('text("SELECT * FROM claims WHERE profile_id = :p")\n')
    (api / "unrelated.py").write_text("x = 'claimstub'\n")
    offenders = {p.name for p in claim_reads_bypassing_service(tmp_path, ("api",))}
    assert offenders == {"bad.py", "raw.py"}
