"""Assignment leases: claim, renew, release, report user contact and decline. Owned by F7.

Shared by the MCP tools and HTTP routes (thin adapters). Policy: Notion "Ongoing Enrichment &
Agent Questions" (lifecycle model, task leases and user outreach).

Rules every operation follows:

- Only the assignment's own respondent delegation may act (anyone else gets ``not_found``, so
  ids reveal nothing), and ``question#respond`` is re-checked first through the
  ``respond_allowed`` seam (``not_permitted`` when denied, ``service_unavailable`` on an
  OpenFGA outage: never an allow).
- The question must be open and before its deadline. An open question past ``expires_at`` is
  marked ``expired`` lazily (``question_expired``); a question that is answered, declined,
  cancelled or closed is ``question_closed``. Lease expiry never revives a question.
- Lease expiry is lazy (``expire_stale_leases``): a lapsed lease goes back to ``available``
  with ``lease_version`` bumped and a ``lease_expired`` event, only while the question is
  open. Renew/release/report/answer on a lapsed lease is ``lease_expired``.
- Every state change is an optimistic ``UPDATE assignments ... WHERE id = :id AND
  lease_version = :v`` that bumps ``lease_version``; zero rows is ``stale_lease``. Callers
  that hold a lease must quote its current ``lease_version``.
- Human contact: one ``human_contact`` lease (``claimed``/``waiting_for_user``) per question,
  enforced by F3's partial unique index (a violation maps to ``human_contact_taken``) after a
  pre-check. The lease must be taken BEFORE the user is asked: a ``human_contact`` claim, or an
  in-place upgrade of one's own ``claimed`` lease; ``report_user_contact`` without it is
  ``human_contact_required`` and never acquires it. When the lease is taken, prior
  ``user_contact_reported`` events on the question are checked: if ANY assistant (this one
  included) contacted the user within ``OUTREACH_COOLDOWN`` the operation is
  ``outreach_cooldown``. Re-confirming an existing contact is not new outreach (it never resets
  the window) but counts against ``MAX_RENEWALS``. The cooldown is derived from events (no
  extra column). Human-contact paths take a per-question
  transaction advisory lock so the cooldown check and the write are serialized.

Statuses: ``available``/``released`` can be claimed; ``claimed``/``waiting_for_user`` hold a
lease; ``declined`` (this respondent will not answer) and ``completed`` (only by an accepted
answer, see ``revel.services.answers``) are terminal. When every assignment of an open question
is declined, the question becomes ``declined``.

Lock order (F4 gate): writes only ``assignments``, ``questions`` and ``question_events`` and
takes a transaction-scoped advisory lock; never writes or row-locks ``connection_requests`` or
``agent_delegations``. Events carry small structured metadata, never text.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ColumnElement, and_, false, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from revel.auth.authz import PrincipalLike
from revel.db.enums import AssignmentStatus, QuestionEventType, QuestionStatus
from revel.db.models import Assignment, Question, QuestionEvent
from revel.services.questions import (
    HELD_LEASE_STATUSES,
    QuestionDeps,
    QuestionError,
    QuestionNotPermitted,
    RespondCheckUnavailable,
    _authz_unavailable,
    _in_thread,
    _require_agent_principal,
    expire_question_if_due,
    expire_stale_leases,
    respond_allowed,
)

# --- Policy constants -------------------------------------------------------------------------

LEASE_DURATION = timedelta(minutes=15)
# How long a lease waits for the user after an agent reports contacting them (response window).
HUMAN_RESPONSE_WINDOW = timedelta(hours=24)
# Renewals allowed per claim (counted from ``renewed`` events since ``claimed_at``).
MAX_RENEWALS = 8
# No new outreach while another assistant contacted the user about this question this recently.
OUTREACH_COOLDOWN = timedelta(hours=24)
CLAIMABLE_STATUSES = (AssignmentStatus.AVAILABLE.value, AssignmentStatus.RELEASED.value)
ACTIVE_ASSIGNMENT_STATUSES = (*CLAIMABLE_STATUSES, *HELD_LEASE_STATUSES)
HUMAN_CONTACT_INDEX = "uq_assignments_question_id_human_contact"
_OUTREACH_LOCK_PREFIX = "revel.questions.outreach:"


# --- Errors -----------------------------------------------------------------------------------


class AssignmentNotFound(QuestionError):
    code = "not_found"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("No such assignment for this assistant.")


class LeaseConflict(QuestionError):
    status_code = 409


class StaleLease(LeaseConflict):
    code = "stale_lease"

    def __init__(self) -> None:
        super().__init__(
            "The lease changed since you read it (stale lease_version). Call get_updates."
        )


class LeaseNotHeld(LeaseConflict):
    code = "lease_not_held"

    def __init__(self, message: str = "You do not hold a lease on this assignment.") -> None:
        super().__init__(message)


class LeaseExpired(LeaseConflict):
    code = "lease_expired"

    def __init__(self) -> None:
        super().__init__("Your lease expired. Claim the assignment again if it is available.")


class AlreadyClaimed(LeaseConflict):
    code = "already_claimed"

    def __init__(self) -> None:
        super().__init__("This assignment is already claimed. Use your lease or wait for it.")


class HumanContactRequired(LeaseConflict):
    code = "human_contact_required"

    def __init__(self) -> None:
        super().__init__(
            "Claim with human_contact=true (or upgrade your claim) before asking your user."
        )


class RenewalLimitReached(LeaseConflict):
    code = "renewal_limit"

    def __init__(self) -> None:
        super().__init__(f"This lease was already renewed {MAX_RENEWALS} times.")


class HumanContactTaken(LeaseConflict):
    code = "human_contact_taken"

    def __init__(self) -> None:
        super().__init__("Another assistant holds the lease to contact the user.")


class OutreachCooldown(LeaseConflict):
    code = "outreach_cooldown"

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            "Another assistant contacted the user about this question recently. "
            "Do not contact them again yet."
        )
        self.retry_after_seconds = retry_after_seconds


class QuestionExpired(LeaseConflict):
    code = "question_expired"

    def __init__(self) -> None:
        super().__init__("The question's deadline has passed.")


class QuestionClosed(LeaseConflict):
    code = "question_closed"

    def __init__(self) -> None:
        super().__init__("The question is no longer open.")


# --- Schemas ----------------------------------------------------------------------------------


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    human_contact: bool = Field(
        default=False, description="Take the exclusive lease to ask the user directly."
    )
    lease_version: int | None = Field(
        default=None, ge=0, description="Optional: the lease_version you last saw."
    )


class LeaseVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_version: int = Field(ge=0)


class UserContactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_version: int = Field(ge=0)
    delivery_confirmed: bool = Field(
        description="True only if the user is known to have received the message."
    )


DeclineReason = Literal["cannot_answer", "insufficient_context", "not_appropriate", "other"]


class DeclineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_version: int | None = Field(
        default=None, ge=0, description="Required when you hold a lease on the assignment."
    )
    reason: DeclineReason = "cannot_answer"


class LeaseResponse(BaseModel):
    assignment_id: UUID
    question_id: UUID
    status: str
    lease_version: int
    lease_expires_at: datetime | None
    human_contact: bool
    renewals_remaining: int | None = None
    # Times the user was already contacted about this question (any assistant): check before
    # asking them again.
    prior_user_contacts: int = 0


# --- Internals --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    id: UUID
    question_id: UUID
    respondent_delegation_id: UUID
    status: str
    lease_version: int
    lease_expires_at: datetime | None
    claimed_at: datetime | None
    human_contact: bool


def _load(session: Session, assignment_id: UUID) -> _Row | None:
    row = session.execute(
        select(
            Assignment.id,
            Assignment.question_id,
            Assignment.respondent_delegation_id,
            Assignment.status,
            Assignment.lease_version,
            Assignment.lease_expires_at,
            Assignment.claimed_at,
            Assignment.human_contact,
        ).where(Assignment.id == assignment_id)
    ).first()
    return None if row is None else _Row(*row)


async def load_own_assignment(
    principal: PrincipalLike, assignment_id: UUID, deps: QuestionDeps
) -> _Row:
    """The caller's own assignment, after re-checking ``respond`` (outside any transaction)."""
    delegation_id = _require_agent_principal(principal)
    with deps.sessionmaker() as session:
        row = await _in_thread(lambda: _load(session, assignment_id))
        session.rollback()
    if row is None or row.respondent_delegation_id != delegation_id:
        raise AssignmentNotFound()
    try:
        allowed = await respond_allowed(principal, row.question_id, fga=deps.fga)
    except RespondCheckUnavailable as exc:
        raise _authz_unavailable(exc) from None
    if not allowed:
        raise QuestionNotPermitted()
    return row


