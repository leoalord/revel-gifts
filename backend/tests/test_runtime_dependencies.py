"""Guard runtime dependencies that the production (no-dev) image needs.

``revel.auth.fga._fetch_google_id_token`` lazily imports Google's urllib3
transport and calls ``google.oauth2.id_token.fetch_id_token``. That needs two
google-auth extras:

- ``urllib3``: the urllib3 transport imports ``packaging`` and ``urllib3``.
- ``requests``: on Cloud Run, ``fetch_id_token_credentials`` imports
  ``google.auth.compute_engine``, whose ``_metadata``/``_mtls`` modules import
  ``requests`` unconditionally. ``fetch_id_token_credentials`` swallows that
  ImportError and raises ``DefaultCredentialsError`` ("Neither metadata server
  or valid service account credentials are found"), hiding the real cause.

Dev dependencies can supply some of these packages, so an import check alone
may pass locally even when the production lock lacks an extra; the metadata
check catches that case.
"""

import importlib
from importlib.metadata import requires

import pytest
from packaging.requirements import Requirement

REQUIRED_GOOGLE_AUTH_EXTRAS = {"requests", "urllib3"}


@pytest.mark.parametrize(
    "module",
    [
        "google.auth.transport.urllib3",
        "google.oauth2.id_token",
        "urllib3",
        "google.auth.compute_engine",
        "google.auth.compute_engine._metadata",
    ],
)
def test_openfga_id_token_transport_imports(module: str) -> None:
    importlib.import_module(module)


def test_google_auth_declares_required_extras() -> None:
    declared = [Requirement(r) for r in requires("revel") or []]
    google_auth = [r for r in declared if r.name == "google-auth"]
    assert google_auth, "revel must depend on google-auth"
    missing = REQUIRED_GOOGLE_AUTH_EXTRAS - google_auth[0].extras
    assert not missing, (
        f"google-auth must be installed with extras {sorted(missing)}; the production "
        "image otherwise cannot import the ID-token transport or reach the Cloud Run "
        "metadata server"
    )
