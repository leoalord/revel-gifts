"""Unit tests for revel.auth.authz against the in-memory model-v1 fake (delegate model)."""

from uuid import uuid4

import pytest
from authz_fakes import FakeFga, Principal, make_person

from revel.auth.authz import (
    AuthorizationDenied,
    TupleHooks,
    ViewerTier,
    acts_for_tuple,
    can_contribute,
    delegate_tuple,
    owner_tuple,
    reconcile_agent_tuples,
    reconcile_owner_tuples,
    require_contribute,
    require_viewer,
    resolve_viewer_tier,
    subject_for,
)

pytestmark = pytest.mark.anyio


# --- Subject mapping --------------------------------------------------------------------------


def test_subject_is_delegation_when_active() -> None:
    user_id, agent_id, delegation_id = uuid4(), uuid4(), uuid4()
    principal = Principal(
        user_id=user_id, agent_id=agent_id, delegation_id=delegation_id, via="mcp"
    )
    assert subject_for(principal) == f"agent:{delegation_id}"
    # Never the global agents.id.
    assert str(agent_id) not in subject_for(principal)


def test_subject_is_user_without_delegation() -> None:
    user_id = uuid4()
    assert subject_for(Principal(user_id=user_id)) == f"user:{user_id}"


@pytest.mark.parametrize("field", ["agent_id", "delegation_id"])
def test_inconsistent_principal_is_denied(field: str) -> None:
    principal = Principal(user_id=uuid4(), **{field: uuid4()})
    with pytest.raises(AuthorizationDenied):
        subject_for(principal)


def test_viewer_tier_values_are_forward_compatible() -> None:
    assert [t.value for t in ViewerTier] == [
        "none",
        "public",
        "connections",
        "trusted_circle",
        "only_me",
    ]


# --- Hooks ------------------------------------------------------------------------------------


async def test_user_created_writes_nothing(fake_fga: FakeFga) -> None:
    result = await TupleHooks(fake_fga).on_user_created(user_id=uuid4())
    assert result.written == ()
    assert fake_fga.tuples == set()
    assert fake_fga.calls == []


async def test_agent_delegated_writes_acts_for_and_delegate_idempotently(
    fake_fga: FakeFga,
) -> None:
    alex = make_person()
    agent_id = uuid4()
    hooks = TupleHooks(fake_fga)
    for _ in range(2):
        await hooks.on_agent_delegated(
            user_id=alex.user_id, agent_id=agent_id, delegation_id=alex.delegation_id
        )
    assert fake_fga.tuples == {
        acts_for_tuple(alex.user_id, alex.delegation_id),
        delegate_tuple(alex.user_id, alex.delegation_id),
    }
    assert all(str(agent_id) not in t.user + t.object for t in fake_fga.tuples)


async def test_delegation_before_profile_then_owner_grants_agent(fake_fga: FakeFga) -> None:
    alex = make_person()
    agent = alex.as_agent(uuid4())
    await TupleHooks(fake_fga).on_agent_delegated(
        user_id=alex.user_id, agent_id=uuid4(), delegation_id=alex.delegation_id
    )
    assert not await can_contribute(fake_fga, agent, alex.profile_id)
    # Profile creation writes only the owner tuple; owner_agent follows via delegate.
    await reconcile_owner_tuples(fake_fga, [(alex.user_id, alex.profile_id)])
    assert await can_contribute(fake_fga, agent, alex.profile_id)
    assert await can_contribute(fake_fga, alex.as_user(), alex.profile_id)


async def test_acts_for_alone_grants_nothing(fake_fga: FakeFga) -> None:
    alex = make_person()
    await reconcile_owner_tuples(fake_fga, [(alex.user_id, alex.profile_id)])
    await fake_fga.write_tuples([acts_for_tuple(alex.user_id, alex.delegation_id)])
    assert not await can_contribute(fake_fga, alex.as_agent(uuid4()), alex.profile_id)


async def test_reconcile_backfills(fake_fga: FakeFga) -> None:
    alex = make_person()
    pairs = [(alex.user_id, alex.delegation_id)]
    assert await reconcile_owner_tuples(fake_fga, [(alex.user_id, alex.profile_id)]) == 1
    assert await reconcile_agent_tuples(fake_fga, pairs) == 2
    assert await reconcile_agent_tuples(fake_fga, pairs) == 2  # rerun is harmless
    assert await can_contribute(fake_fga, alex.as_agent(uuid4()), alex.profile_id)


