"""F7's question checks against a real OpenFGA server (``openfga`` marker; see
test_authz_openfga.py for the compose command). Skipped unless ``OPENFGA_TEST_API_URL`` is set
and reachable. Runs the same matrix as the fake tests in ``test_question_checks.py``."""

import os
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import httpx
import pytest
from authz_fakes import make_person
from question_matrix import connect, run_question_matrix

from revel.auth import fga_bootstrap
from revel.auth.authz import (
    TupleHooks,
    ViewerTier,
    can_release_answer,
    can_respond,
    configure_authz,
    reset_authz,
)
from revel.auth.fga import OpenFgaServiceClient, create_fga_client
from revel.settings import Settings

OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")

pytestmark = [pytest.mark.openfga, pytest.mark.anyio]


@pytest.fixture(scope="module")
def openfga_url() -> str:
    if not OPENFGA_TEST_API_URL:
        pytest.skip("OPENFGA_TEST_API_URL is not set (see test_authz_openfga.py)")
    try:
        healthy = httpx.get(f"{OPENFGA_TEST_API_URL}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        healthy = False
    if not healthy:
        pytest.skip(f"OpenFGA not reachable at {OPENFGA_TEST_API_URL}")
    return OPENFGA_TEST_API_URL


@pytest.fixture
async def fga(openfga_url: str) -> AsyncIterator[OpenFgaServiceClient]:
    base = Settings(_env_file=None, openfga_api_url=openfga_url)
    result = await fga_bootstrap.bootstrap(base, store_name=f"revel-test-{uuid4()}")
    settings = base.model_copy(
        update={
            "openfga_store_id": result.store_id,
            "openfga_authorization_model_id": result.authorization_model_id,
        }
    )
    client = create_fga_client(settings)
    try:
        yield client
        await client.sdk.delete_store()
    finally:
        await client.close()


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_authz()
    yield
    reset_authz()


async def test_question_matrix_on_real_server(fga: OpenFgaServiceClient) -> None:
    await run_question_matrix(fga)


async def test_configured_client_on_real_server(fga: OpenFgaServiceClient) -> None:
    """``fga=None`` uses the client installed by ``configure_authz``."""
    bob, rae = make_person(), make_person()
    for person in (bob, rae):
        await connect(fga, person)
    question = uuid4()
    hooks = TupleHooks(fga)
    await hooks.on_question_created(
        question_id=question, profile_id=bob.profile_id, requester_user_id=rae.user_id
    )
    await hooks.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=rae.user_id, tier="trusted_circle"
    )
    configure_authz(fga)
    assert await can_respond(bob.as_agent(uuid4()), question)
    tier = await can_release_answer(rae.as_user(), question, bob.profile_id)
    assert tier is ViewerTier.trusted_circle
    assert await can_release_answer(rae.as_user(), question, rae.profile_id) is None
