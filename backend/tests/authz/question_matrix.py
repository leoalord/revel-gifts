"""Question-check matrix shared by the fake (unit) and real-OpenFGA tests.

``run_question_matrix(fga)`` drives ``can_respond``/``require_respond``/``can_release_answer``
through grants, a wrong ``profile_id``, a tier change, revocation (``read_answer`` survives,
the release does not), Public opt-in, a disconnected agent and a block.
"""

from uuid import UUID, uuid4

import pytest
from authz_fakes import Person, Principal, make_person

from revel.auth.authz import (
    AuthorizationDenied,
    TupleHooks,
    ViewerTier,
    can_release_answer,
    can_respond,
    delegation_tuples,
    owner_tuple,
    require_respond,
    resolve_viewer_tier,
    subject_for,
)
from revel.auth.fga import FgaClient


async def connect(fga: FgaClient, person: Person) -> None:
    await fga.write_tuples(
        [
            owner_tuple(person.user_id, person.profile_id),
            *delegation_tuples(person.user_id, person.delegation_id),
        ]
    )


async def run_question_matrix(fga: FgaClient) -> None:
    bob, rae, sam, cal, tom = (make_person() for _ in range(5))
    for person in (bob, rae, sam, cal, tom):
        await connect(fga, person)
    hooks = TupleHooks(fga)
    question = uuid4()
    await hooks.on_question_created(
        question_id=question, profile_id=bob.profile_id, requester_user_id=rae.user_id
    )
    q = f"question:{question}"

    async def release(principal: Principal, profile_id: UUID = bob.profile_id) -> ViewerTier | None:
        return await can_release_answer(principal, question, profile_id, fga=fga)

    # can_respond: only the asked-about owner's agent.
    assert await can_respond(bob.as_agent(uuid4()), question, fga=fga)
    await require_respond(bob.as_agent(uuid4()), question, fga=fga)
    for principal in (bob.as_user(), rae.as_agent(uuid4()), rae.as_user(), sam.as_agent(uuid4())):
        assert not await can_respond(principal, question, fga=fga)
        with pytest.raises(AuthorizationDenied):
            await require_respond(principal, question, fga=fga)
    assert not await can_respond(bob.as_agent(uuid4()), uuid4(), fga=fga)  # unknown question

    # Requester with no tier yet: read_answer holds, but nothing may be released.
    assert await fga.check(user=f"user:{rae.user_id}", relation="read_answer", object=q)
    assert await release(rae.as_user()) is None

    # A connections grant releases at connections, to the requester and the requester's agent.
    await hooks.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=rae.user_id, tier="connections"
    )
    assert await release(rae.as_user()) is ViewerTier.connections
    assert await release(rae.as_agent(uuid4())) is ViewerTier.connections
    # The owner side reads its own answers at only_me; strangers get nothing.
    assert await release(bob.as_agent(uuid4())) is ViewerTier.only_me
    assert await release(bob.as_user()) is ViewerTier.only_me
    assert await release(sam.as_user()) is None
    assert await release(sam.as_agent(uuid4())) is None
    # A NON-requester holding a tier on the asked-about profile gets nothing (read_answer is
    # what refuses them), at connections and, separately, at trusted_circle.
    for tier in ("connections", "trusted_circle"):
        await hooks.on_connection_tier_changing(
            profile_id=bob.profile_id, viewer_user_id=tom.user_id, tier=tier
        )
        await hooks.on_connection_granted(
            profile_id=bob.profile_id, viewer_user_id=tom.user_id, tier=tier
        )
        held = await resolve_viewer_tier(fga, tom.as_user(), bob.profile_id)
        assert held is ViewerTier(tier)  # the tier is really held
        for principal in (tom.as_user(), tom.as_agent(uuid4())):
            assert not await fga.check(
                user=subject_for(principal), relation="read_answer", object=q
            )
            assert await release(principal) is None
            assert not await can_respond(principal, question, fga=fga)
    # An unknown question releases nothing, even with a tier on the profile.
    assert await can_release_answer(rae.as_user(), uuid4(), bob.profile_id, fga=fga) is None

    # Wrong profile_id: rae is trusted on cal's profile, but the question is about bob's.
    await hooks.on_connection_granted(
        profile_id=cal.profile_id, viewer_user_id=rae.user_id, tier="trusted_circle"
    )
    assert await release(rae.as_user(), cal.profile_id) is None
    assert await release(rae.as_agent(uuid4()), cal.profile_id) is None
    assert await release(rae.as_user(), rae.profile_id) is None  # rae's own profile (only_me)
    assert await release(cal.as_agent(uuid4()), cal.profile_id) is None  # cal's owner side

    # The CURRENT tier: an upgrade is reflected at once.
    await hooks.on_connection_tier_changing(
        profile_id=bob.profile_id, viewer_user_id=rae.user_id, tier="trusted_circle"
    )
    await hooks.on_connection_granted(
        profile_id=bob.profile_id, viewer_user_id=rae.user_id, tier="trusted_circle"
    )
    assert await release(rae.as_user()) is ViewerTier.trusted_circle

    # Revocation: read_answer survives (by design), the release does not.
    await hooks.on_connection_revoked(profile_id=bob.profile_id, viewer_user_id=rae.user_id)
    for subject in (f"user:{rae.user_id}", f"agent:{rae.delegation_id}"):
        assert await fga.check(user=subject, relation="read_answer", object=q)
    assert await release(rae.as_user()) is None
    assert await release(rae.as_agent(uuid4())) is None

    # Public opt-in: the requester is back at public.
    await hooks.on_public_opt_in_changed(profile_id=bob.profile_id, enabled=True)
    assert await release(rae.as_user()) is ViewerTier.public
    assert await release(rae.as_agent(uuid4())) is ViewerTier.public

    # A disconnected requester agent gets nothing (the user still does).
    await hooks.on_agent_disconnected(user_id=rae.user_id, delegation_id=rae.delegation_id)
    assert await release(rae.as_agent(uuid4())) is None
    assert await release(rae.as_user()) is ViewerTier.public
    # A disconnected owner agent can no longer respond.
    await hooks.on_agent_disconnected(user_id=bob.user_id, delegation_id=bob.delegation_id)
    assert not await can_respond(bob.as_agent(uuid4()), question, fga=fga)
    assert await release(bob.as_user()) is ViewerTier.only_me

    # Block: read_answer and every tier go (Public included); the owner side is unaffected.
    await hooks.on_blocked(profile_id=bob.profile_id, blocked_user_id=rae.user_id)
    assert not await fga.check(user=f"user:{rae.user_id}", relation="read_answer", object=q)
    assert await release(rae.as_user()) is None
    assert await release(bob.as_user()) is ViewerTier.only_me
