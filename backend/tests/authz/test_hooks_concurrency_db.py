"""B1 validation: concurrent first sign-ins through F2's real repository on the APP's pool.

20 new users sign in at once (new user + new delegation each, so both identity hooks run inside
F2's open transactions) through ``SqlAlchemyIdentityRepository`` + ``IdentityResolver`` on an
engine built by ``revel.db.session.create_db_engine`` (POOL_SIZE + MAX_OVERFLOW, default pool
timeout), with the configured fake OpenFGA. Sign-ins legitimately queue on the small pool; none
may fail, and the hooks must not check out a database connection.

Needs ``REVEL_TEST_DATABASE_URL`` (disposable; reset to Alembic head). Run DB suites serially.
"""

import contextvars
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread
import pytest
from authz_fakes import FakeFga
from mcp.server.auth.provider import AccessToken
from sqlalchemy import Engine, create_engine, event, pool, text
from sqlalchemy.orm import sessionmaker

from revel.auth import authz
from revel.auth.hooks import ModuleAuthzHooks
from revel.auth.identity import IdentityResolver
from revel.auth.repository import SqlAlchemyIdentityRepository
from revel.db.session import MAX_OVERFLOW, POOL_SIZE, create_db_engine

DATABASE_URL = os.environ.get("REVEL_TEST_DATABASE_URL")
ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
CONCURRENT_SIGN_INS = 20
TIME_BOUND_SECONDS = 5.0

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not DATABASE_URL, reason="REVEL_TEST_DATABASE_URL not set"),
]

_IN_HOOK: contextvars.ContextVar[str | None] = contextvars.ContextVar("in_hook", default=None)


@pytest.fixture(scope="module")
def migrated() -> None:
    from alembic import command
    from alembic.config import Config

    assert DATABASE_URL
    engine = create_engine(DATABASE_URL, poolclass=pool.NullPool)
    for action, target in ((command.downgrade, "base"), (command.upgrade, "head")):
        config = Config(str(ALEMBIC_INI))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            action(config, target)
    engine.dispose()


@pytest.fixture
def app_engine(migrated: None) -> Iterator[Engine]:
    engine = create_db_engine(DATABASE_URL)
    assert (engine.pool.size(), engine.pool._max_overflow) == (POOL_SIZE, MAX_OVERFLOW)  # type: ignore[attr-defined]
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE claim_events, claims, contribution_policies, profiles, "
                "agent_delegations, agents, users CASCADE"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def hook_checkouts(app_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Record pool checkouts made while a hook runs (context propagates into worker threads)."""
    seen: list[str] = []

    def on_checkout(*_: Any) -> None:
        hook = _IN_HOOK.get()
        if hook is not None:
            seen.append(hook)

    event.listen(app_engine, "checkout", on_checkout)
    for name in ("on_user_created", "on_agent_delegated"):
        original = getattr(authz, name)

        async def wrapped(*args: Any, _name: str = name, _orig: Any = original, **kw: Any) -> Any:
            token = _IN_HOOK.set(_name)
            try:
                return await _orig(*args, **kw)
            finally:
                _IN_HOOK.reset(token)

        # F2's ModuleAuthzHooks looks the functions up by name at call time.
        monkeypatch.setattr(authz, name, wrapped)
    try:
        yield seen
    finally:
        event.remove(app_engine, "checkout", on_checkout)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    authz.reset_authz()
    yield
    authz.reset_authz()


def token(i: int) -> AccessToken:
    return AccessToken(
        token="opaque", client_id="client_muse", scopes=[], subject=f"user_{i}", claims={}
    )


async def test_concurrent_first_sign_ins_on_app_pool(
    app_engine: Engine, hook_checkouts: list[str], fake_fga: FakeFga
) -> None:
    authz.configure_authz(fake_fga)
    factory = sessionmaker(bind=app_engine, expire_on_commit=False)
    resolver = IdentityResolver(
        SqlAlchemyIdentityRepository(lambda: factory), hooks=ModuleAuthzHooks()
    )
    results: dict[int, Any] = {}
    errors: list[BaseException] = []

    async def sign_in(i: int) -> None:
        try:
            results[i] = await resolver.resolve(token(i), "mcp")
        except BaseException as exc:
            errors.append(exc)

    started = time.monotonic()
    async with anyio.create_task_group() as tg:
        for i in range(CONCURRENT_SIGN_INS):
            tg.start_soon(sign_in, i)
    elapsed = time.monotonic() - started

    assert errors == []
    assert len(results) == CONCURRENT_SIGN_INS
    assert elapsed < TIME_BOUND_SECONDS, f"{CONCURRENT_SIGN_INS} sign-ins took {elapsed:.2f}s"
    # Every delegation hook wrote its two tuples; hooks made no DB checkout of their own.
    assert len(fake_fga.tuples) == 2 * CONCURRENT_SIGN_INS
    assert hook_checkouts == []
    print(f"{CONCURRENT_SIGN_INS} concurrent first sign-ins: {elapsed:.2f}s")


async def test_checkout_detector_flags_a_db_touching_hook(
    app_engine: Engine, hook_checkouts: list[str]
) -> None:
    """The detector itself works: a hook that opens a connection is recorded."""

    async def touches_db() -> None:
        token_ = _IN_HOOK.set("bad_hook")
        try:
            await anyio.to_thread.run_sync(lambda: app_engine.connect().close())
        finally:
            _IN_HOOK.reset(token_)

    await touches_db()
    assert hook_checkouts == ["bad_hook"]
