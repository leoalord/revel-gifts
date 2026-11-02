"""F7's question checks (``can_respond``, ``require_respond``, ``can_release_answer``) with fakes.

The same matrix runs against a real server in ``test_question_checks_openfga.py``.
"""

from collections.abc import Callable, Iterator
from uuid import UUID, uuid4

import anyio
import pytest
from authz_fakes import FakeFga, Person, Principal, make_person
from question_matrix import connect, run_question_matrix

from revel.auth import authz
from revel.auth.authz import (
    AuthorizationCheckTimeout,
    AuthorizationDenied,
    TupleHooks,
    ViewerTier,
    can_release_answer,
    can_respond,
    configure_authz,
    require_respond,
    reset_authz,
)
from revel.auth.fga import FgaConfigurationError

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_authz()
    yield
    reset_authz()


async def test_question_matrix_on_the_fake(fake_fga: FakeFga) -> None:
    await run_question_matrix(fake_fga)


async def asked(fga: FakeFga) -> tuple[Person, Person, UUID]:
    """bob's profile, asked about by rae (connections on it); returns (bob, rae, question)."""
    bob, rae = make_person(), make_person()
    for person in (bob, rae):
        await connect(fga, person)
    hooks = TupleHooks(fga)
    question = uuid4()
    await hooks.on_question_created(
        question_id=question, profile_id=bob.profile_id, requester_user_id=rae.user_id
    )
    await hooks.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=rae.user_id, tier="connections"
    )
    return bob, rae, question


async def test_configured_client_is_used_by_default(fake_fga: FakeFga) -> None:
    bob, rae, question = await asked(fake_fga)
    configure_authz(fake_fga)
    assert await can_respond(bob.as_agent(uuid4()), question)
    tier = await can_release_answer(rae.as_user(), question, bob.profile_id)
    assert tier is ViewerTier.connections


async def test_blocked_requester_with_a_stale_tier_gets_nothing(fake_fga: FakeFga) -> None:
    """A block tuple alone (tier tuple left behind) still stops the release."""
    bob, rae, question = await asked(fake_fga)
    await fake_fga.write_tuples([authz.blocked_tuple(bob.profile_id, rae.user_id)])
    for principal in (rae.as_user(), rae.as_agent(uuid4())):
        assert await can_release_answer(principal, question, bob.profile_id, fga=fake_fga) is None


async def test_wrong_profile_is_refused_before_read_answer(fake_fga: FakeFga) -> None:
    """The ``about`` guard runs first: a mismatched profile_id costs one check and no tier."""
    _, rae, question = await asked(fake_fga)
    other = make_person()
    await connect(fake_fga, other)
    await TupleHooks(fake_fga).on_connection_granted(
        profile_id=other.profile_id, viewer_user_id=rae.user_id, tier="trusted_circle"
    )
    fake_fga.calls.clear()
    assert await can_release_answer(rae.as_user(), question, other.profile_id, fga=fake_fga) is None
    assert fake_fga.calls == ["check"]


INCONSISTENT: list[Callable[[UUID], Principal]] = [
    # agent_id without a delegation, MCP without a delegation, UI with a delegation.
    lambda u: Principal(user_id=u, agent_id=uuid4(), delegation_id=None, via="mcp"),
    lambda u: Principal(user_id=u, via="mcp"),
    lambda u: Principal(user_id=u, agent_id=uuid4(), delegation_id=uuid4(), via="ui"),
]


@pytest.mark.parametrize("make", INCONSISTENT)
async def test_inconsistent_principals_are_denied(
    fake_fga: FakeFga, make: Callable[[UUID], Principal]
) -> None:
    bob, rae, question = await asked(fake_fga)
    fake_fga.calls.clear()
    for user_id in (bob.user_id, rae.user_id):
        principal = make(user_id)
        with pytest.raises(AuthorizationDenied):
            await can_respond(principal, question, fga=fake_fga)
        with pytest.raises(AuthorizationDenied):
            await require_respond(principal, question, fga=fake_fga)
        with pytest.raises(AuthorizationDenied):
            await can_release_answer(principal, question, bob.profile_id, fga=fake_fga)
    assert fake_fga.calls == []  # denied before any OpenFGA call


