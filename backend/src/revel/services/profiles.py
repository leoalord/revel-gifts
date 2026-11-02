"""Profiles and contributions (F5, RG-18 / Build Tasks #9): contribution policy, ``add_facts``,
owner reads with a contribution summary, corrections, audience changes, removal and review.

Contract: ``docs/contracts/f5-profiles.md``. Product policy lives in Notion (main page "Profile
fact data model"; Privacy "Contribution policy").

Shared behavior for the MCP tools (``revel.mcp.profile_tools``) and the HTTP routes
(``revel.api.profiles``), which stay thin. Rules this module keeps:

- Claim READS go only through F4's ``visible_claims`` / ``owner_claims``; this module never
  queries the ``claims`` table. Claim WRITES live in ``revel.services.claim_writes`` and run
  only after ``authz.require_contribute`` passed for the caller's own profile.
- Every operation acts on the principal's OWN profile (MVP: third-party contributions are out
  of scope); a fact id of another profile is a 404, indistinguishable from a missing one.
- Agents (any principal with a delegation) never write the policy, approve or reject, and
  only narrow audiences. Policy writes are browser-only (``via="ui"``, no delegation).
- Per-profile serialization: policy changes hold ``profiles`` ``FOR UPDATE`` across the version
  computation; contributions and fact changes hold it ``FOR SHARE``, so they always see (and
  cite) the current policy version.
- Public opt-in (F4 hook timing contract): withdrawal deletes the ``public_viewer`` tuples AFTER
  the policy INSERT and BEFORE commit (a failure rolls back); enabling grants after commit, under
  a fresh ``FOR UPDATE`` on the profile and only if the current policy is still opted in, so a
  later withdrawal cannot be overtaken by this grant.

The database layer is synchronous SQLAlchemy: session work runs in worker threads
(``anyio.to_thread``) while the caller awaits, so a session is never used concurrently.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import aiohttp
import anyio.to_thread
from openfga_sdk.exceptions import OpenApiException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from revel.auth import authz
from revel.auth.authz import AuthorizationDenied, PrincipalLike
from revel.auth.fga import FgaConfigurationError
from revel.auth.visibility import on_profile_created, owner_claims
from revel.db.enums import (
    LIVE_CLAIM_STATUSES,
    AcceptanceMode,
    Audience,
    ClaimStatus,
    Confidence,
    EvidenceType,
    ObservedPrecision,
)
from revel.db.models import Agent, AgentDelegation, Claim, ClaimEvent, ContributionPolicy, Profile
from revel.db.models.profile import STATEMENT_MAX_LENGTH
from revel.db.session import get_sessionmaker
from revel.services import claim_writes
from revel.services.categories import (
    CATEGORIES,
    CATEGORY_BY_ID,
    CategoryError,
    CategorySpec,
    ForbiddenCategory,
    InvalidField,
    UnknownCategory,
    validate_category,
    validate_details,
    validate_statement,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AddFactsResult",
    "AuthorizationUnavailable",
    "FactConflict",
    "FactInput",
    "FactNotFound",
    "FactOutcome",
    "ForbiddenCategory",
    "NotAllowed",
    "PolicyInvalid",
    "PolicyVersionConflict",
    "UnknownCategory",
    "add_facts",
    "effective_max_audience",
    "ensure_profile",
    "validate_category",
]

MAX_FACTS_PER_BATCH = 20
DEFAULT_SUMMARY_WINDOW = timedelta(days=7)
SUMMARY_RECENT_LIMIT = 50
_LIVE = frozenset(LIVE_CLAIM_STATUSES)
DEFAULT_FACT_STATUSES: tuple[ClaimStatus, ...] = (*LIVE_CLAIM_STATUSES, ClaimStatus.PENDING_REVIEW)

# --- Errors -----------------------------------------------------------------------------------


class ProfileServiceError(Exception):
    """Base class for errors adapters map to HTTP statuses / MCP tool errors."""


class FactNotFound(ProfileServiceError):
    """404: no such fact on the caller's profile (never reveals another profile's facts)."""


class NotAllowed(ProfileServiceError):
    """403: the operation is not allowed for this principal (e.g. an agent widening)."""


class AuthorizationUnavailable(ProfileServiceError):
    """503: OpenFGA unconfigured, unreachable or timed out. Carries no SDK detail."""


class FactConflict(ProfileServiceError):
    """409: the fact is not in a state that allows the change; carries the current fact."""

    def __init__(self, fact: FactOut, reason: str) -> None:
        super().__init__(reason)
        self.fact = fact
        self.reason = reason


class PolicyVersionConflict(ProfileServiceError):
    """409: ``expected_version`` is not the current version; carries the current policy."""

    def __init__(self, policy: PolicyResponse) -> None:
        super().__init__("policy_version_conflict")
        self.policy = policy


class PolicyInvalid(ProfileServiceError):
    """400: field errors in a policy update (``[{"loc": [...], "msg": ...}]``)."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("invalid policy")
        self.errors = errors


