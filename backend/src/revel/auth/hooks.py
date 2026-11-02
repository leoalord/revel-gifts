"""Bridge to F4's authorization hooks in ``revel.auth.authz``.

F4 owns ``revel.auth.authz``. Identity resolution calls its hooks only when a
row was actually inserted. The module is looked up at call time so this package
works before F4 lands (missing module or function means no-op).
"""

from __future__ import annotations

import importlib
import logging
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

AUTHZ_MODULE = "revel.auth.authz"


class AuthzHooks(Protocol):
    async def on_user_created(self, *, user_id: UUID) -> None: ...

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> None: ...


class ModuleAuthzHooks:
    """Delegates to ``revel.auth.authz`` functions when present; otherwise no-ops."""

    def _function(self, name: str):
        try:
            module = importlib.import_module(AUTHZ_MODULE)
        except ModuleNotFoundError as exc:
            if exc.name != AUTHZ_MODULE:
                raise
            return None
        return getattr(module, name, None)

    async def on_user_created(self, *, user_id: UUID) -> None:
        fn = self._function("on_user_created")
        if fn is None:
            logger.debug("authz hook on_user_created not available; skipping")
            return
        await fn(user_id=user_id)

    async def on_agent_delegated(
        self, *, user_id: UUID, agent_id: UUID, delegation_id: UUID
    ) -> None:
        fn = self._function("on_agent_delegated")
        if fn is None:
            logger.debug("authz hook on_agent_delegated not available; skipping")
            return
        await fn(user_id=user_id, agent_id=agent_id, delegation_id=delegation_id)
