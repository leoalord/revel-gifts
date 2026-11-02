"""Model v2 against a real OpenFGA server (``openfga`` marker; see test_authz_openfga.py for the
compose command). Skipped unless ``OPENFGA_TEST_API_URL`` is set and reachable."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from auth.fakes import InMemoryIdentityRepository
from authz_fakes import Person, Principal, make_person
from mcp.server.auth.provider import AccessToken

from revel.auth import authz, fga_bootstrap
from revel.auth.agent_disconnect import disconnect_agent
from revel.auth.authz import (
    ViewerTier,
    configure_authz,
    delegation_tuples,
    owner_tuple,
    reset_authz,
    resolve_viewer_tier,
    tier_tuple,
)
from revel.auth.fga import OpenFgaServiceClient, RelationTuple, create_fga_client
from revel.auth.hooks import ModuleAuthzHooks
from revel.auth.identity import DelegationDisconnected, IdentityResolver
from revel.db.enums import ConnectionTier
from revel.settings import Settings

OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")
PROFILE_RELATIONS = ("contribute", "view_only_me", "view_trusted", "view_connections")
PROFILE_RELATIONS += ("view_public",)

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
async def store(openfga_url: str) -> AsyncIterator[Settings]:
    base = Settings(_env_file=None, openfga_api_url=openfga_url)
    result = await fga_bootstrap.bootstrap(base, store_name=f"revel-test-{uuid4()}")
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
    configure_authz(client)
    yield client
    reset_authz(client)
    await client.close()


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_authz()
    yield
    reset_authz()


async def connect(fga: OpenFgaServiceClient, person: Person) -> None:
    await fga.write_tuples(
        [
            owner_tuple(person.user_id, person.profile_id),
            *delegation_tuples(person.user_id, person.delegation_id),
        ]
    )


async def tiers(fga: OpenFgaServiceClient, person: Person, profile_id: UUID) -> set[ViewerTier]:
    """Tiers of the person and of the person's agent (equal unless inheritance breaks)."""
    return {
        await resolve_viewer_tier(fga, person.as_user(), profile_id),
        await resolve_viewer_tier(fga, person.as_agent(uuid4()), profile_id),
    }


async def test_tiers_blocks_revocation_and_public_on_real_server(
    fga: OpenFgaServiceClient,
) -> None:
    bob, tia, cal, sam = (make_person() for _ in range(4))
    for person in (bob, tia, cal, sam):
        await connect(fga, person)
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
    )
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=cal.user_id, tier="connections"
    )
    assert await tiers(fga, bob, bob.profile_id) == {ViewerTier.only_me}
    assert await tiers(fga, tia, bob.profile_id) == {ViewerTier.trusted_circle}
    assert await tiers(fga, cal, bob.profile_id) == {ViewerTier.connections}
    assert await tiers(fga, sam, bob.profile_id) == {ViewerTier.none}

    # Public opt-in reaches any user and any connected agent; withdrawal removes it.
    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)
    assert await tiers(fga, sam, bob.profile_id) == {ViewerTier.public}
    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=False)
    assert await tiers(fga, sam, bob.profile_id) == {ViewerTier.none}
    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)

    # Tier change, then revocation.
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="connections"
    )
    assert await tiers(fga, tia, bob.profile_id) == {ViewerTier.connections}
    await authz.on_connection_revoked(profile_id=bob.profile_id, viewer_user_id=cal.user_id)
    assert await tiers(fga, cal, bob.profile_id) == {ViewerTier.public}

    # Block: every tier including public, even with a stale tier tuple; directional.
    await authz.on_connection_granted(
        profile_id=tia.profile_id, viewer_user_id=bob.user_id, tier="trusted_circle"
    )
    await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=tia.user_id)
    await fga.write_tuples([tier_tuple(bob.profile_id, tia.user_id, ConnectionTier.TRUSTED_CIRCLE)])
    assert await tiers(fga, tia, bob.profile_id) == {ViewerTier.none}
    assert await tiers(fga, bob, tia.profile_id) == {ViewerTier.trusted_circle}
    await authz.on_unblocked(profile_id=bob.profile_id, blocked_user_id=tia.user_id)
    # The stale trusted tuple written after the block is back in effect once unblocked; F6
    # never writes one for a blocked pair (on_blocked deleted the real one).
    assert await tiers(fga, tia, bob.profile_id) == {ViewerTier.trusted_circle}

    # Owner blocking self is harmless.
    await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=bob.user_id)
    assert await tiers(fga, bob, bob.profile_id) == {ViewerTier.only_me}


async def test_question_relations_on_real_server(fga: OpenFgaServiceClient) -> None:
    bob, rae, sam = make_person(), make_person(), make_person()
    for person in (bob, rae, sam):
        await connect(fga, person)
    question = uuid4()
    await authz.on_question_created(
        question_id=question, profile_id=bob.profile_id, requester_user_id=rae.user_id
    )
    q = f"question:{question}"

    async def allowed(subject: str, relation: str) -> bool:
        return await fga.check(user=subject, relation=relation, object=q)

    bob_agent, rae_agent = f"agent:{bob.delegation_id}", f"agent:{rae.delegation_id}"
    assert await allowed(bob_agent, "respond")
    assert not await allowed(f"user:{bob.user_id}", "respond")
    assert not await allowed(rae_agent, "respond")
    for subject in (bob_agent, f"user:{bob.user_id}", rae_agent, f"user:{rae.user_id}"):
        assert await allowed(subject, "read_answer")
    for subject in (f"agent:{sam.delegation_id}", f"user:{sam.user_id}"):
        assert not await allowed(subject, "read_answer")
        assert not await allowed(subject, "respond")
    # A block on the requester stops requester-side answer reads.
    await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=rae.user_id)
    assert not await allowed(rae_agent, "read_answer")
    assert not await allowed(f"user:{rae.user_id}", "read_answer")
    assert await allowed(bob_agent, "read_answer")


