"""Questions: submit_question and the get_updates inbox. Owned by F7 (questions and leases).

Shared by the MCP tools (``revel.mcp.question_tools``) and the HTTP routes
(``revel.api.questions``); adapters stay thin. Policy: Notion "Ongoing Enrichment & Agent
Questions" (routing ladder, lifecycle, surprise-safe questions) and "Relationships, Discovery &
Privacy".

submit_question (routing ladder steps 1-2; step 3 is the lease work of #20, step 4 is off):

1. Only an assistant acting through an active delegation (``via`` mcp/api) may ask. The target
   (by handle or profile id) must not be the requester's own profile, the owner's current policy
   must have ``questions_enabled`` and the requester must hold a live ``connections`` or
   ``trusted_circle`` tier on it (OpenFGA; blocks remove every tier). Every refusal, including
   an unknown handle, is the same ``not_permitted`` so lookup reveals nothing.
2. ``requested_information`` is spoiler-checked against ``private_purpose`` before any model
   call, and identity-checked (claims, the requester's name/handle, the assistant's name) once
   those are read; unsafe text is rejected without echoing it. Candidate-idea terms the
   requester writes are allowed ("Does Alex already own binoculars?").
3. Step 1: the requester's currently permitted profile context (``visible_claims``) is returned.
4. Step 2: the owner's ACTIVE delegations are the candidate pool; Muse Spark (or the fallback)
   minimizes the question and picks respondents from it (``question_routing``). The text about
   to be published passes ``publishable_question_problems`` whichever path produced it.
5. One transaction: per-requester advisory lock, rate limit (``RATE_LIMIT_MAX_QUESTIONS`` per
   ``RATE_LIMIT_WINDOW``), re-check of the requester's tier and ``questions_enabled`` (closes
   the window across the model call), INSERT question, ``on_question_created`` (pre-commit, per
   F4's hook contract), confirm each chosen delegation with ``respond_allowed``
   (``question#respond``; an OpenFGA error rolls back with ``service_unavailable`` rather than
   committing an unrouted question), INSERT ``available`` assignments and ``made_available``
   events, commit.

get_updates: the calling delegation's ``available``/``released``/``claimed``/``waiting_for_user``
assignments on open questions whose requester can still view the profile (tier not ``none``).
Expiry is evaluated lazily HERE: an open question past ``expires_at`` is marked ``expired``
(``closed_at`` set, one ``expired`` event, guarded so concurrent readers write it once) and never
returned; a lapsed lease goes back to ``available`` (``expire_stale_leases``, shared with the
lease operations). ``respond`` is re-checked at read time (an OpenFGA outage
is ``service_unavailable``, not an empty inbox), so a disconnected delegation
or a changed ownership returns nothing. Each returned item gets a ``retrieved`` event.

Lock order (F4 gate): these transactions write only ``questions``, ``assignments`` and
``question_events`` and take a transaction-scoped advisory lock; they read (never lock)
``handles``, ``profiles``, ``users``, ``agents``, ``agent_delegations``, ``contribution_policies``
and ``claims``. They never write or row-lock ``connection_requests`` or ``agent_delegations``.

Privacy: respondents only ever see ``minimized_question``. ``requested_information``,
``private_purpose`` and ``escalation_context`` are never selected on respondent paths
(``private_purpose``/``escalation_context`` are deferred with raiseload and never undeferred
here). Events carry small structured metadata, never text. Requesters get a respondent COUNT,
never respondent identities.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import anyio
import anyio.to_thread
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from revel.auth import authz
from revel.auth.authz import (
    AuthorizationDenied,
    PrincipalLike,
    ViewerTier,
    resolve_viewer_tier,
)
from revel.auth.fga import FgaClient
from revel.auth.identity import Principal
from revel.auth.visibility import SharedClaim, visible_claims
from revel.db.enums import (
    AssignmentStatus,
    DelegationStatus,
    QuestionEventType,
    QuestionStatus,
)
from revel.db.models import (
    Agent,
    AgentDelegation,
    Assignment,
    ContributionPolicy,
    Handle,
    Profile,
    Question,
    QuestionEvent,
    User,
)
from revel.services import connections
from revel.services.identity import agent_label
from revel.services.muse_spark import MuseSparkClient
from revel.services.question_routing import (
    REQUESTED_INFORMATION_MAX_LENGTH,
    Candidate,
    PrivatePurpose,
    RoutingPath,
    UnsafeRequestedInformation,
    publishable_question_problems,
    route_question,
    validate_requested_information,
)

logger = logging.getLogger(__name__)

# --- Policy constants -------------------------------------------------------------------------

RATE_LIMIT_MAX_QUESTIONS = 10
RATE_LIMIT_WINDOW = timedelta(hours=1)
DEFAULT_DEADLINE = timedelta(hours=72)
MIN_DEADLINE = timedelta(hours=1)
MAX_DEADLINE = timedelta(days=14)
# Tiers that may ask questions about a profile (Public viewers and strangers may not).
REQUESTER_TIERS = frozenset({ViewerTier.connections, ViewerTier.trusted_circle})
# The routing ladder step a new question is placed at (step 1 is answered inline).
ROUTING_STEP_OWNER_AGENTS = 2
INBOX_ASSIGNMENT_STATUSES = (
    AssignmentStatus.AVAILABLE.value,
    AssignmentStatus.RELEASED.value,
    AssignmentStatus.CLAIMED.value,
    AssignmentStatus.WAITING_FOR_USER.value,
)
MAX_UPDATES = 50
# Overall cap on get_updates' per-assignment authorization checks (fails closed as 503).
GET_UPDATES_AUTHZ_DEADLINE_SECONDS = 10.0
_RATE_LOCK_PREFIX = "revel.questions.rate:"


# --- Errors -----------------------------------------------------------------------------------


class QuestionError(Exception):
    """A refused operation. ``code``/``message`` are safe to return; no object IDs or text."""

    code = "error"
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QuestionValidationError(QuestionError):
    code = "invalid_request"
    status_code = 422


class UnsafeRequestedInformationError(QuestionValidationError):
    code = "unsafe_requested_information"


class QuestionNotPermitted(QuestionError):
    code = "not_permitted"
    status_code = 403

    def __init__(self) -> None:
        super().__init__("You can't ask or read questions about this profile.")


class QuestionRateLimited(QuestionError):
    code = "rate_limited"
    status_code = 429

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            f"Too many questions: at most {RATE_LIMIT_MAX_QUESTIONS} per "
            f"{int(RATE_LIMIT_WINDOW.total_seconds() // 60)} minutes. Try again later."
        )
        self.retry_after_seconds = retry_after_seconds


class QuestionServiceUnavailable(QuestionError):
    code = "service_unavailable"
    status_code = 503

    def __init__(self) -> None:
        super().__init__("Questions are temporarily unavailable. Try again shortly.")


# --- Request / response schemas ---------------------------------------------------------------


class EscalationContext(BaseModel):
    """Stored with the question for Trusted Circle escalation (routing step 4, off in the MVP)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    urgency: Literal["low", "normal", "high"] | None = None
    notes: str | None = Field(default=None, max_length=500)


class SubmitQuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    handle: str | None = Field(default=None, max_length=64, description="Target's exact handle.")
    profile_id: UUID | None = Field(default=None, description="Target profile id (or handle).")
    requested_information: str = Field(
        min_length=1,
        max_length=REQUESTED_INFORMATION_MAX_LENGTH,
        description="Only what you want to know. Keep purpose details in private_purpose.",
    )
    private_purpose: PrivatePurpose = Field(default_factory=PrivatePurpose)
    expires_at: datetime | None = Field(default=None, description="Timezone-aware deadline.")
    expires_in_hours: float | None = Field(default=None, gt=0)
    escalation_permitted: bool = False
    escalation_context: EscalationContext | None = None

    @model_validator(mode="after")
    def _one_target_one_deadline(self) -> SubmitQuestionRequest:
        if (self.handle is None) == (self.profile_id is None):
            raise ValueError("give exactly one of handle or profile_id")
        if self.expires_at is not None and self.expires_in_hours is not None:
            raise ValueError("give at most one of expires_at or expires_in_hours")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("expires_at must include a timezone")
        return self


class ProfileContextClaim(BaseModel):
    """A fact the requester may already see (routing step 1). Mirrors F4's ``SharedClaim``."""

    id: UUID
    category: str
    statement: str
    details: dict[str, Any] | None
    audience: str
    evidence_type: str
    confidence: str
    observed_at: date | None
    observed_precision: str
    provisional: bool


class ProfileContext(BaseModel):
    tier: str
    claims: list[ProfileContextClaim]


class RoutingSummary(BaseModel):
    routing_step: int
    respondent_count: int
    minimizer: RoutingPath


class SubmitQuestionResponse(BaseModel):
    question_id: UUID
    status: str
    expires_at: datetime
    deadline_clamped: bool
    minimized_question: str
    routing: RoutingSummary
    profile_context: ProfileContext


class PersonRef(BaseModel):
    display_name: str | None
    handle: str | None


class AgentRef(BaseModel):
    name: str | None


class QuestionAssignmentItem(BaseModel):
    """An inbox item for a respondent. Only the purpose-minimized question; identity is
    composed by Revel from the authenticated requester, never from question text."""

    type: Literal["question_assignment"] = "question_assignment"
    assignment_id: UUID
    question_id: UUID
    status: str
    lease_version: int
    minimized_question: str
    requester: PersonRef
    acting_agent: AgentRef | None
    about: PersonRef
    expires_at: datetime
    lease_expires_at: datetime | None


