"""F5 service behavior against real PostgreSQL (and F4's FakeFga): policy, add_facts, owner
reads, corrections, audience changes, removal, review and policy effects on shared reads."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import anyio
import pytest
from authz_fakes import FakeFga
from conftest import World
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from revel.auth import authz
from revel.auth.authz import AuthorizationDenied, owner_tuple, public_tuples
from revel.auth.visibility import visible_claims
from revel.db.enums import Audience, ClaimStatus
from revel.db.models import Claim, ClaimEvent, ContributionPolicy, Profile
from revel.services import profiles
from revel.services.profiles import (
    AuthorizationUnavailable,
    CategorySetting,
    FactConflict,
    FactNotFound,
    FactPatch,
    NotAllowed,
    PolicyInvalid,
    PolicyUpdate,
    PolicyVersionConflict,
)

pytestmark = pytest.mark.anyio


def fact(category: str = "interest", statement: str = "Loves loose-leaf green tea", **kw: Any):
    return {"category": category, "statement": statement, "evidence_type": "explicit", **kw}


async def approve_policy(
    session: Session, world: World, expected: int | None = None, **fields: Any
) -> profiles.PolicyResponse:
    return await profiles.set_policy(
        session, world.alex.ui, PolicyUpdate(expected_version=expected, **fields)
    )


async def add(session: Session, principal, *facts: dict[str, Any]) -> list:
    return (await profiles.add_facts(session, principal, list(facts))).results


def claim_row(factory: sessionmaker[Session], claim_id) -> Claim:
    with factory() as s:
        return s.get(Claim, claim_id)


def events(factory: sessionmaker[Session], claim_id) -> list[ClaimEvent]:
    with factory() as s:
        return list(
            s.execute(
                select(ClaimEvent)
                .where(ClaimEvent.claim_id == claim_id)
                .order_by(ClaimEvent.occurred_at, ClaimEvent.action)
            ).scalars()
        )


def profile_of(factory: sessionmaker[Session], user_id) -> Any:
    with factory() as s:
        return s.execute(select(Profile.id).where(Profile.user_id == user_id)).scalar_one()


# --- Profiles and policy ----------------------------------------------------------------------


async def test_ensure_profile_creates_once_and_grants_owner(
    factory: sessionmaker[Session], fga: FakeFga, world: World
) -> None:
    with factory() as s:
        pid = await profiles.ensure_profile(s, user_id=world.alex.user.id)
        s.commit()
    with factory() as s:
        assert await profiles.ensure_profile(s, user_id=world.alex.user.id) == pid
        s.commit()
    assert owner_tuple(world.alex.user.id, pid) in fga.tuples
    assert fga.calls.count("write") == 1


async def test_ensure_profile_race_returns_one_profile(
    factory: sessionmaker[Session], fga: FakeFga, world: World
) -> None:
    """A concurrent creator waits on the uncommitted insert (ON CONFLICT) and then reads the
    winner's row; the owner hook runs once."""
    first, second = factory(), factory()
    try:
        pid = await profiles.ensure_profile(first, user_id=world.alex.user.id)
        results: list = []

        async def other() -> None:
            results.append(await profiles.ensure_profile(second, user_id=world.alex.user.id))

        async with anyio.create_task_group() as tg:
            tg.start_soon(other)
            await anyio.sleep(0.3)
            assert results == []  # blocked behind the uncommitted insert
            await anyio.to_thread.run_sync(first.commit)
        assert results == [pid]
        second.commit()
    finally:
        first.close()
        second.close()
    assert fga.calls.count("write") == 1


async def test_policy_versioning_and_conflict(session: Session, world: World) -> None:
    initial = await profiles.get_policy(session, world.alex.ui)
    assert initial.policy is None
    interest = next(c for c in initial.categories if c.id == "interest")
    assert interest.label and interest.effective_audience is Audience.CONNECTIONS
    assert interest.allowed_audiences == [
        Audience.ONLY_ME,
        Audience.TRUSTED_CIRCLE,
        Audience.CONNECTIONS,
    ]

    v1 = await approve_policy(session, world)
    assert v1.policy.version == 1 and v1.policy.share_provisional is True
    assert v1.policy.public_opt_in is False and v1.policy.enrichment_enabled is True
    with pytest.raises(PolicyVersionConflict) as info:
        await approve_policy(session, world, expected=None, require_review_all=True)
    assert info.value.policy.policy.version == 1
    v2 = await approve_policy(
        session,
        world,
        expected=1,
        category_settings={"size": CategorySetting(audience=Audience.ONLY_ME)},
    )
    assert v2.policy.version == 2
    assert v2.policy.category_settings["size"].audience is Audience.ONLY_ME
    size = next(c for c in v2.categories if c.id == "size")
    assert size.effective_audience is Audience.ONLY_ME
    # Omitted fields carry forward.
    v3 = await approve_policy(session, world, expected=2, share_provisional=False)
    assert v3.policy.category_settings["size"].audience is Audience.ONLY_ME
    assert v3.policy.share_provisional is False