def _question_of(session: Session, assignment_id: UUID) -> UUID:
    return session.execute(
        select(Assignment.question_id).where(Assignment.id == assignment_id)
    ).scalar_one()


def open_question_expiry(session: Session, question_id: UUID, now: datetime) -> datetime:
    """The question's deadline if it is open and not yet due; otherwise records lazy expiry
    (committed) and raises ``question_expired`` / ``question_closed``."""
    question = session.execute(
        select(Question.status, Question.expires_at).where(Question.id == question_id)
    ).one()
    if question.status == QuestionStatus.OPEN.value and question.expires_at > now:
        return question.expires_at
    if question.status == QuestionStatus.OPEN.value:
        expire_question_if_due(session, question_id, now, detected_by="lease")
        session.commit()
        raise QuestionExpired()
    if question.status == QuestionStatus.EXPIRED.value:
        raise QuestionExpired()
    raise QuestionClosed()


def held_lease(session: Session, assignment_id: UUID, lease_version: int, now: datetime) -> _Row:
    """Re-read the assignment inside the write transaction and require an unexpired lease at
    ``lease_version``. A lapsed lease is expired lazily (committed) and refused."""
    row = _load(session, assignment_id)
    assert row is not None
    if row.status in HELD_LEASE_STATUSES and (
        row.lease_expires_at is None or row.lease_expires_at <= now
    ):
        expire_stale_leases(session, row.question_id, now, detected_by="lease")
        session.commit()
        raise LeaseExpired()
    if row.status not in HELD_LEASE_STATUSES:
        if row.lease_version != lease_version and row.status in CLAIMABLE_STATUSES:
            # A lease that lapsed and was returned to the pool reads as expired, not missing.
            raise LeaseExpired()
        raise LeaseNotHeld()
    if row.lease_version != lease_version:
        raise StaleLease()
    return row