class InvalidRequest(ProfileServiceError):
    """400: field errors in a fact change (``[{"loc": [...], "msg": ...}]``)."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("invalid request")
        self.errors = errors


# OpenFGA failures that mean "authorization unavailable" (fail closed, generic 503).
_AUTHZ_UNAVAILABLE: tuple[type[BaseException], ...] = (
    FgaConfigurationError,
    TimeoutError,  # includes authz.AuthorizationCheckTimeout
    OpenApiException,
    aiohttp.ClientError,
    OSError,
)


async def _authz_call[T](call: Any) -> T:
    """Await an F4 check or hook, mapping OpenFGA failures to ``AuthorizationUnavailable``.
    ``AuthorizationDenied`` propagates unchanged."""
    try:
        return await call
    except AuthorizationDenied:
        raise
    except _AUTHZ_UNAVAILABLE as exc:
        logger.warning("Authorization unavailable (%s)", type(exc).__name__)
        raise AuthorizationUnavailable() from None


# --- Sessions ---------------------------------------------------------------------------------

_sessionmaker_provider: Callable[[], sessionmaker[Session]] = get_sessionmaker


def configure_sessionmaker(provider: Callable[[], sessionmaker[Session]] | None) -> None:
    """Override the session factory provider (tests); ``None`` restores ``get_sessionmaker``."""
    global _sessionmaker_provider
    _sessionmaker_provider = provider or get_sessionmaker


@asynccontextmanager
async def session_scope() -> AsyncIterator[Session]:
    """A session from the shared ``get_sessionmaker`` (no extra engines or pools). Rolled back
    (if still open) and closed in a worker thread."""
    session = _sessionmaker_provider()()
    try:
        yield session
    finally:
        await _in_thread(session.close)


async def _in_thread[T](fn: Callable[[], T]) -> T:
    return await anyio.to_thread.run_sync(fn)


# --- Models -----------------------------------------------------------------------------------


class FactInput(BaseModel):
    """One contributed fact (``add_facts``). Semantic checks (category, statement length,
    details, dates) happen per item, so one bad item does not fail the batch."""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(description="Category id from get_contribution_policy.")
    statement: str = Field(
        description="Short actionable fact, 1-500 characters. Never raw conversation."
    )
    details: dict[str, Any] | None = Field(
        default=None, description="Structured details per the category's details_schema."
    )
    audience: Audience | None = Field(
        default=None, description="Defaults to the category's effective audience."
    )
    evidence_type: EvidenceType
    confidence: Confidence = Confidence.UNKNOWN
    observed_at: date | None = Field(default=None, description="ISO date of the evidence.")
    observed_precision: ObservedPrecision | None = Field(
        default=None, description="'unknown' iff no observed_at; defaults to 'day' with a date."
    )
    supersedes_id: UUID | None = Field(
        default=None, description="A live fact this one corrects or replaces."
    )
    involves_others: bool = Field(
        default=False,
        description=(
            "True when the fact involves other people (e.g. a partner's plans). Defaults the "
            "audience to only_me and always holds an assistant's contribution for review."
        ),
    )


Outcome = Literal["accepted", "pending_review", "rejected"]


class FactOutcome(BaseModel):
    index: int
    outcome: Outcome
    claim_id: UUID | None = None
    status: ClaimStatus | None = None
    audience: Audience | None = None
    reason_code: str | None = None
    message: str | None = None


class AddFactsResult(BaseModel):
    results: list[FactOutcome]


class CategorySetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: Audience | None = None
    require_review: bool | None = None


class PolicyOut(BaseModel):
    version: int
    approved_at: datetime
    public_opt_in: bool
    share_provisional: bool
    require_review_all: bool
    enrichment_enabled: bool
    category_settings: dict[str, CategorySetting]


class CategoryOut(BaseModel):
    id: str
    label: str
    default_audience: Audience
    public_eligible: bool
    effective_audience: Audience
    allowed_audiences: list[Audience]
    require_review: bool
    details_schema: dict[str, Any] | None


class PolicyDefaults(BaseModel):
    """The flags that apply while there is no approved policy (``policy`` is null)."""

    public_opt_in: bool = False
    share_provisional: bool = True
    require_review_all: bool = False
    enrichment_enabled: bool = True


class AudienceOut(BaseModel):
    id: Audience
    label: str


AUDIENCE_LABELS: dict[Audience, str] = {
    Audience.ONLY_ME: "Only me",
    Audience.TRUSTED_CIRCLE: "Trusted Circle",
    Audience.CONNECTIONS: "Connections",
    Audience.PUBLIC: "Public",
}


def _audiences() -> list[AudienceOut]:
    """Audiences narrowest first, with display labels."""
    return [AudienceOut(id=a, label=AUDIENCE_LABELS[a]) for a in reversed(Audience)]


class PolicyResponse(BaseModel):
    policy: PolicyOut | None
    # Set only while ``policy`` is null: the defaults that then apply.
    defaults: PolicyDefaults | None = None
    audiences: list[AudienceOut] = Field(default_factory=_audiences)
    # Shown with provisional sharing: provisional facts are shared with a caveat.
    provisional_caveat: str = (
        "Provisional facts (inferred or low confidence) are shared with a caveat that they are "
        "not yet confirmed by you, unless you turn provisional sharing off."
    )
    categories: list[CategoryOut]


class PolicyUpdate(BaseModel):
    """``PUT /api/ui/profile/policy``. Omitted fields keep their current values."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int | None = None
    public_opt_in: bool | None = None
    share_provisional: bool | None = None
    require_review_all: bool | None = None
    enrichment_enabled: bool | None = None
    category_settings: dict[str, CategorySetting] | None = None


class FactOut(BaseModel):
    id: UUID
    category: str
    statement: str
    details: dict[str, Any] | None
    audience: Audience
    status: ClaimStatus
    provisional: bool
    evidence_type: EvidenceType
    confidence: Confidence
    observed_at: date | None
    observed_precision: ObservedPrecision
    contributed_via: str
    contributor: str
    supersedes_id: UUID | None
    created_at: datetime
    updated_at: datetime


class RecentEvent(BaseModel):
    claim_id: UUID
    action: str
    category: str | None
    audience: Audience | None
    acceptance: AcceptanceMode | None
    actor: str
    occurred_at: datetime


class SummaryCounts(BaseModel):
    by_status: dict[str, int]
    by_audience: dict[str, int]


class SummaryOut(BaseModel):
    since: datetime
    counts: SummaryCounts
    pending_review: int
    recent: list[RecentEvent]


class ProfileOut(BaseModel):
    profile_id: UUID | None
    policy_version: int | None
    policy: PolicyOut | None
    # Set only while ``policy`` is null (see ``PolicyResponse.defaults``).
    defaults: PolicyDefaults | None
    facts: list[FactOut]
    summary: SummaryOut