# Inbox items are typed by ``type``; nudges and occasion items join this union later.
UpdateItem = QuestionAssignmentItem


class UpdatesResponse(BaseModel):
    items: list[UpdateItem]


# --- Dependencies -----------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class QuestionDeps:
    """What the services need. Adapters build it with ``deps_for(settings)``; tests inject."""

    sessionmaker: sessionmaker[Session]
    fga: FgaClient | None = None  # None: the client installed by configure_authz
    muse: MuseSparkClient | None = None
    now: Callable[[], datetime] = field(default=_utcnow)


_deps: dict[int, tuple[Any, QuestionDeps]] = {}


def configure_question_deps(settings: Any, deps: QuestionDeps) -> QuestionDeps:
    """Register ``deps`` for ``settings`` (tests; the app uses the default)."""
    _deps[id(settings)] = (settings, deps)
    return deps


def reset_question_deps() -> None:
    _deps.clear()


def deps_for(settings: Any) -> QuestionDeps:
    """Deps for ``settings``: the shared sessionmaker (no extra pool) and a Muse Spark client."""
    entry = _deps.get(id(settings))
    if entry is not None and entry[0] is settings:
        return entry[1]
    from revel.db.session import get_sessionmaker

    deps = QuestionDeps(
        sessionmaker=get_sessionmaker(), muse=MuseSparkClient.from_settings(settings)
    )
    _deps[id(settings)] = (settings, deps)
    return deps


# --- Authorization helpers --------------------------------------------------------------------


class RespondCheckUnavailable(Exception):
    """OpenFGA could not answer a ``respond`` check (unconfigured, timeout or error)."""


async def respond_allowed(
    principal: PrincipalLike, question_id: UUID, *, fga: FgaClient | None = None
) -> bool:
    """THE seam for ``question#respond``: F4's ``authz.can_respond`` (bounded at
    ``CHECK_TIMEOUT_SECONDS``). ``True``/``False`` is an answer; an inconsistent principal is a
    denial (``False``). ``AuthorizationCheckTimeout``, ``FgaConfigurationError`` and any SDK
    error are NOT a denial: they raise ``RespondCheckUnavailable``, which callers map to
    ``service_unavailable`` (with a rollback when writing), so an outage never commits an
    unrouted question or silently empties an inbox."""
    try:
        return await authz.can_respond(principal, question_id, fga=fga)
    except AuthorizationDenied:
        return False
    except Exception as exc:  # SDK errors carry object ids: keep only the class name
        logger.warning("respond check unavailable (%s)", type(exc).__name__)
        raise RespondCheckUnavailable() from None


async def requester_can_ask(
    requester_user_id: UUID, profile_id: UUID, *, fga: FgaClient | None = None
) -> bool:
    """Whether the requester (as a user) still holds a connection tier on the profile. An OpenFGA
    outage raises ``QuestionServiceUnavailable`` (never a silent hide or show)."""
    requester = Principal(user_id=requester_user_id, agent_id=None, delegation_id=None, via="ui")
    try:
        tier = await resolve_viewer_tier(fga, requester, profile_id)
    except AuthorizationDenied:
        return False
    except Exception as exc:
        raise _authz_unavailable(exc) from None
    return tier in REQUESTER_TIERS


def acting_agent_label(name: str | None, client_id: str | None) -> str:
    """The assistant app's registered name as plain display text (NOT verified identity): F2's
    ``agent_label`` (name, else an https client id's host, else "Assistant"). The single seam
    every question surface uses (inbox ``acting_agent``, candidate labels, identity terms)."""
    return agent_label(name, client_id or "")


def _respondent_principal(owner_user_id: UUID, candidate: Candidate) -> Principal:
    return Principal(
        user_id=owner_user_id,
        agent_id=candidate.agent_id,
        delegation_id=candidate.delegation_id,
        via="api",
    )


def _require_agent_principal(principal: PrincipalLike) -> UUID:
    """Questions are asked and answered by assistants acting through an active delegation."""
    if principal.via not in ("mcp", "api") or principal.delegation_id is None:
        raise QuestionNotPermitted()
    return principal.delegation_id


# --- Deadline ---------------------------------------------------------------------------------


def resolve_deadline(
    now: datetime, *, expires_at: datetime | None, expires_in_hours: float | None
) -> tuple[datetime, bool]:
    """The question's ``expires_at`` within ``[MIN_DEADLINE, MAX_DEADLINE]`` of ``now``
    (``DEFAULT_DEADLINE`` when neither is given), and whether it was clamped."""
    if expires_at is not None:
        requested = expires_at.astimezone(UTC)
    elif expires_in_hours is not None:
        if not math.isfinite(expires_in_hours):
            raise QuestionValidationError("expires_in_hours must be a finite number")
        hours = min(expires_in_hours, MAX_DEADLINE.total_seconds() / 3600 * 2)
        requested = now + timedelta(hours=hours)
    else:
        return now + DEFAULT_DEADLINE, False
    low, high = now + MIN_DEADLINE, now + MAX_DEADLINE
    clamped = min(max(requested, low), high)
    return clamped, clamped != requested


