"""Agent-first owner controls (F5 #29) against real PostgreSQL and F4's FakeFga: the owner's
own agent writes the policy, approves/rejects pending facts and changes audiences; anything
that widens access needs ``owner_confirmed`` (``OwnerConfirmationRequired`` lists the exact
changes), narrowing does not. Decision: Notion Privacy "Agent-first owner controls"."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from authz_fakes import FakeFga
from conftest import World
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from revel.auth import authz
from revel.auth.authz import AuthorizationDenied, public_tuples
from revel.auth.identity import Principal
from revel.db.enums import Audience, ClaimStatus
from revel.db.models import Claim, ClaimEvent, ContributionPolicy, Profile
from revel.services import profiles
from revel.services.profiles import (
    AuthorizationUnavailable,
    CategorySetting,
    FactNotFound,
    InvalidRequest,
    OwnerConfirmationRequired,
    PolicyUpdate,
)

pytestmark = pytest.mark.anyio


def fact(category: str = "interest", statement: str = "Loves loose-leaf green tea", **kw: Any):
    return {"category": category, "statement": statement, "evidence_type": "explicit", **kw}


async def add(session: Session, principal, *facts: dict[str, Any]) -> list:
    return (await profiles.add_facts(session, principal, list(facts))).results


def events(factory: sessionmaker[Session], claim_id) -> list[ClaimEvent]:
    with factory() as s:
        return list(
            s.execute(
                select(ClaimEvent)
                .where(ClaimEvent.claim_id == claim_id)
                .order_by(ClaimEvent.occurred_at, ClaimEvent.action)
            ).scalars()
        )


def claim_row(factory: sessionmaker[Session], claim_id) -> Claim:
    with factory() as s:
        return s.get(Claim, claim_id)


def profile_of(factory: sessionmaker[Session], user_id) -> Any:
    with factory() as s:
        return s.execute(select(Profile.id).where(Profile.user_id == user_id)).scalar_one()


def policy_rows(factory: sessionmaker[Session], user_id) -> list[ContributionPolicy]:
    with factory() as s:
        return list(
            s.execute(
                select(ContributionPolicy)
                .join(Profile, Profile.id == ContributionPolicy.profile_id)
                .where(Profile.user_id == user_id)
                .order_by(ContributionPolicy.version)
            ).scalars()
        )


# Strict and permissive v1 policies (approved by the owner in the browser).
STRICT: dict[str, Any] = {
    "public_opt_in": False,
    "share_provisional": False,
    "require_review_all": True,
    "enrichment_enabled": False,
    "category_settings": {
        "interest": CategorySetting(audience=Audience.ONLY_ME, require_review=True)
    },
}
PERMISSIVE: dict[str, Any] = {
    "public_opt_in": True,
    "share_provisional": True,
    "require_review_all": False,
    "enrichment_enabled": True,
}


# --- Policy -----------------------------------------------------------------------------------


async def test_first_policy_approval_needs_confirmation(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    for principal in (world.alex.mcp, world.alex.api):
        with pytest.raises(OwnerConfirmationRequired) as info:
            await profiles.set_policy(session, principal, PolicyUpdate())
        (change,) = info.value.changes
        assert "first time" in change and "automatically" in change
    assert policy_rows(factory, world.alex.user.id) == []

    v1 = await profiles.set_policy(session, world.alex.mcp, PolicyUpdate(), owner_confirmed=True)
    assert v1.policy.version == 1
    (row,) = policy_rows(factory, world.alex.user.id)
    assert row.approved_by_user_id == world.alex.user.id  # the owner, as for the browser


async def test_first_approval_needs_confirmation_even_when_strict(
    session: Session, world: World
) -> None:
    """The first approval widens (automatic acceptance starts) even if every flag tightens."""
    with pytest.raises(OwnerConfirmationRequired) as info:
        await profiles.set_policy(
            session,
            world.alex.api,
            PolicyUpdate(require_review_all=True, share_provisional=False),
        )
    assert len(info.value.changes) == 1


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ({"public_opt_in": True}, "Opt in to Public"),
        ({"share_provisional": True}, "Share provisional"),
        ({"require_review_all": False}, "every assistant contribution"),
        ({"enrichment_enabled": True}, "contributions back on"),
        (
            {"category_settings": {"interest": CategorySetting(audience=Audience.ONLY_ME)}},
            "Stop requiring your review for 'Interests and hobbies'",
        ),
        (
            {
                "category_settings": {
                    "interest": CategorySetting(audience=Audience.CONNECTIONS, require_review=True)
                }
            },
            "Widen the default audience of 'Interests and hobbies' from Only me to Connections",
        ),
    ],
)
async def test_each_policy_widening_needs_confirmation(
    factory: sessionmaker[Session],
    session: Session,
    world: World,
    update: dict[str, Any],
    expected: str,
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(**STRICT))
    for principal in (world.alex.mcp, world.alex.api):
        with pytest.raises(OwnerConfirmationRequired) as info:
            await profiles.set_policy(
                session, principal, PolicyUpdate(expected_version=1, **update)
            )
        assert any(expected in change for change in info.value.changes), info.value.changes
    assert len(policy_rows(factory, world.alex.user.id)) == 1  # nothing written

    v2 = await profiles.set_policy(
        session, world.alex.mcp, PolicyUpdate(expected_version=1, **update), owner_confirmed=True
    )
    assert v2.policy.version == 2
    assert policy_rows(factory, world.alex.user.id)[-1].approved_by_user_id == world.alex.user.id


@pytest.mark.parametrize(
    "update",
    [
        {"public_opt_in": False},
        {"share_provisional": False},
        {"require_review_all": True},
        {"enrichment_enabled": False},
        {"category_settings": {"size": CategorySetting(audience=Audience.ONLY_ME)}},
        {"category_settings": {"interest": CategorySetting(require_review=True)}},
    ],
)
async def test_each_policy_narrowing_needs_no_confirmation(
    session: Session, world: World, update: dict[str, Any]
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(**PERMISSIVE))
    v2 = await profiles.set_policy(
        session, world.alex.api, PolicyUpdate(expected_version=1, **update)
    )
    assert v2.policy.version == 2


async def test_noop_agent_policy_write_needs_no_confirmation(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(**STRICT))
    same = await profiles.set_policy(
        session, world.alex.mcp, PolicyUpdate(expected_version=1, **STRICT)
    )
    assert same.policy.version == 1
    assert len(policy_rows(factory, world.alex.user.id)) == 1


async def test_mixed_policy_write_needs_confirmation_for_its_widening(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(
        session,
        world.alex.ui,
        PolicyUpdate(
            **PERMISSIVE, category_settings={"size": CategorySetting(audience=Audience.ONLY_ME)}
        ),
    )
    mixed = PolicyUpdate(
        expected_version=1,
        share_provisional=False,  # narrowing
        category_settings={},  # widening: Sizes back to its default (Trusted Circle)
    )
    with pytest.raises(OwnerConfirmationRequired) as info:
        await profiles.set_policy(session, world.alex.mcp, mixed)
    (change,) = info.value.changes  # only the widening part is listed
    assert change == "Widen the default audience of 'Sizes' from Only me to Trusted Circle"
    current = await profiles.get_policy(session, world.alex.mcp)
    assert current.policy.version == 1 and current.policy.share_provisional is True

    v2 = await profiles.set_policy(session, world.alex.mcp, mixed, owner_confirmed=True)
    assert v2.policy.version == 2 and v2.policy.share_provisional is False
    size = next(c for c in v2.categories if c.id == "size")
    assert size.effective_audience is Audience.TRUSTED_CIRCLE


async def test_agent_tightening_narrows_existing_facts_with_its_delegation(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate())
    (r,) = await add(session, world.alex.mcp, fact())
    assert r.audience is Audience.CONNECTIONS
    await profiles.set_policy(
        session,
        world.alex.mcp,
        PolicyUpdate(
            expected_version=1,
            category_settings={"interest": CategorySetting(audience=Audience.ONLY_ME)},
        ),
    )
    assert claim_row(factory, r.claim_id).audience == Audience.ONLY_ME.value
    (changed,) = [e for e in events(factory, r.claim_id) if e.action == "audience_changed"]
    assert changed.actor_user_id == world.alex.user.id
    assert changed.actor_delegation_id == world.alex.delegation.id
    assert changed.acceptance == "automatic"  # no owner confirmation given
    assert changed.policy_version == 2


async def test_agent_public_opt_in_hook_ordering(
    factory: sessionmaker[Session],
    session: Session,
    fga: FakeFga,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same F4 hook timing as the browser: the agent's withdrawal revokes before commit (a
    failure rolls the version back); its confirmed opt-in grants after commit."""
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate())
    pid = profile_of(factory, world.alex.user.id)
    seen: list[tuple[bool, int]] = []
    real = authz.on_public_opt_in_changed

    def committed_version() -> int:
        with factory() as s:
            return s.execute(
                select(func.max(ContributionPolicy.version)).where(
                    ContributionPolicy.profile_id == pid
                )
            ).scalar_one()

    async def spy(*, profile_id, enabled):
        seen.append((enabled, await anyio.to_thread.run_sync(committed_version)))
        return await real(profile_id=profile_id, enabled=enabled)

    monkeypatch.setattr(authz, "on_public_opt_in_changed", spy)
    with pytest.raises(OwnerConfirmationRequired):
        await profiles.set_policy(
            session, world.alex.mcp, PolicyUpdate(expected_version=1, public_opt_in=True)
        )
    assert seen == [] and not set(public_tuples(pid)) & fga.tuples

    await profiles.set_policy(
        session,
        world.alex.mcp,
        PolicyUpdate(expected_version=1, public_opt_in=True),
        owner_confirmed=True,
    )
    assert seen == [(True, 2)]  # after the commit of version 2
    assert set(public_tuples(pid)) <= fga.tuples

    async def failing(tuples):
        raise OSError("boom")

    monkeypatch.setattr(fga, "delete_tuples", failing)
    with pytest.raises(AuthorizationUnavailable):
        await profiles.set_policy(
            session, world.alex.api, PolicyUpdate(expected_version=2, public_opt_in=False)
        )
    assert seen[-1] == (False, 2) and committed_version() == 2  # before commit; rolled back
    monkeypatch.undo()

    await profiles.set_policy(
        session, world.alex.api, PolicyUpdate(expected_version=2, public_opt_in=False)
    )
    assert committed_version() == 3 and not set(public_tuples(pid)) & fga.tuples


