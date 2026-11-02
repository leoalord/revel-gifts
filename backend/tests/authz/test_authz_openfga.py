"""Integration tests against a real OpenFGA server (``openfga`` marker).

Start one (isolated project and ports so concurrent stacks do not collide), then run::

    POSTGRES_HOST_PORT=55480 OPENFGA_HTTP_HOST_PORT=18480 OPENFGA_GRPC_HOST_PORT=18481 \\
        docker compose -p revel-f4 up -d openfga
    OPENFGA_TEST_API_URL=http://localhost:18480 uv run pytest -m openfga
    docker compose -p revel-f4 down -v

Each test bootstraps a fresh, uniquely named store and deletes it afterwards. Skipped with a
message when ``OPENFGA_TEST_API_URL`` is unset (there is no default port) or unreachable.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from authz_fakes import make_person

from revel.auth import fga_bootstrap
from revel.auth.authz import (
    TupleHooks,
    ViewerTier,
    acts_for_tuple,
    can_contribute,
    configure_authz,
    on_agent_disconnected,
    reconcile_agent_tuples,
    reconcile_owner_tuples,
    reset_authz,
    resolve_viewer_tier,
)
from revel.auth.fga import OpenFgaServiceClient, RelationTuple, create_fga_client
from revel.settings import Settings

OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")

pytestmark = [pytest.mark.openfga, pytest.mark.anyio]


def _reachable(url: str) -> bool:
    try:
        return httpx.get(f"{url}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="module")
def openfga_url() -> str:
    if not OPENFGA_TEST_API_URL:
        pytest.skip("OPENFGA_TEST_API_URL is not set (see this module's docstring)")
    if not _reachable(OPENFGA_TEST_API_URL):
        pytest.skip(
            f"OpenFGA not reachable at {OPENFGA_TEST_API_URL}; start it with docker compose and "
            "set OPENFGA_TEST_API_URL (see this module's docstring)"
        )
    return OPENFGA_TEST_API_URL


@pytest.fixture
async def store(openfga_url: str) -> AsyncIterator[Settings]:
    base = Settings(_env_file=None, openfga_api_url=openfga_url)
    name = f"revel-test-{uuid4()}"
    result = await fga_bootstrap.bootstrap(base, store_name=name)
    assert result.store_created and result.model_written
    settings = base.model_copy(
        update={
            "openfga_store_id": result.store_id,
            "openfga_authorization_model_id": result.authorization_model_id,
        }
    )
    yield settings
    client = create_fga_client(settings)
    try:
        await client.sdk.delete_store()
    finally:
        await client.close()


@pytest.fixture
async def fga(store: Settings) -> AsyncIterator[OpenFgaServiceClient]:
    client = create_fga_client(store)
    yield client
    await client.close()


async def test_bootstrap_is_idempotent(store: Settings) -> None:
    client = create_fga_client(store)
    try:
        name = (await client.sdk.get_store()).name
    finally:
        await client.close()
    again = await fga_bootstrap.bootstrap(
        store.model_copy(update={"openfga_store_id": None}), store_name=name
    )
    assert again.store_id == store.openfga_store_id
    assert not again.store_created
    assert not again.model_written
    assert again.authorization_model_id == store.openfga_authorization_model_id


async def test_bootstrap_writes_new_version_when_model_changes(
    store: Settings, tmp_path: Path
) -> None:
    model = fga_bootstrap.load_model(fga_bootstrap.MODEL_JSON_PATH)
    model["type_definitions"].append({"type": "scratch"})
    changed = tmp_path / "model.json"
    changed.write_text(json.dumps(model))
    result = await fga_bootstrap.bootstrap(store, model_path=changed)
    assert result.model_written
    assert result.authorization_model_id != store.openfga_authorization_model_id


async def test_hooks_owner_agent_and_cross_user_isolation(fga: OpenFgaServiceClient) -> None:
    alex, sam = make_person(), make_person()
    hooks = TupleHooks(fga)
    shared_client = uuid4()  # one global agents row for both users' Muse
    for person in (alex, sam):
        await reconcile_owner_tuples(fga, [(person.user_id, person.profile_id)])
        # Twice: duplicate writes must be ignored by the real server.
        for _ in range(2):
            await hooks.on_user_created(user_id=person.user_id)
            await hooks.on_agent_delegated(
                user_id=person.user_id, agent_id=shared_client, delegation_id=person.delegation_id
            )

    alex_muse, sams_muse = alex.as_agent(shared_client), sam.as_agent(shared_client)
    assert await can_contribute(fga, alex.as_user(), alex.profile_id)
    assert await can_contribute(fga, alex_muse, alex.profile_id)
    assert await resolve_viewer_tier(fga, alex_muse, alex.profile_id) is ViewerTier.only_me

    # Same OAuth client, different user: denied.
    assert not await can_contribute(fga, sams_muse, alex.profile_id)
    assert await resolve_viewer_tier(fga, sams_muse, alex.profile_id) is ViewerTier.none
    assert not await can_contribute(fga, sam.as_user(), alex.profile_id)
    assert await can_contribute(fga, sams_muse, sam.profile_id)

    # Disconnect: alex's agent is denied, alex and sam's agent are unaffected; retry is safe.
    for _ in range(2):
        await hooks.on_agent_disconnected(
            user_id=alex.user_id, delegation_id=alex.delegation_id, agent_id=shared_client
        )
    assert not await can_contribute(fga, alex_muse, alex.profile_id)
    assert await resolve_viewer_tier(fga, alex_muse, alex.profile_id) is ViewerTier.none
    assert await can_contribute(fga, alex.as_user(), alex.profile_id)
    assert await can_contribute(fga, sams_muse, sam.profile_id)
    delegate = await fga.read_tuples(
        user=f"agent:{alex.delegation_id}", relation="delegate", object="user:"
    )
    assert delegate == []


async def test_delegation_before_profile_then_owner_tuple(fga: OpenFgaServiceClient) -> None:
    alex = make_person()
    hooks = TupleHooks(fga)
    await hooks.on_user_created(user_id=alex.user_id)
    await hooks.on_agent_delegated(
        user_id=alex.user_id, agent_id=uuid4(), delegation_id=alex.delegation_id
    )
    agent = alex.as_agent(uuid4())
    assert not await can_contribute(fga, agent, alex.profile_id)

    # Only the owner tuple is written at profile creation; owner_agent is computed.
    await reconcile_owner_tuples(fga, [(alex.user_id, alex.profile_id)])
    assert await can_contribute(fga, alex.as_user(), alex.profile_id)
    assert await can_contribute(fga, agent, alex.profile_id)

    await hooks.on_agent_disconnected(user_id=alex.user_id, delegation_id=alex.delegation_id)
    assert not await can_contribute(fga, agent, alex.profile_id)


async def test_owner_agent_cannot_be_written(fga: OpenFgaServiceClient) -> None:
    alex = make_person()
    with pytest.raises(Exception):  # noqa: B017 - the server rejects the type restriction
        await fga.write_tuples(
            [
                RelationTuple(
                    f"agent:{alex.delegation_id}", "owner_agent", f"profile:{alex.profile_id}"
                )
            ]
        )


async def test_stored_tuples_carry_write_times(fga: OpenFgaServiceClient) -> None:
    alex = make_person()
    await reconcile_agent_tuples(fga, [(alex.user_id, alex.delegation_id)])
    stored = await fga.read_stored_tuples()
    assert len(stored) == 2
    assert all(s.written_at.tzinfo is not None for s in stored)


async def test_check_probe_healthy_and_missing_model(store: Settings) -> None:
    assert await fga_bootstrap.check(store) == "healthy"
    missing = store.model_copy(
        update={"openfga_authorization_model_id": "01M6DFE8C5TDTSBCGBA2HK4M0Q"}
    )
    with pytest.raises(Exception):  # noqa: B017 - any SDK error means unhealthy
        await fga_bootstrap.check(missing)


def test_bootstrap_cli_stdout_against_server(
    openfga_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(_env_file=None, openfga_api_url=openfga_url)
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: settings)
    assert fga_bootstrap.main(["--store-name", f"revel-test-{uuid4()}"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split("=")[0] for line in lines] == [
        "OPENFGA_STORE_ID",
        "OPENFGA_AUTHORIZATION_MODEL_ID",
    ]
    configured = settings.model_copy(
        update={
            "openfga_store_id": lines[0].split("=", 1)[1],
            "openfga_authorization_model_id": lines[1].split("=", 1)[1],
        }
    )
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: configured)
    try:
        assert fga_bootstrap.main(["--check"]) == 0
        assert capsys.readouterr().out.strip() == "healthy"
    finally:

        async def delete_store() -> None:
            client = create_fga_client(configured)
            try:
                await client.sdk.delete_store()
            finally:
                await client.close()

        asyncio.run(delete_store())


async def test_disconnect_revokes_every_acts_for_on_the_delegation(
    fga: OpenFgaServiceClient,
) -> None:
    alex, other = make_person(), make_person()
    configure_authz(fga)
    try:
        await reconcile_owner_tuples(fga, [(alex.user_id, alex.profile_id)])
        await reconcile_agent_tuples(fga, [(alex.user_id, alex.delegation_id)])
        await fga.write_tuples([acts_for_tuple(other.user_id, alex.delegation_id)])
        await on_agent_disconnected(user_id=alex.user_id, delegation_id=alex.delegation_id)
        remaining = await fga.read_tuples(relation="acts_for", object=f"agent:{alex.delegation_id}")
        assert remaining == []
        assert not await can_contribute(fga, alex.as_agent(uuid4()), alex.profile_id)
    finally:
        reset_authz()
