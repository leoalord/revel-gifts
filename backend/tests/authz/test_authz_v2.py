"""Model v2 service behavior with the in-memory fake: tiers, v2 hooks and ``disconnect_agent``.

The real model is checked by ``authorization/tests`` (CLI) and ``test_authz_v2_openfga.py``.
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import anyio
import pytest
from auth.fakes import InMemoryIdentityRepository
from authz_fakes import FakeFga, Person, Principal, make_person
from mcp.server.auth.provider import AccessToken

from revel.auth import authz
from revel.auth.agent_disconnect import disconnect_agent
from revel.auth.authz import (
    AuthorizationDenied,
    ViewerTier,
    blocked_tuple,
    configure_authz,
    delegation_tuples,
    owner_tuple,
    public_tuples,
    question_tuples,
    reset_authz,
    resolve_viewer_tier,
    tier_tuple,
)
from revel.auth.fga import FgaConfigurationError, RelationTuple
from revel.auth.hooks import ModuleAuthzHooks
from revel.auth.identity import DelegationDisconnected, IdentityResolver
from revel.db.enums import ConnectionTier

pytestmark = pytest.mark.anyio

V2_HOOKS = (
    "on_connection_granted",
    "on_connection_revoked",
    "on_blocked",
    "on_unblocked",
    "on_public_opt_in_changed",
    "on_question_created",
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_authz()
    yield
    reset_authz()


def connected(fga: FakeFga, person: Person) -> None:
    """Owner tuple plus the agent connect tuples."""
    fga.tuples |= {owner_tuple(person.user_id, person.profile_id)}
    fga.tuples |= set(delegation_tuples(person.user_id, person.delegation_id))


async def tiers(fga: FakeFga, person: Person, profile_id: UUID) -> tuple[ViewerTier, ViewerTier]:
    """Tier of the person (UI) and of the person's agent on ``profile_id``."""
    return (
        await resolve_viewer_tier(fga, person.as_user(), profile_id),
        await resolve_viewer_tier(fga, person.as_agent(uuid4()), profile_id),
    )


# --- Tiers ------------------------------------------------------------------------------------


async def test_tier_hierarchy_and_agent_inheritance(fake_fga: FakeFga) -> None:
    bob, tia, cal, sam = (make_person() for _ in range(4))
    for person in (bob, tia, cal, sam):
        connected(fake_fga, person)
    configure_authz(fake_fga)
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
    )
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=cal.user_id, tier=ConnectionTier.CONNECTIONS
    )
    assert await tiers(fake_fga, bob, bob.profile_id) == (ViewerTier.only_me,) * 2
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.trusted_circle,) * 2
    assert await tiers(fake_fga, cal, bob.profile_id) == (ViewerTier.connections,) * 2
    assert await tiers(fake_fga, sam, bob.profile_id) == (ViewerTier.none,) * 2
    # Directional: Bob holds nothing on Tia's profile.
    assert await tiers(fake_fga, bob, tia.profile_id) == (ViewerTier.none,) * 2

    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)
    assert await tiers(fake_fga, sam, bob.profile_id) == (ViewerTier.public,) * 2
    assert await tiers(fake_fga, cal, bob.profile_id) == (ViewerTier.connections,) * 2


async def test_tier_change_is_one_call_and_leaves_one_tier(fake_fga: FakeFga) -> None:
    bob, tia = make_person(), make_person()
    connected(fake_fga, bob)
    configure_authz(fake_fga)
    for tier in ("trusted_circle", "connections", "trusted_circle", "trusted_circle"):
        result = await authz.on_connection_granted(
            profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier=tier
        )
        assert result is not None
        held = {t for t in fake_fga.tuples if t.user == f"user:{tia.user_id}"}
        assert held == {tier_tuple(bob.profile_id, tia.user_id, ConnectionTier(tier))}
    assert (
        await resolve_viewer_tier(fake_fga, tia.as_user(), bob.profile_id)
        is ViewerTier.trusted_circle
    )
    with pytest.raises(ValueError):
        await authz.on_connection_granted(
            profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="only_me"
        )


