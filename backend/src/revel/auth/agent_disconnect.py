"""Authorized agent disconnect. Owned by F4 (authorization).

``disconnect_agent(principal, delegation_id)`` is the one service F8 (#16) and the MCP
disconnect tool call. It authorizes, then delegates to F2's primitive
``IdentityResolver.disconnect_delegation(user_id=, delegation_id=)``, which flips the row and
runs ``authz.on_agent_disconnected`` in one transaction (a hook failure, including
unconfigured OpenFGA, rolls back and propagates here unchanged).

Authorization: the principal's own ``user_id`` is always the one passed, and F2's guarded
UPDATE only matches an ACTIVE delegation owned by that user. So a user (UI) may disconnect any
of their own delegations, any of the user's active agents may disconnect itself or a sibling
delegation of the same user, and nobody can disconnect another user's delegation. Every
``False`` from F2 (already disconnected, another user's, unknown) raises the same
``AuthorizationDenied``, so the result never reveals whether a delegation exists or whose it is.

Afterwards the delegation holds no OpenFGA tuple (``acts_for``, ``delegate``,
``connected_agent``), and F2's resolver rejects its still-valid tokens instead of re-inserting
the row, so they cannot re-grant it.
"""

from uuid import UUID

from revel.auth.authz import AuthorizationDenied, PrincipalLike, subject_for
from revel.auth.identity import IdentityResolver


def _default_resolver() -> IdentityResolver:
    from revel.auth.runtime import runtime_for
    from revel.settings import get_settings

    return runtime_for(get_settings()).require_resolver()


async def disconnect_agent(
    principal: PrincipalLike,
    delegation_id: UUID,
    *,
    resolver: IdentityResolver | None = None,
) -> None:
    """Disconnect ``delegation_id`` on behalf of ``principal``; raise ``AuthorizationDenied``
    unless this call disconnected one of the principal's own active delegations.

    ``resolver`` defaults to the process runtime's (``runtime_for(get_settings())``); HTTP
    routes for an app built with other settings pass ``get_auth_runtime(request)``'s.
    """
    subject_for(principal)  # rejects inconsistent principals (AuthorizationDenied)
    resolver = resolver if resolver is not None else _default_resolver()
    disconnected = await resolver.disconnect_delegation(
        user_id=principal.user_id, delegation_id=delegation_id
    )
    if not disconnected:
        raise AuthorizationDenied()
