"""The claim WRITE path (F5, RG-18): inserts and state changes of ``claims`` plus their
``claim_events``. Write-only by design and the single allowlisted claim-write module of F4's
static guard (``tests/authz/test_claim_read_guard.py``).

Callers (``revel.services.profiles``) authorize first (``authz.require_contribute``) and decide
policy outcomes; every function here is synchronous, runs on the caller's ``Session`` inside the
caller's transaction (in a worker thread) and never commits. The only reads are the rows being
changed, by primary key and ``FOR UPDATE``, checked against the caller's profile: a fact of
another profile is indistinguishable from a missing one (``ClaimNotFound``).

``claim_events.changes`` carries only ``{"audience": {"old", "new"}}`` and/or
``{"status": {"old", "new"}}``; never statement text or details.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from revel.db.enums import (
    LIVE_CLAIM_STATUSES,
    AcceptanceMode,
    Audience,
    ClaimEventAction,
    ClaimStatus,
)
from revel.db.models import Claim, ClaimEvent

_LIVE = frozenset(s.value for s in LIVE_CLAIM_STATUSES)


class ClaimNotFound(LookupError):
    """No such fact on the caller's profile (also used for another profile's fact)."""


class ClaimStateConflict(Exception):
    """The fact is not in a state that allows the change (e.g. already superseded)."""

    def __init__(self, claim: Claim, reason: str) -> None:
        super().__init__(reason)
        self.claim = claim
        self.reason = reason


class SupersedesNotLive(LookupError):
    """``supersedes_id`` does not name a live fact of the caller's profile."""


@dataclass(frozen=True, slots=True)
class Actor:
    """Who makes the change: the human user and, for an agent, its delegation."""

    user_id: UUID
    delegation_id: UUID | None
    via: str  # ContributedVia value


@dataclass(frozen=True, slots=True)
class NewFact:
    """A fact ready to insert; ``status`` and ``acceptance`` were decided by the caller."""

    category: str
    statement: str
    details: dict[str, Any] | None
    audience: Audience
    evidence_type: str
    confidence: str
    observed_at: date | None
    observed_precision: str
    status: ClaimStatus
    acceptance: AcceptanceMode | None
    policy_version: int | None
    supersedes_id: UUID | None = None


def _event(
    session: Session,
    claim: Claim,
    *,
    action: ClaimEventAction,
    actor: Actor,
    acceptance: AcceptanceMode | None,
    policy_version: int | None,
    changes: dict[str, dict[str, str | None]] | None,
) -> None:
    session.add(
        ClaimEvent(
            claim_id=claim.id,
            profile_id=claim.profile_id,
            action=action.value,
            actor_user_id=actor.user_id,
            actor_delegation_id=actor.delegation_id,
            acceptance=acceptance.value if acceptance else None,
            policy_version=policy_version,
            changes=changes,
        )
    )


def _status_change(old: str | None, new: ClaimStatus) -> dict[str, dict[str, str | None]]:
    return {"status": {"old": old, "new": new.value}}