# --- Review of pending facts ------------------------------------------------------------------


async def test_agent_approves_replacement_atomically_with_confirmation(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(require_review_all=True))
    (old,) = await add(session, world.alex.ui, fact(statement="Owns a Moka pot"))
    (new,) = await add(
        session, world.alex.mcp, fact(statement="Owns a Bialetti", supersedes_id=str(old.claim_id))
    )
    assert new.outcome == "pending_review"

    with pytest.raises(OwnerConfirmationRequired) as info:
        await profiles.approve_fact(session, world.alex.mcp, new.claim_id)
    (change,) = info.value.changes
    assert "Approve the pending 'Interests and hobbies' fact" in change
    assert "visible to Connections" in change and str(old.claim_id) in change
    assert claim_row(factory, new.claim_id).status == ClaimStatus.PENDING_REVIEW.value
    assert claim_row(factory, old.claim_id).status == ClaimStatus.ACTIVE.value

    approved = await profiles.approve_fact(
        session, world.alex.mcp, new.claim_id, owner_confirmed=True
    )
    assert approved.status is ClaimStatus.ACTIVE
    assert claim_row(factory, old.claim_id).status == ClaimStatus.SUPERSEDED.value

    (ev,) = [e for e in events(factory, new.claim_id) if e.action == "approved"]
    assert (ev.acceptance, ev.actor_user_id, ev.actor_delegation_id) == (
        "explicit",
        world.alex.user.id,
        world.alex.delegation.id,
    )
    (sup,) = [e for e in events(factory, old.claim_id) if e.action == "superseded"]
    assert (sup.acceptance, sup.actor_delegation_id) == ("explicit", world.alex.delegation.id)

    # The owner's audit tells the agent's confirmed approval apart from automatic acceptance.
    summary = await profiles.get_summary(session, world.alex.ui)
    approvals = [e for e in summary.recent if e.claim_id == new.claim_id and e.action == "approved"]
    assert [(e.actor, e.acceptance.value) for e in approvals] == [("Muse", "explicit")]
    assert not any(e.action == "auto_accepted" for e in summary.recent)