class FactPatch(BaseModel):
    """``PATCH /api/ui/profile/facts/{id}``: audience in place, or a correction (statement,
    details or date) that creates an explicitly accepted replacement."""

    model_config = ConfigDict(extra="forbid")

    audience: Audience | None = None
    statement: str | None = None
    details: dict[str, Any] | None = None
    observed_at: date | None = None
    observed_precision: ObservedPrecision | None = None


# --- Policy -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Current contribution policy, or the defaults when there is no approved policy."""

    version: int | None = None
    approved_at: datetime | None = None
    public_opt_in: bool = False
    share_provisional: bool = True
    require_review_all: bool = False
    enrichment_enabled: bool = True
    category_settings: dict[str, dict[str, Any]] = field(default_factory=dict)
    questions_enabled: bool = True
    question_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.version is not None

    def setting(self, category: str) -> dict[str, Any]:
        value = self.category_settings.get(category)
        return value if isinstance(value, dict) else {}

    def effective_audience(self, spec: CategorySpec) -> Audience:
        """Category setting, else Public for eligible categories while opted in, else default."""
        configured = self.setting(spec.id).get("audience")
        if configured is not None:
            audience = Audience(configured)
            if audience is not Audience.PUBLIC or self.public_allowed(spec):
                return audience
        if self.public_allowed(spec):
            return Audience.PUBLIC
        return spec.default_audience

    def public_allowed(self, spec: CategorySpec) -> bool:
        return spec.public_eligible and self.public_opt_in

    def requires_review(self, spec: CategorySpec) -> bool:
        return self.require_review_all or bool(self.setting(spec.id).get("require_review"))

    def audience_allowed(self, spec: CategorySpec, audience: Audience) -> bool:
        """Whether a fact of this category may have ``audience`` at all: Public needs
        eligibility and the opt-in; ``sensitive`` facts are Only Me, always."""
        if spec.sensitive:
            return audience is Audience.ONLY_ME
        return audience is not Audience.PUBLIC or self.public_allowed(spec)


def _policy_view(row: ContributionPolicy | None) -> PolicyView:
    if row is None:
        return PolicyView()
    return PolicyView(
        version=row.version,
        approved_at=row.approved_at,
        public_opt_in=row.public_opt_in,
        share_provisional=row.share_provisional,
        require_review_all=row.require_review_all,
        enrichment_enabled=row.enrichment_enabled,
        category_settings=dict(row.category_settings or {}),
        questions_enabled=row.questions_enabled,
        question_settings=dict(row.question_settings or {}),
    )


def _current_policy_row(session: Session, profile_id: UUID) -> ContributionPolicy | None:
    return (
        session.execute(
            select(ContributionPolicy)
            .where(ContributionPolicy.profile_id == profile_id)
            .order_by(ContributionPolicy.version.desc())
            .limit(1)
        )
        .scalars()
        .one_or_none()
    )


def _policy_response(view: PolicyView) -> PolicyResponse:
    policy = None
    if view.approved:
        assert view.version is not None and view.approved_at is not None
        policy = PolicyOut(
            version=view.version,
            approved_at=view.approved_at,
            public_opt_in=view.public_opt_in,
            share_provisional=view.share_provisional,
            require_review_all=view.require_review_all,
            enrichment_enabled=view.enrichment_enabled,
            category_settings={
                k: CategorySetting.model_validate(v)
                for k, v in view.category_settings.items()
                if k in CATEGORY_BY_ID and isinstance(v, dict)
            },
        )
    return PolicyResponse(
        policy=policy,
        defaults=PolicyDefaults() if policy is None else None,
        categories=[
            CategoryOut(
                id=spec.id,
                label=spec.label,
                default_audience=spec.default_audience,
                public_eligible=spec.public_eligible,
                effective_audience=view.effective_audience(spec),
                allowed_audiences=list(spec.allowed_audiences(public_opt_in=view.public_opt_in)),
                require_review=view.requires_review(spec),
                details_schema=spec.details_schema,
            )
            for spec in CATEGORIES
        ],
    )


def _audience_refusal(spec: CategorySpec, audience: Audience) -> str:
    """Why ``audience`` is not allowed for a fact of ``spec`` (``audience_allowed`` failed)."""
    if spec.sensitive:
        return "Sensitive facts are always Only Me"
    if audience is Audience.PUBLIC and not spec.public_eligible:
        return "This category can never be Public"
    return "Public needs the owner's Public opt-in"


def _is_owner_ui(principal: PrincipalLike) -> bool:
    return principal.via == "ui" and principal.delegation_id is None


def _actor(principal: PrincipalLike) -> claim_writes.Actor:
    return claim_writes.Actor(
        user_id=principal.user_id, delegation_id=principal.delegation_id, via=principal.via
    )


