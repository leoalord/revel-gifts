"""Model v2 service behavior with the in-memory fake: tiers, v2 hooks and ``disconnect_agent``.

The real model is checked by ``authorization/tests`` (CLI) and ``test_authz_v2_openfga.py``.
Hook timing (see ``revel.auth.authz``): revoke-class hooks run PRE-COMMIT and raise; grant
hooks on existing users/profiles run POST-COMMIT and log instead of raising.
"""

import inspect
import logging
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

PRE_COMMIT = (
    "on_connection_tier_changing",
    "on_connection_revoked",
    "on_blocked",
    "on_question_created",
)
POST_COMMIT = ("on_connection_granted", "on_unblocked")
V2_HOOKS = (*PRE_COMMIT, *POST_COMMIT, "on_public_opt_in_changed")


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


async def set_tier(profile_id: UUID, viewer_user_id: UUID, tier: str) -> None:
    """F6's approval/tier-change sequence around its commit."""
    await authz.on_connection_tier_changing(
        profile_id=profile_id, viewer_user_id=viewer_user_id, tier=tier
    )
    # ... the caller commits the connection_requests row here ...
    await authz.on_connection_granted(
        profile_id=profile_id, viewer_user_id=viewer_user_id, tier=tier
    )


# --- Tiers ------------------------------------------------------------------------------------


async def test_tier_hierarchy_and_agent_inheritance(fake_fga: FakeFga) -> None:
    bob, tia, cal, sam = (make_person() for _ in range(4))
    for person in (bob, tia, cal, sam):
        connected(fake_fga, person)
    configure_authz(fake_fga)
    await set_tier(bob.profile_id, tia.user_id, "trusted_circle")
    await set_tier(bob.profile_id, cal.user_id, ConnectionTier.CONNECTIONS)
    assert await tiers(fake_fga, bob, bob.profile_id) == (ViewerTier.only_me,) * 2
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.trusted_circle,) * 2
    assert await tiers(fake_fga, cal, bob.profile_id) == (ViewerTier.connections,) * 2
    assert await tiers(fake_fga, sam, bob.profile_id) == (ViewerTier.none,) * 2
    # Directional: Bob holds nothing on Tia's profile.
    assert await tiers(fake_fga, bob, tia.profile_id) == (ViewerTier.none,) * 2

    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)
    assert await tiers(fake_fga, sam, bob.profile_id) == (ViewerTier.public,) * 2
    assert await tiers(fake_fga, cal, bob.profile_id) == (ViewerTier.connections,) * 2


async def test_tier_change_contract(fake_fga: FakeFga) -> None:
    """Pre-commit removes any higher tier (a downgrade's revoke); post-commit writes the tier."""
    bob, tia = make_person(), make_person()
    connected(fake_fga, bob)
    configure_authz(fake_fga)
    tia_tiers = lambda: {t for t in fake_fga.tuples if t.user == f"user:{tia.user_id}"}  # noqa: E731
    trusted = tier_tuple(bob.profile_id, tia.user_id, ConnectionTier.TRUSTED_CIRCLE)
    connection = tier_tuple(bob.profile_id, tia.user_id, ConnectionTier.CONNECTIONS)

    await set_tier(bob.profile_id, tia.user_id, "trusted_circle")
    assert tia_tiers() == {trusted}
    # Downgrade: the PRE-COMMIT half alone already removes trusted (a rollback over-denies).
    fake_fga.calls.clear()
    result = await authz.on_connection_tier_changing(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="connections"
    )
    assert fake_fga.calls == ["delete"] and result.deleted == (trusted,)
    assert tia_tiers() == set()
    # The POST-COMMIT half only widens: it writes, then deletes the other (lower or gone) tier.
    fake_fga.calls.clear()
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="connections"
    )
    assert fake_fga.calls == ["write", "delete"]
    assert tia_tiers() == {connection}
    # Upgrade: the pre-commit half deletes nothing, so access never dips.
    result = await authz.on_connection_tier_changing(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
    )
    assert result.deleted == () and tia_tiers() == {connection}
    await authz.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
    )
    assert tia_tiers() == {trusted}
    for hook in (authz.on_connection_tier_changing, authz.on_connection_granted):
        with pytest.raises(ValueError):
            await hook(profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="only_me")


async def test_rolled_back_grant_never_grants(fake_fga: FakeFga) -> None:
    """The post-commit contract: a transaction that rolls back skips the grant, so nothing is
    granted; its pre-commit half (the only call made) only removes access."""
    bob, tia = make_person(), make_person()
    connected(fake_fga, bob)
    connected(fake_fga, tia)
    configure_authz(fake_fga)

    async def approve(*, commit: bool) -> None:
        await authz.on_connection_tier_changing(
            profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
        )
        if not commit:
            return  # rollback: post-commit hooks are never called
        await authz.on_connection_granted(
            profile_id=bob.profile_id, viewer_user_id=tia.user_id, tier="trusted_circle"
        )

    await approve(commit=False)
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.none,) * 2
    await approve(commit=True)
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.trusted_circle,) * 2