async def test_agent_approves_sensitive_and_involves_others_only_with_confirmation(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate())
    (sensitive,) = await add(session, world.alex.mcp, fact("sensitive", "Volunteered topic"))
    (others,) = await add(
        session, world.alex.mcp, fact(statement="Plans a trip with Sam", involves_others=True)
    )
    for r in (sensitive, others):
        assert r.outcome == "pending_review"
        with pytest.raises(OwnerConfirmationRequired):
            await profiles.approve_fact(session, world.alex.api, r.claim_id)
        out = await profiles.approve_fact(session, world.alex.api, r.claim_id, owner_confirmed=True)
        assert out.status is ClaimStatus.ACTIVE and out.audience is Audience.ONLY_ME


async def test_agent_rejects_without_confirmation(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(require_review_all=True))
    (old,) = await add(session, world.alex.ui, fact())
    (a,) = await add(session, world.alex.mcp, fact(statement="v2", supersedes_id=str(old.claim_id)))
    (b,) = await add(session, world.alex.mcp, fact(statement="v3"))
    rejected = await profiles.reject_fact(session, world.alex.mcp, a.claim_id)
    assert rejected.status is ClaimStatus.REMOVED
    assert claim_row(factory, old.claim_id).status == ClaimStatus.ACTIVE.value  # target stays
    (ev,) = [e for e in events(factory, a.claim_id) if e.action == "rejected"]
    assert (ev.acceptance, ev.actor_delegation_id) == ("automatic", world.alex.delegation.id)
    await profiles.reject_fact(session, world.alex.api, b.claim_id, owner_confirmed=True)
    (ev,) = [e for e in events(factory, b.claim_id) if e.action == "rejected"]
    assert (ev.acceptance, ev.actor_delegation_id) == ("explicit", world.alex.delegation.id)