# --- submit_question --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Target:
    profile_id: UUID
    owner_user_id: UUID


@dataclass(frozen=True)
class _Prepared:
    target: _Target
    questions_enabled: bool
    candidates: tuple[Candidate, ...]
    identity_terms: tuple[str, ...]
    recent_count: int


def _find_target_by_profile(session: Session, profile_id: UUID) -> _Target | None:
    row = session.execute(
        select(Profile.id, Profile.user_id).where(Profile.id == profile_id)
    ).first()
    return None if row is None else _Target(profile_id=row.id, owner_user_id=row.user_id)


async def _find_target(
    session: Session, principal: Principal, request: SubmitQuestionRequest
) -> _Target | None:
    """The question's target, or ``None`` (not permitted) for every hidden case.

    A handle goes through F6's ``resolve_handle``: nonexistent, malformed, undiscoverable and
    blocking targets fail identically, before any OpenFGA work, and misses spend the caller's
    handle-lookup quota, so the questions path is no faster or cheaper enumeration oracle than
    the connections one (Privacy: hidden targets answer identically).
    """
    if request.profile_id is not None:
        return await _in_thread(lambda: _find_target_by_profile(session, request.profile_id))
    assert request.handle is not None
    try:
        resolved = await connections.resolve_handle(session, principal, handle=request.handle)
    except connections.RateLimited:
        raise QuestionRateLimited(int(connections.LOOKUP_WINDOW.total_seconds())) from None
    except connections.ConnectionsUnavailable as exc:
        raise _authz_unavailable(exc) from None
    except connections.ConnectionsError:  # TargetNotFound, NotAllowed: one indistinct answer
        return None
    if resolved.profile_id is None:
        return None
    return _Target(profile_id=resolved.profile_id, owner_user_id=resolved.user_id)