def _lock_outreach(session: Session, question_id: UUID) -> None:
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"{_OUTREACH_LOCK_PREFIX}{question_id}")))
    )


def _new_contact() -> ColumnElement[bool]:
    """A ``user_contact_reported`` event that recorded NEW outreach (re-confirmations of an
    existing contact carry ``re_report: true`` and neither count nor reset the cooldown)."""
    return and_(
        QuestionEvent.event_type == QuestionEventType.USER_CONTACT_REPORTED.value,
        func.coalesce(QuestionEvent.details["re_report"].as_boolean(), false()).is_(False),
    )


def _prior_contacts(session: Session, question_id: UUID) -> int:
    return session.execute(
        select(func.count())
        .select_from(QuestionEvent)
        .where(QuestionEvent.question_id == question_id, _new_contact())
    ).scalar_one()


def check_outreach_allowed(
    session: Session, question_id: UUID, assignment_id: UUID, now: datetime
) -> None:
    """Refuse new outreach while another assignment holds the human-contact lease (the unique
    index is the write-time backstop) or while ANY assistant, this one included, contacted the
    user about this question within ``OUTREACH_COOLDOWN``. Re-confirmations do not count."""
    holder = session.execute(
        select(Assignment.id).where(
            Assignment.question_id == question_id,
            Assignment.id != assignment_id,
            Assignment.human_contact.is_(True),
            Assignment.status.in_(HELD_LEASE_STATUSES),
        )
    ).first()
    if holder is not None:
        raise HumanContactTaken()
    last = session.execute(
        select(func.max(QuestionEvent.occurred_at)).where(
            QuestionEvent.question_id == question_id, _new_contact()
        )
    ).scalar_one()
    if last is not None and last > now - OUTREACH_COOLDOWN:
        retry = max(1, math.ceil((last + OUTREACH_COOLDOWN - now).total_seconds()))
        raise OutreachCooldown(retry)