# --- Audiences --------------------------------------------------------------------------------


async def test_agent_audience_widening_needs_confirmation(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate())
    (r,) = await add(session, world.alex.mcp, fact())
    await profiles.set_fact_audience(session, world.alex.mcp, r.claim_id, Audience.ONLY_ME)
    with pytest.raises(OwnerConfirmationRequired) as info:
        await profiles.set_fact_audience(session, world.alex.mcp, r.claim_id, Audience.CONNECTIONS)
    assert info.value.changes == [
        "Widen who can see this 'Interests and hobbies' fact from Only me to Connections"
    ]
    widened = await profiles.set_fact_audience(
        session, world.alex.api, r.claim_id, Audience.CONNECTIONS, owner_confirmed=True
    )
    assert widened.audience is Audience.CONNECTIONS
    changes = [
        (e.changes["audience"]["new"], e.acceptance, e.actor_delegation_id)
        for e in events(factory, r.claim_id)
        if e.action == "audience_changed"
    ]
    assert changes == [
        ("only_me", "automatic", world.alex.delegation.id),
        ("connections", "explicit", world.alex.delegation.id),
    ]
    # Validation as before, even with the owner's confirmation.
    with pytest.raises(InvalidRequest):  # Public needs the opt-in
        await profiles.set_fact_audience(
            session, world.alex.mcp, r.claim_id, Audience.PUBLIC, owner_confirmed=True
        )
    (s,) = await add(session, world.alex.mcp, fact("sensitive", "Volunteered"))
    with pytest.raises(InvalidRequest):  # sensitive is always Only Me
        await profiles.set_fact_audience(
            session, world.alex.mcp, s.claim_id, Audience.TRUSTED_CIRCLE, owner_confirmed=True
        )


# --- Cross-user and UI ------------------------------------------------------------------------


async def test_another_users_agent_can_never_do_this(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate(require_review_all=True))
    (r,) = await add(session, world.alex.mcp, fact())
    await add(session, world.sam.mcp, fact())  # Sam has a profile too
    for op in (profiles.approve_fact, profiles.reject_fact):
        with pytest.raises(FactNotFound):
            await op(session, world.sam.mcp, r.claim_id, owner_confirmed=True)
    with pytest.raises(FactNotFound):
        await profiles.set_fact_audience(
            session, world.sam.api, r.claim_id, Audience.ONLY_ME, owner_confirmed=True
        )
    forged = Principal(
        user_id=world.alex.user.id,
        agent_id=world.sam.delegation.agent_id,
        delegation_id=world.sam.delegation.id,
        via="mcp",
    )
    with pytest.raises(AuthorizationDenied):
        await profiles.set_policy(
            session, forged, PolicyUpdate(expected_version=1), owner_confirmed=True
        )
    with pytest.raises(AuthorizationDenied):
        await profiles.approve_fact(session, forged, r.claim_id, owner_confirmed=True)
    assert claim_row(factory, r.claim_id).status == ClaimStatus.PENDING_REVIEW.value
    assert len(policy_rows(factory, world.alex.user.id)) == 1


async def test_browser_owner_needs_no_flag(session: Session, world: World) -> None:
    v1 = await profiles.set_policy(session, world.alex.ui, PolicyUpdate(require_review_all=True))
    assert v1.policy.version == 1
    v2 = await profiles.set_policy(
        session, world.alex.ui, PolicyUpdate(expected_version=1, public_opt_in=True)
    )
    assert v2.policy.public_opt_in is True
    (r,) = await add(session, world.alex.mcp, fact())
    approved = await profiles.approve_fact(session, world.alex.ui, r.claim_id)
    assert approved.status is ClaimStatus.ACTIVE
    await profiles.set_fact_audience(session, world.alex.ui, r.claim_id, Audience.ONLY_ME)
    widened = await profiles.set_fact_audience(session, world.alex.ui, r.claim_id, Audience.PUBLIC)
    assert widened.audience is Audience.PUBLIC


