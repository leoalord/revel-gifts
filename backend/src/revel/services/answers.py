"""Evidence-aware answers: submit_answer (respondent), get_answer, get_question_status and
cancel_question (requester). Owned by F7.

Policy: Notion "Ongoing Enrichment & Agent Questions" (answer evidence, lifecycle) and
"Relationships, Discovery & Privacy".

submit_answer(assignment_id, lease_version, body, evidence[]): the assignment's own delegation
(``respond`` re-checked) holding an unexpired lease at ``lease_version`` on an open question
before its deadline. Late submissions are ``question_expired``; stale or lapsed leases are
``stale_lease`` / ``lease_expired``. Evidence (at least one item) is either a ``claim_id`` of a
LIVE (active/provisional) claim of the question's profile, or inline labels: ``category``
(F5's ``validate_category``), ``proposed_audience`` (never more open than the owner's CURRENT
``effective_max_audience`` for the category), ``evidence_type`` and an optional
``observed_month``. No quotes or source details. The answer's audience is the most restrictive
of its evidence (claims use their own audience). One transaction: the assignment becomes
``completed`` (optimistic, version-checked), the question ``answered``, the accepted answer and
its evidence rows are inserted and an ``answered`` event is written. Completion happens only
here, by an accepted answer.

get_answer(question_id): for the requester only (another caller gets ``not_found``). Every
retrieval re-checks, and on ANY failure returns ``no_shareable_answer`` with no reason:
F4's ``can_release_answer`` (about, read_answer, a current tier) and then the answer's
audience, recomputed as the most restrictive of the stored audience and each cited claim's
CURRENT audience, within the tier; every cited claim still in ``visible_claims`` for the
requester; every inline item's proposed audience still within the CURRENT policy maximum for its
category (inline evidence is never broadened: its proposed audience is always what is used) and
within the tier. A success records ``answer_retrieved``; retrieval never changes question or
lease state and never completes anything. A timely accepted answer stays retrievable after the
question deadline. An OpenFGA outage is ``service_unavailable`` (never an allow).

get_question_status(question_id): the requester's view of their question (open, answered,
expired, declined, ...) without respondent details. An open question past its deadline is
marked expired lazily.

cancel_question(question_id): the requester withdraws their own open question (another caller
gets ``not_found``). The question becomes ``cancelled`` (final) with a ``cancelled`` event;
it leaves every inbox and later lease or answer calls are ``question_closed``. Assignments are
left as they are, as with expiry. Repeating it returns the same result; an answered, declined
or closed question is ``question_closed`` and an expired one ``question_expired``. A race with
an answer has one winner: the guarded UPDATE only moves an open question.

Lock order (F4 gate): writes only ``assignments``, ``questions``, ``answers``,
``answer_evidence`` and ``question_events``; reads (never locks) ``claims`` and
``contribution_policies``; never touches ``connection_requests`` or ``agent_delegations``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from revel.auth import authz
from revel.auth.authz import AuthorizationDenied, PrincipalLike, ViewerTier
from revel.auth.visibility import TIER_RANK, SharedClaim, owner_claims, visible_claims
from revel.db.enums import (
    LIVE_CLAIM_STATUSES,
    AnswerStatus,
    AssignmentStatus,
    Audience,
    EvidenceType,
    ObservedPrecision,
    QuestionEventType,
    QuestionStatus,
)
from revel.db.models import (
    Agent,
    AgentDelegation,
    Answer,
    AnswerEvidence,
    Assignment,
    Question,
    QuestionEvent,
)
from revel.db.models.questions import ANSWER_BODY_MAX_LENGTH
from revel.services.categories import CategoryError
from revel.services.profiles import effective_max_audience, validate_category
from revel.services.question_leases import (
    HELD_LEASE_STATUSES,
    QuestionClosed,
    QuestionExpired,
    StaleLease,
    _versioned_update,
    _write,
    check_write_expiry,
    held_lease,
    load_own_assignment,
    open_question_expiry,
)
from revel.services.questions import (
    AgentRef,
    QuestionDeps,
    QuestionError,
    QuestionNotPermitted,
    QuestionValidationError,
    _authz_unavailable,
    _in_thread,
    acting_agent_label,
    expire_question_if_due,
)

logger = logging.getLogger(__name__)

MAX_EVIDENCE_ITEMS = 10


# --- Errors -----------------------------------------------------------------------------------


class InvalidEvidence(QuestionValidationError):
    code = "invalid_evidence"


class QuestionNotFound(QuestionError):
    code = "not_found"
    status_code = 404

    def __init__(self) -> None:
        super().__init__("No such question for you.")


# --- Schemas ----------------------------------------------------------------------------------


class ClaimEvidenceIn(BaseModel):
    """Cite an existing fact on the profile (Revel already knows its audience)."""

    model_config = ConfigDict(extra="forbid")

    claim_id: UUID


class InlineEvidenceIn(BaseModel):
    """Minimal labels for a fact not on the profile. No quotes, sources or extra context."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str = Field(min_length=1, max_length=64)
    proposed_audience: Audience
    evidence_type: EvidenceType
    observed_month: Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] | None = (
        Field(default=None, description="Approximate month observed, YYYY-MM.")
    )


