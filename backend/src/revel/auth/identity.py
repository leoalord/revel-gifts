"""Principal model and identity resolution from verified token claims.

Identity comes only from a verified access token (``sub`` and client id); no
client-supplied identity is ever consulted. Resolution upserts the user, the
agent (one per OAuth client) and the user-agent delegation, but never updates
an existing delegation: a disconnected delegation is never reactivated. After a
disconnect, a fresh OAuth authorization (a new WorkOS ``sid`` issued after the
disconnect) creates a NEW delegation; tokens of the old authorization stay rejected
(see ``IdentityResolver._delegation_for``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from mcp.server.auth.provider import AccessToken

from revel.auth.hooks import AuthzHooks, ModuleAuthzHooks
from revel.auth.repository import AgentRecord, DelegationRecord, IdentityRepository, UserRecord
from revel.auth.workos_clients import (
    ClientDirectory,
    NullClientDirectory,
    is_url_client_id,
    sanitize_agent_name,
)
from revel.auth.workos_users import NullUserDirectory, UserDirectory, UserProfile

logger = logging.getLogger(__name__)

Via = Literal["mcp", "api", "ui"]


@dataclass(frozen=True)
class Principal:
    """The authenticated actor for one request.

    ``agent_id`` and ``delegation_id`` are both set (an assistant acting for the
    user) or both ``None`` (the user acting directly).
    """

    user_id: UUID
    agent_id: UUID | None
    delegation_id: UUID | None
    via: Via

    def __post_init__(self) -> None:
        if self.via not in ("mcp", "api", "ui"):
            raise ValueError("invalid via")
        if (self.agent_id is None) != (self.delegation_id is None):
            raise ValueError("agent_id and delegation_id must both be set or both be None")
        has_agent = self.agent_id is not None
        # Bearer-token transports always act through an assistant delegation; the
        # browser UI is the user acting directly.
        if self.via in ("mcp", "api") and not has_agent:
            raise ValueError(f"via={self.via!r} requires agent_id and delegation_id")
        if self.via == "ui" and has_agent:
            raise ValueError("via='ui' must not carry agent_id or delegation_id")


class IdentityError(Exception):
    """Base class for identity-resolution failures."""


class DelegationDisconnected(IdentityError):
    """The user disconnected this assistant; its tokens no longer act for them."""


class InvalidIdentityClaims(IdentityError):
    """The verified token lacks the claims needed to identify a user and client."""


def _claim_str(access_token: AccessToken, name: str) -> str | None:
    value = (access_token.claims or {}).get(name)
    return value if isinstance(value, str) and value else None


def _claim_issued_at(access_token: AccessToken) -> datetime | None:
    """The verified token's ``iat`` as an aware UTC datetime, or ``None`` if absent/invalid."""
    value = (access_token.claims or {}).get("iat")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