def _questions_enabled(session: Session, profile_id: UUID) -> bool:
    value = session.execute(
        select(ContributionPolicy.questions_enabled)
        .where(ContributionPolicy.profile_id == profile_id)
        .order_by(ContributionPolicy.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return True if value is None else bool(value)  # column default: enabled


def _candidates(session: Session, owner_user_id: UUID) -> tuple[Candidate, ...]:
    """The owner's ACTIVE delegations, with opaque ids and non-sensitive labels."""
    rows = session.execute(
        select(AgentDelegation.id, AgentDelegation.agent_id, Agent.name, Agent.oauth_client_id)
        .join(Agent, Agent.id == AgentDelegation.agent_id)
        .where(
            AgentDelegation.user_id == owner_user_id,
            AgentDelegation.status == DelegationStatus.ACTIVE.value,
        )
        .order_by(AgentDelegation.connected_at, AgentDelegation.id)
    ).all()
    return tuple(
        Candidate(
            id=f"r{index}",
            delegation_id=row.id,
            agent_id=row.agent_id,
            label=acting_agent_label(row.name, row.oauth_client_id)[:60],
        )
        for index, row in enumerate(rows, start=1)
    )


def _person_terms(session: Session, user_id: UUID) -> tuple[str | None, str | None]:
    name = session.execute(select(User.display_name).where(User.id == user_id)).scalar_one_or_none()
    handle = session.execute(
        select(Handle.handle).where(Handle.user_id == user_id)
    ).scalar_one_or_none()
    return name, handle


def _identity_terms(
    session: Session, principal: PrincipalLike, owner_user_id: UUID
) -> tuple[str, ...]:
    """Requester display name, name parts and handle, and the acting assistant's name and label:
    forbidden in requested_information and in any published question. Name parts the owner
    shares (two people called Alex) are left out so the owner can still be named."""
    name, handle = _person_terms(session, principal.user_id)
    owner_name, owner_handle = _person_terms(session, owner_user_id)
    owner_parts = {p.lower() for p in (owner_name or "").split()} | {(owner_handle or "").lower()}
    terms: list[str] = [v for v in (name, handle) if v]
    if name:
        terms.extend(
            part for part in name.split() if len(part) >= 3 and part.lower() not in owner_parts
        )
    if principal.agent_id is not None:
        agent = session.execute(
            select(Agent.name, Agent.oauth_client_id).where(Agent.id == principal.agent_id)
        ).first()
        if agent is not None:
            if agent.name:
                terms.append(agent.name)
            label = acting_agent_label(agent.name, agent.oauth_client_id)
            if label != "Assistant":
                terms.append(label)
    return tuple(dict.fromkeys(t for t in terms if t.lower() not in owner_parts))


def _recent_count(session: Session, requester_user_id: UUID) -> int:
    return session.execute(
        select(func.count())
        .select_from(Question)
        .where(
            Question.requester_user_id == requester_user_id,
            Question.created_at > func.now() - RATE_LIMIT_WINDOW,
        )
    ).scalar_one()


def _retry_after_seconds(session: Session, requester_user_id: UUID) -> int:
    oldest, db_now = session.execute(
        select(func.min(Question.created_at), func.now()).where(
            Question.requester_user_id == requester_user_id,
            Question.created_at > func.now() - RATE_LIMIT_WINDOW,
        )
    ).one()
    if oldest is None:
        return 1
    return max(1, math.ceil((oldest + RATE_LIMIT_WINDOW - db_now).total_seconds()))


def _context(result: Any) -> ProfileContext:
    claims = [
        ProfileContextClaim(
            id=c.id,
            category=c.category,
            statement=c.statement,
            details=c.details,
            audience=c.audience,
            evidence_type=c.evidence_type,
            confidence=c.confidence,
            observed_at=c.observed_at,
            observed_precision=c.observed_precision,
            provisional=c.provisional,
        )
        for c in result.claims
        if isinstance(c, SharedClaim)
    ]
    return ProfileContext(tier=result.tier.value, claims=claims)


async def _in_thread[T](fn: Callable[[], T]) -> T:
    return await anyio.to_thread.run_sync(fn)


def _authz_unavailable(exc: BaseException) -> QuestionServiceUnavailable:
    logger.warning("questions: authorization unavailable (%s)", type(exc).__name__)
    return QuestionServiceUnavailable()


def _unsafe(exc: UnsafeRequestedInformation) -> UnsafeRequestedInformationError:
    return UnsafeRequestedInformationError(str(exc))


async def _requester_still_permitted(
    session: Session, principal: PrincipalLike, profile_id: UUID, fga: FgaClient | None
) -> bool:
    """Write-transaction re-check (closes the window across the model call): the requester
    still holds a requester tier and the owner's current policy still allows questions."""
    try:
        tier = await resolve_viewer_tier(fga, principal, profile_id)
    except AuthorizationDenied:
        return False
    except Exception as exc:
        raise _authz_unavailable(exc) from None
    if tier not in REQUESTER_TIERS:
        return False
    return await _in_thread(lambda: _questions_enabled(session, profile_id))


async def submit_question(
    principal: PrincipalLike, request: SubmitQuestionRequest, *, deps: QuestionDeps
) -> SubmitQuestionResponse:
    """Submit a purpose-bound question about a profile (see the module docstring)."""
    requester_delegation_id = _require_agent_principal(principal)
    purpose = request.private_purpose
    try:
        requested_information = validate_requested_information(
            request.requested_information, purpose
        )
    except UnsafeRequestedInformation as exc:
        raise _unsafe(exc) from None
    except ValueError as exc:
        raise QuestionValidationError(str(exc)) from None
    now = deps.now()
    expires_at, clamped = resolve_deadline(
        now, expires_at=request.expires_at, expires_in_hours=request.expires_in_hours
    )

    # Phase A: short read-only session. Target, policy, step-1 context (tier check), pool.
    with deps.sessionmaker() as session:
        target = await _find_target(session, principal, request)
        session.rollback()  # no transaction held across the OpenFGA checks
        if target is None or target.owner_user_id == principal.user_id:
            raise QuestionNotPermitted()
        try:
            context = await visible_claims(session, principal, target.profile_id, fga=deps.fga)
        except AuthorizationDenied:
            raise QuestionNotPermitted() from None
        except Exception as exc:  # FgaConfigurationError, timeouts, SDK errors (carry ids)
            raise _authz_unavailable(exc) from None
        if context.tier not in REQUESTER_TIERS:
            raise QuestionNotPermitted()

        def prepare() -> _Prepared:
            return _Prepared(
                target=target,
                questions_enabled=_questions_enabled(session, target.profile_id),
                candidates=_candidates(session, target.owner_user_id),
                identity_terms=_identity_terms(session, principal, target.owner_user_id),
                recent_count=_recent_count(session, principal.user_id),
            )

        prepared = await _in_thread(prepare)
        session.rollback()  # end the read transaction before the model call
    if not prepared.questions_enabled:
        raise QuestionNotPermitted()
    # Identity check needs the requester's and assistant's names (read above).
    try:
        validate_requested_information(
            requested_information, purpose, identity_terms=prepared.identity_terms
        )
    except UnsafeRequestedInformation as exc:
        raise _unsafe(exc) from None
    if prepared.recent_count >= RATE_LIMIT_MAX_QUESTIONS:
        with deps.sessionmaker() as session:
            retry = await _in_thread(lambda: _retry_after_seconds(session, principal.user_id))
        raise QuestionRateLimited(retry)

    # Phase B: routing (no database connection held).
    decision = await route_question(
        requested_information=requested_information,
        purpose=purpose,
        candidates=prepared.candidates,
        client=deps.muse,
        identity_terms=prepared.identity_terms,
    )
    # Final gate on the text respondents will see, whichever path produced it.
    problems = publishable_question_problems(
        decision.minimized_question,
        requested_information=requested_information,
        purpose=purpose,
        identity_terms=prepared.identity_terms,
    )
    if problems:
        raise _unsafe(UnsafeRequestedInformation(problems))

    # Phase C: one write transaction.
    question_id = uuid.uuid4()
    with deps.sessionmaker() as session:
        try:

            def lock_and_count() -> None:
                session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtext(f"{_RATE_LOCK_PREFIX}{principal.user_id}")
                        )
                    )
                )
                if _recent_count(session, principal.user_id) >= RATE_LIMIT_MAX_QUESTIONS:
                    raise QuestionRateLimited(_retry_after_seconds(session, principal.user_id))

            await _in_thread(lock_and_count)
            if not await _requester_still_permitted(
                session, principal, target.profile_id, deps.fga
            ):
                raise QuestionNotPermitted()

            def insert_question() -> None:
                session.add(
                    Question(
                        id=question_id,
                        profile_id=target.profile_id,
                        requester_user_id=principal.user_id,
                        requester_delegation_id=requester_delegation_id,
                        requested_information=requested_information,
                        private_purpose=purpose.model_dump(mode="json", exclude_none=True),
                        minimized_question=decision.minimized_question,
                        status=QuestionStatus.OPEN.value,
                        expires_at=expires_at,
                        escalation_permitted=request.escalation_permitted,
                        escalation_context=(
                            request.escalation_context.model_dump(mode="json", exclude_none=True)
                            if request.escalation_context is not None
                            else None
                        ),
                        routing_step=ROUTING_STEP_OWNER_AGENTS,
                    )
                )
                session.flush()

            await _in_thread(insert_question)
            try:
                synced = await authz.on_question_created(
                    question_id=question_id,
                    profile_id=target.profile_id,
                    requester_user_id=principal.user_id,
                )
            except Exception as exc:
                raise _authz_unavailable(exc) from None
            if synced is None:  # OpenFGA unconfigured: never publish an unreachable question
                raise QuestionServiceUnavailable()

            # An OpenFGA error is not a denial: roll back rather than commit an unrouted question.
            confirmed: list[Candidate] = []
            for candidate in decision.respondents:
                respondent = _respondent_principal(target.owner_user_id, candidate)
                try:
                    allowed = await respond_allowed(respondent, question_id, fga=deps.fga)
                except RespondCheckUnavailable as exc:
                    raise _authz_unavailable(exc) from None
                if allowed:
                    confirmed.append(candidate)

            def publish() -> None:
                details = {
                    **decision.event_details(),
                    "routing_step": ROUTING_STEP_OWNER_AGENTS,
                    "respondent_count": len(confirmed),
                    "via": principal.via,
                }
                assignment_ids = [uuid.uuid4() for _ in confirmed]
                for assignment_id, candidate in zip(assignment_ids, confirmed, strict=True):
                    session.add(
                        Assignment(
                            id=assignment_id,
                            question_id=question_id,
                            respondent_delegation_id=candidate.delegation_id,
                            status=AssignmentStatus.AVAILABLE.value,
                            lease_version=0,
                        )
                    )
                session.flush()
                for assignment_id in assignment_ids or [None]:
                    session.add(
                        QuestionEvent(
                            question_id=question_id,
                            assignment_id=assignment_id,
                            event_type=QuestionEventType.MADE_AVAILABLE.value,
                            actor_user_id=principal.user_id,
                            actor_delegation_id=requester_delegation_id,
                            lease_version=0 if assignment_id is not None else None,
                            details=details,
                            occurred_at=now,
                        )
                    )
                session.commit()

            await _in_thread(publish)
        except BaseException:
            session.rollback()
            raise

    return SubmitQuestionResponse(
        question_id=question_id,
        status=QuestionStatus.OPEN.value,
        expires_at=expires_at,
        deadline_clamped=clamped,
        minimized_question=decision.minimized_question,
        routing=RoutingSummary(
            routing_step=ROUTING_STEP_OWNER_AGENTS,
            respondent_count=len(confirmed),
            minimizer=decision.path,
        ),
        profile_context=_context(context),
    )