EvidenceIn = ClaimEvidenceIn | InlineEvidenceIn


class SubmitAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    lease_version: int = Field(ge=0)
    body: str = Field(min_length=1, max_length=ANSWER_BODY_MAX_LENGTH)
    evidence: list[EvidenceIn] = Field(min_length=1, max_length=MAX_EVIDENCE_ITEMS)


class SubmitAnswerResponse(BaseModel):
    answer_id: UUID
    question_id: UUID
    assignment_id: UUID
    question_status: str
    assignment_status: str
    lease_version: int
    audience: str
    evidence_count: int


class EvidenceLabel(BaseModel):
    kind: Literal["claim", "inline"]
    claim_id: UUID | None = None
    category: str
    audience: str
    evidence_type: str
    observed_at: date | None = None
    observed_precision: str


class AnswerOut(BaseModel):
    body: str
    audience: str
    answered_at: datetime
    respondent_agent: AgentRef | None
    evidence: list[EvidenceLabel]


class GetAnswerResponse(BaseModel):
    question_id: UUID
    question_status: str
    # "answer": released below; "no_shareable_answer": an answer may exist but cannot be shared
    # now (never says why); "not_answered": no accepted answer.
    result: Literal["answer", "no_shareable_answer", "not_answered"]
    answer: AnswerOut | None = None


class QuestionStatusResponse(BaseModel):
    question_id: UUID
    status: str
    expires_at: datetime
    created_at: datetime
    closed_at: datetime | None
    answer_available: bool


class GetQuestionResponse(QuestionStatusResponse):
    result: Literal["answer", "no_shareable_answer", "not_answered"]
    answer: AnswerOut | None = None


# --- submit_answer ----------------------------------------------------------------------------


def _observed(month: str | None) -> tuple[date | None, str]:
    if month is None:
        return None, ObservedPrecision.UNKNOWN.value
    year, mon = (int(part) for part in month.split("-"))
    return date(year, mon, 1), ObservedPrecision.MONTH.value


@dataclass(frozen=True)
class _ValidEvidence:
    rows: list[AnswerEvidence]
    audience: Audience


async def _live_claim_audiences(
    session: Session, principal: PrincipalLike, profile_id: UUID, deps: QuestionDeps
) -> dict[UUID, Audience]:
    """The profile's LIVE claims and their audiences, read through F4's ``owner_claims`` as the
    respondent (an owner's assistant). Denied is ``not_permitted``; an outage is 503."""
    try:
        claims = await owner_claims(
            session, principal, profile_id, statuses=LIVE_CLAIM_STATUSES, fga=deps.fga
        )
    except AuthorizationDenied:
        raise QuestionNotPermitted() from None
    except Exception as exc:
        raise _authz_unavailable(exc) from None
    return {claim.id: Audience(claim.audience) for claim in claims}