async def test_revocation_and_public_withdrawal_remove_access(fake_fga: FakeFga) -> None:
    bob, tia = make_person(), make_person()
    connected(fake_fga, bob)
    connected(fake_fga, tia)
    configure_authz(fake_fga)
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
    )
    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)
    for _ in range(2):  # idempotent
        await authz.on_connection_revoked(profile_id=bob.profile_id, viewer_user_id=tia.user_id)
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.public,) * 2
    for _ in range(2):
        await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=False)
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.none,) * 2
    assert not set(public_tuples(bob.profile_id)) & fake_fga.tuples


async def test_block_overrides_every_tier_and_unblock_restores_nothing(fake_fga: FakeFga) -> None:
    bob, tia = make_person(), make_person()
    connected(fake_fga, bob)
    connected(fake_fga, tia)
    configure_authz(fake_fga)
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
    )
    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)
    # Tia granted Bob a connection on her profile (the other direction).
    await authz.on_connection_granted(
        profile_id=tia.profile_id, viewer_user_id=bob.user_id, tier="connections"
    )

    for _ in range(2):
        await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=tia.user_id)
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.none,) * 2
    # A stale tier tuple written afterwards (e.g. a lagging grant) does not beat the block.
    fake_fga.tuples.add(tier_tuple(bob.profile_id, tia.user_id, ConnectionTier.TRUSTED_CIRCLE))
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.none,) * 2
    fake_fga.tuples.discard(tier_tuple(bob.profile_id, tia.user_id, ConnectionTier.TRUSTED_CIRCLE))
    # Directional: Bob keeps what Tia granted him.
    assert await tiers(fake_fga, bob, tia.profile_id) == (ViewerTier.connections,) * 2

    await authz.on_unblocked(profile_id=bob.profile_id, blocked_user_id=tia.user_id)
    assert blocked_tuple(bob.profile_id, tia.user_id) not in fake_fga.tuples
    # The block deleted the old tier; unblocking leaves only the Public opt-in.
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.public,) * 2


async def test_owner_blocking_self_keeps_owner_access(fake_fga: FakeFga) -> None:
    bob = make_person()
    connected(fake_fga, bob)
    configure_authz(fake_fga)
    await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=bob.user_id)
    assert await tiers(fake_fga, bob, bob.profile_id) == (ViewerTier.only_me,) * 2


async def test_question_created_writes_about_and_requester(fake_fga: FakeFga) -> None:
    bob, rae = make_person(), make_person()
    connected(fake_fga, bob)
    connected(fake_fga, rae)
    configure_authz(fake_fga)
    question = uuid4()
    for _ in range(2):
        result = await authz.on_question_created(
            question_id=question, profile_id=bob.profile_id, requester_user_id=rae.user_id
        )
    assert result is not None
    assert set(result.written) == set(question_tuples(question, bob.profile_id, rae.user_id))
    q = f"question:{question}"
    bob_agent, rae_agent = f"agent:{bob.delegation_id}", f"agent:{rae.delegation_id}"
    assert await fake_fga.check(user=bob_agent, relation="respond", object=q)
    assert not await fake_fga.check(user=rae_agent, relation="respond", object=q)
    for subject in (bob_agent, f"user:{bob.user_id}", rae_agent, f"user:{rae.user_id}"):
        assert await fake_fga.check(user=subject, relation="read_answer", object=q)
    assert not await fake_fga.check(user=f"user:{uuid4()}", relation="read_answer", object=q)


# --- Hook contract ----------------------------------------------------------------------------


def _kwargs(name: str, *, enabled: bool = True, tier: str = "trusted_circle") -> dict[str, Any]:
    profile_id, user_id = uuid4(), uuid4()
    return {
        "on_connection_granted": {
            "profile_id": profile_id,
            "viewer_user_id": user_id,
            "tier": tier,
        },
        "on_connection_revoked": {"profile_id": profile_id, "viewer_user_id": user_id},
        "on_blocked": {"profile_id": profile_id, "blocked_user_id": user_id},
        "on_unblocked": {"profile_id": profile_id, "blocked_user_id": user_id},
        "on_public_opt_in_changed": {"profile_id": profile_id, "enabled": enabled},
        "on_question_created": {
            "question_id": uuid4(),
            "profile_id": profile_id,
            "requester_user_id": user_id,
        },
    }[name]


