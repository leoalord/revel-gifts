"""F2 -> F4 integration: the real IdentityResolver and hook bridge driving revel.auth.authz.

Uses F2's in-memory identity repository from ``tests/auth/conftest.py`` (imported, not copied;
``tests/`` is on ``sys.path`` via the root conftest) and the F4 in-memory OpenFGA fake.
"""

import logging
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from auth.conftest import InMemoryIdentityRepository
from authz_fakes import FakeFga
from mcp.server.auth.provider import AccessToken

from revel.auth.authz import (
    PrincipalLike,
    ViewerTier,
    acts_for_tuple,
    can_contribute,
    configure_authz,
    owner_agent_tuple,
    owner_tuple,
    reset_authz,
    resolve_viewer_tier,
    subject_for,
)
from revel.auth.hooks import ModuleAuthzHooks
from revel.auth.identity import IdentityResolver, Principal

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_authz()
    yield
    reset_authz()


def token(sub: str, client_id: str) -> AccessToken:
    return AccessToken(
        token="opaque", client_id=client_id, scopes=[], subject=sub, claims={"iss": "x"}
    )


async def test_resolver_writes_tuples_through_module_hooks(fake_fga: FakeFga) -> None:
    profile_id = uuid4()
    repo = InMemoryIdentityRepository()

    async def resolve_profile(user_id: UUID) -> UUID | None:
        return profile_id

    configure_authz(fake_fga, resolve_profile)
    resolver = IdentityResolver(repo, hooks=ModuleAuthzHooks())

    principal = await resolver.resolve(token("user_alex", "client_muse"), "mcp")
    assert principal.delegation_id is not None
    assert fake_fga.tuples == {
        owner_tuple(principal.user_id, profile_id),
        acts_for_tuple(principal.user_id, principal.delegation_id),
        owner_agent_tuple(principal.delegation_id, profile_id),
    }
    assert await can_contribute(fake_fga, principal, profile_id)
    assert await resolve_viewer_tier(fake_fga, principal, profile_id) is ViewerTier.only_me

    # A second sign-in inserts nothing, so no hooks run and the tuples are unchanged.
    before = set(fake_fga.tuples)
    fake_fga.calls.clear()
    again = await resolver.resolve(token("user_alex", "client_muse"), "mcp")
    assert again == principal
    assert fake_fga.calls == []
    assert fake_fga.tuples == before


async def test_same_client_second_user_is_isolated(fake_fga: FakeFga) -> None:
    profiles: dict[UUID, UUID] = {}

    async def resolve_profile(user_id: UUID) -> UUID | None:
        return profiles.setdefault(user_id, uuid4())

    configure_authz(fake_fga, resolve_profile)
    resolver = IdentityResolver(InMemoryIdentityRepository(), hooks=ModuleAuthzHooks())
    alex = await resolver.resolve(token("user_alex", "client_muse"), "mcp")
    sam = await resolver.resolve(token("user_sam", "client_muse"), "mcp")
    assert alex.agent_id == sam.agent_id  # one global agents row per OAuth client
    assert alex.delegation_id != sam.delegation_id
    assert not await can_contribute(fake_fga, sam, profiles[alex.user_id])


async def test_sign_in_succeeds_with_warning_when_unconfigured(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # configure_authz never called: grant hooks warn and return, sign-in proceeds.
    caplog.set_level(logging.WARNING, logger="revel.auth.authz")
    repo = InMemoryIdentityRepository()
    resolver = IdentityResolver(repo, hooks=ModuleAuthzHooks())
    principal = await resolver.resolve(token("user_alex", "client_muse"), "mcp")
    assert principal.user_id in repo.users
    assert principal.delegation_id in repo.delegations
    assert "OpenFGA is not configured" in caplog.text


async def test_sign_in_rolls_back_when_configured_fga_errors(fake_fga: FakeFga) -> None:
    async def resolve_profile(user_id: UUID) -> UUID | None:
        return uuid4()

    configure_authz(fake_fga, resolve_profile)
    fake_fga.error = RuntimeError("openfga unavailable")
    repo = InMemoryIdentityRepository()
    resolver = IdentityResolver(repo, hooks=ModuleAuthzHooks())
    with pytest.raises(RuntimeError):
        await resolver.resolve(token("user_alex", "client_muse"), "mcp")
    assert repo.users == {}


def test_real_principal_satisfies_principal_like() -> None:
    user_id, agent_id, delegation_id = uuid4(), uuid4(), uuid4()
    ui: PrincipalLike = Principal(user_id=user_id, agent_id=None, delegation_id=None, via="ui")
    mcp: PrincipalLike = Principal(
        user_id=user_id, agent_id=agent_id, delegation_id=delegation_id, via="mcp"
    )
    assert subject_for(ui) == f"user:{user_id}"
    assert subject_for(mcp) == f"agent:{delegation_id}"