async def _validate_evidence(
    session: Session,
    principal: PrincipalLike,
    profile_id: UUID,
    items: list[EvidenceIn],
    now: datetime,
    deps: QuestionDeps,
) -> _ValidEvidence:
    """Validate every item (all problems reported by index, no ids or values echoed)."""
    problems: list[str] = []
    rows: list[AnswerEvidence] = []
    audiences: list[Audience] = []
    claims: dict[UUID, Audience] = {}
    if any(isinstance(item, ClaimEvidenceIn) for item in items):
        claims = await _live_claim_audiences(session, principal, profile_id, deps)
    seen: set[UUID] = set()
    for index, item in enumerate(items):
        if isinstance(item, ClaimEvidenceIn):
            if item.claim_id in seen:
                problems.append(f"evidence[{index}]: duplicate claim")
                continue
            seen.add(item.claim_id)
            audience = claims.get(item.claim_id)
            if audience is None:
                # Not on this profile, removed, superseded or pending: one indistinct reason.
                problems.append(f"evidence[{index}]: claim_not_usable")
                continue
            audiences.append(audience)
            rows.append(AnswerEvidence(profile_id=profile_id, claim_id=item.claim_id))
            continue
        try:
            spec = validate_category(item.category)
            maximum = await effective_max_audience(session, profile_id, spec.id)
        except CategoryError as exc:
            problems.append(f"evidence[{index}]: {exc.reason_code}")
            continue
        if item.proposed_audience.rank < maximum.rank:
            problems.append(
                f"evidence[{index}]: audience_too_open (at most {maximum.value} for "
                f"{spec.id} under the owner's policy)"
            )
            continue
        observed_at, precision = _observed(item.observed_month)
        if observed_at is not None and observed_at > now.date():
            problems.append(f"evidence[{index}]: observed_month is in the future")
            continue
        audiences.append(item.proposed_audience)
        rows.append(
            AnswerEvidence(
                profile_id=profile_id,
                category=spec.id,
                proposed_audience=item.proposed_audience.value,
                evidence_type=item.evidence_type.value,
                observed_at=observed_at,
                observed_precision=precision,
            )
        )
    if problems:
        raise InvalidEvidence("; ".join(problems))
    return _ValidEvidence(rows=rows, audience=max(audiences, key=lambda a: a.rank))


async def submit_answer(
    principal: PrincipalLike,
    assignment_id: UUID,
    request: SubmitAnswerRequest,
    *,
    deps: QuestionDeps,
) -> SubmitAnswerResponse:
    """Complete the caller's lease with an accepted, evidence-backed answer (module docs)."""
    own = await load_own_assignment(principal, assignment_id, deps)
    now = deps.now()
    profile_id = await _profile_of(deps, own.question_id)

    with deps.sessionmaker() as session:
        try:
            # Read-side checks and evidence validation (F5 helpers are async, so this runs
            # here rather than inside the threaded write below; the write re-checks state).
            await _in_thread(lambda: _precheck(session, assignment_id, request.lease_version, now))
            valid = await _validate_evidence(
                session, principal, profile_id, request.evidence, now, deps
            )
        finally:
            session.rollback()

    # Evidence validation can take time. Re-check authorization and sample time again at write.
    await load_own_assignment(principal, assignment_id, deps)
    answer_id = uuid4()

    def work(session: Session) -> SubmitAnswerResponse:
        now = deps.now()
        deadline = open_question_expiry(session, own.question_id, now)
        row = held_lease(session, assignment_id, request.lease_version, now)
        version = _versioned_update(
            session,
            row,
            HELD_LEASE_STATUSES,
            now,
            status=AssignmentStatus.COMPLETED.value,
            completed_at=now,
        )
        answered = session.execute(
            update(Question)
            .where(
                Question.id == row.question_id,
                Question.status == QuestionStatus.OPEN.value,
                Question.expires_at > now,
            )
            .values(status=QuestionStatus.ANSWERED.value)
        )
        if answered.rowcount != 1:
            raise StaleLease()  # another answer won the race; nothing is written
        # Both guarded updates may have waited on row locks. If time elapsed while waiting,
        # fail the submission and roll them both back before writing an answer or event.
        now = deps.now()
        check_write_expiry(now, deadline, row.lease_expires_at)
        session.add(
            Answer(
                id=answer_id,
                question_id=row.question_id,
                profile_id=profile_id,
                assignment_id=row.id,
                respondent_user_id=principal.user_id,
                respondent_delegation_id=principal.delegation_id,
                body=request.body,
                audience=valid.audience.value,
                status=AnswerStatus.ACCEPTED.value,
            )
        )
        session.flush()
        for evidence in valid.rows:
            evidence.answer_id = answer_id
            session.add(evidence)
        claim_refs = sum(1 for e in valid.rows if e.claim_id is not None)
        session.add(
            QuestionEvent(
                question_id=row.question_id,
                assignment_id=row.id,
                event_type=QuestionEventType.ANSWERED.value,
                actor_user_id=principal.user_id,
                actor_delegation_id=principal.delegation_id,
                lease_version=version,
                details={
                    "via": principal.via,
                    "audience": valid.audience.value,
                    "claim_evidence": claim_refs,
                    "inline_evidence": len(valid.rows) - claim_refs,
                    "human_contact": row.human_contact,
                },
                occurred_at=now,
            )
        )
        session.commit()
        return SubmitAnswerResponse(
            answer_id=answer_id,
            question_id=row.question_id,
            assignment_id=row.id,
            question_status=QuestionStatus.ANSWERED.value,
            assignment_status=AssignmentStatus.COMPLETED.value,
            lease_version=version,
            audience=valid.audience.value,
            evidence_count=len(valid.rows),
        )

    return await _write(deps, work)