GRANT_CALLS = [
    ("on_connection_granted", {"tier": "trusted_circle"}),
    ("on_unblocked", {}),
    ("on_public_opt_in_changed", {"enabled": True}),
    ("on_question_created", {}),
]
REVOKE_CALLS = [
    ("on_connection_granted", {"tier": "connections"}),  # may be a downgrade
    ("on_connection_revoked", {}),
    ("on_blocked", {}),
    ("on_public_opt_in_changed", {"enabled": False}),
]
ALL_CALLS = GRANT_CALLS + REVOKE_CALLS


def _call_id(call: tuple[str, dict[str, Any]]) -> str:
    return "-".join([call[0], *map(str, call[1].values())])


def test_v2_hooks_are_module_level_and_keyword_only() -> None:
    import inspect

    for name in V2_HOOKS:
        fn = getattr(authz, name)
        assert inspect.iscoroutinefunction(fn)
        params = inspect.signature(fn).parameters.values()
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)


@pytest.mark.parametrize("configured", [False, True], ids=["never-configured", "ids-unset"])
@pytest.mark.parametrize("call", GRANT_CALLS, ids=_call_id)
async def test_grant_hooks_warn_and_return_when_unconfigured(
    call: tuple[str, dict[str, Any]], configured: bool, caplog: pytest.LogCaptureFixture
) -> None:
    unconfigured = FakeFga(configured=False)
    if configured:
        configure_authz(unconfigured)
    name, extra = call
    assert await getattr(authz, name)(**_kwargs(name, **extra)) is None
    assert unconfigured.calls == []
    assert "not configured" in caplog.text


@pytest.mark.parametrize("configured", [False, True], ids=["never-configured", "ids-unset"])
@pytest.mark.parametrize("call", REVOKE_CALLS, ids=_call_id)
async def test_revoke_hooks_raise_when_unconfigured(
    call: tuple[str, dict[str, Any]], configured: bool
) -> None:
    unconfigured = FakeFga(configured=False)
    if configured:
        configure_authz(unconfigured)
    name, extra = call
    with pytest.raises(FgaConfigurationError):
        await getattr(authz, name)(**_kwargs(name, **extra))
    assert unconfigured.calls == []


@pytest.mark.parametrize("call", ALL_CALLS, ids=_call_id)
async def test_v2_hooks_raise_when_configured_client_errors(
    call: tuple[str, dict[str, Any]], fake_fga: FakeFga
) -> None:
    configure_authz(fake_fga)
    fake_fga.error = RuntimeError("openfga unavailable")
    name, extra = call
    with pytest.raises(RuntimeError, match="openfga unavailable"):
        await getattr(authz, name)(**_kwargs(name, **extra))


class SlowFga(FakeFga):
    async def write_tuples(self, tuples: Any) -> None:
        await anyio.sleep(30)

    async def delete_tuples(self, tuples: Any) -> None:
        await anyio.sleep(30)


@pytest.mark.parametrize("call", ALL_CALLS, ids=_call_id)
async def test_v2_hooks_time_out_promptly(
    call: tuple[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authz, "HOOK_TIMEOUT_SECONDS", 0.2)
    configure_authz(SlowFga())
    name, extra = call
    started = anyio.current_time()
    with pytest.raises(TimeoutError):
        await getattr(authz, name)(**_kwargs(name, **extra))
    assert anyio.current_time() - started < 2


async def test_tier_change_deletes_before_writing(fake_fga: FakeFga) -> None:
    """A failure between the two steps leaves no tier (fails closed)."""
    bob, tia = make_person(), make_person()
    configure_authz(fake_fga)
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
    )
    fake_fga.calls.clear()
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="connections"
    )
    assert fake_fga.calls == ["delete", "write"]
    fake_fga.calls.clear()
    await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=tia.user_id)
    assert fake_fga.calls == ["write", "delete"]  # the block lands first


# --- disconnect_agent -------------------------------------------------------------------------


def token(sub: str, client_id: str = "client_muse") -> AccessToken:
    return AccessToken(
        token="opaque", client_id=client_id, scopes=[], subject=sub, claims={"iss": "x"}
    )


@pytest.fixture
def resolver(fake_fga: FakeFga) -> IdentityResolver:
    configure_authz(fake_fga)
    return IdentityResolver(InMemoryIdentityRepository(), hooks=ModuleAuthzHooks())