def token(sub: str) -> AccessToken:
    return AccessToken(
        token="opaque", client_id="client_muse", scopes=[], subject=sub, claims={"iss": "x"}
    )


async def test_disconnect_agent_denies_every_relation_and_old_token_cannot_regrant(
    fga: OpenFgaServiceClient,
) -> None:
    resolver = IdentityResolver(InMemoryIdentityRepository(), hooks=ModuleAuthzHooks())
    alex = await resolver.resolve(token("user_alex"), "mcp")
    bob = await resolver.resolve(token("user_bob"), "mcp")
    assert alex.delegation_id is not None and bob.delegation_id is not None
    alex_profile, bob_profile = uuid4(), uuid4()
    await fga.write_tuples(
        [owner_tuple(alex.user_id, alex_profile), owner_tuple(bob.user_id, bob_profile)]
    )
    await authz.on_connection_granted(
        profile_id=bob_profile, viewer_user_id=alex.user_id, tier="trusted_circle"
    )
    await authz.on_public_opt_in_changed(profile_id=bob_profile, enabled=True)
    asked, about_alex = uuid4(), uuid4()
    await authz.on_question_created(
        question_id=asked, profile_id=bob_profile, requester_user_id=alex.user_id
    )
    await authz.on_question_created(
        question_id=about_alex, profile_id=alex_profile, requester_user_id=bob.user_id
    )
    agent = f"agent:{alex.delegation_id}"
    checks = [(r, f"profile:{alex_profile}") for r in PROFILE_RELATIONS]
    checks += [(r, f"profile:{bob_profile}") for r in PROFILE_RELATIONS[2:]]
    checks += [("read_answer", f"question:{asked}"), ("respond", f"question:{about_alex}")]
    checks += [("read_answer", f"question:{about_alex}")]
    for relation, obj in checks:
        assert await fga.check(user=agent, relation=relation, object=obj), (relation, obj)

    await disconnect_agent(alex, alex.delegation_id, resolver=resolver)

    for relation, obj in checks:
        assert not await fga.check(user=agent, relation=relation, object=obj), (relation, obj)
    assert await resolve_viewer_tier(fga, alex, bob_profile) is ViewerTier.none

    async def agent_tuples() -> list[RelationTuple]:
        return [
            t.tuple
            for t in await fga.read_stored_tuples()
            if agent in (t.tuple.user, t.tuple.object)
        ]

    assert await agent_tuples() == []
    # The still-valid old token: F2's upsert finds the disconnected row and rejects it.
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(token("user_alex"), "mcp")
    assert await agent_tuples() == []
    for relation, obj in checks:
        assert not await fga.check(user=agent, relation=relation, object=obj), (relation, obj)
    # The human keeps their own access; Bob's agent is unaffected.
    alex_ui = Principal(user_id=alex.user_id, via="ui")
    assert await resolve_viewer_tier(fga, alex_ui, bob_profile) is ViewerTier.trusted_circle
    assert await fga.check(
        user=f"agent:{bob.delegation_id}", relation="respond", object=f"question:{asked}"
    )


def test_bootstrap_writes_new_version_into_existing_store_and_prints_it(
    openfga_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F1's deploy path: OPENFGA_STORE_ID set and a changed model -> a new version, printed."""
    base = Settings(_env_file=None, openfga_api_url=openfga_url)
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: base)
    assert fga_bootstrap.main(["--store-name", f"revel-test-{uuid4()}"]) == 0
    first = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
    existing = base.model_copy(update={"openfga_store_id": first["OPENFGA_STORE_ID"]})
    monkeypatch.setattr(fga_bootstrap, "get_settings", lambda: existing)
    model = fga_bootstrap.load_model(fga_bootstrap.MODEL_JSON_PATH)
    model["type_definitions"].append({"type": "scratch"})
    changed = tmp_path / "model.json"
    changed.write_text(json.dumps(model))
    try:
        assert fga_bootstrap.main(["--model", str(changed)]) == 0
        out = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
        assert out["OPENFGA_STORE_ID"] == first["OPENFGA_STORE_ID"]
        assert out["OPENFGA_AUTHORIZATION_MODEL_ID"] != first["OPENFGA_AUTHORIZATION_MODEL_ID"]
        # Rerunning with the same model writes nothing and prints the same new ID.
        assert fga_bootstrap.main(["--model", str(changed)]) == 0
        again = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines())
        assert again == out
    finally:

        async def delete_store() -> None:
            client = create_fga_client(existing)
            try:
                await client.sdk.delete_store()
            finally:
                await client.close()

        asyncio.run(delete_store())
