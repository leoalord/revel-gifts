"""Bridge to F4's authorization hooks in ``revel.auth.authz``.

F4 owns ``revel.auth.authz``. Identity resolution calls its hooks only when a
row was actually inserted. The module is looked up at call time: if it does not
exist yet, the hooks are no-ops; if it exists but a hook function is missing,
:class:`AuthzHookMissing` is raised (fail loud, so a renamed hook cannot silently
skip authorization setup).

Hook contract (agreed with F4):

* Hooks run *inside* the identity transaction that inserted the row, before
  commit. If a hook raises, the insert is rolled back and the next request
  retries both, so hooks must be **idempotent** (e.g. tolerate an existing tuple).
* Hooks must be **external-only** (e.g. OpenFGA writes). They must not read or
  write the identity tables through another database connection: the new row is
  uncommitted and holds a unique-key lock, so such access would block or miss it.
* A committed tuple can outlive a later commit failure; F4 should treat a tuple
  without a matching row as inert.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import UUID

AUTHZ_MODULE = "revel.auth.authz"


class AuthzHookMissing(RuntimeError):
    """``revel.auth.authz`` exists but does not define a required hook."""


class AuthzHooks(Protocol):
    async def on_user_created(self, *, user_id: UUID) -> None: ...

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> None: ...


class ModuleAuthzHooks:
    """Delegates to ``revel.auth.authz`` functions; no-op only while the module is absent."""

    def _function(self, name: str) -> Callable[..., Awaitable[Any]] | None:
        try:
            module = importlib.import_module(AUTHZ_MODULE)
        except ModuleNotFoundError as exc:
            if exc.name != AUTHZ_MODULE:
                raise
            return None
        fn = getattr(module, name, None)
        if fn is None:
            raise AuthzHookMissing(f"{AUTHZ_MODULE}.{name} is not defined")
        return fn

    async def on_user_created(self, *, user_id: UUID) -> None:
        fn = self._function("on_user_created")
        if fn is not None:
            await fn(user_id=user_id)

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> None:
        fn = self._function("on_agent_delegated")
        if fn is not None:
            await fn(user_id=user_id, agent_id=agent_id, delegation_id=delegation_id)