def _validate_policy_update(update: PolicyUpdate, old: PolicyView) -> PolicyView:
    """The new policy (omitted fields carried forward) or ``PolicyInvalid``."""
    public_opt_in = old.public_opt_in if update.public_opt_in is None else update.public_opt_in
    errors: list[dict[str, Any]] = []
    if update.category_settings is None:
        # Carried forward; a withdrawn opt-in drops Public category audiences.
        settings = {
            k: dict(v)
            for k, v in old.category_settings.items()
            if k in CATEGORY_BY_ID and isinstance(v, dict)
        }
        if not public_opt_in:
            for value in settings.values():
                if value.get("audience") == Audience.PUBLIC.value:
                    value.pop("audience")
    else:
        settings = {}
        for key, setting in update.category_settings.items():
            loc = ["category_settings", key]
            spec = CATEGORY_BY_ID.get(key)
            if spec is None:
                errors.append({"loc": loc, "msg": "unknown category"})
                continue
            value: dict[str, Any] = {}
            if setting.audience is not None:
                allowed = spec.allowed_audiences(public_opt_in=public_opt_in)
                if setting.audience not in allowed:
                    errors.append(
                        {
                            "loc": [*loc, "audience"],
                            "msg": "audience not allowed for this category"
                            + ("" if public_opt_in else " (Public needs public_opt_in)"),
                        }
                    )
                    continue
                value["audience"] = setting.audience.value
            if setting.require_review is not None:
                value["require_review"] = setting.require_review
            if value:
                settings[key] = value
    if errors:
        raise PolicyInvalid(errors)
    return PolicyView(
        public_opt_in=public_opt_in,
        share_provisional=(
            old.share_provisional if update.share_provisional is None else update.share_provisional
        ),
        require_review_all=(
            old.require_review_all
            if update.require_review_all is None
            else update.require_review_all
        ),
        enrichment_enabled=(
            old.enrichment_enabled
            if update.enrichment_enabled is None
            else update.enrichment_enabled
        ),
        category_settings=settings,
        # F7-owned fields are carried forward unchanged.
        questions_enabled=old.questions_enabled,
        question_settings=old.question_settings,
    )


# --- Profiles ---------------------------------------------------------------------------------


def _profile_id_for(session: Session, user_id: UUID) -> UUID | None:
    return session.execute(
        select(Profile.id).where(Profile.user_id == user_id)
    ).scalar_one_or_none()


async def ensure_profile(session: Session, *, user_id: UUID) -> UUID:
    """Get or create the user's profile in the caller's transaction (race-safe ``INSERT ... ON
    CONFLICT (user_id) DO NOTHING``). On creation calls F4's ``on_profile_created`` with this
    session before returning. Never commits: the caller commits (or rolls back)."""

    def get_or_insert() -> tuple[UUID, bool]:
        existing = _profile_id_for(session, user_id)
        if existing is not None:
            return existing, False
        inserted = session.execute(
            pg_insert(Profile)
            .values(user_id=user_id)
            .on_conflict_do_nothing(index_elements=[Profile.user_id])
            .returning(Profile.id)
        ).scalar_one_or_none()
        if inserted is not None:
            return inserted, True
        # A concurrent transaction created it (and committed: ON CONFLICT waited for it).
        found = _profile_id_for(session, user_id)
        assert found is not None
        return found, False

    profile_id, created = await _in_thread(get_or_insert)
    if created:
        await _authz_call(on_profile_created(session, profile_id=profile_id, user_id=user_id))
    return profile_id


async def _ensure_profile_committed(session: Session, user_id: UUID) -> UUID:
    """``ensure_profile`` in its own short transaction (committed before authorization)."""
    try:
        profile_id = await ensure_profile(session, user_id=user_id)
        await _in_thread(session.commit)
    except BaseException:
        await _in_thread(session.rollback)
        raise
    return profile_id


async def _existing_profile(session: Session, user_id: UUID) -> UUID | None:
    def read() -> UUID | None:
        try:
            return _profile_id_for(session, user_id)
        finally:
            session.rollback()  # end the read transaction; nothing was written

    return await _in_thread(read)


def _lock_profile(session: Session, profile_id: UUID, *, exclusive: bool) -> None:
    stmt = select(Profile.id).where(Profile.id == profile_id)
    stmt = stmt.with_for_update() if exclusive else stmt.with_for_update(read=True)
    session.execute(stmt).scalar_one()


# --- Policy operations ------------------------------------------------------------------------


async def get_policy(session: Session, principal: PrincipalLike) -> PolicyResponse:
    """Categories and the caller's current policy (``policy: null`` before the first approval).
    Read-only: never creates a profile."""
    profile_id = await _existing_profile(session, principal.user_id)
    if profile_id is None:
        return _policy_response(PolicyView())
    await _authz_call(authz.require_contribute(None, principal, profile_id))
    return await _in_thread(lambda: _read_policy_response(session, profile_id))


def _read_policy_response(session: Session, profile_id: UUID) -> PolicyResponse:
    try:
        return _policy_response(_policy_view(_current_policy_row(session, profile_id)))
    finally:
        session.rollback()


async def effective_max_audience(session: Session, profile_id: UUID, category: str) -> Audience:
    """Widest audience the owner's CURRENT policy allows for the category without review.
    Raises ``UnknownCategory`` / ``ForbiddenCategory``. The caller authorizes."""
    spec = validate_category(category)
    view = await _in_thread(lambda: _policy_view(_current_policy_row(session, profile_id)))
    return view.effective_audience(spec)


async def set_policy(
    session: Session, principal: PrincipalLike, update: PolicyUpdate
) -> PolicyResponse:
    """Approve or change the policy (owner in the browser only). Appends version max+1;
    ``expected_version`` must equal the current version (``None`` before the first)."""
    if not _is_owner_ui(principal):
        raise NotAllowed()
    profile_id = await _ensure_profile_committed(session, principal.user_id)
    await _authz_call(authz.require_contribute(None, principal, profile_id))
    actor = _actor(principal)

    def write() -> tuple[PolicyView, PolicyView] | PolicyResponse:
        _lock_profile(session, profile_id, exclusive=True)
        old = _policy_view(_current_policy_row(session, profile_id))
        if old.version != update.expected_version:
            return _policy_response(old)
        new = _validate_policy_update(update, old)
        version = (old.version or 0) + 1
        session.add(
            ContributionPolicy(
                profile_id=profile_id,
                version=version,
                approved_by_user_id=principal.user_id,
                public_opt_in=new.public_opt_in,
                share_provisional=new.share_provisional,
                require_review_all=new.require_review_all,
                enrichment_enabled=new.enrichment_enabled,
                questions_enabled=new.questions_enabled,
                question_settings=new.question_settings,
                category_settings=new.category_settings,
            )
        )
        session.flush()
        # Tightened category audiences apply to existing facts too (explicit owner change).
        for spec in CATEGORIES:
            before, after = old.effective_audience(spec), new.effective_audience(spec)
            if after.rank > before.rank:
                claim_writes.narrow_category(
                    session,
                    profile_id=profile_id,
                    category=spec.id,
                    audience=after,
                    actor=actor,
                    policy_version=version,
                )
        return old, new

    try:
        result = await _in_thread(write)
        if isinstance(result, PolicyResponse):
            raise PolicyVersionConflict(result)
        old, new = result
        if old.public_opt_in and not new.public_opt_in:
            # Revoke-class: after the INSERT, before commit; a failure rolls the version back.
            await _authz_call(authz.on_public_opt_in_changed(profile_id=profile_id, enabled=False))
        await _in_thread(session.commit)
    except BaseException:
        await _in_thread(session.rollback)
        raise
    if new.public_opt_in and not old.public_opt_in:
        await _grant_public_after_commit(session, profile_id)
    return await _in_thread(lambda: _read_policy_response(session, profile_id))