def _is_human_contact_violation(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == HUMAN_CONTACT_INDEX


def _renewals_since_claim(session: Session, row: _Row) -> int:
    if row.claimed_at is None:
        return 0
    return session.execute(
        select(func.count())
        .select_from(QuestionEvent)
        .where(
            QuestionEvent.assignment_id == row.id,
            or_(
                QuestionEvent.event_type == QuestionEventType.RENEWED.value,
                and_(
                    QuestionEvent.event_type == QuestionEventType.USER_CONTACT_REPORTED.value,
                    QuestionEvent.details["re_report"].as_boolean().is_(True),
                ),
            ),
            QuestionEvent.occurred_at >= row.claimed_at,
        )
    ).scalar_one()


def _event(
    principal: PrincipalLike,
    row: _Row,
    event_type: QuestionEventType,
    lease_version: int,
    now: datetime,
    **details: object,
) -> QuestionEvent:
    return QuestionEvent(
        question_id=row.question_id,
        assignment_id=row.id,
        event_type=event_type.value,
        actor_user_id=principal.user_id,
        actor_delegation_id=principal.delegation_id,
        lease_version=lease_version,
        details={"via": principal.via, **details},
        occurred_at=now,
    )


def _versioned_update(
    session: Session, row: _Row, expected_statuses: tuple[str, ...], now: datetime, **values: object
) -> int:
    """The optimistic write: bump ``lease_version`` iff it still equals the version read."""
    conditions = [
        Assignment.id == row.id,
        Assignment.lease_version == row.lease_version,
        Assignment.status.in_(expected_statuses),
    ]
    if set(expected_statuses) <= set(HELD_LEASE_STATUSES):
        conditions.append(Assignment.lease_expires_at > now)
    result = session.execute(
        update(Assignment).where(*conditions).values(lease_version=row.lease_version + 1, **values)
    )
    if result.rowcount != 1:
        raise StaleLease()
    return row.lease_version + 1


def _response(
    row: _Row,
    *,
    status: str,
    lease_version: int,
    lease_expires_at: datetime | None,
    human_contact: bool,
    renewals_remaining: int | None = None,
    prior_user_contacts: int = 0,
) -> LeaseResponse:
    return LeaseResponse(
        assignment_id=row.id,
        question_id=row.question_id,
        status=status,
        lease_version=lease_version,
        lease_expires_at=lease_expires_at,
        human_contact=human_contact,
        renewals_remaining=renewals_remaining,
        prior_user_contacts=prior_user_contacts,
    )


async def _write[T](deps: QuestionDeps, work: Callable[[Session], T]) -> T:
    """Run ``work(session)`` in one transaction in a worker thread; roll back on any error.
    Errors raised after an explicit commit inside ``work`` (lazy expiry) keep that commit."""
    with deps.sessionmaker() as session:

        def run() -> T:
            try:
                return work(session)
            except IntegrityError as exc:
                session.rollback()
                if _is_human_contact_violation(exc):
                    raise HumanContactTaken() from None
                raise
            except BaseException:
                session.rollback()
                raise

        return await _in_thread(run)


# --- Operations -------------------------------------------------------------------------------


async def claim_assignment(
    principal: PrincipalLike, assignment_id: UUID, request: ClaimRequest, *, deps: QuestionDeps
) -> LeaseResponse:
    """Atomically claim an ``available``/``released`` assignment for ``LEASE_DURATION``
    (capped at the question deadline). ``human_contact`` also takes the exclusive
    human-contact lease, after the outreach checks.

    In-place upgrade: ``human_contact=true`` with the current ``lease_version`` on this
    delegation's own active ``claimed`` lease (without human contact) runs the same checks
    under the per-question lock (respond re-check, ``human_contact_taken``, the outreach
    cooldown including this assistant), sets ``human_contact``, bumps ``lease_version``, keeps
    ``lease_expires_at`` and records ``claimed`` with ``upgrade: true``. A claim without
    ``human_contact`` on a lease already held is ``already_claimed``."""
    await load_own_assignment(principal, assignment_id, deps)
    now = deps.now()

    def _upgrade(session: Session, row: _Row) -> LeaseResponse:
        check_outreach_allowed(session, row.question_id, row.id, now)
        version = _versioned_update(
            session, row, (AssignmentStatus.CLAIMED.value,), now, human_contact=True
        )
        session.flush()  # surfaces the human-contact unique index violation here
        session.add(
            _event(
                principal,
                row,
                QuestionEventType.CLAIMED,
                version,
                now,
                human_contact=True,
                upgrade=True,
            )
        )
        prior = _prior_contacts(session, row.question_id)
        used = _renewals_since_claim(session, row)
        session.commit()
        return _response(
            row,
            status=AssignmentStatus.CLAIMED.value,
            lease_version=version,
            lease_expires_at=row.lease_expires_at,
            human_contact=True,
            renewals_remaining=MAX_RENEWALS - used,
            prior_user_contacts=prior,
        )

    def work(session: Session) -> LeaseResponse:
        row = _load(session, assignment_id)
        assert row is not None
        if request.human_contact:
            _lock_outreach(session, row.question_id)
        deadline = open_question_expiry(session, row.question_id, now)
        if expire_stale_leases(session, row.question_id, now, detected_by="claim"):
            session.commit()
            row = _load(session, assignment_id)
            assert row is not None
            if request.human_contact:
                _lock_outreach(session, row.question_id)
        if request.lease_version is not None and request.lease_version != row.lease_version:
            raise StaleLease()
        if row.status not in CLAIMABLE_STATUSES:
            if (
                request.human_contact
                and request.lease_version is not None
                and row.status == AssignmentStatus.CLAIMED.value
                and not row.human_contact
            ):
                return _upgrade(session, row)
            if row.status in HELD_LEASE_STATUSES:
                raise AlreadyClaimed()
            raise QuestionClosed()
        if request.human_contact:
            check_outreach_allowed(session, row.question_id, row.id, now)
        expires = min(now + LEASE_DURATION, deadline)
        version = _versioned_update(
            session,
            row,
            CLAIMABLE_STATUSES,
            now,
            status=AssignmentStatus.CLAIMED.value,
            claimed_at=now,
            lease_expires_at=expires,
            human_contact=request.human_contact,
        )
        session.flush()  # surfaces the human-contact unique index violation here
        session.add(
            _event(
                principal,
                row,
                QuestionEventType.CLAIMED,
                version,
                now,
                human_contact=request.human_contact,
            )
        )
        prior = _prior_contacts(session, row.question_id)
        session.commit()
        return _response(
            row,
            status=AssignmentStatus.CLAIMED.value,
            lease_version=version,
            lease_expires_at=expires,
            human_contact=request.human_contact,
            renewals_remaining=MAX_RENEWALS,
            prior_user_contacts=prior,
        )

    return await _write(deps, work)


async def renew_assignment(
    principal: PrincipalLike,
    assignment_id: UUID,
    request: LeaseVersionRequest,
    *,
    deps: QuestionDeps,
) -> LeaseResponse:
    """Extend an unexpired lease by its duration (``LEASE_DURATION``, or
    ``HUMAN_RESPONSE_WINDOW`` while ``waiting_for_user``), capped at the question deadline and
    at ``MAX_RENEWALS`` per claim. Stale or expired leases are refused."""
    await load_own_assignment(principal, assignment_id, deps)
    now = deps.now()

    def work(session: Session) -> LeaseResponse:
        question_id = _question_of(session, assignment_id)
        deadline = open_question_expiry(session, question_id, now)
        row = held_lease(session, assignment_id, request.lease_version, now)
        used = _renewals_since_claim(session, row)
        if used >= MAX_RENEWALS:
            raise RenewalLimitReached()
        window = (
            HUMAN_RESPONSE_WINDOW
            if row.status == AssignmentStatus.WAITING_FOR_USER.value
            else LEASE_DURATION
        )
        expires = min(now + window, deadline)
        version = _versioned_update(
            session, row, HELD_LEASE_STATUSES, now, lease_expires_at=expires
        )
        session.add(_event(principal, row, QuestionEventType.RENEWED, version, now))
        session.commit()
        return _response(
            row,
            status=row.status,
            lease_version=version,
            lease_expires_at=expires,
            human_contact=row.human_contact,
            renewals_remaining=MAX_RENEWALS - used - 1,
        )

    return await _write(deps, work)


async def release_assignment(
    principal: PrincipalLike,
    assignment_id: UUID,
    request: LeaseVersionRequest,
    *,
    deps: QuestionDeps,
) -> LeaseResponse:
    """Give up an unexpired lease: the assignment becomes ``released`` (claimable again, by
    this assistant later) and any human-contact lease is freed. The question stays open."""
    await load_own_assignment(principal, assignment_id, deps)
    now = deps.now()

    def work(session: Session) -> LeaseResponse:
        open_question_expiry(session, _question_of(session, assignment_id), now)
        row = held_lease(session, assignment_id, request.lease_version, now)
        version = _versioned_update(
            session,
            row,
            HELD_LEASE_STATUSES,
            now,
            status=AssignmentStatus.RELEASED.value,
            lease_expires_at=None,
            claimed_at=None,
            human_contact=False,
        )
        session.add(
            _event(
                principal,
                row,
                QuestionEventType.RELEASED,
                version,
                now,
                previous_status=row.status,
            )
        )
        session.commit()
        return _response(
            row,
            status=AssignmentStatus.RELEASED.value,
            lease_version=version,
            lease_expires_at=None,
            human_contact=False,
        )

    return await _write(deps, work)


async def report_user_contact(
    principal: PrincipalLike,
    assignment_id: UUID,
    request: UserContactRequest,
    *,
    deps: QuestionDeps,
) -> LeaseResponse:
    """Record that this assistant asked its user. Requires the caller's lease to hold the
    exclusive human-contact lease already (claimed or upgraded with ``human_contact=true``,
    which is where the outreach checks run); otherwise ``human_contact_required``, and it never
    acquires that lease itself. Moves to ``waiting_for_user`` for ``HUMAN_RESPONSE_WINDOW``
    (capped at the question deadline) and records ``user_contacted_at``,
    ``contact_delivery_confirmed`` and a ``user_contact_reported`` event.

    A repeat report while already ``waiting_for_user`` is a re-confirmation, not new outreach:
    it may update ``contact_delivery_confirmed`` but keeps ``user_contacted_at`` and
    ``lease_expires_at`` (the response window is not reset), counts against ``MAX_RENEWALS``
    and is recorded with ``re_report: true`` (which never counts toward or resets the
    cooldown)."""
    await load_own_assignment(principal, assignment_id, deps)
    now = deps.now()

    def work(session: Session) -> LeaseResponse:
        row = _load(session, assignment_id)
        assert row is not None
        _lock_outreach(session, row.question_id)
        deadline = open_question_expiry(session, row.question_id, now)
        if expire_stale_leases(session, row.question_id, now, detected_by="user_contact"):
            session.commit()  # frees lapsed human-contact leases (possibly this one)
            _lock_outreach(session, row.question_id)
        row = held_lease(session, assignment_id, request.lease_version, now)
        if not row.human_contact:
            raise HumanContactRequired()
        prior = _prior_contacts(session, row.question_id)
        re_report = row.status == AssignmentStatus.WAITING_FOR_USER.value
        values: dict[str, object]
        if re_report:
            if _renewals_since_claim(session, row) >= MAX_RENEWALS:
                raise RenewalLimitReached()
            expires = row.lease_expires_at
            values = {"contact_delivery_confirmed": request.delivery_confirmed}
        else:
            # The outreach checks ran when the human-contact lease was claimed (before the user
            # was asked); this records what happened and never acquires the lease.
            expires = min(now + HUMAN_RESPONSE_WINDOW, deadline)
            values = {
                "status": AssignmentStatus.WAITING_FOR_USER.value,
                "user_contacted_at": now,
                "contact_delivery_confirmed": request.delivery_confirmed,
                "lease_expires_at": expires,
            }
        version = _versioned_update(session, row, HELD_LEASE_STATUSES, now, **values)
        session.flush()
        session.add(
            _event(
                principal,
                row,
                QuestionEventType.USER_CONTACT_REPORTED,
                version,
                now,
                delivery_confirmed=request.delivery_confirmed,
                prior_user_contacts=prior,
                re_report=re_report,
            )
        )
        session.commit()
        return _response(
            row,
            status=AssignmentStatus.WAITING_FOR_USER.value,
            lease_version=version,
            lease_expires_at=expires,
            human_contact=True,
            prior_user_contacts=prior if re_report else prior + 1,
        )

    return await _write(deps, work)


async def decline_assignment(
    principal: PrincipalLike, assignment_id: UUID, request: DeclineRequest, *, deps: QuestionDeps
) -> LeaseResponse:
    """This assistant will not answer: the assignment becomes ``declined`` (terminal). A held
    lease requires its current ``lease_version``. When no assignment of the question is left
    active, the question becomes ``declined`` (closed) with a question-level event."""
    await load_own_assignment(principal, assignment_id, deps)
    now = deps.now()

    def work(session: Session) -> LeaseResponse:
        row = _load(session, assignment_id)
        assert row is not None
        # Serializes declines (and outreach) per question so concurrent last declines see each
        # other's writes and the question closes.
        _lock_outreach(session, row.question_id)
        row = _load(session, assignment_id)
        assert row is not None
        open_question_expiry(session, row.question_id, now)
        if row.status in HELD_LEASE_STATUSES:
            if request.lease_version is None:
                raise LeaseNotHeld("Quote your lease_version to decline a claimed assignment.")
            row = held_lease(session, assignment_id, request.lease_version, now)
            expected: tuple[str, ...] = HELD_LEASE_STATUSES
        elif row.status in CLAIMABLE_STATUSES:
            if request.lease_version is not None and request.lease_version != row.lease_version:
                raise StaleLease()
            expected = CLAIMABLE_STATUSES
        else:
            raise QuestionClosed()
        version = _versioned_update(
            session,
            row,
            expected,
            now,
            status=AssignmentStatus.DECLINED.value,
            lease_expires_at=None,
            claimed_at=None,
            human_contact=False,
        )
        session.add(
            _event(principal, row, QuestionEventType.DECLINED, version, now, reason=request.reason)
        )
        remaining = session.execute(
            select(func.count())
            .select_from(Assignment)
            .where(
                Assignment.question_id == row.question_id,
                Assignment.status.in_(ACTIVE_ASSIGNMENT_STATUSES),
            )
        ).scalar_one()
        if remaining == 0:
            closed = session.execute(
                update(Question)
                .where(Question.id == row.question_id, Question.status == QuestionStatus.OPEN.value)
                .values(status=QuestionStatus.DECLINED.value, closed_at=now)
            )
            if closed.rowcount == 1:
                session.add(
                    QuestionEvent(
                        question_id=row.question_id,
                        event_type=QuestionEventType.DECLINED.value,
                        details={"reason": "all_respondents_declined"},
                        occurred_at=now,
                    )
                )
        session.commit()
        return _response(
            row,
            status=AssignmentStatus.DECLINED.value,
            lease_version=version,
            lease_expires_at=None,
            human_contact=False,
        )

    return await _write(deps, work)