def lock_claim(session: Session, profile_id: UUID, claim_id: UUID) -> Claim:
    """The fact row, locked ``FOR UPDATE``; ``ClaimNotFound`` unless it is on ``profile_id``."""
    claim = (
        session.execute(
            select(Claim)
            .where(Claim.id == claim_id, Claim.profile_id == profile_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .one_or_none()
    )
    if claim is None:
        raise ClaimNotFound()
    return claim


def lock_claims(session: Session, profile_id: UUID, claim_ids: Iterable[UUID]) -> None:
    """Lock the given facts of ``profile_id`` ``FOR UPDATE`` in id order (missing or foreign
    ids are skipped; later per-row checks report them). Callers that will lock several facts
    call this first so concurrent transactions acquire them in one global order."""
    ids = sorted(set(claim_ids))
    if not ids:
        return
    session.execute(
        select(Claim.id)
        .where(Claim.profile_id == profile_id, Claim.id.in_(ids))
        .order_by(Claim.id)
        .with_for_update()
    ).all()


def _supersede(
    session: Session,
    target: Claim,
    *,
    actor: Actor,
    acceptance: AcceptanceMode | None,
    policy_version: int | None,
) -> None:
    old = target.status
    target.status = ClaimStatus.SUPERSEDED.value
    _event(
        session,
        target,
        action=ClaimEventAction.SUPERSEDED,
        actor=actor,
        acceptance=acceptance,
        policy_version=policy_version,
        changes=_status_change(old, ClaimStatus.SUPERSEDED),
    )


def insert_fact(session: Session, *, profile_id: UUID, actor: Actor, fact: NewFact) -> Claim:
    """Insert one fact and its event. A LIVE replacement retires ``supersedes_id`` in the same
    transaction (insert first, then supersede); a pending one leaves the old fact live."""
    target: Claim | None = None
    if fact.supersedes_id is not None:
        try:
            target = lock_claim(session, profile_id, fact.supersedes_id)
        except ClaimNotFound:
            raise SupersedesNotLive() from None
        if target.status not in _LIVE:
            raise SupersedesNotLive()
    claim = Claim(
        profile_id=profile_id,
        category=fact.category,
        statement=fact.statement,
        details=fact.details,
        audience=fact.audience.value,
        evidence_type=fact.evidence_type,
        confidence=fact.confidence,
        observed_at=fact.observed_at,
        observed_precision=fact.observed_precision,
        status=fact.status.value,
        contributor_user_id=actor.user_id,
        contributor_delegation_id=actor.delegation_id,
        contributed_via=actor.via,
        policy_version=fact.policy_version,
        supersedes_id=fact.supersedes_id,
    )
    session.add(claim)
    session.flush()
    action = (
        ClaimEventAction.AUTO_ACCEPTED
        if fact.acceptance is AcceptanceMode.AUTOMATIC
        else ClaimEventAction.CREATED
    )
    _event(
        session,
        claim,
        action=action,
        actor=actor,
        acceptance=fact.acceptance,
        policy_version=fact.policy_version,
        changes=_status_change(None, fact.status),
    )
    if target is not None and fact.status.value in _LIVE:
        _supersede(
            session,
            target,
            actor=actor,
            acceptance=fact.acceptance,
            policy_version=fact.policy_version,
        )
    session.flush()
    session.refresh(claim)
    return claim


def set_audience(
    session: Session,
    *,
    profile_id: UUID,
    claim_id: UUID,
    actor: Actor,
    audience: Audience,
    acceptance: AcceptanceMode,
    policy_version: int | None,
    check: Callable[[Claim], None],
) -> Claim:
    """Change a live or pending fact's audience in place. ``check`` (the caller's policy rule,
    e.g. agents may only narrow) runs on the locked row and may raise."""
    claim = lock_claim(session, profile_id, claim_id)
    if claim.status not in _LIVE and claim.status != ClaimStatus.PENDING_REVIEW.value:
        raise ClaimStateConflict(claim, "fact_not_editable")
    check(claim)
    old = claim.audience
    if old == audience.value:
        return claim
    claim.audience = audience.value
    _event(
        session,
        claim,
        action=ClaimEventAction.AUDIENCE_CHANGED,
        actor=actor,
        acceptance=acceptance,
        policy_version=policy_version,
        changes={"audience": {"old": old, "new": audience.value}},
    )
    session.flush()
    session.refresh(claim)
    return claim


def narrow_category(
    session: Session,
    *,
    profile_id: UUID,
    category: str,
    audience: Audience,
    actor: Actor,
    policy_version: int,
    acceptance: AcceptanceMode = AcceptanceMode.EXPLICIT,
) -> int:
    """Owner tightened a category (in the browser or through their agent): narrow its live and
    pending facts that are wider than ``audience`` (``acceptance`` as the caller decided, under
    the new policy version). Returns how many changed."""
    wider = [a.value for a in Audience if a.rank < audience.rank]
    claims = (
        session.execute(
            select(Claim)
            .where(
                Claim.profile_id == profile_id,
                Claim.category == category,
                Claim.status.in_([*_LIVE, ClaimStatus.PENDING_REVIEW.value]),
                Claim.audience.in_(wider),
            )
            .order_by(Claim.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )
    for claim in claims:
        old = claim.audience
        claim.audience = audience.value
        _event(
            session,
            claim,
            action=ClaimEventAction.AUDIENCE_CHANGED,
            actor=actor,
            acceptance=acceptance,
            policy_version=policy_version,
            changes={"audience": {"old": old, "new": audience.value}},
        )
    session.flush()
    return len(claims)


def remove(
    session: Session,
    *,
    profile_id: UUID,
    claim_id: UUID,
    actor: Actor,
    acceptance: AcceptanceMode,
    policy_version: int | None,
) -> Claim:
    """Remove a live or pending fact (status ``removed``)."""
    claim = lock_claim(session, profile_id, claim_id)
    if claim.status not in _LIVE and claim.status != ClaimStatus.PENDING_REVIEW.value:
        raise ClaimStateConflict(claim, "fact_not_removable")
    old = claim.status
    claim.status = ClaimStatus.REMOVED.value
    _event(
        session,
        claim,
        action=ClaimEventAction.REMOVED,
        actor=actor,
        acceptance=acceptance,
        policy_version=policy_version,
        changes=_status_change(old, ClaimStatus.REMOVED),
    )
    session.flush()
    session.refresh(claim)
    return claim


def approve(
    session: Session,
    *,
    profile_id: UUID,
    claim_id: UUID,
    actor: Actor,
    policy_version: int | None,
    check: Callable[[Claim], None],
) -> Claim:
    """Owner approves a pending fact: it becomes ``active`` (explicitly accepted) and, for a
    replacement, retires its target in the same transaction (409 if the target is no longer
    live). ``check`` runs on the locked row (e.g. Public still allowed) and may raise.

    Lock order: a replacement and its target are locked together in id order (``lock_claims``)
    BEFORE either is locked singly, the same global order ``add_facts`` uses, so an approval
    never holds the replacement while waiting for a target that a concurrent batch (which
    locked the target first) holds while waiting for the replacement."""
    # ``supersedes_id`` is fixed at insert, so this unlocked read names the right pair.
    supersedes_id = session.execute(
        select(Claim.supersedes_id).where(Claim.id == claim_id, Claim.profile_id == profile_id)
    ).scalar_one_or_none()
    if supersedes_id is not None:
        lock_claims(session, profile_id, {claim_id, supersedes_id})
    claim = lock_claim(session, profile_id, claim_id)
    if claim.status != ClaimStatus.PENDING_REVIEW.value:
        raise ClaimStateConflict(claim, "fact_not_pending")
    check(claim)
    target: Claim | None = None
    if claim.supersedes_id is not None:
        try:
            target = lock_claim(session, profile_id, claim.supersedes_id)
        except ClaimNotFound:
            raise ClaimStateConflict(claim, "superseded_fact_not_live") from None
        if target.status not in _LIVE:
            raise ClaimStateConflict(claim, "superseded_fact_not_live")
    old = claim.status
    claim.status = ClaimStatus.ACTIVE.value
    session.flush()
    _event(
        session,
        claim,
        action=ClaimEventAction.APPROVED,
        actor=actor,
        acceptance=AcceptanceMode.EXPLICIT,
        policy_version=policy_version,
        changes=_status_change(old, ClaimStatus.ACTIVE),
    )
    if target is not None:
        _supersede(
            session,
            target,
            actor=actor,
            acceptance=AcceptanceMode.EXPLICIT,
            policy_version=policy_version,
        )
    session.flush()
    session.refresh(claim)
    return claim


def reject(
    session: Session,
    *,
    profile_id: UUID,
    claim_id: UUID,
    actor: Actor,
    policy_version: int | None,
    acceptance: AcceptanceMode = AcceptanceMode.EXPLICIT,
) -> Claim:
    """Owner rejects a pending fact (status ``removed``); a replacement's target stays live."""
    claim = lock_claim(session, profile_id, claim_id)
    if claim.status != ClaimStatus.PENDING_REVIEW.value:
        raise ClaimStateConflict(claim, "fact_not_pending")
    old = claim.status
    claim.status = ClaimStatus.REMOVED.value
    _event(
        session,
        claim,
        action=ClaimEventAction.REJECTED,
        actor=actor,
        acceptance=acceptance,
        policy_version=policy_version,
        changes=_status_change(old, ClaimStatus.REMOVED),
    )
    session.flush()
    session.refresh(claim)
    return claim


def correct(
    session: Session,
    *,
    profile_id: UUID,
    claim_id: UUID,
    actor: Actor,
    build: Callable[[Claim], NewFact],
) -> Claim:
    """Owner correction: insert the replacement ``build(old)`` (explicitly accepted, live) and
    supersede the old live fact in the same transaction. Pending facts are approved or rejected
    instead (409)."""
    old = lock_claim(session, profile_id, claim_id)
    if old.status not in _LIVE:
        raise ClaimStateConflict(old, "fact_not_live")
    fact = build(old)
    return insert_fact(session, profile_id=profile_id, actor=actor, fact=fact)