async def test_unconfigured_raises() -> None:
    person = make_person()
    for fga in (None, FakeFga(configured=False)):
        with pytest.raises(FgaConfigurationError):
            await can_respond(person.as_agent(uuid4()), uuid4(), fga=fga)
        with pytest.raises(FgaConfigurationError):
            await require_respond(person.as_agent(uuid4()), uuid4(), fga=fga)
        with pytest.raises(FgaConfigurationError):
            await can_release_answer(person.as_user(), uuid4(), person.profile_id, fga=fga)


async def test_server_errors_propagate(fake_fga: FakeFga) -> None:
    bob, rae, question = await asked(fake_fga)
    fake_fga.error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await can_respond(bob.as_agent(uuid4()), question, fga=fake_fga)
    with pytest.raises(RuntimeError):
        await can_release_answer(rae.as_user(), question, bob.profile_id, fga=fake_fga)


# --- Timeouts -----------------------------------------------------------------------------------


class SlowFga(FakeFga):
    """Checks of ``slow`` relations (all when empty) sleep ``delay`` seconds first."""

    def __init__(self, delay: float, slow: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self.delay = delay
        self.slow = slow
        self.checked: list[str] = []

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        self.checked.append(relation)
        if not self.slow or relation in self.slow:
            await anyio.sleep(self.delay)
        return await super().check(user=user, relation=relation, object=object)


@pytest.fixture
def short_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authz, "CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(authz, "TIER_RESOLUTION_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(authz, "ANSWER_RELEASE_TIMEOUT_SECONDS", 0.5)


def test_release_bound_is_seconds() -> None:
    assert authz.CHECK_TIMEOUT_SECONDS <= authz.ANSWER_RELEASE_TIMEOUT_SECONDS <= 5


async def test_hanging_respond_check_fails_closed(short_bounds: None) -> None:
    fga = SlowFga(delay=30)
    bob, _, question = await asked(fga)
    with anyio.fail_after(2):
        with pytest.raises(AuthorizationCheckTimeout) as caught:
            await can_respond(bob.as_agent(uuid4()), question, fga=fga)
        with pytest.raises(AuthorizationCheckTimeout):
            await require_respond(bob.as_agent(uuid4()), question, fga=fga)
    assert str(question) not in str(caught.value)


@pytest.mark.parametrize(
    "slow", ["about", "read_answer", "view_only_me", "view_connections"], ids=str
)
async def test_any_hanging_release_check_fails_closed(short_bounds: None, slow: str) -> None:
    """A hang on any step raises, never None and never a tier."""
    fga = SlowFga(delay=30, slow=frozenset({slow}))
    bob, rae, question = await asked(fga)
    with anyio.fail_after(2):
        with pytest.raises(AuthorizationCheckTimeout):
            await can_release_answer(rae.as_user(), question, bob.profile_id, fga=fga)
    assert fga.checked[-1] == slow


async def test_release_has_an_overall_bound(short_bounds: None) -> None:
    """Each check is under the per-check bound (0.2s) and the tier resolution under its own
    (0.5s; it would answer at about 0.6s), but together they exceed the overall bound: raise
    rather than return the tier."""
    fga = SlowFga(delay=0.12)
    bob, rae, question = await asked(fga)
    with anyio.fail_after(2):
        with pytest.raises(AuthorizationCheckTimeout):
            await can_release_answer(rae.as_user(), question, bob.profile_id, fga=fga)
    # about, read_answer, view_only_me, view_trusted: cut off before view_connections answers.
    assert len(fga.checked) <= 5


async def test_release_within_the_bounds_resolves(short_bounds: None) -> None:
    fga = SlowFga(delay=0.02)
    bob, rae, question = await asked(fga)
    tier = await can_release_answer(rae.as_user(), question, bob.profile_id, fga=fga)
    assert tier is ViewerTier.connections
