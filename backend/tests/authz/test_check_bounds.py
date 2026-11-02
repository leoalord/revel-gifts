"""Read-path checks are bounded and fail closed (``AuthorizationCheckTimeout``, never a grant).

Uses a slow in-memory fake and a real SDK client pointed at a server that accepts connections
but never answers (no OpenFGA needed).
"""

import asyncio
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import anyio
import pytest
from authz_fakes import FakeFga, make_person

from revel.auth import authz
from revel.auth.authz import (
    AuthorizationCheckTimeout,
    AuthorizationDenied,
    ViewerTier,
    can_contribute,
    owner_tuple,
    public_tuples,
    require_contribute,
    require_viewer,
    resolve_viewer_tier,
)
from revel.auth.fga import create_fga_client
from revel.settings import Settings

pytestmark = pytest.mark.anyio


class SlowFga(FakeFga):
    """Each check sleeps ``delay`` seconds before answering from the in-memory model."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.checked: list[str] = []

    async def check(self, *, user: str, relation: str, object: str) -> bool:
        self.checked.append(relation)
        await anyio.sleep(self.delay)
        return await super().check(user=user, relation=relation, object=object)


@pytest.fixture
def short_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(authz, "CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(authz, "TIER_RESOLUTION_TIMEOUT_SECONDS", 0.5)


def test_bounds_are_seconds_not_the_sdk_default() -> None:
    assert 0 < authz.CHECK_TIMEOUT_SECONDS <= 5
    assert authz.CHECK_TIMEOUT_SECONDS <= authz.TIER_RESOLUTION_TIMEOUT_SECONDS <= 5
    assert issubclass(AuthorizationCheckTimeout, TimeoutError)
    assert not issubclass(AuthorizationCheckTimeout, AuthorizationDenied)


async def test_hanging_contribute_check_fails_closed(short_bounds: None) -> None:
    person = make_person()
    fga = SlowFga(delay=30)
    await fga.write_tuples([owner_tuple(person.user_id, person.profile_id)])
    started = time.monotonic()
    with pytest.raises(AuthorizationCheckTimeout) as caught:
        await can_contribute(fga, person.as_user(), person.profile_id)
    assert time.monotonic() - started < 1
    assert str(person.profile_id) not in str(caught.value)  # no object IDs in the message
    with pytest.raises(AuthorizationCheckTimeout):
        await require_contribute(fga, person.as_user(), person.profile_id)


async def test_one_hanging_tier_check_fails_closed(short_bounds: None) -> None:
    """A viewer who would resolve to Public: a hang on a higher tier raises, never ``none``."""
    owner, viewer = make_person(), make_person()
    fga = SlowFga(delay=30)
    await fga.write_tuples(public_tuples(owner.profile_id))
    with pytest.raises(AuthorizationCheckTimeout):
        await resolve_viewer_tier(fga, viewer.as_user(), owner.profile_id)
    with pytest.raises(AuthorizationCheckTimeout):
        await require_viewer(fga, viewer.as_user(), owner.profile_id)


async def test_tier_resolution_has_an_overall_bound(short_bounds: None) -> None:
    """Each check is under the per-check bound, but four of them exceed the overall bound: the
    resolution raises rather than returning the Public tier reached last."""
    owner, viewer = make_person(), make_person()
    fga = SlowFga(delay=0.15)
    await fga.write_tuples(public_tuples(owner.profile_id))
    started = time.monotonic()
    with pytest.raises(AuthorizationCheckTimeout):
        await resolve_viewer_tier(fga, viewer.as_user(), owner.profile_id)
    assert time.monotonic() - started < 0.9
    assert fga.checked[-1] == authz.VIEW_PUBLIC  # cut off during the check that would grant


async def test_checks_within_the_bounds_still_resolve(short_bounds: None) -> None:
    owner, viewer = make_person(), make_person()
    fga = SlowFga(delay=0.05)
    await fga.write_tuples(public_tuples(owner.profile_id))
    assert await resolve_viewer_tier(fga, viewer.as_user(), owner.profile_id) is ViewerTier.public


async def test_visible_claims_propagates_the_timeout(
    short_bounds: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Visibility reads go through ``resolve_viewer_tier``: a timeout surfaces before any SQL."""
    from revel.auth import visibility

    owner, viewer = make_person(), make_person()
    fga = SlowFga(delay=30)

    class NoSession:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("no SQL may run after a failed check")

    with pytest.raises(AuthorizationCheckTimeout):
        await visibility.visible_claims(
            NoSession(),  # type: ignore[arg-type]
            viewer.as_user(),
            owner.profile_id,
            fga=fga,
        )


@pytest.fixture
async def silent_server() -> AsyncIterator[str]:
    """A TCP server that accepts connections and never answers (a hung OpenFGA)."""
    hold: list[asyncio.StreamWriter] = []

    async def accept(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        hold.append(writer)

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        for writer in hold:
            writer.close()
        server.close()
        await server.wait_closed()


async def test_real_client_against_a_hanging_server_fails_closed(
    short_bounds: None, silent_server: str
) -> None:
    settings = Settings(
        _env_file=None,
        openfga_api_url=silent_server,
        openfga_store_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        openfga_authorization_model_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
    )
    fga = create_fga_client(settings)
    person = make_person()
    try:
        started = time.monotonic()
        with pytest.raises(AuthorizationCheckTimeout):
            await can_contribute(fga, person.as_user(), uuid4())
        with pytest.raises(AuthorizationCheckTimeout):
            await resolve_viewer_tier(fga, person.as_user(), uuid4())
        assert time.monotonic() - started < 2
    finally:
        await fga.close()