# --- get_updates ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _InboxRow:
    assignment_id: UUID
    question_id: UUID
    status: str
    lease_version: int
    lease_expires_at: datetime | None
    minimized_question: str | None
    expires_at: datetime
    profile_id: UUID
    requester_user_id: UUID
    requester_delegation_id: UUID | None


def _inbox_rows(session: Session, delegation_id: UUID) -> list[_InboxRow]:
    # Explicit columns only: never requested_information, private_purpose or escalation_context.
    rows = session.execute(
        select(
            Assignment.id,
            Assignment.question_id,
            Assignment.status,
            Assignment.lease_version,
            Assignment.lease_expires_at,
            Question.minimized_question,
            Question.expires_at,
            Question.profile_id,
            Question.requester_user_id,
            Question.requester_delegation_id,
        )
        .join(Question, Question.id == Assignment.question_id)
        .where(
            Assignment.respondent_delegation_id == delegation_id,
            Assignment.status.in_(INBOX_ASSIGNMENT_STATUSES),
            Question.status == QuestionStatus.OPEN.value,
        )
        .order_by(Question.expires_at, Assignment.id)
        .limit(MAX_UPDATES * 2)
    ).all()
    return [_InboxRow(*row) for row in rows]


def _expire_questions(
    session: Session, question_ids: set[UUID], now: datetime, *, detected_by: str = "get_updates"
) -> None:
    """Mark open questions past their deadline expired, once each (guarded UPDATE)."""
    for question_id in sorted(question_ids):
        result = session.execute(
            update(Question)
            .where(
                Question.id == question_id,
                Question.status == QuestionStatus.OPEN.value,
                Question.expires_at <= now,
            )
            .values(status=QuestionStatus.EXPIRED.value, closed_at=now)
        )
        if result.rowcount == 1:
            session.add(
                QuestionEvent(
                    question_id=question_id,
                    event_type=QuestionEventType.EXPIRED.value,
                    details={"reason": "deadline_passed", "detected_by": detected_by},
                    occurred_at=now,
                )
            )