def _precheck(session: Session, assignment_id: UUID, lease_version: int, now: datetime) -> None:
    """Refuse late / stale / lapsed submissions before validating evidence (lazy expiry of a
    question or lease is committed by the helpers)."""
    question_id = session.execute(
        select(Assignment.question_id).where(Assignment.id == assignment_id)
    ).scalar_one()
    open_question_expiry(session, question_id, now)
    held_lease(session, assignment_id, lease_version, now)


async def _profile_of(deps: QuestionDeps, question_id: UUID) -> UUID:
    with deps.sessionmaker() as session:
        profile_id = await _in_thread(
            lambda: session.execute(
                select(Question.profile_id).where(Question.id == question_id)
            ).scalar_one()
        )
        session.rollback()
    return profile_id


# --- Requester reads --------------------------------------------------------------------------


@dataclass(frozen=True)
class _OwnQuestion:
    id: UUID
    profile_id: UUID
    status: str
    expires_at: datetime
    created_at: datetime
    closed_at: datetime | None
    answer_id: UUID | None

    def response(self) -> QuestionStatusResponse:
        return QuestionStatusResponse(
            question_id=self.id,
            status=self.status,
            expires_at=self.expires_at,
            created_at=self.created_at,
            closed_at=self.closed_at,
            answer_available=self.answer_id is not None,
        )


def _own_question(
    session: Session, principal: PrincipalLike, question_id: UUID, now: datetime
) -> _OwnQuestion:
    """The requester's own question (``not_found`` otherwise), with lazy question expiry."""
    # Read the accepted answer ID and status in one statement so a concurrent answer
    # cannot produce a response that mixes the open and answered snapshots.
    answer_id = (
        select(Answer.id)
        .where(Answer.question_id == Question.id, Answer.status == AnswerStatus.ACCEPTED.value)
        .scalar_subquery()
    )
    # Explicit columns only: never private_purpose or escalation_context.
    cols = (
        Question.id,
        Question.profile_id,
        Question.status,
        Question.expires_at,
        Question.created_at,
        Question.closed_at,
        answer_id,
        Question.requester_user_id,
    )
    row = session.execute(select(*cols).where(Question.id == question_id)).first()
    if row is None or row.requester_user_id != principal.user_id:
        raise QuestionNotFound()
    if row.status == QuestionStatus.OPEN.value and row.expires_at <= now:
        expire_question_if_due(session, question_id, now, detected_by="requester")
        session.commit()
        row = session.execute(select(*cols).where(Question.id == question_id)).one()
    return _OwnQuestion(*row[:7])


@dataclass(frozen=True)
class _StoredAnswer:
    id: UUID
    body: str
    audience: Audience
    created_at: datetime
    respondent_delegation_id: UUID | None
    evidence: list[AnswerEvidence]
    agent_label: str | None