async def test_disconnect_deletes_acts_for_and_delegate(fake_fga: FakeFga) -> None:
    alex = make_person()
    agent_id = uuid4()
    hooks = TupleHooks(fake_fga)
    await reconcile_owner_tuples(fake_fga, [(alex.user_id, alex.profile_id)])
    await hooks.on_agent_delegated(
        user_id=alex.user_id, agent_id=agent_id, delegation_id=alex.delegation_id
    )
    assert await can_contribute(fake_fga, alex.as_agent(agent_id), alex.profile_id)

    result = await hooks.on_agent_disconnected(
        user_id=alex.user_id, delegation_id=alex.delegation_id, agent_id=agent_id
    )
    assert set(result.deleted) == {
        acts_for_tuple(alex.user_id, alex.delegation_id),
        delegate_tuple(alex.user_id, alex.delegation_id),
    }
    assert fake_fga.tuples == {owner_tuple(alex.user_id, alex.profile_id)}
    assert not await can_contribute(fake_fga, alex.as_agent(agent_id), alex.profile_id)
    assert await can_contribute(fake_fga, alex.as_user(), alex.profile_id)
    # Retrying the disconnect (missing tuples) is safe.
    await hooks.on_agent_disconnected(user_id=alex.user_id, delegation_id=alex.delegation_id)


async def test_disconnect_leaves_other_delegations(fake_fga: FakeFga) -> None:
    alex, sam = make_person(), make_person()
    hooks = TupleHooks(fake_fga)
    shared_client = uuid4()  # same OAuth client (agents.id) for both users
    for person in (alex, sam):
        await reconcile_owner_tuples(fake_fga, [(person.user_id, person.profile_id)])
        await hooks.on_agent_delegated(
            user_id=person.user_id, agent_id=shared_client, delegation_id=person.delegation_id
        )
    await hooks.on_agent_disconnected(user_id=alex.user_id, delegation_id=alex.delegation_id)
    assert await can_contribute(fake_fga, sam.as_agent(shared_client), sam.profile_id)
    assert not await can_contribute(fake_fga, alex.as_agent(shared_client), alex.profile_id)


# --- Checks and tiers -------------------------------------------------------------------------


async def test_cross_user_same_client_is_denied(fake_fga: FakeFga) -> None:
    alex, sam = make_person(), make_person()
    hooks = TupleHooks(fake_fga)
    shared_client = uuid4()
    for person in (alex, sam):
        await reconcile_owner_tuples(fake_fga, [(person.user_id, person.profile_id)])
        await hooks.on_agent_delegated(
            user_id=person.user_id, agent_id=shared_client, delegation_id=person.delegation_id
        )
    sams_muse = sam.as_agent(shared_client)
    assert not await can_contribute(fake_fga, sams_muse, alex.profile_id)
    assert await resolve_viewer_tier(fake_fga, sams_muse, alex.profile_id) is ViewerTier.none
    assert not await can_contribute(fake_fga, sam.as_user(), alex.profile_id)
    with pytest.raises(AuthorizationDenied):
        await require_contribute(fake_fga, sams_muse, alex.profile_id)


async def test_resolve_viewer_tier_uses_one_call(fake_fga: FakeFga) -> None:
    alex = make_person()
    await reconcile_owner_tuples(fake_fga, [(alex.user_id, alex.profile_id)])
    fake_fga.calls.clear()
    assert (
        await resolve_viewer_tier(fake_fga, alex.as_user(), alex.profile_id) is ViewerTier.only_me
    )
    assert fake_fga.calls == ["check"]
    fake_fga.calls.clear()
    assert await resolve_viewer_tier(fake_fga, alex.as_user(), uuid4()) is ViewerTier.none
    assert fake_fga.calls == ["check"]


async def test_require_helpers(fake_fga: FakeFga) -> None:
    alex = make_person()
    await reconcile_owner_tuples(fake_fga, [(alex.user_id, alex.profile_id)])
    await require_contribute(fake_fga, alex.as_user(), alex.profile_id)
    assert await require_viewer(fake_fga, alex.as_user(), alex.profile_id) is ViewerTier.only_me
    stranger = Principal(user_id=uuid4())
    with pytest.raises(AuthorizationDenied) as denied:
        await require_viewer(fake_fga, stranger, alex.profile_id)
    # No detail about the object, subject or reason leaks through the exception.
    assert str(denied.value) == "Not authorized"
    assert str(alex.profile_id) not in repr(denied.value)