async def test_revocation_and_public_withdrawal_remove_access(fake_fga: FakeFga) -> None:
    bob, tia = make_person(), make_person()
    connected(fake_fga, bob)
    connected(fake_fga, tia)
    configure_authz(fake_fga)
    await set_tier(bob.profile_id, tia.user_id, "trusted_circle")
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
    await set_tier(bob.profile_id, tia.user_id, "trusted_circle")
    await authz.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)
    # Tia granted Bob a connection on her profile (the other direction).
    await set_tier(tia.profile_id, bob.user_id, "connections")

    for _ in range(2):
        await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=tia.user_id)
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.none,) * 2
    # A stale tier tuple written afterwards (e.g. a lagging grant) does not beat the block.
    stale = tier_tuple(bob.profile_id, tia.user_id, ConnectionTier.TRUSTED_CIRCLE)
    fake_fga.tuples.add(stale)
    assert await tiers(fake_fga, tia, bob.profile_id) == (ViewerTier.none,) * 2
    fake_fga.tuples.discard(stale)
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
    # read_answer does not follow the requester's tier (F7/#20 re-checks the facts).
    await set_tier(bob.profile_id, rae.user_id, "connections")
    await authz.on_connection_revoked(profile_id=bob.profile_id, viewer_user_id=rae.user_id)
    assert await fake_fga.check(user=rae_agent, relation="read_answer", object=q)


# --- Hook contract ----------------------------------------------------------------------------


def _kwargs(name: str, **extra: Any) -> dict[str, Any]:
    profile_id, user_id = uuid4(), uuid4()
    kwargs = {
        "on_connection_tier_changing": {
            "profile_id": profile_id,
            "viewer_user_id": user_id,
            "tier": "connections",
        },
        "on_connection_granted": {
            "profile_id": profile_id,
            "viewer_user_id": user_id,
            "tier": "trusted_circle",
        },
        "on_connection_revoked": {"profile_id": profile_id, "viewer_user_id": user_id},
        "on_blocked": {"profile_id": profile_id, "blocked_user_id": user_id},
        "on_unblocked": {"profile_id": profile_id, "blocked_user_id": user_id},
        "on_public_opt_in_changed": {"profile_id": profile_id, "enabled": True},
        "on_question_created": {
            "question_id": uuid4(),
            "profile_id": profile_id,
            "requester_user_id": user_id,
        },
    }[name]
    return {**kwargs, **extra}


# Post-commit grants on existing users/profiles: never raise once configured.
POST_COMMIT_CALLS = [
    ("on_connection_granted", {"tier": "connections"}),
    ("on_connection_granted", {"tier": "trusted_circle"}),
    ("on_unblocked", {}),
    ("on_public_opt_in_changed", {"enabled": True}),
]
# Pre-commit revokes: raise when unconfigured and on any error.
REVOKE_CALLS = [
    ("on_connection_tier_changing", {"tier": "connections"}),
    ("on_connection_tier_changing", {"tier": "trusted_circle"}),
    ("on_connection_revoked", {}),
    ("on_blocked", {}),
    ("on_public_opt_in_changed", {"enabled": False}),
]
# Pre-commit grant naming a new row's ID: warns while unconfigured, raises once configured.
PRE_COMMIT_GRANT_CALLS = [("on_question_created", {})]
GRANT_CALLS = POST_COMMIT_CALLS + PRE_COMMIT_GRANT_CALLS
RAISING_CALLS = REVOKE_CALLS + PRE_COMMIT_GRANT_CALLS


def _call_id(call: tuple[str, dict[str, Any]]) -> str:
    return "-".join([call[0], *map(str, call[1].values())])


def test_v2_hooks_are_module_level_and_keyword_only() -> None:
    for name in V2_HOOKS:
        fn = getattr(authz, name)
        assert inspect.iscoroutinefunction(fn)
        params = inspect.signature(fn).parameters.values()
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)
        doc = inspect.getdoc(fn) or ""
        timing = "PRE-COMMIT" if name in PRE_COMMIT else "POST-COMMIT"
        if name == "on_public_opt_in_changed":
            assert "PRE-COMMIT" in doc and "POST-COMMIT" in doc
        else:
            assert timing in doc, name


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


@pytest.mark.parametrize("call", RAISING_CALLS, ids=_call_id)
async def test_pre_commit_hooks_raise_when_configured_client_errors(
    call: tuple[str, dict[str, Any]], fake_fga: FakeFga
) -> None:
    configure_authz(fake_fga)
    fake_fga.error = RuntimeError("openfga unavailable")
    name, extra = call
    with pytest.raises(RuntimeError, match="openfga unavailable"):
        await getattr(authz, name)(**_kwargs(name, **extra))