def _stored_answer(session: Session, answer_id: UUID) -> _StoredAnswer | None:
    answer = session.execute(
        select(Answer).where(Answer.id == answer_id, Answer.status == AnswerStatus.ACCEPTED.value)
    ).scalar_one_or_none()
    if answer is None:
        return None
    evidence = list(
        session.execute(
            select(AnswerEvidence)
            .where(AnswerEvidence.answer_id == answer.id)
            # Claim references first, then inline labels (rows of one answer share created_at).
            .order_by(
                AnswerEvidence.claim_id.is_(None),
                AnswerEvidence.category,
                AnswerEvidence.id,
            )
        ).scalars()
    )
    label = None
    if answer.respondent_delegation_id is not None:
        agent = session.execute(
            select(Agent.name, Agent.oauth_client_id)
            .join(AgentDelegation, AgentDelegation.agent_id == Agent.id)
            .where(AgentDelegation.id == answer.respondent_delegation_id)
        ).first()
        if agent is not None:
            label = acting_agent_label(agent.name, agent.oauth_client_id)
    return _StoredAnswer(
        id=answer.id,
        body=answer.body,
        audience=Audience(answer.audience),
        created_at=answer.created_at,
        respondent_delegation_id=answer.respondent_delegation_id,
        evidence=evidence,
        agent_label=label,
    )


async def get_question_status(
    principal: PrincipalLike, question_id: UUID, *, deps: QuestionDeps
) -> QuestionStatusResponse:
    """The requester's question status, without respondent details."""
    now = deps.now()
    with deps.sessionmaker() as session:

        def read() -> QuestionStatusResponse:
            try:
                return _own_question(session, principal, question_id, now).response()
            finally:
                session.rollback()

        return await _in_thread(read)


async def cancel_question(
    principal: PrincipalLike, question_id: UUID, *, deps: QuestionDeps
) -> QuestionStatusResponse:
    """Withdraw the requester's own open question. Final, and safe to repeat."""
    now = deps.now()

    def response(question: _OwnQuestion, closed_at: datetime | None) -> QuestionStatusResponse:
        return QuestionStatusResponse(
            question_id=question.id,
            status=QuestionStatus.CANCELLED.value,
            expires_at=question.expires_at,
            created_at=question.created_at,
            closed_at=closed_at,
            answer_available=False,  # Only an open question can be cancelled.
        )

    def work(session: Session) -> QuestionStatusResponse:
        question = _own_question(session, principal, question_id, now)
        if question.status == QuestionStatus.CANCELLED.value:
            return response(question, question.closed_at)
        if question.status == QuestionStatus.EXPIRED.value:
            raise QuestionExpired()
        if question.status != QuestionStatus.OPEN.value:
            raise QuestionClosed()
        cancelled = session.execute(
            update(Question)
            .where(
                Question.id == question_id,
                Question.status == QuestionStatus.OPEN.value,
                Question.expires_at > now,
            )
            .values(status=QuestionStatus.CANCELLED.value, closed_at=now)
        )
        if cancelled.rowcount != 1:
            raise QuestionClosed()  # An answer won the race; nothing is written.
        session.add(
            QuestionEvent(
                question_id=question_id,
                event_type=QuestionEventType.CANCELLED.value,
                actor_user_id=principal.user_id,
                actor_delegation_id=principal.delegation_id,
                details={"via": principal.via},
                occurred_at=now,
            )
        )
        session.commit()
        return response(question, now)

    return await _write(deps, work)