async def _grant_public_after_commit(session: Session, profile_id: UUID) -> None:
    """Post-commit Public grant, serialized with later policy changes: hold the profile row
    ``FOR UPDATE`` while re-reading the current policy and granting only if it is still opted
    in. A withdrawal committed first is never overtaken; one started later waits for the grant.
    Never raises (the policy is committed; ``fga_reconcile`` repairs a missing grant)."""
    try:

        def still_opted_in() -> bool:
            _lock_profile(session, profile_id, exclusive=True)
            row = _current_policy_row(session, profile_id)
            return bool(row and row.public_opt_in)

        if await _in_thread(still_opted_in):
            # Grant-class: logs and returns None on failure.
            await authz.on_public_opt_in_changed(profile_id=profile_id, enabled=True)
    except Exception as exc:  # pragma: no cover - defensive; logged, never fails the write
        logger.error("Public opt-in grant step failed (%s)", type(exc).__name__)
    finally:
        await _in_thread(session.rollback)


# --- Contributions ----------------------------------------------------------------------------


class _Rejected(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


def _coerce(raw: FactInput | Mapping[str, Any]) -> FactInput:
    if isinstance(raw, FactInput):
        return raw
    try:
        return FactInput.model_validate(raw)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) or "fact" for e in exc.errors()})
        raise _Rejected("invalid_field", f"Invalid fields: {', '.join(fields)}") from None


def _check_statement(spec: CategorySpec, statement: str) -> str:
    text = statement.strip()
    if not 1 <= len(text) <= STATEMENT_MAX_LENGTH:
        raise InvalidField(f"statement must be 1-{STATEMENT_MAX_LENGTH} characters")
    return validate_statement(spec, text)


def _observed(
    observed_at: date | None, precision: ObservedPrecision | None
) -> tuple[date | None, ObservedPrecision]:
    if observed_at is None:
        if precision not in (None, ObservedPrecision.UNKNOWN):
            raise InvalidField("observed_precision needs observed_at")
        return None, ObservedPrecision.UNKNOWN
    if precision is ObservedPrecision.UNKNOWN:
        raise InvalidField("observed_precision 'unknown' means there is no observed_at")
    if observed_at > datetime.now(UTC).date() + timedelta(days=1):
        raise InvalidField("observed_at is in the future")
    return observed_at, precision or ObservedPrecision.DAY


def _accepted_status(fact: FactInput) -> ClaimStatus:
    if fact.evidence_type is EvidenceType.INFERRED or fact.confidence is Confidence.LOW:
        return ClaimStatus.PROVISIONAL
    return ClaimStatus.ACTIVE


def _plan_fact(
    principal: PrincipalLike, policy: PolicyView, fact: FactInput
) -> claim_writes.NewFact:
    """Validate one fact and decide its status under ``policy`` (raises ``_Rejected``)."""
    try:
        spec = validate_category(fact.category)
        statement = _check_statement(spec, fact.statement)
        details = validate_details(spec, fact.details)
        observed_at, precision = _observed(fact.observed_at, fact.observed_precision)
    except CategoryError as exc:
        raise _Rejected(exc.reason_code, exc.message) from None
    effective = policy.effective_audience(spec)
    # Facts involving other people: tightest plausible audience unless one is given.
    audience = fact.audience or (Audience.ONLY_ME if fact.involves_others else effective)
    if not policy.audience_allowed(spec, audience):
        raise _Rejected("invalid_field", _audience_refusal(spec, audience))
    acceptance: AcceptanceMode | None
    owner_ui = _is_owner_ui(principal)
    if not owner_ui and policy.approved and not policy.enrichment_enabled:
        raise _Rejected("enrichment_disabled", "The owner has turned contributions off")
    if spec.sensitive:
        # Volunteered by the owner only, and always held until the owner approves in Revel.
        if fact.evidence_type is not EvidenceType.EXPLICIT:
            raise _Rejected(
                "sensitive_not_explicit",
                "Sensitive information is saved only when the user volunteered it",
            )
        status, acceptance = ClaimStatus.PENDING_REVIEW, None
    elif owner_ui:
        # The owner's own entry is explicitly accepted.
        status, acceptance = _accepted_status(fact), AcceptanceMode.EXPLICIT
    else:
        if (
            fact.involves_others
            or not policy.approved
            or audience.rank < effective.rank
            or policy.requires_review(spec)
        ):
            status, acceptance = ClaimStatus.PENDING_REVIEW, None
        else:
            status, acceptance = _accepted_status(fact), AcceptanceMode.AUTOMATIC
    return claim_writes.NewFact(
        category=spec.id,
        statement=statement,
        details=details,
        audience=audience,
        evidence_type=fact.evidence_type.value,
        confidence=fact.confidence.value,
        observed_at=observed_at,
        observed_precision=precision.value,
        status=status,
        acceptance=acceptance,
        policy_version=policy.version,
        supersedes_id=fact.supersedes_id,
    )