def agent_tuples(fga: FakeFga, delegation_id: UUID | None) -> set[RelationTuple]:
    agent = f"agent:{delegation_id}"
    return {t for t in fga.tuples if agent in (t.user, t.object)}


async def test_agent_disconnects_itself(resolver: IdentityResolver, fake_fga: FakeFga) -> None:
    alex = await resolver.resolve(token("user_alex"), "mcp")
    profile_id = uuid4()
    fake_fga.tuples.add(owner_tuple(alex.user_id, profile_id))
    await disconnect_agent(alex, alex.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    assert agent_tuples(fake_fga, alex.delegation_id) == set()
    assert await resolve_viewer_tier(fake_fga, alex, profile_id) is ViewerTier.none
    # The still-valid old token cannot re-grant: F2 rejects the disconnected row, no tuples.
    with pytest.raises(DelegationDisconnected):
        await resolver.resolve(token("user_alex"), "mcp")
    assert agent_tuples(fake_fga, alex.delegation_id) == set()


async def test_owner_disconnects_own_delegations_from_ui_or_sibling_agent(
    resolver: IdentityResolver, fake_fga: FakeFga
) -> None:
    muse = await resolver.resolve(token("user_alex", "client_muse"), "mcp")
    other = await resolver.resolve(token("user_alex", "client_other"), "mcp")
    third = await resolver.resolve(token("user_alex", "client_third"), "mcp")
    ui = Principal(user_id=muse.user_id, via="ui")
    await disconnect_agent(ui, muse.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    await disconnect_agent(other, third.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    assert agent_tuples(fake_fga, muse.delegation_id) == set()
    assert agent_tuples(fake_fga, third.delegation_id) == set()
    assert agent_tuples(fake_fga, other.delegation_id) == set(
        delegation_tuples(other.user_id, other.delegation_id)  # type: ignore[arg-type]
    )


async def test_every_other_case_is_the_same_denial(
    resolver: IdentityResolver, fake_fga: FakeFga
) -> None:
    alex = await resolver.resolve(token("user_alex"), "mcp")
    sam = await resolver.resolve(token("user_sam"), "mcp")
    gone = await resolver.resolve(token("user_alex", "client_gone"), "mcp")
    await disconnect_agent(gone, gone.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    before = set(fake_fga.tuples)
    denials = []
    for principal, target in (
        (sam, alex.delegation_id),  # another user's agent
        (Principal(user_id=sam.user_id, via="ui"), alex.delegation_id),  # another user
        (alex, uuid4()),  # unknown
        (alex, gone.delegation_id),  # already disconnected
    ):
        with pytest.raises(AuthorizationDenied) as denied:
            await disconnect_agent(principal, target, resolver=resolver)  # type: ignore[arg-type]
        denials.append((type(denied.value), str(denied.value), denied.value.args))
    assert len(set(denials)) == 1
    assert fake_fga.tuples == before


async def test_inconsistent_principal_is_denied_before_any_call(
    resolver: IdentityResolver, fake_fga: FakeFga
) -> None:
    alex = await resolver.resolve(token("user_alex"), "mcp")
    fake_fga.calls.clear()
    bogus = Principal(user_id=alex.user_id, agent_id=None, delegation_id=None, via="mcp")
    with pytest.raises(AuthorizationDenied):
        await disconnect_agent(bogus, alex.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    assert fake_fga.calls == []
    assert agent_tuples(fake_fga, alex.delegation_id) != set()


async def test_disconnect_failures_propagate_and_roll_back(
    resolver: IdentityResolver, fake_fga: FakeFga
) -> None:
    alex = await resolver.resolve(token("user_alex"), "mcp")
    fake_fga.error = RuntimeError("openfga unavailable")
    with pytest.raises(RuntimeError, match="openfga unavailable"):
        await disconnect_agent(alex, alex.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    fake_fga.error = None
    reset_authz()  # unconfigured: the revoke hook raises, F2 rolls back
    with pytest.raises(FgaConfigurationError):
        await disconnect_agent(alex, alex.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    configure_authz(fake_fga)
    # Still active after both failures, so a retry disconnects it.
    assert (await resolver.resolve(token("user_alex"), "mcp")) == alex
    await disconnect_agent(alex, alex.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    assert agent_tuples(fake_fga, alex.delegation_id) == set()