class IdentityResolver:
    def __init__(
        self,
        repository: IdentityRepository,
        *,
        user_directory: UserDirectory | None = None,
        hooks: AuthzHooks | None = None,
        client_directory: ClientDirectory | None = None,
        name_retry_seconds: float = 300.0,
    ):
        self.repository = repository
        self._directory = user_directory or NullUserDirectory()
        self._clients = client_directory or NullClientDirectory()
        self._hooks = hooks or ModuleAuthzHooks()
        self._name_retry_seconds = name_retry_seconds
        # client_id -> monotonic time before which no new name lookup is attempted.
        self._name_lookup_not_before: dict[str, float] = {}

    async def _capture_agent_name(self, agent: AgentRecord) -> None:
        """Fill an unnamed agent's ``name`` from WorkOS's record of its OAuth client.

        Best effort: never raises, never overwrites a non-empty name, and writes in its
        own ``agents``-only transaction. Lookups per client are rate limited in-process
        (one in flight, then one per ``name_retry_seconds`` while no name is found), so
        an unnamed or unknown client costs at most one WorkOS call per interval.
        """
        client_id = agent.oauth_client_id
        if agent.name or is_url_client_id(client_id):
            return
        now = time.monotonic()
        if self._name_lookup_not_before.get(client_id, 0.0) > now:
            return
        if len(self._name_lookup_not_before) > 10_000:
            self._name_lookup_not_before.clear()
        self._name_lookup_not_before[client_id] = now + self._name_retry_seconds
        try:
            name = sanitize_agent_name(await self._clients.fetch_client_name(client_id))
            if name is not None:
                await self.repository.set_agent_name_if_empty(agent.id, name)
                self._name_lookup_not_before.pop(client_id, None)
        except Exception as exc:
            logger.warning("agent name capture failed: %s", type(exc).__name__)

    async def _ensure_user(
        self, workos_user_id: str, profile: UserProfile | None = None
    ) -> UserRecord:
        """Find or insert the ``users`` row keyed by the WorkOS user id (shared by all paths)."""
        user = await self.repository.get_user_by_workos_id(workos_user_id)
        if user is not None:
            return user
        if profile is None:
            profile = await self._directory.fetch_profile(workos_user_id)

        async def user_created(record: UserRecord) -> None:
            await self._hooks.on_user_created(user_id=record.id)

        user, _ = await self.repository.ensure_user(
            workos_user_id,
            email=profile.email if profile else None,
            display_name=profile.display_name if profile else None,
            on_created=user_created,
        )
        return user

    async def _delegation_for(
        self, user_id: UUID, agent_id: UUID, access_token: AccessToken
    ) -> DelegationRecord:
        """The active delegation this token may act through, creating one on (re)connect.

        Reconnect rule (F2 decision): after a disconnect, only a fresh OAuth authorization
        creates a new delegation; the disconnected row is never reactivated or modified. A
        token is refused (``DelegationDisconnected``) when:

        * its ``sid`` (the WorkOS session/consent it was minted under, which refreshes keep)
          already acted through a disconnected delegation of this pair, or
        * the pair has disconnected history and the token's ``sid`` is not yet associated with
          the active delegation, unless it has a ``sid`` and was issued after the latest
          disconnect. A missing ``sid`` or ``iat`` fails closed.

        So tokens issued before a disconnect, and tokens refreshed from a pre-disconnect
        authorization Revel has seen, never act again; a new ``sid`` issued after the
        disconnect is a fresh authorization. The pair's first connection is unchanged (no
        history: any verified token creates the delegation, as before).
        """
        sid = _claim_str(access_token, "sid")
        state = await self.repository.get_delegation_state(user_id, agent_id, sid)
        if state.sid_on_disconnected:
            raise DelegationDisconnected("token belongs to a disconnected authorization")
        known = state.active is not None and state.sid_on_active
        last_disconnect = state.last_disconnected_at
        if last_disconnect is not None and not known:
            issued_at = _claim_issued_at(access_token)
            if sid is None or issued_at is None or issued_at <= last_disconnect:
                raise DelegationDisconnected("assistant delegation is disconnected")

        delegation = state.active
        recorded = known
        if delegation is None:

            async def delegation_created(record: DelegationRecord) -> None:
                await self._hooks.on_agent_delegated(
                    user_id=record.user_id, agent_id=record.agent_id, delegation_id=record.id
                )

            delegation, created = await self.repository.create_delegation(
                user_id, agent_id, authorized_sid=sid, on_created=delegation_created
            )
            if delegation is None:
                raise DelegationDisconnected("assistant delegation is disconnected")
            recorded = created  # the inserted row carries ``sid`` as ``authorized_sid``
        if sid is not None and not recorded:
            # Remember every sid that acts through the delegation so a later disconnect also
            # refuses its refreshed tokens. Separate transaction (F4: a transaction writing
            # agent_delegations writes nothing else).
            await self.repository.record_delegation_sid(delegation.id, sid)
        return delegation

    async def resolve(self, access_token: AccessToken, via: Via) -> Principal:
        """Resolve an assistant bearer token (``mcp``/``api``) to a delegated principal."""
        workos_user_id = access_token.subject
        client_id = access_token.client_id
        if not workos_user_id or not client_id:
            raise InvalidIdentityClaims("token has no subject or client id")

        user = await self._ensure_user(workos_user_id)
        agent, _ = await self.repository.ensure_agent(client_id)
        delegation = await self._delegation_for(user.id, agent.id, access_token)

        if not agent.name:
            await self._capture_agent_name(agent)

        return Principal(user_id=user.id, agent_id=agent.id, delegation_id=delegation.id, via=via)

    async def resolve_user(
        self, workos_user_id: str, *, profile: UserProfile | None = None
    ) -> Principal:
        """Resolve a verified browser session (the user acting directly) to a ``ui`` principal.

        ``workos_user_id`` must come from a verified WorkOS access token's ``sub``. Uses the
        same ``users`` row as :meth:`resolve`, so browser sign-in and MCP OAuth for one
        WorkOS user are the same Revel user. Never creates or touches agents/delegations.
        """
        if not isinstance(workos_user_id, str) or not workos_user_id:
            raise InvalidIdentityClaims("session has no subject")
        user = await self._ensure_user(workos_user_id, profile)
        return Principal(user_id=user.id, agent_id=None, delegation_id=None, via="ui")

    async def disconnect_delegation(self, *, user_id: UUID, delegation_id: UUID) -> bool:
        """Disconnect ``user_id``'s active delegation, revoking its authorization.

        Calls the authz ``on_agent_disconnected`` hook inside the same transaction, only
        when a row actually changed; a hook failure rolls back and propagates. Returns
        ``False`` (no hook) when already disconnected, not owned by ``user_id`` or unknown.
        Afterwards :meth:`resolve` rejects that authorization's tokens (including ones refreshed
        from it) with ``DelegationDisconnected``; only a fresh authorization reconnects, as a
        new delegation.
        """

        async def disconnected(record: DelegationRecord) -> None:
            await self._hooks.on_agent_disconnected(
                user_id=record.user_id, delegation_id=record.id, agent_id=record.agent_id
            )

        return await self.repository.disconnect_delegation(
            user_id=user_id, delegation_id=delegation_id, on_disconnected=disconnected
        )