# --- Corrections never widen silently (review B1) ---------------------------------------------


async def test_agent_correction_inherits_or_holds_wider_audience(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate())
    (private,) = await add(session, world.alex.mcp, fact(audience="only_me"))
    assert (private.outcome, private.audience) == ("accepted", Audience.ONLY_ME)

    # Reproduction 1: audience omitted -> inherits Only Me (not the Connections default).
    (fixed,) = await add(
        session, world.alex.mcp, fact(statement="Loves sencha", supersedes_id=str(private.claim_id))
    )
    assert (fixed.outcome, fixed.audience) == ("accepted", Audience.ONLY_ME)
    assert claim_row(factory, private.claim_id).status == ClaimStatus.SUPERSEDED.value

    # Reproduction 2: cross-category, audience omitted -> still Only Me.
    (cross,) = await add(
        session,
        world.alex.api,
        fact("style", "Minimalist style", supersedes_id=str(fixed.claim_id)),
    )
    assert (cross.outcome, cross.audience) == ("accepted", Audience.ONLY_ME)

    # Explicit wider audience -> held, old stays live; approval needs the confirmation.
    (wider,) = await add(
        session,
        world.alex.mcp,
        fact(
            "style", "Scandinavian style", audience="connections", supersedes_id=str(cross.claim_id)
        ),
    )
    assert (wider.outcome, wider.audience) == ("pending_review", Audience.CONNECTIONS)
    assert "widen who can see the fact it replaces" in wider.message
    assert claim_row(factory, cross.claim_id).status == ClaimStatus.ACTIVE.value
    with pytest.raises(OwnerConfirmationRequired):
        await profiles.approve_fact(session, world.alex.mcp, wider.claim_id)
    await profiles.approve_fact(session, world.alex.mcp, wider.claim_id, owner_confirmed=True)
    assert claim_row(factory, cross.claim_id).status == ClaimStatus.SUPERSEDED.value


async def test_agent_correction_capped_at_category_default_and_narrower_accepted(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate())
    (wide,) = await add(session, world.alex.mcp, fact())  # interest at Connections
    assert wide.audience is Audience.CONNECTIONS
    # Inherited Connections is wider than Sizes allows: capped at its default (Trusted Circle).
    (size,) = await add(
        session,
        world.alex.mcp,
        fact(
            "size",
            "Wears M",
            details={"item": "shirt", "size": "M"},
            supersedes_id=str(wide.claim_id),
        ),
    )
    assert (size.outcome, size.audience) == ("accepted", Audience.TRUSTED_CIRCLE)
    assert claim_row(factory, wide.claim_id).status == ClaimStatus.SUPERSEDED.value
    # Equal or narrower explicit audience: accepted and supersedes atomically.
    (same,) = await add(
        session,
        world.alex.api,
        fact(
            "size",
            "Wears L",
            details={"item": "shirt", "size": "L"},
            audience="trusted_circle",
            supersedes_id=str(size.claim_id),
        ),
    )
    assert (same.outcome, same.audience) == ("accepted", Audience.TRUSTED_CIRCLE)
    (narrow,) = await add(
        session,
        world.alex.api,
        fact(
            "size",
            "Wears XL",
            details={"item": "shirt", "size": "XL"},
            audience="only_me",
            supersedes_id=str(same.claim_id),
        ),
    )
    assert (narrow.outcome, narrow.audience) == ("accepted", Audience.ONLY_ME)
    assert claim_row(factory, same.claim_id).status == ClaimStatus.SUPERSEDED.value
    (ev,) = [e for e in events(factory, narrow.claim_id) if e.action == "auto_accepted"]
    assert ev.acceptance == "automatic"


async def test_owner_ui_corrections_unchanged(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await profiles.set_policy(session, world.alex.ui, PolicyUpdate())
    (private,) = await add(session, world.alex.ui, fact(audience="only_me"))
    (ui_fix,) = await add(
        session, world.alex.ui, fact(statement="Loves sencha", supersedes_id=str(private.claim_id))
    )
    # The owner's own entry uses the category's effective default and is explicit, as before.
    assert (ui_fix.outcome, ui_fix.audience) == ("accepted", Audience.CONNECTIONS)
    assert claim_row(factory, private.claim_id).status == ClaimStatus.SUPERSEDED.value