@pytest.mark.parametrize("call", POST_COMMIT_CALLS, ids=_call_id)
async def test_post_commit_grants_log_instead_of_raising(
    call: tuple[str, dict[str, Any]], fake_fga: FakeFga, caplog: pytest.LogCaptureFixture
) -> None:
    configure_authz(fake_fga)
    fake_fga.error = RuntimeError("openfga unavailable: secret-ish detail")
    name, extra = call
    caplog.set_level(logging.ERROR, logger="revel.auth.authz")
    assert await getattr(authz, name)(**_kwargs(name, **extra)) is None
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    message = record.getMessage()
    assert name in message and "RuntimeError" in message and "reconcile" in message
    assert "secret-ish" not in message  # exception text (object IDs) is not logged


class SlowFga(FakeFga):
    async def write_tuples(self, tuples: Any) -> None:
        await anyio.sleep(30)

    async def delete_tuples(self, tuples: Any) -> None:
        await anyio.sleep(30)


@pytest.mark.parametrize("call", RAISING_CALLS + POST_COMMIT_CALLS, ids=_call_id)
async def test_v2_hooks_time_out_promptly(
    call: tuple[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authz, "HOOK_TIMEOUT_SECONDS", 0.2)
    configure_authz(SlowFga())
    name, extra = call
    started = anyio.current_time()
    if call in POST_COMMIT_CALLS:
        assert await getattr(authz, name)(**_kwargs(name, **extra)) is None
    else:
        with pytest.raises(TimeoutError):
            await getattr(authz, name)(**_kwargs(name, **extra))
    assert anyio.current_time() - started < 2


async def test_block_lands_before_tier_deletes(fake_fga: FakeFga) -> None:
    bob, tia = make_person(), make_person()
    configure_authz(fake_fga)
    fake_fga.calls.clear()
    await authz.on_blocked(profile_id=bob.profile_id, blocked_user_id=tia.user_id)
    assert fake_fga.calls == ["write", "delete"]


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


async def test_user_disconnects_any_own_delegation(
    resolver: IdentityResolver, fake_fga: FakeFga
) -> None:
    muse = await resolver.resolve(token("user_alex", "client_muse"), "mcp")
    other = await resolver.resolve(token("user_alex", "client_other"), "mcp")
    ui = Principal(user_id=muse.user_id, via="ui")
    await disconnect_agent(ui, muse.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    assert agent_tuples(fake_fga, muse.delegation_id) == set()
    assert agent_tuples(fake_fga, other.delegation_id) == set(
        delegation_tuples(other.user_id, other.delegation_id)  # type: ignore[arg-type]
    )


class CountingResolver(IdentityResolver):
    calls = 0

    async def disconnect_delegation(self, *, user_id: UUID, delegation_id: UUID) -> bool:
        type(self).calls += 1
        return await super().disconnect_delegation(user_id=user_id, delegation_id=delegation_id)


async def test_every_other_case_is_the_same_denial(fake_fga: FakeFga) -> None:
    configure_authz(fake_fga)
    resolver = CountingResolver(InMemoryIdentityRepository(), hooks=ModuleAuthzHooks())
    alex = await resolver.resolve(token("user_alex"), "mcp")
    sibling = await resolver.resolve(token("user_alex", "client_other"), "mcp")
    sam = await resolver.resolve(token("user_sam"), "mcp")
    gone = await resolver.resolve(token("user_alex", "client_gone"), "mcp")
    await disconnect_agent(gone, gone.delegation_id, resolver=resolver)  # type: ignore[arg-type]
    before = set(fake_fga.tuples)
    CountingResolver.calls = 0
    cases = [
        (alex, sibling.delegation_id, False),  # an agent may not cut off a sibling
        (alex, sam.delegation_id, False),  # nor another user's agent
        (sam, alex.delegation_id, False),
        (Principal(user_id=sam.user_id, via="ui"), alex.delegation_id, True),  # another user
        (Principal(user_id=alex.user_id, via="ui"), uuid4(), True),  # unknown
        (Principal(user_id=alex.user_id, via="ui"), gone.delegation_id, True),  # already gone
    ]
    denials = set()
    for principal, target, reaches_f2 in cases:
        calls = CountingResolver.calls
        with pytest.raises(AuthorizationDenied) as denied:
            await disconnect_agent(principal, target, resolver=resolver)  # type: ignore[arg-type]
        denials.add((type(denied.value), str(denied.value), denied.value.args))
        assert CountingResolver.calls == calls + reaches_f2
    assert len(denials) == 1
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