def expire_question_if_due(
    session: Session, question_id: UUID, now: datetime, *, detected_by: str
) -> None:
    """Lazy question expiry for one question (no-op unless open and past ``expires_at``)."""
    _expire_questions(session, {question_id}, now, detected_by=detected_by)


HELD_LEASE_STATUSES = (
    AssignmentStatus.CLAIMED.value,
    AssignmentStatus.WAITING_FOR_USER.value,
)


def expire_stale_leases(
    session: Session, question_id: UUID, now: datetime, *, detected_by: str
) -> list[UUID]:
    """Lazy lease expiry for one question: every ``claimed``/``waiting_for_user`` lease whose
    ``lease_expires_at`` has passed goes back to ``available`` (``lease_version`` bumped, the
    lease and human-contact flag cleared) with a ``lease_expired`` event recording the previous
    status, but ONLY while the question is open and before its deadline. An expired question is
    never revived: its leases are left as they are (every later operation is refused because the
    question is not open). Each row is an optimistic ``UPDATE ... WHERE lease_version = :v`` so
    concurrent callers record each expiry once. Returns the expired assignment ids. The caller
    commits."""
    question = session.execute(
        select(Question.status, Question.expires_at).where(Question.id == question_id)
    ).first()
    if question is None or question.status != QuestionStatus.OPEN.value:
        return []
    if question.expires_at <= now:
        return []
    stale = session.execute(
        select(Assignment.id, Assignment.lease_version, Assignment.status, Assignment.human_contact)
        .where(
            Assignment.question_id == question_id,
            Assignment.status.in_(HELD_LEASE_STATUSES),
            Assignment.lease_expires_at <= now,
        )
        .order_by(Assignment.id)
    ).all()
    expired: list[UUID] = []
    for row in stale:
        result = session.execute(
            update(Assignment)
            .where(
                Assignment.id == row.id,
                Assignment.lease_version == row.lease_version,
                Assignment.status.in_(HELD_LEASE_STATUSES),
            )
            .values(
                status=AssignmentStatus.AVAILABLE.value,
                lease_version=row.lease_version + 1,
                lease_expires_at=None,
                claimed_at=None,
                human_contact=False,
            )
        )
        if result.rowcount == 1:
            session.add(
                QuestionEvent(
                    question_id=question_id,
                    assignment_id=row.id,
                    event_type=QuestionEventType.LEASE_EXPIRED.value,
                    lease_version=row.lease_version + 1,
                    details={
                        "previous_status": row.status,
                        "status": AssignmentStatus.LEASE_EXPIRED.value,
                        "human_contact": bool(row.human_contact),
                        "detected_by": detected_by,
                    },
                    occurred_at=now,
                )
            )
            expired.append(row.id)
    session.flush()
    return expired


def _people(
    session: Session, user_ids: set[UUID], delegation_ids: set[UUID]
) -> tuple[dict[UUID, PersonRef], dict[UUID, AgentRef]]:
    people: dict[UUID, PersonRef] = {}
    if user_ids:
        rows = session.execute(
            select(User.id, User.display_name, Handle.handle)
            .outerjoin(Handle, Handle.user_id == User.id)
            .where(User.id.in_(user_ids))
        ).all()
        people = {
            row.id: PersonRef(display_name=row.display_name, handle=row.handle) for row in rows
        }
    agents: dict[UUID, AgentRef] = {}
    if delegation_ids:
        rows = session.execute(
            select(AgentDelegation.id, Agent.name, Agent.oauth_client_id)
            .join(Agent, Agent.id == AgentDelegation.agent_id)
            .where(AgentDelegation.id.in_(delegation_ids))
        ).all()
        agents = {
            row.id: AgentRef(name=acting_agent_label(row.name, row.oauth_client_id)) for row in rows
        }
    return people, agents