async def add_facts(
    session: Session,
    principal: PrincipalLike,
    facts: Sequence[FactInput | Mapping[str, Any]],
) -> AddFactsResult:
    """Contribute 1-20 facts to the principal's own profile (created if needed); commits.

    Each item gets its own outcome; a rejected item never fails the batch. Contributors are
    the owner (browser, explicit acceptance) and the owner's own agents (MCP/API, accepted
    automatically within the current policy, otherwise held for review)."""
    if not 1 <= len(facts) <= MAX_FACTS_PER_BATCH:
        raise InvalidRequest(
            [{"loc": ["facts"], "msg": f"send 1-{MAX_FACTS_PER_BATCH} facts per call"}]
        )
    profile_id = await _ensure_profile_committed(session, principal.user_id)
    await _authz_call(authz.require_contribute(None, principal, profile_id))
    actor = _actor(principal)

    def write() -> list[FactOutcome]:
        _lock_profile(session, profile_id, exclusive=False)
        policy = _policy_view(_current_policy_row(session, profile_id))
        # Lock every supersede target up front in id order, so two batches naming the same
        # facts in opposite orders queue instead of deadlocking.
        claim_writes.lock_claims(
            session,
            profile_id,
            {
                item.supersedes_id
                for item in facts
                if isinstance(item, FactInput) and item.supersedes_id is not None
            }
            | {
                sid
                for item in facts
                if not isinstance(item, FactInput)
                and (sid := _uuid_or_none(item.get("supersedes_id"))) is not None
            },
        )
        results: list[FactOutcome] = []
        for index, raw in enumerate(facts):
            try:
                item = _coerce(raw)
                planned = _plan_fact(principal, policy, item)
            except _Rejected as exc:
                results.append(_rejected(index, exc.reason_code, exc.message))
                continue
            savepoint = session.begin_nested()
            try:
                claim = claim_writes.insert_fact(
                    session, profile_id=profile_id, actor=actor, fact=planned
                )
                savepoint.commit()
            except claim_writes.SupersedesNotLive:
                savepoint.rollback()
                results.append(
                    _rejected(index, "supersedes_not_found", "supersedes_id is not a live fact")
                )
                continue
            except IntegrityError:
                savepoint.rollback()
                results.append(
                    _rejected(index, "invalid_field", "The fact conflicts with a concurrent change")
                )
                continue
            status = ClaimStatus(claim.status)
            results.append(
                FactOutcome(
                    index=index,
                    outcome="pending_review"
                    if status is ClaimStatus.PENDING_REVIEW
                    else "accepted",
                    claim_id=claim.id,
                    status=status,
                    audience=Audience(claim.audience),
                    message=_outcome_message(status, involves_others=item.involves_others),
                )
            )
        session.commit()
        return results

    try:
        return AddFactsResult(results=await _in_thread(write))
    except BaseException:
        await _in_thread(session.rollback)
        raise


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def _rejected(index: int, reason_code: str, message: str) -> FactOutcome:
    return FactOutcome(index=index, outcome="rejected", reason_code=reason_code, message=message)


def _outcome_message(status: ClaimStatus, *, involves_others: bool = False) -> str | None:
    if status is ClaimStatus.PENDING_REVIEW and involves_others:
        return "Awaits the owner's review because it involves other people; not shared until then."
    if status is ClaimStatus.PENDING_REVIEW:
        return "Held for the owner's review; not shared until approved."
    if status is ClaimStatus.PROVISIONAL:
        return "Accepted as provisional; shared with a caveat while provisional sharing is on."
    return None


# --- Owner reads ------------------------------------------------------------------------------


def _agent_names(session: Session, delegation_ids: Iterable[UUID]) -> dict[UUID, str | None]:
    ids = {d for d in delegation_ids if d is not None}
    if not ids:
        return {}
    rows = session.execute(
        select(AgentDelegation.id, Agent.name)
        .join(Agent, Agent.id == AgentDelegation.agent_id)
        .where(AgentDelegation.id.in_(ids))
    ).all()
    return {row.id: row.name for row in rows}


def _actor_label(
    owner_id: UUID, user_id: UUID, delegation_id: UUID | None, names: Mapping[UUID, str | None]
) -> str:
    if delegation_id is None:
        return "you" if user_id == owner_id else "someone else"
    return names.get(delegation_id) or "Your assistant"


def _fact_out(claim: Claim, owner_id: UUID, names: Mapping[UUID, str | None]) -> FactOut:
    status = ClaimStatus(claim.status)
    return FactOut(
        id=claim.id,
        category=claim.category,
        statement=claim.statement,
        details=claim.details,
        audience=Audience(claim.audience),
        status=status,
        provisional=status is ClaimStatus.PROVISIONAL,
        evidence_type=EvidenceType(claim.evidence_type),
        confidence=Confidence(claim.confidence),
        observed_at=claim.observed_at,
        observed_precision=ObservedPrecision(claim.observed_precision),
        contributed_via=claim.contributed_via,
        contributor=_actor_label(
            owner_id, claim.contributor_user_id, claim.contributor_delegation_id, names
        ),
        supersedes_id=claim.supersedes_id,
        created_at=claim.created_at,
        updated_at=claim.updated_at,
    )


def _facts_out(session: Session, claims: Sequence[Claim], owner_id: UUID) -> list[FactOut]:
    names = _agent_names(session, (c.contributor_delegation_id for c in claims))
    return [_fact_out(c, owner_id, names) for c in claims]


def _empty_summary(since: datetime) -> SummaryOut:
    return SummaryOut(
        since=since,
        counts=SummaryCounts(by_status={}, by_audience={}),
        pending_review=0,
        recent=[],
    )


