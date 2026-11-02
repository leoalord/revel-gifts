"""Guard runtime dependencies that the production (no-dev) image needs.

``revel.auth.fga._fetch_google_id_token`` lazily imports Google's urllib3
transport. That transport needs ``packaging`` and ``urllib3``, which come
from the ``google-auth[urllib3]`` extra. Dev dependencies happen to supply
``packaging``, so an import check alone would pass locally even when the
production lock lacks the extra; the metadata check catches that case.
"""

import importlib
from importlib.metadata import requires

import pytest
from packaging.requirements import Requirement


@pytest.mark.parametrize(
    "module",
    ["google.auth.transport.urllib3", "google.oauth2.id_token", "urllib3"],
)
def test_openfga_id_token_transport_imports(module: str) -> None:
    importlib.import_module(module)


def test_google_auth_declares_urllib3_extra() -> None:
    declared = [Requirement(r) for r in requires("revel") or []]
    google_auth = [r for r in declared if r.name == "google-auth"]
    assert google_auth, "revel must depend on google-auth"
    assert "urllib3" in google_auth[0].extras, (
        "google-auth must be installed with the [urllib3] extra; the production "
        "image otherwise lacks packaging and fails to import the ID-token transport"
    )