async def test_policy_preserves_f7_question_settings(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world)
    with factory() as s:
        row = s.execute(select(ContributionPolicy)).scalar_one()
        row.question_settings = {"blocked_categories": ["size"]}
        row.questions_enabled = False
        s.commit()
    await approve_policy(session, world, expected=1, require_review_all=True)
    with factory() as s:
        latest = s.execute(
            select(ContributionPolicy).order_by(ContributionPolicy.version.desc()).limit(1)
        ).scalar_one()
        assert latest.question_settings == {"blocked_categories": ["size"]}
        assert latest.questions_enabled is False


@pytest.mark.parametrize(
    "settings",
    [
        {"size": CategorySetting(audience=Audience.CONNECTIONS)},  # wider than its default
        {"interest": CategorySetting(audience=Audience.PUBLIC)},  # Public without the opt-in
        {"dislike": CategorySetting(audience=Audience.PUBLIC)},
        {"sensitive": CategorySetting(audience=Audience.TRUSTED_CIRCLE)},
        {"health": CategorySetting(audience=Audience.ONLY_ME)},
    ],
)
async def test_invalid_policy_rejected(session: Session, world: World, settings) -> None:
    with pytest.raises(PolicyInvalid):
        await approve_policy(session, world, category_settings=settings)


async def test_agents_cannot_write_policy(session: Session, world: World) -> None:
    for principal in (world.alex.mcp, world.alex.api):
        with pytest.raises(NotAllowed):
            await profiles.set_policy(session, principal, PolicyUpdate())
    # Agents can read it.
    assert (await profiles.get_policy(session, world.alex.mcp)).policy is None


# --- add_facts --------------------------------------------------------------------------------


async def test_without_policy_agent_facts_are_pending(session: Session, world: World) -> None:
    (r,) = await add(session, world.alex.mcp, fact())
    assert r.outcome == "pending_review" and r.status is ClaimStatus.PENDING_REVIEW