async def get_updates(principal: PrincipalLike, *, deps: QuestionDeps) -> UpdatesResponse:
    """The calling delegation's typed inbox (question assignments in the MVP)."""
    delegation_id = _require_agent_principal(principal)
    now = deps.now()

    with deps.sessionmaker() as session:

        def read_and_expire() -> list[_InboxRow]:
            rows = _inbox_rows(session, delegation_id)
            expired = {row.question_id for row in rows if row.expires_at <= now}
            stale_leases = {
                row.question_id
                for row in rows
                if row.expires_at > now
                and row.status in HELD_LEASE_STATUSES
                and row.lease_expires_at is not None
                and row.lease_expires_at <= now
            }
            if expired:
                _expire_questions(session, expired, now)
            for question_id in sorted(stale_leases):
                expire_stale_leases(session, question_id, now, detected_by="get_updates")
            if expired or stale_leases:
                session.commit()
                if stale_leases:  # statuses and lease versions changed: read them again
                    rows = _inbox_rows(session, delegation_id)
                    session.rollback()
            else:
                session.rollback()
            return [row for row in rows if row.expires_at > now]

        try:
            live = await _in_thread(read_and_expire)
        except BaseException:
            session.rollback()
            raise

    # Re-check respond and the requester's connection tier outside any transaction. Public
    # visibility alone does not authorize an outstanding question or human outreach. Check the
    # requester as a user, with one result per (requester, profile).
    permitted: list[_InboxRow] = []
    requester_visible: dict[tuple[UUID, UUID], bool] = {}
    try:
        # One overall deadline across all per-assignment checks (each has its own timeout).
        with anyio.fail_after(GET_UPDATES_AUTHZ_DEADLINE_SECONDS):
            for row in live:
                try:
                    allowed = await respond_allowed(principal, row.question_id, fga=deps.fga)
                except RespondCheckUnavailable as exc:
                    raise _authz_unavailable(exc) from None
                if not allowed:
                    continue
                key = (row.requester_user_id, row.profile_id)
                if key not in requester_visible:
                    requester_visible[key] = await requester_can_ask(
                        row.requester_user_id, row.profile_id, fga=deps.fga
                    )
                if requester_visible[key]:
                    permitted.append(row)
                if len(permitted) >= MAX_UPDATES:
                    break
    except TimeoutError as exc:
        raise _authz_unavailable(exc) from None
    if not permitted:
        from revel.services.inbox_setup import record_successful_check

        await record_successful_check(principal, 0, deps.sessionmaker, now)
        return UpdatesResponse(items=[])

    profile_owners: dict[UUID, UUID] = {}
    with deps.sessionmaker() as session:

        def build_and_record() -> list[QuestionAssignmentItem]:
            owners = session.execute(
                select(Profile.id, Profile.user_id).where(
                    Profile.id.in_({row.profile_id for row in permitted})
                )
            ).all()
            profile_owners.update({row.id: row.user_id for row in owners})
            people, agents = _people(
                session,
                {row.requester_user_id for row in permitted} | set(profile_owners.values()),
                {row.requester_delegation_id for row in permitted if row.requester_delegation_id},
            )
            unknown = PersonRef(display_name=None, handle=None)
            items = [
                QuestionAssignmentItem(
                    assignment_id=row.assignment_id,
                    question_id=row.question_id,
                    status=row.status,
                    lease_version=row.lease_version,
                    minimized_question=row.minimized_question or "",
                    requester=people.get(row.requester_user_id, unknown),
                    acting_agent=(
                        agents.get(row.requester_delegation_id)
                        if row.requester_delegation_id
                        else None
                    ),
                    about=people.get(profile_owners.get(row.profile_id, row.profile_id), unknown),
                    expires_at=row.expires_at,
                    lease_expires_at=row.lease_expires_at,
                )
                for row in permitted
            ]
            for row in permitted:
                session.add(
                    QuestionEvent(
                        question_id=row.question_id,
                        assignment_id=row.assignment_id,
                        event_type=QuestionEventType.RETRIEVED.value,
                        actor_user_id=principal.user_id,
                        actor_delegation_id=delegation_id,
                        lease_version=row.lease_version,
                        details={"via": principal.via, "item_type": "question_assignment"},
                    )
                )
            session.commit()
            return items

        try:
            items = await _in_thread(build_and_record)
        except BaseException:
            session.rollback()
            raise
    from revel.services.inbox_setup import record_successful_check

    await record_successful_check(principal, len(items), deps.sessionmaker, now)
    return UpdatesResponse(items=items)