async def _summary(
    session: Session, principal: PrincipalLike, profile_id: UUID, since: datetime
) -> SummaryOut:
    """Counts (all facts by status; live facts by audience), pending reviews and recent
    events. Events carry no statement text."""
    every = await _authz_call(owner_claims(session, principal, profile_id))
    live = await _authz_call(
        owner_claims(session, principal, profile_id, statuses=LIVE_CLAIM_STATUSES)
    )
    by_status: dict[str, int] = {}
    for claim in every:
        by_status[claim.status] = by_status.get(claim.status, 0) + 1
    by_audience: dict[str, int] = {}
    for claim in live:
        by_audience[claim.audience] = by_audience.get(claim.audience, 0) + 1
    category_of = {c.id: c.category for c in every}

    def events() -> list[RecentEvent]:
        rows = (
            session.execute(
                select(ClaimEvent)
                .where(ClaimEvent.profile_id == profile_id, ClaimEvent.occurred_at >= since)
                .order_by(ClaimEvent.occurred_at.desc(), ClaimEvent.id)
                .limit(SUMMARY_RECENT_LIMIT)
            )
            .scalars()
            .all()
        )
        names = _agent_names(session, (r.actor_delegation_id for r in rows))
        out = []
        for row in rows:
            audience = None
            changes = row.changes or {}
            if isinstance(changes.get("audience"), dict):
                audience = changes["audience"].get("new")
            out.append(
                RecentEvent(
                    claim_id=row.claim_id,
                    action=row.action,
                    category=category_of.get(row.claim_id),
                    audience=Audience(audience) if audience else None,
                    acceptance=AcceptanceMode(row.acceptance) if row.acceptance else None,
                    actor=_actor_label(
                        principal.user_id, row.actor_user_id, row.actor_delegation_id, names
                    ),
                    occurred_at=row.occurred_at,
                )
            )
        session.rollback()
        return out

    recent = await _in_thread(events)
    return SummaryOut(
        since=since,
        counts=SummaryCounts(by_status=by_status, by_audience=by_audience),
        pending_review=by_status.get(ClaimStatus.PENDING_REVIEW.value, 0),
        recent=recent,
    )


def _since(since: datetime | None) -> datetime:
    if since is None:
        return datetime.now(UTC) - DEFAULT_SUMMARY_WINDOW
    return since if since.tzinfo else since.replace(tzinfo=UTC)


async def get_my_profile(
    session: Session,
    principal: PrincipalLike,
    *,
    statuses: Sequence[ClaimStatus] | None = None,
    since: datetime | None = None,
) -> ProfileOut:
    """The owner's facts (default live and pending; any statuses on request) with audience and
    status, the current policy and a contribution summary. Never creates a profile."""
    since = _since(since)
    profile_id = await _existing_profile(session, principal.user_id)
    if profile_id is None:
        return ProfileOut(
            profile_id=None,
            policy_version=None,
            policy=None,
            defaults=PolicyDefaults(),
            facts=[],
            summary=_empty_summary(since),
        )
    claims = await _authz_call(
        owner_claims(session, principal, profile_id, statuses=statuses or DEFAULT_FACT_STATUSES)
    )

    def read() -> tuple[list[FactOut], PolicyResponse]:
        try:
            facts = _facts_out(session, claims, principal.user_id)
            policy = _policy_response(_policy_view(_current_policy_row(session, profile_id)))
            return facts, policy
        finally:
            session.rollback()

    facts, policy = await _in_thread(read)
    summary = await _summary(session, principal, profile_id, since)
    return ProfileOut(
        profile_id=profile_id,
        policy_version=policy.policy.version if policy.policy else None,
        policy=policy.policy,
        defaults=policy.defaults,
        facts=facts,
        summary=summary,
    )


async def get_summary(
    session: Session, principal: PrincipalLike, *, since: datetime | None = None
) -> SummaryOut:
    since = _since(since)
    profile_id = await _existing_profile(session, principal.user_id)
    if profile_id is None:
        return _empty_summary(since)
    return await _summary(session, principal, profile_id, since)


# --- Fact changes -----------------------------------------------------------------------------


async def _authorized_profile(session: Session, principal: PrincipalLike) -> UUID:
    profile_id = await _existing_profile(session, principal.user_id)
    if profile_id is None:
        raise FactNotFound()
    await _authz_call(authz.require_contribute(None, principal, profile_id))
    return profile_id


async def _change_fact(
    session: Session,
    principal: PrincipalLike,
    change: Callable[[UUID, PolicyView], Claim],
) -> FactOut:
    """Run one claim change under the profile's ``FOR SHARE`` lock and current policy; commit.
    Maps a missing/foreign fact to ``FactNotFound`` and a state conflict to ``FactConflict``."""
    profile_id = await _authorized_profile(session, principal)

    def work() -> FactOut | FactConflict | ProfileServiceError:
        try:
            _lock_profile(session, profile_id, exclusive=False)
            policy = _policy_view(_current_policy_row(session, profile_id))
            claim = change(profile_id, policy)
            out = _facts_out(session, [claim], principal.user_id)[0]
            session.commit()
            return out
        except claim_writes.ClaimNotFound:
            session.rollback()
            return FactNotFound()
        except claim_writes.ClaimStateConflict as exc:
            fact = _facts_out(session, [exc.claim], principal.user_id)[0]
            session.rollback()
            return FactConflict(fact, exc.reason)
        except ProfileServiceError as exc:
            session.rollback()
            return exc
        except BaseException:
            session.rollback()
            raise

    result = await _in_thread(work)
    if isinstance(result, ProfileServiceError):
        raise result
    return result


def _spec_of(claim: Claim) -> CategorySpec:
    spec = CATEGORY_BY_ID.get(claim.category)
    if spec is None:  # not from F5's list (e.g. legacy data): treat as unchangeable
        raise InvalidRequest([{"loc": ["category"], "msg": "unknown category"}])
    return spec