async def _releasable(
    session: Session,
    principal: PrincipalLike,
    question: _OwnQuestion,
    stored: _StoredAnswer,
    deps: QuestionDeps,
) -> list[EvidenceLabel] | None:
    """Evidence labels when the answer may be released NOW, else ``None``. OpenFGA outages
    raise ``QuestionServiceUnavailable``."""
    try:
        tier = await authz.can_release_answer(
            principal, question.id, question.profile_id, fga=deps.fga
        )
    except AuthorizationDenied:
        return None
    except Exception as exc:  # AuthorizationCheckTimeout, FgaConfigurationError, SDK errors
        raise _authz_unavailable(exc) from None
    if tier is None or tier is ViewerTier.none:
        return None
    tier_rank = TIER_RANK[tier]
    audience_rank = stored.audience.rank
    if audience_rank > tier_rank:
        return None

    labels: list[EvidenceLabel] = []
    if any(e.claim_id is not None for e in stored.evidence):
        try:
            visible = await visible_claims(session, principal, question.profile_id, fga=deps.fga)
        except AuthorizationDenied:
            return None
        except Exception as exc:
            raise _authz_unavailable(exc) from None
        # The requester is never the owner (submit_question refuses one's own profile), so
        # these are SharedClaim projections; anything else is not releasable.
        by_id = {c.id: c for c in visible.claims if isinstance(c, SharedClaim)}
    else:
        by_id = {}
    for evidence in stored.evidence:
        if evidence.claim_id is not None:
            shown = by_id.get(evidence.claim_id)
            if shown is None:
                return None
            claim = shown
            current = Audience(claim.audience)
            audience_rank = max(audience_rank, current.rank)
            labels.append(
                EvidenceLabel(
                    kind="claim",
                    claim_id=evidence.claim_id,
                    category=claim.category,
                    audience=current.value,
                    evidence_type=claim.evidence_type,
                    observed_at=claim.observed_at,
                    observed_precision=claim.observed_precision,
                )
            )
            continue
        assert evidence.category is not None and evidence.proposed_audience is not None
        proposed = Audience(evidence.proposed_audience)
        try:
            maximum = await effective_max_audience(session, question.profile_id, evidence.category)
        except CategoryError:
            return None
        # Never broadened: the proposed audience is what applies, and only while the current
        # policy still allows at least that restrictive a label.
        if proposed.rank < maximum.rank or proposed.rank > tier_rank:
            return None
        audience_rank = max(audience_rank, proposed.rank)
        labels.append(
            EvidenceLabel(
                kind="inline",
                category=evidence.category,
                audience=proposed.value,
                evidence_type=evidence.evidence_type or "",
                observed_at=evidence.observed_at,
                observed_precision=evidence.observed_precision or ObservedPrecision.UNKNOWN.value,
            )
        )
    if audience_rank > tier_rank:
        return None
    return labels


def _effective_audience(stored: _StoredAnswer, labels: list[EvidenceLabel]) -> Audience:
    return max(
        [stored.audience, *(Audience(label.audience) for label in labels)], key=lambda a: a.rank
    )


async def get_question(
    principal: PrincipalLike, question_id: UUID, *, deps: QuestionDeps
) -> GetQuestionResponse:
    """Read one requester status snapshot and release its answer if re-checks pass."""
    now = deps.now()
    with deps.sessionmaker() as session:
        try:
            question = await _in_thread(lambda: _own_question(session, principal, question_id, now))
            status = question.response().model_dump()
            if question.answer_id is None:
                return GetQuestionResponse(**status, result="not_answered")
            answer_id = question.answer_id
            stored = await _in_thread(lambda: _stored_answer(session, answer_id))
            session.rollback()
            if stored is None:
                return GetQuestionResponse(**status, result="no_shareable_answer")
            labels = await _releasable(session, principal, question, stored, deps)
            session.rollback()
        except BaseException:
            session.rollback()
            raise
    if labels is None:
        return GetQuestionResponse(**status, result="no_shareable_answer")

    with deps.sessionmaker() as session:

        def record() -> None:
            try:
                session.add(
                    QuestionEvent(
                        question_id=question.id,
                        event_type=QuestionEventType.ANSWER_RETRIEVED.value,
                        actor_user_id=principal.user_id,
                        actor_delegation_id=principal.delegation_id,
                        details={"via": principal.via, "evidence_count": len(labels)},
                        occurred_at=now,
                    )
                )
                session.commit()
            except BaseException:
                session.rollback()
                raise

        await _in_thread(record)
    return GetQuestionResponse(
        **status,
        result="answer",
        answer=AnswerOut(
            body=stored.body,
            audience=_effective_audience(stored, labels).value,
            answered_at=stored.created_at,
            respondent_agent=AgentRef(name=stored.agent_label) if stored.agent_label else None,
            evidence=labels,
        ),
    )


async def get_answer(
    principal: PrincipalLike, question_id: UUID, *, deps: QuestionDeps
) -> GetAnswerResponse:
    """Release the accepted answer to the requester if every re-check passes (module docs)."""
    question = await get_question(principal, question_id, deps=deps)
    return GetAnswerResponse(
        question_id=question.question_id,
        question_status=question.status,
        result=question.result,
        answer=question.answer,
    )