async def test_add_facts_outcomes(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(
        session, world, category_settings={"dislike": CategorySetting(require_review=True)}
    )
    results = await add(
        session,
        world.alex.mcp,
        fact(),  # 0 accepted active
        fact(statement="Might like jazz", evidence_type="inferred"),  # 1 provisional
        fact(statement="Likes jazz", confidence="low"),  # 2 provisional
        fact("size", "Shoe size 42", details={"item": "shoes", "size": "42"}),  # 3 active
        fact("size", "Shirt M", details={"item": "shirt", "size": "M"}, audience="connections"),
        fact("dislike", "Dislikes scented candles"),  # 5 category require_review
        fact("health", "Has a condition"),  # 6 unknown -> sensitive hint
        fact("location", "Lives at 12 Baker Street"),  # 7 address
        fact("birthday", "Birthday", details={"month": 3, "day": 14, "year": 1990}),  # 8
        fact("interest", "x" * 501),  # 9 too long
        fact("address", "x"),  # 10 forbidden
        fact(supersedes_id=str(uuid4())),  # 11 supersedes not found
        {"category": "interest", "statement": "x"},  # 12 missing evidence_type
        fact("interest", "Public please", audience="public"),  # 13 public without opt-in
        fact(observed_at="2026-10-01"),  # 14 accepted, precision defaults to day
        fact(observed_precision="month"),  # 15 precision without a date
    )
    outcomes = [(r.outcome, r.status, r.reason_code) for r in results]
    assert outcomes == [
        ("accepted", ClaimStatus.ACTIVE, None),
        ("accepted", ClaimStatus.PROVISIONAL, None),
        ("accepted", ClaimStatus.PROVISIONAL, None),
        ("accepted", ClaimStatus.ACTIVE, None),
        ("pending_review", ClaimStatus.PENDING_REVIEW, None),
        ("pending_review", ClaimStatus.PENDING_REVIEW, None),
        ("rejected", None, "unknown_category"),
        ("rejected", None, "invalid_field"),
        ("rejected", None, "invalid_details"),
        ("rejected", None, "invalid_field"),
        ("rejected", None, "forbidden_category"),
        ("rejected", None, "supersedes_not_found"),
        ("rejected", None, "invalid_field"),
        ("rejected", None, "invalid_field"),
        ("accepted", ClaimStatus.ACTIVE, None),
        ("rejected", None, "invalid_field"),
    ]
    assert [r.index for r in results] == list(range(16))
    assert results[0].audience is Audience.CONNECTIONS
    assert results[3].audience is Audience.TRUSTED_CIRCLE
    row = claim_row(factory, results[14].claim_id)
    assert row.observed_at == date(2026, 10, 1) and row.observed_precision == "day"
    first = claim_row(factory, results[0].claim_id)
    assert first.contributed_via == "mcp" and first.policy_version == 1
    assert first.contributor_delegation_id == world.alex.delegation.id


async def test_require_review_all_and_enrichment_disabled(session: Session, world: World) -> None:
    await approve_policy(session, world, require_review_all=True)
    (r,) = await add(session, world.alex.api, fact())
    assert r.outcome == "pending_review"
    await approve_policy(session, world, expected=1, enrichment_enabled=False)
    (r,) = await add(session, world.alex.mcp, fact())
    assert (r.outcome, r.reason_code) == ("rejected", "enrichment_disabled")
    # The owner can still add facts themselves (explicitly accepted).
    (r,) = await add(session, world.alex.ui, fact())
    assert (r.outcome, r.status) == ("accepted", ClaimStatus.ACTIVE)


async def test_sensitive_category_rules(session: Session, world: World) -> None:
    await approve_policy(session, world)
    results = await add(
        session,
        world.alex.mcp,
        fact("sensitive", "Observes Ramadan; no daytime food gifts in that month"),
        fact("sensitive", "Probably religious", evidence_type="inferred"),
        fact("sensitive", "Volunteered", evidence_type="observed"),
        fact("sensitive", "Wider", audience="trusted_circle"),
    )
    assert [(r.outcome, r.reason_code) for r in results] == [
        ("pending_review", None),
        ("rejected", "sensitive_not_explicit"),
        ("rejected", "sensitive_not_explicit"),
        ("rejected", "invalid_field"),
    ]
    assert results[0].audience is Audience.ONLY_ME
    # Always reviewed, even for the owner's own entry and with review off.
    (owner,) = await add(session, world.alex.ui, fact("sensitive", "Vegetarian for faith"))
    assert owner.outcome == "pending_review"
    approved = await profiles.approve_fact(session, world.alex.ui, results[0].claim_id)
    assert approved.status is ClaimStatus.ACTIVE and approved.audience is Audience.ONLY_ME
    # It can never be widened, not even by the owner.
    with pytest.raises(profiles.InvalidRequest):
        await profiles.set_fact_audience(
            session, world.alex.ui, results[0].claim_id, Audience.TRUSTED_CIRCLE
        )


async def test_facts_involving_others(session: Session, world: World) -> None:
    await approve_policy(session, world)
    results = await add(
        session,
        world.alex.mcp,
        fact("life_event", "Partner starts a new job in May", involves_others=True),
        fact(
            "wish",
            "Wants a gift her sister would also like",
            involves_others=True,
            audience="only_me",
        ),
        fact("wish", "Same without the flag", audience="only_me"),
    )
    held, narrow, plain = results
    # Omitted audience defaults to Only Me, and it is held for review.
    assert (held.outcome, held.audience) == ("pending_review", Audience.ONLY_ME)
    assert "involves other people" in held.message
    # Even the narrowest audience is held when it involves others.
    assert (narrow.outcome, narrow.audience) == ("pending_review", Audience.ONLY_ME)
    assert plain.outcome == "accepted"
    # The owner's own entry is explicit acceptance.
    (own,) = await add(
        session, world.alex.ui, fact("life_event", "Partner's new job", involves_others=True)
    )
    assert (own.outcome, own.audience) == ("accepted", Audience.ONLY_ME)


# --- Supersede, review and changes ------------------------------------------------------------


async def test_accepted_replacement_supersedes_atomically(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world)
    (old,) = await add(session, world.alex.mcp, fact(statement="Owns a Moka pot"))
    (new,) = await add(
        session,
        world.alex.mcp,
        fact("owned_item", "Owns a Bialetti Moka pot", supersedes_id=str(old.claim_id)),
    )
    assert new.outcome == "accepted"
    assert claim_row(factory, old.claim_id).status == ClaimStatus.SUPERSEDED.value
    assert claim_row(factory, new.claim_id).supersedes_id == old.claim_id
    (superseded_event,) = [e for e in events(factory, old.claim_id) if e.action == "superseded"]
    assert superseded_event.changes == {"status": {"old": "active", "new": "superseded"}}
    # A superseded fact cannot be superseded again.
    (again,) = await add(session, world.alex.mcp, fact(supersedes_id=str(old.claim_id)))
    assert again.reason_code == "supersedes_not_found"


async def test_pending_replacement_keeps_old_until_approved(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world)
    (old,) = await add(session, world.alex.mcp, fact())
    (pending,) = await add(
        session,
        world.alex.mcp,
        fact(statement="Loves matcha", audience="public", supersedes_id=str(old.claim_id)),
    )
    assert pending.reason_code == "invalid_field"  # Public without opt-in: rejected
    (pending,) = await add(
        session,
        world.alex.mcp,
        fact("interest", "Loves matcha", supersedes_id=str(old.claim_id)),
    )
    assert pending.outcome == "accepted"  # sanity: same audience is auto-accepted
    # Widening needs review: the old fact stays live.
    (newer,) = await add(
        session, world.alex.mcp, fact("size", "M", details={"item": "x", "size": "M"})
    )
    (widen,) = await add(
        session,
        world.alex.mcp,
        fact(
            "size",
            "L",
            details={"item": "x", "size": "L"},
            audience="connections",
            supersedes_id=str(newer.claim_id),
        ),
    )
    assert widen.outcome == "pending_review"
    assert claim_row(factory, newer.claim_id).status == ClaimStatus.ACTIVE.value

    approved = await profiles.approve_fact(session, world.alex.ui, widen.claim_id)
    assert approved.status is ClaimStatus.ACTIVE
    assert claim_row(factory, newer.claim_id).status == ClaimStatus.SUPERSEDED.value
    actions = [e.action for e in events(factory, widen.claim_id)]
    assert actions == ["created", "approved"]


async def test_approve_conflicts_when_target_gone_and_reject(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world, require_review_all=True)
    (old,) = await add(session, world.alex.ui, fact())
    (a,) = await add(session, world.alex.mcp, fact(statement="v2", supersedes_id=str(old.claim_id)))
    (b,) = await add(session, world.alex.mcp, fact(statement="v3", supersedes_id=str(old.claim_id)))
    await profiles.approve_fact(session, world.alex.ui, a.claim_id)
    with pytest.raises(FactConflict) as info:
        await profiles.approve_fact(session, world.alex.ui, b.claim_id)
    assert info.value.fact.status is ClaimStatus.PENDING_REVIEW
    rejected = await profiles.reject_fact(session, world.alex.ui, b.claim_id)
    assert rejected.status is ClaimStatus.REMOVED
    assert [e.action for e in events(factory, b.claim_id)] == ["created", "rejected"]
    with pytest.raises(FactConflict):
        await profiles.reject_fact(session, world.alex.ui, b.claim_id)
    # Agents never approve or reject.
    for op in (profiles.approve_fact, profiles.reject_fact):
        with pytest.raises(NotAllowed):
            await op(session, world.alex.mcp, a.claim_id)


async def test_audience_changes(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world)
    (r,) = await add(session, world.alex.mcp, fact())
    narrowed = await profiles.set_fact_audience(
        session, world.alex.mcp, r.claim_id, Audience.TRUSTED_CIRCLE
    )
    assert narrowed.audience is Audience.TRUSTED_CIRCLE
    with pytest.raises(NotAllowed):
        await profiles.set_fact_audience(session, world.alex.api, r.claim_id, Audience.CONNECTIONS)
    widened = await profiles.patch_fact(
        session, world.alex.ui, r.claim_id, FactPatch(audience=Audience.CONNECTIONS)
    )
    assert widened.audience is Audience.CONNECTIONS and widened.id == r.claim_id
    with pytest.raises(profiles.InvalidRequest):
        await profiles.set_fact_audience(session, world.alex.ui, r.claim_id, Audience.PUBLIC)
    changes = [
        (e.changes, e.acceptance, e.actor_delegation_id)
        for e in events(factory, r.claim_id)
        if e.action == "audience_changed"
    ]
    assert changes == [
        (
            {"audience": {"old": "connections", "new": "trusted_circle"}},
            "automatic",
            world.alex.delegation.id,
        ),
        ({"audience": {"old": "trusted_circle", "new": "connections"}}, "explicit", None),
    ]


async def test_ui_correction_replaces_atomically(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world)
    (r,) = await add(session, world.alex.mcp, fact(evidence_type="inferred"))
    replacement = await profiles.patch_fact(
        session,
        world.alex.ui,
        r.claim_id,
        FactPatch(statement="Loves sencha", audience=Audience.TRUSTED_CIRCLE),
    )
    assert replacement.id != r.claim_id and replacement.supersedes_id == r.claim_id
    assert replacement.status is ClaimStatus.ACTIVE and replacement.contributor == "you"
    assert replacement.evidence_type.value == "explicit"
    assert claim_row(factory, r.claim_id).status == ClaimStatus.SUPERSEDED.value
    (created,) = [e for e in events(factory, replacement.id) if e.action == "created"]
    assert created.acceptance == "explicit" and created.policy_version == 1
    with pytest.raises(FactConflict) as info:
        await profiles.patch_fact(session, world.alex.ui, r.claim_id, FactPatch(statement="x"))
    assert info.value.fact.status is ClaimStatus.SUPERSEDED
    with pytest.raises(profiles.InvalidRequest):
        await profiles.patch_fact(
            session, world.alex.ui, replacement.id, FactPatch(details={"year": 1990})
        )
    with pytest.raises(NotAllowed):
        await profiles.patch_fact(session, world.alex.mcp, replacement.id, FactPatch(statement="y"))


async def test_remove_and_other_profiles_facts_are_not_found(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world)
    (r,) = await add(session, world.alex.mcp, fact())
    await add(session, world.sam.mcp, fact())  # Sam has a profile too
    for op in (
        lambda p: profiles.remove_fact(session, p, r.claim_id),
        lambda p: profiles.set_fact_audience(session, p, r.claim_id, Audience.ONLY_ME),
    ):
        for principal in (world.sam.mcp, world.sam.ui):
            with pytest.raises(FactNotFound):
                await op(principal)
    with pytest.raises(FactNotFound):
        await profiles.approve_fact(session, world.sam.ui, r.claim_id)
    with pytest.raises(FactNotFound):
        await profiles.remove_fact(session, world.alex.mcp, uuid4())
    # Cal has no profile at all: still a plain 404.
    with pytest.raises(FactNotFound):
        await profiles.remove_fact(session, world.cal.mcp, r.claim_id)

    removed = await profiles.remove_fact(session, world.alex.mcp, r.claim_id)
    assert removed.status is ClaimStatus.REMOVED
    with pytest.raises(FactConflict):
        await profiles.remove_fact(session, world.alex.mcp, r.claim_id)
    (event,) = [e for e in events(factory, r.claim_id) if e.action == "removed"]
    assert event.changes == {"status": {"old": "active", "new": "removed"}}


async def test_another_users_agent_cannot_contribute(
    session: Session, fga: FakeFga, world: World
) -> None:
    """A principal claiming Alex's user id through Sam's delegation fails F4's contribute check
    (identity resolution never builds one; this guards the service on its own)."""
    await add(session, world.alex.ui, fact())
    forged = profiles_principal(world.alex.user.id, world.sam.delegation)
    with pytest.raises(AuthorizationDenied):
        await profiles.add_facts(session, forged, [fact()])
    with pytest.raises(AuthorizationDenied):
        await profiles.get_my_profile(session, forged)


def profiles_principal(user_id, delegation):
    from revel.auth.identity import Principal

    return Principal(
        user_id=user_id, agent_id=delegation.agent_id, delegation_id=delegation.id, via="mcp"
    )


async def test_fga_unavailable_is_generic(session: Session, fga: FakeFga, world: World) -> None:
    await add(session, world.alex.ui, fact())
    fga.error = OSError("connection refused to fga.internal:8080")
    with pytest.raises(AuthorizationUnavailable) as info:
        await profiles.add_facts(session, world.alex.mcp, [fact()])
    assert "fga.internal" not in str(info.value)
    authz.reset_authz()
    with pytest.raises(AuthorizationUnavailable):
        await profiles.get_my_profile(session, world.alex.mcp)


# --- Owner reads ------------------------------------------------------------------------------


async def test_owner_read_and_summary(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    empty = await profiles.get_my_profile(session, world.alex.mcp)
    assert empty.profile_id is None and empty.facts == [] and empty.policy is None
    await approve_policy(session, world)
    results = await add(
        session,
        world.alex.mcp,
        fact(statement="Secret-ish statement text"),
        fact("size", "M", details={"item": "shirt", "size": "M"}, audience="connections"),
    )
    await add(session, world.alex.ui, fact("wish", "Wants a kayak"))
    await profiles.remove_fact(session, world.alex.ui, results[0].claim_id)

    mine = await profiles.get_my_profile(session, world.alex.mcp)
    assert mine.policy_version == 1 and mine.policy.version == 1
    by_statement = {f.statement: f for f in mine.facts}
    assert set(by_statement) == {"M", "Wants a kayak"}  # live and pending by default
    assert by_statement["M"].status is ClaimStatus.PENDING_REVIEW
    assert by_statement["M"].contributor == "Muse"
    assert by_statement["Wants a kayak"].contributor == "you"
    assert by_statement["Wants a kayak"].audience is Audience.CONNECTIONS

    removed = await profiles.get_my_profile(session, world.alex.ui, statuses=[ClaimStatus.REMOVED])
    assert [f.statement for f in removed.facts] == ["Secret-ish statement text"]

    summary = mine.summary
    assert summary.pending_review == 1
    assert summary.counts.by_status == {"removed": 1, "pending_review": 1, "active": 1}
    assert summary.counts.by_audience == {"connections": 1}
    actions = sorted(e.action for e in summary.recent)
    assert actions == ["auto_accepted", "created", "created", "removed"]
    assert {e.actor for e in summary.recent} == {"Muse", "you"}
    assert "Secret-ish" not in summary.model_dump_json()
    with factory() as s:
        for event in s.execute(select(ClaimEvent)).scalars():
            assert set(event.changes or {}) <= {"audience", "status"}
    assert (await profiles.get_summary(session, world.alex.ui)).pending_review == 1


async def test_claim_events_record_actor_policy_and_acceptance(
    factory: sessionmaker[Session], session: Session, world: World
) -> None:
    await approve_policy(session, world)
    await approve_policy(session, world, expected=1)
    (auto,) = await add(session, world.alex.mcp, fact())
    (mine,) = await add(session, world.alex.ui, fact("wish", "Kayak"))
    (held,) = await add(session, world.alex.mcp, fact("sensitive", "Volunteered"))
    rows = {
        r.claim_id: r
        for r in (events(factory, auto.claim_id) + events(factory, mine.claim_id))
        + events(factory, held.claim_id)
    }
    a, m, h = rows[auto.claim_id], rows[mine.claim_id], rows[held.claim_id]
    assert (a.action, a.acceptance, a.policy_version) == ("auto_accepted", "automatic", 2)
    assert (a.actor_user_id, a.actor_delegation_id) == (
        world.alex.user.id,
        world.alex.delegation.id,
    )
    assert (m.action, m.acceptance, m.actor_delegation_id) == ("created", "explicit", None)
    assert (h.action, h.acceptance) == ("created", None)
    assert h.changes == {"status": {"old": None, "new": "pending_review"}}


# --- Policy changes and shared reads ----------------------------------------------------------


async def statements_for(session: Session, principal, pid) -> set[str]:
    result = await visible_claims(session, principal, pid)
    session.rollback()
    return {c.statement for c in result.claims}


async def test_policy_tightening_applies_to_shared_reads_and_new_contributions(
    factory: sessionmaker[Session], session: Session, fga: FakeFga, world: World
) -> None:
    await approve_policy(session, world, public_opt_in=True)
    await add(
        session,
        world.alex.mcp,
        fact(statement="Public interest"),  # effective audience Public after opt-in
        fact("dislike", "Dislikes candles"),
        fact(statement="Maybe likes jazz", evidence_type="inferred", audience="connections"),
    )
    pid = profile_of(factory, world.alex.user.id)
    assert set(public_tuples(pid)) <= fga.tuples  # granted after commit
    await authz.on_connection_granted(
        profile_id=pid, viewer_user_id=world.cal.user.id, tier="connections"
    )
    cal, sam = world.cal.mcp, world.sam.ui
    assert await statements_for(session, cal, pid) == {
        "Public interest",
        "Dislikes candles",
        "Maybe likes jazz",
    }
    assert await statements_for(session, sam, pid) == {"Public interest"}

    # Disable provisional sharing: the provisional fact disappears for others.
    await approve_policy(session, world, expected=1, share_provisional=False)
    assert "Maybe likes jazz" not in await statements_for(session, cal, pid)

    # Tighten dislike to Trusted Circle: existing facts narrow, Cal loses them, new ones follow.
    await approve_policy(
        session,
        world,
        expected=2,
        category_settings={"dislike": CategorySetting(audience=Audience.TRUSTED_CIRCLE)},
    )
    assert "Dislikes candles" not in await statements_for(session, cal, pid)
    (new,) = await add(session, world.alex.mcp, fact("dislike", "Dislikes glitter"))
    assert (new.outcome, new.audience) == ("accepted", Audience.TRUSTED_CIRCLE)
    (wide,) = await add(
        session, world.alex.mcp, fact("dislike", "Dislikes confetti", audience="connections")
    )
    assert wide.outcome == "pending_review"

    # Withdraw Public: tuples deleted before commit; Sam (public only) loses access.
    await approve_policy(session, world, expected=3, public_opt_in=False)
    assert not set(public_tuples(pid)) & fga.tuples
    with pytest.raises(AuthorizationDenied):
        await statements_for(session, sam, pid)
    assert "Public interest" in await statements_for(session, cal, pid)
    (r,) = await add(session, world.alex.mcp, fact(statement="After withdrawal"))
    assert r.audience is Audience.CONNECTIONS
    (r,) = await add(session, world.alex.mcp, fact(statement="Try public", audience="public"))
    assert r.reason_code == "invalid_field"


async def test_public_withdrawal_hook_runs_before_commit_and_rolls_back(
    factory: sessionmaker[Session],
    session: Session,
    fga: FakeFga,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await approve_policy(session, world, public_opt_in=True)
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

    async def failing(tuples):
        raise OSError("boom")

    # Checks still pass; only the tuple delete/write fails.
    monkeypatch.setattr(fga, "delete_tuples", failing)
    with pytest.raises(AuthorizationUnavailable):
        await approve_policy(session, world, expected=1, public_opt_in=False)
    assert seen == [(False, 1)]  # called before commit
    assert committed_version() == 1  # rolled back
    monkeypatch.undo()
    monkeypatch.setattr(authz, "on_public_opt_in_changed", spy)

    await approve_policy(session, world, expected=1, public_opt_in=False)
    assert seen[-1] == (False, 1)  # still before the commit of version 2
    assert committed_version() == 2 and not set(public_tuples(pid)) & fga.tuples

    await approve_policy(session, world, expected=2, public_opt_in=True)
    assert seen[-1] == (True, 3)  # after the commit of version 3
    assert set(public_tuples(pid)) <= fga.tuples

    # A failing grant never fails the committed change.
    await approve_policy(session, world, expected=3, public_opt_in=False)
    monkeypatch.setattr(fga, "write_tuples", failing)
    result = await approve_policy(session, world, expected=4, public_opt_in=True)
    assert result.policy.version == 5 and result.policy.public_opt_in is True
    assert committed_version() == 5
    assert not set(public_tuples(pid)) & fga.tuples  # reconcile restores it


async def test_enable_grant_skipped_when_withdrawn_meanwhile(
    factory: sessionmaker[Session], session: Session, fga: FakeFga, world: World
) -> None:
    """The post-commit grant re-reads the current policy under the profile lock and skips the
    grant when a later version already withdrew Public."""
    await approve_policy(session, world)
    pid = profile_of(factory, world.alex.user.id)
    with factory() as s:
        s.add(ContributionPolicy(profile_id=pid, version=2, approved_by_user_id=world.alex.user.id))
        s.commit()
    await profiles._grant_public_after_commit(session, pid)
    assert not set(public_tuples(pid)) & fga.tuples