async def set_fact_audience(
    session: Session, principal: PrincipalLike, claim_id: UUID, audience: Audience
) -> FactOut:
    """Change a fact's audience in place. Agents may only narrow (403 otherwise); the owner in
    the browser may also widen, explicitly, within the category's allowed audiences."""
    owner_ui = _is_owner_ui(principal)

    def change(profile_id: UUID, policy: PolicyView) -> Claim:
        def check(claim: Claim) -> None:
            if Audience(claim.audience).rank > audience.rank and not owner_ui:
                raise NotAllowed()
            spec = _spec_of(claim)
            if not policy.audience_allowed(spec, audience):
                raise InvalidRequest(
                    [{"loc": ["body", "audience"], "msg": _audience_refusal(spec, audience)}]
                )

        return claim_writes.set_audience(
            session,
            profile_id=profile_id,
            claim_id=claim_id,
            actor=_actor(principal),
            audience=audience,
            acceptance=AcceptanceMode.EXPLICIT if owner_ui else AcceptanceMode.AUTOMATIC,
            policy_version=policy.version,
            check=check,
        )

    return await _change_fact(session, principal, change)


async def remove_fact(session: Session, principal: PrincipalLike, claim_id: UUID) -> FactOut:
    """Remove (undo) a live or pending fact; the owner or the owner's agent."""
    owner_ui = _is_owner_ui(principal)

    def change(profile_id: UUID, policy: PolicyView) -> Claim:
        return claim_writes.remove(
            session,
            profile_id=profile_id,
            claim_id=claim_id,
            actor=_actor(principal),
            acceptance=AcceptanceMode.EXPLICIT if owner_ui else AcceptanceMode.AUTOMATIC,
            policy_version=policy.version,
        )

    return await _change_fact(session, principal, change)


async def approve_fact(session: Session, principal: PrincipalLike, claim_id: UUID) -> FactOut:
    """Owner (browser) approves a pending fact; a replacement retires its target atomically."""
    if not _is_owner_ui(principal):
        raise NotAllowed()

    def change(profile_id: UUID, policy: PolicyView) -> Claim:
        def check(claim: Claim) -> None:
            if not policy.audience_allowed(_spec_of(claim), Audience(claim.audience)):
                raise claim_writes.ClaimStateConflict(claim, "audience_not_allowed")

        return claim_writes.approve(
            session,
            profile_id=profile_id,
            claim_id=claim_id,
            actor=_actor(principal),
            policy_version=policy.version,
            check=check,
        )

    return await _change_fact(session, principal, change)


async def reject_fact(session: Session, principal: PrincipalLike, claim_id: UUID) -> FactOut:
    """Owner (browser) rejects a pending fact; a replacement's target stays live."""
    if not _is_owner_ui(principal):
        raise NotAllowed()

    def change(profile_id: UUID, policy: PolicyView) -> Claim:
        return claim_writes.reject(
            session,
            profile_id=profile_id,
            claim_id=claim_id,
            actor=_actor(principal),
            policy_version=policy.version,
        )

    return await _change_fact(session, principal, change)


async def patch_fact(
    session: Session, principal: PrincipalLike, claim_id: UUID, patch: FactPatch
) -> FactOut:
    """Owner (browser) edit: audience-only changes update in place; statement, details or date
    changes create an explicitly accepted replacement that supersedes the old fact atomically."""
    if not _is_owner_ui(principal):
        raise NotAllowed()
    fields = patch.model_fields_set
    correction = fields & {"statement", "details", "observed_at", "observed_precision"}
    if not correction:
        if patch.audience is None:
            raise InvalidRequest([{"loc": ["body"], "msg": "nothing to change"}])
        return await set_fact_audience(session, principal, claim_id, patch.audience)

    def change(profile_id: UUID, policy: PolicyView) -> Claim:
        def build(old: Claim) -> claim_writes.NewFact:
            spec = _spec_of(old)
            errors: list[dict[str, Any]] = []

            def field_error(loc: str, exc: CategoryError) -> None:
                errors.append({"loc": ["body", loc], "msg": exc.message})

            statement = old.statement
            if "statement" in fields:
                try:
                    if patch.statement is None:
                        raise InvalidField("statement must be 1-500 characters")
                    statement = _check_statement(spec, patch.statement)
                except CategoryError as exc:
                    field_error("statement", exc)
            details = old.details
            if "details" in fields:
                try:
                    details = validate_details(spec, patch.details)
                except CategoryError as exc:
                    field_error("details", exc)
            observed_at = old.observed_at
            precision = ObservedPrecision(old.observed_precision)
            if fields & {"observed_at", "observed_precision"}:
                try:
                    observed_at, precision = _observed(patch.observed_at, patch.observed_precision)
                except CategoryError as exc:
                    field_error("observed_at", exc)
            audience = patch.audience or Audience(old.audience)
            if not policy.audience_allowed(spec, audience):
                errors.append(
                    {"loc": ["body", "audience"], "msg": _audience_refusal(spec, audience)}
                )
            if errors:
                raise InvalidRequest(errors)
            return claim_writes.NewFact(
                category=old.category,
                statement=statement,
                details=details,
                audience=audience,
                # The owner stated it: explicit and confirmed.
                evidence_type=EvidenceType.EXPLICIT.value,
                confidence=Confidence.HIGH.value,
                observed_at=observed_at,
                observed_precision=precision.value,
                status=ClaimStatus.ACTIVE,
                acceptance=AcceptanceMode.EXPLICIT,
                policy_version=policy.version,
                supersedes_id=old.id,
            )

        return claim_writes.correct(
            session,
            profile_id=profile_id,
            claim_id=claim_id,
            actor=_actor(principal),
            build=build,
        )

    return await _change_fact(session, principal, change)
