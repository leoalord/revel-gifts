"""F5 flows against a REAL OpenFGA server (model v2) and PostgreSQL: profile creation grants the
owner, the owner's agent contributes, another user's agent is denied, Public opt-in grants and
withdrawal revokes for a Public viewer. Needs ``OPENFGA_TEST_API_URL`` and
``REVEL_TEST_DATABASE_URL``."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from conftest import Person, World
from sqlalchemy.orm import Session

from revel.auth import fga_bootstrap
from revel.auth.authz import (
    AuthorizationDenied,
    ViewerTier,
    configure_authz,
    delegation_tuples,
    reset_authz,
)
from revel.auth.fga import OpenFgaServiceClient, create_fga_client
from revel.auth.identity import Principal
from revel.auth.visibility import visible_claims
from revel.db.enums import DelegationStatus
from revel.db.models import Agent, AgentDelegation, User
from revel.services import profiles
from revel.services.profiles import PolicyUpdate
from revel.settings import Settings

OPENFGA_TEST_API_URL = os.environ.get("OPENFGA_TEST_API_URL", "")
pytestmark = [pytest.mark.openfga, pytest.mark.anyio]


@pytest.fixture
def openfga_url() -> str:
    if not OPENFGA_TEST_API_URL:
        pytest.skip("OPENFGA_TEST_API_URL is not set")
    try:
        healthy = httpx.get(f"{OPENFGA_TEST_API_URL}/healthz", timeout=2).status_code == 200
    except httpx.HTTPError:
        healthy = False
    if not healthy:
        pytest.skip(f"OpenFGA not reachable at {OPENFGA_TEST_API_URL}")
    return OPENFGA_TEST_API_URL


@pytest.fixture
async def real_fga(openfga_url: str, world_unconfigured) -> AsyncIterator[OpenFgaServiceClient]:
    base = Settings(_env_file=None, openfga_api_url=openfga_url)
    result = await fga_bootstrap.bootstrap(base, store_name=f"revel-f5-test-{uuid4()}")
    settings = base.model_copy(
        update={
            "openfga_store_id": result.store_id,
            "openfga_authorization_model_id": result.authorization_model_id,
        }
    )
    client = create_fga_client(settings)
    configure_authz(client)
    try:
        yield client
    finally:
        reset_authz(client)
        try:
            await client.sdk.delete_store()
        finally:
            await client.close()


@pytest.fixture
def world_unconfigured(session: Session):
    """Users and delegations (rows only); tuples are written below through the real client."""
    agent = Agent(oauth_client_id=f"client_muse_{uuid4().hex[:6]}", name="Muse")
    session.add(agent)
    session.flush()
    people = {}
    for name in ("alex", "sam", "cal"):
        user = User(workos_user_id=f"user_{name}_{uuid4().hex[:6]}")
        session.add(user)
        session.flush()
        d = AgentDelegation(user_id=user.id, agent_id=agent.id, status=DelegationStatus.ACTIVE)
        session.add(d)
        session.flush()
        people[name] = Person(user=user, delegation=d)
    session.commit()
    return World(agent=agent, **people)


async def test_real_openfga_contribution_and_public_opt_in(
    session: Session, real_fga: OpenFgaServiceClient, world_unconfigured
) -> None:
    world = world_unconfigured
    for person in (world.alex, world.sam):
        await real_fga.write_tuples(delegation_tuples(person.user.id, person.delegation.id))
    alex = world.alex
    (r,) = (
        await profiles.add_facts(
            session,
            alex.mcp,
            [{"category": "interest", "statement": "Loves tea", "evidence_type": "explicit"}],
        )
    ).results
    assert r.outcome == "pending_review"  # no policy yet
    profile = await profiles.get_my_profile(session, alex.mcp)
    pid = profile.profile_id
    assert [f.statement for f in profile.facts] == ["Loves tea"]

    forged = Principal(
        user_id=alex.user.id,
        agent_id=world.sam.delegation.agent_id,
        delegation_id=world.sam.delegation.id,
        via="mcp",
    )
    with pytest.raises(AuthorizationDenied):
        await profiles.add_facts(
            session,
            forged,
            [{"category": "interest", "statement": "x", "evidence_type": "explicit"}],
        )

    await profiles.set_policy(session, alex.ui, PolicyUpdate(public_opt_in=True))
    await profiles.approve_fact(session, alex.ui, r.claim_id)  # Connections audience
    (pub,) = (
        await profiles.add_facts(
            session,
            alex.mcp,
            [{"category": "interest", "statement": "Collects stamps", "evidence_type": "explicit"}],
        )
    ).results
    assert (pub.outcome, pub.audience.value) == ("accepted", "public")
    sam = world.sam.mcp
    seen = await visible_claims(session, sam, pid)
    session.rollback()
    assert seen.tier is ViewerTier.public
    assert [c.statement for c in seen.claims] == ["Collects stamps"]

    await profiles.set_policy(
        session, alex.ui, PolicyUpdate(expected_version=1, public_opt_in=False)
    )
    with pytest.raises(AuthorizationDenied):
        await visible_claims(session, sam, pid)
    session.rollback()
